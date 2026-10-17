# -*- coding: utf-8 -*-
r"""llm_console.ui.dialogs — 独立对话框：退出确认。

原「独立生图窗口」`ImageDialog` 已随2026-10-03 的死代码清理从仓库版本中移除
（原实现本地留存于 `D:\tmp\deadcode_ImageDialog_20261003.py.txt`，不在仓库内）。
生图统一在主聊天流进行，见 `ui/image_gen.py`。
"""

import os
import tkinter as tk
from tkinter import ttk

from .. import texts
from ..core.device_ua import os_label
from . import theme
from . import widgets


class ExitDialog(tk.Toplevel):
    """模态二选：停止服务并退出 / 取消（继续运行）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title(texts.DLG_EXIT_TITLE)
        theme.style_toplevel(self)   # 经典背景 + 标题栏两件套（坑 186）
        self.resizable(False, False)
        self.result = None

        ttk.Label(self, text=texts.DLG_EXIT_RUNNING,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(padx=28, pady=(18, 4))
        ttk.Label(self, text=texts.DLG_EXIT_WILL_STOP, style="Note.TLabel").pack(
            padx=28, pady=(0, 14))
        box = ttk.Frame(self)
        box.pack(padx=28, pady=(0, 14))
        theme.button(box, text=texts.DLG_EXIT_BTN_STOP, width=16,
                   command=lambda: self._done("stop")).pack(side="left", padx=4)
        theme.button(box, text=texts.BTN_CANCEL, width=10,
                   command=lambda: self._done("cancel")).pack(side="left", padx=4)
        ttk.Label(self, text=texts.DLG_EXIT_HINT,
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
            candidates:[{id,name,os_set,added,last_seen}…]}。
    result = None（拒绝/关窗/Esc）或 `{"pid": "p_N" | ""}`（空串=新设备）。
    候选由服务端收窄成同系统设备 + cid 命中的那台（坑 291）；候选为空时
    不出现「这台设备算作」一节，只剩允许 / 拒绝（W 2026-10-16 拍板）。
    cid 命中过已有设备时默认选并入那台并给提示；否则默认"新设备"。
    """

    def __init__(self, parent, info):
        super().__init__(parent)
        self.title(texts.DLG_PAIR_TITLE)
        theme.style_toplevel(self)
        self.resizable(False, False)
        self.result = None
        info = info or {}
        cands = [c for c in (info.get("candidates") or []) if c.get("id")]
        auto_pid = str(info.get("auto") or "")
        if auto_pid and all(c.get("id") != auto_pid for c in cands):
            auto_pid = ""    # 预选的并入目标必须真在候选里，否则"允许"会并到没展示的设备

        ttk.Label(self, text=texts.DLG_PAIR_HEAD,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            padx=28, pady=(18, 6))
        meta = ttk.Frame(self)
        meta.pack(padx=28, anchor="w")
        ttk.Label(meta, text=texts.DLG_PAIR_OS, style="Dim.TLabel").grid(row=0, column=0, sticky="e")
        ttk.Label(meta, text=str(info.get("os") or texts.DLG_PAIR_UNKNOWN_DEVICE)).grid(row=0, column=1, sticky="w")
        ttk.Label(meta, text=texts.DLG_PAIR_SOURCE, style="Dim.TLabel").grid(row=1, column=0, sticky="e")
        ttk.Label(meta, text=str(info.get("ip") or "?")).grid(row=1, column=1, sticky="w")
        agent = str(info.get("agent") or "")
        if agent:
            ttk.Label(self, text=(agent[:90] + "…") if len(agent) > 90 else agent,
                      style="Muted.TLabel",
                      font=("Microsoft YaHei UI", 8), wraplength=420, justify="left").pack(
                padx=28, pady=(4, 2), anchor="w")
        if auto_pid:
            auto_name = next((str(c.get("name") or "?") for c in cands
                              if c.get("id") == auto_pid), info.get("os") or texts.DLG_PAIR_PAIRED_DEVICE)
            ttk.Label(self,
                      text=texts.DLG_PAIR_AUTO_MERGE % auto_name,
                      style="Note.TLabel", wraplength=420, justify="left").pack(
                padx=28, pady=(6, 2), anchor="w")

        # var 无论有没有候选都建：空串 = 新设备，_allow 靠它回决策
        self.var = tk.StringVar(value=auto_pid or "")
        if cands:
            box = ttk.LabelFrame(self, text=texts.DLG_PAIR_COUNT_AS)
            box.pack(padx=28, pady=(8, 4), fill="x")
            ttk.Radiobutton(box, text=texts.DLG_PAIR_NEW,
                            variable=self.var, value="").pack(anchor="w", padx=10, pady=(6, 2))
            for c in cands:
                oses = "、".join(os_label(k) for k in (c.get("os_set") or [])) or texts.DLG_PAIR_UNKNOWN_OS
                seen = str(c.get("last_seen") or "")
                tail = (texts.DLG_PAIR_CAND_TAIL_SEEN % (oses, seen)) if seen else (texts.DLG_PAIR_CAND_TAIL % oses)
                ttk.Radiobutton(box, text=texts.DLG_PAIR_MERGE_INTO
                                          % (str(c.get("name") or "?"), tail),
                                variable=self.var,
                                value=str(c["id"])).pack(anchor="w", padx=10, pady=1)

        ttk.Label(self, text=texts.DLG_PAIR_REVOKE_HINT,
                  style="Muted.TLabel", wraplength=420, justify="left",
                  font=("Microsoft YaHei UI", 9)).pack(padx=28, pady=(4, 8))
        btns = ttk.Frame(self)
        btns.pack(padx=28, pady=(0, 16))
        # 「允许」是主操作（primary 描边），「拒绝」中性次按钮——与 ConfirmDialog
        # 的主次口径一致（2026-10-15 UI 走查，此前两颗同灰、没有视觉主次）
        theme.button(btns, text=texts.DLG_PAIR_BTN_ALLOW, width=10, bootstyle="primary-outline",
                     command=self._allow).pack(side="left", padx=4)
        theme.button(btns, text=texts.DLG_PAIR_BTN_DENY, width=10,
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

    def __init__(self, parent, title, message, *, ok_text=texts.BTN_OK,
                 cancel_text=texts.BTN_CANCEL, danger=False, wrap=420):
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

    def __init__(self, parent, title, prompt, *, initial="", ok_text=texts.BTN_OK,
                 cancel_text=texts.BTN_CANCEL):
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


class ModelPickDialog(tk.Toplevel):
    """模型勾选窗（W 2026-10-17：「全部重新计算」改成可选具体模型）。

    rows = [(key, label, note), …]：key 回传给调用方（这里是 .gguf 的 basename），
    label 是菜单里的显示名，note 是"当前记录"的一行小字。
    result = 勾中的 key 列表（确定；可能为空）/ None（取消、Esc、点叉）。
    默认全选：老用法"点一下全重算"仍只要两次点击（打开 → 确定），但手调过的模型
    可以在这一步取消勾选 —— 原来那条路只有"全清重算"，代价比收益大（16 §14）。
    """

    def __init__(self, parent, rows, *, title=None, hint="", note="", summary=""):
        super().__init__(parent)
        self.title(title or texts.MDL_RECALC_PICK)
        theme.style_toplevel(self)
        self.resizable(False, True)
        self.result = None

        ttk.Label(self, text=title or texts.MDL_RECALC_PICK,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            padx=24, pady=(16, 6), anchor="w")
        if hint:
            ttk.Label(self, text=hint, style="Note.TLabel",
                      wraplength=520, justify="left").pack(
                padx=24, pady=(0, 4), anchor="w")

        # 勾选区：模型可能几十个，必须能滚（Tk 的滚轮只在指针进入可视区时生效）
        wrap = ttk.Frame(self)
        wrap.pack(padx=24, pady=(6, 8), fill="both", expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0,
                           background=theme.c("bg"), height=300, width=520)
        bar = theme.scroll(wrap, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, background=theme.c("bg"))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        widgets.attach_wheel(canvas)

        self.vars = {}
        for key, label, sub in rows:
            v = tk.BooleanVar(value=True)
            self.vars[key] = v
            box = ttk.Frame(inner)
            box.pack(fill="x", pady=2)
            theme.check(box, text=label, variable=v).pack(anchor="w", padx=2)
            if sub:
                ttk.Label(box, text=sub, style="Muted.TLabel",
                          font=("Microsoft YaHei UI", 8)).pack(
                    anchor="w", padx=(30, 2), pady=(0, 2))

        if summary:
            ttk.Label(self, text=summary, style="Dim.TLabel").pack(
                padx=24, anchor="w")
        if note:
            ttk.Label(self, text=note, style="Muted.TLabel",
                      wraplength=520, justify="left").pack(
                padx=24, pady=(2, 0), anchor="w")

        tools = ttk.Frame(self)
        tools.pack(padx=24, pady=(10, 0), fill="x")
        theme.button(tools, text=texts.BTN_SELECT_ALL, width=8,
                     command=lambda: self._set_all(True)).pack(side="left")
        theme.button(tools, text=texts.BTN_SELECT_NONE, width=8,
                     command=lambda: self._set_all(False)).pack(side="left", padx=(6, 0))
        box = ttk.Frame(self)
        box.pack(padx=24, pady=(10, 16))
        theme.button(box, text=texts.BTN_OK, width=12, bootstyle="primary-outline",
                     command=self._ok).pack(side="left", padx=4)
        theme.button(box, text=texts.BTN_CANCEL, width=10,
                     command=self._cancel).pack(side="left", padx=4)

        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.update_idletasks()
        rw = max(560, self.winfo_reqwidth())
        rh = min(560, self.winfo_reqheight())
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - rw) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - rh) // 3)
        self.geometry("%dx%d+%d+%d" % (rw, rh, x, y))
        self.grab_set()

    def _set_all(self, on):
        for v in self.vars.values():
            v.set(bool(on))

    def _ok(self):
        self.result = [k for k, v in self.vars.items() if v.get()]
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class SessionPickDialog(tk.Toplevel):
    """历史对话窗（W 2026-10-17）：列出 `chat_logs` 里的记录，选一份读回来接着聊。

    rows = `core.chatlog.list_sessions()` 的返回值（已按时间倒序，最近的在最上面）。
    result = 选中的那条记录（字典）/ None（取消、Esc、点叉）。
    双击一行同「载入」——列表窗里双击是肌肉记忆，不认的话看着像"点不动"。
    """

    def __init__(self, parent, rows, *, folder="", title=None):
        super().__init__(parent)
        self.title(title or texts.DLG_HIST_TITLE)
        theme.style_toplevel(self)
        self.resizable(True, True)
        self.result = None
        self._rows = list(rows or [])

        ttk.Label(self, text=title or texts.DLG_HIST_TITLE,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            padx=24, pady=(16, 6), anchor="w")
        ttk.Label(self, text=texts.DLG_HIST_HINT, style="Note.TLabel",
                  wraplength=560, justify="left").pack(padx=24, pady=(0, 8), anchor="w")

        if not self._rows:
            # 空态：不摆一个空列表框（那看起来像加载失败），直接说"还没有 + 存哪儿"
            ttk.Label(self, text=texts.DLG_HIST_EMPTY,
                      font=("Microsoft YaHei UI", 10)).pack(padx=24, pady=(4, 2), anchor="w")
            ttk.Label(self, text=texts.DLG_HIST_EMPTY_TAIL % (folder or "-"),
                      style="Dim.TLabel", wraplength=560, justify="left").pack(
                padx=24, pady=(0, 10), anchor="w")
        else:
            wrap = ttk.Frame(self)
            wrap.pack(padx=24, pady=(2, 8), fill="both", expand=True)
            self.listbox = tk.Listbox(
                wrap, height=14, width=76, activestyle="none",
                highlightthickness=1, relief="flat", bd=0,
                background=theme.c("bg"), foreground=theme.c("body"),
                selectbackground=theme.c("accent"), selectforeground=theme.c("bg"),
                font=("Microsoft YaHei UI", 9))
            bar = theme.scroll(wrap, orient="vertical", command=self.listbox.yview)
            self.listbox.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            self.listbox.pack(side="left", fill="both", expand=True)
            for r in self._rows:
                self.listbox.insert(
                    "end", texts.DLG_HIST_ROW % (r.get("title") or r.get("id") or "?",
                                                 int(r.get("turns") or 0),
                                                 r.get("subject") or
                                                 texts.DLG_HIST_ROW_NO_SUBJECT))
            self.listbox.selection_set(0)        # 默认选最近那份：多数时候就是要它
            self.listbox.bind("<Double-Button-1>", lambda e: self._ok())
            widgets.attach_wheel(self.listbox)
            ttk.Label(self, text=texts.DLG_HIST_COUNT % len(self._rows),
                      style="Dim.TLabel").pack(padx=24, anchor="w")

        if folder:
            ttk.Label(self, text=texts.DLG_HIST_FOLDER % folder,
                      style="Dim.TLabel", wraplength=560, justify="left").pack(
                padx=24, pady=(2, 0), anchor="w")

        box = ttk.Frame(self)
        box.pack(padx=24, pady=(12, 16))
        theme.button(box, text=texts.DLG_HIST_LOAD, width=10,
                     bootstyle="primary-outline", command=self._ok).pack(side="left", padx=4)
        if folder:
            theme.button(box, text=texts.DLG_HIST_OPEN_DIR, width=14,
                         command=lambda: self._open_dir(folder)).pack(side="left", padx=4)
        theme.button(box, text=texts.BTN_CANCEL, width=10,
                     command=self._cancel).pack(side="left", padx=4)

        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.update_idletasks()
        rw = max(620, self.winfo_reqwidth())
        rh = min(560, self.winfo_reqheight())
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - rw) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - rh) // 3)
        self.geometry("%dx%d+%d+%d" % (rw, rh, x, y))
        self.grab_set()

    def _open_dir(self, folder):
        """打开记录目录（目录还不存在就先建出来 —— 空状态点这一下不该"什么都没发生"）。"""
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
        except Exception:
            pass

    def _ok(self):
        if not self._rows:
            self._cancel()
            return
        try:
            idx = int(self.listbox.curselection()[0])
        except Exception:
            idx = 0
        self.result = self._rows[idx] if 0 <= idx < len(self._rows) else None
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

