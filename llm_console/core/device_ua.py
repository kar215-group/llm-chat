# -*- coding: utf-8 -*-
"""llm_console.core.device_ua — 从 User-Agent 识别设备系统类型（坑 282）。

只回答"什么系统"，**不回答手机 / 平板 / 电脑的设备形态**（W 2026-10-15：已配对设备
列表只显示系统类型）。纯函数、零依赖（铁律 1：core 不 import tkinter）。

## 判定口径（顺序就是优先级，别随意调换）

1. 纯血鸿蒙：UA 含 `OpenHarmony` / `HarmonyOS` / `ArkWeb` → HMOS。
   **兼容 AOSP 的鸿蒙 4**（浏览器内核基于 Android WebView，UA 只露出 Android）先归
   Android——没有真机 UA 样本前不猜（W 2026-10-15 Q3：先按关键字，后续真机校准）。
2. iOS：iPhone / iPad / iPod（不区分手机平板）。**iPadOS 请求桌面网站时 UA 伪装
   成 Macintosh**，按规则会落 mac——这是苹果自己的行为，只影响徽标不影响安全。
3. Android → Android；4. Windows → Windows；5. Mac → macOS；6. Linux 系 → Linux；
   认不出 → 未知设备。

标签是要落进配对记录与 UI 的用户可见字符串，改动走铁律 14。
"""

from .. import texts

FAMILIES = (
    ("harmony", ("openharmony", "harmonyos", "arkweb"), "HMOS"),
    ("ios", ("iphone", "ipad", "ipod"), "iOS"),
    ("android", ("android",), "Android"),
    ("windows", ("windows",), "Windows"),
    ("macos", ("mac os x", "macintosh", "mac_powerpc"), "macOS"),
    ("linux", ("x11", "linux", "wayland"), "Linux"),
)

OS_UNKNOWN = ("unknown", texts.DUA_L1)
_LABELS = {key: label for key, _n, label in FAMILIES}
_LABELS["unknown"] = texts.DUA_L1


def os_label(key):
    """系统 key → 显示标签（persona.os_set 里存的是 key，界面渲染用它）。"""
    return _LABELS.get(str(key or ""), texts.DUA_L1)


def os_family(ua):
    """UA 字符串 → `(key, 中文标签)`；空串 / 认不出给 `("unknown","未知设备")`。"""
    s = str(ua or "").lower()
    if not s:
        return OS_UNKNOWN
    for key, needles, label in FAMILIES:
        if any(n in s for n in needles):
            return key, label
    return OS_UNKNOWN


def valid_cid(cid):
    """客户端持久设备标识 cid 的格式闸（坑 283）：32~64 位十六进制或 urlsafe 字符。

    cid 是浏览器 localStorage 里的 128bit 随机串，只用来"同一设备换入口时认出彼此"，
    **不是凭证**（真正的鉴权仍靠配对 token）；但落盘前仍要卡格式/长度，免得配对接口
    变成往 secrets.json 塞任意字符串的口子。
    """
    c = str(cid or "").strip()
    if not (16 <= len(c) <= 128):
        return ""
    ok = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-")
    if any(ch not in ok for ch in c):
        return ""
    return c
