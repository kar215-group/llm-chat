# -*- coding: utf-8 -*-
"""llm_console.connection.summarize — 上下文压缩用的**一次非流式**请求（2026-10-17）

为什么单开一个模块：`core/history.compress` 是纯计算（分层铁律：core 不发网络），
它算清"要不要压、压哪一段"之后，把拼好的消息交给这里发出去；调用点只有一个
（`ui/chat._compose_msgs`，在工作线程里）。

为什么不在 `cloud.py` / `stream.py` 里加一个分支：那两个文件的主线是**流式**对话
（SSE 解析、停止旗标、超时重试节奏都为流式设计）。摘要是一次同步请求，它的关注点
完全不同（不需要中断、超时要短、错误要一句话），混进去会让两条路径互相背锅。

走**当前选中的那个模型**（本地 / 云端都一样，W 定的口径）：换模型就等于换了摘要的
写法，但那是既有语义，不为压缩单开一个"摘要模型"选择——多一个旋钮就多一处要同步。
"""
import json
import urllib.error
import urllib.request

from ..core import providers, secrets
from ..core.config import USER_AGENT, api_headers, base_url
from .. import texts
from . import cloud as cloud_conn
from . import stream as stream_conn

SUMMARY_CHARS = 600
"""摘要正文的目标长度（字）。也给 `max_tokens` 当参考：中文约 1 字 1 token，留一倍余量。"""

INSTRUCTION = (
    "请把上面这段对话压成一份给**你自己**看的摘要，用于你之后继续这段对话。\n"
    "要求：\n"
    "1) 只写有用的信息：用户在做什么、提出过哪些要求与偏好、已经定下的决定与结论、"
    "还没解决的问题；\n"
    "2) 具体的东西原样保留：文件名与路径、模型名、参数值、命令、关键数字；\n"
    "3) 不要寒暄、不要评价、不要复述原文，也不要写「用户说」这类转述套话，直接分条写；\n"
    "4) 中文，不超过 %d 字；\n"
    "5) 只输出摘要正文。"
)
"""发给模型的工作提示词 —— 与 cloud.py / stream.py 里那几处同一条口径：写给模型的字
不搬进 texts.py（措辞会直接影响行为），错误回显才收在那里。"""


def _err_text(e, cfg, provider):
    """把异常翻成人话：云端走 cloud 的两条（按 HTTP 状态分档），本地走 stream 的那条。"""
    if provider is not None:
        if isinstance(e, urllib.error.HTTPError):
            try:
                body = e.read().decode("utf-8", "replace")[:900]
            except Exception:
                body = ""
            return cloud_conn.humanize_http(e.code, body, provider)
        return cloud_conn.humanize_net(e, provider)
    return stream_conn.friendly_error(e, cfg)


def summarize(cfg, messages, timeout=0):
    """把这段对话交给当前选中的模型压成摘要 → `(摘要正文, 错误人话)`。

    `messages` 是"要总结的对话"本身（`core/history.compress` 拼好的那一段，可能以
    上次的摘要开头）；本函数负责把 `INSTRUCTION` 追加在后面 —— 提示词与请求体都在
    这一层，core 只管"哪几条进摘要"。

    `timeout=0` = 用默认值（本地 600 秒、云端取服务商的 timeout）：27B 本地模型把
    上千字的老历史读一遍再写 600 字，几十秒是常态，别设成"看起来正常"的 30 秒。
    取不到摘要一律返回空正文 + 一句人话，**不抛异常**（调用方按"压缩失败"处理）。
    """
    msgs = list(messages or [])
    if not msgs:
        return "", texts.SUM_EMPTY
    msgs.append({"role": "user", "content": INSTRUCTION % SUMMARY_CHARS})
    cloud = providers.is_cloud(cfg)
    provider = None
    try:
        if cloud:
            provider = providers.current_provider(cfg)
            if provider is None:
                return "", texts.CLD_L1
            key = secrets.get_api_key(provider["id"])
            if not key:
                return "", texts.CLD_CAT_18 % provider["name"]
            sp = providers.split_cloud_id(cfg.get("model", ""))
            model = sp[1] if sp else ""
            if not model:
                return "", texts.CLD_CAT_19 % cfg.get("model")
            url = providers.chat_completions_url(provider)
            headers = {"Content-Type": "application/json",
                       "Authorization": "Bearer %s" % key,
                       "User-Agent": USER_AGENT}
            headers.update(provider.get("extra_headers") or {})
            to = timeout or int(provider.get("timeout", 600))
            payload = {"model": model, "messages": msgs, "stream": False,
                       "temperature": 0.3,
                       "max_tokens": SUMMARY_CHARS * 2}
        else:
            url = base_url(cfg) + "/v1/chat/completions"
            headers = api_headers(cfg)
            to = timeout or 600
            # chat_template_kwargs 只对本地生效（Qwen3 一类的思考链开关）：不关的话
            # 思考段能把 max_tokens 吃干净，正文一个字都不剩（stream.py 里同一处教训）
            payload = {"model": "local", "messages": msgs, "stream": False,
                       "temperature": 0.3,
                       "max_tokens": SUMMARY_CHARS * 2,
                       "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=to) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return "", _err_text(e, cfg, provider)
    try:
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        text = str(msg.get("content") or "").strip()
    except Exception:
        return "", texts.SUM_PARSE
    if not text:
        # 只回了思考内容 / 空回答：不拿思考当摘要（那是过程不是结论）
        return "", texts.SUM_EMPTY
    return text[:SUMMARY_CHARS * 2], ""
