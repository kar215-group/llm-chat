# -*- coding: utf-8 -*-
"""llm_console.connection.cloud — 云端调用。

一期只有 OpenAI 兼容文本（chat_stream）。二三期在这里加：
  · 阿里云原生生图（Qwen-Image，同步/异步任务，产物 URL 只活 24h → 必须立即下载落地）
  · 万相 / MiniMax 的异步视频任务（创建 → 轮询 → 下载，task_id 需持久化以便断电后取回）

铁律：**厂商字段名只允许出现在本模块**。core/providers.py 负责结构，UI 只消费
与本地一致的事件（reasoning / content / usage / done / stopped / error）。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from ..core import config, providers, secrets
from .stream import iter_sse_chat

RETRY_STATUS = (429, 500, 502, 503)       # 值得退避重试的
MAX_ATTEMPTS = 3

_HTTP_HINT = {
    400: "请求被拒（参数、模型名或接口路径不对）",
    401: "API Key 无效或没有权限",
    403: "无权限访问该模型（可能未开通，或 key 与地域/业务空间不匹配）",
    404: "地址或模型名不存在（检查 base_url 是否该带 /v1、模型名有没有写错）",
    408: "请求超时",
    413: "请求体过大（历史太长，试着清空对话）",
    422: "参数格式不对",
    429: "限流或余额不足",
    500: "服务端内部错误",
    502: "网关错误",
    503: "服务暂时不可用",
    504: "网关超时",
}


def _safe_read(err, limit=900):
    try:
        return err.read().decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def _server_says(body):
    """从错误响应里抠出人话（OpenAI 风格 error.message，或裸文本）。"""
    try:
        j = json.loads(body)
    except Exception:
        return body.strip()[:260]
    if isinstance(j, dict):
        e = j.get("error")
        if isinstance(e, dict) and e.get("message"):
            return str(e["message"])[:260]
        for k in ("message", "msg", "detail", "code_description"):
            if j.get(k):
                return str(j[k])[:260]
    return body.strip()[:260]


def humanize_http(code, body, provider):
    """HTTP 错误 → 可直接显示给用户的中文（含服务端原话与可操作建议）。"""
    hint = _HTTP_HINT.get(code, "HTTP %s" % code)
    msg = "云端请求失败：%s（%s）" % (hint, (provider or {}).get("name") or "云端")
    said = _server_says(body)
    if said:
        msg += "\n  服务端说：%s" % said
    low = (body or "").lower()
    if "stream_options" in low:
        msg += "\n  看起来是这个端点不认 stream_options（用量统计），可关掉重试。"
    if code == 401:
        msg += "\n  检查：设置 → 云端 API → 密钥是否正确、是否过期。"
    elif code == 404:
        msg += "\n  当前请求地址：%s" % providers.chat_completions_url(provider or {})
    elif code == 429:
        msg += "\n  稍等几秒再发，或检查账户余额 / 免费额度。"
    if "inappropriate" in low or "data_inspection" in low or "审核" in (body or ""):
        msg += "\n  （提示词可能被内容审核拦下，换个说法再试。）"
    return msg


def humanize_net(err, provider):
    """网络层错误（DNS / 连接失败 / 超时）→ 人话。"""
    url = providers.chat_completions_url(provider or {})
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", err)
        text = str(reason)
        if "timed out" in text.lower() or "timeout" in text.lower():
            return ("云端连接超时（%s）。\n  可能是网络不通、需要代理，或地址写错。" % url)
        if "getaddrinfo" in text.lower() or "name or service not known" in text.lower():
            return ("域名解析失败：%s\n  检查 base_url 是否写对（含专属域名，如 "
                    "token-plan.cn-beijing.maas.aliyuncs.com）。" % url)
        return "无法连接云端（%s）：%s" % (url, text)
    if isinstance(err, TimeoutError):
        return "云端响应超时（%s）。" % url
    return "云端调用出错：%s" % err


def _plan_prompt(meta_lines, total, budget, question, avg_chars):
    """规划段的提示词：把**预算告诉模型**，否则它"自己决定"就是盲要（常见的是要全部）。"""
    approx_rows = max(1, int(budget * providers_chars_per_token() / max(avg_chars, 1)))
    return (
        "有一份文本附件共 %d 行，因上下文预算限制只能带其中一段。"
        "本次可支配的预算约 %d token，折算下来最多约 %d 行。\n"
        "请只挑选回答问题真正需要的**连续区间**（1 基、含端点）。"
        "如果你需要的行数超过预算，就挑最关键的一段。\n"
        "只输出 JSON：{\"start\": 起始行, \"end\": 结束行}，"
        "不要输出解释、不要代码块标记。\n\n"
        "【文件开头预览（前 %d 行）】\n%s\n\n【我的问题】\n%s"
        % (total, budget, approx_rows, len(meta_lines), "\n".join(meta_lines), question or "（无）"))


def providers_chars_per_token():
    from ..core import textfile
    return textfile.CHARS_PER_TOKEN


_JSON_RANGE = re.compile(r'\{\s*"?start"?\s*:\s*(\d+)\s*,\s*"?end"?\s*:\s*(\d+)\s*\}')


def _parse_window(blob, total):
    """从模型的回话里抠出 {"start":a,"end":b}；返回 (a,b) 或 None。

    取**最后一个**匹配：思考型模型常在推理里先写过草稿再给结论。范围越界一律作废，
    让调用方按原预算发送——宁可不优化，也不能把消息发歪。
    """
    m = None
    for m in _JSON_RANGE.finditer(blob or ""):
        pass
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if not (1 <= a <= b <= total):
        return None
    return (a, b)


def _ask_window(provider, key, model, preview, total, budget, question, timeout=90):
    """向云端要一个读取区间 → (start, end) 或 None（解析失败/超时无所谓，回退即可）。"""
    body = {"model": model, "stream": False, "temperature": 0, "max_tokens": 300,
            "messages": [{"role": "system",
                          "content": "你只输出一个 JSON 对象，不输出其它任何字符。"},
                         {"role": "user",
                          "content": _plan_prompt(preview, total, budget, question,
                                                  sum(len(x) for x in preview) /
                                                  max(len(preview), 1))}]}
    req = urllib.request.Request(providers.chat_completions_url(provider),
                                 data=json.dumps(body).encode("utf-8"),
                                 headers=_headers(provider, key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, humanize_http(e.code, _safe_read(e), provider)
    except Exception as e:
        return None, humanize_net(e, provider)
    choices = data.get("choices") or [{}]
    msg = choices[0].get("message", {}) or {}
    blob = "%s %s" % (msg.get("content") or "", msg.get("reasoning_content") or "")
    win = _parse_window(blob, total)
    if not win:
        return None, "模型没给出可用范围（原文：%s）" % blob.strip()[:120]
    return win, ""


def chat_stream_with_file(cfg, messages, out_q, stop_flag, file_info):
    """云端"让模型自己决定读哪一段"：一次规划请求 → 改写最后一条消息 → 正常流式回答。

    只在「文件被预算截断」且「用户在设置里打开了这个开关」时被调用（默认关）。
    设计约束（都在代码里硬保证，不指望模型自觉）：
      · 规划段带**预算数字**，并要求只回 JSON；
      · 行号越界 / 解析失败 / 请求出错 → 原样按预算发送（绝不因为规划失败而发不出去）；
      · 只规划一次：每多一轮就多一次付费请求，收益不确定时不加码。
    """
    provider = providers.current_provider(cfg)
    if provider is None:
        out_q.put(("error", "当前选中的不是云端模型，或它的 provider 已不存在/被停用。"))
        return
    key = secrets.get_api_key(provider["id"])
    sp = providers.split_cloud_id(cfg.get("model", ""))
    model = sp[1] if sp else ""
    lines = list(file_info.get("lines") or [])
    total = file_info.get("total_lines") or len(lines)
    budget = int(file_info.get("budget") or 4000)
    window = None
    if key and model and lines:
        preview = lines[:80]
        window, why = _ask_window(provider, key, model, preview, total, budget,
                                  file_info.get("question") or "")
        if window:
            a, b = window
            # 再核一遍 token：模型给的范围太大就按预算从起始处截断
            seg = lines[a - 1:b]
            used = 0
            keep = 0
            for ln in seg:
                t = _est_tokens(ln)
                if used + t > budget and keep:
                    break
                used += t
                keep += 1
            if keep < len(seg):
                b = a + keep - 1
                out_q.put(("fileplan", "模型要的范围比预算大，已收到第 %d~%d 行。" % (a, b)))
            else:
                out_q.put(("fileplan", "模型请求读取第 %d~%d 行（共 %d 行）。" % (a, b, total)))
            block = _render_file_block(file_info, lines[a - 1:b], a, b, total, budget)
            if messages:
                messages[-1] = {"role": "user", "content": block}
        else:
            out_q.put(("fileplan", "规划没成功（%s），按原预算发送。" % (why or "原因未知")))
    chat_stream(cfg, messages, out_q, stop_flag)


def _est_tokens(s):
    from ..core import textfile
    return textfile.est_tokens(s)


def _render_file_block(file_info, seg, a, b, total, budget):
    from ..core import textfile
    return textfile.render_block(
        file_info.get("name") or "文件", seg, total, file_info.get("chars") or 0,
        max(0, total - b), file_info.get("encoding") or "utf-8",
        question=file_info.get("question"), budget=budget,
        window="模型选择读取第 %d~%d 行" % (a, b))


def _tiny_png_b64():
    """现造一张 1x1 的 PNG（base64）。

    不用硬编码的 base64 字符串：那种 blob 没法验证、也没法审计；
    这里用 zlib + CRC 现场拼出来，测试里再用 tk.PhotoImage 反向解一遍确认它合法。
    """
    import base64
    import struct
    import zlib

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)      # 1x1、8bit、RGB
    idat = zlib.compress(b"\x00\x7f\x7f\x7f")                 # filter=0 + 一个灰像素
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", idat) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


# 服务端说"我不收图"时常见的措辞（各家不一样，宁可多列几个再配合 HTTP 400 一起判）
_NO_IMAGE_WORDS = ("vision", "multimodal", "mmproj", "projector", "image",
                   "does not support", "not support", "unsupported")


def probe_image_input(cfg, provider=None, api_key=None, model=None, timeout=60):
    """发一次"1x1 图 + max_tokens=1"的最小请求，问服务端到底收不收图。

    这是唯一能**可靠**确定云端能力的方法（/models 不带能力字段，名字只能猜）。
    返回 (verdict, 说明)：verdict ∈ yes / no / unknown。
      · 200                       → yes（真的收下了）
      · 400 且原话提到 视觉/图像   → no
      · 其它（模型名错、鉴权、5xx、服务没起）→ unknown，绝不伪装成"不支持"
    """
    from ..core import capability
    if provider is None:
        # 本地：服务没起来就别发（会吃 TCP 超时），直接 unknown
        try:
            from ..core.server import server_process_alive
            if not server_process_alive():
                return capability.UNKNOWN, "本地服务没启动：先在主页面点「启动服务」再验证"
        except Exception:
            return capability.UNKNOWN, "无法判断本地服务状态"
        url = config.base_url(cfg) + "/v1/chat/completions"
        headers = config.api_headers(cfg)
        m = model or os.path.basename(str(cfg.get("model", "")))
    else:
        key = api_key if api_key is not None else secrets.get_api_key(provider["id"])
        if not key:
            return capability.UNKNOWN, "没有 API Key，验证不了"
        url = providers.chat_completions_url(provider)
        headers = _headers(provider, key)
        sp = providers.split_cloud_id(model or cfg.get("model", ""))
        m = model or (sp[1] if sp else "")
    if not m:
        return capability.UNKNOWN, "没有可验证的模型名"
    body = {"model": m, "stream": False, "max_tokens": 1,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "只回一个字"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,%s" % _tiny_png_b64()}}]}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return capability.YES, "服务端收下了图片输入"
    except urllib.error.HTTPError as e:
        raw = _safe_read(e)
        low = raw.lower()
        if e.code in (400, 404, 415, 422) and any(w in low for w in _NO_IMAGE_WORDS):
            return capability.NO, "服务端明确拒收图片：%s" % _server_says(raw)
        return capability.UNKNOWN, "没问出结论（HTTP %s）：%s" % (e.code, _server_says(raw))
    except Exception as e:
        return capability.UNKNOWN, "没问出结论：%s" % e


def _payload(cfg, model, messages, with_usage=True):
    p = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": cfg.get("temperature", 0.8),
        "top_p": cfg.get("top_p", 0.95),
        "max_tokens": cfg.get("max_tokens", 4096),
    }
    if with_usage:
        # 让最后一个 chunk 带 usage（多数 OpenAI 兼容端支持；不认就自动去掉重试）
        p["stream_options"] = {"include_usage": True}
    try:
        if int(cfg.get("seed", -1)) >= 0:
            p["seed"] = int(cfg["seed"])
    except Exception:
        pass
    return p


def chat_stream(cfg, messages, out_q, stop_flag):
    """云端文本流式对话：推的事件与本地 stream_worker 完全一致。"""
    provider = providers.current_provider(cfg)
    if provider is None:
        out_q.put(("error", "当前选中的不是云端模型，或它的 provider 已不存在/被停用。"))
        return
    key = secrets.get_api_key(provider["id"])
    if not key:
        out_q.put(("error", "还没给「%s」填 API Key（设置 → 云端 API → 密钥）。"
                            % provider["name"]))
        return
    sp = providers.split_cloud_id(cfg.get("model", ""))
    model = sp[1] if sp else ""
    if not model:
        out_q.put(("error", "云端模型名解析失败：%r" % cfg.get("model")))
        return

    url = providers.chat_completions_url(provider)
    headers = _headers(provider, key)
    timeout = int(provider.get("timeout", 600))
    payload = _payload(cfg, model, messages)

    attempt = 0
    while True:
        attempt += 1
        stopped = stop_flag is not None and stop_flag.is_set()
        if stopped:
            out_q.put(("stopped", None))
            return
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                finish, usage = iter_sse_chat(resp, out_q, stop_flag)
            if stop_flag is not None and stop_flag.is_set():
                out_q.put(("stopped", None))
                return
            if usage:
                out_q.put(("usage", usage))
            out_q.put(("done", finish))
            return
        except urllib.error.HTTPError as e:
            body = _safe_read(e)
            # 有些兼容端不认 stream_options：去掉它再试一次，别让用户去猜原因
            if e.code == 400 and "stream_options" in payload and "stream_options" in body.lower():
                payload.pop("stream_options", None)
                continue
            if e.code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                wait = 2 ** attempt
                out_q.put(("retry", "云端返回 %s，%ds 后重试（第 %d/%d 次）…"
                           % (e.code, wait, attempt, MAX_ATTEMPTS - 1)))
                if _sleep(stop_flag, wait):
                    out_q.put(("stopped", None))
                    return
                continue
            out_q.put(("error", humanize_http(e.code, body, provider)))
            return
        except Exception as e:
            if stop_flag is not None and stop_flag.is_set():
                out_q.put(("stopped", None))
            else:
                out_q.put(("error", humanize_net(e, provider)))
            return


def _sleep(stop_flag, seconds):
    """可被"停止生成"打断的等待；返回是否被打断。"""
    end = time.time() + seconds
    while time.time() < end:
        if stop_flag is not None and stop_flag.is_set():
            return True
        time.sleep(min(0.2, max(0.0, end - time.time())))
    return False


def _headers(provider, key):
    h = {"Content-Type": "application/json", "Authorization": "Bearer %s" % key,
         "User-Agent": config.USER_AGENT}
    h.update(provider.get("extra_headers") or {})
    return h


def _parse_model_ids(data):
    """从各家 /models 返回里抠出模型 id：兼容 OpenAI 的 data[]、裸 list、以及 models[]。"""
    try:
        j = json.loads(data)
    except Exception:
        return []
    raw = []
    if isinstance(j, dict):
        raw = j.get("data") or j.get("models") or j.get("result") or []
        if not raw and isinstance(j.get("output"), dict):
            raw = j["output"].get("models") or []
    elif isinstance(j, list):
        raw = j
    out = []
    for it in raw or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict) and it.get("id"):
            out.append(str(it["id"]))
        elif isinstance(it, dict) and it.get("model_id"):
            out.append(str(it["model_id"]))
    seen, uniq = set(), []
    for m in out:
        m = str(m).strip()
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def list_models(provider, api_key=None, timeout=25):
    """拉该服务商的模型清单（GET {base}/models）。返回 (ok, [模型名], 说明)。

    只有设置页点「刷新模型」时才会调它：拉到的结果会缓存进 provider，
    平时启动不请求网络（不少平台的清单给得不全，也不该每次开机都多一次请求）。
    """
    key = api_key if api_key is not None else secrets.get_api_key(provider["id"])
    if not key:
        return False, [], "没有 API Key，无法拉取模型清单。"
    url = providers.models_list_url(provider)
    req = urllib.request.Request(url, headers=_headers(provider, key), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, [], humanize_http(e.code, _safe_read(e), provider)
    except Exception as e:
        return False, [], humanize_net(e, provider)
    ids = _parse_model_ids(data)
    if not ids:
        return False, [], "这个端点没有返回可解析的模型清单（返回体是空的或格式不同）。"
    return True, ids, "取到 %d 个模型（%s）" % (len(ids), url)


def try_model(provider, api_key=None, model=None, timeout=45):
    """拿用户填的模型名去真发一次最小请求，把服务端原话回给他。

    存在的名字 → 通；不存在 / 没开通 → 服务端会点名说 "Model not exist" 或
    "AccessDenied.Unpurchased"，这正是"这个模型我到底能不能用"的准信。
    成本只有一次 max_tokens=1 的文本请求；对生图/生视频类名字，这条探测只说明
    "名字服务端认识吗"，不代表走通了它自己的原生协议。
    """
    key = api_key if api_key is not None else secrets.get_api_key(provider["id"])
    if not key:
        return False, "没有 API Key，试不了。"
    model = str(model or "").strip()
    if not model:
        return False, "先填模型名。"
    url = providers.chat_completions_url(provider)
    body = {"model": model, "stream": False, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=_headers(provider, key), method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read().decode("utf-8", "replace")
        ms = int((time.time() - t0) * 1000)
        try:
            got = json.loads(data).get("model") or model
        except Exception:
            got = model
        return True, "服务端认这个名字（%d ms）：%s" % (ms, got)
    except urllib.error.HTTPError as e:
        return False, humanize_http(e.code, _safe_read(e), provider)
    except Exception as e:
        return False, humanize_net(e, provider)


def probe(provider, api_key=None, model=None):
    """连通性自测：发一次 max_tokens=1 的最小请求，返回 (ok, 说明)。

    设置页的「测试连接」按钮用它——花不到一次对话的零头，就能把
    "地址写错 / key 无效 / 模型名不对 / 端点不支持 stream_options" 区分开。
    """
    key = api_key if api_key is not None else secrets.get_api_key(provider["id"])
    if not key:
        return False, "没有 API Key，先填密钥再测。"
    models = provider.get("models") or []
    if not model:
        # 只拿文本模型测：生图/生视频走的是另一套原生协议，用 chat 请求探它们必然 400，
        # 而且个别平台（阿里云）会把它当成一次真实生成来计费
        texts = [m for m in models
                 if providers.model_kind_of(provider, m) == providers.KIND_TEXT]
        if not texts:
            return False, ("「%s」里没有归类为文本模型的条目——测试连接要用文本模型，"
                           "请先在模型清单里加入一个。" % provider["name"])
        model = texts[0]
    elif providers.model_kind_of(provider, model) != providers.KIND_TEXT:
        return False, "「%s」不是文本模型，不能拿 chat 请求去测（会走错协议）。" % model
    if not model:
        return False, "provider 还没填任何模型名，无法测试。"
    url = providers.chat_completions_url(provider)
    headers = _headers(provider, key)
    body = {"model": model, "stream": False, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read().decode("utf-8", "replace")
        ms = int((time.time() - t0) * 1000)
        try:
            j = json.loads(data)
            got = (j.get("model") or model)
        except Exception:
            got = model
        return True, "连通正常（%d ms）：%s ← %s" % (ms, got, url)
    except urllib.error.HTTPError as e:
        return False, humanize_http(e.code, _safe_read(e), provider)
    except Exception as e:
        return False, humanize_net(e, provider)
