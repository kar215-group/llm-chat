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

from . import theme

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


_BG_CACHE = None          # default_bg 的成功解析结果（主题在运行期不变，查一次就够）


def default_bg(widget=None):
    """当前主题的框架底色。

    为什么不用 parent["bg"]：ttk 控件（ttk.Frame）**没有** -background 选项，读它会抛
    KeyError；而 tk.Label 要是不指定 bg，在这套灰底主题里会显出一块补丁色的边框。
    统一问样式要答案，取不到再退回 Windows 常见的 #f0f0f0。

    `lookup` 的签名是 (样式名, 选项) 两个参数 —— 早先写成单参数 `lookup("TFrame.background")`，
    每次调用都抛 TypeError 再落兜底色（开一次设置页白抛 167 次异常）。修正后在 vista 主题
    下返回的是系统色名 `SystemButtonFace`，开发机实测它解析出的 RGB 与 `#f0f0f0` **逐位相同**
    （winfo_rgb 均为 61680/61680/61680），所以这里顺手把系统色名折算成 #rrggbb：
    返回值形状与历史上的兜底色一致，任何"按字符串比对底色"的地方都不受影响 —— 零外观变化。
    """
    global _BG_CACHE
    if _BG_CACHE:
        return _BG_CACHE
    try:
        v = ttk.Style().lookup("TFrame", "background")
        if v:
            try:
                # 系统色名（SystemButtonFace 这类）解析成规范 #rrggbb；winfo_rgb 给的是
                # 16 位通道（0~65535），右移 8 位回到 0~255
                any_w = widget if widget is not None else tk._default_root
                r, g, b = any_w.winfo_rgb(v)
                v = "#%02x%02x%02x" % (r >> 8, g >> 8, b >> 8)
            except Exception:
                pass                    # 解析不了就按原样用（Tk 也认系统色名）
            _BG_CACHE = v
            return v
    except Exception:
        pass
    return "#f0f0f0"


def set_app_icon(root):
    """把应用图标换成自带的标志（标题栏左上角 + 任务栏），不再用 Tk 那根默认羽毛。

    图标数据内嵌在 `ui/app_icon.py`（base64 的 PNG），**不读外部文件**：单文件 exe 里
    `__file__` 指向临时解包目录（坑 62 同族），走文件就得再算一遍 `_MEIPASS` 路径，
    而 `tk.PhotoImage(data=...)` 原生吃 base64，路径这件事直接不存在。

    `iconphoto(True, ...)` 的 True 是"设为整个应用的默认"，所以设置窗口、「选择模型」
    这些后续新建的 Toplevel 会一起跟上，不用每个窗口各调一次。
    图片对象挂在 root 上：Tk 那边按名字认图，Python 对象一旦被 GC 就会顺手把图像删掉，
    图标随即消失 —— 不留引用就是"启动时是好的，过一会儿变回羽毛"这种查起来很费劲的现象。
    """
    from .app_icon import PNG_B64
    try:
        img = tk.PhotoImage(data=PNG_B64)
    except tk.TclError:
        return                      # 这份 Tk 没编进 PNG 支持：留默认图标，别因此开不了窗
    root._app_icon = img
    root.iconphoto(True, img)


def app_logo(parent):
    """关于页那张大标志（返回 PhotoImage，**调用方必须留住引用**，同 set_app_icon）。

    为什么按 `tk scaling` 挑档位：Windows 上 Tk 会把字体、边框、控件尺寸都按 DPI 放大，
    **但图片不放大**（Tk 8.6 的 PhotoImage 只有 zoom/subsample 这种最近邻操作）。
    所以同一张 128px 图在 200% 屏上看着正好、在 100% 屏上就大一倍。两档都在生成时就
    缩好（`tools/make_icon.py`），取哪一档由界面缩放决定，不在运行期做劣质缩放。
    阈值 1.75 落在 1.0/1.25/1.5 与 2.0 之间：1.5 档（144dpi）用 128 已经偏大。
    """
    from .app_icon import LOGO_B64, PNG_B64
    big = True
    try:
        big = float(parent.tk.call("tk", "scaling")) >= 1.75
    except Exception:
        pass
    try:
        return tk.PhotoImage(data=(LOGO_B64 if big else PNG_B64))
    except tk.TclError:
        return None             # 这份 Tk 没编进 PNG 支持：关于页少张图，别因此开不了窗


