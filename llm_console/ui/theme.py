# -*- coding: utf-8 -*-
"""llm_console.ui.theme — 主页面主题层（ttkbootstrap 试验分支）

ttkbootstrap 的**唯一入口**：主题名在下面的 THEME_NAME 改一处即整体切换明暗主题；
主页面所有颜色经 c() / text_kw() 取、所有 ttk 控件经 button() / radio() / check()
建，不再散落硬编码色值（用户可见外观层，布局与交互不变）。

ttkbootstrap 2.x 的样式引擎只给方角按钮；圆角按钮在这里自建（Round.* 系样式）：
PIL 画圆角矩形贴图 → ttk image 元素九宫格拉伸（border 左右圆角不变形、中段拉宽），
normal / hover / pressed / disabled 四态齐全，颜色全部取自当前主题色 ——
换主题重启后 apply() 重画，自动跟随。ttk 的 element_create 官方幂等，重复构建安全。

**可回退闸（本分支试验纪律）**：ttkbootstrap 没安装时整个模块自动降级 ——
apply() 空操作、取色返回引入前的原值、控件工厂退回原生 ttk 类并丢弃样式参数，
主页面外观与引入前逐位相同，程序照常运行。卸载 ttkbootstrap 即回到主线外观。

范围边界：ttk 主题是整个 Tk 实例级的（ttk.Style 机制），设置页等窗口的 ttk 控件
会一并换肤 —— 这是同一入口的自然结果；tk 原生控件（tk.Label / tk.Frame 等）不受
主题影响，照旧走 widgets.default_bg() 或系统色。ttkbootstrap 2.x import 零副作用
（不全局 patch 原生控件，已实测），未改造的控件行为不变。

贴图几何口径：本程序是 DPI-aware 的，Tk 坐标 = 物理像素，所以贴图用固定物理值
（高 34px ≈ vista 主题按钮高度），不跟 tk scaling 绑定 —— 高分屏上字体渲染大
（pt→px 自动翻倍）但按钮高度与低分屏一致，观感统一。
"""

# ---- 主题名（改这一行整体切换明暗主题，重启生效）----
# 浅色候选：cosmo（默认，蓝主色调，与原界面最接近）/ minty / united / pulse /
#           sandstone / pydata / nord-light / tokyo-night-light / catppuccin-light /
#           solarized-light / one-light / everforest-light / gruvbox-light
# 深色候选：nord-dark / tokyo-night-dark / one-dark / dracula-dark /
#           catppuccin-dark / everforest-dark / solarized-dark / bootstrap-dark
# （c() / button() 的语义键不变，切主题全组件自动跟随）
THEME_NAME = "cosmo"             # 浅色主题（默认）
DARK_THEME_NAME = "nord-dark"     # 深色主题（2026-10-08 W 拍板打样；候选见文件头）

# ---- 发版临时屏蔽（v1.1.1 已发布时置 True；W 2026-10-09 定：深色模式下一步再开发）----
# True = 屏蔽深色模式：resolve_mode 一律返回 light（跟随系统也只是浅色）、设置页
# 不建「界面主题」下拉、外观页的 help 文案同步换短版。机制本体（set_mode / 深色
# 贴图 / DWM 等）全部保留，改回 False 一处即完整恢复（settings 与自检都按它分岔）。
RELEASE_LIGHT_ONLY = False

# ---- 圆角按钮贴图几何（物理像素）----
BTN_H = 34        # 贴图高 = 按钮高（对齐 vista 主题按钮的 34px，2.x 默认 44px）
BTN_RADIUS = 8    # 圆角半径
BTN_MIN_W = 48    # 贴图宽 = 按钮最小宽（九宫格中段随文字拉宽）
BTN_PAD_X = 14    # 文字距左右边的像素
BTN_ICON_PAD_X = 4   # 图标按钮（Round.Icon.TButton）的文字/图标边距
BTN_ICON_W = 36      # 图标按钮贴图宽（近正方形）

# ---- 滑动开关贴图几何（物理像素）----
TOG_W = 44        # 轨道宽（胶囊）
TOG_H = 24        # 轨道高
TOG_KNOB = 18     # 滑块直径
TOG_ANIM_MS = 35  # 切换的中间态过渡时长（一帧中间位，落定即回终态）

# ---- 输入类紧凑尺寸（2.x 默认边距偏大，实测 Combobox 高 43px vs vista 30px）----
# configure 的 padding 由 ttk 按**像素**解释；给 builder.scale_size 包装的值，
# 与主题其余部分同口径（2.x 的缩放系数自管，实测 ≈1.5，非 tk scaling）。
ENTRY_PAD_LOGICAL = (8, 3)

# 语义键 → (ttkbootstrap 色键, 无 ttkbootstrap 时的原值)。
# 原值 = 引入 ttkbootstrap 前主页面各处硬编码的颜色，降级路径靠它逐位还原。
_SEMANTIC = {
    # 强调（模型名按钮 / 用户名 / 云端状态灯 / 取回按钮）原 #0b57d0
    "accent": ("primary", "#0b57d0"),
    # 状态灯：运行中 原 #1a7f37
    "ok": ("success", "#1a7f37"),
    # 状态灯：模型加载中 原 #b58900
    "warn": ("warning", "#b58900"),
    # 状态灯：未运行 / 次要灰字（思考过程、meta、附件状态行、× 按钮）原 #999999 一族
    "muted": ("secondary", "#999999"),
    # 瓷砖文件名、「带图方式」等次级标签 原 #555555 / #5a6a7a
    "label": ("secondary", "#555555"),
    # 回答正文 原 #1f1f1f
    "body": ("fg", "#1f1f1f"),
    # 错误文案 / × 悬停 原 #c01c28
    "error": ("danger", "#c01c28"),
    # 聊天区 / 瓷砖 / 附件条底色 原 #ffffff（default_bg 同源）
    "bg": ("bg", "#ffffff"),
    # 瓷砖边框 / 输入框高亮边 原 #c9c9c9 / #cccccc
    "border": ("border", "#c9c9c9"),
}

# ---- 应用级调色板（键 → (浅色, 深色)）----
# 这些色在 ttkbootstrap 的色表里没有对应键（或现有键的浅色值 ≠ W 调过的现状），
# 所以按**模式**直接给值：浅色值 = 现状字面色（逐位保持今天的视觉），深色值 =
# nord-dark 适配（打样后 W 过目再调）。经 c() 取，切换模式自动跟随。
_APP_PALETTE = {
    "hint":  ("#808080", "#9aa4b5"),   # 弹窗/说明灰
    "note":  ("#5a6a7a", "#8a96a8"),   # 注脚/次级说明蓝灰
    "dim":   ("#5a5a5a", "#7d8899"),   # 行内摘要灰
    "oklit": ("#1a7f37", "#a3be8c"),   # 页面内嵌"运行中/成功"（顶栏状态灯走 c("ok") 语义键）
    "warnlit": ("#b58900", "#ebcb8b"), # 页面内嵌"注意/加载中"
    "errlit": ("#c01c28", "#bf616a"),  # 页面内嵌错误（聊天 error 标签走 c("error") 语义键）
    "navbg": ("#f6f6f6", "#2b303c"),   # 左栏面板
    "navfg": ("#333333", "#d8dee9"),   # 左栏文字
    "navhdr": ("#111111", "#e5e9f0"),  # 左栏组标题
    "flash": ("#fff3c4", "#4a4326"),   # 高级模式跳转黄底
    "panel": ("SystemButtonFace", "#3b4252"),  # 填写类弹窗面板（浅=系统灰）
    "warndim": ("#b06000", "#d08770"), # 源码运行提示的暗琥珀
    "accentlit": ("#0b57d0", "#88c0d0"),  # 页面内嵌强调文字（浅=原字面）
    "hdr": ("#111111", "#e5e9f0"),     # 窗内大标题/区块头
}

