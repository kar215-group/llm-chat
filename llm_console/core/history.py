# -*- coding: utf-8 -*-
"""llm_console.core.history — 对话历史与"这一轮请求带哪些消息"的唯一出口。

**为什么单独成模块**：以后要做**上下文压缩**（长会话逼近 ctx 上限时，把较早的轮次换成
一条摘要）。压缩改的正是"哪些消息进请求体"这件事 —— 现在把那件事收在这一处，实现时只动
`compress()` 与 `request_messages()` 两个函数，渲染层（`ui/chat`）、对话记录
（`core/chatlog`）、附件预算（`core/textfile`）一条都不用碰。

三条不变量（改这个模块前先读）：

  · **历史就是那份列表**：`[{"role": "user"|"assistant", "content": str|list}, …]`，
    与请求体同形 —— `chatlog` 写盘写的就是这一份，"读回历史"那期直接整份回灌即可。
    所以这个模块**不包装**历史（不引入 History 类）：谁都在改同一个 list，
    多一层包装只会让"记录里到底是哪几条"更难看清。
  · **system 提示词不在历史里**：它属于配置，每次请求现拼在最前面。往历史里塞 system
    会让"清空对话"与对话记录都变得不干净。
  · 本模块是纯计算：不 import tkinter、不发请求、不写盘、不碰任何全局状态（分层规矩见坑 36）。

压缩的触发点约定在 `ui/chat.send_message`："构建请求消息之前"（§11 第 5 条 / 第 13 条）。
2026-10-17 落地时的实际挂点：`ui/chat._do_send` 把"拼请求体"这一步交给工作线程
（`_turn_worker` → `_compose_msgs`），因为压缩要发一次**非流式**请求；主线程上做会冻界面。
"""
import time

from .. import texts
from . import textfile

KEEP_TURNS = 4
"""压缩时保留最近几轮（1 轮 = 提问 + 回答）。W 2026-10-17 批复的"最小版"取值。

为什么不按 token 现算保留区：保留区越算越难解释"为什么这轮记得、那轮忘了"；
固定轮数配一条"已把更早的 N 条折成摘要"的说明，用户能预期。
"""

IMAGE_TOKENS = 768
"""一张图按这么多 token 估。内联图片是 `data:image/...;base64,`，字符数是几十万——
拿字符数当 token 数会让"带一张图"看起来就超预算，估出来的数完全不能用。"""

SUMMARY_ROOM = 0.6
"""摘要请求本身最多吃掉预算的这个比例：剩下 40% 留给指令、输出与偏差。"""

SUMMARY_HEAD = "〔以下是更早对话的摘要，供你延续上下文（原文已不在本轮请求里）〕\n\n%s"
"""摘要消息的前缀。**这是发给模型的内容，不是界面文案**，所以留在本模块而不进 texts。

角色选 assistant：历史里已经有一条 system（配置里那个提示词），再塞第二条 system 会让
一部分 OpenAI 兼容端把两段拼成一段或直接 400；assistant 在语义上也说得通（"模型自己
记着的前情"）。"""


def system_message(cfg):
    """这一轮的 system 消息（没填就是 None，不占位、不塞空串）。"""
    s = str((cfg or {}).get("system_prompt", "") or "")
    return {"role": "system", "content": s} if s else None


def request_messages(cfg, history, pending=None):
    """拼出这次要发给模型的消息：`[system] + 历史 + [这一条]`。

    只读，不改 `history`（调用方自己 `append`）—— 顺序上"先拼、再入历史"，是为了
    万一拼接或请求构建失败时历史里不留半条脏数据。

    `history` 传进来的**可能是压缩后的那一份**（`compress()` 的返回值）：压缩只影响
    本轮请求，`App.history` 与对话记录始终是全量，所以这里不需要知道"有没有压过"。
    """
    out = []
    sys_msg = system_message(cfg)
    if sys_msg is not None:
        out.append(sys_msg)
    out += list(history or [])
    if pending is not None:
        out.append(pending)
    return out


