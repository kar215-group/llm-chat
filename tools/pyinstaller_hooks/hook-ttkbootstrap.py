# -*- coding: utf-8 -*-
# 覆盖 ttkbootstrap 自带的同名钩子（ttkbootstrap._pyinstaller 经 pyinstaller40
# entry_point 注册的那份）：它 collect_data_files("ttkbootstrap.assets") 会把整个
# assets（约 1MB）打进 exe。这里收 elements/ + icons/ 两个子目录：
#   · elements/（12 个小贴图 + manifest，共约 30KB）——「滑动开关」
#     （bootstyle="round-toggle"，settings.py 的布尔选项）运行期要读
#     switch-round.png 重着色；checkbox / progressbar 贴图样式同理。
#   · icons/（bootstrap.ttf 457KB + glyphmap.json + icon_metrics.json）——**必须收**：
#     2.2.3 建 Style 时 create_default_style 会急切构建 combobox / spinbox 家族样式
#     （为原生对话框兜底），箭头走 Bootstrap Icons 字体渲染（builders/combobox.py 的
#     a.icon("chevron-down", …) 一族）；字体读不到 → FileNotFoundError 冲出 Style
#     构造 → theme.apply 的兜底 except 吞掉 → _style=None、整套主题静默消失。
#     （2026-10-10 W 报「exe 深色全乱」即此：浅色退回原生外观几乎看不出来，深色一上来
#     全露；旧注释说「icons/ 只有 dialogs / Icon 这类未用组件在运行期读」是错的 ——
#     当时漏追标准样式构建器这条真路径。坑 187。）icons/ 里的 LICENSE 一起带上
#     （Bootstrap Icons 的 MIT 文本随字体分发，约 1KB，别当废物裁掉）。
# 裁掉的只剩 app_icons/（ttkbootstrap 自家 Window / Toplevel 的窗口图标，368KB）：
# 本程序用 tk.Tk、不碰这两个类，且缺件时它自己有内嵌 base64 兜底。
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("ttkbootstrap.assets", subdir="elements",
                           excludes=["*README*"])
datas += collect_data_files("ttkbootstrap.assets", subdir="icons",
                            excludes=["*README*"])