# bootstyle 语义键 → 自建圆角样式名（apply() 构建；见 _build_round_buttons）。
# 键 "" = 无 bootstyle 的默认按钮（设置页大量按钮走它）→ 中性描边圆角。
_ROUND_STYLE = {
    "": "Round.Secondary.TButton",
    "primary": "Round.TButton",                    # 实心主操作（发送）
    "primary-outline": "Round.Primary.TButton",    # 描边（取回 / 去配置引擎）
    "secondary-outline": "Round.Secondary.TButton",
    "danger-outline": "Round.Danger.TButton",
    "success-outline": "Round.Success.TButton",
}

_style = None                 # ttkbootstrap Style 单例（apply 后非 None）
_ACCENT = ""                  # 全局主题色配置（apply 时传入；空 = 跟主题）
_ACCENT_SEL = ""              # 选中文本强调色覆盖（空 = 跟主题默认，**不跟**全局主题色）
_THEME_ACCENT = ""            # 主题自己的强调色（apply 时在注入**前**抓下来当"默认"）
_MODE = "light"               # 当前模式：light / dark（apply / set_mode 维护）
_ROUND_OK = False             # 圆角样式族构建成功（工厂据此决定套样式还是裸原生控件）
# 滚动条样式名 {vertical/horizontal}（apply() 里 _build_fast_scroll 自建）；
# 建不出来就是空 dict，scroll() 回落旧路径
_SCROLL_STYLES = {}
try:
    from ttkbootstrap.style import Style as _TBStyle
    import ttkbootstrap as _ttb
except Exception:             # 未安装：整层降级，绝不拖垮启动
    _TBStyle = None
    _ttb = None


def apply(root, accent=None, accent_sel=None, mode="light"):
    """在 main() 建 root 之后、App 构造之前调用一次：把主题应用到整个窗口。

    mode：light / dark（外观页的"跟随系统"在调用方先经 resolve_mode() 解析成
    这两档）。主题名 = theme_name_for(mode)。

    ttkbootstrap 2.x 的 Style 是 ttk.Style 子类，无参 super().__init__() 会绑定
    当时的默认 root —— 所以必须在 root 建好之后调。同时把根窗底色对齐主题，
    免得深色主题下控件缝隙露出系统白底。
    返回 Style（或降级时 None）；Style 挂在模块级，取色走 c()。

    accent / accent_sel：外观页的「全局主题色」与「选中文本强调色（覆盖）」配置
    （十六进制色或空串）。accent 非空时在样式构建前注入 colors.primary ——
    发送按钮、取回、模型名、用户名、状态灯、滑动开关这些"蓝色元素"全部跟它
    （它们本就同源于 primary 一个变量）。**两者互相独立**：accent_sel 只管
    选中文本，空 = 跟主题默认强调色（不是 accent —— W 2026-10-08 定"解除
    联动、可独立配置"）。主题自己的强调色在注入**前**抓进 _THEME_ACCENT，
    作为两处"恢复默认"的落点（否则恢复默认会被注入色抢回去，实测踩过）。
    运行期改色只影响**选中文本**（Text.configure 便宜），其余样式是启动时
    构建的，改色后下次启动生效。
    """
    global _style, _ACCENT, _ACCENT_SEL, _THEME_ACCENT, _MODE
    _ACCENT = str(accent or "")
    _ACCENT_SEL = str(accent_sel or "")
    _MODE = "dark" if mode == "dark" else "light"
    if _TBStyle is None:
        return None
    # 多 root 场景（自检夹具并存多个 Tk 实例）：Style 是单例且绑定创建时的
    # 解释器 —— 换了 root 必须重置单例重建，否则样式/贴图全落在旧 root 上，
    # 新窗口的控件找不到样式、图片报 doesn't exist（实测复现）。单 root 的
    # 正常路径（main() 只调一次 apply）不受影响。
    try:
        if _TBStyle.instance is not None and _TBStyle.instance.master is not root:
            _TBStyle.instance = None
    except Exception:
        pass
    try:
        _style = _TBStyle(theme=theme_name_for(_MODE))
    except Exception:         # 主题名写错 / 初始化失败：退回原生外观，不让启动炸掉
        _style = None
        return None
    try:
        _THEME_ACCENT = str(_style.colors.get("primary") or "")
    except Exception:
        _THEME_ACCENT = ""
    if _ACCENT:
        try:
            _style.colors.set("primary", _ACCENT)   # 样式构建前注入，全套蓝色元素跟随
        except Exception:
            pass
    try:
        root.configure(background=c("bg"))
    except Exception:
        pass
    try:
        _build_round_buttons()
    except Exception:
        # 圆角层失败不拖垮启动：按钮退回 2.x 原生变体（工厂里 get 不到样式名
        # 时走原生 bootstyle；这里失败时样式名指向不存在的样式 → ttk 默认外观）
        pass
    try:
        _build_fast_scroll()
    except Exception:
        # 滚动条样式建不出来（非 Windows / vista 主题缺失）：scroll() 回落旧路径，
        # 只是拖着窗口缩放时会慢，不影响功能
        pass
    try:
        _build_semantic_styles()
    except Exception:
        pass
    try:
        _ensure_widget_skin()
    except Exception:
        pass
    try:
        _compact_entries()
    except Exception:
        pass
    try:
        install_focus_policy()
    except Exception:
        pass
    try:                       # 标题栏 / 窗框跟着当前模式（深色时翻深）
        set_window_frame(root)
    except Exception:
        pass
    return _style


# 鼠标点击后仍要落焦点的控件（要打字 / 光标指示属于可用性）；
# 其余控件点击后焦点还给 root —— focus 态消失，一切焦点环/系统虚框一起消失
_INPUT_CLASSES = {"Entry", "Text", "Spinbox", "Combobox"}


def install_focus_policy():
    """鼠标点击不落焦点框、键盘 Tab 导航保留（W 定；含 tk.Listbox 的系统点状框）。

    难点：Tk 的 focus 是单比特状态，**分不出"鼠标点的"还是"Tab 走到的"**，
    没有原生开关 —— 唯一通用解是点击事件打标。Tk 的事件处理顺序是
    控件绑定 → 类绑定（ButtonPress 里自聚焦发生在这一层）→ toplevel → all，
    所以 bind_all 的回调在自聚焦**之后**执行，这里把焦点收回到**所在顶层窗口**
    即可让刚点过的控件脱离 focus 态；键盘 Tab 不产生 Button-1，照常聚焦画环。
    ⚠ 焦点收给 `winfo_toplevel()` 而不是全局 root：设置窗口是独立 Toplevel，
    收给 root 会把 OS 活动窗切回主窗（设置窗标题栏失活、z 序可能被顶下去）；
    收给"被点控件所在的那个顶层窗口"则窗口活跃性原样保持，只是控件级焦点清掉。
    输入类（Entry/Text/Spinbox/Combobox）豁免：要打字，其高亮是光标指示。
    """
    root = _style.master

    def on_click(event):
        try:
            cls = str(event.widget.winfo_class())
        except Exception:
            return None
        if cls in _INPUT_CLASSES:
            return None
        try:
            event.widget.winfo_toplevel().focus_set()
        except Exception:
            try:
                root.focus_set()
            except Exception:
                pass
        return None

    try:
        root.bind_all("<Button-1>", on_click, add="+")
    except Exception:
        pass


