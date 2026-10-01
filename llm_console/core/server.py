# -*- coding: utf-8 -*-
"""llm_console.core.server — llama-server 生命周期：命令行组装、启停、三级判活、/props 与 /v1/models 查询"""

import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request

from .config import api_headers, base_url
from .models import find_vl_pairs
from .params import current_ngl


_SERVER_PROC = None        # 本程序 Popen 出的 llama-server 句柄（有则零成本判活）

_ALIVE_CACHE = {"t": 0.0, "v": False}

_ALIVE_TTL = 4.0           # 进程状态缓存有效期（秒）：进程状态不会瞬变。

# 判活认的映像名（与老实现里 `tasklist /fi "imagename eq ..."` 的过滤值逐字一致）
SERVER_IMAGE_NAME = "llama-server.exe"

_K32 = None                # 懒初始化的 kernel32 句柄（独立 WinDLL 实例，不动 ctypes.windll 那份）
_PE32W = None              # 懒初始化的 PROCESSENTRY32W 结构体类


def _k32():
    """kernel32 的独立实例 + 只设本模块用到的那几个函数签名。

    单独 WinDLL(use_last_error=True) 而不是复用 ctypes.windll.kernel32：
    ① 要读到真实的 GetLastError（快照失败时区分"瞬时"还是"真不行"）；
    ② 不去改共享实例的 argtypes，避免和 hardware.py 那边互相影响。
    """
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        pe = _pe32w()
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.restype = wintypes.BOOL
        k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(pe)]
        k.Process32NextW.restype = wintypes.BOOL
        k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(pe)]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        _K32 = k
    return _K32


def _pe32w():
    """Win32 PROCESSENTRY32W（Unicode 版）。

    用 W 版而不是 A 版：映像名可能是中文，A 版会按 OEM 代码页给出乱码。
    th32DefaultHeapID 是 ULONG_PTR，64 位下必须占 8 字节（用 POINTER 保证宽度）。
    类只建一次（每次调用重建 Structure 子类实测要十几毫秒，比枚举本身还贵）。
    """
    global _PE32W
    if _PE32W is None:
        import ctypes
        from ctypes import wintypes

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        _PE32W = _PROCESSENTRY32W
    return _PE32W


def _probe_snapshot():
    """进程内枚举系统进程表，看 SERVER_IMAGE_NAME 在不在（不 spawn 子进程）。

    判据与老实现完全一致（同一个映像名、精确匹配），但实测单次
    240~900ms（spawn 一个 tasklist，它还要逐进程取内存/会话信息）→ ~2ms。
    而没有本地服务在跑时，后台每约 6 秒就要付一次、**空闲也永久付费**。

    CreateToolhelp32Snapshot 官方文档明说可能因进程表在快照期间变化而返回
    ERROR_BAD_LENGTH，所以按文档重试几次；重试完仍失败就**抛**——不静默退回
    tasklist，因为那会把"判活坏了"藏起来，表现成状态灯永远说未运行。
    """
    import ctypes
    k32 = _k32()
    pe_cls = _pe32w()
    TH32CS_SNAPPROCESS = 0x2
    ERROR_BAD_LENGTH = 24
    invalid = ctypes.c_void_p(-1).value
    last = 0
    for _attempt in range(5):
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == invalid:
            last = ctypes.get_last_error()
            if last == ERROR_BAD_LENGTH:
                continue                     # 文档说的瞬时失败：重试
            break
        try:
            entry = pe_cls()
            entry.dwSize = ctypes.sizeof(pe_cls)
            if not k32.Process32FirstW(snap, ctypes.byref(entry)):
                last = ctypes.get_last_error()
                break
            while True:
                if str(entry.szExeFile or "").lower() == SERVER_IMAGE_NAME:
                    return True
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    return False
        finally:
            k32.CloseHandle(snap)
    raise OSError("进程表快照失败（GetLastError=%s）：本地服务判活不可用" % last)


def _reset_alive_cache():
    """服务启停后立刻作废缓存（避免刚 stop 仍被判为存活）。"""
    _ALIVE_CACHE["t"] = 0.0

def _probe_tasklist():
    """真查一次系统进程表（只在缓存过期时付一次）。

    名字沿用 _probe_tasklist：它是三级判活的第 3 级，调用点与测试打桩都指这个名字。
    实现已从"spawn tasklist 子进程"换成进程内 Toolhelp 快照（见 _probe_snapshot）。
    """
    if os.name != "nt":
        return True                      # 非 Windows 无进程表可查，退回 HTTP 语义
    return _probe_snapshot()

