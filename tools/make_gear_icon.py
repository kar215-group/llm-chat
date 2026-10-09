# -*- coding: utf-8 -*-
"""重生成 ui/gear_icon.py 的齿轮图标 base64（源图默认 tools/gear_src.png）。

用法：python tools/make_gear_icon.py [源图.png]
源图改过后重跑一次即可；26px（顶栏）与 32px（备用大档）两档。

源图要求（W 2026-10-09）：**透明底 PNG**。白底源图会走 `strip_white_bg` 兜底
（只抠与画布边界连通的近白区，齿轮中心的白色圆孔保留）——那只是应急，边缘
抗锯齿的反解不完美，能拿到透明底素材就优先用透明底。**别用 JPG 当源图**：
它没有 alpha 通道，透明底无从谈起。
产物必须是 RGBA：Tk 8.6 的 tk.PhotoImage 按 alpha 与控件底色合成，透明区
自动跟随主题（浅 / 深）。曾有一版在这里做 `quantize(128)` —— 量化会把 alpha
通道整个丢掉，透明底源图也被烧成白底（坑 185），所以现在不量化，直接存
优化过的 RGBA PNG（26px 单张 ~1KB，加载零感知）。
"""
import base64
import io
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(__file__), "..", "llm_console", "ui", "gear_icon.py")

# 线稿小尺寸专精的参数与触发门槛（见 b64_of 注释；调参先跑 _selftest 里的编译试验）
_SHRINK_GATE = 4        # 源图边长 / 目标边长 ≥ 此值才启用
_DILATE = 2             # 源尺度上膨胀圈数（每圈 +1px 线宽）
_BINARIZE = 60          # 目标尺度上 alpha 二值化临界

# 深色主题专用的描线色：#d8dee9（深色正文色系）——顶栏按钮底 #2e3440 上对比 ~80%；
# 黑线（原素材色）在它上面只剩 ~19%，糊成暗纹（2026-10-10 真机像素实测，坑 185）
_DARK_LINE = (216, 222, 233)


def _pixels(im):
    return list(im.get_flattened_data() if hasattr(im, "get_flattened_data")
                else im.getdata())


def _recolor_solid(im, rgb):
    """把非透明像素统一染成 rgb（线稿素材专用；半透明像素只换 RGB 不动 alpha）。"""
    r, g, b = rgb
    out = im.copy()
    out.putdata([(r, g, b, p[3]) for p in _pixels(out)])
    return out


def strip_white_bg(im, thresh=42):
    """白底产品图 → 透明底（应急兜底，与 tools/make_file_icons.py 同一份实现）。

    只把**与画布边界连通**的近白区抠掉（四角泛洪）——图形内部的白色（齿轮中心
    圆孔、文档图标的纸面）不是背景，必须保留。边界那一圈抗锯齿像素按"白度"给
    alpha（`255 - min(r,g,b)`）：白 → 透明、纯图形色 → 不透明，浅底上的观感与
    原图一致，深底上也只留淡淡的轮廓过渡。
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

    素材画布四周常留大片透明边距、图形还未必居中（W 2026-10-10 给的 512 齿轮图
    图形只占 84% 且偏右下），直接整幅 resize 会小一圈还偏心；裁剪后与历史上
    "贴边满幅"的旧素材观感一致。非正方图形按长边等比缩放居中（别拉成方）。
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


def _dilate_alpha(im, n):
    """把图形的 alpha 往外扩 n 圈（线加粗；RGB 原样带走，彩色素材也适用）。"""
    a = im.getchannel("A")
    for _ in range(n):
        a = a.filter(ImageFilter.MaxFilter(3))
    out = im.copy()
    out.putalpha(a)
    return out


def b64_of(src, px, dark=False):
    im = prepare_src(src)
    bb = im.getchannel("A").getbbox()
    src_edge = max(bb[2] - bb[0], bb[3] - bb[1]) if bb else max(im.size)
    line_art = src_edge / float(px) >= _SHRINK_GATE
    if line_art:
        im = _dilate_alpha(im, _DILATE)
    im = fit_square(im, px)
    if line_art:
        # 线稿小尺寸专精（W 2026-10-10 给的线稿齿轮实测）：512px、线宽 ~10px 的
        # 图缩到顶栏 26px 是 19.7 倍 —— 细弧线在 LANCZOS 下覆盖率处处不满，
        # 整幅变"淡灰糊团"（像素实测：26px 档 alpha>200 的实色像素占比 0%，
        # 深底上只剩一道影子）。膨胀 2 圈把线宽等效补到 ~1.5px，二值化把残余的
        # 浅 alpha 重新做实 → 26px 上呈常规"像素风小齿轮"（实色 ~24~30%），
        # 深浅底都认得出来。缩小比不大（< 4 倍）时整体不启用，别把好素材过度加工。
        a = im.getchannel("A").point(lambda v: 255 if v >= _BINARIZE else 0)
        im = im.copy()
        im.putalpha(a)
    if dark:
        im = _recolor_solid(im, _DARK_LINE)         # 深色主题版：黑改浅（见常量注释）
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)              # RGBA 直存，别再量化（丢 alpha）
    return base64.b64encode(buf.getvalue()).decode()


def main():
    argv = sys.argv[1:]
    src_path = argv[0] if argv else os.path.join(os.path.dirname(__file__), "gear_src.png")
    src = Image.open(src_path)
    content = ('# -*- coding: utf-8 -*-\n'
               '"""llm_console.ui.gear_icon — 「设置」按钮的齿轮图标（生成物，别手改）。\n\n'
               '源图 tools/gear_src.png（**透明底 PNG**）；重生成：\n'
               'python tools/make_gear_icon.py <源图.png>\n'
               '内嵌而不是发 png 文件：单文件 exe 里 __file__ 指向临时解包目录（坑 62 同族），\n'
               'tk.PhotoImage(data=) 原生吃 base64 —— 与 app_icon / file_icons 同一套口径。\n'
               '两档尺寸：26px（顶栏图标按钮，W 定放大后的档）与 32px（备用大档）；\n'
               '每档两色：原素材色（浅色主题用）与 #d8dee9 描线（深色主题用）——\n'
               '黑线在深色按钮底 #2e3440 上对比只剩 ~19%%（2026-10-10 实测，坑 185）。\n'
               'RGBA 直存不量化（量化会丢 alpha，坑 185）；单张 ~1KB，加载零感知。\n'
               '"""\n\n'
               'GEAR_PNG_B64 = "%s"\n'
               'GEAR_BIG_PNG_B64 = "%s"\n'
               'GEAR_PNG_B64_DARK = "%s"\n'
               'GEAR_BIG_PNG_B64_DARK = "%s"\n'
               % (b64_of(src, 26), b64_of(src, 32),
                  b64_of(src, 26, dark=True), b64_of(src, 32, dark=True)))
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("written:", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
