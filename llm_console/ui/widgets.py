# -*- coding: utf-8 -*-
"""llm_console.ui.widgets — 界面共用小部件（v39 设置窗口重构引入）。

设置窗口从「顶部页签 + 每行内联灰字」改成「左侧可折叠导航 + 右侧滚动页 + 悬停提示」，
这三样是共用的，单独成模块，别把它们塞进 settings.py 那个已经 1400 行的文件里：

  HelpDot    字段旁的小 "?"：悬停显示完整说明，鼠标可以移进气泡里接着读长文案
  SideNav    左栏导航树：分组可展开/收起，叶子项高亮，点击回调交给调用方
  ScrollPage 固定外框内的滚动内容区：**滚轮可用**（指针在哪就滚谁），并提供锚点定位

只在 ui 层用（core / connection 不得依赖本模块），零第三方依赖。
"""

import tkinter as tk
from tkinter import ttk

# 提示气泡的观感：Windows 原生 tooltip 就是这个底色，不抢主窗口的视觉
_TIP_BG = "#ffffe1"
_TIP_BORDER = "#8a8a8a"
_FONT = ("Microsoft YaHei UI", 9)


def _destroy_quietly(w):
    try:
        if w is not None and w.winfo_exists():
            w.destroy()
    except Exception:
        pass


def default_bg(widget=None):
    """当前主题的框架底色。

    为什么不用 parent["bg"]：ttk 控件（ttk.Frame）**没有** -background 选项，读它会抛
    KeyError；而 tk.Label 要是不指定 bg，在这套灰底主题里会显出一块补丁色的边框。
    统一问样式要答案，取不到再退回 Windows 常见的 #f0f0f0。
    """
    try:
        v = ttk.Style().lookup("TFrame.background")
        if v:
            return v
    except Exception:
        pass
    return "#f0f0f0"


class HelpDot(object):
    """字段旁的 "?" 悬停提示。

    为什么不是纯悬停即走：本项目里最长的说明有 120+ 字（比如"留空 = 从上面那段自动推"
    那类），鼠标一滑就消失等于读不到；所以气泡创建后**允许指针移进气泡**继续读，
    离开气泡或点击它才关闭。同一时刻全局只保留一个气泡。
    """

    _current = None

    def __init__(self, parent, text, width=420, fg="#8a8a8a", bg=None):
        self.text = str(text or "").strip()
        self.width = int(width)
        self.dot = None
        self._tip = None
        if not self.text:
            return          # 没有说明就什么都不创建：摆个空 "?" 只会让人以为那里有内容
        # width=2：让控件**自己报一个比字形更宽的需求**。只写 padx 时 Label 的
        # reqwidth 贴着 "?" 字形（高 DPI 下约 17px），一旦所在格子被压到 14px，
        # 问号就会被裁掉半个 —— 版式自检里"被挤扁"就是这个。
        self.dot = tk.Label(parent, text="?", font=_FONT, fg=fg, width=2,
                            bg=bg or default_bg(), cursor="question_arrow",
                            padx=1, pady=0)
        self.dot.bind("<Enter>", self._enter)
        self.dot.bind("<Leave>", self._leave_soon)

    def grid(self, **kw):
        if self.dot is not None:
            self.dot.grid(**kw)
        return self.dot

    def pack(self, **kw):
        if self.dot is not None:
            self.dot.pack(**kw)
        return self.dot

    # ---- 显示 / 隐藏 ----
    def _enter(self, _e=None):
        if self.dot is None or not self.dot.winfo_ismapped():
            return      # "?" 还没真正显示出来就别弹气泡（会贴在屏幕左上角）
        HelpDot._close_current()
        tip = tk.Toplevel(self.dot)
        HelpDot._current = tip
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        body = tk.Message(tip, text=self.text, width=self.width, font=_FONT,
                          background=_TIP_BG, foreground="#202020", justify="left",
                          padx=8, pady=6)
        body.pack(fill="both", expand=True)
        border = tk.Frame(tip, background=_TIP_BORDER)
        border.place(relx=0, rely=0, relwidth=1, relheight=1)
        body.lift()
        tip.update_idletasks()
        x, y = self._place(tip.winfo_reqwidth(), tip.winfo_reqheight())
        tip.wm_geometry("+%d+%d" % (x, y))
        tip.bind("<Enter>", self._cancel_leave)
        tip.bind("<Leave>", self._hide)
        tip.bind("<Button-1>", self._hide)
        self._tip = tip

    def _place(self, w, h):
        """贴着 "?" 下方摆；两个方向都做钳制。

        只钳"放不下就翻到上面"是不够的：窗口本身可能被拖到屏幕外（或多屏坐标为负），
        这时 y 会算成负数、气泡跑到看不见的地方。所以最后再统一夹回屏内。
        """
        self.dot.update_idletasks()
        sw, sh = self.dot.winfo_screenwidth(), self.dot.winfo_screenheight()
        x = self.dot.winfo_rootx()
        y = self.dot.winfo_rooty() + self.dot.winfo_height() + 4
        if y + h > sh - 8:
            y = self.dot.winfo_rooty() - h - 4
        x = max(4, min(x, sw - 8 - w))
        y = max(4, min(y, max(4, sh - 8 - h)))
        return x, y

    def _cancel_leave(self, _e=None):
        after = getattr(self, "_after", None)
        if after:
            try:
                self.dot.after_cancel(after)
            except Exception:
                pass
            self._after = None

    def _leave_soon(self, _e=None):
        # 给指针一段"从字段走到气泡"的路途时间
        self._cancel_leave()
        try:
            self._after = self.dot.after(260, self._hide)
        except Exception:
            self._hide()

    def _hide(self, _e=None):
        self._after = None
        tip = getattr(self, "_tip", None)
        if tip is HelpDot._current:
            HelpDot._current = None
        _destroy_quietly(tip)
        self._tip = None

    @classmethod
    def _close_current(cls):
        tip, cls._current = cls._current, None
        _destroy_quietly(tip)

    close = _hide


