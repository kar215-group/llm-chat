# -*- coding: utf-8 -*-
"""llama_gui.py — 启动入口。

v30 起代码按 core / connection / ui 三层拆进 llm_console 包，本文件只保留入口，
所以命令行、双击 .py、快捷方式、pythonw 无窗启动这几种习惯都不用改。

    python  llama_gui.py     # 带控制台，报错直接看得见，第一次跑推荐用这个
    pythonw llama_gui.py     # 无控制台窗口
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_console.ui.app import main          # noqa: E402

if __name__ == "__main__":
    main()
