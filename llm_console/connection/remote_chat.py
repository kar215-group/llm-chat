# -*- coding: utf-8 -*-
"""llm_console.connection.remote_chat — 远程文本聊天（2026-10-12，99 K7）。

W 三条拍板的口径，每条都是刻意的取舍：

  1. **只本地、且不触发换载**：手机发的消息只打电脑端**当前已加载**的那个本地文本
     模型。服务没起 / 还在加载 / 加载的不是能聊的 → 就地拒并指路（`ready()`），
     **一个字都不发**。与 8081 代理那条 `ProxyServer.ensure_model` 的区别是刻意的：
     那条会停当前服务、改写 `cfg["model"]` 并落盘 —— 让远端改这台机器的加载现场
     不合适（而且手机上没有任何人能给"要换模型、可能 OOM"这件事背书）。
  2. **轮询增量**：与 `/api/task` 同构，不占长连接 —— 因此不与 `IDLE_TIMEOUT_S`
     和并发闸门打架（SSE 要每 15 秒写心跳才不断，那是为了"跟手"付的代码量与运维面）。
     页面上"慢半拍"的代价换来了刷新/切后台/弱网的天然可恢复。
  3. **per-设备会话只在服务端内存**：服务重启即清，**不写 `chat_logs`**（那是本机
     "只存不读"的口径，掺进远程内容就毁了它的可回灌契约）。

⚠ **聊天历史永不跨设备**：`remote_history` 那个开关只管**产物取回**，
绝不放宽到会话内容 —— 别的设备能看见这台设备跟模型说了什么，是完全不同量级的事。

流式解析与采样参数完全复用 `connection/stream.py`（本地与云端同一份 SSE 解析、
`friendly_error` 同一份人话），本模块只负责"把事件流变成可轮询的增量 + 会话"。
"""

import os
import queue
import re
import threading
import time

from ..core.models import display_name, scan_models
from ..core.server import _query_serving_model, server_state
from .stream import stream_worker

# 会话上限：按**字符数**从头截（不是按轮数——一轮长思考就能吃掉几千字）。
# 超上下限时 llama-server 会回 400，`friendly_error` 已能把那句原话翻出来，
# 这里先自己截一刀，免得手机上看到"超上下文"却不知道发生了什么。
HISTORY_CHARS = 24000
TEXT_MAX_CHARS = 4000           # 与远程生图的提示词同一条上限

# 用户可见文案（W 2026-10-12 批准清单外的新句，逐字待点；口径 = 只给人话 + 指得出去哪儿修）
NOT_RUNNING_MSG = ("电脑端的文本服务没在跑：在电脑端主界面点「启动服务」。"
                   "远程不代起服务，也不换模型。")
LOADING_MSG = "电脑端服务还在加载模型，过几秒再发。"
NOT_CHAT_MSG = "电脑端现在加载的不是能聊的文本模型（%s）——在电脑端换一个。"
BUSY_MSG = "上一条还在回答（或电脑端正在忙），等它结束再发。"
EMPTY_MSG = "先打点什么。"
TOO_LONG_MSG = "这条太长了（上限 %d 字）。" % TEXT_MAX_CHARS


_PATH_RX = re.compile(r'[A-Za-z]:[\\/]\S+')


def safe_text(msg):
    """错误文本进手机端前抹掉本机绝对路径（坑 238 同一条纪律：`friendly_error`
    翻得好，但它会把 llama-server 的原话带进来，那句里可能有模型文件路径）。"""
    return _PATH_RX.sub("本机路径", str(msg or ""))


# scan_models 的短 TTL 备忘（2026-10-12 评估轮 B1）：`ready()` 被 `/api/caps`
# （进页 + 任务收尾）和每次聊天发送调用，背后是整盘模型目录遍历——GGUF 头侧
# 已有 `_GGUF_CACHE`（core/gguf.py，按 mtime/size 失效），缺的是目录层。
# **只备忘扫描**：服务判活（`server_state`）与在服务的模型
# （`_query_serving_model`）保持实时——那两项是毫秒级本地 HTTP，且服务起停 /
# 换模型要立刻反映出来。3 秒窗口只影响"刚增删的模型文件"。
_SCAN_TTL_S = 3.0
_scan_cache = {"t": 0.0, "val": None}
_scan_lock = threading.Lock()


