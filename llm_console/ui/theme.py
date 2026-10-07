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
THEME_NAME = "cosmo"

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
_ROUND_OK = False             # 圆角样式族构建成功（工厂据此决定套样式还是裸原生控件）
try:
    from ttkbootstrap.style import Style as _TBStyle
    import ttkbootstrap as _ttb
except Exception:             # 未安装：整层降级，绝不拖垮启动
    _TBStyle = None
    _ttb = None


def apply(root):
    """在 main() 建 root 之后、App 构造之前调用一次：把主题应用到整个窗口。

    ttkbootstrap 2.x 的 Style 是 ttk.Style 子类，无参 super().__init__() 会绑定
    当时的默认 root —— 所以必须在 root 建好之后调。同时把根窗底色对齐主题，
    免得深色主题下控件缝隙露出系统白底。
    返回 Style（或降级时 None）；Style 挂在模块级，取色走 c()。
    """
    global _style
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
        _style = _TBStyle(theme=THEME_NAME)
    except Exception:         # 主题名写错 / 初始化失败：退回原生外观，不让启动炸掉
        _style = None
        return None
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
    """主页面取主题色的唯一出口。key 见 _SEMANTIC；无 ttkbootstrap 返回原值。"""
    tb_key, fallback = _SEMANTIC[key]
    if _style is None:
        return fallback
    try:
        return _style.colors.get(tb_key)
    except Exception:         # 色键在新主题缺失：宁可退原值也不停摆
        return fallback


def text_kw(fallback_bg=None):
    """tk 原生文本控件（聊天区 / 输入框）的主题配色参数。

    返回 dict 直接 ** 展开进构造参数：有主题时给 background / foreground /
    insertbackground（插入光标，深色主题下不配上等于看不见光标）与选区色；
    无主题时给 {}（或仅 fallback_bg，供原来就显式白底的聊天区逐位还原）。
    """
    if _style is None:
        return {"background": fallback_bg} if fallback_bg else {}
    colors = _style.colors
    return {"background": _safe(colors, "inputbg", c("bg")),
            "foreground": _safe(colors, "inputfg", c("body")),
            "insertbackground": c("body"),
            "selectbackground": _safe(colors, "selectbg", c("accent")),
            "selectforeground": _safe(colors, "selectfg", "#ffffff")}


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


def scroll(master, **kw):
    """ttk.Scrollbar 工厂：圆角胶囊样式（2.x 内置 round 变体）。"""
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
