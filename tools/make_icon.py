# -*- coding: utf-8 -*-
"""从设计源图生成产品图标（开发机工具，产品代码不 import 它）。

    D:/Python/python.exe tools/make_icon.py <源图.png>
    D:/Python/python.exe tools/make_icon.py --from-ico      # 源图不在了，从 app.ico 反推

产出两份，都在 `llm_console/ui/` 里：

  app.ico      多尺寸（16/24/32/48/64/128/256）—— PyInstaller `--icon` 的输入，
               决定资源管理器 / 任务栏 / 桌面里 exe 自己长什么样。
  app_icon.py  两张 PNG 的 base64：`PNG_B64`（64px，喂标题栏与任务栏的
               `tk.PhotoImage`）+ `LOGO_B64`（128px，设置 → 关于 那页的大标志）。

为什么把图标内嵌成 base64 而不是发布一个 png 文件：单文件 exe 里 `__file__` 指向临时解包目录
（坑 62 同族），外部资源要么跟着 `--add-data` 再算一遍 `_MEIPASS` 路径，要么就地找不到；
而 `tk.PhotoImage` 原生吃 PNG 的 base64，一行 `data=` 就没有路径这件事了。

为什么放在 `llm_console/ui/` 而不是新建 `assets/`：这个仓库是**白名单式** `.gitignore`
（§2.5），`llm_console/` 已整体放行，而根目录新建一层要记得加 `!` 才会进库；备份脚本的
`SOURCES` 也只递归收 `llm_console/`。少一处"忘了放行就静默不入库"的口子。

本脚本需要 Pillow —— 它是**开发机的造资源工具**，产物是静态文件，
运行期的 GUI 与打包流水线都不依赖 Pillow（零第三方依赖这条没被破坏）。
"""

import base64
import io
import os
import struct
import sys
import textwrap

try:
    from PIL import Image
except ImportError:
    sys.exit("需要 Pillow（只在开发机上装）：python -m pip install pillow")

# .ico 里的档位：Windows 按显示位置挑（标题栏 16、任务栏 24~32、资源管理器 48、缩放后更大）
SIZES = (16, 24, 32, 48, 64, 128, 256)
WINDOW_PX = 64        # 内嵌给 tk.PhotoImage 的边长（标题栏与任务栏都够，再大只是喂 base64 体积）
LOGO_PX = 128         # 关于页那张大标志：图**不跟着 Tk 缩放走**，所以高分屏要单独给一档
PAD = 0.06            # 图形四周留白比例：贴边的图标在任务栏里会被"切"得很廉价
VEIL = 8              # alpha 低于这个值的一律打掉（透明导出常留一层 alpha=1 的薄雾底）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ICO = os.path.join(ROOT, "llm_console", "ui", "app.ico")
OUT_MOD = os.path.join(ROOT, "llm_console", "ui", "app_icon.py")


