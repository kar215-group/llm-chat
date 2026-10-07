# -*- coding: utf-8 -*-
"""生成附件条文件瓷砖的图标 base64（开发机工具，产品代码不 import 它）。

   D:/Python/python.exe tools/make_file_icons.py <文档样图.jpg>

产出 `llm_console/ui/file_icons.py`，四个 base64 块：

  DOC_PNG_B64 / DOC_PNG_B64_BIG    文档瓷砖的样图（48 / 96px，源图由 W 指定）
  IMG_PNG_B64 / IMG_PNG_B64_BIG    Tk 画不出的图片格式的占位图（48 / 96px，本脚本画的）

为什么内嵌成 base64：与 app_icon.py 同一条理由 —— 单文件 exe 里外部资源要么跟
`--add-data` 再算一遍 `_MEIPASS`，要么就地找不到（坑 62 同族）。

为什么源图是 jpg 却要转成 PNG 内嵌：Tk 8.6 的 `tk.PhotoImage` 只认 PNG / 静态 GIF
（坑 132），运行期读不了 jpg；样图只能在开发机上先转好烧进去。

为什么两档：Tk 的图**不跟 `tk scaling` 走**（app_logo 同款教训），高 DPI 屏用 96 档、
其余用 48 档，取哪档由界面缩放决定（阈值 1.75，与 app_logo 一致）。

本脚本需要 Pillow —— 它是**开发机的造资源工具**，产物是静态文件，
运行期的 GUI 与打包流水线都不依赖 Pillow（零第三方依赖这条没被破坏）。
"""

import base64
import io
import os
import sys
import textwrap

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("需要 Pillow（只在开发机上装）：python -m pip install pillow")

SMALL_PX = 48         # 100%~150% 缩放档
BIG_PX = 96           # 200% 缩放档（tk scaling >= 1.75）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_MOD = os.path.join(ROOT, "llm_console", "ui", "file_icons.py")

# 占位图的配色：跟界面灰底白瓷砖一个色系，不抢视觉
_FRAME = (143, 155, 168, 255)
_FILL = (238, 242, 246, 255)
_SUN = (183, 196, 207, 255)


def png_bytes(im):
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


def draw_img_glyph(px):
    """"图片"占位图：圆角框 + 太阳 + 山，一眼能跟文档样图区分开。"""
    im = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    u = px / 96.0                       # 按 96 档的坐标画，48 档等比缩
    d.rounded_rectangle([4 * u, 10 * u, 92 * u, 86 * u], radius=10 * u,
                        fill=_FILL, outline=_FRAME, width=max(2, int(4 * u)))
    d.ellipse([20 * u, 26 * u, 40 * u, 46 * u], fill=_SUN)
    d.polygon([(14 * u, 78 * u), (38 * u, 50 * u), (54 * u, 66 * u),
               (68 * u, 52 * u), (84 * u, 78 * u)], fill=_FRAME)
    return im


def _b64_block(name, im):
    raw = png_bytes(im)
    b64 = base64.b64encode(raw).decode("ascii")
    body = "\n".join('    "%s"' % ln for ln in textwrap.wrap(b64, 76))
    print("  %s：%dpx  PNG %d 字节 → base64 %d 字符" % (name, im.width, len(raw), len(b64)))
    return "%s = (\n%s\n)" % (name, body)


def main():
    if len(sys.argv) != 2:
        sys.exit("用法：python tools/make_file_icons.py <文档样图.jpg>")
    doc = Image.open(sys.argv[1]).convert("RGBA")
    print("文档样图 %s → %s" % (os.path.basename(sys.argv[1]), "x".join(map(str, doc.size))))
    blocks = [
        _b64_block("DOC_PNG_B64", doc.resize((SMALL_PX, SMALL_PX), Image.LANCZOS)),
        _b64_block("DOC_PNG_B64_BIG", doc.resize((BIG_PX, BIG_PX), Image.LANCZOS)),
        _b64_block("IMG_PNG_B64", draw_img_glyph(SMALL_PX)),
        _b64_block("IMG_PNG_B64_BIG", draw_img_glyph(BIG_PX)),
    ]
    with open(OUT_MOD, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            '# -*- coding: utf-8 -*-\n'
            '"""附件条文件瓷砖的图标数据（48px 与 96px PNG 的 base64）—— '
            '**由 tools/make_file_icons.py 生成，别手改**。\n'
            '\n'
            '换样图请改源图后重跑那条命令。\n'
            '`DOC_*` 是文本 / Word 附件瓷砖的样图（源图由 W 指定）；\n'
            '`IMG_*` 是 Tk 8.6 画不出的图片格式（JPEG / WEBP / BMP，坑 132）的占位图。\n'
            '取哪档由 `tk scaling` 决定（阈值 1.75，与 widgets.app_logo 同一口径）。\n'
            '"""\n\n'
            + "\n\n".join(blocks) + "\n")
    print("写入 %s  %d 字节" % (os.path.relpath(OUT_MOD, ROOT), os.path.getsize(OUT_MOD)))


if __name__ == "__main__":
    main()
