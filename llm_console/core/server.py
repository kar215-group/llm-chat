# -*- coding: utf-8 -*-
"""llm_console.core.server — llama-server 生命周期：命令行组装、启停、三级判活、/props 与 /v1/models 查询"""

import json
import os
import struct
import subprocess
import time
import urllib.error
import urllib.request

from .config import api_headers, base_url, split_extra_args
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
    ERROR_NO_MORE_FILES = 18
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
                    err = ctypes.get_last_error()
                    if err == ERROR_NO_MORE_FILES:
                        return False         # 枚举正常走完 = 没有目标进程（R-75）
                    last = err               # 真失败：交给外层抛，不静默当 False
                    break
        finally:
            k32.CloseHandle(snap)
    raise OSError("进程表快照失败（GetLastError=%s）：本地服务判活不可用" % last)


def _reset_alive_cache():
    """服务启停后立刻作废缓存（避免刚 stop 仍被判为存活）。"""
    _ALIVE_CACHE["t"] = 0.0

def _server_pids():
    """本机所有 llama-server.exe 的 PID 集合（Toolhelp 快照；枚举失败返回空集）。

    与 _probe_snapshot 同一套结构体，只是把"在不在"换成"是哪些"。这里失败**不抛**：
    它只服务 stop_server 的兜底路径，拿不到名单的后果是"一个都不杀 + 如实说没找到"，
    比按映像名盲杀全机安全（排查报告 R-01）。
    """
    import ctypes
    pids = set()
    try:
        k32 = _k32()
        pe_cls = _pe32w()
        TH32CS_SNAPPROCESS = 0x2
        invalid = ctypes.c_void_p(-1).value
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == invalid:
            return pids
        try:
            entry = pe_cls()
            entry.dwSize = ctypes.sizeof(pe_cls)
            if not k32.Process32FirstW(snap, ctypes.byref(entry)):
                return pids
            while True:
                if str(entry.szExeFile or "").lower() == SERVER_IMAGE_NAME:
                    pids.add(int(entry.th32ProcessID))
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    return pids
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return pids

def _listening_pids(port):
    """监听指定 TCP 端口的进程 PID 集合（GetExtendedTcpTable；IPv4 + IPv6）。

    失败（非 Windows / API 不可用 / 端口非法）一律返回空集——调用侧的语义是
    "找不到就不杀任何进程并如实告知"，宁可不动也不能扩大杀伤面（R-01）。
    """
    import ctypes
    out = set()
    try:
        port = int(port)
    except Exception:
        return out
    if not (0 < port < 65536) or os.name != "nt":
        return out
    TCP_TABLE_OWNER_PID_LISTENER = 5
    AF_INET, AF_INET6 = 2, 23
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        for af in (AF_INET, AF_INET6):
            size = ctypes.c_ulong(0)
            # 第一次调用拿所需缓冲大小（返回 ERROR_INSUFFICIENT_BUFFER 是预期行为）
            iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, af,
                                         TCP_TABLE_OWNER_PID_LISTENER, 0)
            if not size.value:
                continue
            buf = ctypes.create_string_buffer(size.value)
            if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, af,
                                            TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
                continue
            # 行布局（DWORD 对齐）：IPv4 = 本地地址4+端口4+远端地址4+端口4+PID4 = 20B；
            # IPv6 = 本地地址16+端口4+远端地址16+端口4+PID4 = 44B。
            # 端口 DWORD 的前 2 字节是网络序端口号；PID 在行尾、本机序。
            addr_len = 4 if af == AF_INET else 16
            row_len = 2 * addr_len + 12
            n = struct.unpack_from("I", buf.raw, 0)[0]
            for i in range(n):
                off = 4 + i * row_len
                if off + row_len > size.value:
                    break
                lport = struct.unpack_from("!H", buf.raw, off + addr_len)[0]
                if lport == port:
                    out.add(struct.unpack_from("I", buf.raw, off + row_len - 4)[0])
    except Exception:
        return out
    return out

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
      2) 4 秒内的缓存结果（_ALIVE_TTL；进程状态不会瞬间变化）；
      3) 进程内 Toolhelp 快照实查一次并写缓存。
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
        # 401/403 = 服务活着、只是要鉴权（unauthorized 恰恰是"进程在跑"的最强证据）：
        # 原来把它们折成"进程不在"，状态灯说未运行、还可能被重复启动（R-74）
        return e.code in (200, 401, 403, 503), e.code == 200
    except Exception:
        return False, False

def server_alive(cfg):
    """进程在不在（200=就绪，503=模型加载中，都算在）。"""
    return server_state(cfg)[0]

def server_ready(cfg):
    """是否已就绪（200），模型加载中的 503 不算。"""
    return server_state(cfg)[1]

def build_server_cmd(cfg, ctx=None):
    # ctx 空值兜底（R-77）：坏配置（""/None）原来会 int(None) 直接抛，或发出 -c 0；
    # 按保守的 10240 兜底（与 DEFAULT_CONFIG["ctx"] 同值，宁可小不能炸）
    try:
        ctx_val = int(ctx or cfg.get("ctx") or 10240)
    except Exception:
        ctx_val = 10240
    args = [cfg["exe"], "-m", cfg["model"],
            "-c", str(ctx_val),
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
        # config.split_extra_args：posix shlex 会吃掉 Windows 路径的反斜杠（R-22）
        args += split_extra_args(cfg["extra_args"])
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

def stop_server(cfg=None):
    """停掉本配置在用的 llama-server → 返回杀掉的进程数（0 = 没找到/没动手）。

    兜底路径**绝不按映像名 taskkill**（排查报告 R-01）：同机多开时那会杀掉挂在
    别的端口上跑别的活的无辜实例，且零提示。句柄缺失（外部 vbs 启动等场景）时
    改为按 `cfg["port"]` 反查监听 PID，确认映像名是 llama-server.exe 才按 PID 杀；
    查不到就一个都不杀——调用侧按返回值如实说"没有找到监听端口 %d 的服务进程"。
    """
    global _SERVER_PROC
    if os.name != "nt":
        return 0
    killed = 0
    p = _SERVER_PROC
    if p is not None:
        # 有句柄只杀自己拉起的进程
        try:
            if p.poll() is None:
                p.kill()
                killed = 1
        except Exception:
            pass                        # 进程已退出等情况——目标同样是"它停了"
    else:
        # 句柄缺失：按端口反查 PID（监听者 ∩ llama-server 映像名单），逐个 /pid 杀
        try:
            port = int((cfg or {}).get("port") or 0)
        except Exception:
            port = 0
        targets = _listening_pids(port) & _server_pids() if port else set()
        flags = subprocess.CREATE_NO_WINDOW
        for pid in sorted(targets):
            r = subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                               creationflags=flags,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                killed += 1
    _SERVER_PROC = None
    _reset_alive_cache()
    return killed

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
