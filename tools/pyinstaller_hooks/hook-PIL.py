# 精简版 PyInstaller 钩子（包级）：与内置 hook-PIL 同名，经 --additional-hooks-dir
# 传入时**覆盖**内置那份（内置只排除 tkinter/Qt，不做收集也不做裁剪）。
#
# 为什么要有它：Pillow 12 的 Windows 轮子里躺着几个本项目用不到的大件 ——
#   _avif.pyd 7.9MB（AVIF 解码）、_imagingft.pyd 2.2MB（FreeType 画字）、
#   _imagingcms.pyd 0.3MB（色彩管理）。这里只放行 ui/imgdecode.py 实际用到的
#   核心解码部件；各格式的 ImagePlugin 插件清单在 hook-PIL.Image.py 里（同名
#   覆盖内置那份"全量收集"）。
#
# 没收进来的格式 = 运行期认不出 → ui/imgdecode 走占位图兜底（坑 132），不报错。
# 各平台通用：模块名在 Linux / macOS 的轮子里一致，将来跨平台打包照用。
#
# excludedimports 写在**包级**钩子（父包兜底对所有 PIL.* 模块生效），这是实测
# 踩出来的位置：Pillow 12 里 `PIL._typing` 的 TYPE_CHECKING 块 import 了
# numpy.typing、`PIL.Image` 函数体里 import numpy / 引用 ImageCms —— 静态分析
# 都会跟进，把 numpy 全家（含 20MB 的 openblas DLL）与 yaml（numpy.__config__
# 引）拖进包。缺了它们只影响我们从不调用的 numpy 互转 / ICC 色彩管理两条支路。
excludedimports = ["numpy", "PIL.ImageCms"]
hiddenimports = [
    "PIL._imaging",           # 核心 C 解码器（jpeg/zlib 已静态链入）
    "PIL._webp",              # WEBP 解码（libwebp 静态链入）
    "PIL.Image",
    "PIL.ImageFile",
    "PIL.ImageMode",
    "PIL.ImagePalette",
    "PIL.ImageColor",
    "PIL.ImageOps",           # exif_transpose（手机照片摆正）
]
