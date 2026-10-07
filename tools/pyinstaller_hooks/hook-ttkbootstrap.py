# -*- coding: utf-8 -*-
# 覆盖 ttkbootstrap 自带的同名钩子（ttkbootstrap._pyinstaller 经 pyinstaller40
# entry_point 注册的那份）：它 collect_data_files("ttkbootstrap.assets") 会把整个
# assets（约 1MB）打进 exe。这里只收 elements/（12 个小贴图，共约 30KB）——
# 「滑动开关」（bootstyle="round-toggle"，settings.py 的布尔选项）运行期要读
# switch-round.png 重着色；checkbox / progressbar 贴图样式同理。可以省下的是
# icons/（图标字体 628KB）与 app_icons/（ttkbootstrap 自己的应用图标 368KB），
# 只有 dialogs / Icon 这类未用组件在运行期读。
# 若将来用上 dialogs，删掉本钩子即可恢复自带行为。
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("ttkbootstrap.assets", subdir="elements",
                           excludes=["*README*"])
