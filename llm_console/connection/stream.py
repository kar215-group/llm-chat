# -*- coding: utf-8 -*-
"""llm_console.connection.stream — 对话连接层：SSE 流式请求、错误人话化、（备用）模型自取别名"""

import json
import os
import urllib.error
import urllib.request

from ..core.config import api_headers, base_url
from ..core.models import clean_alias


def request_auto_alias(cfg, path):
    """【暂时没用，后续可能启用】请模型为自己起一个简短别名。

    现在显示名已改为 make_alias() 从文件名自动生成（更快、零成本、可离线），
    本函数与 _auto_alias_thread 保留备用：若以后想要"模型自己起名"的个性化
    别名，把 _status_loop 里标注的触发段取消注释即可。
    """
    payload = {
        "model": "local",
        "messages": [
            {"role": "user", "content":
                '你的模型文件名是"%s"。请给它起一个简短易辨识的英文别名：'
                "只使用小写字母、数字和连字符，长度 4 到 16 个字符。"
                "只输出这个别名本身，不要输出任何解释、其他内容或引号。"
                % os.path.basename(path)}
        ],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 48,
        # Qwen3：关闭本次请求的思考链，避免别名被思考内容挤掉
        "chat_template_kwargs": {"enable_thinking": False},
    }
    url = base_url(cfg) + "/v1/chat/completions"
    req = urllib.request.Request(url,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers=api_headers(cfg), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
            choices = data.get("choices") or [{}]
            text = choices[0].get("message", {}).get("content", "")
    except Exception:
        return None
    return clean_alias(text)

def count_tokens(cfg, text, timeout=3):
    """用本地 llama-server 的 /tokenize 精确数 token；拿不到就返回 None（调用方回退估算）。

    先做毫秒级的进程检查再发 HTTP：服务没起来时盲目请求要吃 2 秒 TCP 超时
    （这个坑在设置页开页卡顿上踩过一次），而"数 token"只是锦上添花，不值得等。
    """
    if not text:
        return 0
    try:
        from ..core.server import server_process_alive
        if not server_process_alive():
            return None
    except Exception:
        return None
    try:
        req = urllib.request.Request(
            base_url(cfg) + "/tokenize",
            data=json.dumps({"content": text, "add_special": False,
                             "parse_special": True}).encode("utf-8"),
            headers=api_headers(cfg), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        toks = data.get("tokens")
        return len(toks) if isinstance(toks, list) else None
    except Exception:
        return None


def friendly_error(e, cfg):
    """把常见网络错误翻译成人话。"""
    if isinstance(e, urllib.error.URLError):
        return "无法连接本地服务（%s）——请先点「启动服务」或检查端口设置。" % base_url(cfg)
    if isinstance(e, urllib.error.HTTPError):
        return "服务返回错误（HTTP %s）。" % e.code
    return str(e)

REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


def iter_sse_chat(resp, out_q, stop_flag=None):
    """读 OpenAI 兼容的 SSE 流，把增量推进 out_q；返回 (finish_reason, usage)。

    本地 llama-server 与云端 OpenAI 兼容端点**共用这一份解析**，避免两套实现漂移。
    思考链字段各家命名不同（reasoning_content / reasoning / thinking），按顺序取第一个非空的。
    """
    finish_reason, usage = None, None
    for raw in resp:
        if stop_flag is not None and stop_flag.is_set():
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            break
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        fr = choices[0].get("finish_reason")
        if fr:
            finish_reason = fr
        for k in REASONING_KEYS:
            v = delta.get(k)
            if v:
                out_q.put(("reasoning", v))
                break
        ct = delta.get("content")
        if ct:
            out_q.put(("content", ct))
    return finish_reason, usage


def stream_worker(cfg, messages, out_q, stop_flag):
    payload = {
        "model": "local",
        "messages": messages,
        "stream": True,
        "temperature": cfg.get("temperature", 0.8),
        "top_p": cfg.get("top_p", 0.95),
        "top_k": cfg.get("top_k", 40),
        "repeat_penalty": cfg.get("repeat_penalty", 1.1),
        "max_tokens": cfg.get("max_tokens", 4096),
        "cache_prompt": True,
    }
    if int(cfg.get("seed", -1)) >= 0:
        payload["seed"] = int(cfg["seed"])

    url = base_url(cfg) + "/v1/chat/completions"
    req = urllib.request.Request(url,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers=api_headers(cfg), method="POST")
    try:
        # 27B 生成 ~3.7 tok/s：长思考可达十几分钟，超时要给足
        with urllib.request.urlopen(req, timeout=1800) as resp:
            finish_reason, usage = iter_sse_chat(resp, out_q, stop_flag)
        if stop_flag.is_set():
            out_q.put(("stopped", None))
            return
        if usage:
            out_q.put(("usage", usage))
        out_q.put(("done", finish_reason))
    except Exception as e:
        if stop_flag.is_set():
            out_q.put(("stopped", None))
        else:
            out_q.put(("error", friendly_error(e, cfg)))
