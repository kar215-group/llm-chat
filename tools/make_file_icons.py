# -*- coding: utf-8 -*-
"""生成附件条文件瓷砖的图标 base64（开发机工具，产品代码不 import 它）。

   D:/Python/python.exe tools/make_file_icons.py <文档样图.png>

产出 `llm_console/ui/file_icons.py`，四个 base64 块：

  DOC_PNG_B64 / DOC_PNG_B64_BIG    文档瓷砖的样图（48 / 96px，源图由 W 指定）
  IMG_PNG_B64 / IMG_PNG_B64_BIG    Tk 画不出的图片格式的占位图（48 / 96px，本脚本画的）

源图要求（W 2026-10-09）：**透明底 PNG**。白底图会走 `strip_white_bg` 兜底
（只抠与画布边界连通的近白区，文档纸面的白色保留）——那只是应急，边缘抗锯齿
的反解不完美，能拿到透明底素材就优先用透明底。**别用 JPG 当源图**：它没有
alpha 通道，透明底无从谈起（内嵌出去的 PNG 要按 alpha 与瓷砖底色合成，
透明区才会跟随浅 / 深主题，坑 185）。

为什么内嵌成 base64：与 app_icon.py 同一条理由 —— 单文件 exe 里外部资源要么跟
`--add-data` 再算一遍 `_MEIPASS`，要么就地找不到（坑 62 同族）。

为什么源图要先转成 PNG 再内嵌：Tk 8.6 的 `tk.PhotoImage` 只认 PNG / 静态 GIF
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

# 深色主题专用的描线色：#d8dee9（深色正文色系）——深色瓷砖底 #2e3440 上对比 ~80%；
# 黑线（原素材色）只剩 ~19%，糊成暗纹（2026-10-10 真机像素实测，坑 185）
_DARK_LINE = (216, 222, 233)


def _pixels(im):
    return list(im.get_flattened_data() if hasattr(im, "get_flattened_data")
                else im.getdata())


def _recolor_solid(im, rgb):
    """把非透明像素统一染成 rgb（线稿样图专用；半透明像素只换 RGB 不动 alpha）。"""
    r, g, b = rgb
    out = im.copy()
    out.putdata([(r, g, b, p[3]) for p in _pixels(out)])
    return out


def strip_white_bg(im, thresh=42):
    """白底样图 → 透明底（应急兜底，与 tools/make_gear_icon.py 同一份实现）。

    只把**与画布边界连通**的近白区抠掉（四角泛洪）——图形内部的白色（文档图标
    的纸面）不是背景，必须保留。边界那一圈抗锯齿像素按"白度"给 alpha
    （`255 - min(r,g,b)`）：白 → 透明、纯图形色 → 不透明，浅底上的观感与原图
    一致，深底上也只留淡淡的轮廓过渡。
    """
    im = im.convert("RGBA")
    w, h = im.size
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        try:
            ImageDraw.floodfill(im, xy, (255, 0, 255, 255), thresh=thresh)
        except Exception:
            pass
    data = list(im.get_flattened_data() if hasattr(im, "get_flattened_data")
                else im.getdata())
    is_bg = [False] * (w * h)
    for i, (r, g, b, a) in enumerate(data):
        if r > 240 and b > 240 and g < 60:          # 泛洪标定色（品红）
            is_bg[i] = True
    out = []
    for i, (r, g, b, a) in enumerate(data):
        if is_bg[i]:
            out.append((255, 255, 255, 0))          # RGB 留白：缩放的抗暗边
            continue
        x, y = i % w, i // w
        edge = ((x > 0 and is_bg[i - 1]) or (x < w - 1 and is_bg[i + 1]) or
                (y > 0 and is_bg[i - w]) or (y < h - 1 and is_bg[i + w]))
        out.append((r, g, b, max(0, 255 - min(r, g, b))) if edge
                   else (r, g, b, a))
    im2 = Image.new("RGBA", (w, h))
    im2.putdata(out)
    return im2


def prepare_src(im):
    """源图 → 透明底 RGBA：已经是透明底的直接用；白底源图自动抠白。"""
    im = im.convert("RGBA")
    if im.getchannel("A").getextrema()[0] < 250:    # 已有透明像素 = 透明底素材
        return im
    return strip_white_bg(im)


def fit_square(im, px):
    """裁到图形边界、按比例居中放进 px×px 方画布（不拉伸变形）。

    素材画布四周常留大片透明边距（W 2026-10-10 给的 1440 文档图图形只占 61% 宽），
    直接整幅 resize 会小一圈；裁剪后与历史上"贴边满幅"的旧样图观感一致。
    非正方图形按长边等比缩放居中（文档纸面略高于宽，别拉成方）。
    """
    bb = im.getchannel("A").getbbox()
    if bb:
        im = im.crop(bb)
    w, h = im.size
    s = px / float(max(w, h))
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    im2 = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    canvas.paste(im2, ((px - nw) // 2, (px - nh) // 2), im2)
    return canvas


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
        sys.exit("用法：python tools/make_file_icons.py <文档样图.png>")
    doc = prepare_src(Image.open(sys.argv[1]))
    print("文档样图 %s → %s（透明底素材直接用；白底自动抠白）"
          % (os.path.basename(sys.argv[1]), "x".join(map(str, doc.size))))
    blocks = [
        _b64_block("DOC_PNG_B64", fit_square(doc, SMALL_PX)),
        _b64_block("DOC_PNG_B64_BIG", fit_square(doc, BIG_PX)),
        _b64_block("DOC_PNG_B64_DARK", _recolor_solid(fit_square(doc, SMALL_PX), _DARK_LINE)),
        _b64_block("DOC_PNG_B64_BIG_DARK", _recolor_solid(fit_square(doc, BIG_PX), _DARK_LINE)),
        _b64_block("IMG_PNG_B64", draw_img_glyph(SMALL_PX)),
        _b64_block("IMG_PNG_B64_BIG", draw_img_glyph(BIG_PX)),
    ]
    with open(OUT_MOD, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            '# -*- coding: utf-8 -*-\n'
            '"""附件条文件瓷砖的图标数据（48px 与 96px PNG 的 base64）—— '
            '**由 tools/make_file_icons.py 生成，别手改**。\n'
            '\n'
            '换样图请改源图后重跑那条命令（源图要**透明底 PNG**，见坑 185）。\n'
            '`DOC_*` 是文本 / Word 附件瓷砖的样图（源图由 W 指定）；`*_DARK` 是深色主题版'
            '（描线染 #d8dee9 —— 黑线在深色瓷砖底上对比只剩 ~19%，坑 185）；\n'
            '`IMG_*` 是 Tk 8.6 画不出的图片格式（JPEG / WEBP / BMP，坑 132）的占位图'
            '（没有 DARK 版，界面在深色下沿用原样）。\n'
            '取哪档由 `tk scaling` 决定（阈值 1.75，与 widgets.app_logo 同一口径），'
            '深浅由 theme.mode() 决定，见 chat._file_icon。\n'
            '"""\n\n'
            + "\n\n".join(blocks) + "\n")
    print("写入 %s  %d 字节" % (os.path.relpath(OUT_MOD, ROOT), os.path.getsize(OUT_MOD)))


if __name__ == "__main__":
    main()
