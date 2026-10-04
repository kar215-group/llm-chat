# -*- coding: utf-8 -*-
"""llm_console.core.hardware — 该机器硬件探测：GPU 型号/显存（nvidia-smi）、GPU 厂商与物理内存"""

import os
import subprocess

# 显卡名 / 适配器描述里的厂商标记，**按这个顺序判**（先命中的先算），所以更具体的写在前面。
# `ati ` 带尾空格：免得把 "Innovation" 之类误认成 ATI。
_VENDOR_TOKENS = (
    ("nvidia", ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")),
    ("amd", ("amd", "radeon", "ryzen", "firepro", "ati ")),
    ("intel", ("intel", "uhd graphics", "iris")),
    ("adreno", ("adreno", "qualcomm", "snapdragon")),
)

# Linux 侧 `/sys/class/drm/card*/device/vendor` 的 PCI 厂商号（读文件，不 spawn `lspci`）
_PCI_VENDOR = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel", "0x5143": "adreno"}

# 一次最多看多少个显示适配器：实测 W 的机器（2026-10-04）报了 4 个 Intel 核显 +
# 4 个 NVIDIA + 8 个 MuMu 虚拟显示器，16 个就截断会漏掉后面的真卡，故放宽到 64。
_MAX_ADAPTERS = 64


def detect_gpu():
    """经 nvidia-smi 读取 GPU 型号与显存；失败返回 ("", 0)。"""
    cmds = [r"C:\Windows\System32\nvidia-smi.exe", "nvidia-smi"]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    for c in cmds:
        try:
            out = subprocess.run([c, "--query-gpu=name,memory.total",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True,
                                 timeout=10, creationflags=flags)
            for line in out.stdout.splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 2 and parts[1].isdigit():
                    return parts[0], round(int(parts[1]) / 1024.0, 1)
        except Exception:
            continue
    return "", 0


def classify_gpu(name):
    """把显卡名 / 适配器描述归到厂商标记（nvidia / amd / intel / adreno）；认不出返回空串。

    **认不出不猜** —— 虚拟显示器（MuMu）、Microsoft Basic Display Adapter、远程桌面适配器
    都归空串，否则"这台机器有没有 N 卡"的判断会被它们污染。
    """
    s = " %s " % str(name or "").strip().lower()
    for kind, toks in _VENDOR_TOKENS:
        for t in toks:
            if t in s:
                return kind
    return ""


def detect_adapters():
    """列出本机显示适配器的描述（去重、去空、保序）；一条都没有返回 []。

    Windows 走 `EnumDisplayDevicesW`（纯 ctypes，**不 spawn 外部命令** —— 坑 4 那条纪律：
    热路径里别起进程。实测一次约 5ms，比 `nvidia-smi` 那 10 秒的最坏值便宜三个数量级）；
    Linux 读 `/sys/class/drm/card*/device/vendor`，**直接给厂商标记**。

    一台机器会同时报出核显、独显与虚拟适配器（实测：Intel UHD ×4 + RTX 4060 Laptop ×4 +
    MuMu 虚拟显示器 ×8），所以这里只"如实列出"，取舍留给调用方
    （`core.engine_install.recommend_flavor`）。
    """
    out = []
    try:
        if os.name == "nt":
            import ctypes

            class _DID(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong),
                            ("DeviceName", ctypes.c_wchar * 32),
                            ("DeviceString", ctypes.c_wchar * 128),
                            ("StateFlags", ctypes.c_ulong),
                            ("DeviceID", ctypes.c_wchar * 128),
                            ("DeviceKey", ctypes.c_wchar * 128)]

            edd = ctypes.windll.user32.EnumDisplayDevicesW
            for i in range(_MAX_ADAPTERS):
                d = _DID()
                d.cb = ctypes.sizeof(_DID)
                if not edd(None, i, ctypes.byref(d), 0):
                    break
                s = str(d.DeviceString or "").strip()
                if s and s not in out:
                    out.append(s)
        else:
            drm = "/sys/class/drm"
            cards = sorted(os.listdir(drm)) if os.path.isdir(drm) else []
            for card in cards:
                vf = os.path.join(drm, card, "device", "vendor")
                if not os.path.exists(vf):
                    continue
                with open(vf, encoding="utf-8") as f:
                    vend = _PCI_VENDOR.get(f.read().strip().lower(), "")
                if vend and vend not in out:
                    out.append(vend)
    except Exception:
        return []          # 探不到不算错误：调用方按"认不出"处理，绝不猜
    return out


def gpu_kinds(cfg=None):
    """本机显卡厂商标记的集合，例如 {"nvidia", "intel"}；**判不出返回空集合**。

    三级判据，便宜的先问（越往后越慢，最坏那一级会 spawn 外部命令）：
      ① `cfg["gpu_name"]` —— 探测预填、也允许用户手改（`config.STR_KEYS`），所以**只按名字
         里的厂商标记认**，认不出（用户填了"无"之类）就继续往下问，不硬当成 N 卡；
      ② `detect_adapters()` 枚举显示适配器（毫秒级、不 spawn）；
      ③ 前两级都没认出 N 卡时才 `detect_gpu()` 兜底 —— 远程桌面 / 无头会话里适配器枚举
         可能看不见独显，而 nvidia-smi 看得见。**没装 N 卡的机器上这一步的花费是
         "找不到文件"级别**（nvidia-smi 根本不存在），不是那 10 秒最坏值。

    调用点纪律（坑 4）：只在用户动作点上用一次（点「刷新版本」的子线程里），别进热路径。
    """
    kinds = set()
    k0 = classify_gpu((cfg or {}).get("gpu_name"))
    if k0:
        kinds.add(k0)
    try:
        ads = detect_adapters()
    except Exception:
        ads = []           # 与本模块其余探测同一条口径：探不到不算错误，按"认不出"处理
    for a in ads:
        k = classify_gpu(a)
        if k:
            kinds.add(k)
    if "nvidia" not in kinds:
        try:
            n2, _v = detect_gpu()
        except Exception:
            n2 = ""
        if str(n2).strip():
            kinds.add(classify_gpu(n2) or "nvidia")
    return kinds


def detect_ram_gb():
    """读取物理内存总量（GB）；失败返回 0。"""
    if os.name != "nt":
        return 0
    try:
        import ctypes

        class _MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MEM()
        st.dwLength = ctypes.sizeof(_MEM)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return round(st.ullTotalPhys / (1 << 30), 1)
    except Exception:
        pass
    return 0
