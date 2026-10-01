# -*- coding: utf-8 -*-
"""llm_console.core.chatlog — 对话记录落盘（**只对文本语言模型**；本期只存不读）

为什么只存文本模型：生图 / 生视频的成果本来就是落盘的文件，聊天流里那几行只是路径与
状态，存下来没有意义（W 定的范围）。

数据形状一次给全，为的是后面"加载历史 / 继续对话 / 上下文压缩"不用改格式：

    {
      "version": 1,
      "id": "20261001-131502",            # 会话号：建会话时的本地时间，文件名与它一致
      "created_at": 1790831702.0,         # epoch 秒
      "updated_at": 1790831902.0,
      "app_version": "0.0.3beta",
      "model": "deepseek::deepseek-flash",# 就是 cfg["model"] 的原值：本地=文件名，云端=pid::模型
      "provider": "deepseek",             # local 或服务商 id
      "model_kind": "chat",
      "title": "2026-10-01 14:31:07",       # = **保存时间**（W 定的），会话列表按它认
      "subject": "第一句用户话的前 24 字",   # 留给"读回历史"那期显示内容摘要
      "turns": 3,
      "messages": [{"role": "user", "content": "…"}, …],   # **只有 role/content**：与请求体同形
      "usage": {}                         # 预留：各家 usage 字段不同，先不聚合
    }

**`messages` 里的 role/content 就是 OpenAI 请求体的形状**，所以"加载后继续对话"=
`App.history = [{"role": m["role"], "content": m["content"]} for m in msgs]`，
不需要任何迁移。反过来，记录里**故意不写** `ts` / `model` 这类附加字段：
`App.history` 会被整份塞进请求体，多一个键就可能被某家服务端拒（各家对未知字段
的容忍度不一样），所以宁可让消息保持纯净，时间戳这类信息将来要加就加在**外层**
（例如并列一个 `marks: [...]` 按下标对齐），别混进 role/content 那一份。

两条纪律：
  · 多模态消息里的图片是 `data:image/...;base64,` 内联的，一张图几百 KB —— 写盘前
    一律折成占位串（与云端生图日志同一个道理，见 connection/cloud_media._shrink_for_log
    与坑 102），否则一份记录会变成查不动的砖头，而且原图本来就在本机上。
  · 这个函数**永不抛异常**：它挂在每轮结束与关窗路径上，存不进去顶多少一份记录，
    不能把"发下一条消息"或"正常退出"带崩。失败原因回给调用方显示一行。
"""

import json
import os
import time

from .config import APP_DIR, APP_VERSION

VERSION = 1
TITLE_CHARS = 24
PLACEHOLDER = "〔图片已省略：%d 字符的 base64；原图见本机文件〕"


def enabled(cfg):
    return bool((cfg or {}).get("chat_log_save", True))


def log_dir(cfg):
    """记录目录：配置项优先，留空回退 `<程序目录>/chat_logs`（不写死盘符，项目要分发）。"""
    d = str((cfg or {}).get("chat_log_dir", "") or "").strip()
    return d or os.path.join(APP_DIR, "chat_logs")


def new_id():
    return time.strftime("%Y%m%d-%H%M%S")


def path_for(cfg, sid):
    return os.path.join(log_dir(cfg), "%s.json" % sid)


def title_of(messages):
    """标题 = 第一句用户话的前若干字（换行压成空格）。没有用户话就用模型名兜底。"""
    for m in messages or []:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):          # 多模态：取里面那段 text
            c = " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
        s = " ".join(str(c or "").split()).strip()
        if s:
            return s[:TITLE_CHARS] + ("…" if len(s) > TITLE_CHARS else "")
    return "（无标题）"


def _shrink(obj):
    """把内联的 base64 图片折成占位串：记录要能读、能 diff，不能塞几百 KB 的 blob。"""
    if isinstance(obj, dict):
        return {k: _shrink(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shrink(v) for v in obj]
    s = str(obj or "")
    if len(s) > 200 and ("base64," in s or s.startswith("data:")):
        head = s[:40]
        return PLACEHOLDER % len(s) + "（%s…）" % head
    return obj


def save(cfg, sid, messages, model="", provider="", model_kind="chat"):
    """把这一会话写成一整份 JSON（覆盖同名文件）→ (path, error)。

    整份重写而不是追加：会话文件本来就小（折掉 base64 后通常几十 KB），
    重写比维护"追加 + 崩溃截断恢复"简单得多，也天然幂等。
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict) and m.get("role")]
    if not msgs:
        return "", "没有内容可存"
    if not sid:
        sid = new_id()
    p = path_for(cfg, sid)
    now = time.time()
    payload = {
        "version": VERSION,
        "id": sid,
        "created_at": now,
        "updated_at": now,
        "app_version": APP_VERSION,
        "model": model or "",
        "provider": provider or "",
        "model_kind": model_kind or "chat",
        # 标题暂时就是**保存时间**（W 定的）：会话列表按它排序一眼就能认出是哪一份。
        # 第一句用户话另存在 `subject`，等"读回历史"那期的列表想显示内容摘要时直接用。
        "title": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "subject": title_of(msgs),
        "turns": sum(1 for m in msgs if m.get("role") == "user"),
        "messages": _shrink(msgs),
        "usage": {},
    }
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)                # 写完再改名：中途被强杀也不会留下半份记录
    except Exception as e:
        try:
            os.remove(p + ".part")
        except Exception:
            pass
        return "", "写入失败：%s" % e
    return p, ""


def list_sessions(cfg, limit=50):
    """按时间倒序列出已有记录（**本期界面里没有入口**，留给"加载历史"那期用）。

    只读文件头部的元信息，不解析整份 messages。
    """
    d = log_dir(cfg)
    out = []
    try:
        names = sorted((n for n in os.listdir(d) if n.endswith(".json")), reverse=True)
    except Exception:
        return out
    for n in names[:limit]:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        out.append({"id": j.get("id") or n[:-5], "path": os.path.join(d, n),
                    "title": j.get("title") or "", "model": j.get("model") or "",
                    "turns": j.get("turns") or 0,
                    "created_at": j.get("created_at") or 0})
    return out