def budget(cfg, cloud=None):
    """这一轮"能塞多少 token"的预算。

    实现只有一处（`core/textfile.budget_tokens`：本地按该模型 ctx − max_tokens − 预留，
    云端按服务商窗口或保守默认值），这里给"上下文压缩"这类同族逻辑一个**统一入口**，
    免得公式被抄成第二份（抄第二份必然漂移）。`cloud` 不传就按当前选的模型判断。
    """
    return textfile.budget_tokens(cfg, cloud=cloud)


def content_text(content):
    """一条消息的可读文本：多模态取 `text` 段拼起来（图片另按 IMAGE_TOKENS 计）。"""
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content
                       if isinstance(p, dict))
    return str(content or "")


def image_count(content):
    """一条消息里带几张图（只认 OpenAI 多模态的 image_url 段）。"""
    if isinstance(content, list):
        return sum(1 for p in content
                   if isinstance(p, dict) and p.get("type") == "image_url")
    return 0


def estimate_tokens(messages):
    """粗估这批消息要多少 token —— 与附件预算**同一套系数**（`textfile.CHARS_PER_TOKEN`）。

    只求量级对：真值要靠服务端的 usage 或 `/tokenize`，而"要不要压缩"这件事在发请求
    之前就得决定，拿不到真值。每条另加 4 token 的对话模板开销（角色标记 / 分隔符）。
    """
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        total += textfile.est_tokens(content_text(c))
        total += image_count(c) * IMAGE_TOKENS + 4
    return total


def _split_index(hist, keep_turns):
    """从末尾数 `keep_turns` 个提问，返回"保留区"的起点下标（0 = 没有可折的部分）。"""
    seen = 0
    for i in range(len(hist) - 1, -1, -1):
        if str((hist[i] or {}).get("role")) == "user":
            seen += 1
            if seen >= keep_turns:
                return i
    return 0


def empty_state():
    """压缩状态的初值（`App.__init__` / 清空对话 / 载入记录三处共用一份形状）。

    形状就是 `compress()` 返回的 info 那几个键；调用方把它整个替换掉，不做原地改动 ——
    这份状态是"上一次摘要覆盖到第几条"，改错一个数字就会漏掉或重讲若干轮。
    """
    return {"covered": 0, "turns": 0, "kept": 0, "text": "", "at": 0.0}


