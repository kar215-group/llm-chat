# -*- coding: utf-8 -*-
r"""llm_console.ui.dialogs — 独立对话框：退出确认。

原「独立生图窗口」`ImageDialog` 已随2026-10-03 的死代码清理从仓库版本中移除
（原实现本地留存于 `D:\tmp\deadcode_ImageDialog_20261003.py.txt`，不在仓库内）。
生图统一在主聊天流进行，见 `ui/image_gen.py`。
"""

import tkinter as tk
from tkinter import ttk

from . import theme


class ExitDialog(tk.Toplevel):
    """模态二选：停止服务并退出 / 取消（继续运行）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("关闭 LLM 对话台")
        self.resizable(False, False)
        self.result = None

        ttk.Label(self, text="后台模型服务正在运行。",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(padx=28, pady=(18, 4))
        ttk.Label(self, text="关闭程序会同时停止后台服务。", foreground="#666666").pack(
            padx=28, pady=(0, 14))
        box = ttk.Frame(self)
        box.pack(padx=28, pady=(0, 14))
        theme.button(box, text="停止服务并退出", width=16,
                   command=lambda: self._done("stop")).pack(side="left", padx=4)
        theme.button(box, text="取消", width=10,
                   command=lambda: self._done("cancel")).pack(side="left", padx=4)
        ttk.Label(self, text="「取消」返回主窗口，程序与服务继续运行。Esc 也可取消。",
                  foreground="#999999",
                  font=("Microsoft YaHei UI", 9)).pack(padx=28, pady=(0, 14))

        self.bind("<Escape>", lambda e: self._done("cancel"))
        self.protocol("WM_DELETE_WINDOW", lambda: self._done("cancel"))
        self.transient(parent)
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 3)
        self.geometry("+%d+%d" % (x, y))
        self.grab_set()

    def _done(self, result):
        self.result = result
        self.destroy()

