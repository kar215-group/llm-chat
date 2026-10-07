# 精简版 PyInstaller 钩子（模块级，PIL.Image 挂点）：**同名覆盖** PyInstaller
# 内置的 hook-PIL.Image.py。内置版把**全部** *ImagePlugin 子模块当 hiddenimport
# 收集 —— 在 Pillow 12 上连带把 _avif.pyd（7.9MB，经 AvifImagePlugin）与 numpy
# 等无关大件一起拖进包，exe 体积翻倍还多。
#
# 本项目只做 JPEG / BMP / WEBP / PNG / GIF 解码 + PNG 编码（ui/imgdecode.py，
# 2026-10-07 W 定「轻量、低第三方依赖」），只放行这几个插件。
# 没列进来的格式 = 运行期认不出 → 走占位图兜底（坑 132），不报错。
#
# numpy / ImageCms 的排除写在包级 hook-PIL.py（要覆盖 PIL._typing 那条边，
# 见彼处注释）——这里只管"收哪些插件"。
hiddenimports = [
    "PIL.GifImagePlugin",
    "PIL.BmpImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin",
    "PIL.WebPImagePlugin",
]
