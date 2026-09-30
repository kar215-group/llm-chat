# -*- coding: utf-8 -*-
"""llm_console.core.secrets — 云端密钥存储（单独文件，**不进备份**）。

为什么不放 gui_config.json：那个文件会随备份打包，云端 key 进去就会跟着备份外流。
文件缺失或损坏时返回空表、不抛异常——它是可选文件，不该让界面崩掉；
缺 key 的提示由调用方给（设置页与发送前的校验）。
"""
import json
import os
import threading

from .config import APP_DIR

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
    """写回密钥表；返回是否成功（失败不抛，由调用方提示）。"""
    with _LOCK:
        try:
            with open(SECRETS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False


def get_api_key(provider_id):
    return str((load_secrets().get("api_keys") or {}).get(provider_id, "") or "")


def set_api_key(provider_id, key):
    """写入（key 为空则删除该 provider 的条目）。"""
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


def mask(key):
    """界面显示用：只露首尾，避免截屏/录屏泄漏完整 key。"""
    k = str(key or "")
    if not k:
        return "（未设置）"
    if len(k) <= 8:
        return "*" * len(k)
    return "%s…%s（共 %d 字符）" % (k[:4], k[-4:], len(k))