class SideNav(object):
    """左栏导航：分组标题行（点击展开/收起）+ 缩进的叶子行（点击回调）。

    不用 ttk.Treeview：那是"文件树"长相，行高/缩进/选中色在 Windows 主题下能调的余地
    很小，而且我们要的是"分组标题不导航、叶子导航"这套语义。自己画一共也就这几十行，
    还能被版式自检逐行量。
    """

    BG = "#f6f6f6"
    FG = "#333333"
    SEL_BG = "#d7e6f7"
    SEL_FG = "#0b57d0"
    HDR_FG = "#111111"

    def __init__(self, parent, spec, on_select, width=228):
        self.spec = spec                      # [{key,label,children:[{key,label,...}]} 或 {...,page=...}]
        self.on_select = on_select
        self.collapsed = set()
        self.selected = None
        self._rows_of = {}                    # 叶子 key → 它的 Label（只重画高亮时用）
        self._painted = None                  # 当前已按"选中"样式画出来的那个 key
        self.frame = tk.Frame(parent, background=self.BG, width=width)
        self.frame.pack(side="left", fill="y")
        self.frame.pack_propagate(False)
        self.title = tk.Label(self.frame, text="设置", font=("Microsoft YaHei UI", 11, "bold"),
                              background=self.BG, foreground=self.HDR_FG, anchor="w")
        self.title.pack(fill="x", padx=12, pady=(12, 6))
        tk.Frame(self.frame, background="#dddddd", height=1).pack(fill="x", padx=8)
        self.body = tk.Frame(self.frame, background=self.BG)
        self.body.pack(fill="both", expand=True, padx=4, pady=(4, 8))
        self.render()

    # ---- 内部 ----
    def _rows(self, items=None, depth=0):
        """把树拍平成可见行：收起的组不输出其子项，但组本身保留（再点一次就展开）。"""
        out = []
        for item in (items if items is not None else self.spec):
            kids = item.get("children")
            if kids:
                opened = item["key"] not in self.collapsed
                out.append(("group", item, opened, depth))
                if opened:
                    out += self._rows(kids, depth + 1)
            else:
                out.append(("leaf", item, None, depth))
        return out

    def render(self):
        for c in self.body.winfo_children():
            c.destroy()
        self._rows_of = {}
        for kind, item, opened, depth in self._rows():
            self._mk_row(kind, item, opened, depth)
        self._painted = self.selected

    def _mk_row(self, kind, item, opened, depth=0):
        pad = 8 + 13 * depth
        if kind == "group":
            mark = "▾ " if opened else "▸ "
            lbl = tk.Label(self.body, text=mark + item["label"],
                           font=(_FONT[0], _FONT[1], "bold") if depth == 0 else _FONT,
                           background=self.BG, foreground=self.HDR_FG, anchor="w",
                           padx=pad, pady=5, cursor="hand2")
            lbl.bind("<Button-1>", lambda e, k=item["key"]: self.toggle(k))
        else:
            sel = self.selected == item["key"]
            lbl = tk.Label(self.body, text=item["label"],
                           font=(_FONT[0], _FONT[1], "bold") if sel else _FONT,
                           background=self.SEL_BG if sel else self.BG,
                           foreground=self.SEL_FG if sel else self.FG,
                           anchor="w", padx=pad + 12, pady=4, cursor="hand2")
            lbl.bind("<Button-1>", lambda e, it=item: self.select(it["key"]))
            self._rows_of[item["key"]] = lbl      # 只重画高亮时要能直接找到它
        lbl.pack(fill="x")

    def items(self):
        out = []
        for grp in self.spec:
            out.append(grp)
            out += self._flatten(grp.get("children"))
        return out

    def _flatten(self, items):
        out = []
        for it in items or []:
            out.append(it)
            out += self._flatten(it.get("children"))
        return out

    def find(self, key):
        for it in self.items():
            if it["key"] == key:
                return it
        return None

    # ---- 对外 ----
    def select(self, key):
        was = set(self.collapsed)
        self.selected = key
        # 选中的叶子若藏在某个收起的组里，先把它的**所有祖先组**展开，否则高亮根本看不见
        for path in self._paths():
            for grp in path:
                if any((it.get("key") == key) for it in grp.get("children") or []):
                    self.collapsed.discard(grp["key"])
        if self.collapsed == was and self._rows_of:
            # 可见行没变、只是高亮在动：改两个 Label 的样式就够。
            # 原来每次点击都 destroy/recreate 整栏 14 个标签（实测 12~21ms），
            # 还会连带让导航列重新排一遍——左栏点一下要等一百多毫秒，大头在这。
            self._repaint()
        else:
            self.render()          # 展开/收起了某个组：行数真的变了，只能整栏重建
        item = self.find(key)
        if item is not None and self.on_select is not None:
            self.on_select(item)

    def _repaint(self):
        """只重画"上一个选中"与"这一个选中"两行，其余标签一个都不碰。"""
        for k in (self._painted, self.selected):
            if k is None:
                continue
            lbl = self._rows_of.get(k)
            if lbl is None:
                continue
            try:
                if not lbl.winfo_exists():
                    continue
            except Exception:
                continue
            sel = (k == self.selected)
            lbl.configure(font=(_FONT[0], _FONT[1], "bold") if sel else _FONT,
                          background=self.SEL_BG if sel else self.BG,
                          foreground=self.SEL_FG if sel else self.FG)
        self._painted = self.selected

    def _paths(self, items=None, chain=None):
        """返回每个组节点及其祖先链（三层导航里"展开祖先"必须一路往上找）。"""
        out = []
        chain = chain or []
        for it in (items if items is not None else self.spec):
            if it.get("children"):
                out.append(chain + [it])
                out += self._paths(it["children"], chain + [it])
        return out

    def toggle(self, key):
        if key in self.collapsed:
            self.collapsed.discard(key)
        else:
            self.collapsed.add(key)
        self.render()


