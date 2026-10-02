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
"""


def system_message(cfg):
    """这一轮的 system 消息（没填就是 None，不占位、不塞空串）。"""
    s = str((cfg or {}).get("system_prompt", "") or "")
    return {"role": "system", "content": s} if s else None


def request_messages(cfg, history, pending=None):
    """拼出这次要发给模型的消息：`[system] + 历史 + [这一条]`。

    只读，不改 `history`（调用方自己 `append`）—— 顺序上"先拼、再入历史"，是为了
    万一拼接或请求构建失败时历史里不留半条脏数据。

    **上下文压缩的实现位置就在这儿**：将来把 `history` 里较早的轮次换成摘要消息，
    其余调用点（渲染、记录、预算）都不需要知道。
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
    from . import textfile
    return textfile.budget_tokens(cfg, cloud=cloud)


def compress(cfg, history, budget_tokens=None):
    """把较早的轮次压成摘要 —— **尚未实现，本函数是给那一期占住的位置**。

    W 已定：上下文压缩还没排期（§11 第 5 条）。占位而不是留空，是为了让"改哪儿"
    这件事有名字、有位置：
      · 触发判据（历史估算 token 逼近 `budget()`）与摘要怎么写，都挂在这里；
      · 结果要么改写 `history`（把旧轮次换掉）、要么返回一条"给用户看的说明"，
        由 `chat.send_message` 显示 —— 两种都只影响本模块与那一个调用点；
      · 摘要结果要落进对话记录**外层**字段（`chatlog` 的 payload），**别改
        `messages` 的形状**（§11 第 13 条③）。

    现在返回 None = 什么都没做。
    """
    return None