def _scan_chat_models(cfg):
    with _scan_lock:
        c = _scan_cache
        if c["val"] is not None and time.time() - c["t"] < _SCAN_TTL_S:
            return c["val"]
    val = scan_models(cfg)
    with _scan_lock:
        _scan_cache.update(t=time.time(), val=val)
    return val


def _scan_reset():
    """清扫描备忘（测试换桩后用；生产不需要——3 秒自然过期）。"""
    with _scan_lock:
        _scan_cache.update(t=0.0, val=None)


def ready(cfg):
    """现在能不能聊 → (能不能, 为什么, 在服务的是哪个模型)。

    一次 `/health` + 一次 `/v1/models`（`server_state` 已是合成过的那一次，
    别退回 `server_alive` + `server_ready` 两次请求，坑 232 那类"两个入口"的教训）。
    模型显示名走 `core.models.display_name` 与菜单**同一口径**（坑 128③）。
    """
    alive, ok = server_state(cfg)
    if not alive:
        return False, NOT_RUNNING_MSG, ""
    if not ok:
        return False, LOADING_MSG, ""
    tb = _query_serving_model(cfg) or ""
    _d, chat, _i = _scan_chat_models(cfg)
    for p in chat:
        if os.path.basename(p) == tb:
            return True, "", display_name(cfg, p)
    return False, NOT_CHAT_MSG % (tb or "未知"), ""