def compress(cfg, history, budget_tokens=None, summarize=None, prev=None):
    """超出预算时把更早的轮次折成一条摘要 → `(本轮该发的历史, 说明, info)`。

    三条口径（W 2026-10-17 批复，别重新设计）：
      · **不改传入的 `history`**：压缩只影响这一轮请求，`App.history` 与对话记录
        始终是全量（"`messages` 必须保持能原样回灌"）；
      · 摘要写成一条 assistant 消息放在最前面，原文照旧一条不动地接在后面；
      · 失败就把原样历史还回去 + 一句说明 —— 宁可超长报错，不许悄悄丢轮次。

    `summarize(messages) -> (摘要正文, 错误人话)`：真发请求的那一层由调用方注入
    （本模块是纯计算，不发网络）。不注入 = 什么都不做（纯计算调用 / 自测用）。
    `prev` 是上一次的摘要 `{"covered": n, "text": …}`：给了就只总结"上次之后新增的
    那几条"，把旧摘要当素材一起喂进去 —— 否则每轮都要把全部老历史重讲一遍，
    一次比一次贵（对话越长越贵，最后连摘要请求本身都塞不进上下文）。

    返回的 `info` 只在**真的压了**的时候非 None，形状：
    `{"covered": 被摘要取代的消息条数（= 本轮请求里不再出现的那几条）, "turns": 其中
      含几个提问, "kept": 原样保留几条, "text": 摘要正文, "at": epoch 秒,
      "skipped": 太长没折进摘要、直接省掉的条数}`。
    调用方把它写进对话记录的**外层字段**（`chatlog.save(compressed=…)`）。
    """
    hist = list(history or [])
    if len(hist) < 2 or summarize is None:
        return hist, "", None
    # ⚠ 局部变量不能叫 `budget`：那会遮住同名的模块函数，`budget(cfg)` 直接
    # UnboundLocalError，再被下面/外面的 try 兜成"什么都没做"这种最坏的静默失败
    # （2026-10-17 自检 [5.19] 抓到的就是这个：不传预算的那条路一次都没走过）。
    if budget_tokens is not None:
        try:
            limit = int(budget_tokens)
        except Exception:
            return hist, "", None
    else:
        try:
            limit = int(budget(cfg))
        except Exception:
            return hist, "", None
    if limit <= 0:
        return hist, "", None
    sys_msg = system_message(cfg)
    used = estimate_tokens(hist) + (estimate_tokens([sys_msg]) if sys_msg else 0)
    if used <= limit:
        return hist, "", None            # 没超：一个字都不动（这是绝大多数轮次的路）
    k = _split_index(hist, KEEP_TURNS)
    if k <= 0:
        return hist, "", None            # 还不够 KEEP_TURNS 轮，没有可折的部分
    base, seed = 0, ""
    if isinstance(prev, dict):
        try:
            covered = int(prev.get("covered") or 0)
        except Exception:
            covered = 0
        if 0 < covered <= k and str(prev.get("text") or "").strip():
            base, seed = covered, str(prev["text"]).strip()
    chunk = hist[base:k]
    if not chunk:
        # 没有"新增的可折部分"（上次的摘要刚好覆盖到保留区边界）：**上一次的摘要照用**。
        # 这里不能退回整份历史 —— 那样等于"压过一次以后反倒全量发送"，比不压还糟。
        # 也不说话：什么都没变，没必要在聊天区再刷一行。
        info = {"covered": k,
                "turns": sum(1 for m in hist[:k] if str(m.get("role")) == "user"),
                "kept": len(hist) - k, "text": seed, "at": time.time(), "skipped": 0}
        return [{"role": "assistant", "content": SUMMARY_HEAD % seed}] + hist[k:], "", info
    # 摘要请求本身也得塞得进上下文：按预算的 60% 截，太长就先整条丢掉最旧的那几条。
    # 丢掉的会在说明里如实报数 —— 不能让"摘要里没提"变成静默丢失。
    seed_msg = ({"role": "assistant", "content": SUMMARY_HEAD % seed} if seed else None)
    room = int(limit * SUMMARY_ROOM)
    chunk_tokens = estimate_tokens(chunk) + (estimate_tokens([seed_msg]) if seed_msg else 0)
    skipped = 0
    while len(chunk) > 1 and chunk_tokens > room:
        chunk_tokens -= estimate_tokens([chunk[0]])
        chunk = chunk[1:]
        skipped += 1
    payload = ([seed_msg] if seed_msg else []) + list(chunk)
    try:
        text, err = summarize(payload)
    except Exception as e:                 # 注入方抛什么都只算"压缩没成功"
        text, err = "", str(e)
    text = str(text or "").strip()
    if err or not text:
        return hist, texts.HIS_FAIL % (err or texts.HIS_EMPTY), None
    summary_msg = {"role": "assistant", "content": SUMMARY_HEAD % text}
    out = [summary_msg] + hist[k:]
    turns = sum(1 for m in hist[:k] if str(m.get("role")) == "user")
    info = {"covered": k, "turns": turns, "kept": len(hist) - k, "text": text,
            "at": time.time(), "skipped": skipped}
    note = texts.HIS_OK % (k, len(hist) - k)
    if skipped:
        note += texts.HIS_SKIPPED % skipped
    left = estimate_tokens(out) + (estimate_tokens([sys_msg]) if sys_msg else 0)
    if left > limit:
        # 压缩救不回来（保留区自己就超了）：先说清，别让用户对着一条上下文超限的报错猜
        note += texts.HIS_OVER % (left, limit)
    return out, note, info
