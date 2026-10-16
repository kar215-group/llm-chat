# -*- coding: utf-8 -*-
"""llm_console.connection.remote_uploads — 附件上传面（参考图 / 遮罩，2026-10-12 模块化）。

这是一条**独立的安全面**（99 K4），服务端硬行为，不靠开关拼：单张 20MB、
四格式、**只信字节魔数不信扩展名与 Content-Type**（坑 209⑧）、落盘名 = 随机 hex
（客户端给的任何名字都不进路径）、登记"哪台设备传的"（跨设备引用拒收）、
TTL + 总量上限惰性清理、启动清孤儿。

口径统一在消费点前一处（坑 218）：遮罩一律归一成 **L 模式 PNG**（引擎 `--mask`
只在 GUI 的 L 口径上真机实测过），参考图**不归一**（GUI 本来就原样交文件）。
"""

import os
import secrets as _pysecrets
import threading
import time

from ..core.config import APP_DIR
from .remote_audit import audit
from .. import texts

# ---- 二期上传面（参考图 / 遮罩）----
UPLOAD_DIR = os.path.join(APP_DIR, "remote_uploads")
UPLOAD_MAX_BYTES = 20 * 1024 * 1024   # 单文件上限（W 2026-10-11 拍板 20MB）
UPLOAD_TTL_S = 2 * 3600               # 传上来多久没用就清（惰性，不起线程）
UPLOAD_MAX_FILES = 20                 # 登记总量上限（超了逐最旧）
UPLOAD_KINDS = ("ref", "mask")

# ---------------------------------------------------------------------------
# 上传登记（参考图 / 遮罩）：内存登记 + 落盘随机名 + 惰性清理
# ---------------------------------------------------------------------------

_uploads_lock = threading.Lock()
_uploads = {}          # name -> {"device_id", "kind", "ts", "path"}


def _sniff_image_ext(raw):
    """魔数 → 真实格式扩展名（认不出来 = 空串）。**只信字节魔数，不信客户端
    的扩展名与 Content-Type**（坑 209⑧ 纪律）。四种 = GUI 附件过滤器同一套
    （ui/chat._IMG_FILTER：png / jpg / jpeg / webp / bmp）。"""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:2] == b"BM":
        return ".bmp"
    return ""


def _sweep_uploads():
    """惰性清理（持锁调）：过 TTL 的、超出总量上限的（逐最旧），连登记带文件。"""
    now = time.time()
    gone = [n for n, u in _uploads.items() if now - u["ts"] > UPLOAD_TTL_S]
    if len(_uploads) - len(gone) > UPLOAD_MAX_FILES:
        rest = sorted((u["ts"], n) for n, u in _uploads.items() if n not in gone)
        gone += [n for _t, n in rest[:len(_uploads) - UPLOAD_MAX_FILES]]
    for n in gone:
        u = _uploads.pop(n, None)
        if u:
            try:
                os.remove(u["path"])
            except Exception:
                pass


def _mask_to_gray_png(raw):
    """遮罩落盘归一：任何输入 → **L 模式 PNG**（坑 218）。

    网页 canvas 导出的是 RGBA（笔画边缘带抗锯齿灰阶），而 GUI maskedit 落盘
    的遮罩是 L 模式 PNG——引擎 `--mask` 只在后者上真机实测过（坑 214 数据），
    不引入第二种口径。Pillow 就是既有运行期依赖（01 §1）；解不开 = 坏图拒收。
    抗锯齿灰阶**保留**（convert("L") 不二值化）：软边缘对引擎是混合权重，
    比硬边更贴近"涂过的地方重绘"的语义。参考图不归一——GUI 同样原样交文件。
    """
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            buf = io.BytesIO()
            im.convert("L").save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        return None


def upload_store(device_id, kind, raw):
    """存一份上传 → `(登记名, 错误文案)`。错误非空 = 没存。

    落盘名 = 随机 hex + **魔数定出来的**扩展名；客户端给的任何名字都不进路径
    （坑 209⑤ 同族）。目录在程序目录 `remote_uploads/`，与台账 / 审计同级。
    遮罩（kind="mask"）落盘前经 `_mask_to_gray_png` 归一成 L 模式 PNG。
    """
    if kind not in UPLOAD_KINDS:
        return "", texts.RUP_L1
    if not raw:
        return "", texts.RUP_L1_2
    if len(raw) > UPLOAD_MAX_BYTES:
        return "", (texts.RUP_CAT
                    % (len(raw) / 1048576.0, UPLOAD_MAX_BYTES // 1048576))
    ext = _sniff_image_ext(raw)
    if not ext:
        return "", texts.RUP_L1_3
    if kind == "mask":
        norm = _mask_to_gray_png(raw)
        if norm is None:
            return "", texts.RUP_L1_3
        raw, ext = norm, ".png"
    with _uploads_lock:
        _sweep_uploads()
        if len(_uploads) >= UPLOAD_MAX_FILES:
            return "", texts.RUP_L1_4
        name = _pysecrets.token_hex(8) + ext
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            path = os.path.join(UPLOAD_DIR, name)
            with open(path, "wb") as f:
                f.write(raw)
        except Exception as e:
            # 原话（含本机绝对路径与用户名）只进本机审计，不回给手机端（坑 209⑥）
            audit("upload", device_id, False, "save failed: %s: %s"
                  % (type(e).__name__, e))
            return "", texts.RUP_L1_5
        _uploads[name] = {"device_id": device_id, "kind": kind,
                          "ts": time.time(), "path": path}
    audit("upload", device_id, True, "%s %dB" % (kind, len(raw)))
    return name, ""


def upload_paths(device_id, names, kind):
    """登记名列表 → 本地路径列表。任何一张对不上（过期 / 不是这台设备的 /
    类型不对 / 文件没了）都整单拒绝："附件已过期，重新选一次。"

    设备判据与产物取回同口径：loopback（device_id == "local"）全可见。
    """
    out = []
    with _uploads_lock:
        _sweep_uploads()
        for n in names:
            u = _uploads.get(str(n or ""))
            if (not u or u["kind"] != kind
                    or (device_id != "local" and u["device_id"] != device_id)
                    or not os.path.isfile(u["path"])):
                return [], texts.RUP_L1_6
            out.append(u["path"])
    return out, ""


def uploads_reset():
    """清空登记与目录（服务重启 / 测试用；文件尽力删，删不掉不留脏登记）。"""
    with _uploads_lock:
        for u in list(_uploads.values()):
            try:
                os.remove(u["path"])
            except Exception:
                pass
        _uploads.clear()


def sweep_orphan_uploads():
    """服务启动时清孤儿文件：登记是内存态，重启即空 —— 目录里比 TTL 还老的
    文件必然没人再引用（新文件留着：可能是并行实例刚收的，宁可放过）。"""
    try:
        now = time.time()
        for n in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, n)
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) > UPLOAD_TTL_S:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass                      # 目录还不存在 = 没孤儿，正常路径
