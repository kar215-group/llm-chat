# -*- coding: utf-8 -*-
"""llm_console.core.secrets — 云端密钥存储（单独文件，**不进备份**）。

为什么不放 gui_config.json：那个文件会随备份打包，云端 key 进去就会跟着备份外流。
文件缺失或损坏时返回空表、不抛异常——它是可选文件，不该让界面崩掉；
缺 key 的提示由调用方给（设置页与发送前的校验）。
"""
import json
import os
import threading

from .config import APP_DIR, atomic_write_json

SECRETS_PATH = os.path.join(APP_DIR, "secrets.json")

# 可重入锁：save_secrets 自带锁，调用方若已持锁再调不会自我死锁
# （同 _CFG_LOCK 的教训，见交接文档坑 12）
_LOCK = threading.RLock()


def load_secrets():
    """读取密钥表；文件不存在 / 解析失败一律返回 {}。"""
    with _LOCK:
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def save_secrets(data):
    """写回密钥表；返回是否成功（失败不抛，由调用方提示）。原子写（见 config.atomic_write_json）。"""
    with _LOCK:
        return atomic_write_json(SECRETS_PATH, data)


def get_api_key(provider_id):
    return str((load_secrets().get("api_keys") or {}).get(provider_id, "") or "")


def set_api_key(provider_id, key):
    """写入（key 为空则删除该 provider 的条目）。

    读-改-写全程持锁（RLock 可重入，内部的 load/save 不会再卡）：两个调用方并发时
    后写者不能拿"改之前的快照"把先写者的条目覆盖掉。
    """
    with _LOCK:
        data = load_secrets()
        data.setdefault("version", 1)
        keys = data.setdefault("api_keys", {})
        if str(key or "").strip():
            keys[str(provider_id)] = str(key).strip()
        else:
            keys.pop(str(provider_id), None)
        return save_secrets(data)


def has_api_key(provider_id):
    return bool(get_api_key(provider_id))


def get_github_token():
    """开发者选项里填的 GitHub 令牌（「检查更新」带认证时用；没填返回空串）。

    为什么也放这份文件：它是一把凭据，和云端 key 一样只该留在本机、不进 gui_config.json
    （那个文件会随备份一起外流）。它是**独立的键**、不塞进 `api_keys` —— 那里按 provider id
    索引，塞一个假的 provider 进去会污染"这台机器配了哪几家"的判据。
    """
    return str(load_secrets().get("github_token", "") or "")


def set_github_token(token):
    """写入 GitHub 令牌（空则删除该键）。读-改-写持锁，理由同 `set_api_key`。"""
    with _LOCK:
        data = load_secrets()
        data.setdefault("version", 1)
        t = str(token or "").strip()
        if t:
            data["github_token"] = t
        else:
            data.pop("github_token", None)
        return save_secrets(data)


def mask(key):
    """界面显示用：只露首尾，避免截屏/录屏泄漏完整 key。"""
    k = str(key or "")
    if not k:
        return "（未设置）"
    if len(k) <= 8:
        return "*" * len(k)
    return "%s…%s（共 %d 字符）" % (k[:4], k[-4:], len(k))


# ---------------------------------------------------------------------------
# 远程访问的已配对设备（2026-10-11，99 K1）。与 api_keys / github_token 同文件分键：
# 也是一把凭据，只留在本机、不进 gui_config.json（备份会带走）。**token 本体只在
# 配对成功那一刻下发给设备，本机只存 SHA-256 哈希** —— secrets.json 意外外流也
# 直接冒充不了任何设备（哈希不可逆推，坑 209 的"密钥文件最小暴露"纪律）。
# 条目形状：{id, name, token_hash, added, last_seen}
# ---------------------------------------------------------------------------

def remote_devices():
    """已配对设备清单（返回副本，改它不落盘）。"""
    with _LOCK:
        data = load_secrets().get("remote_devices")
        return [dict(d) for d in data if isinstance(d, dict)] \
            if isinstance(data, list) else []


def find_remote_device_by_hash(token_hash):
    """按 token 哈希找设备（鉴权用）；没配对返回 None。"""
    h = str(token_hash or "")
    if not h:
        return None
    for d in remote_devices():
        if d.get("token_hash") == h:
            return d
    return None


def _save_devices(devices):
    data = load_secrets()
    data.setdefault("version", 1)
    data["remote_devices"] = devices
    return save_secrets(data)


def add_remote_device(name, token_hash, now_iso):
    """登记一台新设备（配对确认成功时调）；返回设备 id。读-改-写全程持锁。"""
    with _LOCK:
        did = "dev_%d" % (len(remote_devices()) + 1)
        while any(d.get("id") == did for d in remote_devices()):
            did += "x"
        _save_devices(remote_devices() + [{
            "id": did, "name": str(name or "未命名设备"),
            "token_hash": str(token_hash), "added": now_iso,
            "last_seen": now_iso}])
        return did


def remove_remote_device(dev_id):
    """吊销一台设备（返回是否真的删了）。"""
    with _LOCK:
        devices = remote_devices()
        rest = [d for d in devices if d.get("id") != str(dev_id)]
        if len(rest) == len(devices):
            return False
        return _save_devices(rest)


def touch_remote_device(dev_id, now_iso):
    """更新 last_seen（鉴权成功时顺手记；失败静默 —— 回显丢了不值得报）。"""
    with _LOCK:
        devices = remote_devices()
        changed = False
        for d in devices:
            if d.get("id") == str(dev_id):
                d["last_seen"] = now_iso
                changed = True
        return _save_devices(devices) if changed else True
