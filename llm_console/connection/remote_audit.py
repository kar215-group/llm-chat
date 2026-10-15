# -*- coding: utf-8 -*-
"""llm_console.connection.remote_audit — 远程访问的审计台账（2026-10-12 模块化）。

`remote_audit.jsonl` 是"谁碰过我这台机"的唯一痕迹，所以它要回答的是**关键事件**，
不是轮询心跳：手机端 1.5 秒一轮的状态查询如果逐条落盘，超 1MB 触发轮转（保尾 500
条）之后，配对、吊销、拒绝这类真正要看的行会被十几分钟内的轮询彻底冲走。
因此轮询类 GET 走 `audit_poll`（同源同端点 60 秒聚合成一条），其余一律逐条。

只记元数据（时间 / 来源 / 动作 / 成败 / 耗时 / 短 detail），**不含密钥与请求体**；
detail 由调用方保证不含本机绝对路径（坑 209⑥）。
"""

import json
import os
import threading
import time

from ..core.config import APP_DIR

AUDIT_PATH = os.path.join(APP_DIR, "remote_audit.jsonl")
AUDIT_ROTATE_BYTES = 1024 * 1024
AUDIT_KEEP = 500

# ---------------------------------------------------------------------------
# 审计（JSONL 追加；轮转 = 保尾部整份**纯文本**重写，见 audit() 里那段为什么不用
# atomic_write_json）
# ---------------------------------------------------------------------------

_audit_lock = threading.Lock()

# 轮询聚合窗口（秒）：见 `audit_poll` 的为什么
POLL_WINDOW_S = 60.0
_poll_gate = {}        # (action, ip, detail) -> [窗口起点, 窗口内累计次数]


def _write_line(action, ip, ok, detail, ms):
    """落一行 JSONL + 必要时轮转。**调用方持 `_audit_lock`**。"""
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"),
           "action": action, "ip": str(ip), "ok": bool(ok),
           "detail": str(detail)[:200]}
    if ms is not None:
        rec["ms"] = int(ms)          # 这一条请求在本机花了多久（毫秒）
    line = json.dumps(rec, ensure_ascii=False)
    try:
        rotate = (os.path.isfile(AUDIT_PATH)
                  and os.path.getsize(AUDIT_PATH) > AUDIT_ROTATE_BYTES)
        if rotate:
            try:
                with open(AUDIT_PATH, "r", encoding="utf-8") as f:
                    tail = f.readlines()[-AUDIT_KEEP:]
                # **纯文本写回**，不能用 atomic_write_json：这份文件是 JSONL
                # （一行一条），而 atomic_write_json 写的是 `indent=2` 的 JSON
                # 数组 —— 轮转一次之后整个文件就不是 JSONL 了，read_audit 逐行
                # loads 拿到的是**字符串**，设置页「查看审计…」的 r.get("t") 当场
                # AttributeError（保留的 500 条也全读不出来），再轮转一次转义还
                # 指数增长。原子性靠 .keep + os.replace 自己保证。
                with open(AUDIT_PATH + ".keep", "w", encoding="utf-8") as f:
                    f.writelines(ln if ln.endswith("\n") else ln + "\n"
                                 for ln in tail)
                os.replace(AUDIT_PATH + ".keep", AUDIT_PATH)
            except Exception:
                pass
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass                      # 审计写失败不拦业务；诊断页可见目录权限问题


def audit(action, ip, ok, detail="", ms=None):
    """记一条**关键事件**（只记元数据；调用方保证 detail 不含密钥 / 请求体）。"""
    with _audit_lock:
        _write_line(action, ip, ok, detail, ms)


def audit_poll(action, ip, ok, detail="", ms=None):
    """轮询类心跳：同源、同端点、同成败 **60 秒只落一行**，次数写进 detail。

    不聚合会怎样：手机端 1.5 秒问一次进度，两台设备一小时就是几千行，而这份
    文件超过 1MB 就轮转保尾 500 条 —— 配对、吊销、鉴权失败这些真正要看的行
    会在十几分钟内被心跳冲干净，审计就失去了"谁碰过我这台机"的意义（P0-3）。
    窗口里攒下的次数在下一条落盘时带上（`/api/task ×39`），一行也看得懂谁在看。
    """
    key = (str(action), str(ip), str(detail), bool(ok))
    now = time.time()
    with _audit_lock:
        # 顺手清过期窗口（坑 270）：移动网 v6 前缀漂移 / 来源 IP 变化会让键无限
        # 累积；活键每 60 秒只一条，过期项清掉成本极低，别让这个 dict 只增不减。
        # 过期但攒过多次的窗口先补一条 ×N 聚合行进文件（原来靠"同源下次调用"带出，
        # 源再也不来就永久丢了），再删。
        stale_keys = [k for k, g in _poll_gate.items()
                      if now - g[0] >= POLL_WINDOW_S]
        for k in stale_keys:
            g = _poll_gate.pop(k, None)
            if g and g[1] > 1:
                _write_line(k[0], k[1], k[3],
                            "%s ×%d" % (k[2], g[1]), None)
        gate = _poll_gate.get(key)
        if gate and now - gate[0] < POLL_WINDOW_S:
            gate[1] += 1
            return False
        if gate:
            detail = "%s ×%d" % (detail, gate[1])
        _poll_gate[key] = [now, 1]
        _write_line(action, ip, ok, detail, ms)
    return True


def poll_reset():
    """清掉聚合窗口（服务重启与自检用）。"""
    with _audit_lock:
        _poll_gate.clear()



def read_audit(limit=200):
    """读最近 limit 条审计（设置页"查看"用；文件按行倒序取尾）。"""
    try:
        with _audit_lock:
            with open(AUDIT_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def clear_audit():
    try:
        with _audit_lock:
            with open(AUDIT_PATH, "w", encoding="utf-8") as f:
                f.write("")
        return True
    except Exception:
        return False