def prepare(path):
    """源图 → 去雾底 → 按真实笔画裁切 → 补成正方形（高分辨率，后面各档都从它缩）。"""
    im = Image.open(path).convert("RGBA")
    a = im.getchannel("A")
    span = max(255 - VEIL, 1)
    im.putalpha(a.point(lambda p: 0 if p <= VEIL else min(255, (p - VEIL) * 255 // span)))
    box = im.getchannel("A").getbbox()
    if not box:
        raise SystemExit("源图去雾底之后什么都不剩，检查 alpha 是不是本来就全 0")
    g = im.crop(box)
    side = max(g.size)
    out = Image.new("RGBA", (int(round(side * (1 + 2 * PAD))),) * 2, (0, 0, 0, 0))
    out.alpha_composite(g, ((out.width - g.width) // 2, (out.height - g.height) // 2))
    return out


def master_from_ico(path):
    """没有设计源图时，从已生成的 app.ico 反推大图。

    取 256 那一档：它是**原样塞进去的 PNG**（见 write_ico 里 `s >= 256` 的分支），
    所以能无损拿回来，不用重跑一遍裁切补边（那会把已经留好的边距再乘一次）。
    """
    with open(path, "rb") as f:
        data = f.read()
    n = struct.unpack("<H", data[4:6])[0]
    best = None
    for i in range(n):
        e = data[6 + 16 * i: 22 + 16 * i]
        size, off = struct.unpack("<II", e[8:16])
        if data[off:off + 8] == b"\x89PNG\r\n\x1a\n":
            side = e[0] or 256
            if best is None or side > best[0]:
                best = (side, data[off:off + size])
    if best is None:
        raise SystemExit("app.ico 里没有 PNG 档位，反推不出大图")
    return Image.open(io.BytesIO(best[1])).convert("RGBA")


def png_bytes(im):
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


def _dib(im):
    """32 位图标 DIB：BITMAPINFOHEADER(高写两倍) + 自底向上的 BGRA + 1bpp AND 掩码。"""
    w, h = im.size
    px = im.convert("RGBA")
    rows = [px.crop((0, y, w, y + 1)).tobytes("raw", "BGRA") for y in range(h)]
    xor = b"".join(reversed(rows))
    stride = ((w + 31) // 32) * 4
    a = px.getchannel("A")
    mask = bytearray(stride * h)
    for i, y in enumerate(range(h - 1, -1, -1)):          # 掩码同样自底向上
        row = a.crop((0, y, w, y + 1)).tobytes()
        for x, v in enumerate(row):
            if v < 128:                                   # 位=1 表示这一像素是透明的
                mask[i * stride + (x >> 3)] |= 0x80 >> (x & 7)
    hdr = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                      len(xor) + len(mask), 0, 0, 0, 0)
    return hdr + xor + bytes(mask)


def write_ico(path, master, sizes):
    """手写 .ico：小档位一律 BMP/DIB，只有 256 用 PNG 压缩。

    为什么不直接 `PIL.save(..., "ICO")`：Pillow 12 会把**每一档**都编成 PNG，外壳认这套
    （Vista+），但 .NET 的 `Icon.ExtractAssociatedIcon` 一类的老接口读 PNG 压缩条目会拿到
    空图 —— 表现就是"exe 上明明有图标资源，属性页与快捷方式里却显示默认图标"。
    Windows 自己的图标工具也是这个形状：≤128 走 DIB，256 走 PNG。
    """
    entries, blobs = [], []
    for s in sizes:
        im = master.resize((s, s), Image.LANCZOS)
        if s >= 256:
            data, bpp = png_bytes(im), 0
        else:
            data, bpp = _dib(im), 32
        entries.append((s, bpp, len(data)))
        blobs.append(data)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = len(head) + 16 * len(sizes)
    dir_ = b""
    for (s, bpp, n), blob in zip(entries, blobs):
        dir_ += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, bpp, n, off)
        off += len(blob)
    with open(path, "wb") as f:
        f.write(head + dir_ + b"".join(blobs))
    return entries


def _b64_block(name, im):
    raw = png_bytes(im)
    b64 = base64.b64encode(raw).decode("ascii")
    body = "\n".join('    "%s"' % ln for ln in textwrap.wrap(b64, 76))
    print("  %s：%dpx  PNG %d 字节 → base64 %d 字符" % (name, im.width, len(raw), len(b64)))
    return "%s = (\n%s\n)" % (name, body)


def main():
    if len(sys.argv) != 2:
        sys.exit("用法：python tools/make_icon.py <源图.png|--from-ico>")
    from_ico = sys.argv[1] == "--from-ico"
    if from_ico:
        master = master_from_ico(OUT_ICO)
        print("从 %s 的 256 档反推 → %s"
              % (os.path.relpath(OUT_ICO, ROOT), "x".join(map(str, master.size))))
    else:
        src = sys.argv[1]
        master = prepare(src)
        print("源图 %s → 裁切补边后 %s" % (os.path.basename(src), "x".join(map(str, master.size))))
        entries = write_ico(OUT_ICO, master, SIZES)
        print("写入 %s  %d 字节  %s"
              % (os.path.relpath(OUT_ICO, ROOT), os.path.getsize(OUT_ICO),
                 "、".join("%d(%s)" % (s, "PNG" if bpp == 0 else "DIB")
                          for s, bpp, _n in entries)))

    blocks = [
        _b64_block("PNG_B64", master.resize((WINDOW_PX, WINDOW_PX), Image.LANCZOS)),
        _b64_block("LOGO_B64", master.resize((LOGO_PX, LOGO_PX), Image.LANCZOS)),
    ]
    with open(OUT_MOD, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            '# -*- coding: utf-8 -*-\n'
            '"""应用图标数据（64px 与 128px PNG 的 base64）—— **由 tools/make_icon.py 生成，别手改**。\n'
            '\n'
            '换图请改源图后重跑那条命令（源图不在了就用 `--from-ico` 从 app.ico 反推）。\n'
            '`PNG_B64` 给标题栏与任务栏（`ui/widgets.set_app_icon`），\n'
            '`LOGO_B64` 给设置 → 关于 那页的大标志（`ui/widgets.app_logo`）。\n'
            '"""\n\n'
            '%s\n\n%s\n' % (blocks[0], blocks[1]))
    print("写入 %s  %d 字节" % (os.path.relpath(OUT_MOD, ROOT), os.path.getsize(OUT_MOD)))


if __name__ == "__main__":
    main()
