# -*- coding: utf-8 -*-
"""llm_console.connection.optimize — 提示词优化：请求层（三路并行、非流式）。

消息形状与参数由 core/promptopt 出（前缀一致、仅末尾策略指令不同——为命中模型侧
prompt cache；官方页写明并发首轮不保证命中，受益的是"失败只重试那一路"与短时间
内的再次优化，见 core/promptopt 头注与 `11-云端接入.md` §9）。本层只管：

  · 目标解析（resolve_target）：优化模型由 cfg["prompt_opt_model"] 指定（W 2026-10-08
    定的口径：留空 = 跟随当前选中的文本模型，选中生图/生视频时回退第一个可用文本
    模型——本地优先，其次带密钥的云端文本模型；显式 = "local" 或 "pid::model"）。
  · 关思考参数按厂商官方页配置（坑 52 纪律：点名出处，见 _thinking_off）；端点不认
    该参数时按"400 + 回包点名 → 摘除重试一次"回退（坑 46 同族）——最坏是没关掉
    思考，绝不因此发不出请求。
  · 关不掉思考的模型（deepseek-reasoner / qwq 一族）：W 2026-10-08 定「照发 + 检测
    回报」——不按名字预判（与看图能力判定"不靠名字猜"同一纪律）；回包
    reasoning_content 非空即记 thinking_seen，正文被思考额度吃光（坑 49 形态）时
    如实报"换模型"。
  · 三路并行（threading，标准库）；单路失败只重试那一路（429/5xx 退避重试与网络
    错误同路上限 MAX_ATTEMPTS）；stop_flag 全程可打断。

铁律：厂商字段名只允许出现在 connection 层；本模块零 tkinter，可无头自检。
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..core import config, models, promptopt, providers, secrets
from . import cloud
from .cloud import MAX_ATTEMPTS, RETRY_STATUS, humanize_http, humanize_net
from .stream import REASONING_KEYS

# 本地 27B ≈3.7 tok/s（开发机基线），2048 额度最坏 ≈10 分钟——超时给足，
# 与 stream_worker 的"长思考要等得起"同一考虑
LOCAL_TIMEOUT = 1200


def _host_of(url):
    try:
        return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    except Exception:
        return ""


def _host_in(host, domain):
    """域名点边界匹配（防 evil-deepseek.com 混进来，同 providers.media_api 的口径）。"""
    return host == domain or host.endswith("." + domain)


def _thinking_off(provider):
    """该厂商"关思考"的请求参数（只认官方页核实过的形状，出处逐条点名）。

    · DeepSeek：thinking={"type":"disabled"}
      —— api-docs.deepseek.com/api/create-chat-completion（2026-10-08 核实）
    · 阿里云兼容模式：enable_thinking=false（HTTP 直发 = 请求体顶层字段）
      —— help.aliyun.com/zh/model-studio/deep-thinking（2026-10-08 核实；
      官方同页：仅思考模型"无法关闭"，参数不适用但仍会吐 reasoning_content → 检测兜底）
    · 智谱 GLM：thinking={"type":"disabled"}
      —— ⚠ 官方页 2026-10-08 未核实到该取值（本机未配置智谱，等真有 key 时按
      docs.bigmodel.cn 复核）；400 摘除回退兜底
    · 其余 OpenAI 兼容端：chat_template_kwargs.enable_thinking=false
      —— vLLM / SGLang 系网关与 llama-server 的通行约定（本项目
      stream.request_auto_alias 同手法）；不认就 400 摘除回退
    """
    p = provider or {}
    host = _host_of(p.get("base_url"))
    if _host_in(host, "deepseek.com"):
        return {"thinking": {"type": "disabled"}}
    if _host_in(host, "aliyuncs.com"):
        return {"enable_thinking": False}
    if _host_in(host, "bigmodel.cn") or _host_in(host, "zhipuai.cn"):
        return {"thinking": {"type": "disabled"}}
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _local_target(cfg):
    m = str(cfg.get("model") or "")
    label = "" if providers.is_cloud(cfg) else os.path.basename(m)
    return {
        "kind": "local", "provider": None,
        "url": config.base_url(cfg) + "/v1/chat/completions",
        "headers": config.api_headers(cfg),
        "model": "local",
        "timeout": LOCAL_TIMEOUT,
        "label": label or "本地模型",
        "thinking": {"chat_template_kwargs": {"enable_thinking": False}},
        # 与 stream_worker 同款：llama-server 的 prompt cache。本地引擎默认单槽，
        # 三路请求实际串行——但同前缀让第 2/3 路的 prefill 几乎免费（并行的时延
        # 收益只在云端成立，这条差异写进 `11` §9）
        "extra_body": {"cache_prompt": True},
    }


def _headers(provider, key):
    h = {"Content-Type": "application/json",
         "Authorization": "Bearer %s" % key,
         "User-Agent": config.USER_AGENT}
    h.update(provider.get("extra_headers") or {})
    return h


def _cloud_target(cfg, pid, model):
    p = providers.get_provider(cfg, pid)
    if p is None:
        return None, "优化模型指定的服务商「%s」不存在或已停用。" % pid
    if providers.model_kind_of(p, model) != providers.KIND_TEXT:
        return None, "「%s」不是文本模型——提示词优化只能走文本链路（别的会发错协议）。" % model
    key = secrets.get_api_key(pid)
    if not key:
        return None, "还没给「%s」填 API Key（设置 → 云端模型 → 服务商与密钥）。" % p.get("name")
    return {
        "kind": "cloud", "provider": p,
        "url": providers.chat_completions_url(p),
        "headers": _headers(p, key),
        "model": model,
        "timeout": int(p.get("timeout", 600)),
        "label": "%s／%s" % (p.get("name"), model),
        "thinking": _thinking_off(p),
        "extra_body": {},
    }, ""


def resolve_target(cfg):
    """优化模型的目标解析 → (target, "") 或 (None, 人话原因)。判据只写这一处。"""
    spec = str(cfg.get("prompt_opt_model", "") or "").strip()
    if spec and spec != "local":
        sp = providers.split_cloud_id(spec)
        if sp:
            return _cloud_target(cfg, sp[0], sp[1])
        return None, ("优化模型指定值不认识：%r（留空 = 跟随当前文本模型；"
                      "或填 local / 云端模型）。" % spec)
    if spec == "local":
        return _local_target(cfg), ""
    # 留空 = 自动跟随（W 2026-10-08 定）
    if str(cfg.get("model_kind") or "chat") == "chat":
        if providers.is_cloud(cfg):
            sp = providers.split_cloud_id(cfg.get("model", ""))
            if sp:
                return _cloud_target(cfg, sp[0], sp[1])
            return None, "当前选中的云端模型解析失败：%r" % cfg.get("model")
        return _local_target(cfg), ""
    # 选中的是生图/生视频：回退第一个可用文本模型（本地优先，坑 150 的可用性判据）
    if models.usable_local(cfg).get("chat"):
        return _local_target(cfg), ""
    for pid, _pname, m in providers.cloud_models(cfg, providers.KIND_TEXT):
        if secrets.has_api_key(pid):
            return _cloud_target(cfg, pid, m)
    return None, "没有可用于提示词优化的文本模型（本地引擎/模型未就位，云端也没有填过密钥的文本模型）。"


def _safe_read(err, limit=900):
    try:
        return err.read().decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def _server_says(body):
    try:
        j = json.loads(body)
    except Exception:
        return str(body or "").strip()[:260]
    if isinstance(j, dict):
        e = j.get("error")
        if isinstance(e, dict) and e.get("message"):
            return str(e["message"])[:260]
        for k in ("message", "msg", "detail", "code_description"):
            if j.get(k):
                return str(j[k])[:260]
    return str(body or "").strip()[:260]


def _sleep(stop_flag, seconds):
    """可被 stop_flag 打断的等待；返回是否被打断（同 cloud._sleep 的语义）。"""
    end = time.time() + seconds
    while time.time() < end:
        if stop_flag is not None and stop_flag.is_set():
            return True
        time.sleep(min(0.2, max(0.0, end - time.time())))
    return False


def _fail(msg, attempts, t0, stopped=False):
    return {"ok": False, "text": "", "thinking_seen": False, "reasoning_chars": 0,
            "finish": "", "usage": None, "cache_hit": None,
            "attempts": attempts, "elapsed_ms": int((time.time() - t0) * 1000),
            "error": msg, "stopped": stopped}


def _parse_ok(data, attempts, t0):
    """成功回包 → 结果 dict。形状防炸口径同 cloud._ask_window（message 可能是裸串）。"""
    ms = int((time.time() - t0) * 1000)
    if not isinstance(data, dict):
        return _fail("回包不是 JSON 对象（原文：%s）" % str(data)[:120], attempts, t0)
    choices = data.get("choices") or [{}]
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = first.get("message") or {}
    if not isinstance(msg, dict):
        msg = {}
    text = str(msg.get("content") or "").strip()
    reasoning = ""
    for k in REASONING_KEYS:
        v = msg.get(k)
        if v:
            reasoning = str(v)
            break
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    finish = str(first.get("finish_reason") or "")
    out = {"ok": True, "text": text, "thinking_seen": bool(reasoning),
           "reasoning_chars": len(reasoning), "finish": finish, "usage": usage,
           "cache_hit": promptopt.extract_cache_hit(usage),
           "attempts": attempts, "elapsed_ms": ms, "error": "", "stopped": False}
    if not text and reasoning:
        # 坑 49 形态：思考与回答共享同一份额度，正文被思考吃光 = 这个模型关不掉思考。
        # W 2026-10-08 定「照发 + 检测回报」：如实说，不伪装成"优化失败"。
        out["ok"] = False
        out["error"] = ("该模型没能关闭思考：输出全花在思考过程（%d 字），正文一个字没剩。"
                        "请换一个不带思考、或支持关闭思考的文本模型。" % len(reasoning))
    elif not text:
        out["ok"] = False
        out["error"] = "模型返回了空内容（finish_reason=%s）。" % (finish or "未知")
    return out


def optimize_once(target, messages, temperature, max_tokens, stop_flag=None):
    """单路非流式请求 → 结果 dict（形状见 _fail/_parse_ok）。

    重试口径（需求第 3 条：单个请求失败只重试那一个——它的前缀大概率还在缓存里）：
      · 429/5xx：退避 2^n 秒，共 MAX_ATTEMPTS 次（与 cloud.chat_stream 同参数）；
      · 网络层错误（拒连/超时/DNS）：同一路上限——本地服务没起时秒拒，不拖时间；
      · 400 且回包点名关思考参数：摘除该参数重试一次（不计入失败，坑 46 同族）。
    """
    body = {"model": target["model"], "messages": messages, "stream": False,
            "temperature": temperature, "max_tokens": max_tokens}
    body.update(target.get("extra_body") or {})
    thinking = dict(target.get("thinking") or {})
    body.update(thinking)
    attempt = 0
    t0 = time.time()
    while True:
        attempt += 1
        if stop_flag is not None and stop_flag.is_set():
            return _fail("已停止。", attempt, t0, stopped=True)
        req = urllib.request.Request(target["url"],
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=target["headers"], method="POST")
        try:
            with urllib.request.urlopen(req, timeout=target["timeout"]) as r:
                raw_ok = r.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw_ok)
            except ValueError:
                # 200 但回包不是 JSON（网关插页 / 代理劫持一类）：重试也大概率还是它，
                # 如实报原文摘录，不走网络重试
                return _fail("服务返回了 200，但回包不是 JSON（原文：%s）"
                             % raw_ok.strip()[:160], attempt, t0)
            return _parse_ok(data, attempt, t0)
        except urllib.error.HTTPError as e:
            raw = _safe_read(e)
            low = raw.lower()
            if (e.code == 400 and thinking
                    and any(str(k).lower() in low for k in thinking)):
                for k in thinking:
                    body.pop(k, None)
                thinking = {}          # 只摘一次：再 400 就走正常报错
                continue
            if e.code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                if _sleep(stop_flag, 2 ** attempt):
                    return _fail("已停止。", attempt, t0, stopped=True)
                continue
            if target["kind"] == "cloud":
                msg = humanize_http(e.code, raw, target.get("provider"))
            else:
                said = _server_says(raw)
                msg = "本地服务返回错误（HTTP %s）。%s" % (e.code, said or "")
            return _fail(msg, attempt, t0)
        except Exception as e:
            if stop_flag is not None and stop_flag.is_set():
                return _fail("已停止。", attempt, t0, stopped=True)
            if attempt < MAX_ATTEMPTS:
                if _sleep(stop_flag, 2 ** attempt):
                    return _fail("已停止。", attempt, t0, stopped=True)
                continue
            if target["kind"] == "cloud":
                msg = humanize_net(e, target.get("provider"))
            else:
                msg = ("无法连接本地服务（%s）——请先点「启动服务」或检查端口设置。"
                       % target["url"])
            return _fail(msg, attempt, t0)


def _emit(on_event, strategy, state, detail=""):
    """进度回调绝不让异常穿出去打死工作线程（UI 轮接 _ui_q 用）。"""
    if on_event is None:
        return
    try:
        on_event(strategy, state, detail)
    except Exception:
        pass


def optimize_all(cfg, user_text, scenario=None, kind=None, has_image=False,
                 ref_mode="", stop_flag=None, on_event=None):
    """一次提示词优化的全流程 → 结果 dict（三路并行，固定顺序返回）。

    返回 {"ok": 任一路成功, "scenario", "model", "elapsed_ms", "error",
          "results": [{"strategy", "label", "ok", "text", "suspect", "missing",
                       "thinking_seen", "cache_hit", "usage", "finish",
                       "attempts", "elapsed_ms", "error", "stopped"} × 3]}
    —— results 顺序恒为 保守/增强/精炼（UI 并列展示与"回退原始输入"都按这个序）。
    suspect/missing = core.promptopt.fidelity_check 的「疑似偏离原意」标记。
    on_event(strategy, state, detail)：state ∈ sent/done/error/stopped，可选。
    """
    text = str(user_text or "").strip()
    if not text:
        return {"ok": False, "error": "没有可优化的内容。", "results": [],
                "scenario": "", "model": "", "elapsed_ms": 0}
    if scenario is None:
        scenario = promptopt.resolve_scenario(
            kind if kind is not None else cfg.get("model_kind"),
            has_image, ref_mode)
    if scenario not in promptopt.SCENARIOS:
        return {"ok": False, "error": "不认识的优化场景：%r" % scenario,
                "results": [], "scenario": "", "model": "", "elapsed_ms": 0}
    target, err = resolve_target(cfg)
    if target is None:
        return {"ok": False, "error": err, "results": [], "scenario": scenario,
                "model": "", "elapsed_ms": 0}
    if target["kind"] == "local":
        # 先做毫秒级进程检查再发 HTTP（stream.count_tokens 同款纪律）：服务没起时
        # 三路各吃一遍 TCP 超时 + 重试纯属浪费
        try:
            from ..core.server import server_process_alive
            alive = server_process_alive()
        except Exception:
            alive = True
        if not alive:
            return {"ok": False, "error": "本地服务没启动：先在主页面点「启动服务」再优化提示词。",
                    "results": [], "scenario": scenario, "model": target["label"],
                    "elapsed_ms": 0}

    t0 = time.time()
    out = {}
    lock = threading.Lock()

    def run(strategy):
        try:
            msgs = promptopt.build_messages(scenario, text, strategy)
            _emit(on_event, strategy, "sent")
            r = optimize_once(target, msgs, promptopt.TEMPERATURE[strategy],
                              promptopt.plan_max_tokens(text, strategy), stop_flag)
            if r["ok"]:
                fid = promptopt.fidelity_check(text, r["text"])
                r["suspect"], r["missing"] = fid["suspect"], fid["missing"]
            else:
                r["suspect"], r["missing"] = False, []
            r["strategy"] = strategy
            r["label"] = promptopt.STRATEGY_LABEL[strategy]
            _emit(on_event, strategy,
                  "stopped" if r.get("stopped") else ("done" if r["ok"] else "error"),
                  r.get("error") or "")
        except Exception as e:      # 兜底：任何意外都不许让 join 缺一路
            r = _fail("优化请求内部出错：%s: %s" % (type(e).__name__, e), 0, t0)
            r.update(strategy=strategy, label=promptopt.STRATEGY_LABEL[strategy],
                     suspect=False, missing=[])
        with lock:
            out[strategy] = r

    threads = [threading.Thread(target=run, args=(s,), daemon=True,
                                name="promptopt-%s" % s)
               for s in promptopt.STRATEGIES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results = [out[s] for s in promptopt.STRATEGIES if s in out]
    ok = any(r["ok"] for r in results)
    return {"ok": ok, "scenario": scenario, "model": target["label"],
            "results": results,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "error": "" if ok else (results[0]["error"] if results else "请求没能发出。")}