def attach_wheel(canvas, viewport=None, step=3):
    """把鼠标滚轮接到某个 Canvas 上（设置窗口与「选择模型」窗口共用）。

    为什么绑在**顶层窗口**而不是 Canvas：Tk 的 <MouseWheel> 只发给指针下面的控件，
    子控件（勾选框、下拉、标签）会把它吃掉，绑 Canvas 根本收不到。
    可绑顶层又会抢走别的区域（聊天区、另一个窗口）的滚轮 —— 所以再加一道
    "指针在可视区内才响应"的判定，两者兼顾。W 报的原话就是"选择模型页面好像不能用鼠标滚动"。
    """
    holder = viewport or canvas

    def _on(event):
        try:
            if not canvas.winfo_exists():
                return None
            x, y = canvas.winfo_pointerxy()
            hx, hy = holder.winfo_rootx(), holder.winfo_rooty()
            if not (hx <= x < hx + holder.winfo_width()
                    and hy <= y < hy + holder.winfo_height()):
                return None                  # 指针不在可视区：把滚轮让出去
            delta = getattr(event, "delta", 0) or 0
            if not delta:                     # Linux 走 Button-4/5
                delta = -120 if getattr(event, "num", 4) == 5 else 120
            units = int(-delta / 120) or (-1 if delta < 0 else 1)
            canvas.yview_scroll(max(-6, min(6, units * int(step))), "units")
        except Exception:
            return None
        return "break"

    top = canvas.winfo_toplevel()
    top.bind("<MouseWheel>", _on, add="+")
    top.bind("<Button-4>", _on, add="+")
    top.bind("<Button-5>", _on, add="+")
    return _on