def c(key):
    """主页面取主题色的唯一出口。

    解析顺序：应用调色板（_APP_PALETTE，浅/深两套现值）→ _SEMANTIC（ttkbootstrap
    色键）→ 原值。无 ttkbootstrap 时调色板键仍给浅色值（与无主题时代的观感一致）。
    """
    pal = _APP_PALETTE.get(key)
    if pal is not None:
        return pal[1] if _MODE == "dark" else pal[0]
    tb_key, fallback = _SEMANTIC[key]
    if _style is None:
        return fallback
    try:
        return _style.colors.get(tb_key)
    except Exception:         # 色键在新主题缺失：宁可退原值也不停摆
        return fallback


def mode():
    """当前模式：light / dark。"""
    return _MODE


def theme_name_for(mode):
    """模式 → 主题名（浅 = cosmo，深 = nord-dark）。"""
    return DARK_THEME_NAME if mode == "dark" else THEME_NAME


def system_prefers_dark():
    r"""Windows 系统是否偏好深色应用（跟随系统的判据）。

    读 HKCU\...\Themes\Personalize 的 AppsUseLightTheme（DWORD，0 = 深色）；
    键不存在 / 读不了（老系统、非 Windows）一律按浅色。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def resolve_mode(value):
    """cfg["ui_theme"]（auto/light/dark）→ light/dark。auto = 跟随系统。"""
    if RELEASE_LIGHT_ONLY:
        return "light"            # v1.1.1 临时屏蔽：一律浅色（见顶部开关注释）
    v = str(value or "auto")
    if v == "dark":
        return "dark"
    if v == "light":
        return "light"
    return "dark" if system_prefers_dark() else "light"


def _dwm_frame_now(win):
    """真正去挂 DWM 属性（见 set_window_frame 的说明）。"""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        val = ctypes.c_int(1 if _MODE == "dark" else 0)
        for attr in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
            except Exception:
                pass
        try:
            if _MODE == "dark":
                bc = c("bg")                     # 页面底色，如 "#2e3440"
                try:
                    r, g, b = (int(bc[1:3], 16), int(bc[3:5], 16), int(bc[5:7], 16))
                except (ValueError, IndexError):
                    r, g, b = (0x2E, 0x34, 0x40)
                color = ctypes.c_uint(r | (g << 8) | (b << 16))   # COLORREF 0x00BBGGRR
            else:
                color = ctypes.c_uint(0xFFFFFFFF)                 # DWMWA_COLOR_DEFAULT
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
        except Exception:
            pass
    except Exception:
        pass


def set_window_frame(win):
    """把窗口的标题栏 / 边框切到深色（Windows DWM；非 Windows / 失败一律静默）。

    2026-10-09 W 报「深色下应用边框仍然是白色的」—— Tk 的客户区换深了，
    OS 画的标题栏还是浅色。DWMWA_USE_IMMERSIVE_DARK_MODE（属性号：新系统 20、
    老版本 19）一挂即翻；翻深之后 DWM 还会在标题栏外画一圈 1px 描边，用
    DWMWA_BORDER_COLOR（属性号 34）把它染成页面底色，浅色发 DWMWA_COLOR_DEFAULT
    还系统默认。
    ⚠ **映射前挂不上**（2026-10-09 像素级实测：apply() 在建窗之前调的那次全被
    覆盖，标题栏保持浅色；窗口映射后再挂一次立刻变深）——所以这里挂完再排一次
    after 延迟重挂；窗口在延迟到点前销毁就跳过。hwnd 取法：winfo_id() 是内层
    客户窗，顶层的真 hwnd 是父窗（GetParent）。任何 Toplevel 建完都可以调。
    """
    _dwm_frame_now(win)
    try:
        win.after(250, lambda: _dwm_frame_now(win)
                  if win.winfo_exists() else None)
    except Exception:
        pass
    # 真正的兜底是映射事件：窗口**显示那一下**会把映射前挂的属性覆盖掉，而 250ms
    # 的延迟在启动路径里可能仍早于映射（实测截图回浅）。Map 事件是"已显示"的硬
    # 信号，挂在这里必中；重复触发（最小化还原等）重挂也无害。
    try:
        win.bind("<Map>", lambda _e, w=win: _dwm_frame_now(w), add="+")
    except Exception:
        pass


def style_toplevel(win, bg_key="bg"):
    """**每个 Toplevel 建完必调**：经典背景染主题色 + 标题栏 / 框线翻深。

    两件事收在一处（坑 184 是前一件、深色标题栏是后一件）：
      · 经典 Toplevel 的背景是 SystemButtonFace（#f0f0f0）**不跟主题** —— 深色下
        内容件与窗缘之间那圈 pack 边距把它露出来，就是"最外层一圈白边"；
      · OS 画的标题栏 / 1px 框线要 DWM 属性才翻深（set_window_frame 的活）。
    为什么必须有这个入口：2026-10-10 W 报"次级窗大量深色 bug"—— 上一轮只在
    设置窗修了这两件事，**九个次级窗全部漏接**（坑 186 的教训：修复要收口 + 拉
    全清单）。新窗一律走这里，别再手写两行。
    `bg_key`：窗内主体是 ttk 面板（Frame/Label，底色=主题 bg）用默认 "bg"；
    填写类小对话（内容走 panel_label 面板色）传 "panel"。
    """
    try:
        win.configure(background=c(bg_key))
    except Exception:
        pass
    set_window_frame(win)


def default_accent():
    """主题自己的强调色（未注入任何配置时的 primary）。

    外观页两处"恢复默认"与选中文本的默认都落在这里 —— **不能**用 c("accent")
    当默认：全局主题色非空时它已被注入成用户配置的色，"恢复默认"会被抢回
    旧配置（2026-10-08 W 报的"恢复默认恢复的是上次保存的颜色"）。
    """
    return _THEME_ACCENT or _SEMANTIC["accent"][1]


# ttk 语义样式：字面色 foreground 的替代品 —— 控件挂样式名而不是写死色，
# 样式在 apply / set_mode 时按当前模式 configure，切换主题自动跟随
# （ttk 控件显式 foreground= 是控件级选项，theme_use 不会重设 —— 别走那条路）。
_SEMANTIC_STYLES = {
    "Hint.TLabel": "hint",
    "Note.TLabel": "note",
    "Dim.TLabel": "dim",
    "OkLit.TLabel": "oklit",
    "WarnLit.TLabel": "warnlit",
    "WarnDim.TLabel": "warndim",
    "ErrLit.TLabel": "errlit",
    "Muted.TLabel": "muted",
    "Label2.TLabel": "label",
    "AccentLit.TLabel": "accentlit",
    "Hdr.TLabel": "hdr",
}


def _build_semantic_styles():
    """按当前模式 configure 语义样式（apply 与 set_mode 各调一次）。"""
    st = _style
    if st is None:
        return
    for name, key in _SEMANTIC_STYLES.items():
        try:
            st.configure(name, foreground=c(key))
        except Exception:
            pass


def set_mode(mode, accent=None, accent_sel=None):
    """运行期切换浅 / 深模式（同一个 root 上实时生效）。

    `style.theme_use(主题名)` 重画 ttk 控件与它注册过的经典 tk 控件（实测聊天
    Text 都会自动翻深）；自绘贴图（Round 圆角族 / 滑动开关）颜色烘焙在 PIL 图里，
    要重跑三个构建函数（幂等，取当前主题色）；`default_bg()` 的全局缓存清掉；
    `_THEME_ACCENT` 重新抓新主题原色（"恢复默认"的落点跟着换），强调色配置重放。
    经典控件里烘焙的语义色由调用方的 retint（app.App.retint + retint_widgets）
    重涂。返回是否成功（ttkbootstrap 没装 / 切换抛错 = False，界面原地不动）。
    """
    global _MODE, _THEME_ACCENT
    if _style is None:
        return False
    try:
        _style.theme_use(theme_name_for(mode))
    except Exception:
        return False
    _MODE = "dark" if mode == "dark" else "light"
    try:
        _THEME_ACCENT = str(_style.colors.get("primary") or "")
    except Exception:
        _THEME_ACCENT = ""
    if _ACCENT:
        try:
            _style.colors.set("primary", _ACCENT)
        except Exception:
            pass
    for build in (_build_round_buttons, _build_fast_scroll, _build_semantic_styles):
        try:
            build()
        except Exception:
            pass
    try:                       # 已建出的滚动条换到当前模式的样式（深色不再留白条）
        restyle_scrollbars(_style.master)
    except Exception:
        pass
    try:                       # 标题栏 / 窗框跟着翻深（W 2026-10-09 报）
        set_window_frame(_style.master)
    except Exception:
        pass
    return True


def tint(w, fg=None, bg=None):
    """按语义键给经典控件上色并盖 `_tint` 戳。

    经典控件（tk.Label / tk.Text / 显式 foreground 的 ttk.Label）的颜色是
    创建时烘焙的，theme_use 不会重设 —— 戳在控件上，主题切换后 retint_widgets
    遍历重涂。fg / bg 是 c() 的键名（None = 不动该向）。
    """
    kw = {}
    if fg:
        kw["foreground"] = c(fg)
    if bg:
        kw["background"] = c(bg)
    if kw:
        try:
            w.configure(**kw)
        except Exception:
            pass
    try:
        w._tint = (fg, bg)
    except (AttributeError, TypeError):
        pass
    return w


def retint_widgets(root_widget):
    """遍历存活的控件树，按 `_tint` 戳重涂（主题切换后调用一次）。

    从 root 走 winfo_children：Toplevel 是 root 的孩子，所以**开着的次级窗**
    一起被走到。控件已销毁 / configure 失败都按没看见处理。
    """
    stack = [root_widget]
    while stack:
        w = stack.pop()
        t = getattr(w, "_tint", None)
        if t:
            fk, bk = t
            kw = {}
            if fk:
                kw["foreground"] = c(fk)
            if bk:
                kw["background"] = c(bk)
            if kw:
                try:
                    w.configure(**kw)
                except Exception:
                    pass
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass


def text_kw(fallback_bg=None):
    """tk 原生文本控件（聊天区 / 输入框）的主题配色参数。

    返回 dict 直接 ** 展开进构造参数：有主题时给 background / foreground /
    insertbackground（插入光标，深色主题下不配上等于看不见光标）与选区色；
    无主题时给 {}（或仅 fallback_bg，供原来就显式白底的聊天区逐位还原）。

    选区色：**只跟「选中文本强调色」配置（_ACCENT_SEL），与全局主题色互相
    独立**；留空 = 主题默认强调色（default_accent()）。选区文字色按底色亮度
    自动配黑/白（同实心按钮的规则），用户选浅色时不会白底白字。
    """
    if _style is None:
        return {"background": fallback_bg} if fallback_bg else {}
    colors = _style.colors
    sel_bg = _ACCENT_SEL or default_accent()
    sel_fg = "#ffffff" if _luma(sel_bg) < 0.62 else "#1a1a1a"
    return {"background": _safe(colors, "inputbg", c("bg")),
            "foreground": _safe(colors, "inputfg", c("body")),
            "insertbackground": c("body"),
            "selectbackground": sel_bg,
            "selectforeground": sel_fg}


def _safe(colors, key, fallback):
    try:
        v = colors.get(key)
        return v if v else fallback
    except Exception:
        return fallback


def button(master, **kw):
    """ttk.Button 工厂：接受 theme.button(parent, bootstyle="primary-outline", …)。

    有主题且圆角样式族构建成功 → 一律套自建圆角样式（已知变体用对应色；
    无 bootstyle 的默认按钮套中性描边 Round.Secondary.TButton —— 设置页大量
    按钮走这条）。
    ⚠ 不能用 ttkbootstrap.Button：它的 BootMixin 会把显式 style= 当 bootstyle
    重新解析覆盖掉（实测 "Round.Secondary.TButton" 被改成 "secondary.TButton"），
    圆角样式永远挂不上 —— 原生 ttk.Button 原样保留 style=，且圆角样式本就注册
    在 ttk 引擎里，兼容一切既有调用。
    无 ttkbootstrap / 构建失败 → 原生 ttk.Button，样式参数丢弃（原生控件不认
    bootstyle，会直接 TclError）。
    """
    boot = kw.pop("bootstyle", None)
    if _style is not None and _ROUND_OK and "style" not in kw:
        # 显式 style= 优先（Round.Icon.TButton 这类特殊几何直接指定，工厂不路由）
        name = _ROUND_STYLE.get(boot or "")
        if name:
            return _ttk().Button(master, style=name, **kw)
    return _ttk().Button(master, **kw)


def entry(master, **kw):
    """ttk.Entry 工厂：圆角输入框（Round.TEntry，自建 field 贴图）。"""
    if _style is not None and _ROUND_OK:
        return _ttk().Entry(master, style="Round.TEntry", **kw)
    return _ttk().Entry(master, **kw)


def comb(master, **kw):
    """ttk.Combobox 工厂：圆角下拉框（Round.TCombobox，field 贴图 + 原生箭头）。"""
    if _style is not None and _ROUND_OK:
        return _ttk().Combobox(master, style="Round.TCombobox", **kw)
    return _ttk().Combobox(master, **kw)


def radio(master, **kw):
    """ttk.Radiobutton 工厂，与 button() 同一套降级规则。"""
    boot = kw.pop("bootstyle", None)
    if _ttb is not None and _style is not None:
        if boot:
            return _ttb.Radiobutton(master, bootstyle=boot, **kw)
        return _ttb.Radiobutton(master, **kw)
    return _ttk().Radiobutton(master, **kw)


def check(master, **kw):
    """ttk.Checkbutton 工厂：布尔选项统一成滑动开关（自建 Round.Toggle.* 样式）。

    动画 = 控件级切样式（点击后先切中间帧样式 Round.Toggle.Mid，TOG_ANIM_MS
    后切回终态套 Round.Toggle）——**不能用 style.map(image=…)**：样式级 map
    映射到的是 Checkbutton 的控件 -image 选项，选中态会额外渲染出一张轨道贴图
    （"第二个开关"）并挤掉文字（实测复现）；真正的轨道元素走 element 级固定
    状态表，样式级 map 根本作用不到它。
    原生 ttk.Checkbutton（不用 _ttb 子类）：显式 style= 原样保留，且绕开
    AutoStyleMixin 的一切动态改写（与按钮工厂同一条纪律）。
    变量 trace 兜底：程序性改值（不经点击）时同步回终态套；点击路径 Tk 的
    顺序是"变量写 → trace → command"，trace 先设终态、command 再设中间帧，
    顺序天然正确。「管理本地模型」表格行首的小勾选列不走这里、保留原生样式。
    """
    variable = kw.get("variable")
    cmd = kw.pop("command", None)
    if _TOG_IMGS and _style is not None:
        def wrapped(_cmd=cmd):
            _toggle_animate(sw)
            if _cmd:
                _cmd()
        kw["command"] = wrapped
        sw = _ttk().Checkbutton(master, style=TOG_STYLE, **kw)
        if variable is not None:
            def _sync(*_args, _sw=sw):
                try:
                    if _sw.winfo_exists():
                        _sw.configure(style=TOG_STYLE)
                except Exception:
                    pass
            try:
                variable.trace_add("write", _sync)
            except Exception:
                pass
        return sw
    if cmd:
        kw["command"] = cmd
    return _ttk().Checkbutton(master, **kw)


# 两套开关样式：TOG_STYLE 终态（element 级 selected→on）；Mid 中间帧（动画用）
TOG_STYLE = "Round.Toggle"
TOG_MID_STYLE = "Round.Toggle.Mid"
_TOG_IMGS = {}                # 帧 → 贴图（off/mid/on/d_off/d_on/r_off/r_on，全预生成缓存）


def _toggle_animate(sw):
    """切换的中间态过渡（W 定口径：极短、落定即回终态、不引入重排掉帧）。

    开销 = 一次控件 configure（切样式，微秒级）+ 一次 after；五帧贴图全部
    预生成进 Style 图片缓存，运行期零 PIL 调用、图片尺寸不变 → 零重排。
    连点幂等：再次进入就是重设中间帧样式 + 续排落定；控件销毁后 after 触发
    由 winfo_exists 兜住，不炸。
    """
    if _style is None:
        return
    try:
        sw.configure(style=TOG_MID_STYLE)

        def settle(_sw=sw):
            try:
                if _sw.winfo_exists():
                    _sw.configure(style=TOG_STYLE)
            except Exception:
                pass
        _style.master.after(TOG_ANIM_MS, settle)
    except Exception:
        pass


def _build_toggles(colors, bg):
    """构建两套滑动开关样式（apply 时调一次，随主题重画）。

    TOG_STYLE：element 级状态表（!selected→off 帧、selected→on 帧、disabled
    两帧）——终态渲染零切换开销。TOG_MID_STYLE：全部帧换成 mid（滑块居中、
    轨道过渡色），只在切换的 35ms 里挂上去。
    """
    from ttkbootstrap.constants import NSEW, LEFT
    from ttkbootstrap.style.layout import El, layout, image_element

    accent = colors.get("primary")
    border_c = colors.get("border")
    fg = colors.get("fg")
    dis_fg = colors.get("secondary")
    _TOG_IMGS.update({
        "off": _toggle_png(border_c, 0, False, bg),
        "mid": _toggle_png(_blend(border_c, accent, 0.5), 1, False, bg),
        "on": _toggle_png(accent, 2, False, bg),
        "d_off": _toggle_png(_blend(border_c, bg, 0.5), 0, True, bg),
        "d_on": _toggle_png(_blend(accent, bg, 0.65), 2, True, bg),
        # 键盘焦点帧（W：鼠标点击不留框、键盘导航保留）：fg 色 2px 内环。
        # 鼠标路径走 install_focus_policy 把焦点还给根窗 ⇒ 这两帧不出现
        "r_off": _toggle_png(border_c, 0, False, bg, ring=fg),
        "r_on": _toggle_png(accent, 2, False, bg, ring=fg),
    })
    for fam, default_img, sel_img in (
            (TOG_STYLE, _TOG_IMGS["off"], _TOG_IMGS["on"]),
            (TOG_MID_STYLE, _TOG_IMGS["mid"], _TOG_IMGS["mid"])):
        el = fam + ".track"
        # 状态顺序 = 优先序（Tk 取第一个命中的）：focus selected → focus →
        # disabled selected → disabled → selected；Mid 样式不配焦点帧（35ms
        # 中间态，不值得再来一张）
        if fam == TOG_STYLE:
            states = {"focus selected": _TOG_IMGS["r_on"],
                      "focus": _TOG_IMGS["r_off"],
                      "disabled selected": _TOG_IMGS["d_on"],
                      "disabled": _TOG_IMGS["d_off"],
                      "selected": sel_img}
        else:
            states = {"disabled selected": _TOG_IMGS["d_on"],
                      "disabled": _TOG_IMGS["d_off"],
                      "selected": sel_img}
        image_element(_style, el, default=default_img, states=states,
                      sticky="w")
        # 布局照抄 2.x toggle.py 的骨架（Checkbutton 引擎类是 Toolbutton.*）
        layout(_style, fam,
               El("Toolbutton.border", sticky=NSEW, children=[
                   El("Toolbutton.padding", sticky=NSEW, children=[
                       El(el, side=LEFT, sticky=""),
                       El("Toolbutton.focus", side=LEFT, sticky="", children=[
                           El("Toolbutton.label", side=LEFT, sticky="")])])]))
        _style.configure(fam, background=bg, foreground=fg,
                         borderwidth=0, relief="flat", padding=(2, 2),
                         focusthickness=0, focuscolor="")
        _style.map(fam, foreground=[("disabled", dis_fg)])


def _toggle_png(track, pos, disabled, bg, ring=None):
    """画一帧开关轨道（含滑块；底色不透明防毛边，同按钮贴图口径）。

    pos：0=滑块居左（off）、1=居中（mid 过渡帧）、2=居右（on）。
    ring：给非 None 时在胶囊内侧再描一圈（键盘焦点帧用）。
    """
    from PIL import Image, ImageDraw, ImageTk
    knob_c = "#ffffff" if not disabled else _blend("#ffffff", bg, 0.35)
    knob_edge = _blend("#ffffff", track, 0.3)
    key = ("llmtoggle", str(track), pos, disabled, bg, str(ring))

    def render():
        # 画布右侧多留 5px 底色当文字间距（不透明底，毛边口径同按钮）
        img = Image.new("RGBA", (TOG_W + 5, TOG_H), bg)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, TOG_W - 1, TOG_H - 1],
                            radius=TOG_H // 2, fill=track)
        if ring:
            # 键盘焦点环：胶囊内侧 1px 处描一圈（先画，滑块盖住重叠段更干净；
            # 鼠标路径走焦点策略、不会走到这帧）
            d.rounded_rectangle([1, 1, TOG_W - 2, TOG_H - 2],
                                radius=TOG_H // 2 - 1, outline=ring, width=2)
        y = (TOG_H - TOG_KNOB) // 2
        if pos == 1:
            x = (TOG_W - TOG_KNOB) // 2
        elif pos == 2:
            x = TOG_W - TOG_KNOB - y
        else:
            x = y
        d.ellipse([x, y, x + TOG_KNOB - 1, y + TOG_KNOB - 1],
                  fill=knob_c, outline=knob_edge, width=1)
        return ImageTk.PhotoImage(img)

    return _style._get_or_create_image(key, render)


def _build_fast_scroll():
    """自建滚动条样式：滑块 / 滑槽从**系统主题**（vista）复制原生元素，不用贴图元素。

    为什么（2026-10-08 实测，W 报"拖动窗口缩放明显卡顿"，见 `08` §10）：ttkbootstrap
    的滚动条滑块是**九宫格贴图元素**（PIL 现画 + 按目标尺寸做一次合成），单次重绘
    实测 4.5~13 ms；拖动窗口一次会触发 3 次以上重绘 ⇒ 主页面与设置页**每档拖拽
    多付 40~50 ms**（拖 20 档白花约 1 秒，肉眼就是卡顿）。换成原生元素后单次重绘
    **0.18 ms（约 25×）**，外观同样是中性灰的「细圆角滑块」（比原来的粗胶囊更清爽），
    滑槽颜色跟着页面底色 = 视觉上只有一条细滑块。

    两条实测纪律（踩过）：
      · **元素名必须是源主题里的真名**（vista 是 `Vertical.Scrollbar.thumb/trough`、
        clam 是 `thumb/trough`）。临时编个名字（如 `My.Thumb`）Tk 不报错，但造出来的是
        **画不出任何东西的空元素**（`element_options() == ()`），量出来的"变快了"
        是假象 —— 必须截图 / 数像素确认滑块真画出来了（坑 170）。
      · 用**自己的样式名**（`Native.*`），别覆盖 ttkbootstrap 的 `Round.*`：它的构建器
        在 Style 重建（多 root 的测试夹具）时会把自己的样式重新注册回去，覆盖我们的
        布局就白改了（自检里换过好几轮 root）。
      · **深色模式不建、也不用**（2026-10-09 W 报深色下滚动条仍是白色条）：vista
        原生滑块是浅色的，`_scroll_style_name` 在深色返回 "" → 走主题自带的
        round 变体（nord-dark 下是深色的）；深色下付的贴图税换正确性（同中性按钮）。
    """
    st = _style
    if st is None or _MODE != "light":
        return None
    for axis in ("Vertical", "Horizontal"):
        st.element_create("%s.Scrollbar.thumb" % axis, "from", "vista")
        st.element_create("%s.Scrollbar.trough" % axis, "from", "vista")
        name = "Native.%s.TScrollbar" % axis
        stick = "ns" if axis == "Vertical" else "we"
        st.layout(name, [("%s.Scrollbar.trough" % axis, {"sticky": stick, "children": [
            ("%s.Scrollbar.thumb" % axis, {"expand": "1", "sticky": stick})]})])
        st.configure(name, troughcolor=c("bg"), borderwidth=0, arrowsize=0)
        _SCROLL_STYLES[axis.lower()] = name
    return _SCROLL_STYLES


def _scroll_style_name(orient):
    """当前模式该用的滚动条样式名：浅色 = 自建 Native.*，深色 = ""（主题自带 round）。"""
    key = "horizontal" if str(orient or "").lower().startswith("h") else "vertical"
    if _MODE == "light" and _SCROLL_STYLES.get(key):
        return _SCROLL_STYLES[key]
    return ""


def restyle_scrollbars(root_widget):
    """主题切换后把**已建出**的滚动条换到当前模式的样式（新建的走 scroll() 工厂自带）。

    没有这一步，切深色后聊天区 / 设置页那几条 `Native.*` 原生滚动条会保持浅色
    （2026-10-09 W 报深色下"右边一条白条"）。
    """
    stack = [root_widget]
    while stack:
        w = stack.pop()
        try:
            if w.winfo_class() == "TScrollbar":
                w.configure(style=_scroll_style_name(w.cget("orient")))
        except Exception:
            pass
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass


def scroll(master, **kw):
    """ttk.Scrollbar 工厂：浅色 = 自建「原生元素」样式（见 `_build_fast_scroll`），
    深色 = 主题自带 round 变体（深色正确；贴图税换正确性，同中性按钮）。

    建不出样式时回落：有 ttkbootstrap 用它的 round 变体，否则裸原生 ttk。
    """
    kw.pop("bootstyle", None)          # 兼容既有调用；自建样式不吃 bootstyle
    name = _scroll_style_name(kw.get("orient"))
    if name:
        return _ttk().Scrollbar(master, style=name, **kw)
    if _ttb is not None and _style is not None:
        return _ttb.Scrollbar(master, bootstyle="round", **kw)
    return _ttk().Scrollbar(master, **kw)


def _ttk():
    from tkinter import ttk
    return ttk


# ---- 圆角按钮样式（PIL 贴图九宫格）----

def _blend(hex1, hex2, t):
    """两个 #rrggbb 按 t 混合（t=0 取前者）。贴图四态的色阶来源。"""
    a = [int(hex1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _luma(hexcolor):
    """0~1 相对亮度：决定实心按钮上的文字用白还是黑。"""
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _round_png(fill, outline, outline_w, w=BTN_MIN_W):
    """渲染一张 w×BTN_H 物理像素的圆角矩形 → tk.PhotoImage。

    底色画**不透明的页面底色**而不是透明：ttk 对贴图边缘半透明像素的合成
    会出白色毛边（实测），四角直接用 bg 色与容器无缝。键含全部颜色输入，
    换主题底色变化时自动重画。
    """
    bg = c("bg")
    key = ("llmtheme.round", w, BTN_H, BTN_RADIUS,
           str(fill), str(outline), outline_w, bg)
    return _style._get_or_create_image(key, _make_render(fill, outline, outline_w, bg, w))


def _make_render(fill, outline, outline_w, bg, w=BTN_MIN_W):
    from PIL import Image, ImageDraw, ImageTk

    def render():
        img = Image.new("RGBA", (w, BTN_H), bg)
        d = ImageDraw.Draw(img)
        box = [outline_w // 2, outline_w // 2, w - 1 - outline_w // 2,
               BTN_H - 1 - outline_w // 2]
        if fill:
            d.rounded_rectangle(box, radius=BTN_RADIUS, fill=fill)
        if outline:
            d.rounded_rectangle(box, radius=BTN_RADIUS, outline=outline,
                                width=outline_w)
        # 必须 ImageTk.PhotoImage：Style 的图片缓存读 `__photo.name`（PIL 结构）；
        # Python 3.14 的 tkinter.PhotoImage 内部字段已改名，喂给它会在缓存登记
        # 时 AttributeError（实测）。
        return ImageTk.PhotoImage(img)

    return render


def _build_native_flat(ttk_style, colors, bg):
    """中性描边按钮换 vista 原生按钮元素（2026-10-08，W 定「只换中性按钮」）。

    ttk 的 image 元素**每档窗口缩放都要按目标尺寸重合成整幅贴图**，且与源图
    几何无关——按宽度预切、拆三件套、恒等尺寸全部实测省不掉（坑 173：
    三件套反而慢 7 倍，已回滚）。真加速只有换原生 OS 元素：同窗拖动
    -56~67 ms/档（`08` §10.6）。中性按钮是数量大头（设置页几乎全部），
    它们换原生；彩色 / 实心按钮数量少、税小，保留圆角贴图（发送的蓝色
    主操作、取回的描边强调不丢）。输入框 / 下拉 / 开关与图标按钮照旧
    （开关与图标是固定尺寸贴图，从不缩放，本就无税）。
    外观：方角系统按钮 + 系统悬停态。padding = (BTN_PAD_X-4, 2, BTN_PAD_X-4, 2)
    —— vista 的 Button.button 自带 ~11px/侧内边距，实测 4 字按钮 78×30（圆角版
    ~86×34）；叠 10px/侧样式 padding 后 ~98×34，比圆角版略宽、同高（2026-10-08
    W："清空对话等按钮过于窄小，保持与其他按钮风格一致"；顶栏三个按钮共 +60px，
    1080 默认宽的顶栏余量还剩 ~96px）。padding 必须由布局里的 Button.padding
    元素消费（见下）。复制不到 vista 元素时抛错，调用方落回圆角贴图路径。
    """
    from ttkbootstrap.constants import NSEW
    from ttkbootstrap.style.layout import El, layout
    _style.element_create("Native.Flat.button", "from", "vista", "Button.button")
    _style.element_create("Native.Flat.label", "from", "vista", "Button.label")
    # padding 必须由 padding 元素消费：vista 的 -padding 归 Button.padding 管，
    # 布局直接 button→label 会把样式 padding 整个跳过（实测按钮纹丝不动）
    _style.element_create("Native.Flat.padding", "from", "vista", "Button.padding")
    layout(_style, ttk_style,
           El("Native.Flat.button", sticky=NSEW, children=[
               El("Native.Flat.padding", sticky=NSEW, children=[
                   El("Native.Flat.label", sticky=NSEW)])]))
    _style.configure(ttk_style, anchor="center",
                     padding=(BTN_PAD_X - 4, 2, BTN_PAD_X - 4, 2),
                     foreground=colors.get("fg"), background=bg,
                     borderwidth=0, relief="flat",
                     focusthickness=0, focuscolor="")
    _style.map(ttk_style, foreground=[("disabled", colors.get("secondary"))])


def _build_round_buttons():
    """构建 Round.* 圆角按钮样式（apply 时调一次）。

    五个变体贴齐主页面用途：实心（primary）一个 + 描边四个。贴图四态
    （normal / active / pressed / disabled）颜色全部取自当前主题。
    """
    from ttkbootstrap.constants import NSEW, RIGHT
    from ttkbootstrap.style.layout import El, layout, image_element

    colors = _style.colors
    bg = colors.get("bg")

    # ---- 描边（中性 + 彩色）变体 ----
    # 中性（Round.Secondary，也是无 bootstyle 按钮的默认）：黑字灰边 —— W 定的
    # 顶栏配色规范（可点击=黑、禁用=灰），设置页等其余默认按钮同此规范。
    # hover/pressed 用主题 active 色阶（灰底浮起），四态区分清晰。
    variants = [
        ("Round.TButton", "primary", True),
        ("Round.Primary.TButton", "primary", False),
        ("Round.Danger.TButton", "danger", False),
        ("Round.Success.TButton", "success", False),
        ("Round.Secondary.TButton", None, False),       # None = 中性黑字
        ("Round.Icon.TButton", None, False),            # 中性 + 方形小几何（图标按钮）
    ]
    for ttk_style, colorkey, solid in variants:
        icon_geom = ttk_style == "Round.Icon.TButton"
        if ttk_style == "Round.Secondary.TButton":
            # 中性按钮 = 数量大头（设置页几乎全部），换 vista 原生按钮元素
            # 免掉贴图税（W 2026-10-08 定「只换中性按钮」；机制与实测见
            # `_build_native_flat` 与 `08` §10.6）。复制不到 vista 元素就
            # 落回下面的圆角贴图路径，绝不拖垮启动。
            # ⚠ 深色模式**不许**走原生元素（2026-10-09 W 报「按钮白底白字」）：
            # vista 原生按钮跟随 OS 浅色渲染，而样式 foreground 取的是深色主题的
            # 浅色文字 → 浅底浅字。深色一律走下面那支深色贴图（文字色、四态全部
            # 按当前主题画；代价是深色付回贴图税 —— 正确性优先）。
            if _MODE == "light":
                try:
                    _build_native_flat(ttk_style, colors, bg)
                    continue
                except Exception:
                    pass
        if solid:
            accent = colors.get(colorkey)
            hov = _blend(accent, "#ffffff", 0.12)
            prs = _blend(accent, "#000000", 0.14)
            dis = _blend(accent, bg, 0.5)
            imgs = {"default": _round_png(accent, None, 0),
                    "active": _round_png(hov, None, 0),
                    "pressed": _round_png(prs, None, 0),
                    "disabled": _round_png(dis, None, 0)}
            fg = "#ffffff" if _luma(accent) < 0.62 else "#1a1a1a"
            dis_fg = fg
            # 键盘焦点帧：实心底 + 深一档的同色环
            imgs["focus"] = _round_png(accent, _blend(accent, "#000000", 0.5), 2)
        elif colorkey is None and not icon_geom:
            # 中性四态：黑字 + 灰边白底 → 悬停浅灰底 → 按下更深 → 禁用灰字淡边
            # （Colors 的原生键是 fg/secondary —— body/muted 是语义层名字）
            accent = colors.get("border")
            fg = colors.get("fg")
            dis_fg = colors.get("secondary")
            hov = colors.get("active")
            prs = _blend(colors.get("active"), accent, 0.55)   # 比 hover 深一档
            w1 = max(2, round(BTN_RADIUS / 3))
            imgs = {"default": _round_png(None, accent, w1),
                    "active": _round_png(hov, accent, w1),
                    "pressed": _round_png(prs, accent, w1),
                    "disabled": _round_png(None, _blend(accent, bg, 0.55), w1)}
            # 键盘焦点帧：灰边换主色（一眼区分"键盘走到了这里"）
            imgs["focus"] = _round_png(None, colors.get("primary"), w1)
        elif colorkey is None and icon_geom:
            # 图标按钮（设置齿轮）：无外框（W 定）——常态就是干净的图标，
            # 悬停 / 按下才给灰底反馈，禁用与常态同（图标色随文字色变灰即可）
            accent = colors.get("border")
            fg = colors.get("fg")
            dis_fg = colors.get("secondary")
            hov = colors.get("active")
            prs = _blend(colors.get("active"), accent, 0.55)
            tw = BTN_ICON_W
            imgs = {"default": _round_png(None, None, 0, w=tw),
                    "active": _round_png(hov, None, 0, w=tw),
                    "pressed": _round_png(prs, None, 0, w=tw),
                    "disabled": _round_png(None, None, 0, w=tw)}
            # 键盘焦点帧：平时无框的按钮，键盘走到时才显示主色框
            imgs["focus"] = _round_png(None, colors.get("primary"), 2, w=tw)
        else:
            # 描边彩色：底透明，悬停浮起 = accent 12% / 按下 24% 叠在底色上
            accent = colors.get(colorkey)
            hov = _blend(accent, bg, 0.88)
            prs = _blend(accent, bg, 0.76)
            dis_fg = _blend(accent, bg, 0.55)
            w1 = max(2, round(BTN_RADIUS / 3))
            imgs = {"default": _round_png(None, accent, w1),
                    "active": _round_png(hov, accent, w1),
                    "pressed": _round_png(prs, accent, w1),
                    "disabled": _round_png(None, dis_fg, w1)}
            fg = accent
            # 键盘焦点帧：同色边框加粗一档
            imgs["focus"] = _round_png(None, accent, w1 + 2)
        el = ttk_style + ".btn"
        # 状态顺序 = 优先序（Tk 取第一个命中）：pressed → focus → disabled →
        # active。九宫格 border 四边都给圆角半径（W 报"发送按钮缺角"的根因：
        # 此前上下给 0，按钮垂直拉伸时圆角上下弧被整段拉长变形 → 缺角）。
        # focus 帧只在键盘导航时命中（鼠标路径由 install_focus_policy 把焦点
        # 还给根窗，见那里的说明）
        image_element(_style, el,
                      default=imgs["default"],
                      states={"pressed": imgs["pressed"],
                              "focus": imgs["focus"],
                              "disabled": imgs["disabled"],
                              "active": imgs["active"]},
                      border=(BTN_RADIUS, BTN_RADIUS, BTN_RADIUS, BTN_RADIUS),
                      padding=(BTN_PAD_X if not icon_geom else BTN_ICON_PAD_X, 0,
                               BTN_PAD_X if not icon_geom else BTN_ICON_PAD_X, 0),
                      sticky=NSEW)
        layout(_style, ttk_style,
               El(el, sticky=NSEW, children=[
                   El("Button.focus", sticky=NSEW, children=[
                       El("Button.label", sticky=NSEW)])]))
        # focusthickness=0 + focuscolor 空：点击后不留焦点虚框（W 定；顶栏尤其
        # 明显）。两个都清——focusthickness 控制宽度，focuscolor 空串保证即便
        # 有厚度也不画色
        _style.configure(ttk_style, anchor="center", padding=0,
                         foreground=fg, background=bg, borderwidth=0,
                         relief="flat", focusthickness=0, focuscolor="")
        _style.map(ttk_style, foreground=[("disabled", dis_fg)])
    _build_toggles(colors, bg)

    # ---- 圆角输入框 / 下拉框（field 贴图 + 原生 textarea / 箭头元素）----
    # 贴图同按钮几何；九宫格四边 border=圆角半径 → 行高被拉高时中段拉伸、
    # 圆角不变形。focus 态边框换主色加粗（输入定位一眼可见）。
    border_c = colors.get("border")
    input_fg = colors.get("inputfg")
    for ttk_style, is_combo in (("Round.TEntry", False), ("Round.TCombobox", True)):
        pad = (scale_x(8), scale_x(4), scale_x(22) if is_combo else scale_x(8),
               scale_x(4))
        el = ttk_style + ".field"
        image_element(
            _style, el,
            default=_round_png(None, border_c, 1),
            states={"focus": _round_png(None, colors.get("primary"), 2),
                    "disabled": _round_png(None, _blend(border_c, bg, 0.5), 1)},
            border=(BTN_RADIUS, BTN_RADIUS, BTN_RADIUS, BTN_RADIUS),
            padding=pad, sticky=NSEW)
        children = [El("textarea", sticky=NSEW)]
        if is_combo:
            # clam 引擎的原生箭头元素，保留系统下拉手感。
            # sticky 必须显式给空串：El 的默认值是 nswe，箭头贴图会被拉伸变形
            children.append(El("Combobox.downarrow", side=RIGHT, sticky=""))
        layout(_style, ttk_style, El(el, sticky=NSEW, children=children))
        _style.configure(ttk_style, borderwidth=0, relief="flat",
                         foreground=input_fg, insertbackground=input_fg)
        _style.map(ttk_style,
                   foreground=[("disabled", _blend(input_fg, bg, 0.5))])
    global _ROUND_OK
    _ROUND_OK = True


def scale_x(v):
    """逻辑值 → 主题缩放系物理值（builder.scale_size 包装；2.x 自管缩放系数）。"""
    if _style is None:
        return v
    try:
        return _style._theme_objects[_style.theme.name].scale_size(v)
    except Exception:
        return v


def _ensure_widget_skin():
    """显式构建"默认样式"控件的皮肤 —— 2.x 的素材类皮肤是懒构建的（W 报的复选框问题）。

    根因（实测钉死）：ttkbootstrap 2.x 里 Checkbutton 这类**素材型**样式的构建由它自己的
    控件子类（BootMixin）触发；而我们为绕开 BootMixin 覆盖显式 style= 的问题，表格里的
    复选框用的是**原生 `ttk.Checkbutton`**（`style` 留空 → 默认 TCheckbutton）——懒构建
    从未触发，`layout("TCheckbutton")` 一直是 clam 原生版：**10px 的小空框 + clam 的勾
    在 2 倍缩放下看着像"✗"**（W 的原话："比现在要大一点的蓝色的打勾的而不是打叉的"）。
    手工调一次它自己的 builder 就会换成 ttkbootstrap 的皮肤（蓝底白勾、18 逻辑 px），
    与"直接用 ttkbootstrap 的复选框"完全一致 —— 所以不另造素材，显式触发即可。
    ⚠ 私有模块路径，锁 2.2.3（CI 有 TTKBOOTSTRAP_VERSION）；触发失败就退回 clam 原生
    （不好看但可用），绝不拖垮启动。
    范围：只处理 W 点名的复选框（表格勾选列）。同类里单选钮（Radiobutton）也落在 clam
    原生渲染上（主页面「带图方式」那三个），W 未要求、暂不动 —— 要统一的话在同一处
    加一行 `build_radiobutton_style(builder, "")` 即可。
    """
    from ttkbootstrap.style.builders.checkbutton import build_checkbutton_style
    builder = _style._theme_objects[_style.theme.name]
    build_checkbutton_style(builder, "")


def _compact_entries():
    """把 ttk 输入类（设置页大量 Entry / Combobox / Spinbox）压回紧凑尺寸。

    configure 写进 Style 的 user_options 层，每次样式构建后自动重放
    （含切主题后的重建），不会被 builder 的默认值覆盖。
    """
    try:
        scale = _style._theme_objects[_style.theme.name].scale_size
        pad = tuple(scale(v) for v in ENTRY_PAD_LOGICAL)
    except Exception:
        pad = ENTRY_PAD_LOGICAL
    _style.configure("TEntry", padding=pad)
    _style.configure("TCombobox", padding=pad)
    _style.configure("TSpinbox", padding=pad)