class RemoteChat:
    """远程聊天：单槽 + per-设备会话（与 `RemoteJobs` 同款互斥纪律）。

    单槽是**跨设备**的：本机一份 llama-server 只有一条解码队列，两部手机同时发
    只会互相拖慢，所以第二台直接吃"上一条还在回答"。与 GUI 的互斥仍是双向的
    （`App._any_busy` ↔ `busy()`，坑 226），生图也一样——聊天在跑时手机提交生图
    会被拒，反过来也是。
    """

    def __init__(self, cfg, local_busy=None):
        self.cfg = cfg
        self._local_busy = local_busy or (lambda: False)
        self._lock = threading.Lock()        # 护 _job 与 _sessions
        self._job = None                     # 运行中的那条（单槽的"槽"就是它）
        self._sessions = {}                  # device_id -> [ {role, content} ]

    # ---- 状态 ----

    def busy(self):
        """有没有**正在跑**的那条。不抢锁（同 `RemoteJobs.busy`：主线程每 3 秒问一次，
        不能被提交路径的锁卡住）。"""
        job = self._job
        return bool(job and job["state"] == "running")

    def _owner_ok(self, job_dev, device_id):
        """会话归属判据：**只认本设备**，loopback 例外（本机浏览器要能看自己在干什么）。
        这里**不看** `remote_history` —— 那个开关只放宽产物取回，不放宽聊天内容。"""
        return device_id == "local" or job_dev == device_id

    # ---- 发送 ----

    def send(self, device_id, text):
        """提交一条 → `(能不能, 错误文案)`。判据顺序：内容 → 自己忙 → 本机忙 → 就绪。"""
        text = str(text or "").strip()
        if not text:
            return False, EMPTY_MSG
        if len(text) > TEXT_MAX_CHARS:
            return False, TOO_LONG_MSG
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                return False, BUSY_MSG
            if self._local_busy():
                return False, BUSY_MSG
        ok, why, _m = ready(self.cfg)
        if not ok:
            return False, why
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                return False, BUSY_MSG      # 二次判：上面那次在锁外
            hist = list(self._sessions.get(device_id) or [])
            hist.append({"role": "user", "content": text})
            self._sessions[device_id] = _trim(hist)
            job = {"device_id": device_id, "state": "running",
                   "answer": "", "thinking": "", "error": "", "usage": None,
                   "lock": threading.Lock(),
                   "messages": list(self._sessions[device_id])}
            q = queue.Queue()
            stop = threading.Event()
            job["stop"] = stop
            self._job = job
        threading.Thread(target=self._produce, args=(job, q, stop),
                         daemon=True, name="remote-chat").start()
        threading.Thread(target=self._drain, args=(job, q),
                         daemon=True, name="remote-chat-io").start()
        return True, ""

    def _produce(self, job, q, stop):
        try:
            stream_worker(self.cfg, job["messages"], q, stop)
        except Exception as e:                    # stream_worker 自己兜异常，这里只兜调用点
            q.put(("error", str(e)))
        q.put(("__end__", None))

    def _drain(self, job, q):
        """把事件并进 job 的两个缓冲：内存上就是一份回答的长度，不攒无界的队列。"""
        while True:
            kind, val = q.get()
            if kind == "__end__":
                break
            with job["lock"]:
                if kind == "content":
                    job["answer"] += val or ""
                elif kind == "reasoning":
                    job["thinking"] += val or ""
                elif kind == "usage":
                    job["usage"] = val
                elif kind == "error":
                    job["error"] = safe_text(val or "出错了")
                    job["state"] = "failed"
                elif kind == "stopped":
                    job["state"] = "cancelled"
                elif kind == "done":
                    # 没收到收尾标志 = 流被提前掐断（`stream.py` 的 `incomplete`），
                    # 内容照留但如实标出来，页面会提示"可能不完整"
                    job["state"] = "incomplete" if val == "incomplete" else "done"
        with job["lock"]:
            ans = job["answer"]
            if job["state"] != "failed" and ans.strip():
                # 半截回答也写回会话（与本机 GUI 同口径：已经生成了的不作废）
                with self._lock:
                    hist = list(self._sessions.get(job["device_id"]) or [])
                    hist.append({"role": "assistant", "content": ans})
                    self._sessions[job["device_id"]] = _trim(hist)
            if job["state"] == "running":
                job["state"] = "done"
            # **不清 _job**：终态与残余文本留着给下一次轮询——手机切后台/亮屏后
            # 才回来问的话，这里清了它就只看到"idle + 空回答"，整段回答凭空消失

    # ---- 查询 / 停止 / 新对话 ----

    def poll(self, device_id, sa=0, st=0, want_hist=False):
        """增量出答：客户端带着它已经收到过的偏移，这里只给它没看过的部分。

        刷新/切后台不用特殊处理：页面重开时带 `hist=1` 与 `sa=0`，
        运行中的那条整段重放、跑完的从会话里读 —— 恢复是这套结构的赠品。
        """
        with self._lock:
            job = self._job
            hist = list(self._sessions.get(device_id) or [])
        out = {"state": "idle", "answer": "", "thinking": "", "sa": sa,
               "st": st, "error": "", "usage": None, "turns": len(hist)}
        if job is not None and self._owner_ok(job["device_id"], device_id):
            with job["lock"]:
                out.update(state=job["state"], answer=job["answer"][sa:],
                           thinking=job["thinking"][st:],
                           sa=len(job["answer"]), st=len(job["thinking"]),
                           error=job["error"], usage=job["usage"])
        if want_hist:
            out["history"] = [{"role": h["role"], "content": h["content"][:8000]}
                              for h in hist]
        return out

    def cancel(self, device_id):
        with self._lock:
            job = self._job
            # 只看**正在跑**的那条：终态留着是为了让晚来的轮询看得到回答，
            # 不是为了让"停止"能对一条已经答完的话再停一次
            if (job is None or job["state"] != "running"
                    or not self._owner_ok(job["device_id"], device_id)):
                return False
            job["stop"].set()
        # 打断口径与本机 GUI 的「停止」逐字一致：置 stop_flag，由 `stream.py` 的解析
        # 循环**每收到一行**查一次。不在这儿强关那条 HTTP 连接 —— 本机那条链路也
        # 这么做，多写一套"关连接"的代码只是多一个没人验证过的分支（W：不过度防御）。
        return True

    def clear(self, device_id):
        with self._lock:
            n = len(self._sessions.get(device_id) or [])
            self._sessions.pop(device_id, None)
        return n


def _trim(hist):
    """按字符数从头截，保住最近的那些（并尽量从 user 开头，免得回灌成半句话）。"""
    total = sum(len(h["content"]) for h in hist)
    out = list(hist)
    while total > HISTORY_CHARS and len(out) > 1:
        total -= len(out[0]["content"])
        out.pop(0)
    while out and out[0]["role"] != "user":
        total -= len(out[0]["content"])
        out.pop(0)
    return out
