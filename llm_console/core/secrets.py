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
from .device_ua import valid_cid as _valid_cid

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
#
# 两层身份（2026-10-15 persona 模型，坑 283）：
#   · remote_devices[]  = **连接入口/凭证**（一条 Cookie token 一条；同一台手机从
#     局域网地址与设备直连地址分别配对，就是这里两条）。
#     条目：{id:dev_N, name, token_hash, persona:p_N, os, cid, last_ip, agent,
#           added, last_seen}
#   · remote_personas[] = **逻辑设备**（同一台物理设备的多个入口归一）。
#     条目：{id:p_N, name(备注), os_set:[系统 key…], cids:[客户端持久 id…],
#           merged_into:""(合并重定向), added}
# 历史产物 / 聊天会话 / 上传归属一律按 **persona id**；dev_N 老条目不做兼容映射
#（W 2026-10-15：升级即断层，旧台账条目对手机端不可见，无所谓）。
# ---------------------------------------------------------------------------

_PERSONA_CID_CAP = 8      # 每个逻辑设备记多少个客户端 cid（同设备多浏览器够用即可）


def _devices_locked(data):
    d = data.get("remote_devices")
    return [dict(x) for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def _personas_locked(data):
    p = data.get("remote_personas")
    return [dict(x) for x in p if isinstance(x, dict)] if isinstance(p, list) else []


def remote_personas():
    """逻辑设备清单（返回副本）。"""
    with _LOCK:
        return _personas_locked(load_secrets())


def ensure_personas():
    """老数据一次性迁移：给每条没有效 persona 的凭证建一个独立逻辑设备。

    **只在 RemoteServer.start() 调一次**，不挂进 load_secrets（那是鉴权热路径，
    多线程首调并发写盘会互踩）。幂等判据 = 每条凭证的 persona 都指向存在的 persona。
    文件存在但**解析失败时绝不写盘**：load 对损坏文件返回 {}，照存会把 api_keys /
    github_token / 设备表整份抹掉（坑 283 评审加固点）。返回是否做了迁移。
    """
    with _LOCK:
        try:
            if os.path.exists(SECRETS_PATH):
                with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    return False
            else:
                raw = {}
        except Exception:
            return False                 # 损坏：不迁移、不覆盖
        data = load_secrets()
        devs = _devices_locked(data)
        pers = _personas_locked(data)
        pids = {p.get("id") for p in pers}
        changed = False
        for d in devs:
            if d.get("persona") in pids:
                continue
            pid = "p_%d" % (len(pers) + 1)
            while pid in pids:
                pid += "x"
            os_key = str(d.get("os") or "unknown")
            pers.append({"id": pid, "name": str(d.get("name") or "未知设备")[:20],
                         "os_set": [os_key] if os_key != "unknown" else [],
                         "cids": [], "merged_into": "",
                         "added": str(d.get("added") or "")})
            d["persona"] = pid
            pids.add(pid)
            changed = True
        if changed:
            data["remote_personas"] = pers
            data["remote_devices"] = devs
            save_secrets(data)
        return changed


def _terminal_persona(pers, pid):
    """沿 merged_into 走到最终 persona；环 / 不存在返回 None。"""
    seen = set()
    cur = str(pid or "")
    while cur and cur not in seen:
        seen.add(cur)
        p = next((x for x in pers if x.get("id") == cur), None)
        if p is None:
            return None
        nxt = str(p.get("merged_into") or "")
        if not nxt:
            return p
        cur = nxt
    return None


def resolve_persona(x):
    """归属键 → 最终 persona id；`"local"` 短路；认不出 / 已吊销 → None（fail-closed）。

    只认 p_N（含合并重定向）。**不认 dev_N**：升级前老台账条目不做兼容（W 拍板）；
    凭证本身的归属在配对落盘时就已写成 persona，不需要从 dev_N 反查。
    """
    s = str(x or "")
    if s == "local":
        return "local"
    if not s.startswith("p_"):
        return None
    with _LOCK:
        p = _terminal_persona(_personas_locked(load_secrets()), s)
        return p.get("id") if p else None


def find_persona_by_cid(cid):
    """按客户端持久 id 找逻辑设备（自动合并用）；cid 脏 / 多设备撞号（不该发生）→ None。"""
    c = _valid_cid(cid)
    if not c:
        return None
    with _LOCK:
        hits = [p for p in _personas_locked(load_secrets())
                if not p.get("merged_into") and c in (p.get("cids") or [])]
        return dict(hits[0]) if len(hits) == 1 else None


def _next_pid(pers):
    pid = "p_%d" % (len(pers) + 1)
    pids = {p.get("id") for p in pers}
    while pid in pids:
        pid += "x"
    return pid


def finalize_pairing(token_hash, now_iso, *, name, os_key, cid, ip, agent,
                     merge_pid=""):
    """配对确认后的**唯一落盘出口**（坑 283）：一把锁内建/并 persona + 加凭证。

    merge_pid 非空 = 用户在弹窗显式选了并入某台已有设备；等待确认的 120s 里那台
    可能已被吊销 → 落盘前复验，没了就抛 KeyError（handler 回 403、不 Set-Cookie）。
    cid 去重追加（上限 _PERSONA_CID_CAP，新的在前、超了丢最旧的）。
    """
    c = _valid_cid(cid)
    with _LOCK:
        data = load_secrets()
        devs = _devices_locked(data)
        pers = _personas_locked(data)
        did = "dev_%d" % (len(devs) + 1)
        while any(d.get("id") == did for d in devs):
            did += "x"
        target = None
        if merge_pid:
            target = _terminal_persona(pers, merge_pid)
            if target is None:
                raise KeyError("merge target gone: %s" % merge_pid)
        else:
            target = {"id": _next_pid(pers),
                      "name": str(name or "未知设备")[:20],
                      "os_set": [os_key] if os_key and os_key != "unknown" else [],
                      "cids": [], "merged_into": "", "added": now_iso}
            pers.append(target)
        if os_key and os_key != "unknown" and os_key not in target.setdefault("os_set", []):
            target["os_set"].append(os_key)
        if c and c not in target.setdefault("cids", []):
            target["cids"] = ([c] + list(target["cids"]))[:_PERSONA_CID_CAP]
        devs.append({"id": did,
                     "name": str(name or "未知设备")[:20],
                     "token_hash": str(token_hash),
                     "persona": target["id"],
                     "os": str(os_key or "unknown"),
                     "cid": c, "last_ip": str(ip or "")[:64],
                     "agent": str(agent or "")[:160],
                     "added": now_iso, "last_seen": now_iso})
        data["remote_personas"] = pers
        data["remote_devices"] = devs
        save_secrets(data)
        return target["id"], did


def merge_persona(src_pid, dst_pid):
    """把 src 整台并进 dst：凭证 reparent、os/cid 合并、src 置 merged_into。

    台账**不重写**：resolve_persona 透传 merged_into，旧归属条目自动连续。
    src==dst / 任一不存在 / dst 自身已被并 → 返回 False。
    """
    with _LOCK:
        data = load_secrets()
        devs = _devices_locked(data)
        pers = _personas_locked(data)
        src = _terminal_persona(pers, src_pid)
        dst = _terminal_persona(pers, dst_pid)
        if src is None or dst is None or src["id"] == dst["id"]:
            return False
        for k in ("os_set", "cids"):
            merged = list(dict.fromkeys(list(dst.get(k) or [])
                                        + list(src.get(k) or [])))
            dst[k] = merged[:_PERSONA_CID_CAP] if k == "cids" else merged
        for d in devs:
            if d.get("persona") == src["id"]:
                d["persona"] = dst["id"]
        src["merged_into"] = dst["id"]
        data["remote_personas"] = pers
        data["remote_devices"] = devs
        save_secrets(data)
        return True


def rename_persona(pid, name):
    """改逻辑设备备注（[:20] 夹取）；persona 不存在返回 False。"""
    nm = str(name or "").strip()[:20]
    if not nm:
        return False
    with _LOCK:
        data = load_secrets()
        pers = _personas_locked(data)
        p = next((x for x in pers if x.get("id") == str(pid)), None)
        if p is None:
            return False
        p["name"] = nm
        data["remote_personas"] = pers
        return save_secrets(data)


def revoke_persona(pid):
    """设备级全局吊销：删 persona（含合并重定向链上的它）+ 它名下**全部**入口凭证。"""
    with _LOCK:
        data = load_secrets()
        devs = _devices_locked(data)
        pers = _personas_locked(data)
        term = _terminal_persona(pers, pid)
        if term is None:
            return False
        # 同一终态的所有 persona id（直接命中 + 已 merged_into 到它的）
        gone = {x.get("id") for x in pers
                if _terminal_persona(pers, x.get("id")) is not None
                and _terminal_persona(pers, x.get("id"))["id"] == term["id"]}
        gone.add(str(pid))
        rest_dev = [d for d in devs if d.get("persona") not in gone]
        rest_per = [p for p in pers if p.get("id") not in gone]
        if len(rest_dev) == len(devs) and len(rest_per) == len(pers):
            return False
        data["remote_personas"] = rest_per
        data["remote_devices"] = rest_dev
        return save_secrets(data)


def personas_with_creds():
    """已配对设备窗的聚合视图：每个**存活** persona 一条，含入口清单与最近活跃。"""
    with _LOCK:
        data = load_secrets()
        devs = _devices_locked(data)
        pers = _personas_locked(data)
    out = []
    for p in pers:
        if p.get("merged_into"):
            continue
        creds = sorted((dict(d) for d in devs if d.get("persona") == p["id"]),
                       key=lambda d: str(d.get("added") or ""))
        last = max((str(d.get("last_seen") or "") for d in creds), default="")
        out.append({"id": p["id"], "name": p.get("name") or "未知设备",
                    "os_set": list(p.get("os_set") or []),
                    "cids": list(p.get("cids") or []),
                    "added": p.get("added") or "", "last_seen": last,
                    "credentials": creds})
    out.sort(key=lambda x: x["last_seen"], reverse=True)
    return out

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
