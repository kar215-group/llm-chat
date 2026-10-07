# -*- coding: utf-8 -*-
"""重生成 ui/gear_icon.py 的齿轮图标 base64（源图默认 tools/gear_src.png）。

用法：python tools/make_gear_icon.py [源图.png]
源图改过后重跑一次即可；22px / 32px 两档 + 128 色量化与最初生成同口径。
"""
import base64
import io
import os
import sys

from PIL import Image

OUT = os.path.join(os.path.dirname(__file__), "..", "llm_console", "ui", "gear_icon.py")


def b64_of(src, px):
    im = src.convert("RGBA").resize((px, px), Image.LANCZOS)
    pal = im.quantize(colors=128, method=Image.FASTOCTREE)
    buf = io.BytesIO()
    pal.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    argv = sys.argv[1:]
    src_path = argv[0] if argv else os.path.join(os.path.dirname(__file__), "gear_src.png")
    src = Image.open(src_path)
    content = ('# -*- coding: utf-8 -*-\n'
               '"""llm_console.ui.gear_icon — 「设置」按钮的齿轮图标（生成物，别手改）。\n\n'
               '源图 tools/gear_src.png；重生成：python tools/make_gear_icon.py <源图.png>\n'
               '内嵌而不是发 png 文件：单文件 exe 里 __file__ 指向临时解包目录（坑 62 同族），\n'
               'tk.PhotoImage(data=) 原生吃 base64 —— 与 app_icon / file_icons 同一套口径。\n'
               '两档尺寸：26px（顶栏图标按钮，W 定放大后的档）与 32px（备用大档）。\n'
               '128 色量化后单张 < 1KB，加载零感知。\n'
               '"""\n\n'
               'GEAR_PNG_B64 = "%s"\n'
               'GEAR_BIG_PNG_B64 = "%s"\n' % (b64_of(src, 26), b64_of(src, 32)))
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("written:", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
