# -*- coding: utf-8 -*-
r"""llm_console.ui.dialogs — 独立对话框：退出确认。

原「独立生图窗口」`ImageDialog` 已随2026-10-03 的死代码清理从仓库版本中移除
（原实现本地留存于 `D:\tmp\deadcode_ImageDialog_20261003.py.txt`，不在仓库内）。
生图统一在主聊天流进行，见 `ui/image_gen.py`。
"""

import tkinter as tk
from tkinter import ttk

from ..core.device_ua import os_label
from . import theme


class ExitDialog(tk.Toplevel):
    """模态二选：停止服务并退出 / 取消（继续运行）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("关闭 LLM 对话台")
        theme.style_toplevel(self)   # 经典背景 + 标题栏两件套（坑 186）
        self.resizable(False, False)
        self.result = None

        ttk.Label(self, text="后台模型服务正在运行。",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(padx=28, pady=(18, 4))
        ttk.Label(self, text="关闭程序会同时停止后台服务。", style="Note.TLabel").pack(
            padx=28, pady=(0, 14))
        box = ttk.Frame(self)
        box.pack(padx=28, pady=(0, 14))
        theme.button(box, text="停止服务并退出", width=16,
                   command=lambda: self._done("stop")).pack(side="left", padx=4)
        theme.button(box, text="取消", width=10,
                   command=lambda: self._done("cancel")).pack(side="left", padx=4)
        ttk.Label(self, text="「取消」返回主窗口，程序与服务继续运行。Esc 也可取消。",
                  style="Muted.TLabel",
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


class PairConfirmDialog(tk.Toplevel):
    """远程配对确认（坑 283）：新设备 / 并入已有逻辑设备，二选一 + 拒绝。

    info = {ip, os(系统标签), os_key, agent, cid, auto(自动识别到的 persona id),
            candidates:[{id,name,os_set,last_seen}…]}。
    result = None（拒绝/关窗/Esc）或 `{"pid": "p_N" | ""}`（空串=新设备）。
    cid 命中过已有设备时默认选并入那台并给提示；否则默认"新设备"。
    """

    def __init__(self, parent, info):
        super().__init__(parent)
        self.title("远程配对请求")
        theme.style_toplevel(self)
        self.resizable(False, False)
        self.result = None
        info = info or {}
        cands = [c for c in (info.get("candidates") or []) if c.get("id")]
        auto_pid = str(info.get("auto") or "")

        ttk.Label(self, text="有设备请求连接远程访问",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            padx=28, pady=(18, 6))
        meta = ttk.Frame(self)
        meta.pack(padx=28, anchor="w")
        ttk.Label(meta, text="系统：", style="Dim.TLabel").grid(row=0, column=0, sticky="e")
        ttk.Label(meta, text=str(info.get("os") or "未知设备")).grid(row=0, column=1, sticky="w")
        ttk.Label(meta, text="来源：", style="Dim.TLabel").grid(row=1, column=0, sticky="e")
        ttk.Label(meta, text=str(info.get("ip") or "?")).grid(row=1, column=1, sticky="w")
        agent = str(info.get("agent") or "")
        if agent:
            ttk.Label(self, text=(agent[:90] + "…") if len(agent) > 90 else agent,
                      style="Muted.TLabel",
                      font=("Microsoft YaHei UI", 8), wraplength=420, justify="left").pack(
                padx=28, pady=(4, 2), anchor="w")
        if auto_pid:
            auto_name = next((str(c.get("name") or "?") for c in cands
                              if c.get("id") == auto_pid), info.get("os") or "已配对设备")
            ttk.Label(self,
                      text="识别到这是你已配对的「%s」（换了连接方式或浏览器），"
                           "默认并入它，之前的历史产物都还在。" % auto_name,
                      style="Note.TLabel", wraplength=420, justify="left").pack(
                padx=28, pady=(6, 2), anchor="w")

        box = ttk.LabelFrame(self, text="这台设备算作：")
        box.pack(padx=28, pady=(8, 4), fill="x")
        self.var = tk.StringVar(value=auto_pid or "")
        ttk.Radiobutton(box, text="一台新设备（独立的历史产物，不与其他设备互通）",
                        variable=self.var, value="").pack(anchor="w", padx=10, pady=(6, 2))
        if cands:
            for c in cands:
                oses = "、".join(os_label(k) for k in (c.get("os_set") or [])) or "未知"
                seen = str(c.get("last_seen") or "")
                tail = ("（%s，最近活跃 %s）" % (oses, seen)) if seen else ("（%s）" % oses)
                ttk.Radiobutton(box, text="并入「%s」%s —— 共用它的历史产物"
                                          % (str(c.get("name") or "?"), tail),
                                variable=self.var,
                                value=str(c["id"])).pack(anchor="w", padx=10, pady=1)
        else:
            ttk.Label(box, text="（还没有其他已配对设备）", style="Muted.TLabel").pack(
                anchor="w", padx=10, pady=(0, 6))

        ttk.Label(self, text="只允许你自己发起的设备接入。选错了随时可在"
                             "「设置 → 远程访问 → 已配对设备」里吊销。",
                  style="Muted.TLabel", wraplength=420, justify="left",
                  font=("Microsoft YaHei UI", 9)).pack(padx=28, pady=(4, 8))
        btns = ttk.Frame(self)
        btns.pack(padx=28, pady=(0, 16))
        # 「允许」是主操作（primary 描边），「拒绝」中性次按钮——与 ConfirmDialog
        # 的主次口径一致（2026-10-15 UI 走查，此前两颗同灰、没有视觉主次）
        theme.button(btns, text="允许", width=10, bootstyle="primary-outline",
                     command=self._allow).pack(side="left", padx=4)
        theme.button(btns, text="拒绝", width=10,
                     command=self._deny).pack(side="left", padx=4)

        self.bind("<Escape>", lambda e: self._deny())
        self.protocol("WM_DELETE_WINDOW", self._deny)
        self.transient(parent)
        self.update_idletasks()
        # 无 UA / 无候选等短内容时窗口会被收成窄条：夹一个设计最小宽
        rw = max(470, self.winfo_reqwidth())
        rh = self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - rw) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - rh) // 3)
        self.geometry("%dx%d+%d+%d" % (rw, rh, x, y))
        self.grab_set()

    def _allow(self):
        self.result = {"pid": str(self.var.get() or "")}
        self.destroy()

    def _deny(self):
        self.result = None
        self.destroy()


class ConfirmDialog(tk.Toplevel):
    """项目风格的二选一确认弹窗（替代系统 messagebox.askyesno，2026-10-15 UI 走查）。

    result = True（确定）/ False（取消、Esc、点叉）。danger=True 时确定键套
    Round.Danger 危险描边样式（吊销 / 删除类操作）。布局 / 间距 / 居中与
    ExitDialog、PairConfirmDialog 同一套：style_toplevel + theme.button。
    """

    def __init__(self, parent, title, message, *, ok_text="确定",
                 cancel_text="取消", danger=False, wrap=420):
        super().__init__(parent)
        self.title(title)
        theme.style_toplevel(self)
        self.resizable(False, False)
        self.result = False

        ttk.Label(self, text=title,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            padx=28, pady=(18, 8))
        ttk.Label(self, text=message, style="Note.TLabel",
                  wraplength=wrap, justify="left").pack(
            padx=28, pady=(0, 14))
        box = ttk.Frame(self)
        box.pack(padx=28, pady=(0, 16))
        theme.button(box, text=ok_text, width=12,
                     bootstyle="danger-outline" if danger else "primary-outline",
                     command=self._ok).pack(side="left", padx=4)
        theme.button(box, text=cancel_text, width=10,
                     command=self._cancel).pack(side="left", padx=4)

        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.update_idletasks()
        rw, rh = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - rw) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - rh) // 3)
        self.geometry("%dx%d+%d+%d" % (rw, rh, x, y))
        self.grab_set()

    def _ok(self):
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()


class PromptDialog(tk.Toplevel):
    """项目风格的单行输入弹窗（替代系统 simpledialog.askstring，2026-10-15 UI 走查）。

    result = 输入串（确定 / 回车）或 None（取消、Esc、点叉）；空串确定按取消处理。
    调用方仍按自己的规则 strip / 夹长度（与 askstring 调用点行为一致）。
    """

    def __init__(self, parent, title, prompt, *, initial="", ok_text="确定",
                 cancel_text="取消"):
        super().__init__(parent)
        self.title(title)
        theme.style_toplevel(self)
        self.resizable(False, False)
        self.result = None

        ttk.Label(self, text=title,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            padx=28, pady=(18, 8))
        ttk.Label(self, text=prompt, style="Note.TLabel",
                  wraplength=420, justify="left").pack(
            padx=28, pady=(0, 8), anchor="w")
        self.var = tk.StringVar(value=str(initial or ""))
        ent = theme.entry(self, textvariable=self.var, width=30)
        ent.pack(padx=28, fill="x")
        box = ttk.Frame(self)
        box.pack(padx=28, pady=(12, 16))
        theme.button(box, text=ok_text, width=12, bootstyle="primary-outline",
                     command=self._ok).pack(side="left", padx=4)
        theme.button(box, text=cancel_text, width=10,
                     command=self._cancel).pack(side="left", padx=4)

        self.bind("<Escape>", lambda e: self._cancel())
        self.bind("<Return>", lambda e: self._ok())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.update_idletasks()
        # 输入框自然宽度偏窄：与确认窗对齐夹一个设计最小宽
        rw = max(470, self.winfo_reqwidth())
        rh = self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - rw) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - rh) // 3)
        self.geometry("%dx%d+%d+%d" % (rw, rh, x, y))
        self.grab_set()
        ent.focus_set()
        ent.select_range(0, "end")
        ent.icursor("end")

    def _ok(self):
        v = self.var.get()
        if not v or not v.strip():
            self._cancel()
            return
        self.result = v
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def ask_confirm(parent, title, message, **kw):
    """模态确认的一行版（替代 messagebox.askyesno）：返回 True / False。"""
    dlg = ConfirmDialog(parent, title, message, **kw)
    dlg.wait_window()
    return bool(dlg.result)


def ask_text(parent, title, prompt, **kw):
    """模态单行输入的一行版（替代 simpledialog.askstring）：返回 str / None。"""
    dlg = PromptDialog(parent, title, prompt, **kw)
    dlg.wait_window()
    return dlg.result