def server_process_alive():
    """本地进程级检查：llama-server.exe 是否在运行。

    性能关键：server_alive/server_ready 走 HTTP（服务停止时要等 ~2 秒
    TCP 超时）；主线程与高频轮询应先做本检查，进程存在才发 HTTP。

    判活按成本递增的三级：
      1) 本程序启动过的句柄 -> poll()，无系统调用；
      2) 1 秒内的缓存结果（进程状态不会瞬间变化）；
      3) tasklist 实查一次并写缓存。
    """
    p = _SERVER_PROC
    if p is not None and p.poll() is None:
        return True
    now = time.time()
    if now - _ALIVE_CACHE["t"] < _ALIVE_TTL:
        return _ALIVE_CACHE["v"]
    v = _probe_tasklist()
    _ALIVE_CACHE["t"], _ALIVE_CACHE["v"] = now, v
    return v

def server_state(cfg):
    """一次 /health 同时得到 (进程在, 已就绪)；200=就绪，503=加载中。

    状态轮询每 3 秒一轮，此前是 server_alive + server_ready 两次请求；
    合成一次后，服务加载中/未监听时的等待也减半。
    """
    try:
        req = urllib.request.Request(base_url(cfg) + "/health",
                                     headers=api_headers(cfg))
        with urllib.request.urlopen(req, timeout=2) as r:
            return True, r.status == 200
    except urllib.error.HTTPError as e:
        return e.code in (200, 503), e.code == 200
    except Exception:
        return False, False

def server_alive(cfg):
    """进程在不在（200=就绪，503=模型加载中，都算在）。"""
    return server_state(cfg)[0]

def server_ready(cfg):
    """是否已就绪（200），模型加载中的 503 不算。"""
    return server_state(cfg)[1]

def build_server_cmd(cfg, ctx=None):
    args = [cfg["exe"], "-m", cfg["model"],
            "-c", str(int(ctx or cfg["ctx"])),
            "--n-gpu-layers", str(current_ngl(cfg)),
            "--host", cfg["host"], "--port", str(cfg["port"])]
    if cfg.get("api_key"):
        args += ["--api-key", cfg["api_key"]]
    if cfg.get("threads"):
        args += ["--threads", str(cfg["threads"])]
    rm = cfg.get("reasoning_mode", "default")
    if rm == "off":
        args += ["--no-reasoning"]
    elif rm == "budget":
        args += ["--reasoning-budget", str(cfg.get("reasoning_budget", 1024))]
    if str(cfg.get("extra_args", "")).strip():
        args += shlex.split(str(cfg["extra_args"]))
    # 可看图模型：附加视觉投影器（mmproj）——先查记录，无记录则自动配对回退
    b = os.path.basename(cfg.get("model", ""))
    proj = (cfg.get("model_mmproj") or {}).get(b)
    if not (proj and os.path.isfile(proj)):
        proj = find_vl_pairs(cfg).get(cfg.get("model", ""))
    if proj and os.path.isfile(proj):
        args += ["--mmproj", proj]
    return args

def start_server(cfg, ctx=None):
    """启动 llama-server，返回进程句柄（供关闭时中止加载用）。

    ctx 显式给定时用它（agent 场景按 model_ctx_api 记录；GUI 场景用 model_ctx）。
    """
    global _SERVER_PROC
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.Popen(build_server_cmd(cfg, ctx),
                         creationflags=flags,
                         cwd=os.path.dirname(cfg["exe"]) or None,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _SERVER_PROC = p                     # 记句柄：判活走 poll()，不再 tasklist
    _reset_alive_cache()
    return p

def stop_server():
    global _SERVER_PROC
    if os.name != "nt":
        return
    p = _SERVER_PROC
    if p is not None:
        # 有句柄只杀自己拉起的进程：taskkill 按映像名会误伤用户手动开的其他
        # llama-server 实例（它们可能挂在完全不同的端口上跑别的活）。
        try:
            p.kill()
        except Exception:
            pass                        # 进程已退出等情况——目标同样是"它停了"
    else:
        # 句柄缺失（外部 vbs 启动等场景）：退回按映像名兜底（原有行为）
        flags = subprocess.CREATE_NO_WINDOW
        subprocess.run(["taskkill", "/f", "/im", "llama-server.exe"],
                       creationflags=flags,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _SERVER_PROC = None
    _reset_alive_cache()

def _query_serving_model(cfg):
    """查询当前服务实际加载的模型（文件名）；失败返回 None。"""
    try:
        req = urllib.request.Request(base_url(cfg) + "/v1/models",
                                     headers=api_headers(cfg))
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            items = data.get("data") or []
            mid = items[0].get("id", "") if items else ""
            return os.path.basename(mid) if mid else None
    except Exception:
        return None

def _query_serving_ctx(cfg):
    """查询当前服务实际加载的 n_ctx（/props）；失败返回 None。"""
    try:
        req = urllib.request.Request(base_url(cfg) + "/props",
                                     headers=api_headers(cfg))
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return int((data.get("default_generation_settings") or {}).get("n_ctx") or 0) or None
    except Exception:
        return None
