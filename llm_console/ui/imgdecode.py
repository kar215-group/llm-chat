"""图片解码补口：Tk 8.6 画不出的 JPEG / WEBP / BMP（坑 132）。

Pillow 是本项目第一个运行期第三方依赖（2026-10-07 W 定「轻量、低第三方依赖」），
配套纪律三条：
· 只在 ui 层用：core / connection 不得 import 本模块（同 widgets 的分层规矩，
  AST 扫 import 照查）；
· Pillow 懒加载：import 放在函数里 —— 没装 Pillow（源码运行）时降级返回 None，
  调用方退回占位样图（ui/file_icons），行为与引入 Pillow 之前完全一致；
· 打包走 tools/pyinstaller_hooks/hook-PIL.py 精简钩子：只收解码需要的
  _imaging / _webp 与几个插件，_avif(7.9MB) / _imagingft(2.2MB) / _imagingcms
  这些用不到的大件一律不进包（W 定：exe 体积影响压到最低，不伤 Linux 适配）。
"""

import base64
import io
import tkinter as tk


def photo(path, max_px):
    """图片文件 → tk.PhotoImage（最长边缩进 max_px 内，只缩不放），失败返回 None。

    Pillow 优先（全部格式统一 LANCZOS 缩放，质量比 Tk 8.6 只有最近邻的
    subsample 好 —— 坑 122，这是"桌面级真实缩略图"的另一半）；Pillow 缺席
    或解码失败时退 Tk 原生（PNG / 静态 GIF 照旧整数倍 subsample）；再不行
    返回 None，调用方用占位样图兜底。本函数不抛：任何一步失败都是 None。
    """
    img = _via_pillow(path, max_px)
    if img is not None:
        return img
    try:
        img = tk.PhotoImage(file=path)
        f = max(1, int(round(max(img.width(), img.height()) / float(max_px))))
        if f > 1:
            img = img.subsample(f, f)
        return img
    except Exception:
        return None


def photo_of(im, fast=False):
    """PIL 图 → tk.PhotoImage（经内存里的 PNG）。失败返回 None，不抛。

    走 PNG 而不是 ImageTk：本项目的图片路径一律**不依赖 `PIL.ImageTk`**
    （它在部分 Pillow 构建里缺席），与 `_via_pillow` 同一条路。

    `fast=True` 只把 PNG 压缩降档（6 → 1）：交互式重画（涂抹预览）要的是"这一帧
    什么时候出来"，临时 PNG 大一点无所谓 —— 实测这一步占单帧成本的大头。
    """
    try:
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1 if fast else 6)
        return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
    except Exception:
        return None


def _via_pillow(path, max_px):
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)      # 手机照片按 EXIF 摆正
            if im.mode not in ("RGB", "RGBA"):
                alpha = im.mode in ("LA", "PA") or "transparency" in im.info
                im = im.convert("RGBA" if alpha else "RGB")
            im.thumbnail((max_px, max_px), getattr(Image, "Resampling", Image).LANCZOS)
        return photo_of(im)
    except Exception:
        return None
