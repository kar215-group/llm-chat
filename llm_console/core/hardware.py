# -*- coding: utf-8 -*-
"""llm_console.core.hardware — 该机器硬件探测：GPU 型号/显存（nvidia-smi）与物理内存"""

import os
import subprocess


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