class SpotlightGuide(object):
    """全窗口遮罩式新手引导：把当前步骤指的那块"挖亮"，旁边贴一段说明。

    两档实现，运行时自动选（`self.punch` 为真才是甲）：

      甲 · 真半透明 + 洞内可点：无边框 Toplevel 盖住主窗口客户区，整窗 `-alpha` 变暗，
          再用 `-transparentcolor` 把洞那一块设成穿透色。Windows 对 color-key 像素
          **既不画也不收鼠标**，所以被指着的那个控件用户能直接点 —— 引导不该拦着人真操作
          （让他当场点一次「启动服务」看状态灯变，比读三行字有用）。
      乙 · 四块不透明深色矩形**只围出洞的轮廓**：`-transparentcolor` 或 `-alpha` 有一个不可用就退到这档。
          视觉是纯黑遮罩 + 一圈 ACCENT 描边 —— 目标控件**看不见也点不到**（不透明的窗口挡在上面），
          所以它只是"引导还能打开"的兜底，别指望它和甲档一样能当场操作（Windows 10/11 上实测走的是甲档）。

    文案面板的坐标一律钳回可视区内（沿用 HelpDot 那套纪律，坑 75 / 58）；
    主窗口移动或缩放时跟着重算（绑 `<Configure>`，重画前 `after(60)` 去抖，
    不然拖动窗口过程中每帧都要重排一次整张遮罩）。
    """

    DARK = "#14141c"
    KEY = "#010203"                 # 穿透色：正常界面里几乎不可能出现的颜色
    ACCENT = "#8fd4ff"
    PANEL_W = 420
    GAP = 14                        # 洞与面板之间的距离

    def __init__(self, host, steps, on_close=None):
        self.host = host
        self.steps = [s for s in steps if s]
        self.on_close = on_close
        self.i = 0
        self._sync_id = None
        self._top_id = None                   # 层序自查那个 60ms 定时器的句柄（close 要取消）
        self._fnt = None                      # 折行用的字体度量（第一次画时才建）
        self._rendering = False               # 重入保护：遮罩自己的 Configure 会再触发一次画
        self._last_size = None
        self._geom = None                     # 我们请求给遮罩的尺寸（画遮罩以它为准）
        self._last_geom = None                # 上一次真的设了几何（没变就别再设，防 Configure 空转）
        self._nav_bar = None                  # 按钮条常驻：只挪位置改文字，不反复建销
        self._nav_skip = self._nav_step = None
        self._nav_next = self._nav_prev = None
        self.closed = False
        self.win = tk.Toplevel(host, bg=self.KEY)
        self.win.overrideredirect(True)
        self.win.transient(host)
        self.punch = self._try_punch()
        self.cv = tk.Canvas(self.win, bg=(self.KEY if self.punch else self.DARK),
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        # 绑在**主窗**上的每一对 (事件, funcid) 都记下来：close() 要逐个解掉。
        # 必须记 funcid —— 不带 funcid 的 `unbind(seq)` 会把主窗自己在这条事件上的
        # 绑定一起删掉（那会顺手弄坏聊天区），而漏解则会每次重看引导都留下四个死回调。
        self._host_binds = []
        self._bind_host("<Configure>", self._on_host_resize)
        # 遮罩自己被改大小之后还要再同步一次：主窗口的 <Configure> 到得比遮罩几何生效**早**，
        # 那一刻量到的画布尺寸还是旧的（W 报的"没完全遮住"）
        self.win.bind("<Configure>", self._on_overlay_resize, add="+")
        # 深色区域被点到时把遮罩抬回最前
        self.cv.bind("<Button-1>", lambda e: self._ensure_top(), add="+")
        # `overrideredirect` 的窗口不归窗口管理器管：用户点一下别的程序再回来，Windows 会把
        # 主窗口抬到遮罩**上面**，遮罩就沉到界面底下了（W 报的第一条，坑 120）。
        # 为什么是"即时事件 + 60ms 兜底"而不是别的两种做法（都实测过）：
        #   · `<Activate>` / `<Deactivate>`：真跨进程实验里（SetForegroundWindow 切到另一个
        #     进程的窗口再切回来）**一次都没发出来**，事件驱动实现不了 —— W 说"切走就藏
        #     没生效"就是这个原因；
        #   · `-topmost`：层序是稳，但遮罩会浮在别的程序上面挡路（要配藏匿，而藏匿靠的就是
        #     上面那个不发的事件，做不到）。
        # 绑在**主窗**上的 Button-1 是关键：遮罩已经沉下去时它收不到点击，只能靠主窗收到后
        # 把遮罩抬回来；`<FocusIn>` / `<Map>` 管 Alt+Tab 回来与最小化恢复。
        self._bind_host("<Button-1>", self._on_activate)
        self._bind_host("<FocusIn>", self._on_activate)
        self._bind_host("<Map>", self._on_activate)
        self._wrap_cache = {}               # 正文折行按步号缓存（拖动尺寸时不必反复重算）
        self._pw_cached = None
        self._itm = {}                      # 画布图形按类别池化复用（见 _pool）
        self._place()
        self._render()
        self._ensure_top()
        self._keep_top()

    def _bind_host(self, seq, fn):
        """在主窗上挂一个回调，并把 funcid 记进 `_host_binds`（close 时逐个解掉）。"""
        fid = self.host.bind(seq, fn, add="+")
        self._host_binds.append((seq, fid))
        return fid

    # ---- 层序 ----
    def _ensure_top(self):
        """把遮罩抬回主窗口上面。幂等，可以随便多调几次。"""
        try:
            self.win.lift()
        except Exception:
            pass

    def _on_activate(self, _e=None):
        self._ensure_top()

    def _app_is_foreground(self):
        """本应用现在是不是在前台。是才抬升 —— 否则遮罩会浮在别的程序上面挡路。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            ga_root = 2
            hwnd = user32.GetAncestor(self.host.winfo_id(), ga_root)
            return bool(hwnd) and user32.GetForegroundWindow() == hwnd
        except Exception:
            return True               # 问不出来就照旧抬升：宁可多抬，别把遮罩丢到下面去

    def _keep_top(self):
        """引导开着期间每 60ms 看一眼：主窗口在前台而遮罩在它下面，就把遮罩抬回来。

        为什么不能只绑 `<Activate>`：`overrideredirect` 的遮罩不归窗口管理器管，
        用户点一下别的程序再回来，Windows 会把**主窗口**抬到遮罩上面，而 Tk 这边
        Activate / Deactivate 并不总发（实测进程内焦点绕一圈就收不到），
        于是遮罩沉到界面底下透出来（W 报的第一条）。
        60ms 一次、整轮实测 0.0195ms（约 0.016% CPU），只在引导打开期间存在，关闭即停 ——
        与 `_poll`(80ms) / `_status_loop`(3s) 是同一类主线程定时器，不碰线程边界。
        """
        self._top_id = None
        if not self.alive():
            return
        if self._app_is_foreground():
            self._ensure_top()
        try:
            self._top_id = self.host.after(60, self._keep_top)
        except Exception:
            pass

    # ---- 能力探测 ----
    def _try_punch(self):
        try:
            self.win.attributes("-transparentcolor", self.KEY)
            self.win.attributes("-alpha", 0.80)
            return True
        except tk.TclError:
            try:
                self.win.configure(bg=self.DARK)
            except Exception:
                pass
            return False

    # ---- 生命周期 ----
    def alive(self):
        try:
            return (not self.closed) and self.win.winfo_exists()
        except Exception:
            return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self._sync_id:
                self.host.after_cancel(self._sync_id)
        except Exception:
            pass
        try:
            if getattr(self, "_top_id", None):
                self.host.after_cancel(self._top_id)      # 层序自查的 60ms 定时器要停掉
        except Exception:
            pass
        # 主窗上那四个绑定必须逐个解掉：漏解的话每重看一次引导就永久留下四个指向
        # 已销毁窗口的死回调，之后每次点击 / 拖动都空跑一轮 Tcl 求值，越用越卡
        for seq, fid in getattr(self, "_host_binds", []):
            try:
                self.host.unbind(seq, fid)
            except Exception:
                pass
        self._host_binds = []
        try:
            self.win.destroy()
        except Exception:
            pass
        cb = self.on_close
        self.on_close = None
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _on_host_resize(self, _e=None):
        if not self.alive():
            return
        # 定位**立刻**做（一次 geometry 调用，很便宜），让遮罩跟得上窗口边框；
        # 重画用 after_idle 合并 —— 拖一次窗口会送来几十次 Configure，每次都全量重画
        # 就是白烧 CPU（实测一次重画 14ms，正是"跟不上手"的来源）。
        self._place()
        if self._sync_id:
            return
        try:
            self._sync_id = self.host.after_idle(self._redraw)
        except Exception:
            self._redraw()

    def _redraw(self):
        self._sync_id = None
        if not self.alive():
            return
        self._place()                 # after_idle 期间可能又拖了几下，取最新的
        # 一次尺寸变化会绕出 2~3 次 Configure（主窗 → 遮罩 → 遮罩自己改完几何又回一次），
        # 实测 20 步拖动能触发 58 次重画。尺寸没变就直接返回：重画只跟着"真的变了"走。
        # 换步骤那条路走的是 _render()，不经这里，所以不会被这道判断挡住。
        if (self._geom or (0, 0)) == self._last_size:
            return
        self._render()

    def _on_overlay_resize(self, _e=None):
        """遮罩自己的几何生效后再走一遍同样的路径（尺寸没变时 `_place` 会自己短路）。"""
        if not self.alive() or self._rendering:
            return
        self._on_host_resize()

    def _place(self):
        try:
            x, y = self.host.winfo_rootx(), self.host.winfo_rooty()
            w, h = self.host.winfo_width(), self.host.winfo_height()
            if w < 40 or h < 40:
                w, h = self.host.winfo_reqwidth(), self.host.winfo_reqheight()
            if (w, h, x, y) == self._last_geom:
                return                  # 没变就别再设一次：设了会再触发 Configure，白绕一圈
            self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self._geom = (w, h)          # 画遮罩用**请求的尺寸**，不量画布（见 _draw）
            self._last_geom = (w, h, x, y)
        except Exception:
            pass

    # ---- 画 ----
    def _hole(self, step):
        """目标控件在遮罩坐标系里的矩形；没有目标（欢迎页）返回 None。"""
        w = step.get("target")
        if w is None:
            return None
        try:
            if not w.winfo_exists() or not w.winfo_ismapped():
                return None
            pad = int(step.get("pad", 6))
            return (w.winfo_rootx() - self.host.winfo_rootx() - pad,
                    w.winfo_rooty() - self.host.winfo_rooty() - pad,
                    w.winfo_rootx() - self.host.winfo_rootx() + w.winfo_width() + pad,
                    w.winfo_rooty() - self.host.winfo_rooty() + w.winfo_height() + pad)
        except Exception:
            return None

    def _render(self):
        """重画一遍遮罩。

        重入标志**必须用 try/finally 收**：中途抛一次异常就让标志永远停在 True，
        之后所有尺寸变化都不再重画 —— 症状就是"拖窗口时遮罩只盖住一半 / 组件留残影"
        （W 报的第三条，开发机实测能稳定复现到卡死状态）。
        """
        if not self.alive() or self._rendering:
            return
        self._rendering = True
        try:
            self._draw()
        finally:
            self._rendering = False

    def _draw(self):
        if not self.alive():
            return
        cv = self.cv
        step = self.steps[min(self.i, len(self.steps) - 1)]
        # 尺寸用**我们请求给遮罩的那个数**，不量画布：`cv.winfo_width()` 在几何刚改完时
        # 常常还是旧值（update_idletasks 也不保证刷新窗口尺寸），照着旧值画就是
        # "拖窗口时遮罩只盖住一半"（W 报的第三条）。请求值永远是对的。
        W, H = self._geom or (cv.winfo_width(), cv.winfo_height())
        hole = self._hole(step)
        if hole:
            x0, y0, x1, y1 = [int(v) for v in hole]
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(W, x1), min(H, y1)
            anchor = (x0, y0, x1, y1)
        else:
            x0 = y0 = x1 = y1 = 0
            anchor = None
        # 遮罩的矩形：甲档 = 整幅深色 + 一块穿透色把洞"挖"回来；乙档 = 四块围出洞。
        # 图形**建一次就复用**（`_pool` + `coords`），只改坐标不再 delete/create ——
        # 拖一次窗口要重画几十次，delete+create 那 30 多个 Tcl 调用就是"跟不上手"的大头
        if anchor:
            masks = [(0, 0, W, H), (x0, y0, x1, y1)] if self.punch else [
                (0, 0, W, y0), (0, y1, W, H), (0, y0, x0, y1), (x1, y0, W, y1)]
        else:
            masks = [(0, 0, W, H)]
        ids = self._pool("mask", len(masks), lambda: cv.create_rectangle(
            0, 0, 0, 0, outline=""))
        for i, (a, b, c, d) in enumerate(masks):
            top = ids[i]
            cv.coords(top, a, b, c, d)
            cv.itemconfig(top, fill=(self.KEY if (anchor and self.punch and i == 1)
                                     else self.DARK),
                          outline=(self.KEY if (anchor and self.punch and i == 1)
                                   else self.DARK))
        ring = self._pool("ring", 1 if anchor else 0, lambda: cv.create_rectangle(
            0, 0, 0, 0, outline=self.ACCENT, width=2))
        if ring:
            cv.coords(ring[0], x0, y0, x1, y1)

        body = step.get("body") or []
        pw = min(self.PANEL_W, max(220, W - 24))
        fnt = self._font()
        inner = max(60, pw - 32)
        # 折行结果按"步号"缓存：`_wrap_px` 是逐字符 measure（48 字一行实测 3.4ms），
        # 而拖动窗口时同一屏会被重画几十次 —— 只有面板宽度变了才值得重新折
        if pw != self._pw_cached:
            self._wrap_cache = {}
            self._pw_cached = pw
        wrapped = self._wrap_cache.get(self.i)
        if wrapped is None:
            wrapped = [(t, _wrap_px(t, fnt, inner)) for t in body]
            self._wrap_cache[self.i] = wrapped
        ph = 14 + (26 if step.get("title") else 0) + sum(len(ls) for _t, ls in wrapped) * 18 + 8
        if anchor:
            px = min(max(8, anchor[0]), max(8, W - pw - 8))
            py = anchor[3] + self.GAP
            if py + ph > H - 8:
                py = max(8, anchor[1] - ph - self.GAP)
        else:
            px = max(8, (W - pw) // 2)
            py = max(8, (H - ph) // 2)
        px = max(8, min(px, max(8, W - pw - 8)))
        py = max(8, min(py, max(8, H - ph - 8)))
        pnl = self._pool("panel", 1, lambda: cv.create_rectangle(
            0, 0, 0, 0, fill="#22222e", outline="#3c3c50", width=1))
        cv.coords(pnl[0], px, py, px + pw, py + ph)
        ttl = self._pool("title", 1 if step.get("title") else 0, lambda: cv.create_text(
            0, 0, anchor="w", fill="#ffffff", font=("Microsoft YaHei UI", 11, "bold")))
        y = py + 12
        if ttl:
            cv.coords(ttl[0], px + 14, y)
            cv.itemconfig(ttl[0], text=step["title"])
            y += 26
        flat = []
        for warn, ls in wrapped:
            for ln in ls:
                flat.append((ln, warn.startswith("!")))
        ln_ids = self._pool("lines", len(flat), lambda: cv.create_text(
            0, 0, anchor="w", font=fnt))
        for i, (top, (txt, amber)) in enumerate(zip(ln_ids, flat)):
            cv.coords(top, px + 14, y + i * 18)
            cv.itemconfig(top, text=txt, fill=("#ffd47a" if amber else "#d8d8e4"))
        # 自检要拿这两块矩形判断"洞真的包住了目标""面板没溢出窗口"（留句柄比让它去猜
        # canvas 里的图形可靠，同坑 78 给区块留 _sec_id 的做法）
        self.hole_rect = hole
        self.panel_rect = (px, py, px + pw, py + ph)
        self._last_size = (W, H)              # 与 _geom 同一口径（都是"请求的尺寸"）
        self._nav(px, py, pw, ph, W, H)
        self._rendering = False

    def _font(self):
        if self._fnt is None:
            from tkinter import font as tkfont
            self._fnt = tkfont.Font(family="Microsoft YaHei UI", size=9)
        return self._fnt

    def _pool(self, key, n, maker):
        """让画布上某一类图形的数量等于 n（多退少补），返回 id 列表。

        复用而不是 delete + create：拖动窗口时每一步都要重画，而图形种类和数量是稳定的，
        变的只有坐标和文字 —— `coords()` / `itemconfig()` 一次调用比"销毁再新建"便宜得多
        （实测单次重画 3.6ms → 约 1ms）。
        """
        cv = self.cv
        ids = self._itm.setdefault(key, [])
        while len(ids) < n:
            ids.append(maker())
        while len(ids) > n:
            cv.delete(ids.pop())
        return ids

    def _nav(self, px, py, pw, ph, W, H):
        """跳过 / 上一步 / 下一步 + 第几步：**贴着面板下方居中，控件只建一次**。

        两件事都在这里：
          · 位置：原来钉在窗口底边，而面板经常一路铺到底 → 按钮条正好压住最后一行
            介绍（W 报的）。改成跟着面板走：下面放得下就放下面，放不下挪到面板上方。
          · 开销：原来每次重画都新建一套按钮再销毁旧的，而拖一次窗口会重画几十次 ——
            建控件（开发机实测每个约 1.3ms）全砸在拖动路径上，这就是"遮罩跟不上手"的
            另一半。现在按钮条建一次，之后只 `place` 挪位置 + 改文字。
        """
        b = self._nav_bar
        if b is None or not b.winfo_exists():
            b = self._nav_bar = tk.Frame(self.win, bg="#22222e")
            self._nav_skip = tk.Button(b, text="跳过引导", command=self.close, relief="flat",
                                       bg="#22222e", fg="#a8a8b8", activebackground="#2e2e3c",
                                       activeforeground="#ffffff",
                                       font=("Microsoft YaHei UI", 9))
            self._nav_skip.pack(side="left", padx=(6, 0))
            self._nav_step = tk.Label(b, bg="#22222e", fg="#8f8fa4",
                                      font=("Microsoft YaHei UI", 9))
            self._nav_step.pack(side="left", padx=10)
            self._nav_next = tk.Button(b, command=self._next, relief="flat",
                                       bg="#3a6df0", fg="#ffffff", activebackground="#4a7dff",
                                       activeforeground="#ffffff",
                                       font=("Microsoft YaHei UI", 9, "bold"))
            self._nav_next.pack(side="right", padx=(0, 6))
            self._nav_prev = tk.Button(b, text="← 上一步", command=self._prev, relief="flat",
                                       bg="#2b2b38", fg="#d8d8e4", activebackground="#35354a",
                                       activeforeground="#ffffff",
                                       font=("Microsoft YaHei UI", 9))
        last = self.i >= len(self.steps) - 1
        self._nav_step.configure(text="%d / %d" % (self.i + 1, len(self.steps)))
        self._nav_next.configure(text="完成" if last else "下一步 →")
        if self.i:
            self._nav_prev.pack(side="right", padx=(0, 6))
        else:
            self._nav_prev.pack_forget()
        bar_h = 34
        y = py + ph + 6
        if y + bar_h > H - 6:
            y = max(6, py - bar_h - 6)
        if y + bar_h > H - 6:
            y = max(6, H - bar_h - 6)
        b.place(relx=0.5, y=y, height=bar_h, anchor="n")

    def _drop_nav(self):
        bar = getattr(self, "_nav_bar", None)
        self._nav_bar = None
        try:
            if bar is not None:
                bar.destroy()
        except Exception:
            pass

    def _next(self):
        if self.i >= len(self.steps) - 1:
            self.close()
            return
        self.i += 1
        self._render()               # 按钮条是常驻的，_nav 自己改文字与"上一步"的显隐

    def _prev(self):
        if self.i:
            self.i -= 1
            self._render()


def _wrap_px(text, fnt, maxw):
    """按**像素宽度**折行。

    别按字符数估：一个中日韩字在 9pt 下约 18px，而标点、英文、数字各不一样，
    200% 缩放的机器上按"每行 35 字"排出来的文案会直接溢出面板甚至窗口
    （开发机实测：面板 420px，那行字要 700px）。
    """
    text = str(text)
    if text.startswith("!"):
        text = text[1:]
    lines, cur = [], ""
    for ch in text:
        if fnt.measure(cur + ch) > maxw and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [""]


class HelpDot(object):
    """字段旁的 "?" 悬停提示。

    为什么不是纯悬停即走：本项目里最长的说明有 120+ 字（比如"留空 = 从上面那段自动推"
    那类），鼠标一滑就消失等于读不到；所以气泡创建后**允许指针移进气泡**继续读，
    离开气泡或点击它才关闭。同一时刻全局只保留一个气泡。
    """

    _current = None

    def __init__(self, parent, text, width=420, fg="#8a8a8a", bg=None):
        # 说明文案里成对的 `**强调**` 是写给源码看的 markdown：气泡是 tk.Message，
        # 只会原样画出星号（用户看到的是"这里为什么有星号"）。单星号留着 ——
        # 那是尺寸写法「宽*高」的一部分
        self.text = str(text or "").strip().replace("**", "")
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
        # 先把"离开后延时关闭"那个定时器取消掉：指针在 "?" 上抖一下（离开又马上回来）时，
        # 旧定时器会在 260ms 后命中 `self._tip` —— 那已经是**新**气泡了，于是刚弹出来的
        # 说明被上一次的延时顺手销毁（表现为"气泡闪一下就没"）
        self._cancel_leave()
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


_TIP_FNT = None       # bind_tip 量宽用的字体对象（懒建；Font 需要默认 root 已存在）


def bind_tip(widget, text_fn, width=360):
    """给任意控件挂悬停气泡（附件瓷砖的完整文件名、置灰选项的原因等）。

    与 `HelpDot` 的气泡同款样式，并共用它的全局"同时只有一个气泡"登记表
    （`HelpDot._current`）：点开「?」会收掉这里的，反之亦然。
    与 HelpDot 的差别：不要求把指针移进气泡里读 —— 这里挂的是至多两三行的
    短信息（长解释仍归「?」），鼠标离开即收。
    text_fn 可以是字符串，也可以是零参函数（弹层时才取现值，刷新后置灰理由不会过期）。
    """
    def _cancel():
        after = getattr(widget, "_tip_after", None)
        if after:
            try:
                widget.after_cancel(after)
            except Exception:
                pass
        widget._tip_after = None

    def _hide(_e=None):
        _cancel()
        tip = getattr(widget, "_tip_win", None)
        if tip is HelpDot._current:
            HelpDot._current = None
        _destroy_quietly(tip)
        widget._tip_win = None

    def _show(_e=None):
        _cancel()
        text = text_fn() if callable(text_fn) else str(text_fn or "")
        text = text.strip().replace("**", "")
        if not text or not widget.winfo_ismapped():
            return
        HelpDot._close_current()
        tip = tk.Toplevel(widget)
        HelpDot._current = tip
        widget._tip_win = tip
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        # tk.Message 的 width 是"最长一行的像素宽"：先按上限折行，再取实测最宽行，
        # 短文本就不会撑成一个空荡荡的 360px 大方块。
        # _FONT 是元组，量宽得用 tkfont.Font（建一次缓存住，Font 要有默认 root 才能建）
        global _TIP_FNT
        if _TIP_FNT is None:
            from tkinter import font as tkfont
            _TIP_FNT = tkfont.Font(family=_FONT[0], size=_FONT[1])
        lines = _wrap_px(text, _TIP_FNT, width)
        body = tk.Message(tip, text=text, font=_FONT,
                          width=min(width, max(_TIP_FNT.measure(ln) for ln in lines) + 20),
                          background=_TIP_BG, foreground="#202020", justify="left",
                          padx=8, pady=6)
        body.pack(fill="both", expand=True)
        border = tk.Frame(tip, background=_TIP_BORDER)
        border.place(relx=0, rely=0, relwidth=1, relheight=1)
        body.lift()
        tip.update_idletasks()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        sw, sh = widget.winfo_screenwidth(), widget.winfo_screenheight()
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        if y + h > sh - 8:
            y = widget.winfo_rooty() - h - 4            # 下方放不下 → 弹上方
        x = max(4, min(x, sw - 8 - w))                  # 贴边时整体收回屏内
        y = max(4, min(y, max(4, sh - 8 - h)))
        tip.wm_geometry("+%d+%d" % (x, y))

    def _leave(_e=None):
        _cancel()
        try:
            widget._tip_after = widget.after(180, _hide)   # 指针抖动不误收
        except Exception:
            _hide()

    widget.bind("<Enter>", _show)
    widget.bind("<Leave>", _leave)
    return widget


class SideNav(object):
    """左栏导航：分组标题行（点击展开/收起）+ 缩进的叶子行（点击回调）。

    不用 ttk.Treeview：那是"文件树"长相，行高/缩进/选中色在 Windows 主题下能调的余地
    很小，而且我们要的是"分组标题不导航、叶子导航"这套语义。自己画一共也就这几十行，
    还能被版式自检逐行量。

    叶子可以带 `"nav_hide": <别的叶子 key>`：**这一项不在左栏成行**，但仍然是导航目标
    （`find`/`select`/展开祖先都照旧认得它），选中它时高亮记在它写的那个"替身"行上。
    给"一个左栏项指向同一页里的第二段内容"用 —— 页面由若干区块拼成（设置窗口按页懒建、
    同页区块一次建齐），左栏不该逼着一页里的每一段都占一行。
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
        self._top_of = {}                     # 叶子 key → 是不是顶层条目（决定要不要一直加粗）
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
        """把树拍平成可见行：收起的组不输出其子项，但组本身保留（再点一次就展开）。

        `nav_hide` 的叶子不在这里出现（它由 `find`/`select` 那条路走，见类注释）。
        """
        out = []
        for item in (items if items is not None else self.spec):
            kids = item.get("children")
            if kids:
                opened = item["key"] not in self.collapsed
                out.append(("group", item, opened, depth))
                if opened:
                    out += self._rows(kids, depth + 1)
            elif not item.get("nav_hide"):
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
            # 顶层叶子（"本地模型 API""关于与诊断"）本身就是第一级标题，没选中也要加粗：
            # 和缩在组里的二级项（"文本模型"）在层级上要一眼能分出来（W 2026-10-01 定）。
            self._top_of[item["key"]] = (depth == 0)
            lbl = tk.Label(self.body, text=item["label"],
                           font=(_FONT[0], _FONT[1], "bold") if (sel or depth == 0) else _FONT,
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

    def _hl_key(self, key):
        """高亮该记在哪一行：`nav_hide` 的叶子自己不成行，交回它写的替身 key。

        不这么做的话，跳到一个隐藏目标（输出栏的「去配置引擎」就是这么进的）之后
        左栏一行都不亮，用户不知道自己在这儿的哪里。
        """
        it = self.find(key)
        return (it or {}).get("nav_hide") or key

    # ---- 对外 ----
    def select(self, key):
        was = set(self.collapsed)
        self.selected = self._hl_key(key)
        # 选中的叶子若藏在收起的组里，先把它的**所有祖先组**展开（三层导航里 g_local 也可能
        # 是收着的），否则高亮根本看不见。判据要走 `_flatten`（含孙辈）：只比"直接子项"时
        # 三层叶子会被静默漏掉 —— 页面照切、左栏没有任何一行亮着
        for path in self._paths():
            for grp in path:
                kids = self._flatten(grp.get("children"))
                if any(it.get("key") == key for it in kids):
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
            lbl.configure(font=(_FONT[0], _FONT[1], "bold") if (sel or self._top_of.get(k)) else _FONT,
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
            # 指针底下若是**自己会滚的控件**（设置页的多行 Text、勾选窗口的 Listbox），
            # 让给它：抢过来的话两边会同时滚（页面 + 控件），看着像界面在乱动
            under = canvas.winfo_containing(x, y)
            while under is not None and under is not canvas:
                try:
                    if under.winfo_class() in ("Text", "Listbox"):
                        return None
                except Exception:
                    break
                under = getattr(under, "master", None)
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


class PageStack(object):
    """同一容器里"多页常驻、一次只显示一页"的叠放器。

    **为什么不用 pack_forget + pack**（2026-10-08 实测，W 报"设置页切页卡 + 整页跳一下"）：
    `pack_forget` 会让那一支的**几何缓存全部失效**，下一次 `update_idletasks()`
    必须重算该支所有控件的几何。代价按"被标脏的控件数"线性增长。于是那一次
    "整页硬切"既卡又跳（旧页已收、新页未画，中间态被 Windows 合成器看见）。

      |切法 | 隔离树实测 | 真实设置窗 |
      |---|---|---|
      | `pack_forget` + `pack`（旧） | 30.9 ms（最大 67.3） | **89.3 ms** |
      | `grid` + `grid_remove` | 4.6 ms（最大 46.0） | — |
      | **同格 `grid` 常驻 + `tkraise()`（现行）** | **0.94 ms（最大 2.0）** | **2.0 ms** |

    做法：所有页 `grid` 到**同一个格子**（row=0, column=0）互相重叠，
    切页只调`tkraise()` 把目标页提到最前 —— Tk 里`tkraise` 只改叠放次序，
    **不产生几何失效**，切页近乎免费（实测 44倍）。

    ⚠ **代价 A：几何不再标脏 = 内容变了不会自己重排**。所以增删控件后仍要调
    `_refresh()`（`set_page` 的首屏分支、以及任何会改内容的路径）。

    两条必须知道的性质（调用方要按这个来）：
      · `inner` 的高度 = **最高页**的高度，不再随当前页变。
        好处是切页不再改变 scrollregion（少一次滚动跳变）；
        代价是"矮页滚不动"（内容比视口矮时，本来到底也滚不了，语义不变）。
      · **所有页始终 `ismapped`**。判断"当前是哪一页"要用
        `PageStack.current`，**不能**再用 `winfo_ismapped()`（旧实现靠后者区分，
        自检 `test_settings_layout.py` 有多处这么写，改栈后要一并改）。

    ⚠ **2026-10-08 第二轮复核（W 报"设置页拖动缩放卡顿"）**：试过"只让当前页受管
    （其余 `grid_remove`）"的变体，**同进程 A/B 实测零收益**（123.8 vs 125.5 ms/档）
    —— 隐藏页的控件本来就不参与缩放重排（隔离实测：+4 个隐藏页 / 180 控件 = 0 成本），
    故**维持叠放常驻**。真正的残因与数据见 `08` §10.4 / 坑 171。

    只做几何与可见性，不碰滚动、不碰内容 —— 那些仍归 `ScrollPage`。
    """

    CELL = (0, 0)                # 所有页重叠的格子
    PADX = (16, 18)              # 内容左右留白（与旧 set_page 的 pack 参数一致）
    PADY = (12, 16)

    def __init__(self, container):
        self.container = container
        self._frames = []                # 按加入顺序（= 建页顺序）保序
        self._current = None

    # ---- 对外 ----
    def add(self, frame):
        """把一页挂进容器（同格叠放）。重复挂同一帧是幂等的。"""
        if frame in self._frames:
            return frame
        self._frames.append(frame)
        frame.grid(row=self.CELL[0], column=self.CELL[1], sticky="nsew",
                   padx=self.PADX, pady=self.PADY)
        # 页面自己内部用 pack/grid 混排时，需要内层格子能撑开
        try:
            self.container.grid_rowconfigure(self.CELL[0], weight=1)
            self.container.grid_columnconfigure(self.CELL[1], weight=1)
        except Exception:
            pass
        return frame

    def show(self, frame):
        """显示某一页（提到最前）。这一页没被add 过就先add。

        **不触发几何重算** —— 这是本类存在的全部意义。
        （2026-10-08 复核过"只让当前页受管（其余 `grid_remove`）"的替代方案：
        同进程 A/B 实测**无收益** —— 隐藏页的控件本来就不参与缩放重排，
        见类注释与 `08` §10.4。）
        """
        if frame not in self._frames:
            self.add(frame)
        self._current = frame
        frame.tkraise()

    @property
    def current(self):
        """当前显示的那一页（`show` 的最后一次）。没显示过就是None。"""
        return self._current

    @property
    def frames(self):
        """全部已挂进来的页（建过的都留着，与旧实现"建过就不销毁"一致）。"""
        return list(self._frames)

    def is_current(self, widget):
        """`widget` 是否属于当前页 —— **判"当前页"的唯一判据**。

        页面共格叠放（且非当前页 `grid_remove`），`winfo_ismapped` 分不清
        "属于哪一页"（它只说明"现在有没有显示"）。调用方（自检、页内逻辑）
        判断页归属一律走这里。
        """
        cur = self._current
        if cur is None or widget is None:
            return False
        try:
            w = widget
            while w is not None:
                if w is cur:
                    return True
                w = getattr(w, "master", None)
        except Exception:
            return False
        return False


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
        # 滚动条走 theme 工厂：自建「原生元素」样式，重绘成本 ~25× 低 ——
        # 这是设置页 / 管理本地模型窗口拖动缩放卡顿的修复（2026-10-08，`08` §10）
        self.bar = theme.scroll(wrap, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, background=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._stack = PageStack(self.inner)   # 页面叠放（见 PageStack 类注释）
        self._wheel = attach_wheel(self.canvas)  # 滚轮走全窗口共用那套（见 attach_wheel）

    # ---- 页切换 ----
    def set_page(self, frame):
        """显示某一页。

        内部走 `PageStack`（同格叠放 + `tkraise`），**不再 pack_forget/pack** ——
        后者会让整支几何失效、切页付 30~67ms（实测，见 `PageStack` 类注释）。
        这里仍保留一次 `_refresh()`：新页刚挂进来时它的 reqwidth/reqheight 还没算，
        要算出来才能定scrollregion 与 `goto` 用的偏移。
        """
        first = self._stack.current is None
        self._stack.show(frame)
        if first:
            # 首屏：内容从无到有，这一次几何重算是必须的
            self._refresh()
        else:
            # 切页：页面几何早已算好（它一直在容器里），这里只更新滚动区域
            self.canvas.yview_moveto(0)
            self._sync_region()

    @property
    def current_page(self):
        """当前显示的页（`set_page` 的最后一次）。"""
        return self._stack.current

    def is_current(self, widget):
        """`widget` 是否在当前页里 —— 判"当前页"的唯一判据。

        ⚠ 别改用 `winfo_ismapped()`：页面共格叠放，非当前页只是 unmapped，
        而"属于哪一页"要按 `PageStack._current` 的归属走（自检
        `test_settings_layout.py` 全量走这里）。
        """
        return self._stack.is_current(widget)

    def page_frames(self):
        """全部已挂进来的页（建过的都留着，与旧实现"建过就不销毁"一致）。"""
        return self._stack.frames

    # ---- 尺寸 ----
    def _sync_region(self, _e=None):
        """scrollregion 与实际内容对齐（W 报"所有子页面都能向上滚动出大片空白"）。

        根因（实测钉死）：Tk 画布在 `scrollregion 高 < 视口高` 时，origin 的
        合法区间不是 [0,0] 而是 **[region−视口, 0] —— 允许负值**（向上滚能把
        内容推下去、露出 region−视口 那么大一片空白），而向下滚又被钳在 0，
        所以只有"向上"方向能滚出空白。W 的窗口高 891、所有页都矮于视口 ⇒
        每一页都中招。⚠ 排查提示：`yview()` 的分数上报会把负 origin 饱和成
        (0.0,1.0)，看着像"没滚"，量这个必须用 `canvasy(0)`（本轮因此绕了一大圈）。
        修法：region 撑到"内容与视口取大" ⇒ slack=0 ⇒ origin 区间 [0,0]，
        矮页滚不动、长页区间恰为 内容−视口（可滚动范围与实际内容一致）。
        canvas 的 <Configure>（窗口缩放）与 inner 的 <Configure>（内容增减）
        都走这里，两个方向的变化都被覆盖。
        不做 update_idletasks：Configure 事件本身就代表几何已落定，而本函数
        在缩放拖动中会被连续调用 —— 每次插一次 idle 排空会把重建过程层层放大。
        """
        bbox = self.canvas.bbox("all") or (0, 0, 0, 0)
        vh = max(1, self.canvas.winfo_height())
        vw = max(1, self.canvas.winfo_width())
        self.canvas.configure(scrollregion=(0, 0, max(bbox[2], vw), max(bbox[3], vh)))

    def _on_inner_configure(self, _e=None):
        self._sync_region()

    def _on_canvas_configure(self, event):
        # 内容宽度贴着可视宽度：否则长控件会把 scrollregion 撑出右边，
        # 看上去像"页面比窗口宽"，实际却滚不到
        self.canvas.itemconfigure(self._win, width=max(1, event.width))
        self._sync_region()

    # ---- 几何 ----
    def _refresh(self):
        self.canvas.update_idletasks()
        self.inner.update_idletasks()
        self._sync_region()

    # ---- 锚点定位 ----
    def goto(self, widget, offset=8):
        """滚到某个区块标题的正上方（左栏点「服务参数」这类跳转就靠它）。

        **几何已在 `set_page` 里算好了**（叠放实现下页面一直挂在容器里，
        它的reqwidth/reqheight 不会因为切页而失效），所以这里**不再**付一次
        全树 `update_idletasks`。确有变化时才补算：`widget` 不是当前页的
        （理论上不会 —— 定位的都是刚显示的那页）或它还没被映射过。

        原来写的是"只付一次布局"（沿父链累加 `winfo_y()` 求偏移，与当前滚到
        哪儿无关，所以不用先滚到顶再量）。那条继续成立；本次去掉的只是
        那次多余的 `update_idletasks`（实测 0.02~0.9ms，慢主题下更高）。
        """
        if widget is None:
            return
        if not self._geom_ready(widget):
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

    def _geom_ready(self, widget):
        """`widget` 的几何是否已经可信（当前页 + 容器已完成过布局）。"""
        try:
            if not widget.winfo_ismapped():
                return False
            if self._stack.current is not None and not self._stack.is_current(widget):
                return False
            # 内层高度为 1 表示画布窗口还没真正 layout 过
            return self.canvas.winfo_width() > 1
        except Exception:
            return False


def center_on(win, host=None, size=None):
    """把这个 Toplevel 摆到 `host`（默认它的父窗口）水平居中、垂直略偏上，然后显示它。

    `size=(宽, 高)`：**调用方显式定过窗口尺寸时把它传进来**。窗口还没映射时
    `winfo_width()` 与 `geometry()` 都还是 `1x1`（坑 161），那时读回来的尺寸是假的，
    原样写回去会被 `minsize` 夹成最小窗，我们显式设过的尺寸也被这一次写覆盖掉了。

    为什么需要：Tk 的 Toplevel **不给位置就摆在屏幕左上角**，于是「API Key」「选择模型」
    「诊断」「成本估算」「管理本地模型」这一排子窗口全叠在左上角，跟它们所属的设置页
    分离。`dialogs.ExitDialog` 早就是自己算居中的，这里把那套算法收成一个地方。

    **调用前先把窗口 `withdraw()` 掉**（见各处建窗的写法），否则会看到"先在左上角闪一下
    再跳到中间"—— Tk 的 Toplevel 一创建就映射，`center_on` 是在控件建完之后才调到的，
    那中间几十毫秒用户看得见（W 2026-10-03 报的"管理本地模型"按钮闪动就是这个）。

    量尺寸前必须 `update_idletasks()`：还没布局过时 `winfo_width()` 是 1。
    子窗口比宿主大时 `max(0, ...)` 把它顶到宿主左上角，不会算出负坐标把窗口甩出屏幕。
    """
    host = host or win.master
    try:
        win.update_idletasks()
        # ⚠ `withdraw()` 状态下 `winfo_width()` 与 `geometry()` 都可能还是 **1x1**
        # （Tk 还没把那次 geometry 请求落到窗口上），这时读回来的尺寸是假的：
        # 原样写回去等于请求一个 1x1 的窗，被 `minsize` 夹成最小窗，调用方显式设过的
        # 尺寸也被这一次写覆盖掉（坑 161）。所以：`size` 传了就用它；否则先试
        # `winfo_width`，再试 geometry 串，都拿不到（还是 1x1）就**只写位置**，
        # 尺寸留给调用方那次请求生效。用 reqwidth 只为把位置算得接近。
        w, h = size if size else (win.winfo_width(), win.winfo_height())
        geom = win.geometry().split("+")[0]              # "400x300"
        if (w <= 1 or h <= 1) and "x" in geom and geom != "1x1":
            sw, sh = geom.split("x", 1)
            w, h = int(sw), int(sh)
        known = w > 1 and h > 1
        if not known:
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()   # 只拿来算位置
        x = host.winfo_rootx() + max(0, (host.winfo_width() - w) // 2)
        y = host.winfo_rooty() + max(0, (host.winfo_height() - h) // 3)
        # 尺寸没把握时**只写位置**：写 "1x1+x+y" 会被 minsize 夹成最小窗，
        # 并把调用方那次 geometry 请求覆盖掉（坑 161）
        win.geometry(("%dx%d+%d+%d" % (w, h, x, y)) if (size or known)
                     else ("+%d+%d" % (x, y)))
    except Exception:
        pass               # 宿主已销毁 / 还没映射：位置摆不了就不摆，别为它炸出调用方
    try:
        win.deiconify()     # 摆好再显示：用户第一眼看到的就是最终位置
    except Exception:
        pass