class ScrollPage(object):
    """固定外框里的滚动内容区。

    滚轮的处理要点（W 明确点过"选择模型页面好像不能用鼠标滚动"）：Tk 的
    ``<MouseWheel>`` 只有 bind_all 才能从子控件那里收到，而 bind_all 会**全局抢滚轮**。
    这里用"指针在可视区内才响应"来判定，既不用给每个子控件补绑定，也不会把滚轮
    从聊天区/别的窗口手里抢走。
    """

    def __init__(self, parent):
        bg = default_bg()
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0,
                                background=bg)
        self.bar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, background=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._page = None
        self._wheel = attach_wheel(self.canvas)      # 滚轮走全窗口共用那套（见 attach_wheel）

    # ---- 尺寸 ----
    def _on_inner_configure(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))

    def _on_canvas_configure(self, event):
        # 内容宽度贴着可视宽度：否则长控件会把 scrollregion 撑出右边，
        # 看上去像"页面比窗口宽"，实际却滚不到
        self.canvas.itemconfigure(self._win, width=max(1, event.width))

    # ---- 页切换 ----
    def set_page(self, frame):
        if self._page is not None and self._page is not frame:
            try:
                self._pack_forget()
            except Exception:
                pass
        self._page = frame
        frame.pack(in_=self.inner, fill="both", expand=True, padx=(16, 18), pady=(12, 16))
        self.canvas.yview_moveto(0)
        self._refresh()

    def _pack_forget(self):
        if self._page is not None:
            self._page.pack_forget()

    def _refresh(self):
        self.canvas.update_idletasks()
        self.inner.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))

    # ---- 锚点定位 ----
    def goto(self, widget, offset=8):
        """滚到某个区块标题的正上方（左栏点「服务参数」这类跳转就靠它）。

        只付**一次**布局。原来是"refresh → 滚到顶 → 再 refresh → 拿屏幕坐标差量位置"，
        三次 update_idletasks，而每次都是整棵控件树的几何重算（实测单次 17ms、
        占一次左栏点击的大头）。改成沿父链累加 winfo_y() 求"在滚动内容里的偏移"：
        这个值与当前滚到哪儿无关，所以既不用先滚到顶，也不用滚完再量一次。
        """
        if widget is None:
            return
        self._refresh()
        try:
            y, w = 0, widget
            while w is not None and w is not self.inner:
                y += w.winfo_y()
                w = getattr(w, "master", None)
            if w is not self.inner:
                return                  # 不在这块滚动内容里：别乱滚
            total = max(1, self.inner.winfo_height())
        except Exception:
            return
        frac = (y - int(offset)) / float(total)
        # 末尾的区块要允许滚到底：moveto(1.0) 正好把最后一屏露出来
        self.canvas.yview_moveto(max(0.0, min(1.0, frac)))
