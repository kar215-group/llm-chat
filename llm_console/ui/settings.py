# -*- coding: utf-8 -*-
"""llm_console.ui.settings — 界面 Mixin：设置窗口（v39：左侧可折叠导航 + 右侧滚动区块，
按页懒加载、同页区块一起建；字段说明走 widgets.HelpDot 悬停；底部「保存」统一跑当前页各区块的钩子）"""


import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

from .. import texts
from ..core import (capability, codesign, cloudjobs, engine_install, hardware,
                    localmodels, media, providers, sdprofile, secrets, textfile, updater)
from ..core.config import (APP_DIR, APP_VERSION, CFG_VERSION, DEFAULT_CONFIG,
                           FLOAT_KEYS, INT_KEYS, STR_KEYS, cloud_media_dir,
                           gen_api_key, save_config)
from ..core.models import (chat_start_block, has_local_chat, scan_models,
                           scan_video_models, usable_local)
from ..core.params import ctx_for, current_ngl
from ..core.server import _query_serving_model, server_alive, server_process_alive
from ..connection import cloud
from ..connection import cloud_media
from ..connection import remote as remote_mod
from . import dialogs
from . import theme
from . import widgets
from .service import START_BLOCK_TIP

# 分辨率写法警示（坑 183，W 2026-10-09 定）：不合格值**不拦发送**（发送侧照旧回退
# 默认档 / 不送该参数），红字只负责把「静默回退」变成看得见。文案是 W 原话
# 「写法不合格，请填入"X*Y"格式」按 hint 位宽度精简的版本：
#   · 宽高语义的框（本地生图 / 生视频、云端生图）用带格式提示的长文案；
#   · 云端「生图 / 生视频」那屏是左右两列（坑 113），红字比常态 hint 宽会**把右列
#     整体右推** —— 实测量过（2026-10-09）：左列长文案 +117px、那屏列尾只剩 74px
#     预算，两处同时亮长文案会出界 22px。所以那屏两个框都收短成「写法不合格」；
#   · 云端视频分辨率的框（两处）也用短文案：档位标签（720P）是合法写法，
#     "填 X*Y" 的提示对它本就半对半错，示例留在该行 "?" 气泡里。
SIZE_WARN = texts.SET_SIZE_WARN
SIZE_WARN_RES = texts.SET_SIZE_WARN_RES


def _vidres_allow(t):
    """云端视频分辨率的档位写法放行：720P / 1080P / 2K 这类标签与自由文本照旧交
    发送侧（认不出就不送，见 cloud_media.video_body）；留空合法；纯数字（含全角，
    多半是漏了分隔）与「带了宽高分隔符却解析不出」才算写法不合格。"""
    t = str(t).strip()
    if not t:
        return True
    if t.isdigit():                     # isdigit 认全角数字：768 / ７６８ 都算没写完整
        return False
    return not any(ch in "xXｘＸ*＊×，," for ch in t)


# ---------------------------------------------------------------------------
# 左栏导航（v39）：组 → 子组 → 叶子，叶子指向"哪个页面的哪个区块"。
#
# 为什么叶子比页面多：本地文本模型的「生成参数」和「服务参数」其实**在同一页里**
# （两者都是 llama-server 的事，分两页只是来回跳），但服务参数那一段很长，
# 左栏单列一项点过去就直接定位过去 —— 这也是给"以后配置项更多"留的位置。
# 反过来也允许：一项管两段（「模型文件与引擎」= 同页的 files + eng），靠
# widgets.SideNav 的 `nav_hide` 让后一段不在左栏成行，但区块仍被建、jump 仍定位得到。
# title / help 是给页面用的：title 是区块大标题，help 是标题旁 "?" 里的那段说明。
# ---------------------------------------------------------------------------
NAV_SPEC = [
    {"key": "g_local", "label": "本地模型", "children": [
        {"key": "g_ltext", "label": "文本模型", "children": [
            {"key": "gen", "label": "生成参数", "page": "local_text", "section": "gen",
             "title": "生成参数（保存即生效）",
             "help": "这一页改的是采样参数，下一次请求就生效，不用重启服务。\n"
                     "思考型模型的「思考过程」和「正式回答」共用 max_tokens 那一份额度，"
                     "思考写得长就会挤掉回答。"},
            {"key": "svc", "label": "服务参数", "page": "local_text", "section": "svc",
             "title": "服务参数（保存后重启生效）",
             "help": "这些是 llama-server 的启动参数，改完必须重启服务才生效。\n"
                     "点底部「保存」时，如果服务正在运行，会问你要不要立即重启。\n"
                     "GPU 层数（ngl）与上下文（ctx）都是**按模型分别记忆**的：切换模型时"
                     "自动带上各自的值，这里改的是当前选中模型的那一份。"},
        ]},
        {"key": "g_lmedia", "label": "图像与视频模型", "children": [
            {"key": "img", "label": "生图（sd.cpp）", "page": "local_media", "section": "img",
             "title": "生图（sd.cpp）",
             "help": "本地生图走 sd.cpp 引擎：sd-cli 按需拉起、进程退出就释放显存，所以每次"
                     "生成都要重载一次权重。\n图生图要哪些配套件由模型族决定（Qwen-Image 要"
                     "视觉投影器 mmproj，FLUX 用 Kontext 变体），缺件在发送前就会点名说缺什么。"},
            {"key": "vid", "label": "生视频（sd.cpp）", "page": "local_media", "section": "vid",
             "title": "生视频（sd.cpp）",
             "help": "本地生视频与生图**共用同一个 sd-cli.exe**，只是多了视频参数。\n"
                     "一般要三件套权重：扩散主体、文本编码器、视频 VAE，**VAE 最容易漏下**，"
                     "缺任一项在发送前就会点名说缺什么，不会让你白排队。\n"
                     "出片耗时取决于显卡与档位，生成中可随时点「停止生成」。"},
        ]},
        {"key": "files", "label": "模型文件与引擎", "page": "files", "section": "files",
         "title": "模型文件管理",
         "help": "扫描模型目录、补全每个模型缺的层数 / 上下文 / 视觉投影器记录，"
                 "以及把散落的模型文件整理成「一个模型一个文件夹」。\n"
                 "进这一页会自动补全一次（10 分钟内不重复）；缺归属的文件可用"
                 "「模型来源…」把文件夹或单个 .gguf 加进来。\n"
                 "整理是**先预览、后执行，只移动不删除**；判不出归属的文件保持原位。"},
        # 「模型文件管理」与「引擎管理」本来就同一页，左栏合成一项（W 2026-10-02：两项
        # 各自一行是重复劳动）。`nav_hide` 让它不在左栏成行，但区块照旧建、jump="eng"
        # 照旧滚得到（输出栏「去配置引擎」用的就是它），高亮记在「模型文件与引擎」那行。
        {"key": "eng", "label": "引擎管理", "page": "files", "section": "eng",
         "nav_hide": "files",
         "title": "引擎管理",
         "help": "这一屏解决「还没装引擎」：说清现在缺哪一个、可以选哪几档、"
                 "点一下装到程序目录的 engines 里。\n"
                 "引擎已在自己机器上、只是换了位置：进这一屏会自动扫一遍软件所在文件夹，"
                 "也可以用「手动定向」自己指 —— 两个引擎与模型文件同处一个目录也认得出。\n"
                 "CUDA 档要连运行库一起下（几百 MB），所以下之前会先说清多大；"
                 "装完会自动指到刚装的那份。\n"
                 "「检查更新」联网查可用版本，查到新版本它自己变成「更新引擎」，"
                 "装的过程中可点「取消安装」；网络不通下不动时还有「复制下载链接」"
                 "（给的是选中档位的安装包直链）。"},
    ]},
    {"key": "g_cloud", "label": "云端模型", "children": [
        {"key": "c_prov", "label": "服务商与密钥", "page": "cloud_p", "section": "prov",
         "title": "服务商、密钥与模型清单",
         "help": "填连接信息 → 填密钥 → 测试连接 → 在「选择模型」里勾选要用的模型。\n"
                 "清单拉过一次就缓存；密钥只存本机 secrets.json。"},
        {"key": "c_text", "label": "文本模型", "page": "cloud_t", "section": "ctext",
         "title": "云端文本模型",
         "help": "云端文本走 OpenAI 兼容协议；这里的设置只影响文本对话。\n"
                 "附件每轮都重发，所以有预算上限。"},
        {"key": "c_media", "label": "生图 / 生视频", "page": "cloud_m", "section": "cmedia",
         "title": "云端生图 / 生视频",
         "help": "云端生图（同步，可挂参考图）/ 生视频（异步任务），走服务商原生接口，"
                 "不启动本地引擎。\n"
                 "产物拿到就下载落地（云端地址会过期）；费用单价按模型填。"},
    ]},
    {"key": "api", "label": "本地模型 API", "page": "api", "section": "api",
     "title": "本地模型 API（给 agent 或其他软件调用）",
     "help": "把**当前选中的本地模型**变成一个 OpenAI 兼容的本机地址，别的软件\n"
             "（agent、脚本、外部工具）填这个地址就能直接用它。\n"
             "只转本地模型：云端对话在应用内直连服务商，生图 / 生视频也不经这里。\n"
             "要有本地文本模型才开得起 —— 没有时这一项默认关闭。"},
    {"key": "remote", "label": "远程访问", "page": "remote", "section": "remote",
     "title": "远程访问（手机浏览器）",
     "help": "把本机变成同一 Wi-Fi 里手机浏览器能访问的小站：手机打开这里显示的"
             "地址、输入 6 位配对码，电脑端确认后即可使用。\n"
             "只有配对过的设备能连（电脑端随时可吊销）；传输走自签 HTTPS 证书，"
             "浏览器首次访问会提示不安全，点「继续访问」即可，身份验证靠配对码。\n"
             "一期只开放本地生图；开启前请先在「通用」页了解「允许锁屏运行」。"},
    {"key": "general", "label": "通用", "page": "general", "section": "general",
     "title": "通用",
     "help": "提示词优化（输入框右上角的星星）用哪个文本模型三路重写。\n"
             "加入第一个文本模型时会自动选上，之后以你的选择为准；本地模型经本机"
             "服务转发（当前已加载的那个），云端文本模型逐个可选。"},
    {"key": "appearance", "label": "外观", "page": "appearance", "section": "appearance",
     "title": "外观",
     "help": "界面主题（跟随系统 / 浅色 / 深色）与颜色的入口。\n"
             "主题切换在后续版本生效，当前先记住选择。「全局主题色」管发送按钮、"
             "取回、模型名、状态灯与左栏选中项；「选中文本强调色」只管聊天里拖蓝"
             "的部分 —— 两者互相独立，改完选中文本立刻变、按钮下次启动生效。"},
    {"key": "about", "label": "关于与诊断", "page": "about", "section": "about",
     "title": "关于与诊断",
     "help": "这一页认亲：这是什么软件、什么版本、怎么重看新手引导。\n"
             "「诊断」按钮开次级页面，那里回答另一件事：我这份是哪来的（下载的 exe 还是"
             "源码跑的）、这台机器上缺什么 —— 运行方式、程序与配置与密钥的位置、运行库、"
             "错误日志、一键诊断都在那一页。\n"
             "一键诊断只读本地信息：不联网、不启动推理引擎、不碰显卡。"
             "结果可以复制成一段文字贴给别人求助，也可以存成文件。"},
]


# ---------------------------------------------------------------------------
# 普通用户模式的导航（2026-10-07 W 定：新增"普通用户模式"并作为**默认模式**，
# 上面那份 NAV_SPEC 原样保留成"高级用户模式"）。同一批区块的精简版：
#   · 页更少更平：本地文本不再分「生成参数 / 服务参数」两页；本地生图与生视频
#     **合成一项「本地生图/视频」**（一页两块，vid 走 nav_hide）；云端按 W 第二轮
#     拆成三页 —— 「服务商与密钥」/「云端文本模型」/「云端生图/视频」（c_img +
#     c_vid 一页两块，c_vid 走 nav_hide；**高级模式同样拆三页**，页 id cloud_p /
#     cloud_t / cloud_m，但高级的导航名不改）；「本地模型 API」整页不出现
#     （那是高级能力，默认关，只在高级模式手动开）。
#   · 每页只留最常用的几件（清单是 W 逐页定的，见各区块构建函数的 simple 分支）。
#   · 全页不放 "?" 气泡：help 一律空串（HelpDot 对空文案本来就不建控件），
#     区块构建里散落的 HelpDot / 灰色小字也按 simple 分支裁掉。
# 叶子 key 与高级模式尽量同名（gen/img/vid/files/eng/c_prov/c_text/about）：
# 底部「用户模式」切换按钮重建窗口时按 key 落到对应页；c_img / c_vid 是普通模式
# 专有的两个区块（对应高级模式 cmedia 的左右两列）。
# nav_hide 与高级模式同一套语义（坑 142：不成行但仍在规格里、仍被建）。
# ---------------------------------------------------------------------------
SIMPLE_NAV_SPEC = [
    {"key": "g_local", "label": "本地模型", "children": [
        {"key": "gen", "label": "本地文本模型", "page": "local_text", "section": "gen",
         "title": "本地文本模型", "help": ""},
        {"key": "img", "label": "本地生图/视频", "page": "local_media", "section": "img",
         "title": "本地生图", "help": ""},
        # 生图与生视频在普通模式**合成一项**（W 2026-10-07 第二轮）：与「模型文件与
        # 引擎」同一套 nav_hide 语义 —— vid 不成行但仍在规格里、区块照建（同页两块），
        # 高亮记在替身行上（坑 142）
        {"key": "vid", "label": "本地生视频", "page": "local_media", "section": "vid",
         "nav_hide": "img",
         "title": "本地生视频", "help": ""},
        {"key": "files", "label": "模型文件与引擎", "page": "files", "section": "files",
         "title": "模型文件与引擎", "help": ""},
        {"key": "eng", "label": "引擎管理", "page": "files", "section": "eng",
         "nav_hide": "files",
         "title": "引擎管理", "help": ""},
    ]},
    {"key": "g_cloud", "label": "云端模型", "children": [
        {"key": "c_prov", "label": "服务商与密钥", "page": "cloud_p", "section": "prov",
         "title": "服务商与密钥", "help": ""},
        {"key": "c_text", "label": "云端文本模型", "page": "cloud_t", "section": "ctext",
         "title": "云端文本模型", "help": ""},
        # 云端生图与生视频在普通模式也**合成一项**（W 2026-10-07 第二轮）：一页两块，
        # 各自的「成本估算」按钮仍在各自块里；c_vid 走 nav_hide 不成行但照建（坑 142）
        {"key": "c_img", "label": "云端生图/视频", "page": "cloud_m", "section": "cimg",
         "title": "云端生图", "help": ""},
        {"key": "c_vid", "label": "云端生视频", "page": "cloud_m", "section": "cvid",
         "nav_hide": "c_img",
         "title": "云端生视频", "help": ""},
    ]},
    {"key": "remote", "label": "远程访问", "page": "remote", "section": "remote",
     "title": "远程访问（手机浏览器）", "help": ""},
    {"key": "general", "label": "通用", "page": "general", "section": "general",
     "title": "通用", "help": ""},
    {"key": "appearance", "label": "外观", "page": "appearance", "section": "appearance",
     "title": "外观", "help": ""},
    {"key": "about", "label": "关于与诊断", "page": "about", "section": "about",
     "title": "关于与诊断", "help": ""},
]


# v1.1.1 发版临时屏蔽（W 2026-10-09）：外观页的「界面主题」下拉不建，帮助文案
# 同步换成短版（不留着指向一个不存在的控件）；theme.RELEASE_LIGHT_ONLY 改回
# False 即恢复原文案与下拉（改动只有上面那一处开关与这里的 if）。
# ⚠ 只改高级 NAV_SPEC：普通模式的 help 必须保持空串 —— 它靠"空文案不建 HelpDot"
# 保证全页无 "?"，填了非空会凭空冒出一个气泡（本套件当场抓到）。
if theme.RELEASE_LIGHT_ONLY:
    for _it in NAV_SPEC:
        if _it.get("key") == "appearance":
            _it["help"] = ("界面颜色的入口：「全局主题色」管发送按钮、取回、"
                           "模型名、状态灯与左栏选中项；「选中文本强调色」只管"
                           "聊天里拖蓝的部分 —— 两者互相独立，改完选中文本立刻变、"
                           "按钮下次启动生效。")


def _ask_color(parent, title, initial, cust):
    """Windows 标准选色对话框，**自带可持久化的「自定义颜色」色板**。

    tkinter 自带的 colorchooser 把对话框左侧那 16 格「自定义颜色」藏在 Tk
    进程内部：同一进程里一直都在，但进程一退出就丢 —— 重启软件后上次添加的
    全没了（W 2026-10-08 报的"添加到自定义颜色不能保存"）。根因是那份数组的
    归属：Windows 的 ChooseColor 约定 lpCustColors 由**调用方**持有，Tk 自己
    持有就没人替它落盘。这里直接 ctypes 调 comdlg32.ChooseColorW，数组由我们
    持有：`cust` 是 16 个 "#rrggbb" / "" 的列表，**原地更新**（确定或取消、
    只要开过对话框，格子的最终状态都留在里面），调用方负责对比后落盘。

    返回选中的 "#rrggbb"，取消 = None。hwndOwner 钉在设置窗上（模态）；
    ctypes 走不通时回落 tkinter.colorchooser（色板不持久化，仅保底不炸）。
    """
    import ctypes
    from tkinter import colorchooser
    try:
        from ctypes import wintypes

        class CHOOSECOLORW(ctypes.Structure):
            _fields_ = [("lStructSize", wintypes.DWORD),
                        ("hwndOwner", wintypes.HWND),
                        ("hInstance", wintypes.HINSTANCE),
                        ("rgbResult", wintypes.COLORREF),
                        ("lpCustColors", ctypes.POINTER(wintypes.COLORREF)),
                        ("Flags", wintypes.DWORD),
                        ("lCustData", ctypes.c_long),
                        ("lpfnHook", ctypes.c_void_p),
                        ("lpTemplateName", ctypes.c_wchar_p)]

        def _to_ref(hexv):
            hexv = (hexv or "").strip().lstrip("#")
            if len(hexv) != 6:
                return 0x00FFFFFF   # 空格 / 脏值 = Windows 默认白
            try:
                r, g, b = int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16)
            except ValueError:
                return 0x00FFFFFF
            return r | (g << 8) | (b << 16)      # COLORREF = 0x00BBGGRR

        def _to_hex(ref):
            return "#%02x%02x%02x" % (ref & 0xFF, (ref >> 8) & 0xFF, (ref >> 16) & 0xFF)

        arr = (wintypes.COLORREF * 16)(
            _to_ref(c) for c in (list(cust) + [""] * 16)[:16])
        hwnd = 0
        try:
            hwnd = ctypes.windll.user32.GetParent(parent.winfo_id()) \
                or parent.winfo_id()
        except Exception:
            pass
        cc = CHOOSECOLORW()
        cc.lStructSize = ctypes.sizeof(CHOOSECOLORW)
        cc.hwndOwner = hwnd
        cc.rgbResult = _to_ref(initial)
        cc.lpCustColors = ctypes.cast(arr, ctypes.POINTER(wintypes.COLORREF))
        cc.Flags = 0x1 | 0x2          # CC_RGBINIT（从当前色起跳）| CC_FULLOPEN
        ok = ctypes.windll.comdlg32.ChooseColorW(ctypes.byref(cc))
        cust[:] = [_to_hex(ref) for ref in arr]   # 确定或取消都把格子最终态带回来
        return _to_hex(cc.rgbResult) if ok else None
    except Exception:
        got = colorchooser.askcolor(color=initial or None, parent=parent, title=title)
        return got[1] if got and got[1] else None


def _simple_mode(cfg):
    """是不是普通用户模式：除显式 "advanced" 外一律按普通算（默认就是普通）。"""
    return str((cfg or {}).get("user_mode", "simple") or "simple") != "advanced"


# ---------------------------------------------------------------------------
# 开发者模式（W 2026-10-04）：关于页那行版本号**连点 5 次**才现身的隐藏页。
#
# 为什么**不**写进 NAV_SPEC：NAV_SPEC 说的是"普通用户看得到的左栏"，自检也按它的叶子数
# 钉着（`_selftest/test_settings_layout.py`）—— 把一项藏进去、只在运行时过滤掉，会让
# "这份常量到底描述谁"变得说不清。所以它单放一个常量，由 `_nav_items()` 在运行时拼。
#
# 也**不是** `nav_hide` 那种"合成项"（坑 142）：那是"这一项不在左栏成行、但它仍是导航
# 目标"；这里要的是"非开发者模式下它根本不存在"。
#
# `help` 空着是有意的：目标用户是开发者，这一页不放 "?"（W 2026-10-04 点名）。
# ---------------------------------------------------------------------------
DEV_NAV_ITEM = {"key": "dev", "label": "开发者选项", "page": "dev", "section": "dev",
                "title": "开发者选项", "help": ""}

# 后台查更新的间隔不在这里：它是"我们能多频繁地问 GitHub"的一部分，和 `COOLDOWN` /
# `COOLDOWN_TOKEN` 同一个账本，定在 `core/updater.POLL_EVERY`；起停逻辑在 ui/app.py
# 的 `_dev_upd_start`（只有填了 GitHub 令牌才会跑）。

# 关于页那行版本号的"隐形开关"（W 2026-10-04）：**连点 5 次**进开发者模式，
# 两次之间超过 3 秒就当没在连点、从 1 重新数（判定宽松一点，手速慢的人也能连上）。
DEV_TAP_TIMES = 5
DEV_TAP_GAP = 3.0


def _nav_items(dev_mode=False, simple=False):
    """左栏条目：普通用户模式 = SIMPLE_NAV_SPEC；高级用户模式 = NAV_SPEC；
    开发者模式 = 末尾多一项「开发者选项」（两种用户模式都追加，开发者模式不做改动）。

    尾部追加正好落在"关于与诊断"那组后面，也就是 W 要的"关于导航栏下方"。
    """
    base = SIMPLE_NAV_SPEC if simple else NAV_SPEC
    return list(base) + ([DEV_NAV_ITEM] if dev_mode else [])


def _upd_help_text():
    """关于页「检查更新」那个 "?" 的说明。

    为什么是个函数而不是写死的字符串：末句"后台定期查"**跟着令牌变**（只有开发者模式填了
    令牌才跑），令牌换了不重算就与事实不符 —— 开发者选项那边一改令牌就回来重算它。

    W 2026-10-05：把"多久不重复查 / 限额多少"这类**内部节流**的说法全删了 —— 用户不需要
    知道我们多久问一次 GitHub、额度怎么算（那是省额度的事）。只留"它是什么、什么时候会自己
    查、查到会怎样"。后台定期查这条要留着：弹窗会在用户没动过手时冒出来，得让他知道正常。
    """
    tail = texts.SET_UPD_HELP_TEXT_TAIL
    if secrets.get_github_token():
        tail += texts.SET_UPD_HELP_TEXT_TAIL_2
    return (texts.SET_UPD_HELP_TEXT_CAT + tail + "。")


def _nav_leaves(items=None, out=None):
    """把导航树拍平成叶子列表（页面顺序、区块归属都由它推）。"""
    out = out if out is not None else []
    for it in (items if items is not None else NAV_SPEC):
        kids = it.get("children")
        if kids:
            _nav_leaves(kids, out)
        else:
            out.append(it)
    return out


def _idle_fill(widget, fn, *args):
    """把"开页时要读盘 / 读 GGUF 头"的**回显**计算挪到 after_idle。

    为什么：设置页是按页建的，而几个回显值要经 scan_models / detect_file / 
    scan_video_models（实测 10~20ms/页，模型库越大、放在网络盘上越贵）——
    那笔钱不该算在"点一下左栏"这一下上。`fn` 只给已经建好的 Label / StringVar 填值，
    所以延后一拍用户看不见。

    ⚠ 回调里必须自己兜住异常与"控件已销毁"：`after_idle` 是挂在 Tcl 解释器上的，
    窗口被销毁**不会**取消它，这时去 configure 已销毁的控件会抛 TclError ——
    只会往崩溃日志里刷噪音（坑 135）。
    """
    def _run():
        try:
            if widget is not None and not widget.winfo_exists():
                return
        except Exception:
            return
        try:
            fn(*args)
        except Exception:
            pass
    try:
        widget.after_idle(_run)
    except Exception:
        _run()


def _open_outdir(path, what, setting=""):
    """打开一个输出目录：还没生成就先建，没配就点名该去哪儿配。

    原来是裸 `os.startfile(...)`：目录不存在时它抛 OSError，而 Tk 只把回调异常打到
    stderr（--windowed 的 exe 连 stderr 都没有）——用户点了按钮什么都没发生，
    看着像按钮坏了（与坑 88 同族的"根因在 stderr"症状）。
    另外 `sd_dir` 留空时 `os.path.join("", "output")` 会得到相对路径 "output"，
    那会在**当前工作目录**下建一个 output 并打开它，比报错更糟。
    """
    p = str(path or "").strip()
    if not p or not os.path.isabs(p):
        msg = texts.SET_OPEN_OUTDIR_CAT % what
        if setting:
            msg += texts.SET_OPEN_OUTDIR_CAT_2 % setting
        messagebox.showwarning(texts.SET_OPEN_OUTDIR_A0, msg)
        return False
    try:
        os.makedirs(p, exist_ok=True)
        os.startfile(p)
        return True
    except Exception as e:
        messagebox.showwarning(texts.SET_OPEN_OUTDIR_A0, texts.SWF_FILE_OPEN_FAIL % (p, e))
        return False


NEW_PROVIDER_LABEL = texts.SET_NEW_PROVIDER_LABEL      # 下拉里"还没建起来"那一项的标签

# 数值键的**可接受区间**：只列"填错了会静默出事"的那些 —— 端口填 99999 = 服务起不来、
# ctx 填十亿 = 启动即 OOM、步数 0 = 引擎报错。不在这张表里的键只查"是不是数字"。
# 注意 0 的语义：ngl / threads / top_k / vram_gb / ram_gb 的 0 都是"自动 / 全 CPU"，
# 必须留在区间内（别把 0 当非法值拦掉）。
_NUM_RANGE = {
    "port": (1, 65535), "proxy_port": (1, 65535), "remote_port": (1024, 65535),
    "ctx": (512, 1048576), "ngl": (0, 999), "threads": (0, 512),
    "max_tokens": (1, 1048576), "reasoning_budget": (0, 1048576),
    "seed": (-1, 2147483647), "top_k": (0, 100000),
    "img_steps": (1, 200), "img_seed": (-1, 2147483647),
    "vid_steps": (1, 200), "vid_frames": (1, 2000), "vid_fps": (1, 240),
    "vid_seed": (-1, 2147483647),
    "cloud_poll_seconds": (5, 3600), "cloud_wait_minutes": (1, 1440),
    "cloud_image_wait_seconds": (10, 3600), "cloud_submit_timeout": (10, 3600),
    "cloud_download_seconds": (10, 3600), "cloud_video_duration": (1, 60),
    "cloud_keep_days": (1, 365),
}
_FLOAT_RANGE = {
    "temperature": (0.0, 10.0), "top_p": (0.0, 1.0), "repeat_penalty": (0.0, 10.0),
    "vram_gb": (0.0, 4096.0), "ram_gb": (0.0, 4096.0),
    "img_cfg": (0.0, 100.0), "img_strength": (0.0, 1.0), "vid_cfg": (0.0, 100.0),
    # 底图重绘的另外两档与 `img_strength` 同尺度（0~1）
    "img_strength_light": (0.0, 1.0), "img_strength_mid": (0.0, 1.0),
    # 与 img_cfg / vid_cfg 同尺度（都是 --cfg-scale 一族的旋钮）；0 = 不传、跟 --cfg-scale 走
    "img_guide_scale": (0.0, 100.0),
}


def _num_error(v):
    """数值键的预检 → "" 表示没问题，否则返回给用户看的一句话。

    **先验后写**：写一半才发现某个键不合格，cfg 会停在"改了一半"的状态；
    更要紧的是这些键填错了不是"界面难看"而是真出事（见 `_NUM_RANGE` 的注释），
    而原来一律静默保留旧值 ——"静默"正是这个项目到处在消灭的东西。
    报错点名**键名**（设置页里那几行的标签基本就是键名），并写清区间。
    """
    for k in INT_KEYS:
        if k not in v:
            continue
        raw = str(v[k].get()).strip()
        try:
            n = int(raw)
        except Exception:
            return texts.SET_NUM_ERROR_CAT % (k, raw[:20])
        lo, hi = _NUM_RANGE.get(k, (None, None))
        if lo is not None and not (lo <= n <= hi):
            return texts.SET_NUM_ERROR_CAT_2 % (k, lo, hi, n)
    for k in FLOAT_KEYS:
        if k not in v:
            continue
        raw = str(v[k].get()).strip()
        try:
            f = float(raw)
        except Exception:
            return texts.SET_NUM_ERROR_CAT_3 % (k, raw[:20])
        lo, hi = _FLOAT_RANGE.get(k, (None, None))
        if lo is not None and not (lo <= f <= hi):
            return texts.SET_NUM_ERROR_CAT_4 % (k, lo, hi, f)
    # 「agent 上下文长度」不在 INT_KEYS 里（它写的是按模型记录，不是配置键），这里补上
    # 等价校验——此前它绕过全部数值检查与区间夹取：填 99999999 照样保存，agent 场景
    # 下次启动直接按它拉起服务 OOM；填非数字则写回静默失败（排查报告 R-115）。
    if "agent_ctx" in v:
        raw = str(v["agent_ctx"].get()).strip()
        try:
            n = int(raw)
        except Exception:
            return texts.SET_NUM_ERROR_CAT_5 % raw[:20]
        lo, hi = _NUM_RANGE["ctx"]
        if not (lo <= n <= hi):
            return texts.SET_NUM_ERROR_CAT_6 % n
    return ""




class SettingsMixin:
    """App 的设置窗口与本地模型 API 页职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    def _api_base_url(self):
        return "http://127.0.0.1:%s/v1" % self.cfg.get("proxy_port", 8081)

    def _api_model_name(self):
        """建议填写的模型名。

        性能关键：先做毫秒级的本地进程检查，进程存在才查 /v1/models（HTTP）；
        否则直接回退模型列表。此前无条件发 HTTP，服务停止时主线程
        要等 ~2 秒 TCP 超时（设置页打开卡顿的根因）。
        """
        if server_process_alive():
            m = _query_serving_model(self.cfg)
            if m:
                return m
        _d, chat, _img = scan_models(self.cfg)
        return os.path.basename(chat[0]) if chat else ""

    def _api_config_text(self):
        return (texts.SET_API_CONFIG_TEXT_CAT
                % (self._api_base_url(), self.cfg.get("api_key", ""),
                   self._api_model_name() or texts.SET_API_CONFIG_TEXT_N))

    def _gen_api_key(self):
        k = gen_api_key()
        self.cfg["api_key"] = k
        save_config(self.cfg)
        messagebox.showinfo("API Key",
                            texts.SET_GEN_API_KEY_CAT % k)
        return k

    def _restart_proxy(self):
        self.proxy.stop()
        if not has_local_chat(self.cfg):
            self._proxy_usable = False
            self._append(texts.SET_RESTART_PROXY_A0, "meta")
            return
        self._proxy_usable = True
        # 手动「重启代理」只看功能启用与否，不看「自启动」—— 自启动只管程序启动那一次
        if self.cfg.get("proxy_enabled"):
            self.proxy.start()
        else:
            self._append(texts.SET_RESTART_PROXY_A0_2, "meta")

    def open_settings(self, jump=""):
        """打开设置窗口。`jump` = 要直接定位到的导航叶子 key（输出栏那两个按钮用）。"""
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            if jump and self._settings_nav is not None:
                try:
                    self._settings_nav.select(jump)
                except Exception:
                    pass
            return
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title(texts.WDG_SETTINGS_TITLE)
        # 经典背景 + DWM 标题栏 / 框线两件套收在 theme.style_toplevel（坑 184/186）
        theme.style_toplevel(win)
        # 实测（DPI-aware，scaling≈2.0）滚动内容最宽一行需要 789px，左栏 232 + 边距
        # → 最小宽取 1080：横向不能滚，缩一点就是"右边那半截永远看不见"
        # 默认宽 1160（W 2026-10-03 要求"适当调大"）：1080 下实测内容最右到 1070、
        # 只剩 10px 余量；「模型族」那类"下拉 + 右侧状态回显"同排的版式最怕这个余量
        # （见 13坑 76）。**只加宽、不加高**：1366x768 扣任务栏约 728 可用，
        # 740 的高度已经贴边，加高会把按钮那排挤出屏幕。
        #
        # 位置（W 2026-10-09 定两条）：① 首次打开"与主页面位置保持一致"= 主窗当前
        # 左上角（原来不给位置 → Windows 把 Toplevel 丢在屏幕左上角）；② 重建路径
        # （切主题 / 切用户模式）在 destroy 前把当前几何存进 `_settings_geom`，这里
        # 消费它**原地恢复**（尺寸也一起，用户拖动 / 缩放过的位置不丢）。
        _g = str(getattr(self, "_settings_geom", "") or "")
        if _g:
            self._settings_geom = ""
            win.geometry(_g)
        else:
            win.geometry("1160x740+%d+%d" % (self.root.winfo_x(), self.root.winfo_y()))
        win.minsize(1080, 600)
        win.transient(self.root)

        # ---- 固定外框 ----
        # 底部按钮条**先**按 side="bottom" 入列：pack 按入列顺序分配空间，先入列的保住自己，
        # 窗口缩小时被压的应该是右侧内容区，而不是「保存」（同主页输入区的教训，坑 56）。
        bar = ttk.Frame(win)
        bar.pack(side="bottom", fill="x", padx=12, pady=(4, 10))
        main = ttk.Frame(win)
        main.pack(side="top", fill="both", expand=True, padx=(10, 10), pady=(10, 0))

        v = {}
        save_hooks = {}            # 区块 frame → callable()→(ok, 说明)：底部「保存」要一起跑
        registry = {}              # (page, section) → 构建函数
        # page → [callable]：**切进这一页时**要跑的事（目前只有关于页「进页自动查更新」）。
        # 区块是懒建的，回调在区块构建时才登记进来，所以执行放在 _nav_select 建完区块之后。
        enter_hooks = {}
        bodies = {}                # (page, section) → 区块内容 frame（已建则复用）
        heads = {}                 # (page, section) → 区块标题 frame（锚点）
        page_frames = {}           # page → 页面 frame
        state = {"page": None, "section": None, "leaf": None}
        # 用户模式（2026-10-07 W 定）：普通（默认）/ 高级两套导航与两套页面裁剪，
        # 底部按钮互切（存进配置、整窗重建 —— 预热全部重走，不留半态）
        simple = _simple_mode(self.cfg)
        leaves = _nav_leaves(_nav_items(self._dev_mode, simple))
        page_order, page_items = [], {}
        for it in leaves:
            if it["page"] not in page_order:
                page_order.append(it["page"])
                page_items[it["page"]] = []
            page_items[it["page"]].append(it)

        nav = widgets.SideNav(main, _nav_items(self._dev_mode, simple), width=232,
                              on_select=lambda it: _nav_select(it))
        self._settings_nav = nav            # 输出栏的按钮要能直接跳到某个叶子

        def _rebuild_nav():
            """开发者模式开关一动 → 重算左栏（关于页版本号连点 5 次那条路会调它）。

            项数变了，`leaves` / `page_items` 都得跟着变 —— `_make_page` 是按
            `page_items[page]` 建区块的，不重算就建不出「开发者选项」那一页。
            `page_items` 必须**原地清空再填**：`_nav_select` / `_make_page` / `_global_save`
            都闭包引着这个 dict 对象，换成新对象它们就永远看不见新页了。
            """
            nonlocal leaves
            items = _nav_items(self._dev_mode, simple)
            nav.spec = items
            leaves = _nav_leaves(items)
            page_order[:] = []
            page_items.clear()
            for it in leaves:
                if it["page"] not in page_order:
                    page_order.append(it["page"])
                    page_items[it["page"]] = []
                page_items[it["page"]].append(it)
            nav.render()

        sp = widgets.ScrollPage(main)
        # 页面栈挂在实例上：判断"某个控件是不是在当前页"要走 `sp.is_current()`
        # （叠放实现里所有页都ismapped，`winfo_ismapped()` 不再能区分 —— 见
        # widgets.PageStack 类注释）。自检也按这个判据筛当前页。
        self._settings_sp = sp
        # 拖动缩放"渲染跟在后面"（W 2026-10-10 批的方案 A，08 §10.7）：拖动期把
        # 整个滚动区摘出布局（只剩左栏 + 底部按钮条外壳，实测 326.6 → 23.1 ms/档），
        # 停手 300ms 装回一次。摘/装的 pack 参数照 ScrollPage.__init__ 那两行。
        self._settings_gate = widgets.ResizeDeferGate(
            win,
            lambda: (sp.canvas.pack_forget(), sp.bar.pack_forget()),
            lambda: (sp.bar.pack(side="right", fill="y"),
                     sp.canvas.pack(side="left", fill="both", expand=True)))

        # ---- 「高级参数」折叠区（设置页分层）----
        # 口径：把"族相关 / 高级"参数默认收起来，页面只剩常用的那几项；控件**照旧建**
        # （收起走 `grid_remove()`，只挪格子不销毁）→ 值不丢、底部「保存」的钩子也照跑
        # （与 ScrollPage / 勾选窗口折叠组同一套路，见 13 坑 85 的教训）。
        # "用户改过"的项绝不藏：但它只体现在折叠标题的计数上（`已改 N`），不把行搬出来
        # —— 搬行要重排行号、还会让版面随用户历史漂移（v40「收起状态也要让用户看见这组里
        # 有我勾的东西」是同一判据）。
        folds = {}          # name → [控件]（收起时 grid_remove）
        fold_heads = {}     # name → 折叠标题 Label
        fold_keys = {}      # name → [配置键]（算总数与"已改 N"）
        sess = getattr(self, "_settings_ui", None)
        if not isinstance(sess, dict):        # 老实例 / 直接构造时兜底
            sess = {"fold": {}}
            self._settings_ui = sess
        sess.setdefault("fold", {})

        def _changed(key):
            """这一项是不是改过出厂默认（改过的项绝不被藏）。"""
            return str(self.cfg.get(key, "")) != str(DEFAULT_CONFIG.get(key, ""))

        def _render_fold(name):
            """按当前状态摆/收这一组，并刷新标题（▸/▾ + 项数 + 已改数）。"""
            open_ = bool((sess.get("fold") or {}).get(name))
            for w in folds.get(name) or []:
                try:
                    if open_:
                        w.grid()
                    else:
                        w.grid_remove()
                except Exception:
                    pass
            head = fold_heads.get(name)
            if head is None:
                return
            keys = fold_keys.get(name) or []
            cut = sum(1 for k in keys if _changed(k))
            tail = (texts.SET_RENDER_FOLD_CAT % (len(keys), cut)) if cut else ("（%d）" % len(keys))
            try:
                head.configure(text=texts.SET_RENDER_FOLD_CAT_2 % ("▾" if open_ else "▸", tail))
            except Exception:
                pass

        def _toggle_fold(name):
            d = sess.setdefault("fold", {})
            d[name] = not bool(d.get(name))
            _render_fold(name)

        def _fold_head(parent, rows, name, keys):
            """插一行折叠标题（可点）：展开 / 收起这一组。"""
            fold_keys[name] = list(keys)
            i = rows["i"]
            rows["i"] += 1
            lab = tk.Label(parent, text="", cursor="hand2", anchor="w",
                           background=widgets.default_bg(),
                           font=("Microsoft YaHei UI", 10, "bold"))
            lab.grid(row=i, column=0, columnspan=3, sticky="w", pady=(10, 2))
            lab.bind("<Button-1>", lambda _e, n=name: _toggle_fold(n))
            fold_heads[name] = lab
            _render_fold(name)
            return lab

        def _state_row(parent, rows, var, fn, *args):
            """区块顶部的动态状态回显（一行）：这一块"能不能用 / 缺什么"。

            走 textvariable 留在页面上 —— 动态回显不进悬停（红/黄与状态那一条口径）。
            依赖没就绪**不藏参数**：指路靠的就是块内那几项，藏了用户就没地方填。
            填充函数经 `_idle_fill` 延后跑（那一步可能要扫盘 / 读 GGUF 头）。
            """
            i = rows["i"]
            rows["i"] += 1
            lab = ttk.Label(parent, textvariable=var, style="Note.TLabel",
                            font=("Microsoft YaHei UI", 9))
            lab.grid(row=i, column=0, columnspan=3, sticky="w", pady=(0, 6))
            # 变量必须挂在控件上保活：`StringVar` 一旦被 GC，Tcl 侧的名字就没了，
            # 这个 Label 会静默变空（同坑 122 的 `lab._logo_img`）。after_idle 的闭包
            # 只在回调期间持有它 —— 撑不住。
            lab._state_var = var
            _idle_fill(parent, fn, *args)

        def section(page_id, sec_id):
            """注册一个区块的构建函数（构建粒度是**页**，注册粒度是区块）。

            分成两段是为了：左栏每个叶子指向"哪页哪段"，点下去能滚到那一段的标题；
            但真正建内容时把**这一页的所有区块一起建**（见 _nav_select）——
            只建一段会得到半页空白 + 保存漏钩子。
            仍然按页懒建的理由是每台 ttk 控件创建+布局约 1.3ms，五页一次全建约 280ms 会卡。
            """
            def deco(build):
                registry[(page_id, sec_id)] = build
                return build
            return deco

        def _row(parent, rows, label, widget, desc, hint="", lw=14, fold=None,
                 hint_var=None):
            """一行：标签（旁边挂 "?"）/ 控件 / 短摘要。

            v39 起灰色长说明**不再内联**（它把窗口撑到 1020 宽、还把版面切成三段），
            改成 "?" 悬停；只有真正要"填之前就知道"的约束留在外面（hint，≤14 字）。

            "?" 挂在**标签**这一侧（2026-10-01 改）：它解释的是"这一行是什么"，跟着标签才
            读得顺；留在填写栏右边看着像那栏的校验提示。红 / 黄警告与动态状态回显照旧内联
            在控件那一侧 —— 那是"当前状态"不是"说明"（§5.2 第 14 条）。

            `lw` 是标签列的字符宽：整页布局用 14（要容得下 repeat_penalty 这种长英文名），
            云端那两列并排时只有 802/2 的横向预算，标签列收到 8（中文标签最长 5 个字）。

            `fold` 给一个折叠组名时，这一行的三格（标签 / 控件 / 摘要）会一起登记进那一组，
            并按当前状态收起 —— 只是 `grid_remove()` 挪格子，控件与它的值都还在。

            `hint_var`（坑 183）：给一个 StringVar 时摘要格挂 textvariable，行外可以
            换文案与样式（分辨率框的红字警示走这条路）；返回摘要格的句柄，没有则 None。
            """
            i = rows["i"]
            rows["i"] += 1
            bg = widgets.default_bg()
            # 标签格只在"真会放 ?"时才套 Frame：普通模式 help 一律空串（HelpDot
            # 不建控件）、高级模式 desc 为空的行也一样 —— 那时标签直接进列 0，
            # **省掉每行一个包装 Frame**（W 2026-10-08：每个行包装 Frame 约让窗口
            # 缩放贵 2 ms/档，见 `08` §10.5；列索引 / columnspan / 折叠登记全不动）。
            if not simple and desc:
                # 普通用户模式全页不放 "?"（2026-10-07 W 定）；说明照旧只给高级模式
                lab = tk.Frame(parent, background=bg)
                lab.grid(row=i, column=0, sticky="w", padx=(0, 8), pady=5)
                ttk.Label(lab, text=label, width=lw, anchor="w").pack(side="left")
                widgets.HelpDot(lab, desc).pack(side="left", padx=(2, 0))
            else:
                lab = ttk.Label(parent, text=label, width=lw, anchor="w")
                lab.grid(row=i, column=0, sticky="w", padx=(0, 8), pady=5)
            widget.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=5)
            cell = None
            if hint or hint_var is not None:
                # 摘要同样不套 Frame（原来 Frame 里就一个 Label）
                if hint_var is not None:
                    cell = ttk.Label(parent, textvariable=hint_var,
                                     style="Dim.TLabel",
                                     font=("Microsoft YaHei UI", 9))
                else:
                    cell = ttk.Label(parent, text=hint, style="Dim.TLabel",
                                     font=("Microsoft YaHei UI", 9))
                cell.grid(row=i, column=2, sticky="w", pady=5)
            if fold:
                grp = folds.setdefault(fold, [])
                for w in (lab, widget, cell):
                    if w is not None:
                        grp.append(w)
                # 登记后立刻按当前状态摆 / 收 —— 不能无条件 grid_remove：
                # 会话里展开过的组，重开设置窗时本来就该是展开的
                _render_fold(fold)
            return cell

        def row(parent, rows, label, widget, desc, hint="", lw=14, fold=None,
                hint_var=None):
            return _row(parent, rows, label, widget, desc, hint, lw, fold, hint_var)

        def ent(parent, rows, key, label, desc, width=8, var=None, trace=None, hint="",
                lw=14, fold=None, hint_var=None):
            """一行"标签 + 输入框"，说明在标签旁的 "?" 里。

            key 非空时变量登记进 v（由 _apply_settings 统一写回 cfg）；
            传 var 则用外部变量（云端服务商那几项是结构化数据，自己管保存，
            不走 _apply_settings）。trace 用于即时联动（如回显请求地址）。
            返回 (输入框, 摘要格) —— 摘要格可能为 None；旧调用方都不接返回值。
            """
            if var is None:
                var = v.setdefault(key, tk.StringVar(value=str(self.cfg.get(key, ""))))
            e = theme.entry(parent, textvariable=var, width=width)
            if trace is not None:
                var.trace_add("write", lambda *a: trace())
            cell = _row(parent, rows, label, e, desc, hint, lw, fold, hint_var)
            return e, cell

        def _size_entry(parent, rows, key, label, desc, width=12, hint="", lw=14,
                        fold=None, allow=None, warn_text=None):
            """分辨率类输入框（坑 183，W 2026-10-09）：写法守卫挂在**失焦**上。

            · 能解析成 宽x高 的写法（x/X/*/＊/×/，/,/全角数字，判据只有
              core.media.parse_size 这一份）失焦时归一成半角 x 显示 ——
              832*480 填进去，走开就变成 832x480；
            · 解析不出又不在 `allow` 例外的（云端出图尺寸的 16:9 比例、
              云端视频分辨率的 720P 档位）就在框后那格（hint 位）挂红字警示。
              **只提示不拦发送**：发送侧不合格值照旧回退默认档 / 不送该参数
              （W 2026-10-09 定），红字把「静默回退」变成看得见。
            开窗时对存量值也做一次同样的事（老配置里可能存着 1024*1024 这类）。
            """
            var = v.setdefault(key, tk.StringVar(value=str(self.cfg.get(key, ""))))
            hvar = tk.StringVar(value=hint)
            e, cell = ent(parent, rows, key, label, desc, width=width, var=var,
                          hint=hint, hint_var=hvar, lw=lw, fold=fold)
            bad_text = warn_text or SIZE_WARN

            def _bad(t):
                return media.parse_size(t) is None and not (allow(t) if allow else False)

            def _refresh():
                if _bad(var.get()):
                    cell.configure(style="ErrLit.TLabel")
                    hvar.set(bad_text)
                else:
                    cell.configure(style="Dim.TLabel")
                    hvar.set(hint)

            def _leave(_e=None):
                p = media.parse_size(var.get())
                if p is not None and "%dx%d" % p != var.get():
                    var.set("%dx%d" % p)
                _refresh()

            e.bind("<FocusOut>", _leave)
            _leave()
            return e, cell

        def _active_media_cap(kind):
            """主页面当前选中的云端媒体模型 → (provider, model, profile)。

            不是该链路（本地模型 / 选的是文本模型 / 没选云端）→ (None, "", {})。
            设置页按**打开本页时**的选择渲染；发送侧仍以实际模型为准。
            """
            if not providers.is_cloud(self.cfg):
                return None, "", {}
            p = providers.current_provider(self.cfg) or {}
            sp = providers.split_cloud_id(self.cfg.get("model", ""))
            if not p or not sp:
                return None, "", {}
            if providers.model_kind_of(p, sp[1]) != kind:
                return None, "", {}
            return p, sp[1], providers.profile_for(p, sp[1])

        def _combo_entry(parent, rows, key, label, desc, values, width=10, hint="",
                         lw=14, fold=None):
            """可编辑下拉：白名单来自能力档案，**仍允许手填**（坑 73：官方枚举问不全）。"""
            var = v.setdefault(key, tk.StringVar(value=str(self.cfg.get(key, ""))))
            cb = theme.comb(parent, textvariable=var, state="normal", width=width,
                            values=list(values))
            cell = _row(parent, rows, label, cb, desc, hint, lw, fold)
            return cb, cell

        def _media_cap_line(parent, rows, kind, lw=8, colspan=3, wraplength=300):
            """按当前云端模型回显"这家是什么"，并返回能力档案供调用方置灰 / 选下拉。"""
            p, m, pf = _active_media_cap(kind)
            if not pf:
                txt = texts.SET_CMEDIA_N_8 % providers.KIND_LABEL[kind]
            else:
                proto = providers.media_api(p)
                txt = texts.SET_CMEDIA_N_7 % (
                    providers.short_of(m),
                    providers.MEDIA_LABEL.get(proto, proto or ""))
            lbl = ttk.Label(parent, text=txt, style="Hint.TLabel",
                            wraplength=wraplength, justify="left",
                            font=("Microsoft YaHei UI", 9))
            lbl.grid(row=rows["i"], column=0, columnspan=colspan, sticky="w",
                     pady=(0, 4))
            rows["i"] += 1
            return p, m, pf, lbl

        # ---- 区块 1：本地文本模型 / 生成参数 ----
        @section("local_text", "gen")
        def _t1(t1, r1):
            if simple:
                # 普通用户模式（2026-10-07 W 定）：只留三件 —— 展示思考过程、随机性
                # （temperature 的易理解命名）、系统提示词；不再区分「生成参数 / 服务参数」
                # （服务参数整段只在高级模式出现，ngl / ctx / 端口那些由程序自动管）。
                i = r1["i"]
                r1["i"] += 1
                v["show_reasoning"] = tk.BooleanVar(
                    value=bool(self.cfg.get("show_reasoning", True)))
                theme.check(t1, text=texts.SET_LTGEN_N,
                                variable=v["show_reasoning"]).grid(
                    row=i, column=0, columnspan=3, sticky="w", pady=8)
                ent(t1, r1, "temperature", texts.SET_LTGEN_A3,
                    texts.SET_LTGEN_A4)
                i = r1["i"]
                r1["i"] += 1
                head = tk.Frame(t1, background=widgets.default_bg())
                head.grid(row=i, column=0, sticky="nw", padx=(0, 8), pady=5)
                ttk.Label(head, text=texts.SET_LTGEN_N_2, width=14, anchor="nw").pack(side="left")
                # 边框必须显式给：裸 Text 的默认高亮环色与主题底色同灰，四边全部
                # 融进背景（视觉上"下边框丢了"，实测四边都丢）；焦点态给主色，与
                # 圆角输入框（Round.TEntry 的 focus 态）同一套语义
                v["system_prompt"] = tk.Text(t1, height=3, width=44,
                                             font=("Microsoft YaHei UI", 9),
                                             bd=0, highlightthickness=1,
                                             highlightbackground=theme.c("border"),
                                             highlightcolor=theme.c("accent"),
                                             **theme.text_kw())
                v["system_prompt"].grid(row=i, column=1, columnspan=2, sticky="nsew", pady=5)
                v["system_prompt"].insert("1.0", str(self.cfg.get("system_prompt", "")))
                return
            ent(t1, r1, "temperature", "temperature",
                texts.SET_LTGEN_A4)
            ent(t1, r1, "top_p", "top_p",
                texts.SET_LTGEN_A4_2)
            ent(t1, r1, "max_tokens", "max_tokens",
                texts.SET_LTGEN_A4_3,
                hint=texts.SET_LTGEN_N_3)
            i = r1["i"]
            r1["i"] += 1
            head = tk.Frame(t1, background=widgets.default_bg())
            head.grid(row=i, column=0, sticky="nw", padx=(0, 8), pady=5)
            ttk.Label(head, text="system_prompt", width=14, anchor="nw").pack(side="left")
            widgets.HelpDot(head, texts.SET_LTGEN_A1).pack(side="left", padx=(2, 0))
            # width 必须显式给：tk.Text 默认 80 字符，在高 DPI 下要 1080px，
            # 会把这一行顶出滚动可视区（横向不能滚，等于看不见）
            # 边框显式给（同上面 simple 分支：默认高亮环色与主题底色融合会隐形）
            v["system_prompt"] = tk.Text(t1, height=3, width=44,
                                         font=("Microsoft YaHei UI", 9),
                                         bd=0, highlightthickness=1,
                                         highlightbackground=theme.c("border"),
                                         highlightcolor=theme.c("accent"),
                                         **theme.text_kw())
            v["system_prompt"].grid(row=i, column=1, columnspan=2, sticky="nsew", pady=5)
            v["system_prompt"].insert("1.0", str(self.cfg.get("system_prompt", "")))

            i = r1["i"]
            r1["i"] += 1
            v["show_reasoning"] = tk.BooleanVar(value=bool(self.cfg.get("show_reasoning", True)))
            theme.check(t1, text=texts.SET_LTGEN_N_4,
                            variable=v["show_reasoning"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=8)

            # 「高级参数」：采样细节（top_k / 重复惩罚）与随机种子 —— 平时不动它们
            fold_gen = "local_text:gen"
            _fold_head(t1, r1, fold_gen, ("top_k", "repeat_penalty", "seed"))
            ent(t1, r1, "top_k", "top_k",
                texts.SET_LTGEN_A4_4, fold=fold_gen)
            ent(t1, r1, "repeat_penalty", "repeat_penalty",
                texts.SET_LTGEN_A4_5,
                fold=fold_gen)
            ent(t1, r1, "seed", "seed",
                texts.SET_LTGEN_A4_6, fold=fold_gen)

        # ---- 区块 2：本地文本模型 / 服务参数 ----
        @section("local_text", "svc")
        def _t2(t2, r2):

            # 2026-10-16 清理（W 定）：原来这里还有 model（模型位置）/ models_dir（模型文件夹）/
            # exe（server 路径）三行 —— "手填路径"时代的遗留设计。选模型的正路是主界面模型
            # 菜单（外挂来源走「模型来源…」），模型目录与引擎归「模型文件与引擎」页管
            # （模型来源… / 引擎管理），三行全是重复入口，已删；连坑 146 那套
            # 「cloud id 不能串进本地参数」的特例写回也一并消失（见 _apply_settings）。
            # ngl：显示/修改的是"当前模型"的值（按模型分别记忆）
            v["ngl"] = tk.StringVar(value=str(current_ngl(self.cfg)))
            e_ngl = theme.entry(t2, textvariable=v["ngl"], width=8)
            row(t2, r2, "n-gpu-layers", e_ngl,
                texts.SET_LTSVC_A4_3)
            # ctx：与 ngl 同一口径 —— 显示/修改的是"当前模型"的值（按模型记忆）。
            # 原来这里预填的是全局兜底 cfg["ctx"]，而保存又无条件写进当前模型的记录，
            # 于是"打开任意一页点保存"就会把该模型自动算出的 context 覆盖掉（坑 130）
            v["ctx"] = tk.StringVar(value=str(ctx_for(self.cfg)))
            # 建页时的"当前模型"快照：设置窗非模态、这一页只建一次，开着窗在主页切了
            # 模型再点保存时，输入框里装的仍是**旧模型**的值 —— 写回必须落到快照那个
            # 模型的记录上，不能落到切换后的新模型头上（排查报告 R-114 加重项）。
            v["_svc_snap"] = os.path.basename(str(self.cfg.get("model", "")))
            e_ctx = theme.entry(t2, textvariable=v["ctx"], width=8)
            _row(t2, r2, "context (-c)", e_ctx,
                 texts.SET_LTSVC_A4_4,
                 hint=texts.SET_LTSVC_N)
            ent(t2, r2, "port", "port",
                texts.SET_LTSVC_A4_5)
            # 这里**不放 api_key**：那是「本地模型 API」那一页的事（生成 / 复制 / 撤销都在一处），
            # 摆在服务参数里会让人以为改完要重启服务，也会和那页的只读回显对不上
            v["reasoning_mode"] = tk.StringVar(value=str(self.cfg.get("reasoning_mode", "default")))
            cb = theme.comb(t2, textvariable=v["reasoning_mode"],
                              values=["default", "off", "budget"], width=8, state="readonly")
            row(t2, r2, "reasoning", cb,
                texts.SET_LTSVC_A4_6)

            # 「高级参数」：硬件属性（自动探测预填，平时不用看）、线程数、
            # 思考预算（只在 reasoning=budget 时用）、附加命令行 —— 都是"配一次就不管"的，
            # 收起来；常用那几个（层数 / 上下文 / 端口 / 思考模式）留在外面
            fold_svc = "local_text:svc"
            _fold_head(t2, r2, fold_svc,
                       ("gpu_name", "vram_gb", "ram_gb", "threads",
                        "reasoning_budget", "extra_args"))
            ent(t2, r2, "gpu_name", texts.SET_LTSVC_A3_2,
                texts.SET_LTSVC_A4_8, width=32, fold=fold_svc)
            ent(t2, r2, "vram_gb", texts.SET_LTSVC_A3_3,
                texts.SET_LTSVC_A4_9, width=8, fold=fold_svc)
            ent(t2, r2, "ram_gb", texts.SET_LTSVC_A3_4,
                texts.SET_LTSVC_A4_10, width=8,
                fold=fold_svc)
            ent(t2, r2, "threads", "threads",
                texts.SET_LTSVC_A4_11, fold=fold_svc)
            ent(t2, r2, "reasoning_budget", "budget tokens",
                texts.SET_LTSVC_A4_12, fold=fold_svc)
            ent(t2, r2, "extra_args", "extra_args",
                texts.SET_LTSVC_A4_13, width=24,
                fold=fold_svc)

        # ---- 区块 3：本地图像与视频 / 生图 ----
        #
        # 分层：状态行（依赖就绪与否）→ 常改的 7 项 → 「高级参数」折叠区（配套文件 + 后端 /
        # 种子 / 附加参数 / 输出目录 / 带图两档的可调项，共 13 项，默认收起）。折叠只挪格子不毁控件
        # —— 值不丢、保存钩子照跑（见 _row 的 fold 说明）；每组标题点一下即可展开。
        @section("local_media", "img")
        def _t3(t3, r3):
            if simple:
                # 普通用户模式（2026-10-07 W 定）：分辨率 + 输出目录 + 打开输出目录 三件；
                # 模型文件夹 / 模型族 / 步数 / CFG 与全部配套件都在高级模式
                # （程序按扫描与模型族表自动安排，普通用户不需要碰）。
                # 引擎目录（sd_dir）2026-10-16 起两档都不再放了 —— 引擎由「模型文件与引擎 →
                # 引擎管理」统一管（进页自动定向 + 手动定向 + 装完自动指路）。
                _size_entry(t3, r3, "img_size", texts.SET_LMIMG_A3,
                    texts.SET_LMIMG_A4,
                    width=12, hint=texts.SET_LMIMG_N)
                ent(t3, r3, "img_output_dir", texts.SET_OPEN_OUTDIR_A0,
                    texts.SET_LMIMG_A4_2,
                    width=30, hint=texts.SET_LMIMG_N_2)
                fr_i = ttk.Frame(t3)
                theme.button(fr_i, text=texts.SET_LMIMG_N_3,
                           command=lambda: _open_outdir(
                               media.img_out_dir(self.cfg),
                               texts.SET_LMIMG_A1,
                               texts.SET_LMIMG_A2)).pack(side="left")
                row(t3, r3, "", fr_i,
                    texts.SET_LMIMG_A4_3)
                return

            def _fill_state(var):
                """依赖就绪回显：认出几个生图模型（延后跑，要扫盘）。"""
                try:
                    _dd, _chat, imgs = scan_models(self.cfg)
                    n = len(imgs)
                except Exception:
                    n = 0
                if n:
                    var.set(texts.SET_LMIMG_CAT % n)
                else:
                    var.set(texts.SET_LMIMG_A0)

            img_state = tk.StringVar(value="")
            _state_row(t3, r3, img_state, _fill_state, img_state)
            # 「生图模型文件夹 / 默认生图模型」两行 2026-10-16 已删（W 定）：用哪个模型
            # 一律认主菜单里选中的那个（坏选中就报错让重选，不静默换）；"模型在哪"归
            # 「模型来源…」——旧两个键既不显示也不再生效，老配置目录会在 load_config
            # 里一次性折进 extra_models（见 core/config.py 迁移段、`13` §7.58）。
            # 模型族：识别结果只决定"拼哪些参数、要哪些配套件"，参数值仍来自下面这些设置
            _fam_opts = sdprofile.family_choices("image")
            _code2label = {c: t for c, t in _fam_opts}
            v["img_family"] = tk.StringVar(
                value=_code2label.get(str(self.cfg.get("img_family", "") or "").strip(),
                                      _fam_opts[0][1]))
            fam_cb = theme.comb(t3, textvariable=v["img_family"], state="readonly",
                                  width=24, values=[t for _c, t in _fam_opts])
            img_note = tk.StringVar(value="")
            _fam_row = r3["i"]
            row(t3, r3, texts.SET_LMIMG_A2_2, fam_cb,
                texts.SET_LMIMG_A4_7)
            # 「识别为…」与下拉同排、贴它右侧（W 2026-10-03）：状态回显归控件那一侧，
            # 不另起一行占版心
            ttk.Label(t3, textvariable=img_note, style="Note.TLabel",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=_fam_row, column=2, sticky="w", padx=(0, 10), pady=5)

            def refresh_img_note(*_a):
                """回显识别结果，按**当前选中的**生图模型现算（2026-10-16 收口）。
                内容长度固定 —— 状态类 Label 拼长文案会引发整页重排（坑 92）。"""
                code = {t: c for c, t in sdprofile.family_choices("image")}.get(
                    v["img_family"].get(), sdprofile.AUTO)
                path = media.resolve_img_model_path(self.cfg)
                if not path:
                    img_note.set(texts.SET_LMIMG_N_15)
                    return
                fid, _basis, _ = sdprofile.detect_file(path, kind="image", forced=code)
                # 只留族名：「通用（只喂文件）」那半句是给下拉看的说明，贴进状态行
                # 会把这一行顶出右边界（坑 76）
                img_note.set(texts.SET_LMIMG_CAT_2 % sdprofile.label_of(fid).split("（")[0])

            v["img_family"].trace_add("write", refresh_img_note)
            # 开页这次回显要读模型文件头，延到开页之后（_idle_fill）；下拉联动那次仍即时算
            _idle_fill(t3, refresh_img_note)
            ent(t3, r3, "img_steps", texts.SET_LMIMG_A3_5,
                texts.SET_LMIMG_A4_8,
                hint=texts.SET_LMIMG_N_4)
            _size_entry(t3, r3, "img_size", texts.SET_LMIMG_A3_6,
                texts.SET_LMIMG_A4,
                width=12, hint=texts.SET_LMIMG_N)
            ent(t3, r3, "img_cfg", texts.SET_LMIMG_A3_7,
                texts.SET_LMIMG_A4_9)
            fold_img = "local_media:img"
            _fold_head(t3, r3, fold_img,
                       ("img_vae_file", "img_llm_file", "img_clip_l_file",
                        "img_clip_g_file", "img_t5_file", "img_negative",
                        "img_seed", "img_backend", "img_params_backend",
                        "img_extra_args", "img_output_dir",
                        "img_strength_auto", "img_strength_light", "img_strength_mid",
                        "img_strength",
                        "img_guide_scale", "img_ref_args"))
            ent(t3, r3, "img_output_dir", texts.SET_OPEN_OUTDIR_A0,
                texts.SET_LMIMG_A4_10,
                width=30, hint=texts.SET_LMIMG_N_2, fold=fold_img)
            ent(t3, r3, "img_vae_file", texts.SET_LMIMG_A3_8,
                texts.SET_LMIMG_A4_11, width=30, hint=texts.SET_LMIMG_N_5, fold=fold_img)
            ent(t3, r3, "img_llm_file", texts.SET_LMIMG_A3_9,
                texts.SET_LMIMG_A4_12,
                width=30, hint=texts.SET_LMIMG_N_5, fold=fold_img)
            ent(t3, r3, "img_clip_l_file", "CLIP-L",
                texts.SET_LMIMG_A4_13, width=30, hint=texts.SET_LMIMG_N_5,
                fold=fold_img)
            ent(t3, r3, "img_clip_g_file", "CLIP-G",
                texts.SET_LMIMG_A4_14, width=30, hint=texts.SET_LMIMG_N_5,
                fold=fold_img)
            ent(t3, r3, "img_t5_file", "T5-XXL",
                texts.SET_LMIMG_A4_15, width=30, hint=texts.SET_LMIMG_N_5,
                fold=fold_img)
            ent(t3, r3, "img_negative", texts.SET_LMIMG_A3_10,
                texts.SET_LMIMG_A4_16, width=30, hint=texts.SET_LMIMG_N_6, fold=fold_img)
            ent(t3, r3, "img_seed", texts.SET_LMIMG_A3_11,
                texts.SET_LMIMG_A4_17, fold=fold_img)
            ent(t3, r3, "img_backend", texts.SET_LMIMG_A3_12,
                texts.SET_LMIMG_A4_18,
                width=30, hint=texts.SET_LMIMG_N_7, fold=fold_img)
            ent(t3, r3, "img_params_backend", texts.SET_LMIMG_A3_13,
                texts.SET_LMIMG_A4_19, width=30, hint=texts.SET_LMIMG_N_5,
                fold=fold_img)
            ent(t3, r3, "img_extra_args", texts.SET_LMIMG_A3_14,
                texts.SET_LMIMG_A4_20,
                width=30, hint=texts.SET_LMIMG_N_8, fold=fold_img)
            # 底图重绘的强度分档（2026-10-11）：判据与取值口径只有 core/media.edit_strength
            # 那一处，这里只是把三档的值交给人。`img_strength`（常规档）以前**页面根本没有
            # 入口**，只能改配置文件 —— 顺带补上。
            v["img_strength_auto"] = tk.BooleanVar(
                value=bool(self.cfg.get("img_strength_auto", True)))
            cb_auto_st = theme.check(t3, text=texts.SET_LMIMG_N_9,
                                     variable=v["img_strength_auto"])
            row(t3, r3, texts.SET_LMIMG_A2_3, cb_auto_st,
                texts.SET_LMIMG_A4_21,
                fold=fold_img)
            ent(t3, r3, "img_strength_light", texts.SET_LMIMG_A3_15,
                texts.SET_LMIMG_A4_22,
                hint=texts.SET_LMIMG_N_10, fold=fold_img)
            ent(t3, r3, "img_strength_mid", texts.SET_LMIMG_A3_16,
                texts.SET_LMIMG_A4_23,
                hint=texts.SET_LMIMG_N_11, fold=fold_img)
            ent(t3, r3, "img_strength", texts.SET_LMIMG_A3_17,
                texts.SET_LMIMG_A4_24,
                hint=texts.SET_LMIMG_N_12, fold=fold_img)

            # 带图两档各自专属的可调项（W 2026-10-06：先收进折叠区，日后随高级选项
            # 一起搬去独立页）。参数名以 sd-cli --help 为准（坑 95：没实测就写明依据）：
            #   --img-cfg-scale  default: same as --cfg-scale（底图重绘那一路的原文）
            #   --ref-image-args empty = 按模型权重自动判断（主体参考那一路的原文）
            ent(t3, r3, "img_guide_scale", texts.SET_LMIMG_A3_18,
                texts.SET_LMIMG_A4_25,
                hint=texts.SET_LMIMG_N_13, fold=fold_img)
            ent(t3, r3, "img_ref_args", texts.SET_LMIMG_A3_19,
                texts.SET_LMIMG_A4_26,
                width=30, hint=texts.SET_LMIMG_N_5, fold=fold_img)

            # 「输出目录」这一行与生视频那块**同一形状**（标签 + 打开按钮 + 一句说明），
            # 两块的尾巴长得一样，扫一眼就知道哪儿开文件夹
            fr_i = ttk.Frame(t3)
            theme.button(fr_i, text=texts.SET_LMIMG_N_14,
                       command=lambda: _open_outdir(
                           media.img_out_dir(self.cfg),
                           texts.SET_LMIMG_A1,
                           texts.SET_LMIMG_A2_4)).pack(side="left")
            row(t3, r3, texts.SET_OPEN_OUTDIR_A0, fr_i,
                texts.SET_LMIMG_A4_27)
            refresh_img_note()

        # ---- 区块 4：本地图像与视频 / 生视频 ----
        #
        # 分层同生图：状态行 → 常改的 8 项（目录 / 主体 / 族 / 分辨率 / 帧数 / 帧率 / 步数 /
        # CFG）→ 「高级参数」折叠区（编码器 / VAE / MoE 高噪段 / 音频 VAE / 负向词 / 容器 /
        # 后端 / 输出目录，共 13 项，默认收起）。
        @section("local_media", "vid")
        def _t3b(t3b, r3b):
            if simple:
                # 普通用户模式（2026-10-07 W 定）：分辨率 / 帧率 / 秒数 / 输出目录 /
                # 打开输出目录 五件；主体文件 / 模型族 / 步数 / CFG 与全部配套件在高级模式。
                # 「秒数」（2026-10-17 W）：普通用户想的是"生成多长"，不是帧数——
                # 落盘仍折回 vid_frames = 秒数 × 帧率（帧是引擎口径，1~2000 帧的
                # 区间与高级模式共用 _NUM_RANGE 一处）；回显 = cfg 帧数 ÷ cfg 帧率、
                # 一位小数。帧本来就会被引擎"自动对齐"（坑 39），回显舍入的 ±1 帧
                # 差不改变出片档位。
                _size_entry(t3b, r3b, "vid_size", texts.SET_LMIMG_A3,
                    texts.SET_LMVID_A4, width=12)
                ent(t3b, r3b, "vid_fps", texts.SET_LMVID_A3,
                    texts.SET_LMVID_A4_2)

                def _fps_int(raw):
                    try:
                        n = int(str(raw).strip())
                    except Exception:
                        n = 0
                    return n if n > 0 else int(DEFAULT_CONFIG["vid_fps"])

                def _frames_int(raw):
                    try:
                        n = int(str(raw).strip())
                    except Exception:
                        n = 0
                    return n if n > 0 else int(DEFAULT_CONFIG["vid_frames"])

                v["_vid_sec"] = tk.StringVar(t3b, value="%g" % round(
                    _frames_int(self.cfg.get("vid_frames"))
                    / float(_fps_int(self.cfg.get("vid_fps"))), 1))
                _sec_ent = theme.entry(t3b, textvariable=v["_vid_sec"], width=8)
                row(t3b, r3b, texts.SET_LMVID_SEC, _sec_ent,
                    texts.SET_LMVID_SEC_DESC, hint=texts.SET_LMVID_SEC_HINT)

                def _sec_hook():
                    """「秒数」→ vid_frames = 秒数 × 帧率（底部「保存」钩子）。

                    坏值开口不落盘（与 _num_error 同口径）：非数字回「要填数字」，
                    折出的帧数出 1~2000 界就按**当前帧率**反算成秒数区间告诉用户。
                    """
                    raw = str(v["_vid_sec"].get()).strip()
                    try:
                        sec = float(raw)
                    except Exception:
                        return False, (texts.SET_NUM_ERROR_CAT_3
                                       % (texts.SET_LMVID_SEC, raw[:20]))
                    fps = _fps_int(v["vid_fps"].get())
                    lo, hi = _NUM_RANGE["vid_frames"]
                    frames = sec * fps
                    if not (sec > 0) or not (lo <= frames <= hi):
                        return False, (texts.SET_NUM_ERROR_CAT_4
                                       % (texts.SET_LMVID_SEC, lo / float(fps),
                                          hi / float(fps), sec))
                    self.cfg["vid_frames"] = int(round(frames))
                    return True, ""
                save_hooks[t3b] = _sec_hook
                ent(t3b, r3b, "vid_output_dir", texts.SET_OPEN_OUTDIR_A0,
                    texts.SET_LMVID_A4_4,
                    width=30, hint=texts.SET_LMIMG_N_2)
                fr_v = ttk.Frame(t3b)
                theme.button(fr_v, text=texts.SET_LMIMG_N_3,
                           command=lambda: _open_outdir(
                               media.vid_out_dir(self.cfg),
                               texts.SET_LMVID_A1,
                               texts.SET_LMVID_A2)).pack(side="left")
                row(t3b, r3b, "", fr_v,
                    texts.SET_LMVID_A4_5)
                return

            def _fill_state(var):
                """依赖就绪回显：扫到几个视频扩散主体（延后跑，要读 GGUF 头）。"""
                try:
                    vids, _encs = scan_video_models(self.cfg)
                    n = len(vids)
                except Exception:
                    n = 0
                if n:
                    var.set(texts.SET_LMVID_CAT % n)
                else:
                    var.set(texts.SET_LMVID_A0)

            vid_state = tk.StringVar(value="")
            _state_row(t3b, r3b, vid_state, _fill_state, vid_state)
            # 「视频模型文件夹 / 扩散主体文件名」两行 2026-10-16 已删（W 定）：主体一律认
            # 主菜单里选中的那个生视频模型（坏选中报错让重选，不再静默换"扫描第一个"）；
            # "组件在哪"归「模型来源…」（见 core/config.py 迁移段、`13` §7.58）。
            _vf_opts = sdprofile.family_choices("video")
            _vf2code = {t: c for c, t in _vf_opts}
            v["vid_family"] = tk.StringVar(
                value=_vf2code.get(str(self.cfg.get("vid_family", "") or "").strip(),
                                   _vf_opts[0][1]))
            vfile_cb = theme.comb(t3b, textvariable=v["vid_family"], state="readonly",
                                    width=24, values=[t for _c, t in _vf_opts])
            vid_note = tk.StringVar(value="")
            _vfam_row = r3b["i"]
            row(t3b, r3b, texts.SET_LMIMG_A2_2, vfile_cb,
                texts.SET_LMVID_A4_8)
            ttk.Label(t3b, textvariable=vid_note, style="Note.TLabel",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=_vfam_row, column=2, sticky="w", padx=(0, 10), pady=5)

            def refresh_vid_note(*_a):
                """回显识别结果，按**当前选中的**生视频模型现算（2026-10-16 收口）；
                一行、长度固定（长文案塞进 Label 会引发整页重排，坑 92）。"""
                code = {t: c for c, t in sdprofile.family_choices("video")}.get(
                    v["vid_family"].get(), sdprofile.AUTO)
                cur = str(self.cfg.get("model") or "")
                path = ""
                if str(self.cfg.get("model_kind") or "") == "video" and cur:
                    if os.path.isfile(cur):
                        path = cur
                if not path:
                    vid_note.set(texts.SET_LMVID_N_6)
                    return
                fid, _basis, _ = sdprofile.detect_file(path, kind="video", forced=code)
                vid_note.set(texts.SET_LMIMG_CAT_2 % sdprofile.label_of(fid).split("（")[0])

            v["vid_family"].trace_add("write", refresh_vid_note)
            _size_entry(t3b, r3b, "vid_size", texts.SET_LMIMG_A3,
                texts.SET_LMVID_A4, width=12)
            ent(t3b, r3b, "vid_frames", texts.SET_LMVID_A3_2,
                texts.SET_LMVID_A4_3,
                hint=texts.SET_LMVID_N)
            ent(t3b, r3b, "vid_fps", texts.SET_LMVID_A3,
                texts.SET_LMVID_A4_2)
            ent(t3b, r3b, "vid_steps", texts.SET_LMVID_A3_5,
                texts.SET_LMVID_A4_9)
            ent(t3b, r3b, "vid_cfg", "CFG",
                texts.SET_LMVID_A4_10)

            fold_vid = "local_media:vid"
            _fold_head(t3b, r3b, fold_vid,
                       ("vid_llm_file", "vid_vae_file", "vid_t5_file",
                        "vid_tokenizer_file", "vid_high_noise_file",
                        "vid_audio_vae_file", "vid_neg_prompt", "vid_format",
                        "vid_seed", "vid_backend", "vid_params_backend",
                        "vid_extra_args", "vid_output_dir"))
            ent(t3b, r3b, "vid_output_dir", texts.SET_OPEN_OUTDIR_A0,
                texts.SET_LMVID_A4_11,
                width=30, hint=texts.SET_LMIMG_N_2, fold=fold_vid)
            ent(t3b, r3b, "vid_llm_file", texts.SET_LMVID_A3_6,
                texts.SET_LMVID_A4_12,
                width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_vae_file", texts.SET_LMVID_A3_7,
                texts.SET_LMVID_A4_13,
                width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_t5_file", texts.SET_LMVID_A3_8,
                texts.SET_LMVID_A4_14,
                width=30, hint=texts.SET_LMIMG_N_5, fold=fold_vid)
            ent(t3b, r3b, "vid_tokenizer_file", texts.SET_LMVID_A3_9,
                texts.SET_LMVID_A4_15,
                width=30, hint=texts.SET_LMVID_N_2, fold=fold_vid)
            ent(t3b, r3b, "vid_high_noise_file", texts.SET_LMVID_A3_10,
                texts.SET_LMVID_A4_16,
                width=30, hint=texts.SET_LMVID_N_3, fold=fold_vid)
            ent(t3b, r3b, "vid_audio_vae_file", texts.SET_LMVID_A3_11,
                texts.SET_LMVID_A4_17, width=30, hint=texts.SET_LMIMG_N_5,
                fold=fold_vid)
            ent(t3b, r3b, "vid_neg_prompt", texts.SET_LMIMG_A3_10,
                texts.SET_LMVID_A4_18,
                width=30, hint=texts.SET_LMVID_N_4, fold=fold_vid)
            ent(t3b, r3b, "vid_format", texts.SET_LMVID_A3_12,
                texts.SET_LMVID_A4_19, width=10,
                fold=fold_vid)
            ent(t3b, r3b, "vid_seed", texts.SET_LMVID_A3_13, texts.SET_LMVID_A4_20, fold=fold_vid)
            ent(t3b, r3b, "vid_backend", texts.SET_LMIMG_A3_12,
                texts.SET_LMVID_A4_21, width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_params_backend", texts.SET_LMIMG_A3_13,
                texts.SET_LMVID_A4_22,
                width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_extra_args", texts.SET_LMIMG_A3_14,
                texts.SET_LMVID_A4_23,
                width=30, fold=fold_vid)

            fr_v = ttk.Frame(t3b)
            theme.button(fr_v, text=texts.SET_LMVID_N_5,
                       command=lambda: _open_outdir(
                           media.vid_out_dir(self.cfg),
                           texts.SET_LMVID_A1,
                           texts.SET_LMVID_A2_2)).pack(side="left")
            row(t3b, r3b, texts.SET_OPEN_OUTDIR_A0, fr_v,
                texts.SET_LMVID_A4_24)
            # 开页这次回显可能要扫视频目录，延到开页之后（_idle_fill）；下拉联动那次仍即时算
            _idle_fill(t3b, refresh_vid_note)

        # ---- 区块 5：本地模型 API（给 agent 调用） ----
        @section("api", "api")
        def _t4(t4, r4):

            self.api_hint_var = tk.StringVar(value="")
            api_key_var = tk.StringVar(value=str(self.cfg.get("api_key", "")))
            # 模型名回显要读一次盘（服务没在跑时走 scan_models），延到开页之后填：
            # 别挡在"点一下左栏"这一下上（_idle_fill）
            api_model_var = tk.StringVar(value=texts.SET_API_N)

            def _fill_api_model(var=api_model_var):
                var.set(self._api_model_name() or texts.SET_API_N_2)

            _idle_fill(t4, _fill_api_model)
            # 没有本地可转发的文本模型 = 这个功能开不起来：默认关、复选框锁住、顶上说明原因
            usable = has_local_chat(self.cfg)
            self._proxy_usable = usable

            running = self.proxy.running()
            ttk.Label(t4, text=(texts.SET_API_CAT % self.cfg.get("proxy_port", 8081))
                      if running else texts.SET_API_N_3,
                      style="OkLit.TLabel" if running else "Muted.TLabel",
                      font=("Microsoft YaHei UI", 10, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4["i"] = 1
            if not usable:
                ttk.Label(t4, text=texts.SET_API_N_4,
                          style="ErrLit.TLabel", wraplength=740, justify="left",
                          font=("Microsoft YaHei UI", 9)).grid(
                    row=r4["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
                r4["i"] += 1

            fr = ttk.Frame(t4)
            # 启动引擎的闸门与主页面同一条判据（core.models.chat_start_block；坑 128：
            # 一个事实只写一处，两个入口各查各的迟早走偏）。这一页的 usable 本来就是
            # has_local_chat，置灰只是把它也落到这个按钮上；悬停文案与顶栏共用一份表。
            blk = chat_start_block(self.cfg)
            svc_btn = theme.button(fr, text=texts.SET_API_N_5, width=22,
                       command=self.on_start_restart_agent,
                       state="disabled" if blk else "normal")
            widgets.bind_tip(svc_btn, lambda: START_BLOCK_TIP.get(blk, ""))
            svc_btn.pack(side="left", padx=(0, 6))
            theme.button(fr, text=texts.APP_BTN_STOP_SVC, width=10,
                       command=self.stop_server_async).pack(side="left", padx=(0, 6))
            theme.button(fr, text=texts.SET_API_N_6, width=10,
                       command=self._restart_proxy).pack(side="left")
            row(t4, r4, texts.SET_API_A2, fr,
                texts.SET_API_A4)

            self.agent_ctx_var = tk.StringVar(
                value=str(ctx_for(self.cfg, agent=True)))
            row(t4, r4, texts.SET_API_A2_2,
                theme.entry(t4, textvariable=self.agent_ctx_var, width=10),
                texts.SET_API_A4_2)
            v["agent_ctx"] = self.agent_ctx_var
            # 建页时的"当前模型"快照（与服务参数页同一理由，见 _svc_snap；R-114/R-115）
            v["_api_snap"] = os.path.basename(str(self.cfg.get("model", "")))

            fr = ttk.Frame(t4)
            v_bu = tk.StringVar(value=self._api_base_url())
            theme.entry(fr, textvariable=v_bu, width=25, state="readonly").pack(side="left", padx=(0, 6))
            theme.button(fr, text=texts.SET_API_N_7, width=6,
                       command=lambda: self._copy_text(v_bu.get(), "Base URL")).pack(side="left")
            row(t4, r4, texts.SET_API_A2_3, fr,
                texts.SET_API_A4_3)

            fr = ttk.Frame(t4)
            theme.entry(fr, textvariable=api_key_var, width=20, state="readonly").pack(
                side="left", padx=(0, 6))
            theme.button(fr, text=texts.SET_API_N_7, width=6,
                       command=lambda: self._copy_text(api_key_var.get(), "API Key")).pack(
                side="left", padx=(0, 6))
            theme.button(fr, text=texts.SET_API_N_8, width=10,
                       command=lambda: (api_key_var.set(self._gen_api_key()),
                                        self.api_hint_var.set(
                                            texts.SET_API_A0))).pack(side="left")
            row(t4, r4, "API Key", fr, texts.SET_API_A4_4)

            fr = ttk.Frame(t4)
            theme.entry(fr, textvariable=api_model_var, width=25, state="readonly").pack(
                side="left", padx=(0, 6))
            theme.button(fr, text=texts.SET_API_N_7, width=6,
                       command=lambda: self._copy_text(api_model_var.get(), texts.SET_API_A1)).pack(side="left")
            row(t4, r4, texts.SET_API_A1, fr, texts.SET_API_A4_5)

            fr = ttk.Frame(t4)
            theme.button(fr, text=texts.SET_API_N_9, width=24,
                       command=lambda: self._copy_text(self._api_config_text(), texts.SET_API_A1_2)).pack(side="left")
            row(t4, r4, texts.SET_API_A2_4, fr, texts.SET_API_A4_6)

            ent(t4, r4, "proxy_port", texts.SET_API_A3,
                texts.SET_API_A4_7,
                width=8, hint=texts.SET_API_N_10)
            # 启用与自启动是两个开关（2026-10-07 W 定）：
            #   启用   = 这个功能开不开（默认关；普通用户模式整页不出现，切回去也不会被关）
            #   自启动 = 程序启动时自动把代理服务带起来；不勾 = 要用时在本页手动启动
            v_px = tk.BooleanVar(value=bool(self.cfg.get("proxy_enabled")) and usable)
            cb_px = theme.check(t4, text=texts.SET_API_N_11, variable=v_px,
                                    state="normal" if usable else "disabled")
            row(t4, r4, texts.SET_API_A2_5, cb_px,
                texts.SET_API_N_12 if usable else
                texts.SET_API_N_13)
            v["proxy_enabled"] = v_px
            v_auto = tk.BooleanVar(value=bool(self.cfg.get("proxy_autostart", True)))
            cb_auto = theme.check(t4, text=texts.SET_API_N_14,
                                      variable=v_auto,
                                      state="normal" if usable else "disabled")
            row(t4, r4, texts.SET_API_A2_6, cb_auto,
                texts.SET_API_A4_8)
            v["proxy_autostart"] = v_auto

            ttk.Label(t4, textvariable=self.api_hint_var, style="OkLit.TLabel",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=1, columnspan=2, sticky="w", pady=4)
            r4["i"] += 1
            ttk.Label(t4, text=(texts.SET_API_CAT_2
                                # 这一页是 agent 场景，显示 agent 那一份（与上面那个输入框同源）；
                                # 原来显示全局兜底 cfg["ctx"]，与按模型记忆的实际值对不上（坑 130）
                                % ctx_for(self.cfg, agent=True)),
                      style="WarnLit.TLabel", wraplength=740, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=0, columnspan=3, sticky="w", pady=(8, 2))
            r4["i"] += 1
            ttk.Label(t4, text=(texts.SET_API_N_15),
                      style="ErrLit.TLabel", wraplength=740, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=0, columnspan=3, sticky="w", pady=(8, 2))
            r4["i"] += 1

            # 底部「保存」在本页时的额外动作：端口/启停真的变了就把代理重启一次，
            # 否则"保存了却没生效"和"保存了却关窗"一样让人以为配好了
            init_proxy = (int(self.cfg.get("proxy_port", 8081) or 0),
                          bool(self.cfg.get("proxy_enabled")))

            def _api_hook():
                try:
                    new = (int(str(v["proxy_port"].get()).strip()), bool(v_px.get()))
                except Exception:
                    return False, texts.SET_API_L1
                if new != init_proxy:
                    self._restart_proxy()
                    self.api_hint_var.set(texts.SET_API_CAT_3 % new[0])
                return True, ""

            save_hooks[t4] = _api_hook

        # ---- 区块 6：云端模型 / 服务商与密钥 ----
        @section("cloud_p", "prov")
        def _t4b(t4b, r4b):
            """云端服务商区块。页面开头那段说明在 NAV_SPEC 的 help 里（标题旁的 "?"）。

            约定（v33）：内置服务商只内置"名称 + base_url"；添加服务商**不会**自动把
            它名下所有模型塞进主页面菜单。流程是 填信息 → 填密钥 → 测试连接 →
            通过后弹出独立的「模型选择」界面，由用户勾选要用的模型；
            拉到的清单缓存在 provider.model_kinds 里，只有点「刷新模型」才重新请求。
            """

            st = {"pid": "", "builtin": False, "models": [], "kinds": {}, "cands": []}
            vars_ = {}
            for k in ("name", "base_url", "timeout", "ctx", "media_base_url"):
                vars_[k] = tk.StringVar()
            # 连接配置二级窗（2026-10-17）：窗只此一个；conn_btn 是主页面那颗入口按钮，
            # load() 按"内置 / 自定义"换它的文案（查看连接… / 连接配置…）。
            conn_win = [None]
            conn_btn = [None]
            new_prev = [""]          # 选「＋ 新建服务商…」之前停在哪家（取消时退回去）
            # 「原生接口协议」下拉里摆的是中文标签，存进配置的是代码（auto/none/aliyun/…）：
            # 域名认不出来的自建代理只能靠这一项手动指过去
            _ma_opts = [("auto", texts.SET_PROV_L1),
                        ("none", texts.SET_PROV_L1_2)] + [
                (c, providers.MEDIA_LABEL[c]) for c in providers.MEDIA_APIS
                if c not in ("auto", "none")]
            _ma_text = {t: c for c, t in _ma_opts}
            _ma_code = {c: t for c, t in _ma_opts}
            vars_["media_api"] = tk.StringVar(value=_ma_code["auto"])
            vars_["enabled"] = tk.BooleanVar(value=True)
            url_lbl = tk.StringVar(value="")
            media_lbl = tk.StringVar(value="")
            jobs_lbl = tk.StringVar(value="")
            key_lbl = tk.StringVar(value="")
            cat_lbl = tk.StringVar(value="")
            msg_lbl = tk.StringVar(value="")
            menu_lbl = tk.StringVar(value="")
            # 下面那个 Listbox 的"显示行 → 配置里的原名"对照表。列表里显示的是缩写名
            # （short_labels），**配置里存的永远是原名** —— 「移出选中项」必须按这张表反查，
            # 直接拿显示行去比原名会一个都删不掉却报成功（坑 131）
            lb_map = {}

            def provider_snapshot():
                """把页面上正在编辑的内容拼成 provider 结构（可能还没保存到配置里）。"""
                p = providers.get_provider(self.cfg, st["pid"]) if st["pid"] else None
                base = dict(p) if p else {}
                base.update({"id": st["pid"], "name": vars_["name"].get() or
                             (providers.builtin(st["pid"]).get("name") or st["pid"]),
                             "base_url": vars_["base_url"].get(),
                             "models": list(st["models"]),
                             "model_kinds": dict(st["kinds"]),
                             "enabled": vars_["enabled"].get(),
                             "timeout": vars_["timeout"].get(),
                             "ctx": vars_["ctx"].get(),
                             "media_base_url": vars_["media_base_url"].get(),
                             "media_api": _ma_text.get(vars_["media_api"].get(), "auto")})
                return providers.normalize_provider(base)

            def refresh_url(*_a):
                if st["builtin"] and not providers.builtin_base_editable(st["pid"]):
                    url_lbl.set(texts.SET_PROV_CAT
                                % providers.chat_completions_url(
                                    providers.builtin(st["pid"])))
                else:
                    url_lbl.set(texts.SET_PROV_CAT
                                % providers.chat_completions_url(
                                    {"base_url": vars_["base_url"].get()}))

            def refresh_key(*_a):
                m = secrets.mask(secrets.get_api_key(st["pid"])) if st["pid"] else ""
                key_lbl.set(texts.SET_PROV_CAT_2 % m if m else texts.SET_PROV_N)

            def refresh_media(*_a):
                """回显"这个服务商能不能走云端生图/生视频"，省得用户猜。

                原生接口地址是**从 base_url 推**的（同族域名只差 /compatible-mode 这一段），
                推不出来时在这里说清楚，而不是等用户发一条消息换来一个看不懂的 404。
                """
                p = provider_snapshot()
                api, root = providers.media_api(p), providers.media_api_root(p)
                # 只在"这条接不了 / 要你手填"时才占一行（W 2026-10-03）；认出来了不说话
                if str(p.get("media_api") or "auto") == "none":
                    media_lbl.set(texts.SET_PROV_A0)
                elif not api:
                    media_lbl.set(texts.SET_PROV_A0_2)
                elif not root:
                    media_lbl.set(texts.SET_PROV_A0_3)
                else:
                    media_lbl.set("")

            def refresh_jobs(*_a):
                """任务台账一行的现状：产物只活 24 小时，积压要看得见。"""
                try:
                    pend = cloudjobs.unfinished()
                except Exception as e:
                    jobs_lbl.set(texts.SET_PROV_CAT_3 % e)
                    return
                if not pend:
                    jobs_lbl.set(texts.SET_PROV_A0_4)
                    return
                jobs_lbl.set(texts.SET_PROV_CAT_4
                             % (len(pend),
                                "；".join(cloudjobs.describe(j) for j in pend[:3])))

            def refresh_menu_list(_e=None):
                lb.delete(0, "end")
                lb_map.clear()
                # 显示缩写（与主页面菜单同一个规则），配置里存的仍是原名
                lab = providers.short_labels(st["models"])
                for m in st["models"]:
                    row_label = "%s · %s" % (providers.KIND_LABEL[
                        providers.model_kind_of({"model_kinds": st["kinds"]}, m)],
                        lab.get(m, m))
                    lb.insert("end", row_label)
                    lb_map[row_label] = m
                menu_lbl.set(texts.SET_PROV_CAT_5
                             % len(st["models"]))
                cat_lbl.set(texts.SET_PROV_CAT_6
                            % len(providers.catalog(
                                {"models": st["models"],
                                 "model_kinds": st["kinds"]})))

            # 地址类变量的联动挂**一次**（二级窗反复开关不再重复 trace）：
            # base_url 同时影响"请求地址"回显与原生接口推导，原生接口地址只影响推导。
            vars_["base_url"].trace_add("write",
                                        lambda *a: (refresh_url(), refresh_media()))
            vars_["media_base_url"].trace_add("write", lambda *a: refresh_media())

            def load(pid):
                st["pid"] = pid
                st["builtin"] = providers.is_builtin(pid)
                p = providers.get_provider(self.cfg, pid) if pid else None
                st["models"] = list((p or {}).get("models") or [])
                st["kinds"] = dict((p or {}).get("model_kinds") or {})
                vars_["name"].set((p or {}).get("name", ""))
                vars_["base_url"].set((p or {}).get("base_url", ""))
                vars_["timeout"].set(str((p or {}).get("timeout", 600)))
                vars_["ctx"].set(str((p or {}).get("ctx", 0) or 0))
                vars_["media_base_url"].set((p or {}).get("media_base_url", "") or "")
                _ma = str((p or {}).get("media_api") or "auto")
                vars_["media_api"].set(_ma_code.get(_ma, _ma_code["auto"]))
                vars_["enabled"].set(bool((p or {}).get("enabled", True)) if p else True)
                # 连接字段全部收进二级窗（2026-10-17）：主页面只剩一颗入口按钮。
                # 内置服务商是"查看"（名称/地址只读，editable_base_url 那两条在窗内放开），
                # 自定义与「＋ 新建服务商…」是"配置"。
                if conn_btn[0] is not None:
                    conn_btn[0].configure(
                        text=(texts.SET_PROV_N_25 if st["builtin"]
                              else texts.SET_PROV_N_24))
                refresh_url()
                refresh_media()
                refresh_key()
                refresh_menu_list()
                msg_lbl.set("")

            def commit(silent=False):
                """把页面当前内容写进配置；返回 (ok, 说明)。底部「保存」也走这里。"""
                if not st["pid"]:
                    name = vars_["name"].get().strip()
                    burl = vars_["base_url"].get().strip()
                    if not name and not burl and not st["models"]:
                        return True, ""      # 这页压根没填，没什么要保存的，别拦着关窗
                    nid = (name or "cloud").replace(" ", "-")
                    base_id, n = nid, 2
                    while providers.get_provider(self.cfg, base_id):
                        base_id = "%s%d" % (nid, n)
                        n += 1
                    ok, err = providers.add_provider(self.cfg, {
                        "id": base_id, "name": name, "base_url": burl,
                        "models": list(st["models"]), "model_kinds": dict(st["kinds"]),
                        "enabled": vars_["enabled"].get(),
                        "timeout": vars_["timeout"].get(),
                        "ctx": vars_["ctx"].get(),
                        "media_base_url": vars_["media_base_url"].get(),
                        "media_api": _ma_text.get(vars_["media_api"].get(), "auto")})
                    if not ok:
                        msg_lbl.set(texts.SET_PROV_CAT_7 % err)
                        return False, err      # 半填的新服务商：留着窗口让用户补
                    st["pid"] = base_id
                    save_config(self.cfg)
                    refresh_combo(keep=base_id)
                    if not silent:
                        msg_lbl.set(texts.SET_PROV_CAT_8 % (name or base_id))
                    self._update_model_label()
                    return True, ""
                data = {"name": vars_["name"].get(), "base_url": vars_["base_url"].get(),
                        "models": list(st["models"]), "model_kinds": dict(st["kinds"]),
                        "enabled": vars_["enabled"].get(),
                        "timeout": vars_["timeout"].get(),
                        "ctx": vars_["ctx"].get(),
                        "media_base_url": vars_["media_base_url"].get(),
                        "media_api": _ma_text.get(vars_["media_api"].get(), "auto")}
                if not providers.update_provider(self.cfg, st["pid"], data):
                    msg_lbl.set(texts.SET_PROV_CAT_9 % st["pid"])
                    return False, texts.SET_PROV_L1_3
                save_config(self.cfg)
                if not silent:
                    msg_lbl.set(texts.SET_PROV_CAT_10
                                % (st["pid"], len(st["models"])))
                self._update_model_label()
                return True, ""

            def ensure_saved():
                """密钥/模型选择/测试都要先有个能用的 provider id。"""
                ok, msg = commit(silent=True)
                if not ok:
                    messagebox.showwarning(texts.SET_PROV_A0_5, msg)
                return ok

            def open_task_probe(host):
                """「只查不提交」的核对窗（16 §24.1 / R-04 后半，2026-10-17 落地）。

                场景只有一个：提交请求超时 / 断连，"没拿到回包"不等于"没建单"。
                重发一次就是第二条账单，所以先按模型名 + 时间窗查一遍对方的任务列表；
                查到就把那条加进台账，之后走既有的「取回」（查状态 + 下载，不重新提交）。
                只有阿里云系有这条批量端点，别家点进来会被告知"只能按 task_id 单查"。
                """
                p = provider_snapshot()
                if not p:
                    msg_lbl.set(texts.SET_PROV_A0_8)
                    return
                d = tk.Toplevel(host)
                d.title(texts.SET_PROV_PROBE_TITLE)
                theme.style_toplevel(d)
                d.transient(host)
                d.minsize(620, 0)
                body = ttk.Frame(d)
                body.pack(fill="both", expand=True, padx=14, pady=(12, 4))
                ttk.Label(body, text=texts.SET_PROV_PROBE_HINT, style="Hint.TLabel",
                          wraplength=580, justify="left",
                          font=("Microsoft YaHei UI", 9)).grid(
                    row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

                mvar = tk.StringVar(value="")
                wvar = tk.StringVar(value=texts.SET_PROV_PROBE_WINS[1])
                ttk.Label(body, text=texts.SET_PROV_PROBE_MODEL).grid(
                    row=1, column=0, sticky="e", padx=(0, 6), pady=3)
                theme.entry(body, textvariable=mvar, width=28).grid(
                    row=1, column=1, sticky="w", pady=3)
                ttk.Label(body, text=texts.SET_PROV_PROBE_WIN).grid(
                    row=1, column=2, sticky="e", padx=(12, 6), pady=3)
                cb = ttk.Combobox(body, textvariable=wvar, state="readonly", width=12,
                                  values=list(texts.SET_PROV_PROBE_WINS))
                cb.grid(row=1, column=3, sticky="w", pady=3)

                lst = tk.Listbox(body, height=8, activestyle="none",
                                 background=theme.c("bg"), foreground=theme.c("body"),
                                 selectbackground=theme.c("accent"),
                                 selectforeground=theme.c("bg"),
                                 highlightthickness=0, bd=0,
                                 font=("Consolas", 9))
                lst.grid(row=2, column=0, columnspan=4, sticky="we", pady=(8, 4))
                sb = theme.scroll(body, orient="vertical", command=lst.yview)
                sb.grid(row=2, column=4, sticky="ns", pady=(8, 4))
                lst.configure(yscrollcommand=sb.set)
                body.columnconfigure(1, weight=1)

                stat = tk.StringVar(value="")
                ttk.Label(body, textvariable=stat, style="Hint.TLabel",
                          wraplength=580, justify="left").grid(
                    row=3, column=0, columnspan=5, sticky="w")
                got = {"items": []}

                def _window_seconds():
                    label = wvar.get()
                    idx = list(texts.SET_PROV_PROBE_WINS).index(label) \
                        if label in texts.SET_PROV_PROBE_WINS else 1
                    return (1800, 7200, 86400)[idx]

                def _fmt(t):
                    return time.strftime("%Y%m%d%H%M%S",
                                         time.localtime(time.time() - t))

                def query():
                    stat.set(texts.SET_PROV_PROBE_QUERYING)
                    go_btn.configure(state="disabled")
                    win = _window_seconds()
                    model = mvar.get().strip()

                    def work():
                        ok, items, err, _raw = cloud_media.list_tasks(
                            p, model=model, start=_fmt(win), end=_fmt(0),
                            page=1, size=20)

                        def show():
                            go_btn.configure(state="normal")
                            got["items"] = items if ok else []
                            lst.delete(0, "end")
                            for it in got["items"]:
                                lst.insert("end", "%s ｜ %-8s ｜ %s ｜ %s" % (
                                    it["task_id"][:20],
                                    cloud_media._LABEL.get(it["status"], it["status"]),
                                    (it.get("model") or "?")[:22],
                                    it.get("submit_time") or ""))
                            stat.set(err if not ok else
                                     (texts.SET_PROV_PROBE_EMPTY if not got["items"]
                                      else texts.SET_PROV_PROBE_N % len(got["items"])))
                        self._ui_q.put(show)
                    threading.Thread(target=work, daemon=True).start()

                def take():
                    sel = lst.curselection()
                    if not sel or sel[0] >= len(got["items"]):
                        stat.set(texts.SET_PROV_PROBE_PICK)
                        return
                    it = got["items"][sel[0]]
                    urls = list(it.get("urls") or [])
                    if not urls:
                        stat.set(texts.SET_PROV_PROBE_NOT_READY)
                        return
                    kind = "video" if any(
                        str(u).lower().split("?")[0].endswith((".mp4", ".webm", ".mov"))
                        for u in urls) else "image"
                    tid = it["task_id"]
                    if not cloudjobs.get_job(tid):
                        cloudjobs.add_job(tid, kind, p.get("id", ""),
                                          p.get("name", ""), it.get("model") or "", "",
                                          "", status=it.get("status") or "succeeded")
                    cloudjobs.update_job(tid, urls=urls,
                                         status=it.get("status") or "succeeded")
                    job = cloudjobs.get_job(tid) or {"task_id": tid, "kind": kind,
                                                     "urls": urls}
                    stat.set(texts.SET_PROV_PROBE_TAKEN % tid[:16])
                    try:
                        d.destroy()
                    except Exception:
                        pass
                    self._recover_job(job)

                bf = ttk.Frame(d)
                bf.pack(side="bottom", fill="x", padx=14, pady=(4, 12))
                theme.button(bf, text=texts.SET_PROV_PROBE_ADD, width=18,
                             command=take).pack(side="right")
                theme.button(bf, text=texts.BTN_CANCEL, width=8,
                             command=d.destroy).pack(side="right", padx=6)
                go_btn = theme.button(bf, text=texts.SET_PROV_PROBE_GO, width=10,
                                      bootstyle="primary-outline", command=query)
                go_btn.pack(side="left")
                d.bind("<Escape>", lambda _e: d.destroy())
                widgets.center_on(d, host)
                return d

            def open_conn_dialog(new=False):
                """「连接配置 / 查看连接」二级窗（2026-10-17，W 批准）。

                低频的连接字段全在这里：名称 / base_url / 原生接口地址 / 原生接口协议
                （高级模式再加超时、上下文窗口、启用与任务台账）。主页面只留
                服务商下拉、这颗入口、删除、测试连接、密钥与模型清单。

                · 内置服务商的名称与地址只读；`editable_base_url` 那两条
                  （按量百炼 / 方舟）照旧放开 base_url，并挂各自条目的 base_note。
                · **控件直接绑 vars_**：commit 仍走既有那一条路，窗内不另写保存逻辑。
                · 「确定」成功才 destroy；「取消」/ 右上角 X = 丢弃（从已存配置重载）。
                """
                d = conn_win[0]
                if d is not None and d.winfo_exists():
                    d.lift()
                    try:
                        d.focus_set()
                    except Exception:
                        pass
                    return d
                pid = st["pid"]
                is_builtin = providers.is_builtin(pid)
                editable = providers.builtin_base_editable(pid)
                pname = (providers.builtin(pid).get("name")
                         or vars_["name"].get().strip()
                         or pid or texts.SET_PROV_N_3)
                d = tk.Toplevel(win)
                conn_win[0] = d
                d.withdraw()                      # 摆正了再显示（坑 186 同款）
                d.title(texts.SET_CONN_TITLE % pname)
                theme.style_toplevel(d)
                d.transient(win)
                d.minsize(560, 0)
                body = ttk.Frame(d)
                body.pack(side="top", fill="both", expand=True, padx=14, pady=(12, 4))
                body.columnconfigure(1, weight=1)
                rows = {"i": 0}
                # 普通模式没有页级 "?"，新建用户进窗后看不到流程；这一行放在最上面。
                ttk.Label(body, text=texts.SET_CONN_N_1, style="Hint.TLabel",
                          wraplength=540, justify="left",
                          font=("Microsoft YaHei UI", 9)).grid(
                    row=rows["i"], column=0, columnspan=3, sticky="w",
                    padx=(0, 8), pady=(0, 6))
                rows["i"] += 1

                def lrow(label, widget, desc="", hint=""):
                    i = rows["i"]
                    rows["i"] += 1
                    if not simple and desc:
                        lab = tk.Frame(body, background=widgets.default_bg())
                        lab.grid(row=i, column=0, sticky="w", padx=(0, 8), pady=5)
                        ttk.Label(lab, text=label, width=12,
                                  anchor="w").pack(side="left")
                        widgets.HelpDot(lab, desc).pack(side="left", padx=(2, 0))
                    else:
                        ttk.Label(body, text=label, width=12, anchor="w").grid(
                            row=i, column=0, sticky="w", padx=(0, 8), pady=5)
                    widget.grid(row=i, column=1, sticky="w", padx=(0, 8), pady=5)
                    if hint:
                        ttk.Label(body, text=hint, style="Dim.TLabel",
                                  font=("Microsoft YaHei UI", 9)).grid(
                            row=i, column=2, sticky="w", pady=5)

                e_name = theme.entry(body, textvariable=vars_["name"], width=34)
                if is_builtin:
                    e_name.configure(state="disabled")
                lrow(texts.SET_PROV_A3, e_name, texts.SET_PROV_A4)

                e_base = theme.entry(body, textvariable=vars_["base_url"], width=46)
                if is_builtin and not editable:
                    e_base.configure(state="disabled")
                lrow("base_url", e_base, texts.SET_PROV_A4_2)

                # 可改地址的内置条目各自带一句"改成什么"（按条目挂，A 家例子不串到 B 家）
                note = providers.builtin_base_note(pid) if (is_builtin and editable) else ""
                note_lbl = None
                if note:
                    note_lbl = ttk.Label(body, text=note, style="Hint.TLabel",
                                         wraplength=540, justify="left",
                                         font=("Microsoft YaHei UI", 9))
                    note_lbl.grid(
                        row=rows["i"], column=0, columnspan=3, sticky="w",
                        padx=(0, 8), pady=(0, 4))
                    rows["i"] += 1

                e_media = theme.entry(body, textvariable=vars_["media_base_url"], width=46)
                if is_builtin:
                    e_media.configure(state="disabled")
                lrow(texts.SET_PROV_A3_2, e_media, texts.SET_PROV_A4_3)

                cb_media = theme.comb(body, textvariable=vars_["media_api"],
                                      state=("disabled" if is_builtin else "readonly"),
                                      width=22, values=[t for _c, t in _ma_opts])
                if not is_builtin:
                    cb_media.bind("<<ComboboxSelected>>", lambda *a: refresh_media())
                lrow(texts.SET_PROV_A2, cb_media, texts.SET_PROV_A4_4)

                # 推导结果 / 缺什么（原来在主页面上；低频，跟着字段一起收进来）
                ttk.Label(body, textvariable=media_lbl, style="Hint.TLabel",
                          wraplength=540, justify="left",
                          font=("Microsoft YaHei UI", 9)).grid(
                    row=rows["i"], column=0, columnspan=3, sticky="w",
                    padx=(0, 8), pady=(2, 6))
                rows["i"] += 1

                cb_en = None
                jobs_btn = None
                if not simple:
                    e_to = theme.entry(body, textvariable=vars_["timeout"], width=10)
                    lrow(texts.SET_PROV_A3_3, e_to, texts.SET_PROV_A4_5)
                    e_ctx = theme.entry(body, textvariable=vars_["ctx"], width=14)
                    lrow(texts.SET_PROV_A3_4, e_ctx,
                         texts.SET_PROV_CAT_34 % textfile.CLOUD_TOKEN_CAP)
                    cb_en = theme.check(body, text=texts.SET_PROV_N_21,
                                        variable=vars_["enabled"])
                    cb_en.grid(row=rows["i"], column=1, sticky="w", pady=4)
                    rows["i"] += 1
                    ttk.Separator(body, orient="horizontal").grid(
                        row=rows["i"], column=0, columnspan=3, sticky="we",
                        pady=(8, 4))
                    rows["i"] += 1
                    jr = ttk.Frame(body)
                    jr.grid(row=rows["i"], column=0, columnspan=3, sticky="w")
                    rows["i"] += 1
                    ttk.Label(jr, textvariable=jobs_lbl, style="Hint.TLabel",
                              wraplength=430, justify="left",
                              font=("Microsoft YaHei UI", 9)).pack(side="left")
                    jobs_btn = theme.button(jr, text=texts.SET_PROV_N_22, width=14,
                                            command=refresh_jobs)
                    jobs_btn.pack(side="left", padx=(8, 0))
                    # 「只查不提交」的核对出口（16 §24.1）：提交超时但对方可能已建单时，
                    # 先查一次再决定要不要重发 —— 重发就是第二条账单。
                    probe_btn = theme.button(jr, text=texts.SET_PROV_N_28, width=14,
                                             command=lambda: open_task_probe(win))
                    probe_btn.pack(side="left", padx=(8, 0))

                err_var = tk.StringVar(value="")
                ttk.Label(body, textvariable=err_var, style="ErrLit.TLabel",
                          wraplength=540, justify="left",
                          font=("Microsoft YaHei UI", 9)).grid(
                    row=rows["i"], column=0, columnspan=3, sticky="w")
                rows["i"] += 1

                def close():
                    conn_win[0] = None
                    try:
                        d.destroy()
                    except Exception:
                        pass

                def cancel():
                    # 丢弃未保存的改动：从配置重载；新建时退回原来的服务商
                    if new:
                        load("")
                        if new_prev[0]:
                            refresh_combo(keep=new_prev[0])
                    else:
                        load(pid)
                    close()

                def ok():
                    good, why = commit()
                    if not good:
                        err_var.set(why or texts.SET_PROV_A0_5)
                        return
                    close()
                    refresh_combo(keep=st["pid"])
                    if st["pid"]:
                        msg_lbl.set(texts.SET_PROV_CAT_10
                                    % (st["pid"], len(st["models"])))

                bf = ttk.Frame(d)
                bf.pack(side="bottom", fill="x", padx=14, pady=(4, 12))
                ok_btn = theme.button(bf, text=texts.BTN_OK, width=10, command=ok)
                ok_btn.pack(side="right")
                cancel_btn = theme.button(bf, text=texts.BTN_CANCEL, width=8,
                                          command=cancel)
                cancel_btn.pack(side="right", padx=6)
                # 稳定句柄：自检按"字段键 / 按钮角色"找控件，不按屏幕上的字找
                # （W 2026-10-17：文案由他随时改，测试别钉措辞；同坑 78 的 _sec_id 手法）
                d._conn_fields = {"name": e_name, "base_url": e_base,
                                  "media_base_url": e_media, "media_api": cb_media}
                if not simple:
                    d._conn_fields.update({"timeout": e_to, "ctx": e_ctx,
                                           "enabled": cb_en})
                d._conn_buttons = {"ok": ok_btn, "cancel": cancel_btn}
                d._conn_note = note_lbl
                d._conn_jobs_btn = jobs_btn
                d.protocol("WM_DELETE_WINDOW", cancel)
                d.bind("<Escape>", lambda _e: cancel())

                refresh_url()
                refresh_media()
                if not simple:
                    refresh_jobs()
                widgets.center_on(d, win)
                try:
                    if not is_builtin:
                        e_name.focus_set()
                    elif editable:
                        e_base.focus_set()
                    else:
                        d.focus_set()
                except Exception:
                    pass
                return d

            def do_delete():
                pid = st["pid"]
                if not pid:
                    msg_lbl.set(texts.SET_PROV_A0_6)
                    return
                if conn_win[0] is not None and conn_win[0].winfo_exists():
                    try:
                        conn_win[0].destroy()
                    except Exception:
                        pass
                    conn_win[0] = None
                tip = (texts.SET_PROV_CAT_11
                       % providers.provider_label_for(self.cfg, pid)) if providers.is_builtin(pid) \
                    else texts.SET_PROV_CAT_12 % providers.provider_label_for(self.cfg, pid, vars_["name"].get())
                if not messagebox.askyesno(texts.SET_PROV_A0_7,
                                           tip + texts.SET_PROV_CAT_13):
                    return
                providers.remove_provider(self.cfg, pid)
                secrets.set_api_key(pid, "")
                if self.cfg.get("model_provider") == pid:
                    # 正在用的云端模型被删了：回到本地模型，避免发送时才发现 provider 不存在
                    self.cfg["model_provider"] = providers.LOCAL
                    _d, chat, _i = scan_models(self.cfg)
                    if chat:
                        self.cfg["model"] = chat[0]
                    self._update_model_label()
                    self._render_status(self._server_alive_flag, self._server_ready_flag)
                save_config(self.cfg)
                refresh_combo()
                msg_lbl.set(texts.SET_PROV_A0_8)

            # ---------------- 密钥 ----------------
            def do_key_dialog():
                if not st["pid"]:
                    # 还没建服务商：先走连接配置窗（否则密钥会写到空 id 上）
                    open_conn_dialog(new=True)
                    return
                d = tk.Toplevel(win)
                theme.style_toplevel(d, "panel")   # 经典背景 + 标题栏两件套（坑 186）
                d.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
                d.title("API Key")
                d.transient(win)
                pname = (providers.builtin(st["pid"]).get("name")
                         or vars_["name"].get() or st["pid"] or texts.SET_PROV_N_3)
                widgets.panel_label(d, text=texts.SET_PROV_CAT_14 % pname,
                                    font=("Microsoft YaHei UI", 10, "bold")).pack(
                    anchor="w", padx=14, pady=(12, 4))
                widgets.panel_label(d, fg_key="hint", text=texts.SET_PROV_N_4,
                                    wraplength=420, justify="left",
                                    font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
                e = theme.entry(d, width=40, show="●")
                e.pack(padx=14, pady=10)
                tip = widgets.panel_label(d, textvariable=key_lbl, fg_key="hint",
                                          font=("Microsoft YaHei UI", 9))
                tip.pack(anchor="w", padx=14)
                bf = tk.Frame(d, background=widgets.panel_bg(d))
                bf.pack(padx=14, pady=(6, 14), anchor="e")

                def save():
                    k = e.get().strip()
                    if not k:
                        e.delete(0, "end")
                        d.destroy()
                        return
                    if not st["pid"]:
                        # 还没建起来的服务商：先按页面上填的名称/地址把它建起来，
                        # 密钥要有 id 才能落进 secrets.json
                        ok, err = commit(silent=True)
                        if not ok:
                            messagebox.showwarning("API Key", err or texts.SET_PROV_N_5)
                            return
                    secrets.set_api_key(st["pid"], k)
                    refresh_key()
                    msg_lbl.set(texts.SET_PROV_CAT_15 % st["pid"])
                    d.destroy()
                    # 填完密钥可能就凑齐了"第一个可用模型"（这台机器没装任何引擎的
                    # 常见情形）⇒ 立刻切过去（坑 150）
                    try:
                        self._maybe_adopt_first_model(force=True)
                    except Exception:
                        pass

                def clear():
                    if st["pid"] and messagebox.askyesno("API Key", texts.SET_PROV_CAT_16
                                                         % st["pid"]):
                        secrets.set_api_key(st["pid"], "")
                        refresh_key()
                    d.destroy()

                theme.button(bf, text=texts.SET_PROV_N_6, width=8, command=clear).pack(side="left", padx=4)
                theme.button(bf, text=texts.BTN_CANCEL, width=8, command=d.destroy).pack(side="left", padx=4)
                theme.button(bf, text=texts.SET_PROV_N_7, width=10, command=save).pack(side="left")
                e.focus_set()
                widgets.center_on(d, win)   # 摆到设置页正中，别落在屏幕左上角

            # ---------------- 模型选择界面 ----------------
            def open_picker(fetch=None, explain=""):
                """独立的模型勾选界面：勾中的才进主页面菜单；可手填模型名并"试一试"。"""
                if not st["pid"]:
                    messagebox.showwarning(texts.SET_PROV_A0_9, texts.SET_PROV_A1)
                    return
                pid = st["pid"]
                known = providers.catalog({"models": st["models"],
                                           "model_kinds": st["kinds"]})
                if fetch is None:
                    # W 定的规矩：拉过就缓存，除非用户点「刷新清单」，否则不重复请求
                    fetch = not known
                d = tk.Toplevel(win)
                d.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
                d.title(texts.SET_PROV_CAT_17
                        % (providers.get_provider(self.cfg, pid) or {}).get("name", pid))
                theme.style_toplevel(d)
                d.geometry("560x520")
                d.minsize(500, 380)
                d.transient(win)
                HDR = ("Microsoft YaHei UI", 9, "bold")
                rows = {}                       # 模型名 → [勾选 Var, 能力 Var, 图片输入 Var]

                def declare_now(m, label):
                    """改「图片输入」下拉即写声明并落盘。

                    以前只有点「确定」才生效：从「取消」或右上角 X 出去时，刚标好的「支持」
                    被静默丢掉，回到主页面照样被问"能不能看图"（W 报的 bug）。
                    能力声明与"勾哪些进菜单"不是同一件事，不该共用同一次提交。
                    """
                    capability.set_choice(self.cfg,
                                          capability.LABEL_CHOICE.get(label, capability.AUTO),
                                          providers.make_cloud_id(pid, m))
                    save_config(self.cfg)
                    self._update_model_label()
                # 顶部只放说明文字，按钮一律挪到底部：窄窗口里左右对撞会互相盖住
                head = ttk.Frame(d)
                head.pack(side="top", fill="x", padx=12, pady=(10, 2))
                hint_lbl = ttk.Label(head, text=texts.SET_PROV_N_8,
                                     style="Hint.TLabel", wraplength=520, justify="left",
                                     font=("Microsoft YaHei UI", 9))
                hint_lbl.pack(side="left")
                status = tk.StringVar(value=explain or
                                      (texts.SET_PROV_CAT_18
                                       % len(known)))
                st_lbl = ttk.Label(d, textvariable=status, style="Hint.TLabel",
                                   wraplength=600, justify="left",
                                   font=("Microsoft YaHei UI", 9))
                st_lbl.pack(side="top", anchor="w", padx=12, pady=(0, 2))

                wrap = ttk.Frame(d)
                wrap.pack(side="top", fill="both", expand=True, padx=12)
                # 经典 Canvas 默认背景是**纯白**（不跟主题）：浅色主题下窗底是系统灰，
                # 这块白就是"文字底下大量和底色不同的纯白"（W 2026-10-10 报）；深色下
                # 更是一整块白。与 ScrollPage 同款，显式染主题 frame 底色。
                cv = tk.Canvas(wrap, highlightthickness=0, borderwidth=0,
                               background=widgets.default_bg())
                sb = theme.scroll(wrap, orient="vertical", command=cv.yview)
                cv.configure(yscrollcommand=sb.set)
                sb.pack(side="right", fill="y")
                cv.pack(side="left", fill="both", expand=True)
                table = ttk.Frame(cv)
                inner = cv.create_window((0, 0), window=table, anchor="nw")
                # 滚轮（W 报的"选择模型页面好像不能用鼠标滚动"）：绑在这个窗口的顶层 +
                # 指针在列表区内才响应，所以不会把滚轮从表头/状态行或别的窗口抢走
                widgets.attach_wheel(cv)

                # 列宽先量后用：拍脑袋的数字若比控件真实宽度小，grid 会把列撑开，
                # 表头就和下面每行的下拉错位（这正是"比例失衡"的观感来源）
                _p_kind = theme.comb(table, state="readonly", width=9, values=[
                    providers.KIND_LABEL[k] for k in providers.KIND_ORDER])
                _p_img = theme.comb(table, state="readonly", width=9, values=[
                    capability.CHOICE_LABEL[c] for c in capability.CHOICES])
                _p_chk = ttk.Checkbutton(table, text="")
                table.update_idletasks()
                COL_W = max(_p_kind.winfo_reqwidth(), _p_img.winfo_reqwidth())
                CK_PAD = _p_chk.winfo_reqwidth()      # 勾选方框 + 内边距
                for _p in (_p_kind, _p_img, _p_chk):
                    _p.destroy()
                GAP = 6

                # ---- 表头钉在滚动区外面：行滚多少，标题都留在原位 ----
                # 标题不用 grid，照着第一行三个控件的真实 x 摆（place）：
                # 网格那点内部边距猜不准，量出来的位置才不会让表头和列错开
                hdr = ttk.Frame(d, height=20)
                hdr.pack(side="top", fill="x", padx=12, before=wrap)
                hdr_lbl = {txt: ttk.Label(hdr, text=txt, font=HDR)
                           for txt in (texts.COST_LBL_MODEL, texts.CHAT_IMAGE_INPUT, texts.SET_PROV_L2)}
                for txt in hdr_lbl:
                    hdr_lbl[txt].place(x=0, y=1)
                # 表头框高度按真实字号给：写死的数字在高 DPI 下会把标题切一半
                hdr.configure(height=max(l.winfo_reqheight() for l in hdr_lbl.values()) + 2)
                ttk.Separator(d, orient="horizontal").pack(
                    side="top", fill="x", padx=12, pady=(2, 3), before=wrap)

                table.columnconfigure(1, minsize=COL_W)
                table.columnconfigure(2, minsize=COL_W)
                table.columnconfigure(3, weight=1)   # 富余宽度交给这列空白，两列下拉贴着名字
                cells = {}                      # 模型名 → (勾选框, 图片输入下拉, 类型下拉)
                sizes = {"col0": 300, "need0": 200}   # 模型列：当前宽 / 名字真正需要的宽
                _fnt = tkfont.Font(family="Microsoft YaHei UI", size=9)
                labels = {}        # 模型名 → 缩写显示名（每次并进来新模型都整体重算，见 add_models）
                spec = []          # 显示顺序：[("hdr", 组键) | ("row", 模型名), ...]
                folds = {}         # 组键 → {"models": [...], "open": bool, "hdr": Label}

                _short_cache = {}        # (名字, 列宽) → 截断后的显示名；随窗口销毁

                def shorten(name, px):
                    """按列宽截断显示名；rows 的键仍是完整模型名，写配置不会错。

                    ttk.Checkbutton 在这个 Tk 版本上没有 anchor/justify/wraplength，
                    只能从"显示"这一侧解决：量着列宽裁。

                    两处提速（原来是逐字符往回退，一个 50 字的名字要问字体 50 次；
                    而 relabel() 每次拖动/加行都对全表重跑一遍）：
                      · 二分找截断点：log2(50)≈6 次字体度量，不是 50 次；
                      · 按 (名字, 列宽) 缓存：列宽没变就一次都不问字体。
                    缓存只活在这个窗口里、键含列宽，所以不存在"改了列宽还用旧值"。
                    """
                    px = max(px, 24)
                    key = (name, px)
                    hit = _short_cache.get(key)
                    if hit is not None:
                        return hit
                    if _fnt.measure(name) <= px:
                        _short_cache[key] = name
                        return name
                    lo, hi = 0, len(name)      # 最大的 k 使 name[:k]+"…" 放得下；0=一个都放不下
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if _fnt.measure(name[:mid] + "…") <= px:
                            lo = mid
                        else:
                            hi = mid - 1
                    out = (name[:lo] if lo else name[:1]) + "…"
                    _short_cache[key] = out
                    return out

                def label_px():
                    return sizes["col0"] - CK_PAD - 4

                def relabel():
                    """勾选框上的文字 = 缩写名，再按列宽做像素级截断（两道都过一遍）。"""
                    for m, (c, _i, _k) in cells.items():
                        c.configure(text=shorten(labels.get(m, m), label_px()))

                def align_hdr():
                    base = hdr.winfo_rootx()
                    if base <= 0 or not cells:
                        return
                    # 优先拿"真的显示出来的第一行"量；整页都收起时才临时挂回一行量一次
                    # （表头必须跟列对齐，而列位置只有格子里有真实控件时才量得出来）
                    cb = ic = kc = None
                    for ent in spec:
                        if ent[0] != "row":
                            continue
                        c = cells.get(ent[1])
                        if c and c[0].winfo_ismapped():
                            cb, ic, kc = c
                            break
                    was = cb is not None
                    if not was:
                        cb, ic, kc = next(iter(cells.values()))
                        for w in (cb, ic, kc):
                            w.grid()
                        table.update_idletasks()
                    for lbl, w in ((hdr_lbl[texts.COST_LBL_MODEL], cb), (hdr_lbl[texts.CHAT_IMAGE_INPUT], ic),
                                   (hdr_lbl[texts.SET_PROV_L2], kc)):
                        lbl.place(x=max(w.winfo_rootx() - base, 0), y=1)
                    if not was:
                        for w in (cb, ic, kc):
                            w.grid_remove()

                def on_resize(_e=None):
                    """按画布实际宽度定列宽：模型列"够用就好"，两个下拉列定宽。

                    以前只设 inner 宽度，长模型名会把右侧两列挤出容器（W 报的"溢出"）；
                    模型列拉满整行又会让名字和下拉之间空一大截，所以按最长的名字收口，
                    多出来的宽度交给第 4 列空白。

                    宽度早退：<Configure> 在拖动窗口时每秒触发几十次，而**纵向**拖动
                    根本不改变宽度 —— 那时候 itemconfigure/columnconfigure（会让整表
                    480 个控件重排）+ relabel（全表字体度量）全是白做的，实测拖动
                    135ms/帧（≈7fps）。所以三个输入都没变就直接返回。
                    need0 必须进指纹：add_models 收到更长的名字后会直接调本函数，
                    那时宽度没变但列宽该放宽。
                    """
                    avail = max(cv.winfo_width() - 4, 300)
                    inner_w = max(320, d.winfo_width() - 30)
                    sig = (avail, inner_w, sizes["need0"])
                    if sizes.get("sig") == sig:
                        return
                    sizes["sig"] = sig
                    sizes["col0"] = max(min(sizes["need0"], avail - 2 * (COL_W + GAP)), 110)
                    cv.itemconfigure(inner, width=avail)
                    table.columnconfigure(0, minsize=sizes["col0"])
                    st_lbl.configure(wraplength=inner_w)
                    hint_lbl.configure(wraplength=inner_w)
                    relabel()
                    cv.update_idletasks()
                    align_hdr()
                    bb = cv.bbox("all")
                    if bb:
                        cv.configure(scrollregion=bb)
                cv.bind("<Configure>", on_resize)

                def _mk_vars(m, kind, checked):
                    """建这一行的三个变量（只建一次；重排/折叠都不碰它们，状态自然留住）。"""
                    kv = tk.BooleanVar(value=bool(checked))
                    cv_kind = tk.StringVar(value=providers.KIND_LABEL[kind])
                    # 图片输入三态：自动判断 / 支持 / 不支持。云端 /models 不带能力字段，
                    # 只能由用户声明或实测一次；本地投影器判据可靠，默认停在"自动判断"。
                    try:
                        declared = capability.get_choice(
                            self.cfg, providers.make_cloud_id(pid, m))
                    except Exception:
                        declared = capability.AUTO
                    cv_img = tk.StringVar(
                        value=capability.CHOICE_LABEL.get(declared,
                                                          capability.CHOICE_LABEL[capability.AUTO]))
                    # 一改即落盘：不依赖用户点「确定」（见 declare_now 的说明）
                    cv_img.trace_add("write", lambda *a: declare_now(m, cv_img.get()))
                    rows[m] = [kv, cv_kind, cv_img]

                def _mk_widgets(m):
                    kv, cv_kind, cv_img = rows[m]
                    cb = ttk.Checkbutton(table, text=shorten(labels.get(m, m), label_px()),
                                         variable=kv)
                    ic = theme.comb(table, textvariable=cv_img, state="readonly", width=9,
                                      values=[capability.CHOICE_LABEL[c] for c in
                                              capability.CHOICES])
                    kc = theme.comb(table, textvariable=cv_kind, state="readonly",
                                      width=9,
                                      values=[providers.KIND_LABEL[k] for k in
                                              providers.KIND_ORDER])
                    cells[m] = (cb, ic, kc)

                def layout_spec():
                    """决定显示顺序：模型多的服务商按名字前缀收组。组头落在"组里第一个成员
                    原来所在的那一行"，成员紧跟组头之后 —— 不这样排就看不出组是从哪冒出来的。
                    **成员必须也进 spec**：regrid 是照 spec 逐行排格子的，只写组头的话展开时
                    没有控件被 grid 回来（第一版就漏在这儿）。

                    凑不成组的零散模型不再一条条平铺在顶上，而是统一收进末尾的「其他」组
                    （与主页面菜单同一套口径，W 提的）。收起 ≠ 藏起来：行照常建、勾选照常算，
                    `ok_apply` 收的是 `rows` 里的变量，跟折不折没关系。
                    """
                    names = list(rows)
                    many = len(names) > providers.FOLD_AT
                    groups, flat = providers.fold_groups(
                        names, at=1 if many else providers.FOLD_AT)
                    if many and flat:
                        groups = groups + [{"key": providers.REST_KEY, "models": list(flat)}]
                        flat = []
                    owner, members_of = {}, {}
                    for g in groups:
                        members_of[g["key"]] = list(g["models"])
                        for m in g["models"]:
                            owner[m] = g["key"]
                    out, emitted = [], set()
                    for m in names:
                        k = owner.get(m)
                        if k is None:
                            out.append(("row", m, [m]))       # 没建组的（含模型本来就少的）平铺
                            continue
                        if k in emitted:
                            continue                          # 成员已在组头后面排过
                        emitted.add(k)
                        out.append(("hdr", k, members_of[k]))
                        out += [("row", mm, [mm]) for mm in members_of[k]]
                    return out

                def owner_map():
                    out = {}
                    for ent in spec:
                        if ent[0] == "hdr":
                            for m in ent[2]:
                                out[m] = ent[1]
                    return out

                def toggle(key):
                    f = folds.get(key)
                    if f is None:
                        return
                    f["open"] = not f["open"]
                    f["hdr"].configure(text=group_text(key, f["models"], f["open"]))
                    regrid()

                def group_text(key, models, open_):
                    """组名带上"里面有没有我当前勾中的"：收起时也能看见这组有几个要用的。"""
                    n_on = sum(1 for m in models if rows[m][0].get())
                    return texts.MDL_SECTION_HEAD % ("▾" if open_ else "▸", key, len(models),
                                            (texts.SET_PROV_CAT_19 % n_on) if n_on else "")

                def sync_folds():
                    for ent in spec:
                        if ent[0] != "hdr":
                            continue
                        key, models = ent[1], ent[2]
                        f = folds.get(key)
                        if f is None:
                            lbl = tk.Label(table, text="", cursor="hand2",
                                           font=("Microsoft YaHei UI", 9, "bold"),
                                           background=widgets.default_bg(), anchor="w")
                            # 「其他」默认**展开**：它里面本来就是要露面的零散模型，
                            # 收起来等于在这份"选哪些进主页面"的清单里把它们藏了。
                            # （主页面菜单那边是 tk.Menu 的 cascade，天生收起，不冲突）
                            f = folds[key] = {"open": key == providers.REST_KEY,
                                              "hdr": lbl, "models": []}
                            lbl.bind("<Button-1>", lambda e, k=key: toggle(k))
                        f["models"] = list(models)
                        f["hdr"].configure(text=group_text(key, models, f["open"]))

                def regrid():
                    """按 spec + 折叠状态重排 grid 行号：**只挪格子不重建控件**。
                    成员一律先 grid 到它该在的行、再按折叠状态 `grid_remove()` ——
                    这样收起的行**也记得自己的格子配置**：`grid()` 不带参数恢复原位
                    （表头对齐要临时挂回一行来量坐标），配置没被赋过的话会全部落到
                    row0/col0，量回来的就是第一列的 x，表头跟着整排错位。"""
                    own = owner_map()
                    i = 0
                    for ent in spec:
                        if ent[0] == "hdr":
                            folds[ent[1]]["hdr"].grid(row=i, column=0, columnspan=4,
                                                      sticky="w", pady=(7, 1))
                            i += 1
                            continue
                        cb, ic, kc = cells[ent[1]]
                        cb.grid(row=i, column=0, sticky="w", pady=1)
                        ic.grid(row=i, column=1, sticky="w", padx=(GAP, 0), pady=1)
                        kc.grid(row=i, column=2, sticky="w", padx=(GAP, 0), pady=1)
                        if own.get(ent[1]) and not folds[own[ent[1]]]["open"]:
                            for w in (cb, ic, kc):
                                w.grid_remove()     # 行号照样占着：展开时原位回来
                        i += 1
                    table.update_idletasks()
                    bb = cv.bbox("all")
                    if bb:
                        cv.configure(scrollregion=bb)
                    align_hdr()
                    return i

                def add_models(items):
                    """items = [(模型名, 能力, 是否勾中)]。新增 + 重算缩写 + 重排整表。

                    缩写只在**可能撞名**时才整批重算：`short_labels` 最多跑 4 轮、
                    每轮对全表做一次正则压缩，加一个模型就把 119 个名字重压一遍是白付的。
                    判据是 short_labels 自己的规则——"撞车才逐级放宽"：新名字的最简形式
                    既不撞已有标签、彼此也不撞时，放宽档数不需要变，其余行的标签必然原样。
                    一旦撞了就整批重算（保持"批内不重名"这条不变量，见坑 84）。
                    """
                    fresh = [m for m, _k, _c in items if m not in rows]
                    for m, k, c in items:
                        if m not in rows:
                            _mk_vars(m, k, c)
                    if not labels:
                        labels.update(providers.short_labels(list(rows)))   # 首次建表
                    else:
                        cand = providers.short_labels(fresh) if fresh else {}
                        used = set(labels.values())
                        if len(set(cand.values())) != len(cand) or any(v in used for v in cand.values()):
                            labels.clear()
                            labels.update(providers.short_labels(list(rows)))
                        else:
                            labels.update(cand)
                    need = max([CK_PAD + _fnt.measure(labels[m]) + 10 for m in rows],
                               default=200)
                    grew = need > sizes["need0"]
                    sizes["need0"] = max(sizes["need0"], need)
                    for m in fresh:
                        _mk_widgets(m)
                    spec[:] = layout_spec()
                    sync_folds()
                    regrid()
                    if grew:
                        on_resize()       # 列宽到顶时 shorten 接手按像素截断

                add_models([(m, providers.model_kind_of(
                    {"model_kinds": st["kinds"]}, m), m in st["models"]) for m in known])
                on_resize()

                def pull(force=False):
                    """向接口拉 /models；拉到的补进列表（勾选状态保留）。"""
                    if not force and not fetch:
                        return
                    status.set(texts.SET_PROV_A0_10)

                    snap = provider_snapshot()          # Tk 变量只能在主线程读
                    key = secrets.get_api_key(pid)

                    def work():
                        okk, ids, text = cloud.list_models(snap, api_key=key)
                        self._ui_q.put(lambda: got(okk, ids, text))
                    threading.Thread(target=work, daemon=True).start()

                def got(okk, ids, text):
                    if not d.winfo_exists():
                        return
                    if not okk:
                        status.set(texts.SET_PROV_CAT_20
                                   % text)
                        return
                    added = 0
                    new = []
                    for m in ids:
                        if m not in rows:
                            new.append((m, providers.guess_kind(m), m in st["models"]))
                            added += 1
                    if new:
                        add_models(new)     # 一次并进来：整批重算缩写 + 重排一次
                    # 顶部说明已经讲过"勾中并点确定"，这里只报接口给了多少，不重复
                    status.set(texts.SET_PROV_CAT_21 % (len(ids), added))

                def try_add():
                    """"这名字服务端认不认"只有**对话模型**能零成本问出来。

                    生图 / 生视频的模型名走各家原生接口：拿 `chat/completions` 去试必然
                    `Model not exist`（服务端不认它是对话模型），而真正的媒体端点一试就是
                    真金白银（MiniMax 出图按张计费）。所以认出来不是文本模型时**不发那次
                    注定没意义的请求**，直接按名字收进表并说清为什么验不了。
                    """
                    m = e_new.get().strip()
                    if not m:
                        status.set(texts.SET_PROV_A0_11)
                        return
                    if providers.guess_kind(m) != "text":
                        manual_add(texts.SET_PROV_A0_12)
                        return
                    status.set(texts.SET_PROV_CAT_22 % m)

                    snap = provider_snapshot()          # 同上：主线程取值，子线程只用数据
                    key = secrets.get_api_key(pid)

                    def work():
                        okk, text = cloud.try_model(snap, api_key=key, model=m)
                        self._ui_q.put(lambda: tried(okk, m, text))
                    threading.Thread(target=work, daemon=True).start()

                def tried(okk, m, text):
                    if not d.winfo_exists():
                        return
                    if okk:
                        add_models([(m, providers.guess_kind(m), True)])
                        status.set(texts.SET_PROV_CAT_23 % (m, text))
                    else:
                        status.set("❌ %s：%s" % (m, text))

                def manual_add(why=""):
                    """生图 / 生视频的模型**不在** /models 清单里，也不能拿文本请求去试跑
                    （服务端会因"这个模型不能聊天"拒掉）。v40 接了 MiniMax/智谱/华为的
                    原生媒体接口后这条路必须开：按名字直接收进表，能力按名字给默认值，
                    用户在那一行右侧的下拉里改。`why` 是"为什么没走验证"的说明。
                    """
                    m = e_new.get().strip()
                    if not m:
                        status.set(texts.SET_PROV_A0_11)
                        return False
                    if m in rows:
                        status.set(texts.SET_PROV_CAT_24 % m)
                        return False
                    k = providers.guess_kind(m)
                    add_models([(m, k, True)])
                    extra = ""
                    if k != "text":
                        p = providers.get_provider(self.cfg, pid) or {}
                        if not providers.supports_media(p, k):
                            extra = texts.SET_PROV_CAT_25 \
                                % providers.KIND_LABEL[k]
                    status.set(texts.SET_PROV_CAT_26
                               % ((why + "，") if why else "", m,
                                  providers.KIND_LABEL[k], extra))
                    e_new.delete(0, "end")
                    return True

                # 底部三件套：手填行（含刷新/验证按钮）+ 确定取消，都排在表格之前分配空间，
                # 窗口再矮也是压表格，不会把按钮挤掉
                botf = ttk.Frame(d)
                botf.pack(side="bottom", fill="x", padx=12, pady=(4, 2), before=wrap)
                ttk.Label(botf, text=texts.SET_API_A1, style="Hint.TLabel",
                          font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 4))
                e_new = theme.entry(botf, width=16)
                e_new.pack(side="left", fill="x", expand=True)
                d._picker_e_new = e_new      # 稳定句柄：自检不按屏幕上的字找
                # 回车 = 直接加入。以前这个框一个绑定都没有：敲完名字按回车什么也不会发生，
                # 看起来就是"加不进去"（W 实测报的那条）。
                e_new.bind("<Return>", lambda _e: manual_add())
                e_new.bind("<KP_Enter>", lambda _e: manual_add())
                theme.button(botf, text=texts.SET_PROV_N_9, width=14,
                           command=try_add).pack(side="left", padx=(6, 0))
                theme.button(botf, text=texts.SET_PROV_N_10, width=10,
                           command=lambda: manual_add()).pack(side="left", padx=(6, 0))
                # 最下沿：左边两个动作按钮，右边确定/取消，一行装得下 470px 的最小宽度
                bf2 = ttk.Frame(d)
                bf2.pack(side="bottom", fill="x", padx=12, pady=(2, 12), before=botf)
                theme.button(bf2, text=texts.SET_PROV_N_11, width=9,
                           command=lambda: pull(True)).pack(side="left")
                theme.button(bf2, text=texts.SET_PROV_N_12, width=12,
                           command=lambda: do_verify()).pack(side="left", padx=(6, 0))
                theme.button(bf2, text=texts.BTN_CANCEL, width=8, command=d.destroy).pack(side="right")
                theme.button(bf2, text=texts.BTN_OK, width=10,
                           command=lambda: ok_apply(d)).pack(side="right", padx=6)
                # 媒体模型建议名单（C3）：各家 /models 不带媒体模型（坑 50），
                # 这里把**官方页核实过**的名字摆出来，点「填入」进手填框；
                # 仍不自动进菜单、不预置清单（11 §7.3 ① 不变）。
                _pf = providers.profile_for(providers.get_provider(self.cfg, pid) or {})
                _sug = list(_pf.get("suggest_models") or ())
                if _sug:
                    sugf = ttk.Frame(d)
                    sugf.pack(side="bottom", fill="x", padx=12, pady=(0, 2),
                              before=bf2)
                    ttk.Label(sugf, text=texts.SET_PROV_N_26, style="Hint.TLabel",
                              font=("Microsoft YaHei UI", 9)).pack(anchor="w")
                    _sug_row = ttk.Frame(sugf)
                    _sug_row.pack(fill="x", pady=(2, 0))
                    _sug_cb = theme.comb(_sug_row, state="readonly", width=30,
                                         values=_sug)
                    _sug_cb.pack(side="left", fill="x", expand=True)

                    def _fill_sug(_e=None):
                        m = str(_sug_cb.get() or "").strip()
                        if m:
                            e_new.delete(0, "end")
                            e_new.insert(0, m)
                            e_new.focus_set()

                    _sug_cb.bind("<<ComboboxSelected>>", _fill_sug)
                    _sug_cb.set(_sug[0])
                    d._picker_suggest = _sug_cb
                    theme.button(_sug_row, text=texts.SET_PROV_N_27, width=6,
                                 command=_fill_sug).pack(side="left", padx=(6, 0))

                def ok_apply(dlg):
                    picked, kinds = [], {}
                    for m, (kv, kvar, _ivar) in rows.items():
                        k = [x for x in providers.KIND_ORDER
                             if providers.KIND_LABEL[x] == kvar.get()][0]
                        kinds[m] = k
                        if kv.get():
                            picked.append(m)
                    apply_declarations()
                    st["models"] = picked
                    st["kinds"].update(kinds)
                    commit(silent=True)      # save_config 顺带把 model_image_input 落盘
                    refresh_menu_list()
                    # 别把勾中的模型名全拼进这个 wraplength=680 的 Label：119 个原始名
                    # 是 3440 字，实测把 Label 撑到 1948px 高、逼整个滚动区重排，
                    # 点「确定」后的整窗重绘要 739ms（只报数量+头两个 = 18ms）。
                    # 完整清单本来就在上面那个 Listbox 里，一条都没少。
                    # 缩写直接复用 add_models 已经算好的 labels，不再重算一遍。
                    if not picked:
                        msg_lbl.set(texts.SET_PROV_A0_13)
                    else:
                        msg_lbl.set(texts.SET_PROV_CAT_27
                                    % (len(picked),
                                       "、".join(labels.get(m, m) for m in picked[:2]),
                                       " …" if len(picked) > 2 else ""))
                    dlg.destroy()

                def apply_declarations():
                    """把每行的「图片输入」三态写进配置；选"自动判断"= 删记录、回到判据。"""
                    for m, (_kv, _kvar, ivar) in rows.items():
                        choice = capability.LABEL_CHOICE.get(ivar.get(), capability.AUTO)
                        capability.set_choice(self.cfg, choice,
                                              providers.make_cloud_id(pid, m))

                def do_verify():
                    """实测：发一张 1x1 图问服务端收不收。比按名字猜可靠，但要花一次最小请求。"""
                    targets = [m for m, (kv, _k, _i) in rows.items() if kv.get()][:5]
                    if not targets:
                        status.set(texts.SET_PROV_A0_14)
                        return
                    key = secrets.get_api_key(pid)
                    snap = provider_snapshot()
                    status.set(texts.SET_PROV_CAT_28
                               % len(targets))

                    def work():
                        out = []
                        for m in targets:
                            try:
                                out.append((m,) + cloud.probe_image_input(
                                    self.cfg, provider=snap, api_key=key, model=m))
                            except Exception as e:
                                out.append((m, capability.UNKNOWN, texts.SET_PROV_CAT_29 % e))
                        self._ui_q.put(lambda: verified(out))
                    threading.Thread(target=work, daemon=True).start()

                def verified(results):
                    if not d.winfo_exists():
                        return
                    lines = []
                    for m, v, why in results:
                        if v in (capability.YES, capability.NO):
                            lab = capability.CHOICE_LABEL[v]
                            rows[m][2].set(lab)
                            lines.append("%s → %s" % (m, lab))
                        else:
                            lines.append(texts.SET_PROV_CAT_30 % (m, why))
                    status.set(texts.SET_PROV_CAT_31 + "；".join(lines)
                               + texts.SET_PROV_CAT_32)

                # 拖动门控（W 2026-10-10）：云端模型清单长了拖动同样卡，做法与设置窗一致
                widgets.ResizeDeferGate(
                    d,
                    lambda: (cv.pack_forget(), sb.pack_forget()),
                    lambda: (sb.pack(side="right", fill="y"),
                             cv.pack(side="left", fill="both", expand=True)))
                widgets.center_on(d, win)   # 摆到设置页正中（控件都建完了，尺寸才量得准）
                if fetch:
                    pull()

            def do_pick_models():
                open_picker()          # 打开就用缓存；要重新请求请点「刷新清单」

            def do_remove_selected():
                sel = lb.curselection()
                if not sel:
                    msg_lbl.set(texts.SET_PROV_A0_15)
                    return
                # 反查原名（列表里是缩写显示行，配置里是原名 —— 坑 131）
                m = lb_map.get(lb.get(sel[0]))
                if not m:
                    msg_lbl.set(texts.SET_PROV_A0_16)
                    return
                st["models"] = [x for x in st["models"] if x != m]
                commit(silent=True)
                refresh_menu_list()
                msg_lbl.set(texts.SET_PROV_CAT_33
                            % providers.short_of(m))

            # ---------------- 连通性 ----------------
            def do_test():
                if not ensure_saved():
                    return
                key = secrets.get_api_key(st["pid"])
                if not key:
                    msg_lbl.set(texts.SET_PROV_A0_17)
                    return
                if not st["models"]:
                    # 还没勾任何模型：没有文本模型可测，但拉清单同样能验 key。
                    # 直接把「选择模型」窗打开并拉一次，用户勾完就能用。
                    # 主页面状态行留空：进度与结果由选择窗的状态行说，别关窗后挂着一句旧话。
                    msg_lbl.set("")
                    open_picker(fetch=True)
                    return
                msg_lbl.set(texts.SET_PROV_A0_18)
                t4b.update_idletasks()

                p = providers.get_provider(self.cfg, st["pid"]) or provider_snapshot()
                # 主页面当前选中的就是这家的云文本模型时，优先测它（用户实际在用的那个）；
                # 否则交给 providers.test_connection_model 自动挑常见聊天模型。
                _pref = ""
                if providers.is_cloud(self.cfg)                         and str(self.cfg.get("model_provider")) == st["pid"]:
                    _sp = providers.split_cloud_id(self.cfg.get("model", ""))
                    if _sp and providers.model_kind_of(
                            {"model_kinds": st["kinds"]}, _sp[1]) == providers.KIND_TEXT:
                        _pref = _sp[1]

                def work():
                    ok, text = cloud.probe({**p, "models": st["models"],
                                            "model_kinds": st["kinds"]}, api_key=key,
                                           model=_pref or None)
                    def after():
                        if not win.winfo_exists():
                            return          # 设置窗口已关：别弹孤立的对话框
                        msg_lbl.set(("✅ " if ok else "❌ ") + text)
                        if ok:
                            # 2026-10-03（W）：服务商填完、连接测通就自动开「选择模型」，
                            # 不再问一句「现在要选模型吗」—— 顺带把清单重拉一次，
                            # 免得刚填好的服务商配着上一家留下的旧缓存清单。
                            open_picker(
                                fetch=True,
                                explain=texts.SET_PROV_N_13)
                        else:
                            messagebox.showwarning(texts.SET_PROV_A0_19, text)
                    self._ui_q.put(after)
                threading.Thread(target=work, daemon=True).start()

            def refresh_combo(keep=None):
                """下拉只列显示名，pid 留在背后用（把代码 id 摆到界面上，用户认不出哪家）。"""
                pairs = providers.provider_labels(self.cfg)
                combo["values"] = [lb for lb, _p in pairs] + [NEW_PROVIDER_LABEL]
                by_pid = {p: lb for lb, p in pairs}
                pid = keep if keep in by_pid else (pairs[0][1] if pairs else "")
                combo.set(by_pid.get(pid, NEW_PROVIDER_LABEL))
                load(pid)

            def on_pick(_e=None):
                sel = combo.get()
                pid = {lb: p for lb, p in providers.provider_labels(self.cfg)}.get(sel, "")
                # 切换服务商 = 放弃上一个窗里没保存的改动（与「取消」同一语义）
                if conn_win[0] is not None and conn_win[0].winfo_exists():
                    try:
                        conn_win[0].destroy()
                    except Exception:
                        pass
                    conn_win[0] = None
                if not pid:
                    # 「＋ 新建服务商…」：主页面已经没有可填的连接字段，直接弹窗
                    # （W 2026-10-17 批准；普通/高级同一口径）
                    new_prev[0] = st["pid"] or ""
                    load("")
                    open_conn_dialog(new=True)
                    return
                load(pid)

            bar = ttk.Frame(t4b)
            bar.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4b["i"] += 1
            ttk.Label(bar, text=texts.SET_PROV_N_14).pack(side="left")
            combo = theme.comb(bar, width=24, state="readonly")
            combo.pack(side="left", padx=(0, 6))
            combo.bind("<<ComboboxSelected>>", on_pick)
            t4b._prov_combo = combo
            conn_btn[0] = theme.button(
                bar, text=texts.SET_PROV_N_24, width=12,
                command=lambda: open_conn_dialog(new=not st["pid"]))
            conn_btn[0].pack(side="left", padx=3)
            t4b._conn_btn = conn_btn[0]      # 稳定句柄：自检不按按钮文字找（坑 78 手法）
            delete_btn = None
            if not simple:
                # 「删除」是管理动作，普通用户模式不出现（测试连接按 W 的口径保留）
                delete_btn = theme.button(bar, text=texts.SET_PROV_N_15, width=6,
                                          command=do_delete)
                delete_btn.pack(side="left", padx=3)
            t4b._delete_btn = delete_btn
            test_btn = theme.button(bar, text=texts.SET_PROV_N_16, width=10,
                                    command=do_test)
            test_btn.pack(side="left", padx=3)
            t4b._test_btn = test_btn

            kr = ttk.Frame(t4b)
            kr.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4b["i"] += 1
            key_btn = theme.button(kr, text=texts.SET_PROV_N_17, width=20,
                                   command=do_key_dialog)
            key_btn.pack(side="left")
            t4b._key_btn = key_btn
            # 原先这里还有一个「配置本地 API 地址」按钮跳去API 页（W 2026-10-03 要求移除）：
            # 云端这一页只管密钥与模型，混一个"去改本地端口"的入口只会让人以为两者相关。
            # 本地端口在左栏「本地模型 API」那一页。
            # 密钥状态单独一行：它跟着按钮排在同一行时，掩码文本会把这行撑得比
            # 可视区宽（横向不可滚 = 后面的内容看不见）
            key_widget = ttk.Label(t4b, textvariable=key_lbl, style="Hint.TLabel",
                                   wraplength=560, justify="left",
                                   font=("Microsoft YaHei UI", 9))
            key_widget.grid(row=r4b["i"], column=0, columnspan=3, sticky="w",
                            pady=(0, 6))
            t4b._key_lbl = key_widget
            r4b["i"] += 1

            # 连接字段（名称 / base_url / 原生接口地址 / 原生接口协议）已收进
            # 「连接配置…」二级窗（2026-10-17，专题 A）。主页面不再有内联输入行，
            # 也不再随选中项临时展开 —— 那种"页面形态跟着下拉跳"的观感一并消失。

            mf = ttk.Frame(t4b)
            mf.grid(row=r4b["i"], column=1 if not simple else 0,
                    columnspan=2 if not simple else 3, sticky="w", pady=(2, 4))
            r4b["i"] += 1
            lb = theme.tint(tk.Listbox(mf, height=5, width=34, exportselection=False,
                                       font=("Microsoft YaHei UI", 9),
                                       background=theme.c("bg"),
                                       foreground=theme.c("body")),
                            fg="body", bg="bg")
            # 经典 Listbox 默认**白底**（不跟主题）：浅色主题下窗底是系统灰，清单区
            # 一片纯白就是 W 报的"文字底下大量和底色不同的纯白色"（深色下更刺眼）。
            # tint 盖章的写法与整理预览的 Text 同款：切主题时 retint 重涂。
            if not simple:
                lb.pack(side="left")
            # lb 在普通模式下不摆出来但**照建**：refresh_menu_list / do_remove_selected
            # 都操作它，对没映射的 Listbox 做 delete/insert 完全合法
            btns = ttk.Frame(mf)
            btns.pack(side="left", padx=(6, 0) if not simple else (0, 0), fill="y")
            pick_btn = theme.button(btns, text=texts.SET_PROV_N_19, width=12,
                                    command=do_pick_models)
            pick_btn.pack(anchor="w", pady=1)
            t4b._pick_btn = pick_btn
            remove_btn = refresh_btn = None
            if not simple:
                remove_btn = theme.button(btns, text=texts.SET_PROV_N_20, width=12,
                                          command=do_remove_selected)
                remove_btn.pack(anchor="w", pady=1)
                refresh_btn = theme.button(
                    btns, text=texts.SET_PROV_N_11, width=12,
                    command=lambda: (commit(silent=True), open_picker(fetch=True)))
                refresh_btn.pack(anchor="w", pady=1)
            t4b._remove_btn = remove_btn
            t4b._refresh_btn = refresh_btn

            if not simple:
                ttk.Label(t4b, textvariable=menu_lbl, style="Hint.TLabel", wraplength=430,
                          justify="left", font=("Microsoft YaHei UI", 9)).grid(
                    row=r4b["i"], column=1, columnspan=2, sticky="w")
            r4b["i"] += 1
            if not simple:
                ttk.Label(t4b, textvariable=cat_lbl, style="Hint.TLabel", wraplength=430,
                          justify="left", font=("Microsoft YaHei UI", 9)).grid(
                    row=r4b["i"], column=1, columnspan=2, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            # 「每轮显示用量」与「让云端模型自选读取区间」属于云端**文本**对话，
            # 移到本区块之后（左栏「云端模型 → 文本模型」），别混在服务商清单里。
            # 超时 / 上下文窗口 / 启用 / 任务台账也随连接字段一起进了二级窗
            # （高级模式才有；普通模式照旧不出现）。

            def save_page():
                """「保存本页」= 通用档位 + 本页 provider 字段，和底部「保存」同一条路。

                以前只走 commit()，站在这页改了云端档位再点「保存本页」会被静默丢掉
                （底部按钮才写回），是个说不通的差别。
                """
                ok, why = self._apply_settings(v)
                if not ok:
                    msg_lbl.set(why)
                    return
                commit()
                refresh_combo(keep=st["pid"])

            if not simple:
                sf = ttk.Frame(t4b)
                sf.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(4, 4))
                r4b["i"] += 1
                save_page_btn = theme.button(sf, text=texts.SET_PROV_N_23, width=12,
                                             command=save_page)
                save_page_btn.pack(side="left")
                t4b._save_page_btn = save_page_btn
                ttk.Label(sf, textvariable=url_lbl, style="Hint.TLabel", wraplength=430,
                          justify="left", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)
            # 蓝色状态行两种模式都留：测试连接 / 选模型的结果要有地方说话
            ttk.Label(t4b, textvariable=msg_lbl, style="AccentLit.TLabel", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(2, 0))
            r4b["i"] += 1

            def _t4b_save():
                # 连接配置窗还开着：以窗内的「确定 / 取消」为准 —— 从背后点底部
                # 「保存」时不能把窗里尚未确认的改动偷偷提交（「取消」= 丢弃）。
                if conn_win[0] is not None and conn_win[0].winfo_exists():
                    return True, ""
                return commit(silent=True)
            save_hooks[t4b] = _t4b_save
            refresh_combo()
            refresh_jobs()
        # ---- 区块 6b：云端 / 文本模型（对话行为与附件预算，跟具体服务商无关）----
        @section("cloud_t", "ctext")
        def _t4d(t4d, r4d):
            """云端文本对话的开关：思考过程（独立存储）+ 用量显示 + 附件自选区间。

            「展示思考过程」与本地模型的那个**完全独立**（2026-10-07 W 定）：
            本地存 cfg["show_reasoning"]、云端存 cfg["cloud_show_reasoning"]，
            渲染侧按本轮链路分流（ui/app.py 的 _poll + _turn_cloud）。
            """
            i = r4d["i"]
            r4d["i"] += 1
            v["cloud_show_reasoning"] = tk.BooleanVar(
                value=bool(self.cfg.get("cloud_show_reasoning", True)))
            theme.check(t4d, text=texts.SET_CTEXT_N,
                            variable=v["cloud_show_reasoning"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=6)
            if simple:
                # 普通用户模式：这一页只留上面那一个开关（2026-10-07 W 定）
                return

            i = r4d["i"]
            r4d["i"] += 1
            v["show_usage"] = tk.BooleanVar(value=bool(self.cfg.get("show_usage", True)))
            # 勾选框文字长，必须跨列放：占在 column=1 上会把整列撑宽，
            # 于是右侧"短摘要 + ?"那一格被 grid 挤扁（版式自检抓到过）
            theme.check(t4d, text=texts.SET_CTEXT_N_2,
                            variable=v["show_usage"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=6)

            i = r4d["i"]
            r4d["i"] += 1
            v["cloud_file_model_decides"] = tk.BooleanVar(
                value=bool(self.cfg.get("cloud_file_model_decides", False)))
            theme.check(
                t4d, text=texts.SET_CTEXT_N_3,
                variable=v["cloud_file_model_decides"]).grid(
                row=i, column=0, columnspan=2, sticky="w", pady=(0, 6))
            widgets.HelpDot(t4d,
                            texts.SET_CTEXT_A1
                            ).grid(row=i, column=2, sticky="e", pady=(0, 6))

            _row(t4d, r4d, texts.SET_CTEXT_A2, ttk.Label(
                t4d, text=texts.SET_CTEXT_N_4, style="Dim.TLabel"),
                texts.SET_CTEXT_A4,
                hint=texts.SET_CTEXT_N_5)

            # 云端文本自己的成本估算（2026-10-07 W 定：三条云端链路各一个入口、
            # 单价表与计费单位各自独立；文本这条只在高级模式出现）
            fc = ttk.Frame(t4d)
            theme.button(fc, text=texts.SET_CTEXT_N_6, width=14,
                       command=lambda: self.open_cost_window(
                           providers.KIND_TEXT)).pack(side="left")
            _row(t4d, r4d, texts.SET_CTEXT_A2_2, fc,
                 texts.SET_CTEXT_A4_2)
        # ---- 区块 7：云端 / 生图与生视频（服务商原生接口，与本地 sd.cpp 无关）----
        @section("cloud_m", "cmedia")
        def _t4c(t4c, r4c):
            """云端生图 / 生视频的档位与落地目录。

            **左右两列**：左列生图、右列生视频，各自的存放目录与「打开文件夹」放在**本列底部**
            （原先目录两行夹在中间，扫一眼看不出哪条属于哪边）。
            轮询间隔与等待上限是**两条链路共用**的（生图万一回的是 task_id 也会就地转轮询），
            所以不属于任何一列，压在两列下面一条横栏里。
            费用单价不在这里填：它按模型走，「成本估算」按钮两列各一个（各查各的表）。
            """
            cols = ttk.Frame(t4c)
            cols.grid(row=r4c["i"], column=0, columnspan=3, sticky="nw")
            r4c["i"] += 1
            col_l = ttk.Frame(cols)
            col_l.grid(row=0, column=0, sticky="nw")
            col_r = ttk.Frame(cols)
            col_r.grid(row=0, column=1, sticky="nw", padx=(30, 0))
            for f, name in ((col_l, texts.IMG_CLOUD_TAG), (col_r, texts.VID_CONFIRM_TITLE)):
                ttk.Label(f, text=name, font=("Microsoft YaHei UI", 10, "bold")).grid(
                    row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
            rl, rr = {"i": 1}, {"i": 1}
            # 按主页面当前选中的云端模型回显"这家是什么"；没选就按服务商默认
            _, _, _pf_img, _cap_img = _media_cap_line(
                col_l, rl, providers.KIND_IMAGE, lw=8)
            _, _, _pf_vid, _cap_vid = _media_cap_line(
                col_r, rr, providers.KIND_VIDEO, lw=8)
            t4c._cap_img_lbl = _cap_img
            t4c._cap_vid_lbl = _cap_vid

            _size_entry(col_l, rl, "cloud_img_size", texts.SET_CMEDIA_A3,
                texts.SET_CMEDIA_A4,
                width=11, hint=texts.SET_LMIMG_N, lw=8, warn_text=SIZE_WARN_RES,
                allow=lambda t: ":" in str(t).replace("：", ":"))
            _neg_e, _neg_cell = ent(col_l, rl, "cloud_img_negative",
                texts.SET_CMEDIA_A3_2, texts.SET_CMEDIA_A4_2, lw=8,
                hint=texts.SET_LMIMG_N_8)
            t4c._neg_entry = _neg_e
            t4c._neg_hint = _neg_cell
            if _pf_img:
                if _pf_img.get("img_negative") is False:
                    # 这家没有该字段：旧值不留在配置里（否则置灰框里挂着一句
                    # 永远不会发出去的话），提交侧另有"已忽略"的回显兜老配置。
                    v["cloud_img_negative"].set("")
                    _neg_e.configure(state="disabled")
                    if _neg_cell is not None:
                        _neg_cell.configure(text=texts.SET_CMEDIA_N_9)
                elif _pf_img.get("img_negative") is None:
                    if _neg_cell is not None:
                        _neg_cell.configure(text=texts.SET_CMEDIA_N_10)
            ent(col_l, rl, "cloud_img_dir", texts.SET_CMEDIA_A3_3,
                texts.SET_CMEDIA_A4_3, lw=8)
            fl = ttk.Frame(col_l)
            theme.button(fl, text=texts.SET_CMEDIA_N, width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "image"),
                           texts.SET_CMEDIA_A1,
                           texts.SET_CMEDIA_A2)).pack(side="left")
            row(col_l, rl, texts.SET_OPEN_OUTDIR_A0, fl,
                texts.SET_CMEDIA_A4_4, lw=8)
            # 成本估算按链路拆开（2026-10-07 W 定）：生图 / 生视频各一个按钮、
            # 各一张单价表、各自的计费单位（原来两条链路共用一个「成本预估算」）
            flc = ttk.Frame(col_l)
            theme.button(flc, text=texts.SET_CTEXT_N_6, width=14,
                       command=lambda: self.open_cost_window(
                           providers.KIND_IMAGE)).pack(side="left")
            row(col_l, rl, texts.SET_CTEXT_A2_2, flc,
                texts.SET_CMEDIA_A4_5, lw=8)

            _res_vals = tuple((_pf_vid or {}).get("vid_res") or ())
            if _res_vals:
                _res_w, _ = _combo_entry(col_r, rr, "cloud_video_resolution",
                    texts.SET_CMEDIA_A3_4, texts.SET_CMEDIA_A4_6,
                    _res_vals, width=10, hint=texts.SET_LMIMG_N_7, lw=8)
            else:
                _res_w, _ = _size_entry(col_r, rr, "cloud_video_resolution",
                    texts.SET_CMEDIA_A3_4, texts.SET_CMEDIA_A4_6,
                    hint=texts.SET_LMIMG_N_7, lw=8, width=8, warn_text=SIZE_WARN_RES,
                    allow=_vidres_allow)
            t4c._video_res_widget = _res_w
            _dur = (_pf_vid or {}).get("vid_duration")
            _dur_hint = (texts.SET_CMEDIA_N_12 % (int(_dur[0]), int(_dur[1]))
                         if _dur else texts.SET_CMEDIA_N_2)
            if _dur:
                _dur_w, _dur_c = _combo_entry(
                    col_r, rr, "cloud_video_duration", texts.SET_CMEDIA_A3_5,
                    texts.SET_CMEDIA_A4_7,
                    [str(i) for i in range(int(_dur[0]), int(_dur[1]) + 1)],
                    width=10, hint=_dur_hint, lw=8)
            else:
                _dur_w, _dur_c = ent(col_r, rr, "cloud_video_duration",
                    texts.SET_CMEDIA_A3_5, texts.SET_CMEDIA_A4_7,
                    hint=_dur_hint, lw=8)
            t4c._video_duration_widget = _dur_w
            t4c._video_duration_hint = _dur_c
            _ratio_vals = tuple((_pf_vid or {}).get("vid_ratio") or ())
            if _ratio_vals:
                _ratio_w, _ = _combo_entry(col_r, rr, "cloud_video_ratio",
                    texts.SET_CMEDIA_A3_6, texts.SET_CMEDIA_A4_8,
                    _ratio_vals, width=10, hint=texts.SET_LMIMG_N_8, lw=8)
            else:
                _ratio_w, _ = ent(col_r, rr, "cloud_video_ratio",
                    texts.SET_CMEDIA_A3_6, texts.SET_CMEDIA_A4_8,
                    hint=texts.SET_LMIMG_N_8, lw=8)
            t4c._video_ratio_widget = _ratio_w
            ent(col_r, rr, "cloud_vid_dir", texts.SET_CMEDIA_A3_7,
                texts.SET_CMEDIA_A4_9, lw=8)
            fr = ttk.Frame(col_r)
            theme.button(fr, text=texts.SET_CMEDIA_N_3, width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "video"),
                           texts.SET_CMEDIA_A1_2,
                           texts.SET_CMEDIA_A2_2)).pack(side="left")
            row(col_r, rr, texts.SET_OPEN_OUTDIR_A0, fr,
                texts.SET_CMEDIA_A4_10, lw=8)
            frc = ttk.Frame(col_r)
            theme.button(frc, text=texts.SET_CTEXT_N_6, width=14,
                       command=lambda: self.open_cost_window(
                           providers.KIND_VIDEO)).pack(side="left")
            row(col_r, rr, texts.SET_CTEXT_A2_2, frc,
                texts.SET_CMEDIA_A4_11, lw=8)

            ttk.Separator(t4c).grid(row=r4c["i"], column=0, columnspan=3,
                                    sticky="we", pady=(12, 4))
            r4c["i"] += 1
            i = r4c["i"]
            r4c["i"] += 1
            tk.Label(t4c, text=texts.SET_CMEDIA_N_4, foreground=theme.c("dim"),
                     font=("Microsoft YaHei UI", 9, "bold"),
                     background=widgets.default_bg()).grid(
                row=i, column=0, columnspan=3, sticky="w")
            # 产物位置（2026-10-06）：本地与云端四条链路共用的落地根。
            # 标签 6 个字超出共用栏 lw=8 的宽度（每个中文字占 2 格，坑 113 同族），这一行收到 12
            ent(t4c, r4c, "output_dir", texts.SET_CMEDIA_A3_8,
                texts.SET_CMEDIA_A4_12,
                width=24, hint=texts.SET_CMEDIA_N_5, lw=12)
            ent(t4c, r4c, "cloud_poll_seconds", texts.SET_CMEDIA_A3_9,
                texts.SET_CMEDIA_A4_13, hint=texts.SET_CMEDIA_N_6, lw=8)
            ent(t4c, r4c, "cloud_wait_minutes", texts.SET_CMEDIA_A3_10,
                texts.SET_CMEDIA_A4_14,
                lw=8)
            # 原来这里有一条共用的「费用单价 → 成本预估算」：2026-10-07 起成本估算
            # 按链路拆开（W 定），按钮分别挂在左右两列的底部（各自的单价表与计费单位）

        # ---- 区块 7b / 7c（**普通用户模式专有**）：云端生图 / 云端生视频各自一页 ----
        # 高级模式用的是上面 cmedia 那一屏（左右两列合一页）；普通模式把两条链路拆成
        # 两个导航项，各留 W 点名的几件（2026-10-07）。区块键 cimg / cvid 只出现在
        # SIMPLE_NAV_SPEC 里（tools/check_structure.py 的 [2] 段按两份规格一起判死区块）。
        @section("cloud_m", "cimg")
        def _t4e(t4e, r4e):
            _media_cap_line(t4e, r4e, providers.KIND_IMAGE, colspan=3, wraplength=560)
            _size_entry(t4e, r4e, "cloud_img_size", texts.SET_LMIMG_A3,
                texts.SET_CIMG_A4,
                width=11, hint=texts.SET_LMIMG_N,
                allow=lambda t: ":" in str(t).replace("：", ":"))
            ent(t4e, r4e, "cloud_img_dir", texts.SET_OPEN_OUTDIR_A0,
                texts.SET_CIMG_A4_2,
                width=30, hint=texts.SET_LMIMG_N_2)
            fl = ttk.Frame(t4e)
            theme.button(fl, text=texts.SET_LMIMG_N_3, width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "image"),
                           texts.SET_CMEDIA_A1,
                           texts.SET_CIMG_A2)).pack(side="left")
            row(t4e, r4e, "", fl,
                texts.SET_CIMG_A4_3)
            fc = ttk.Frame(t4e)
            theme.button(fc, text=texts.SET_CTEXT_N_6, width=14,
                       command=lambda: self.open_cost_window(
                           providers.KIND_IMAGE)).pack(side="left")
            row(t4e, r4e, texts.SET_CTEXT_A2_2, fc,
                texts.SET_CMEDIA_A4_5)

        @section("cloud_m", "cvid")
        def _t4f(t4f, r4f):
            _, _, _pf_vid, _cap_vid = _media_cap_line(
                t4f, r4f, providers.KIND_VIDEO, colspan=3, wraplength=560)
            t4f._cap_vid_lbl = _cap_vid
            _res_vals = tuple((_pf_vid or {}).get("vid_res") or ())
            if _res_vals:
                _combo_entry(t4f, r4f, "cloud_video_resolution", texts.SET_LMIMG_A3,
                    texts.SET_CVID_A4, _res_vals, width=10,
                    hint=texts.SET_LMIMG_N_7)
            else:
                _size_entry(t4f, r4f, "cloud_video_resolution", texts.SET_LMIMG_A3,
                    texts.SET_CVID_A4,
                    hint=texts.SET_LMIMG_N_7, width=8, warn_text=SIZE_WARN_RES,
                    allow=_vidres_allow)
            _dur = (_pf_vid or {}).get("vid_duration")
            _dur_hint = (texts.SET_CMEDIA_N_12 % (int(_dur[0]), int(_dur[1]))
                         if _dur else texts.SET_CMEDIA_N_2)
            if _dur:
                _combo_entry(
                    t4f, r4f, "cloud_video_duration", texts.SET_CVID_A3,
                    texts.SET_CVID_A4_2,
                    [str(i) for i in range(int(_dur[0]), int(_dur[1]) + 1)],
                    width=10, hint=_dur_hint)
            else:
                ent(t4f, r4f, "cloud_video_duration", texts.SET_CVID_A3,
                    texts.SET_CVID_A4_2, hint=_dur_hint)
            _ratio_vals = tuple((_pf_vid or {}).get("vid_ratio") or ())
            if _ratio_vals:
                _combo_entry(t4f, r4f, "cloud_video_ratio", texts.SET_CVID_A3_2,
                    texts.SET_CMEDIA_A4_8, _ratio_vals, width=10,
                    hint=texts.SET_LMIMG_N_8)
            else:
                ent(t4f, r4f, "cloud_video_ratio", texts.SET_CVID_A3_2,
                    texts.SET_CMEDIA_A4_8, hint=texts.SET_LMIMG_N_8)
            ent(t4f, r4f, "cloud_vid_dir", texts.SET_OPEN_OUTDIR_A0,
                texts.SET_CVID_A4_3,
                width=30, hint=texts.SET_LMIMG_N_2)
            fr = ttk.Frame(t4f)
            theme.button(fr, text=texts.SET_LMIMG_N_3, width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "video"),
                           texts.SET_CMEDIA_A1_2,
                           texts.SET_CVID_A2)).pack(side="left")
            row(t4f, r4f, "", fr,
                texts.SET_CVID_A4_4)
            fc = ttk.Frame(t4f)
            theme.button(fc, text=texts.SET_CTEXT_N_6, width=14,
                       command=lambda: self.open_cost_window(
                           providers.KIND_VIDEO)).pack(side="left")
            row(t4f, r4f, texts.SET_CTEXT_A2_2, fc,
                texts.SET_CMEDIA_A4_11)

        @section("general", "general")
        def _tgen(tgen, rgen):
            """通用页（2026-10-09 W 定）：提示词优化的模型选择。

            选项 = 主页面可选的**文本**模型：
              · 「本地模型」合成一项 —— 本地链路经本机 llama-server 转发、单槽只
                服务"当前已加载的那个"，逐列本地模型名会假装可选择；
              · 云端文本模型逐个列出（值 = "pid::model"）。
            （W 2026-10-09 二轮：**去掉「跟随当前模型」** —— 本功能主场景是生图 /
            生视频时的提示词优化，就是一个明确的选择；第一个文本模型加入时由
            `App._opt_fill_prompt_model` 自动选上，之后以用户的选择为准。）
            值写 cfg["prompt_opt_model"]；解析与回落全在 connection/optimize。
            """
            items = []
            try:
                use = usable_local(self.cfg)["chat"]
                hid = localmodels.hidden_set(self.cfg)
                if [p for p in use if os.path.basename(p) not in hid]:
                    items.append((texts.SET_GEN_L0, "local"))
            except Exception:
                pass
            try:
                for pid, _pname, m in providers.cloud_models(self.cfg,
                                                             providers.KIND_TEXT):
                    cid = providers.make_cloud_id(pid, m)
                    items.append((providers.display_of_cloud(self.cfg, cid), cid))
            except Exception:
                pass
            by_label = {d: val for d, val in items}
            labels = [d for d, _val in items]
            cur = str(self.cfg.get("prompt_opt_model", "") or "")
            if cur not in [val for _d, val in items]:
                # 没选过 / 记的是过期值：直接显示第一个可选模型（保存即落盘）
                cur = items[0][1] if items else ""
            gvar = tk.StringVar(value=next((d for d, val in items if val == cur), ""))
            tgen._var_general = gvar       # 挂控件保活：StringVar 被 GC 下拉就静默变空（坑 145 家族）
            comb = theme.comb(tgen, textvariable=gvar, state="readonly",
                              width=30, values=labels)
            row(tgen, rgen, texts.POPT_WINDOW_TITLE, comb,
                texts.SET_GEN_A4,
                hint=texts.SET_GEN_N, lw=8)
            if not items:
                i = rgen["i"]
                rgen["i"] += 1
                ttk.Label(tgen, text=texts.SET_GEN_N_2,
                          style="Dim.TLabel").grid(
                    row=i, column=0, columnspan=3, sticky="w", pady=(0, 4))

            # ---- 允许锁屏运行（2026-10-11 远程连接前置，99 K1；W 定主次两键联动）----
            # 主键 = 阻止系统睡眠（core/keepawake）；次生键「仅接电源时保持唤醒」
            # 只在主键开着时显示（W 2026-10-11 定）。保存时顺手 refresh 让效果
            # 立即生效（不等 keepawake 线程的下个 tick）。
            v["keepawake_enabled"] = tk.BooleanVar(
                value=bool(self.cfg.get("keepawake_enabled", False)))
            v["keepawake_ac_only"] = tk.BooleanVar(
                value=bool(self.cfg.get("keepawake_ac_only", False)))
            i = rgen["i"]
            rgen["i"] += 1
            kw_cb = theme.check(tgen, text=texts.SET_GEN_N_3,
                                variable=v["keepawake_enabled"])
            kw_cb.grid(row=i, column=0, columnspan=2, sticky="w", pady=(10, 4))
            if not simple:
                widgets.HelpDot(tgen,
                                texts.SET_GEN_A1).grid(row=i, column=2, sticky="w",
                                                        pady=(10, 4))
            i = rgen["i"]
            rgen["i"] += 1
            kw_ac = theme.check(tgen, text=texts.SET_GEN_N_4,
                                variable=v["keepawake_ac_only"])
            kw_ac.grid(row=i, column=0, columnspan=3, sticky="w",
                       padx=(18, 0), pady=(0, 6))

            def _kw_sync():
                # 次生键显隐随主键：grid_remove 只挪格子不销毁，值不丢
                if v["keepawake_enabled"].get():
                    kw_ac.grid()
                else:
                    kw_ac.grid_remove()
            kw_cb.configure(command=_kw_sync)
            _kw_sync()

            # ---- 历史对话（2026-10-17：把 chat_logs 里的记录读回来接着聊）----
            # 只是颗按钮不是参数：没有可保存的值，所以不进 `_save_general`。
            # 入口放这一页是因为顶栏那排按钮已经挤到"再加一个就压掉清空对话"的地步
            # （见 `_build_topbar` 里 width 那段教训），而"读回历史"本来就是低频动作。
            # 普通模式也留着：它不属于高级能力，用户丢了一次对话就该能找回来。
            hb = ttk.Frame(tgen)
            theme.button(hb, text=texts.SET_GEN_N_6, width=14,
                         command=self.open_history_dialog).pack(side="left")
            # 不带 hint：这一格是"摘要"列，按像素吃饭，一句长话就把整页顶出右边界
            # （普通模式逐页右界那条断言当场抓到）——"记录在哪儿"放进 "?" 里说
            row(tgen, rgen, texts.SET_GEN_N_5, hb, texts.SET_GEN_A2, lw=8)

            def _save_general():
                # 读控件当前文本而不是 gvar：ttk.Combobox.set() 不保证回写
                # textvariable（Tk 的历史语义），readonly 态下 comb.get() 才是真值
                self.cfg["prompt_opt_model"] = by_label.get(str(comb.get()), "")
                self.cfg["keepawake_enabled"] = bool(v["keepawake_enabled"].get())
                self.cfg["keepawake_ac_only"] = bool(v["keepawake_ac_only"].get())
                self._keepawake.refresh()   # 保存即生效，不等下个续约周期
                # 钩子自己落盘（本页的值不在 _apply_settings 的 v 里；外观页同款约定）
                try:
                    save_config(self.cfg)
                except Exception:
                    pass
                return True, ""
            save_hooks[tgen] = _save_general

        @section("remote", "remote")
        def _tremote(trem, rrem):
            """远程访问页（2026-10-11，99 K1；普通 + 高级模式都有；同日 W 三条
            精简：主页面只留状态 + **** + 两个次级窗入口）。

            启用开关（拨上即生效）+ 端口（改动走底部「保存」）+ HTTPS 开关
            （**仅高级模式**，普通模式恒 https 不出选项）+ 状态行（只说
            "已启动"，**不显示网址**）+「网址 ****」→ 次级窗（一个 IPv6 +
            一个 IPv4，各带「复制」+ 证书指纹）+ 配对码（6 位一次性、5 分钟
            有效，W 拍板的设备配对制）+「已配对设备」次级窗（可吊销）。
            布尔键与重启逻辑在本页钩子里（`_apply_settings` 不管布尔键 ——
            与 keepawake 两键同款约定）。
            """
            v["remote_enabled"] = tk.BooleanVar(
                value=bool(self.cfg.get("remote_enabled", False)))
            v["remote_https"] = tk.BooleanVar(
                value=True if simple else bool(self.cfg.get("remote_https", True)))
            # 四档标签的唯一来源在 remote_net.BIND_LABELS（界面不写第二份字面量）
            _bind_labels = [remote_mod.BIND_LABELS[k]
                            for k in (remote_mod.BIND_ALL,
                                      remote_mod.BIND_TAILSCALE,
                                      remote_mod.BIND_V4,
                                      remote_mod.BIND_WFD)]
            _bind_by_label = {remote_mod.BIND_LABELS[k]: k
                              for k in (remote_mod.BIND_ALL,
                                        remote_mod.BIND_TAILSCALE,
                                        remote_mod.BIND_V4,
                                        remote_mod.BIND_WFD)}
            bind_cb = None
            # 建页时的生效值快照（enabled/https/bind/port）：底部「保存」会跑
            # **所有已建页**的钩子，值没变就不该重启监听（坑 262）——否则进过
            # 一次远程页，之后在外观/通用页保存都会断一次手机连接、WFD 档还收组。
            _remote_snap = [None]

            def _cur_remote_vals():
                bind_val = (str(self.cfg.get("remote_bind") or remote_mod.BIND_ALL)
                            if bind_cb is None else
                            _bind_by_label.get(str(bind_cb.get()),
                                              remote_mod.BIND_ALL))
                port_val = (str(v["remote_port"].get()).strip()
                            if v.get("remote_port") is not None
                            else str(self.cfg.get("remote_port", 8443)))
                return (bool(v["remote_enabled"].get()),
                        True if simple else bool(v["remote_https"].get()),
                        bind_val, port_val)

            def _apply_remote():
                """读本页开关与档位 → 写 cfg → 重启监听 → 回显。

                W 2026-10-11 定的交互：**开关即保存即生效**（不是"打勾 → 底部
                保存"两步）——开关一拨，状态行立刻更新。端口改动仍走底部
                「保存」统一入口。启动失败（端口被占用等）不抛、不弹窗：
                状态行给人话，让"保存"照常走完（坑 194 家族的 cfg 写侧已在此
                前完成，这里只影响监听起不起得来）。普通模式 **恒 https**
                （W 2026-10-11：普通用户不露这个选项，默认开启）。

                `remote_bind` 不在 STR_KEYS 通用循环里：**改了必须重绑监听才成立**，
                所以与两个布尔键同款走本页钩子（钩子自己落盘）。四项与建页快照
                逐字一致时整段 no-op（坑 262）：别的页点保存不断手机连接。
                """
                cur = _cur_remote_vals()
                if cur == _remote_snap[0] and self.remote.running() == cur[0]:
                    return
                en, https = cur[0], cur[1]
                self.cfg["remote_enabled"] = en
                self.cfg["remote_https"] = https
                if bind_cb is not None:
                    self.cfg["remote_bind"] = _bind_by_label.get(
                        str(bind_cb.get()), remote_mod.BIND_ALL)
                try:
                    save_config(self.cfg)
                except Exception:
                    pass
                # 值已落盘：快照推进到新值（"运行态与期望不一致"那条仍会让下次
                # 保存重试启动——cur[0] 与 running() 比的就是这个，坑 262）
                _remote_snap[0] = cur
                if self.remote.running():
                    # 快速收组（2026-10-13 关窗提速轮）：直连组在后台自行散净，
                    # 不阻塞界面；立刻重开由 remote.start 的 settle_stale 兜住
                    self.remote.stop(quick=True)
                if en:
                    # 起监听那一步**首次要同步跑 PowerShell 造自签证书**（超时给到
                    # 60s）——挂主线程就是"拨一下开关、整个界面冻住"。挪后台线程，
                    # 状态行先说一句，起完再回显（跨线程一律经 _ui_q，铁律 7）。
                    stat_var.set(
                        texts.SET_REMOTE_N
                        if str(self.cfg.get("remote_bind") or "") == remote_mod.BIND_WFD
                        else texts.SET_REMOTE_N_2)
                    code_var.set("—")

                    def _started(err):
                        try:
                            if not trem.winfo_exists():
                                return          # 页面已经关了（切主题会整窗重建）
                        except Exception:
                            return
                        if err:
                            if str(self.cfg.get("remote_bind") or "") == remote_mod.BIND_WFD:
                                # 直连档的失败原因自带指路（组起不来 / 没链路 /
                                # 端口），不套"端口被占用"那半句去误导
                                stat_var.set(texts.SET_REMOTE_CAT % err)
                            else:
                                stat_var.set(
                                    texts.SET_REMOTE_CAT_2 % err)
                            code_var.set("—")
                            return
                        _remote_refresh()

                    self.start_remote_async(on_done=_started, note_ok=False)
                    return
                _remote_refresh()

            def _save_remote():
                _apply_remote()
                return True, ""
            save_hooks[trem] = _save_remote
            i = rrem["i"]
            rrem["i"] += 1
            theme.check(trem, text=texts.SET_REMOTE_N_3,
                        variable=v["remote_enabled"],
                        command=_apply_remote).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(4, 4))
            if not simple:
                widgets.HelpDot(trem,
                                texts.SET_REMOTE_A1).grid(
                    row=i, column=2, sticky="w", pady=(4, 4))
            ent(trem, rrem, "remote_port", texts.SET_REMOTE_A3,
                texts.SET_REMOTE_A4,
                width=7)
            if not simple:
                i = rrem["i"]
                rrem["i"] += 1
                theme.check(trem, text=texts.SET_REMOTE_N_4,
                            variable=v["remote_https"],
                            command=_apply_remote).grid(
                    row=i, column=0, columnspan=3, sticky="w", pady=(4, 4))
                widgets.HelpDot(trem,
                                texts.SET_REMOTE_A1_2).grid(
                    row=i, column=2, sticky="w", pady=(4, 4))
                # 对外开放范围（P0-5，2026-10-12；仅高级模式——普通模式照旧
                # "拨一下开关就能用"，不多一个会让人连不上的选项）
                i = rrem["i"]
                rrem["i"] += 1
                ttk.Label(trem, text=texts.SET_REMOTE_N_5).grid(
                    row=i, column=0, sticky="w", padx=(0, 8), pady=5)
                bind_cb = ttk.Combobox(trem, values=_bind_labels,
                                       state="readonly", width=22)
                bind_cb.set(remote_mod.BIND_LABELS.get(
                    str(self.cfg.get("remote_bind", remote_mod.BIND_ALL)
                        or remote_mod.BIND_ALL),
                    remote_mod.BIND_LABELS[remote_mod.BIND_ALL]))
                bind_cb.grid(row=i, column=1, sticky="w", pady=5)
                bind_cb.bind("<<ComboboxSelected>>",
                             lambda *a: _apply_remote())
                widgets.HelpDot(trem,
                                texts.SET_REMOTE_A1_3).grid(
                    row=i, column=2, sticky="w", pady=5)
            # 建页完成：钉初始快照（控件此时才全部建好；ent 的 remote_port
            # 与档位下拉都在了）。之后别的页点保存，值没变就不重启监听（坑 262）
            _remote_snap[0] = _cur_remote_vals()

            # ---- 状态行 + 网址(****) + 配对码 + 设备(次级窗) ----
            stat_var = tk.StringVar(value="")
            code_var = tk.StringVar(value="")
            i = rrem["i"]
            rrem["i"] += 1
            ttk.Label(trem, textvariable=stat_var, style="Dim.TLabel",
                      wraplength=430, justify="left").grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(6, 2))
            i = rrem["i"]
            rrem["i"] += 1
            ttk.Label(trem, text=texts.SET_REMOTE_N_6).grid(row=i, column=0, sticky="w",
                                              padx=(0, 8), pady=5)
            theme.button(trem, text="****", width=7,
                         command=lambda: _open_urls()).grid(
                row=i, column=1, sticky="w", pady=5)
            if not simple:
                widgets.HelpDot(trem,
                                texts.SET_REMOTE_A1_4).grid(
                    row=i, column=2, sticky="w", pady=5)
            i = rrem["i"]
            rrem["i"] += 1
            ttk.Label(trem, text=texts.SET_REMOTE_N_7).grid(row=i, column=0, sticky="w",
                                                padx=(0, 8), pady=5)
            ttk.Label(trem, textvariable=code_var, font=(
                "Consolas", 14, "bold")).grid(row=i, column=1, sticky="w",
                                              pady=5)
            if not simple:
                widgets.HelpDot(trem,
                                texts.SET_REMOTE_A1_5).grid(row=i, column=2, sticky="w", pady=5)
            i = rrem["i"]
            rrem["i"] += 1
            btn_row = ttk.Frame(trem)
            btn_row.grid(row=i, column=0, columnspan=3, sticky="w", pady=(0, 6))

            _panels = {}        # 标题 → 已开着的次级窗（单实例判据，坑 208⑤）

            def _panel(title):
                """本页三个次级窗的共同骨架（坑 161 / 186：withdraw 后
                center_on、style_toplevel 必调）。

                **单实例**：同一个窗已经开着就把它抬到前面并返回 `(None, None)` ——
                连点两下叠两个一模一样的窗，用户分不清哪个是当前状态（成本估算窗
                早已这么治，R-85；这一页是 2026-10-11 新加的，当时漏了）。
                调用方拿到 None 直接 return。
                """
                old = _panels.get(title)
                if old is not None:
                    try:
                        if old.winfo_exists():
                            old.lift()
                            old.focus_force()
                            return None, None
                    except Exception:
                        pass
                    _panels.pop(title, None)
                w = tk.Toplevel(win)
                w.withdraw()
                w.title(title)
                _panels[title] = w

                def _gone(e, t=title, ww=w):
                    if getattr(e, "widget", None) is ww:
                        _panels.pop(t, None)

                w.bind("<Destroy>", _gone, add="+")
                theme.style_toplevel(w, "panel")
                frm = ttk.Frame(w, padding=14)
                frm.pack(fill="both", expand=True)
                return w, frm

            def _open_urls():
                """网址次级窗：一个 IPv6 + 一个 IPv4（W 2026-10-11 定：不铺
                全部），各带「复制」；证书指纹也收在这里。设备直连档只给
                直连那一条地址（另加直连组的组名 / 密码给手机对）。"""
                w, frm = _panel(texts.SET_REMOTE_A0)
                if w is None:
                    return                      # 已经开着：抬到前面就够了
                if not self.remote.running():
                    ttk.Label(frm, text=texts.SET_REMOTE_N_8, style="Dim.TLabel"
                              ).pack(anchor="w")
                    widgets.center_on(w, host=win)
                    return
                scheme = self.remote.scheme()
                port = self.cfg.get("remote_port", 8443)
                rows = []
                if (str(self.cfg.get("remote_bind") or "")
                        == remote_mod.BIND_WFD and self.remote.bind):
                    # 设备直连档：只给这一条直连地址（其他网口本来就没监听，
                    # 不列出来）；电脑侧直连组还给组名与密码
                    rows.append((texts.SET_REMOTE_L0,
                                 "%s://%s:%s" % (scheme, self.remote.bind, port)))
                    if getattr(self.remote, "bind_kind", "") == "wfd":
                        rows.append((texts.SET_REMOTE_L0_2,
                                     remote_mod.WFD_SSID))
                        rows.append((texts.SET_REMOTE_L0_3, remote_mod.WFD_PASS))
                else:
                    ips = remote_mod.local_ips() or []
                    v6 = next((x for x in ips
                               if remote_mod.ip_label(x) == "v6"), "")
                    v4 = next((x for x in ips
                               if remote_mod.ip_label(x) in ("v4", "tailscale")), "")
                    if v6:
                        rows.append((texts.SET_REMOTE_L0_4,
                                     "%s://[%s]:%s" % (scheme, v6, port)))
                    if v4:
                        lab = ("（Tailscale）" if remote_mod.ip_label(v4) ==
                               "tailscale" else texts.SET_REMOTE_N_9)
                        rows.append(("IPv4" + lab, "%s://%s:%s" % (scheme, v4, port)))
                if not rows:
                    ttk.Label(frm, text=texts.SET_REMOTE_N_10,
                              style="Dim.TLabel").pack(anchor="w", pady=4)
                for name, url in rows:
                    ttk.Label(frm, text=name, style="Dim.TLabel"
                              ).pack(anchor="w", pady=(6, 0))
                    rowf = ttk.Frame(frm)
                    rowf.pack(anchor="w")
                    ttk.Label(rowf, text=url, font=("Consolas", 11)).pack(
                        side="left")
                    b = theme.button(rowf, text=texts.SET_API_N_7, width=6)

                    def _copy(u=url, bt=None):
                        win.clipboard_clear()
                        win.clipboard_append(u)
                        if bt and bt.winfo_exists():
                            bt.configure(text=texts.SET_REMOTE_N_11)
                        win.after(1200, lambda: (
                            bt.winfo_exists() and bt.configure(text=texts.SET_API_N_7)))

                    b.configure(command=lambda u=url, bt=b: _copy(u, bt))
                    b.pack(side="left", padx=8)
                if self.remote.cert_fp:
                    ttk.Label(frm, text=texts.SET_REMOTE_CAT_3
                              % self.remote.cert_fp, style="Dim.TLabel",
                              wraplength=380, justify="left"
                              ).pack(anchor="w", pady=(10, 0))
                widgets.center_on(w, host=win)

            def _open_devices():
                """已配对设备次级窗（吊销 / 改名后列表就地重画）。

                2026-10-15 UI 走查：窗口给最小尺寸（空态不再缩成一小条）、
                redraw 后按内容重算大小并重新居中；吊销 / 改名全走项目风格
                dialogs（系统 messagebox / simpledialog 下线），入口级吊销
                原来没二次确认，补一道危险操作确认。
                """
                w, frm = _panel(texts.SET_REMOTE_A0_2)
                if w is None:
                    return                      # 已经开着：抬到前面就够了
                DEV_MIN = (540, 260)           # 空态也保持设计规范的最小窗
                w.minsize(*DEV_MIN)

                def _fit():
                    """内容多寡不一（空态 / 多设备 / 多入口）：按请求尺寸重算
                    窗口（夹最小宽高）并重新居中到设置窗。"""
                    w.update_idletasks()
                    rw = max(DEV_MIN[0], frm.winfo_reqwidth() + 4)
                    rh = max(DEV_MIN[1], frm.winfo_reqheight() + 4)
                    w.geometry("%dx%d" % (rw, rh))
                    widgets.center_on(w, host=win, size=(rw, rh))

                def redraw():
                    from ..core.device_ua import os_label
                    for c in frm.winfo_children():
                        c.destroy()
                    persons = self.remote.personas()
                    if not persons:
                        empty = ttk.Frame(frm)
                        empty.pack(anchor="w", pady=(26, 10))
                        ttk.Label(empty, text=texts.SET_REMOTE_N_12,
                                  font=("Microsoft YaHei UI", 10, "bold")).pack(
                            anchor="w")
                        ttk.Label(
                            empty,
                            text=texts.SET_REMOTE_N_13,
                            style="Dim.TLabel", wraplength=480,
                            justify="left").pack(anchor="w", pady=(10, 2))
                        ttk.Label(
                            empty,
                            text=texts.SET_REMOTE_N_14,
                            style="Dim.TLabel", wraplength=480,
                            justify="left").pack(anchor="w", pady=(2, 0))
                    else:
                        ttk.Label(
                            frm,
                            text=texts.SET_REMOTE_N_15
                            if simple else
                            texts.SET_REMOTE_N_16,
                            style="Dim.TLabel", wraplength=520, justify="left").pack(
                            anchor="w", pady=(0, 6))
                        for p in persons:
                            oses = "、".join(os_label(k) for k in (p.get("os_set") or []))
                            tag = oses or texts.DLG_PAIR_UNKNOWN_DEVICE
                            if len(p.get("os_set") or []) > 1:
                                tag += texts.SET_REMOTE_TAG
                            pf = ttk.Frame(frm)
                            pf.pack(anchor="w", pady=3, fill="x")
                            # 按钮组统一贴右缘成一列：设备名长短不一时「改名 / 吊销」
                            # 不再跟着文字漂（2026-10-15 UI 走查）
                            acts = ttk.Frame(pf)
                            acts.pack(side="right")
                            ttk.Label(pf, text=texts.SET_REMOTE_CAT_4 % (
                                tag, p.get("name", "?"),
                                p.get("last_seen") or "—")).pack(side="left")
                            pid = p["id"]

                            def _rename(pid=pid, old=p.get("name", "")):
                                nm = dialogs.ask_text(
                                    w, texts.SET_REMOTE_A1_6, texts.SET_REMOTE_A2,
                                    initial=old)
                                if nm and nm.strip():
                                    self.remote.rename_persona(pid, nm.strip())
                                    redraw()

                            theme.button(acts, text=texts.SET_REMOTE_N_17, width=6,
                                         command=_rename).pack(side="left", padx=(8, 0))

                            def _revoke(pid=pid, name=p.get("name", "?")):
                                if dialogs.ask_confirm(
                                        w, texts.SET_REMOTE_A1_7,
                                        texts.SET_REMOTE_CAT_5 % name,
                                        ok_text=texts.SET_REMOTE_A1_7, danger=True):
                                    self.remote.revoke_persona(pid)
                                    redraw()

                            theme.button(acts, text=texts.SET_REMOTE_N_18, width=6,
                                         command=_revoke).pack(side="left", padx=4)
                            # 入口级清单只在高级模式露出（W Q4②：普通用户容易把
                            # "换个入口重配"误当成数据被删，坑 284）
                            if not simple:
                                creds = p.get("credentials") or []
                                if len(creds) > 1:
                                    for cd in creds:
                                        cf = ttk.Frame(frm)
                                        cf.pack(anchor="w", padx=(24, 0))
                                        ttk.Label(
                                            cf, style="Muted.TLabel",
                                            font=("Microsoft YaHei UI", 8),
                                            text=texts.SET_REMOTE_CAT_6 % (
                                                cd.get("id", "?"),
                                                os_label(cd.get("os")),
                                                cd.get("last_ip") or "—",
                                                cd.get("added") or "—")).pack(side="left")

                                        def _revoke_cred(did=cd.get("id", "")):
                                            if dialogs.ask_confirm(
                                                    w, texts.SET_REMOTE_A1_8,
                                                    texts.SET_REMOTE_CAT_7 % did,
                                                    ok_text=texts.SET_REMOTE_N_19,
                                                    danger=True):
                                                self.remote.revoke(did)
                                                redraw()

                                        theme.button(
                                            cf, text=texts.SET_REMOTE_N_19, width=10,
                                            command=_revoke_cred
                                        ).pack(side="left", padx=6)
                    code_var.set(_pair_text())
                    _fit()

                redraw()

            def _remote_refresh():
                """状态行 / 配对码的回显（进页 + 保存后都调）。网址与设备
                收在次级窗（W 2026-10-11 精简），主页面只说"已启动"。"""
                if self.remote.running():
                    stat = texts.SET_REMOTE_STAT
                    if self.remote.scheme() != "https":
                        stat += texts.SET_REMOTE_STAT_2
                    if (str(self.cfg.get("remote_bind") or "")
                            == remote_mod.BIND_WFD and self.remote.bind):
                        stat += texts.SET_REMOTE_CAT_8 % self.remote.bind
                    # 想只绑 Tailscale 但本机没那个地址 → 已退回全部网卡，如实说
                    if getattr(self.remote, "bind_note", ""):
                        stat += "（" + self.remote.bind_note + "）"
                    code_var.set(_pair_text())
                    stat_var.set(stat)
                else:
                    code_var.set("—")
                    stat_var.set(texts.SET_REMOTE_A0_3)

            def _pair_text():
                # 上一码被试错作废（按码限次，15 §1 2026-10-12 行）；原话 W 已逐字
                # 点头，常量在 remote 侧（与手机端那句同处一行，别改一处漏一处）。
                if self.remote.pair_burned():
                    return remote_mod.PAIR_BURNED_PAGE
                left = self.remote.pair_code_left()
                return texts.SET_REMOTE_CAT_9 % left if left else "—"

            def _new_code():
                if not self.remote.running():
                    stat_var.set(texts.SET_REMOTE_A0_4)
                    return
                code_var.set(self.remote.new_pair_code())
            theme.button(btn_row, text=texts.SET_REMOTE_N_20, width=12,
                         command=_new_code).pack(side="left")
            theme.button(btn_row, text=texts.SET_REMOTE_N_21, width=14,
                         command=_open_devices).pack(side="left", padx=8)
            # 「停止远程任务」（16 §4 第 6 条，二期 W 拍板并入）：一期 API 已有
            # /api/cancel，GUI 侧一直没有按钮。device_id="local" = 本机全可见
            # （与产物取回同一口径）；置 stop_flag 后由 reader 线程收进程。
            def _stop_remote_job():
                # 两种远程任务都能停（生图 / 对话）：这一颗按钮是电脑端唯一的人工出口，
                # 拆成两颗只会让人先猜哪颗管哪件事。
                r = self.remote
                jobs = getattr(r, "jobs", None)
                chat = getattr(r, "chat", None)
                if jobs is not None and jobs.busy() and jobs.cancel(None, "local"):
                    stat_var.set(texts.SET_REMOTE_A0_5)
                elif chat is not None and chat.busy() and chat.cancel("local"):
                    stat_var.set(texts.SET_REMOTE_A0_6)
                else:
                    stat_var.set(texts.SET_REMOTE_A0_7)
            theme.button(btn_row, text=texts.SET_REMOTE_N_22, width=13,
                         command=_stop_remote_job).pack(side="left", padx=8)

            def _open_audit():
                """审计次级窗（仅高级模式；普通模式可裁 = 16 §4 第 6 条口径）。
                只读展示 remote_audit.jsonl 的元数据行，清空前问一句。"""
                w, frm = _panel(texts.SET_REMOTE_A0_8)
                if w is None:
                    return                      # 已经开着：抬到前面就够了
                box = tk.Text(frm, height=16, width=64, wrap="word",
                              font=("Consolas", 9), bd=0, highlightthickness=1,
                              highlightbackground=theme.c("border"),
                              highlightcolor=theme.c("accent"),
                              **theme.text_kw())
                box.pack(fill="both", expand=True)

                def redraw():
                    rows = remote_mod.read_audit()
                    box.configure(state="normal")
                    box.delete("1.0", "end")
                    if not rows:
                        box.insert("end", texts.SET_REMOTE_A1_9)
                    else:
                        for r in rows:      # read_audit 已按时间倒序
                            box.insert("end", "%s  %-14s %-15s %s  %s\n" % (
                                r.get("t", "?"), r.get("ip", "?"),
                                r.get("action", "?"),
                                texts.SET_REMOTE_N_23 if r.get("ok") else texts.POPT_FAILED,
                                str(r.get("detail", ""))[:60]))
                    box.configure(state="disabled")

                def _clear():
                    if dialogs.ask_confirm(w, texts.SET_REMOTE_A1_10,
                                          texts.SET_REMOTE_A2_2,
                                          ok_text=texts.MASK_CLEAR, danger=True):
                        remote_mod.clear_audit()
                        redraw()
                bar = ttk.Frame(frm)
                bar.pack(anchor="w", pady=(8, 0))
                theme.button(bar, text=texts.MASK_CLEAR, width=8,
                             command=_clear).pack(side="left")
                ttk.Label(bar, text=texts.SET_REMOTE_N_24,
                          style="Dim.TLabel").pack(side="left", padx=8)
                redraw()
                widgets.center_on(w, host=win)
            if not simple:
                theme.button(btn_row, text=texts.SET_REMOTE_N_25, width=11,
                             command=_open_audit).pack(side="left", padx=8)

            # 历史产物开关（16 §4 第 3 条，二期 W 拍板并入；默认关）。布尔键
            # 在本页钩子里即拨即存（与 remote_enabled 同款约定，_apply_settings
            # 不管布尔键）；取回判据每次请求现读，不用重启监听。
            v["remote_history"] = tk.BooleanVar(
                value=bool(self.cfg.get("remote_history", False)))

            def _apply_history():
                self.cfg["remote_history"] = bool(v["remote_history"].get())
                try:
                    save_config(self.cfg)
                except Exception:
                    pass
            i = rrem["i"]
            rrem["i"] += 1
            theme.check(trem, text=texts.SET_REMOTE_N_26,
                        variable=v["remote_history"],
                        command=_apply_history).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(2, 4))
            if not simple:
                widgets.HelpDot(trem,
                                texts.SET_REMOTE_A1_11).grid(
                    row=i, column=2, sticky="w", pady=(2, 4))

            _remote_refresh()

        @section("appearance", "appearance")
        def _tapr(tapr, rapr):
            """外观页（2026-10-08 W 定）：界面主题下拉 + 全局主题色 / 选中文本强调色。

            「界面主题」（跟随系统 / 浅色 / 深色）**选完即切**：theme.set_mode
            同 root 实时换主题（ttk 与注册过的经典 tk 控件自动重画）+ App.retint
            重涂烘焙色 + 设置窗整窗重建；跟随系统由 App 的 30 秒轮询兜住后续变化。
            「全局主题色」一个变量管发送按钮、取回、模型名、用户名、状态灯、
            滑动开关、左栏选中项（本就同源 primary 一个色）；「选中文本强调色」
            与它**互相独立**（W 同日二轮定"解除联动、可独立配置"，两者各自的
            "恢复默认"都落主题原色，不是彼此）。改色即时**预览**选中文本
            （Text.configure 便宜），按钮等样式色下次启动生效（样式是启动时
            构建的）。选色走 `_ask_color`（Windows 原生对话框 + 可持久化的
            自定义色板，随改随落盘）。
            """
            _THEME_DISPLAY = ((texts.SET_APPR_L0, "auto"), (texts.SET_APPR_L0_2, "light"),
                              (texts.SET_APPR_L0_3, "dark"))
            _BY_VALUE = {v: d for d, v in _THEME_DISPLAY}
            theme_var = tk.StringVar(
                value=_BY_VALUE.get(str(self.cfg.get("ui_theme", "auto") or "auto"),
                                    texts.SET_APPR_L0))
            accent_var = tk.StringVar(value=str(self.cfg.get("ui_accent", "") or ""))
            sel_var = tk.StringVar(value=str(self.cfg.get("ui_accent_sel", "") or ""))
            swatches = {}              # 配置键 → 色块 Label（变量一变就染新色）
            # 自定义色格（Windows 选色器左侧那 16 格）：本进程持有一份、随改随落盘，
            # 重开面板 / 重启软件都还在（tkinter 自带的 colorchooser 把它藏进 Tk
            # 进程内部，重启就丢 —— W 报的"添加到自定义颜色不能保存"）
            cust = list(self.cfg.get("ui_custom_colors") or [])[:16]
            cust += [""] * (16 - len(cust))

            def _sel_colors():
                """当前生效的选区 (背景, 文字)：**只看选中文本强调色**，与全局
                主题色互相独立（W 2026-10-08 定"解除联动"）；留空 = 主题默认。"""
                hexv = str(sel_var.get() or "").strip()
                if hexv:
                    try:
                        return hexv, ("#ffffff" if theme._luma(hexv) < 0.62
                                      else "#1a1a1a")
                    except Exception:      # 手改配置写出非法色值：宁灰勿炸
                        return hexv, "#ffffff"
                bg = theme.default_accent()
                return bg, ("#ffffff" if theme._luma(bg) < 0.62 else "#1a1a1a")

            def _preview_selection():
                """改色即时预览到聊天区 / 输入框的选中文本（其余样式重启生效）。"""
                bgc, fgc = _sel_colors()
                for t in (getattr(self, "chat", None), getattr(self, "input", None)):
                    try:
                        if t is not None and t.winfo_exists():
                            t.configure(selectbackground=bgc, selectforeground=fgc)
                    except Exception:
                        pass

            def _refresh_swatch(key):
                sw = swatches.get(key)
                if sw is None:
                    return
                if key == "ui_accent_sel":
                    hexv = _sel_colors()[0]
                else:
                    # "默认"必须是主题原色 —— c("accent") 在配置非空时已被注入成
                    # 用户配置的色，拿它当默认会让"恢复默认"恢复回旧配置（W 报）
                    hexv = str(accent_var.get() or "").strip() or theme.default_accent()
                try:
                    sw.configure(background=hexv)
                except Exception:
                    pass

            def _pick(key):
                before = list(cust)
                cur = str((accent_var if key == "ui_accent" else sel_var).get()
                          or "").strip() or None
                got = _ask_color(win,
                                 texts.SET_APPR_N if key == "ui_accent"
                                 else texts.SET_APPR_N_2,
                                 cur, cust)
                if list(cust) != before:
                    # 自定义色格在**任何关闭时机**（含取消）都持久化 —— 这是色板
                    # 草稿纸，不是一项设置，走保存按钮反而丢
                    self.cfg["ui_custom_colors"] = list(cust)
                    save_config(self.cfg)
                if not got:
                    return                  # 取消：选择不动
                (accent_var if key == "ui_accent" else sel_var).set(got)
                _preview_selection()

            def _reset(key):
                (accent_var if key == "ui_accent" else sel_var).set("")
                _preview_selection()

            def _color_cell(key):
                """取色件一行：色块 + 「选颜色…」 + 「恢复默认」。"""
                cell = ttk.Frame(tapr)
                sw = tk.Label(cell, width=4, background=theme.default_accent(),
                              relief="groove", bd=1)
                sw.pack(side="left", pady=2)
                swatches[key] = sw
                theme.button(cell, text=texts.SET_APPR_N_3, width=10,
                             command=lambda: _pick(key)).pack(side="left", padx=(8, 0))
                theme.button(cell, text=texts.SET_APPR_N_4, width=10,
                             command=lambda: _reset(key)).pack(side="left", padx=(6, 0))
                _refresh_swatch(key)
                return cell

            # 「界面主题」下拉 = 深色模式的唯一开关：v1.1.1 发版临时屏蔽（W 2026-10-09，
            # 下一步再开发）。theme.RELEASE_LIGHT_ONLY 一处开关控制，机制本体都在。
            theme_comb = None
            if not theme.RELEASE_LIGHT_ONLY:
                theme_comb = theme.comb(tapr, textvariable=theme_var, state="readonly",
                                        width=14,
                                        values=[d for d, _v in _THEME_DISPLAY])

                def _on_theme_pick(_e=None):
                    """选完即切：写 cfg → 落盘 → 实时切换（浅/深）。

                    跟随系统 = 每次按当前系统偏好解析（30 秒轮询在 App 侧兜住后续
                    变化）。设置窗由 retint 整窗重建，停回"外观"这一页。
                    """
                    value = dict((d, v) for d, v in _THEME_DISPLAY).get(
                        theme_var.get(), "auto")
                    self.cfg["ui_theme"] = value
                    try:
                        save_config(self.cfg)
                    except Exception:
                        pass
                    want = theme.resolve_mode(value)
                    if want != theme.mode():
                        self._switch_theme(want)

                theme_comb.bind("<<ComboboxSelected>>", _on_theme_pick)
                row(tapr, rapr, texts.SET_APPR_A2, theme_comb,
                    texts.SET_APPR_A4)
            row(tapr, rapr, texts.SET_APPR_A2_2, _color_cell("ui_accent"),
                texts.SET_APPR_A4_2)
            row(tapr, rapr, texts.SET_APPR_A2_3, _color_cell("ui_accent_sel"),
                texts.SET_APPR_A4_3)

            accent_var.trace_add("write", lambda *_: _refresh_swatch("ui_accent"))
            sel_var.trace_add("write", lambda *_: _refresh_swatch("ui_accent_sel"))

            def _save_appearance():
                self.cfg["ui_theme"] = dict((d, v) for d, v in _THEME_DISPLAY).get(
                    theme_var.get(), "auto")
                self.cfg["ui_accent"] = accent_var.get().strip()
                self.cfg["ui_accent_sel"] = sel_var.get().strip()
                save_config(self.cfg)   # 钩子在 _apply_settings 落盘**之后**跑（同 provider commit）
                return True, ""

            save_hooks[tapr] = _save_appearance

        @section("about", "about")
        def _t9(t9, r9):
            """关于页：标志 + 名字 + 版本，加两个入口（新手引导 / 诊断）。

            技术信息（运行方式、各种路径、运行库、错误日志）与诊断都搬进了
            `SubWindowMixin.open_diag_window` 那个次级页面：这一页只回答"这是什么软件"，
            而且要保证**首屏就能点到「新手引导」**—— 它原来排在七行路径 + 一个大结果框
            下面，第一次用的人翻不到（W 2026-10-01 提的）。
            """
            head = tk.Frame(t9, background=widgets.default_bg())
            i = r9["i"]
            r9["i"] += 1
            head.grid(row=i, column=0, columnspan=3, sticky="w", pady=(2, 10))
            logo = widgets.app_logo(t9)
            if logo is not None:
                lab = tk.Label(head, image=logo, background=widgets.default_bg())
                lab._logo_img = logo    # 引用留在控件上：对象被 GC，图就没了（同 set_app_icon）
                lab.pack(side="left", padx=(0, 16))
            names = tk.Frame(head, background=widgets.default_bg())
            names.pack(side="left")

            def _tap_version(_e=None):
                """版本号那行被点了一下：连够 `DEV_TAP_TIMES` 次就进开发者模式。

                判据重点是"**连着**点"：距上次超过 `DEV_TAP_GAP` 秒就当没在连点、从 1 重数。
                够数以后**直接跳到「开发者选项」页**当作反馈 —— 不弹窗：这一页开头就写着
                开关状态，而在左栏平白多一行、还不告诉你进了哪儿，等于让人自己找。
                """
                now = time.time()
                if now - float(self._dev_tap.get("at") or 0) > DEV_TAP_GAP:
                    self._dev_tap["count"] = 0
                self._dev_tap["count"] = int(self._dev_tap.get("count") or 0) + 1
                self._dev_tap["at"] = now
                if self._dev_tap["count"] < DEV_TAP_TIMES:
                    return
                self._dev_tap["count"] = 0
                self._dev_mode = True
                _rebuild_nav()              # 左栏补上「开发者选项」
                nav.select("dev")

            # 版本号那行同时是开发者模式的**入口**。"隐形按钮"的做法就用这个 Label 自己接
            # 点击：不加控件、字体颜色都不动、光标也保持默认箭头 —— 用户看不出任何差别
            # （W 2026-10-04 要的"无法察觉"）。比在它下面另摆一个透明按钮更稳：那个无论怎么
            # 调都会多占一点纵向版面，还容易和 `place` 的层次、`grid` 的格子打架。
            for txt_, font, fg, pady in (
                    ("LLM Chat", ("Microsoft YaHei UI", 16, "bold"), theme.c("hdr"), (0, 2)),
                    (texts.SET_ABOUT_CAT % APP_VERSION,
                     ("Microsoft YaHei UI", 9), theme.c("dim"), (0, 2)),
                    (texts.SET_ABOUT_L0,
                     ("Microsoft YaHei UI", 9), theme.c("hint"), (0, 0))):
                lab = ttk.Label(names, text=txt_, foreground=fg, font=font,
                                wraplength=420, justify="left")
                lab.pack(anchor="w", pady=pady)
                if txt_.startswith(texts.SET_ABOUT_A0):
                    lab.bind("<Button-1>", _tap_version)

            bf = ttk.Frame(t9)
            i = r9["i"]
            r9["i"] += 1
            bf.grid(row=i, column=0, columnspan=3, sticky="w", pady=(0, 6))
            theme.button(bf, text=texts.APP_GUIDE_TITLE, width=12,
                       command=self.start_guide).pack(side="left", padx=(0, 6))
            theme.button(bf, text=texts.DIAGW_TITLE, width=12,
                       command=self.open_diag_window).pack(side="left")

            # 检查更新：联网在子线程、结果回主线程走 _ui_q
            # （坑 54），通道与本机版本在主线程取快照；查不到就报查不到（见 core/updater.py）
            #
            # 检查状态挂 App（`self._upd_check`）而不是这个窗口：关掉设置窗再开，10 分钟冷却
            # 与上次结果都还在 —— 冷却本来就是为"来回翻设置页"这种进页动作准备的
            # （W 2026-10-04）。
            import webbrowser

            st = self._upd_check        # App 级会话状态：at / busy / last / cache / render
            up_url = {"v": ""}          # 有新版本时才填上，"打开下载页"才可点
            ch_var = tk.StringVar(value=updater.CHANNEL_LABEL[updater.CHANNEL_STABLE])
            ustate = tk.StringVar(value=texts.SET_ABOUT_N)

            ust_lab = ttk.Label(t9, textvariable=ustate, style="Note.TLabel",
                                wraplength=560, justify="left",
                                font=("Microsoft YaHei UI", 9))

            def _say(text, color=theme.c("note")):
                ustate.set(text)
                ust_lab.configure(foreground=color)

            def _open_page(url):
                if not url:
                    messagebox.showinfo(texts.SET_ABOUT_A0_2, texts.SET_ABOUT_A1)
                    return
                try:
                    if not webbrowser.open(url):
                        raise RuntimeError(texts.UPDW_BROWSER_NORESP)
                except Exception as e:
                    messagebox.showwarning(
                        texts.SET_ABOUT_A0_2, texts.SET_ABOUT_CAT_2
                        % (e, url))

            def _render(state, info, note="", dialog=False):
                """把一次结果写到**当前**关于页那行状态上（控件已销毁就什么都不画）。

                `note` = 冷却期进页时追加的说明（"检查时间 14:32 …"）；`dialog` = 弹不弹窗，
                只给**用户亲手点的那一次** —— 进页自动查与冷却回显都不弹（每次进设置都弹
                一个框会烦人）。

                放在 `_do_check` **外面**：冷却回显那次根本没发请求，也得能把上次结果写出来；
                它只认 `info`（本机版本与通道名都在里面），不依赖某一次请求的局部量。
                """
                if not ust_lab.winfo_exists():       # 窗口/控件已销毁（坑 135）
                    return
                cur = updater.display_version(info.get("current") or APP_VERSION)
                ch_text_ = updater.channel_label(
                    info.get("channel") or updater.CHANNEL_STABLE)
                btn_chk.configure(state="normal")
                if state == updater.STATE_UPDATE:
                    up_url["v"] = info.get("url") or ""
                    btn_open.configure(state="normal")
                    _say(texts.SET_ABOUT_CAT_3
                         % (info.get("name") or info["tag"],
                            info["tag"], cur, info.get("published") or texts.UPDW_DATE_UNKNOWN)
                         + note, theme.c("oklit"))
                    if dialog:
                        # 手动点的：弹「发现新版本」次级窗口（能一键更新），永远弹
                        # —— 不再看 upd_dismissed 的旧账（2026-10-05）
                        self.open_update_window(info, auto=False)
                    elif info.get("tag") and info.get("tag") != str(
                            self.cfg.get("upd_dismissed", "") or ""):
                        # 自动检查（进页自动查）查到新版本也弹（W 2026-10-05 定）；
                        # 同一个版本被关掉过就只留这行状态，不再弹第二次
                        self.open_update_window(info, auto=True)
                elif state == updater.STATE_LATEST:
                    _say(texts.SET_ABOUT_CAT_4
                         % (updater.display_version(info.get("tag") or cur), ch_text_)
                         + note, theme.c("oklit"))
                elif state == updater.STATE_AHEAD:
                    # 报实话：不并进「已是最新」（多半是自己编的版本还没打 tag）；
                    # 彩蛋（W 2026-10-05）：本机比线上最新的还新时，文案末尾追一句
                    # 「莫非你是测试用户！」—— 只看本次返回结果，不额外去查另一个通道。
                    _say(texts.SET_ABOUT_CAT_5
                         % (cur, updater.display_version(info.get("tag") or "？"),
                            ch_text_, updater.AHEAD_EGG) + note, theme.c("warndim"))
                else:
                    # msg 自带"检查更新失败："前缀，这里不再叠一层（2026-10-03）
                    msg = info.get("msg") or texts.SET_ABOUT_N_2
                    _say(msg + note, theme.c("errdeep"))
                    if dialog:
                        messagebox.showwarning(texts.SET_ABOUT_A0_3, msg)

            def _do_check(channel=None, notify=True):
                if st["busy"]:          # 不叠第二个请求：状态与按钮都由前一个负责收尾
                    return
                # 主线程取快照（通道 + 本机版本），子线程只用这份数据、不读 Tk 变量（坑 54）。
                # `channel` 显式给出时按它查（进页自动查「正式版」就是走这条），**不去读也
                # 不去改**上面那个下拉的选中值 —— 用户选了测试版，进来一次不该被悄悄改回去。
                ch_code = channel or updater.channel_of_label(ch_var.get())
                ch_text = updater.CHANNEL_LABEL.get(ch_code, ch_var.get())
                cur = updater.display_version(APP_VERSION)
                # 令牌也在主线程取好（坑 54：子线程只用这份快照，不自己去读盘 / 读 Tk）。
                # 空串 = 走匿名接口（限额 60 次/小时）。
                tok = secrets.get_github_token()
                st["busy"] = True
                st["at"] = time.time()      # 冷却从"真发出了一次请求"起算（失败也算，别反复打）
                btn_chk.configure(state="disabled")     # 防连点：一次只发一个请求
                btn_open.configure(state="disabled")
                up_url["v"] = ""
                _say(texts.SET_ABOUT_CAT_6 % (ch_text, cur))

                def done(state, info):
                    st["busy"] = False
                    st["last"] = (state, info)      # 冷却期进页靠它回显（见下面 _enter_about）
                    r = st.get("render")
                    if callable(r):
                        # 结果写给**当前**那个关于页：用户可能已经关掉又重开了
                        r(state, info, dialog=bool(notify and ust_lab.winfo_exists()))

                def work():
                    try:
                        # cache：上次响应的 ETag 与结果带下去 —— 没变化时 GitHub 回 304
                        # （不计入匿名限流额度），直接复用上次那份，不重新解析
                        res = updater.check_update(cur, ch_code, cache=st["cache"],
                                                   token=tok)
                    except Exception as e:          # 兜底：异常不许穿回 UI 线程
                        res = (updater.STATE_ERROR, {"msg": texts.APP_UPD_CHECK_ERR % e})
                    self._ui_q.put(lambda: done(*res))

                threading.Thread(target=work, daemon=True).start()

            # 控件放在回调之后建：`command=名字` 是建控件那一刻就要绑定的（坑 115）。
            # 顺序按 W 2026-10-04 的要求：检查更新 → 正式版/测试版 → 打开下载页 → "?"，
            # 并且**删掉左边的「版本类型」标签** —— 那一行的内容自解释，不需要一个名词占位。
            uf = ttk.Frame(t9)
            btn_chk = theme.button(uf, text=texts.SET_ABOUT_A0_3, width=12, command=_do_check)
            btn_chk.pack(side="left", padx=(0, 6))
            ch_cb = theme.comb(uf, textvariable=ch_var, state="readonly", width=10,
                                 values=[lab for _c, lab in updater.CHANNELS])
            ch_cb.pack(side="left", padx=(0, 6))
            btn_open = theme.button(uf, text=texts.SET_ABOUT_A0_2, width=12,
                                  command=lambda: _open_page(up_url["v"]),
                                  state="disabled")
            btn_open.pack(side="left", padx=(0, 6))
            # 普通用户模式不放 "?"（2026-10-07 W 定：关于页完整保留、仅移除 ? 组件）。
            # 对象照建（文案给空 = 不建点控件）：st["help"] 与 _sync_upd_help
            # （开发者选项改令牌后回来重算文案）都还指着它，None 会把那条路打断
            upd_help = widgets.HelpDot(uf, "" if simple else _upd_help_text())
            upd_help.pack(side="left")
            # 挂在 App 级状态里：开发者选项改了令牌，要能回头把这段说明改成实话
            st["help"] = upd_help
            uf.grid(row=r9["i"], column=0, columnspan=3, sticky="w", pady=(4, 0))
            r9["i"] += 1
            ust_lab.grid(row=r9["i"], column=0, columnspan=3, sticky="w", pady=(2, 6))
            r9["i"] += 1

            # 渲染器登记给 App：窗口关掉后这个指针还指着这行控件（`_render` 里用
            # winfo_exists 兜住），而新窗口一建就把它换成新那行 —— 于是"关窗之后结果才
            # 回来"既不会写丢，也不会写进一个已经不存在的窗口。
            st["render"] = _render

            # 进页就自动按「正式版」查一次（W 2026-10-04），但带 **10 分钟冷却**
            # （W 2026-10-04 加，`updater.COOLDOWN`）：来回翻设置页不再反复打 GitHub 匿名
            # 接口（每 IP 每小时 60 次）。冷却只拦"自动查"这条路径，手动点按钮不受限。
            #
            # 冷却期**不能装作刚查过**（W 点名的就是这条文案异常）：把上次结果原样写出来，
            # 末尾注明检查时刻与"要立刻复查请点按钮"，让人一眼看出这不是刚刚的结果。
            # 这一次同样 `notify=False`：不弹窗，只写这行状态。
            def _enter_about():
                if st["busy"]:          # 上一个请求还在飞：把同一句"正在查"说出来
                    _say(texts.SET_ABOUT_CAT_6
                         % (updater.CHANNEL_LABEL[updater.CHANNEL_STABLE],
                            updater.display_version(APP_VERSION)))
                    return
                # 冷却多久**看有没有令牌**（updater.cooldown_seconds）：匿名 10 分钟是因为
                # 60 次/小时的额度经不起来回翻页，填了令牌就是 5000 次/小时、压到 5 秒。
                cd = updater.cooldown_seconds(secrets.get_github_token())
                if updater.cooldown_left(st["at"], time.time(), cd) <= 0:
                    _do_check(channel=updater.CHANNEL_STABLE, notify=False)
                    return
                last, r = st.get("last"), st.get("render")
                if last is None or not callable(r):
                    # 没有上次结果可回显（走不到：`at` 一旦设上，结果回来时必写 `last`；
                    # 结果没回来则 busy 已在上面拦掉）—— 真遇上就照常查一次，别留空话
                    _do_check(channel=updater.CHANNEL_STABLE, notify=False)
                    return
                # W 2026-10-05：这里原来会追一句"（检查时间 HH:MM，N 分钟内不再自动查…）"——
                # 冷却是我们自己的节流，用户不需要知道（写出来反而像在解释自己的毛病）。
                # 冷却照旧生效，只是不再说出来；上次结果本身照旧原样回显。
                r(last[0], last[1])

            enter_hooks.setdefault("about", []).append(_enter_about)

            # 高分屏清晰度（DPI 感知）。**冷切换**：Windows 只允许一个进程标一次，
            # 窗口一建出来就改不动了，所以这里只能"记住 + 下次生效"，不能骗用户说立刻变。
            # 判据用 diagnose 现查，而不是照抄配置：配置里写的是"想要哪档"，
            # 真正跑起来是哪档只有 Windows 知道（清单里带了 dpiAware 时配置就不作数）。
            from ..core import diagnose as _diag
            now = _diag._dpi_aware()
            dpi_var = tk.BooleanVar(value=bool(int(self.cfg.get("dpi_aware", 1) or 0)))
            dpi_status = tk.StringVar(value="")
            df = ttk.Frame(t9)
            i = r9["i"]
            r9["i"] += 1
            df.grid(row=i, column=0, columnspan=3, sticky="w", pady=(8, 0))

            def set_dpi():
                self.cfg["dpi_aware"] = 1 if dpi_var.get() else 0
                save_config(self.cfg)
                dpi_status.set(texts.SET_ABOUT_A0_4)

            theme.check(df, text=texts.SET_ABOUT_N_3, variable=dpi_var,
                            command=set_dpi).pack(side="left")
            ttk.Label(df, text=texts.SET_ABOUT_CAT_7 % (texts.SET_ABOUT_N_4 if now else texts.SET_ABOUT_N_5),
                      style="Hint.TLabel", font=("Microsoft YaHei UI", 9)).pack(
                side="left", padx=(10, 0))
            if not simple:      # 普通用户模式不放 "?"（2026-10-07 W 定）
                widgets.HelpDot(df, texts.SET_ABOUT_A1_2).pack(side="left", padx=(6, 0))
            ttk.Label(t9, textvariable=dpi_status, style="Hint.TLabel", wraplength=360,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r9["i"], column=0, columnspan=3, sticky="w")

        # ---- 开发者页（隐藏入口：关于页版本号连点 5 次，见 `DEV_NAV_ITEM`）----
        @section("dev", "dev")
        def _t_dev(t_dev, r_dev):
            """开发者选项：关掉这个模式 / 填 GitHub 令牌 / 把本软件签名列入本机可信名单 /
            把本页填过的东西清回默认。

            **不放 "?"**（W 2026-10-04 点名）：目标用户是开发者，这一页每件事都自解释。
            文案按"开发者"口吻压缩：只留状态与怎么重新进来，限额/冷却这类数字属于
            关心就查的细节（W 2026-10-04），解释性长句一律收进按钮弹窗里。
            这里的设置**不随"关闭开发者模式"消失**（W 2026-10-04 明确要求）：关掉的只是入口
            —— 令牌在 secrets.json、已提醒过的版本在配置里，两处都不看这个开关。
            """
            tok_lab = tk.StringVar()
            msg_lab = tk.StringVar()

            def _refresh():
                t = secrets.get_github_token()
                if t:
                    tok_lab.set(texts.SET_DEV_CAT + secrets.mask(t))
                else:
                    tok_lab.set(texts.SET_DEV_A0)

            def _sync_upd_help():
                """令牌一变，关于页那个 "?" 里"多久不再自动查"就得跟着变（见 `_upd_help_text`）。
                关于页没建过、或它那个窗口已关，就什么都不用做（文本是悬停时才读的）。"""
                h = self._upd_check.get("help")
                if h is None:
                    return
                h.text = _upd_help_text()

            def close_dev():
                self._dev_mode = False
                _rebuild_nav()              # 左栏把「开发者选项」收走
                nav.select("about")         # 把用户放回「关于」——他刚点的那个入口就在那儿

            def fill_token():
                d = tk.Toplevel(win)
                theme.style_toplevel(d, "panel")   # 经典背景 + 标题栏两件套（坑 186）
                d.withdraw()                # 先藏起来，摆正了再显示（否则左上角闪一下）
                d.title(texts.SET_DEV_A0_2)
                widgets.panel_label(d, text=texts.SET_DEV_A0_2,
                                    font=("Microsoft YaHei UI", 10, "bold")).pack(
                    anchor="w", padx=14, pady=(12, 4))
                widgets.panel_label(d, text=texts.SET_DEV_N,
                          fg_key="hint", wraplength=430, justify="left",
                          font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
                e = theme.entry(d, width=48, show="●")
                e.pack(padx=14, pady=10)
                widgets.panel_label(d, textvariable=tok_lab, fg_key="hint",
                                    font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
                bf = tk.Frame(d, background=widgets.panel_bg(d))
                bf.pack(padx=14, pady=(6, 14), anchor="e")

                def save():
                    k = e.get().strip()
                    if not k:       # 空 = 不改（要清掉有下面那个「清除」）
                        d.destroy()
                        return
                    secrets.set_github_token(k)
                    _refresh()
                    _sync_upd_help()
                    self._dev_upd_start()   # 令牌到手 → 后台那个定时器可以起来了
                    msg_lab.set(texts.SET_DEV_A0_3)
                    d.destroy()

                def clear():
                    if messagebox.askyesno(texts.SET_DEV_A0_2, texts.SET_DEV_A1):
                        secrets.set_github_token("")
                        _refresh()
                        _sync_upd_help()
                        self._dev_upd_start()   # 令牌没了 → 定时器自己会停
                        msg_lab.set(texts.SET_DEV_A0_4)
                    d.destroy()

                theme.button(bf, text=texts.SET_PROV_N_6, width=8, command=clear).pack(side="left", padx=4)
                theme.button(bf, text=texts.BTN_CANCEL, width=8, command=d.destroy).pack(side="left", padx=4)
                theme.button(bf, text=texts.SET_DEV_N_2, width=10, command=save).pack(side="left")
                e.focus_set()
                widgets.center_on(d, win)   # 摆到设置页正中，别落在屏幕左上角

            def reset_dev():
                t = secrets.get_github_token()
                if not messagebox.askyesno(
                        texts.SET_DEV_A0_5,
                        texts.SET_DEV_CAT_2
                        % (texts.SET_DEV_CAT_3 % secrets.mask(t) if t else texts.SET_DEV_N_3)):
                    return
                secrets.set_github_token("")
                self.cfg["dev_upd_dismissed"] = ""   # 旧键：2026-10-05 前的后台弹窗记录，顺手清
                self.cfg["upd_dismissed"] = ""       # 自动弹窗的版本记录（进页自动查/后台轮询共用）
                save_config(self.cfg)
                _refresh()
                _sync_upd_help()
                self._dev_upd_start()       # 令牌清空了 → 后台定时器停掉
                msg_lab.set(texts.SET_DEV_A0_6)

            def trust_sign():
                """把本程序 exe 的签名证书列入**当前用户**的受信任根（免管理员）。

                顺序：先读现在这份 exe 有没有签名 / 是否已列入 → 把危险点摆给用户确认 →
                确认后才动手。危险点是「信了这张证书 = 这台机器会信所有用它签的东西」，
                所以那一句必须原样说给用户听，不能替他跳过（`core/codesign.py` 开头有边界说明）。
                """
                info, rwhy = codesign.read_cert_detail()
                if info is None:
                    msg_lab.set(texts.SET_DEV_CAT_4 % (rwhy or texts.SET_DEV_N_4))
                    return
                if info.get("trusted"):
                    msg_lab.set(texts.SET_DEV_A0_7)
                    return
                if not messagebox.askyesno(
                        texts.SET_DEV_A0_8,
                        texts.SET_DEV_CAT_5
                        % (info.get("subject") or texts.SET_DEV_N_5,
                           info.get("thumbprint") or "")):
                    return
                good, why = codesign.trust()
                if good:
                    self._codesign_info = dict(info, trusted=True)   # 下次建页不再显示这条
                    if btn_trust_user is not None:
                        btn_trust_user.pack_forget()                 # 已经信任了，这条没用了
                    msg_lab.set(texts.SET_DEV_A0_9)
                else:
                    msg_lab.set(texts.SET_DEV_CAT_4 % why)

            def trust_sign_machine():
                """把本程序 exe 的签名证书列入**本机（所有用户）**信任根 —— 需要管理员。

                比用户级那一步重得多：影响这台机器上**所有** Windows 用户，而且要过 UAC。
                所以警告写得比用户级那条更狠（W 点名的"很严重的警告"），并把"接下来会弹
                管理员授权"提前说清楚 —— 用户在 UAC 上点取消就是放弃，不是失败。
                """
                info, rwhy = codesign.read_cert_detail()
                if info is None:
                    msg_lab.set(texts.SET_DEV_CAT_4 % (rwhy or texts.SET_DEV_N_4))
                    return
                if info.get("trusted_machine"):
                    msg_lab.set(texts.SET_DEV_A0_10)
                    return
                if not messagebox.askyesno(
                        texts.SET_DEV_A0_11,
                        texts.SET_DEV_CAT_6
                        % (info.get("subject") or texts.SET_DEV_N_5,
                           info.get("thumbprint") or "")):
                    return
                msg_lab.set(texts.SET_DEV_A0_12)
                ok, why = codesign.trust_machine()
                if ok:
                    msg_lab.set(texts.SET_DEV_A0_13)
                else:
                    msg_lab.set(texts.SET_DEV_CAT_4 % why)

            def untrust_sign():
                """撤销**当前用户**的信任（16 §19.3）：与 trust_sign 对称的那一半。

                只动 CurrentUser（免管理员）；机器级那份是管理员写进系统的，
                这里不假装能撤 —— 那句话在文案里说清，别让用户白点。
                """
                info, rwhy = codesign.read_cert_detail()
                if info is None:
                    msg_lab.set(texts.SET_DEV_CAT_4 % (rwhy or texts.SET_DEV_N_4))
                    return
                if not info.get("trusted"):
                    msg_lab.set(texts.SET_DEV_N_11)
                    return
                if not dialogs.ask_confirm(
                        t_dev, texts.SET_DEV_N_10,
                        texts.SET_DEV_CAT_7
                        % (info.get("subject") or texts.SET_DEV_N_5,
                           info.get("thumbprint") or ""),
                        danger=True, ok_text=texts.SET_DEV_N_10):
                    return
                ok, why = codesign.untrust()
                if ok:
                    self._codesign_info = dict(info, trusted=False)
                    msg_lab.set(texts.SET_DEV_A0_14)
                else:
                    msg_lab.set(texts.SET_DEV_CAT_8 % why)

            btn_trust_user = None       # 用户级那个按钮（已信任时不建；点了成功就收起来）

            def _trusted_here():
                """这张签名是不是已经在这台机器上被信任了（用户级**或**机器级）。

                读启动那次检测的结果（`App._codesign_info`）：拿不到就按"还没信任"处理 ——
                宁可能点（点进去也会被告知已信任），别把按钮藏得用户找不到。
                机器级也要算（16 §19.4②）：走过「所有用户」那条的人，用户级那一格
                永远是空的，只判 user 会让他看见一个点过又点一次的按钮。
                """
                info = getattr(self, "_codesign_info", None)
                return bool(info and (info.get("trusted") or info.get("trusted_machine")))

            # 控件放在回调之后建：`command=名字` 是建控件那一刻就要绑定的（坑 115）
            i = r_dev["i"]
            r_dev["i"] += 1
            ttk.Label(t_dev, text=texts.SET_DEV_N_6,
                      style="Note.TLabel", wraplength=720, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(0, 6))

            i = r_dev["i"]
            r_dev["i"] += 1
            # 按钮**竖直排列**（W 2026-10-05）：关闭在最上、重置在最下，两条都"始终"成立
            # —— 所以签名那条按需少建一个时，也不许挪动这两头的位置。
            brf = ttk.Frame(t_dev)
            brf.grid(row=i, column=0, columnspan=3, sticky="w", pady=(2, 6))
            theme.button(brf, text=texts.SET_DEV_N_7,
                       command=close_dev).pack(side="top", anchor="w", pady=(0, 4))
            theme.button(brf, text=texts.SET_DEV_N_8,
                       command=fill_token).pack(side="top", anchor="w", pady=(0, 4))
            # 「本机已写入签名」就不再显示用户级那一条（W 2026-10-05）：判据用启动时那次
            # 检测的结果（`App._codesign_info`），不在这里再起一次 PowerShell 卡界面。
            if not _trusted_here():
                btn_trust_user = theme.button(brf, text=texts.SET_DEV_A0_8,
                                            command=trust_sign)
                btn_trust_user.pack(side="top", anchor="w", pady=(0, 4))
            else:
                # 只有"当前用户级"真信过才给撤销（16 §19.3）：机器级那份是管理员
                # 写进系统的，这里撤不掉，摆一个按不动的按钮只会误导。
                if (getattr(self, "_codesign_info", None) or {}).get("trusted"):
                    theme.button(brf, text=texts.SET_DEV_N_10,
                               command=untrust_sign).pack(side="top", anchor="w", pady=(0, 4))
            theme.button(brf, text=texts.SET_DEV_N_9,
                       command=trust_sign_machine).pack(side="top", anchor="w", pady=(0, 4))
            theme.button(brf, text=texts.SET_DEV_A0_5,
                       command=reset_dev).pack(side="top", anchor="w")

            for var, fg in ((tok_lab, theme.c("note")), (msg_lab, theme.c("oklit"))):
                i = r_dev["i"]
                r_dev["i"] += 1
                ttk.Label(t_dev, textvariable=var, foreground=fg, wraplength=720,
                          justify="left", font=("Microsoft YaHei UI", 9)).grid(
                    row=i, column=0, columnspan=3, sticky="w", pady=(0, 4))
            _refresh()

        # ---- 区块 10：引擎管理（与「模型文件管理」同一页；左栏合成一项「模型文件与引擎」，
        #      这一段的锚点靠 nav_hide 走 jump="eng" 定位）----
        @section("files", "eng")
        def _t10(t10, r10):
            """引擎管理：说清哪个引擎就位了没有 + 可选档位 + 一键装到程序目录的 engines 下。

            **口径（W 2026-10-05 第二轮重构）**：
              · 定向：**进这一屏会自动扫一遍**（W 2026-10-08 删掉了「自动定向」按钮 ——
                引擎没就位时进页自动按 `engine_install.auto_locate` 找一次，找到就写回指路
                并刷新状态行；找不到保持原提示、不弹窗）。「手动定向」= 用户自己指一个文件夹。
                两个引擎与模型文件同处一个目录也各认各的 —— 判据是 `engine_install.find_exe`：
                **按 exe 名**找，不看目录名。
              · 「检查更新」联网查可用版本；查到比**已装版本**更新的就原地变成「更新引擎」，
                点它开始下载安装、按钮再变成「取消安装」；已是最新则维持「检查更新」。
                已装版本记在 `cfg["engine_installed"]`（自动安装成功时写入；手动定向 / 用户
                自己放的引擎不记 ⇒ 被当成"可能有新版"，程序无从知道他那份是什么版本）。
              · 「打开下载页」已删（W 2026-10-05）：下不动就「复制下载链接」，拿到的就是
                选中档位的安装包直链（没查过版本才退回发布列表页）。

            **开页零请求**（这条没变）：不注册 `enter_hooks`，不点「检查更新」不联网；
            版本清单也不自动提示 —— ETag 只省额度，不变成提示。

            定向与安装**会**替用户写 `exe` / `sd_dir`：定向本身就是"用户要求定位引擎"；
            安装成功后指向刚装的那份是 W 2026-10-05 明确要求的（**推翻了** `15` §2 第 4 条
            "装完只报路径、不自动改配置"，已登记 `99`）。
            """
            # (键, 标题, 用户在配置里填的那项, 真正要存在才行的文件, 该放哪儿, 目录怎么称呼, 指路在哪, 额外说明)
            engines = [
                ("llama",
                 texts.SET_FENG_L1,
                 lambda: str((self.cfg.get("exe") or "").strip()),
                 lambda: str((self.cfg.get("exe") or "").strip()),
                 lambda: os.path.dirname(str((self.cfg.get("exe") or "").strip())) or APP_DIR,
                 texts.SET_FENG_L5,
                 texts.SET_FENG_L6,
                 texts.SET_FENG_L7),
                ("sd",
                 texts.SET_FENG_L1_2,
                 lambda: str((self.cfg.get("sd_dir") or "").strip()),
                 lambda: os.path.join(str((self.cfg.get("sd_dir") or "").strip()),
                                      engine_install.exe_name("sd")),
                 lambda: str((self.cfg.get("sd_dir") or "").strip()),
                 texts.SET_FENG_L5_2,
                 texts.SET_FENG_L6_2,
                 texts.SET_FENG_L7_2),
            ]

            def _state_of(cfg_of, exe_of):
                # 判"没指路"要看**用户填的那一项**，不能拿拼出来的文件路径判空：
                # sd 那项拼的是 os.path.join(sd_dir, 可执行文件名)，sd_dir 留空时得到
                # 相对路径 "sd-cli.exe"，非空但也不存在 —— 报"找不到文件"就冤枉人了
                # （和坑 42 里 `os.path.join("", "output")` 在 CWD 建目录是同一个根子）。
                # 四态里的"未就位 / 已就位"落在这里，"未就位"后面跟原因（W 2026-10-05）。
                if not str(cfg_of() or "").strip():
                    return texts.SET_FENG_L0, theme.c("errdeep")
                exe = str(exe_of() or "")
                if os.path.isfile(exe):
                    return texts.SET_FENG_CAT + exe, theme.c("oklit")
                return texts.SET_FENG_CAT_2 + exe, theme.c("errdeep")

            # ---- 这几个 helper **定义在循环之外**，全部显式收参。
            #
            # 为什么不能定义在循环里按默认参数绑（那正是下面注释里写的正确做法）：
            # 一旦循环内的函数**按名字**去调另一个循环内定义的函数，那个名字在运行时
            # 解析到的是**最后一次循环**的定义 —— 也就是两个引擎都去动 sd 那份。
            # 2026-10-04 真踩过：点 llama 的「检查更新」（当时叫「刷新版本」），写进状态行
            # 的是 sd 的版本行（坑 124 的同族，只是方向反过来）。
            def _say(sv, stl, text="", color=theme.c("dim")):
                sv.set(text)
                stl.configure(foreground=color)

            def _newer_tag(b, k):
                """查到的最新可用版本相对**已装版本**：新的返回 tag；已是最新 / 查不到返回空串。"""
                builds = b.get("builds") or []
                latest = str((builds[0].get("tag") if builds else "") or "")
                if not latest:
                    return ""
                installed = str((self.cfg.get("engine_installed") or {}).get(k) or "")
                if not installed:
                    return latest          # 没记录 ⇒ 无从知道他那份是什么版本，当作可能有新版
                ik, lk = engine_install.build_key(installed), engine_install.build_key(latest)
                if ik and lk and ik >= lk:
                    return ""              # 已装的不比线上旧（含"自己装过更新的 nightly"）
                return latest

            def _flavor_line(b, k, fv):
                """「查询到新版本」那一行：版本 + 档位 + 体积（含不含运行库）。"""
                if not b.get("builds"):
                    return texts.SET_FENG_RET
                build = engine_install.pick_build(b["builds"], fv.get())
                fl = [f for f in engine_install.flavors_from(build) if f["id"] == fv.get()]
                if not fl:
                    return texts.SET_FENG_CAT_3 % (
                        build.get("tag") or "？")
                mb = fl[0]["size"] / 1048576.0
                extra = texts.SET_FENG_N if fl[0].get("extra") else ""
                return texts.SET_FENG_CAT_4 % (
                    build.get("tag") or "？", fl[0].get("label") or fv.get(), mb, extra)

            def _plan_of(b, k, fv):
                """当前选中的档位落成下载清单；没查过版本就返回 None（不猜）。"""
                if not b["builds"] or not fv.get():
                    return None
                build = engine_install.pick_build(b["builds"], fv.get())
                if not build:
                    return None
                return engine_install.plan(k, build, fv.get())

            def _on_pick(b, k, fv, sv, stl):
                """换档位就换状态行：版本号 + 档位 + 体积（"查询到新版本"这一态的家）。"""
                _say(sv, stl, _flavor_line(b, k, fv))

            def _guard(b, k, co):
                """装之前拦一下"正在被用"的那份引擎：覆盖它等于把跑着的服务弄坏。

                判活两级（2026-10-11 排查报告，与 _confirm_shared_vram 的收紧同思路，
                坑 189）：本程序旗标优先；旗标说没跑而全机有 llama-server.exe 时，
                按**本配置端口** HTTP 复核一次——映像名判活分不清"别人挂在别的端口
                上的实例"，原来直接拦会把别窗口 / vbs / 别的前端的服务误伤进来。
                """
                cur = str(co() or "").strip()
                if not cur:
                    return ""
                try:
                    same = os.path.normcase(os.path.dirname(cur)) == os.path.normcase(
                        engine_install.default_dir(k))
                except Exception:
                    return ""
                if not same:
                    return ""
                if self._server_alive_flag or (
                        server_process_alive() and server_alive(self.cfg)):
                    return texts.SET_FENG_RET_2
                return ""

            eng_refresh = {}      # {引擎键: 该引擎状态行的刷新函数}，给"进页自动定向"用
            for key, title, cfg_of, exe_of, dir_of, what, setting, note in engines:
                i = r10["i"]
                r10["i"] += 1
                th = tk.Frame(t10, background=widgets.default_bg())
                th.grid(row=i, column=0, columnspan=3, sticky="w", pady=(10, 2))
                tk.Label(th, text=title, font=("Microsoft YaHei UI", 10, "bold"),
                         background=widgets.default_bg(), anchor="w").pack(side="left")
                if not simple:      # 普通用户模式不放 "?"（引擎管理的功能与按钮本身不变）
                    widgets.HelpDot(th, texts.SET_FENG_CAT_5
                                         + note +
                                         texts.SET_FENG_CAT_6).pack(side="left", padx=(6, 0))

                # 状态行：四态文案（未就位 / 已就位 / 正在查 / 查询到新版本）**都写在这一行**，
                # 不另设提示行（W 2026-10-05）。变量挂控件保活（坑 145 ①）。
                st_var = tk.StringVar(value="")
                st = ttk.Label(t10, textvariable=st_var, wraplength=600, justify="left",
                               style="Dim.TLabel", font=("Microsoft YaHei UI", 9))
                st.grid(row=r10["i"], column=1, columnspan=2, sticky="w", pady=(0, 2))
                r10["i"] += 1

                # 按钮分两行（W 2026-10-05 第二轮；2026-10-08 删「自动定向」→ 三个其他按钮）：
                # 上一行是三个"其他按钮"（复制下载链接 / 手动定向 / 打开目标目录），
                # 下一行**只放**合并按钮「检查更新」——
                # 它右边挂档位下拉，**点过「检查更新」才显示**。`ScrollPage` 只竖滚不横滚
                # （坑 106），但这四个按钮按字符宽实测仍在可视区内（自检里有一条右界断言）。
                bf = ttk.Frame(t10)
                bf.grid(row=r10["i"], column=1, columnspan=2, sticky="w", pady=(2, 2))
                r10["i"] += 1
                vf = ttk.Frame(t10)
                vf.grid(row=r10["i"], column=1, columnspan=2, sticky="w", pady=(0, 6))
                r10["i"] += 1

                flavor_var = tk.StringVar(value="")
                # width 14：第四态「补全缺失文件」6 个汉字，12 字符宽会挤（坑 280）
                btn_act = theme.button(vf, text=texts.SET_ABOUT_A0_3, width=14)
                btn_act.pack(side="left")
                # 档位下拉先建不 pack：**点过「检查更新」才 pack 出来**（W 2026-10-05）。
                # 构造时就绑 textvariable（坑 112：只 set 变量而没绑，界面是空的而数据是对的）。
                flavor_lbl = ttk.Label(vf, text=texts.SET_FENG_N_2, style="Dim.TLabel",
                                       font=("Microsoft YaHei UI", 9))
                flavor_cb = theme.comb(vf, textvariable=flavor_var, state="readonly",
                                         width=16, values=[])
                shown = {"v": False}

                def _show_flavor(lbl=flavor_lbl, cb=flavor_cb, sh=shown):
                    if sh["v"]:
                        return
                    sh["v"] = True
                    lbl.pack(side="left", padx=(10, 4))
                    cb.pack(side="left")

                # 这一行引擎的会话状态：可用版本 / 正在装 / 取消标志 / 当前模式。
                # mode：idle（未就位或已就位）· checking（正在查）· ready（查到新版本）·
                #       installing（整包下载安装）· repair（exe 在但缺 CUDA 运行库）·
                #       repair_body（exe 不在，主体不完整）· repairing（补全下载合并中）。
                # 按钮文字与状态行都由 `_render` 按它刷（坑 280 加后三态）。
                box = {"builds": [], "busy": False, "stop": None, "mode": "idle",
                       "checked": False}

                def _render(b=box, co=cfg_of, so=exe_of, sv=st_var, stl=st, btn=btn_act,
                            fv=flavor_var, k=key):
                    """按当前 mode 刷按钮文字与状态行（各态文案的唯一出口）。"""
                    mode = b.get("mode") or "idle"
                    if mode == "checking":
                        _say(sv, stl, texts.SET_FENG_A2, theme.c("warnlit"))
                        btn.configure(text=texts.SET_ABOUT_A0_3, state="disabled")
                        return
                    if mode == "ready":
                        _say(sv, stl, _flavor_line(b, k, fv))
                        btn.configure(text=texts.SET_FENG_N_3, state="normal")
                        return
                    if mode in ("installing", "repairing"):
                        btn.configure(text=texts.SET_FENG_N_4, state="normal")
                        return
                    if mode == "repair":
                        # CUDA 两个 zip 只放齐一个：exe 在、ggml-cuda 在、cudart64 缺
                        _say(sv, stl,
                             texts.SET_FENG_CAT_7 % so(),
                             theme.c("warnlit"))
                        btn.configure(text=texts.SET_FENG_N_5, state="normal")
                        return
                    if mode == "repair_body":
                        _say(sv, stl,
                             texts.SET_FENG_CAT_8
                             % engine_install.exe_name(k),
                             theme.c("errdeep"))
                        btn.configure(text=texts.SET_FENG_N_5, state="normal")
                        return
                    text, color = _state_of(co, so)
                    if b.get("checked") and text.startswith(texts.SET_FENG_A0):
                        text += texts.SET_FENG_TEXT      # 查过且没有更新版本
                    _say(sv, stl, text, color)
                    btn.configure(text=texts.SET_ABOUT_A0_3, state="normal")

                def _refresh_inspect(b=box, k=key, do=dir_of, co=cfg_of, rf=_render):
                    """看这份引擎是不是半装，据结果在 idle / repair / repair_body 间定态。

                    只在**空闲类**状态、已指路、且没在忙时看目录（一次 listdir，几毫秒；
                    坑 280）。查到新版本的 ready / 正在查 / 正在装都不碰——那几态优先级
                    更高。判据在 `engine_install.inspect_engine` 一处（diagnose 也用它）。
                    """
                    if b.get("busy") or (b.get("mode") not in
                                         ("idle", "repair", "repair_body")):
                        return
                    if not str(co() or "").strip():
                        b["mode"] = "idle"
                        rf()
                        return
                    try:
                        insp = engine_install.inspect_engine(k, do())
                        miss = insp.get("missing") or ""
                    except Exception:
                        miss = ""
                    b["mode"] = {"cudart": "repair",
                                 "body": "repair_body"}.get(miss, "idle")
                    rf()

                def _apply_dir(kk, folder):
                    """把选中的文件夹写回该引擎的指路项。

                    形状归一（llama 存 exe 路径 / sd 存目录）在 `engine_install.set_dir` 一处
                    —— **首次打开的自动扫描走的是同一个函数**，不在这里再写一遍（坑 128）。
                    （原来还要回填「服务参数」的 exe 与生图页的 sd_dir 输入框防旧值回写；
                    那两行 2026-10-16 已删 —— 这两个键现在只有本屏一处入口，没人再会覆盖它们。）
                    """
                    engine_install.set_dir(self.cfg, kk, folder)
                    save_config(self.cfg)
                    # 引擎刚就位往往就是"第一个可用模型"的时刻（模型早就下好了只差引擎）
                    # ⇒ 立刻切过去，不等状态轮询（坑 150）
                    try:
                        self._maybe_adopt_first_model(force=True)
                    except Exception:
                        pass

                def do_check(_e=None, b=box, k=key, cb=flavor_cb, fv=flavor_var,
                             sv=st_var, stl=st, rf=_render, sf=_show_flavor,
                             ri=_refresh_inspect):
                    # ri 必须在**这一层**（循环体当场执行的 def）默认参数上绑定：内层
                    # done 的默认参数要等到 do_check 被调用时才求值，那时名字已漂到
                    # 最后一轮 sd（坑 124 同款，坑 280 排查时实踩）。
                    """点「检查更新」才联网（开页不联网：首屏与无头自检都不该被网络拖累）。

                    令牌与显卡型号都在主线程取快照再进子线程（子线程不读 Tk 变量、不翻配置，坑 54）。
                    """
                    if b["busy"]:
                        return
                    tok = secrets.get_github_token()
                    # 有值就不必 spawn nvidia-smi（适配器枚举那一级照走，毫秒级、不 spawn）
                    hw = {"gpu_name": str(self.cfg.get("gpu_name") or "")}
                    b["busy"] = True
                    b["mode"] = "checking"
                    rf()

                    def done(builds, why, kinds, kk=k, bb=b, cb=cb, vv=fv, sv=sv, stl=stl,
                             rf=rf, sf=sf, ri=ri):
                        bb["busy"] = False
                        if not builds:
                            bb["mode"] = "idle"
                            rf()
                            _say(sv, stl, why or texts.SET_FENG_N_6, theme.c("errdeep"))
                            return
                        bb["builds"] = builds
                        ids = [f["id"] for f in engine_install.flavors_from(builds[0])]
                        cb.configure(values=ids)
                        if ids:
                            # 默认档位按本机显卡挑（W 2026-10-04）：没有 N 卡时不许默认 CUDA
                            # ——显示序把 CUDA 排最前只是"清单顺序"，默认值另算。判据在
                            # `engine_install.recommend_flavor`（一处）。
                            vv.set(engine_install.recommend_flavor(builds[0], kinds) or ids[0])
                        sf()                       # 点过「检查更新」才显示档位下拉
                        if _newer_tag(bb, kk):
                            bb["mode"] = "ready"   # 状态行报「查询到新版本」，按钮变「更新引擎」
                            rf()
                        else:
                            # 先退出 checking 态：_refresh_inspect 的守卫只放行
                            # idle/repair*，不复位它会把这次刷新整个跳过（状态行
                            # 停在"正在查…"）
                            bb["mode"] = "idle"
                            bb["checked"] = True   # 查过且没有更新版本
                            # 查完也顺手看一眼半装（坑 280）：没新版但目录缺 cudart
                            # 时，这一步把按钮切成「补全缺失文件」
                            ri()

                    def work():
                        try:
                            builds, why = engine_install.list_builds(k, token=tok)
                        except Exception as e:       # 兜底：异常不许穿回 UI 线程（坑 54）
                            builds, why = [], texts.SET_FENG_CAT_9 % e
                        # 探显卡放在联网之后、且只在**子线程**里做（坑 4：最坏会 spawn
                        # nvidia-smi 等满 10 秒，放这儿不冻界面）。三级判据便宜的先问，只有
                        # "没认到 N 卡"才走到 spawn 那一级，而那类机器通常连 nvidia-smi 都没装
                        # （"找不到文件"级别的花费）。探失败就当"认不出"，默认值退 CPU 档，不猜。
                        try:
                            kinds = hardware.gpu_kinds(hw)
                        except Exception:
                            kinds = set()
                        self._ui_q.put(lambda: done(builds, why, kinds))

                    threading.Thread(target=work, daemon=True).start()

                def copy_link(_e=None, b=box, k=key, fv=flavor_var, sv=st_var, stl=st):
                    """复制**选中档位的安装包直链**；没查过版本才退回发布列表页。

                    原来复制的是发布列表页地址，用户贴过去还要自己翻哪个包；
                    查过版本后直接给能点的那个包。
                    """
                    p = _plan_of(b, k, fv)
                    u = (p["files"][0]["url"] if p and p["files"]
                         else engine_install.page_url(k))
                    try:
                        t10.winfo_toplevel().clipboard_clear()
                        t10.winfo_toplevel().clipboard_append(u)
                        _say(sv, stl, texts.SET_FENG_N_7
                             if p and p["files"] else texts.SET_FENG_N_8)
                    except Exception as e:
                        messagebox.showwarning(texts.SET_FENG_A0_2, texts.SET_FENG_CAT_10 % e)

                def manual_dir(_e=None, k=key, b=box, rf=_render, ad=_apply_dir,
                               ri=_refresh_inspect, do=dir_of, co=cfg_of):
                    """「手动定向」：用户自己指一个文件夹（该引擎所在目录）。"""
                    folder = filedialog.askdirectory(
                        title=texts.SET_FENG_CAT_11 % engine_install.exe_name(k),
                        parent=t10.winfo_toplevel())
                    if not folder:
                        return
                    exe = os.path.join(folder, engine_install.exe_name(k))
                    if not os.path.isfile(exe) and not messagebox.askyesno(
                            texts.SET_FENG_A0_3,
                            texts.SET_FENG_CAT_12
                            % engine_install.exe_name(k)):
                        return
                    ad(k, folder)
                    b["mode"] = "idle"      # 先退忙碌/ready 态，ri 的守卫才放行
                    b["checked"] = False
                    # 用户指的目录可能正是半装那一份：重判一次（坑 280）
                    ri(b, k, do, co, rf)

                def do_install(_e=None, b=box, k=key, sv=st_var, stl=st, fv=flavor_var,
                               co=cfg_of, do=dir_of, btn=btn_act, rf=_render,
                               ad=_apply_dir, ri=_refresh_inspect, setting=setting):
                    # ri/do 也必须在这一层绑定（同 do_check：内层 finish 默认参数晚求值，
                    # 直接写名字会漂到 sd 轮，坑 124/280）
                    """点「更新引擎」：选档 → 下载校验 → 解压换目录；**装完指向新那份**。"""
                    p = _plan_of(b, k, fv)
                    if not p or not p["files"]:
                        # 同一句也写进状态行：无头自检看不见 messagebox，
                        # 而"没查过就点更新"这条规矩必须能被断言（也更像这页的口径）。
                        _say(sv, stl, texts.SET_FENG_A2_2,
                             theme.c("errdeep"))
                        return
                    why = _guard(b, k, co)
                    if why and not messagebox.askyesno(texts.SET_FENG_N_3, why + texts.SET_FENG_CAT_13):
                        return
                    dest = p["dir"]
                    tok = secrets.get_github_token()
                    b["stop"] = threading.Event()
                    b["busy"] = True
                    b["mode"] = "installing"
                    btn.configure(text=texts.SET_FENG_N_4, state="normal")
                    _say(sv, stl, texts.SET_FENG_CAT_14
                         % (p["files"][0]["name"], p["total"] / 1048576.0))

                    def emit(text):
                        self._ui_q.put(lambda: _say(sv, stl, text))

                    def finish(ok, why_, exe, p=p, b=b, sv=sv, stl=stl, k=k, btn=btn,
                               rf=rf, ad=ad, setting=setting, ri=ri,
                               do=do, co=co):
                        b["busy"] = False
                        b["mode"] = "idle"   # 退 installing 态，ri 守卫才放行
                        if not ok:
                            # 回到目录的真实状态（repair_body 来的失败后仍是 repair_body）
                            ri(b, k, do, co, rf)
                            _say(sv, stl, why_, theme.c("errdeep"))
                            messagebox.showwarning(
                                texts.SET_FENG_N_3,
                                texts.SET_FENG_CAT_15 % why_)
                            return
                        # 记下装上的版本（下次「检查更新」据此判「查询到新版本 / 已是最新」）
                        # + **把路径指到刚装的那份**（W 2026-10-05 明确要求，见本函数口径）
                        rec = dict(self.cfg.get("engine_installed") or {})
                        rec[k] = p.get("tag") or ""
                        self.cfg["engine_installed"] = rec
                        ad(k, p.get("dir") or engine_install.default_dir(k))
                        b["mode"] = "idle"
                        b["checked"] = False
                        ri(b, k, do, co, rf)   # 新装必完整 → idle（坑 280 同一出口）
                        messagebox.showinfo(
                            texts.SET_FENG_N_3,
                            texts.SET_FENG_CAT_16 % (exe, setting))

                    def work():
                        tmp = engine_install.staging_dir()
                        got, why2, exe3, files = True, "", "", []
                        try:
                            for f in p["files"]:
                                emit(texts.SET_FENG_CAT_17
                                     % (f["name"], f["size"] / 1048576.0))
                                # 落盘名净化（R-109）：资产名直取有 `..` / 盘符时可写到
                                # 暂存目录之外；basename + 白名单字符兜底
                                _safe = re.sub(r"[^0-9A-Za-z._-]", "_",
                                               os.path.basename(
                                                   str(f.get("name") or "engine.zip")
                                                   .replace("\\", "/"))) or "engine.zip"
                                dest_f = os.path.join(tmp, _safe)
                                ok, why2 = engine_install.download(
                                    f["url"], dest_f, emit=emit, stop_flag=b["stop"],
                                    digest=f.get("digest") or "",
                                    expected_size=int(f.get("size") or 0))
                                if not ok:
                                    got = False
                                    break
                                f2 = dict(f)
                                f2["path"] = dest_f
                                files.append(f2)
                            if got:
                                ok3, why3, exe3 = engine_install.install(
                                    k, files, dest, emit=emit, stop_flag=b["stop"],
                                    guard=lambda: texts.SET_FENG_N_9 if b["stop"].is_set() else "")
                                got, why2 = ok3, why3
                        except Exception as e:
                            got, why2 = False, texts.SET_FENG_CAT_18 % e
                        finally:
                            engine_install.cleanup(tmp)
                        self._ui_q.put(lambda: finish(got, why2, exe3))

                    def work_safe():
                        try:
                            work()
                        except Exception as e:      # 兜底：异常不许穿回 UI 线程（坑 54）
                            self._ui_q.put(
                                lambda: finish(False, texts.SET_FENG_CAT_18 % e, ""))

                    threading.Thread(target=work_safe, daemon=True).start()

                def do_repair(_e=None, b=box, k=key, sv=st_var, stl=st, fv=flavor_var,
                              co=cfg_of, do=dir_of, btn=btn_act, rf=_render,
                              ri=_refresh_inspect, cb=flavor_cb, sf=_show_flavor,
                              on_install=do_install):
                    """半装一键补全（坑 280）。

                    · repair（exe 在、缺 cudart）：确认后**只下载运行库 zip** 并就地合并进
                      用户现有的引擎目录（不重下主包、不换目录、不改 engine_installed——
                      他那份的版本我们无从知道）；
                    · repair_body（exe 都不在）：无法逐文件知道还缺啥，确认后**整包重装**
                      （复用 do_install：主包+运行库 → 默认目录 → 指路）。
                    没查过版本就先在子线程查一次（与 do_check 同款，子线程不读 Tk 变量）。
                    """
                    mode = b.get("mode") or ""
                    if b.get("busy") or mode not in ("repair", "repair_body"):
                        return
                    # dll/exe 会被运行中的进程占用：先拦（判据与 _guard 同源）
                    if str(co() or "").strip() and (
                            self._server_alive_flag or
                            (server_process_alive() and server_alive(self.cfg))):
                        messagebox.showwarning(
                            texts.SET_FENG_N_5,
                            texts.SET_FENG_A1)
                        return
                    body_mode = (mode == "repair_body")

                    def begin():
                        if body_mode:
                            _begin_body()
                        else:
                            _begin_cudart()

                    def _begin_body():
                        p = _plan_of(b, k, fv)
                        if not p or not p["files"]:
                            messagebox.showwarning(
                                texts.SET_FENG_N_5,
                                texts.SET_FENG_A1_2)
                            return
                        if not messagebox.askyesno(
                                texts.SET_FENG_N_5,
                                texts.SET_FENG_CAT_19
                                % (p["total"] / 1048576.0)):
                            return
                        b["mode"] = "ready"        # do_install 的入口态；其 finish 会再 inspect
                        on_install()

                    def _begin_cudart():
                        build = engine_install.pick_build(b["builds"], fv.get())
                        rp = engine_install.runtime_plan(k, build, fv.get())
                        f = rp.get("file")
                        if not f:
                            messagebox.showwarning(
                                texts.SET_FENG_N_5,
                                texts.SET_FENG_CAT_20
                                % (fv.get() or "？"))
                            return
                        if not messagebox.askyesno(
                                texts.SET_FENG_N_5,
                                texts.SET_FENG_CAT_21
                                % (rp.get("tag") or "？", rp.get("flavor") or fv.get(),
                                   (f.get("size") or 0) / 1048576.0, do())):
                            return
                        target = do()
                        b["stop"] = threading.Event()
                        b["busy"] = True
                        b["mode"] = "repairing"
                        rf()
                        _say(sv, stl, texts.SET_FENG_CAT_14
                             % (f.get("name"), (f.get("size") or 0) / 1048576.0))

                        def emit(text):
                            self._ui_q.put(lambda: _say(sv, stl, text))

                        def finish_rt(ok, why_, merged):
                            b["busy"] = False
                            b["mode"] = "idle"   # 退 repairing 态，ri 守卫才放行
                            ri(b, k, do, co, rf)       # 重新 inspect：补齐→idle，失败→回 repair
                            if not ok:
                                _say(sv, stl, why_, theme.c("errdeep"))
                                if why_ != texts.SET_FENG_N_9:
                                    messagebox.showwarning(
                                        texts.SET_FENG_N_5,
                                        texts.SET_FENG_CAT_22
                                        % why_)
                                return
                            messagebox.showinfo(
                                texts.SET_FENG_N_5,
                                texts.SET_FENG_CAT_23
                                % len(merged))

                        def work():
                            tmp = engine_install.staging_dir()
                            ok, why_, merged = True, "", []
                            zp = ""
                            try:
                                _safe = re.sub(r"[^0-9A-Za-z._-]", "_",
                                               os.path.basename(
                                                   str(f.get("name") or "cudart.zip")
                                                   .replace("\\", "/"))) or "cudart.zip"
                                zp = os.path.join(tmp, _safe)
                                emit(texts.SET_FENG_CAT_17
                                     % (f.get("name"), (f.get("size") or 0) / 1048576.0))
                                ok, why_ = engine_install.download(
                                    f.get("url"), zp, emit=emit, stop_flag=b["stop"],
                                    digest=f.get("digest") or "",
                                    expected_size=int(f.get("size") or 0))
                                if ok:
                                    ok, why_, merged = engine_install.merge_runtime(
                                        k, zp, target, emit=emit, stop_flag=b["stop"])
                            except Exception as e:
                                ok, why_ = False, texts.SET_FENG_CAT_24 % e
                            finally:
                                engine_install.cleanup(tmp)
                            self._ui_q.put(lambda: finish_rt(ok, why_, merged))

                        def work_safe_rt():
                            try:
                                work()
                            except Exception as e:      # 兜底：异常不穿回 UI 线程（坑 54）
                                self._ui_q.put(
                                    lambda: finish_rt(False, texts.SET_FENG_CAT_24 % e, []))

                        threading.Thread(target=work_safe_rt, daemon=True).start()

                    # 已查过版本并选了档 → 直接进确认；否则先联网查（按钮置灰防重复点）
                    if b.get("builds") and fv.get():
                        begin()
                        return
                    tok = secrets.get_github_token()
                    hw = {"gpu_name": str(self.cfg.get("gpu_name") or "")}
                    b["busy"] = True
                    btn.configure(state="disabled", text=texts.SET_FENG_N_5)
                    _say(sv, stl, texts.SET_FENG_A2_3, theme.c("warnlit"))

                    def fetched(builds, why, kinds, bb=b, cb=cb, vv=fv, sv=sv, stl=stl,
                                sf=sf, btn=btn, ri=ri, do=do, co=co, begin=begin):
                        bb["busy"] = False
                        btn.configure(state="normal")
                        if not builds:
                            ri(b, k, do, co, rf)
                            _say(sv, stl, why or texts.SET_FENG_N_6,
                                 theme.c("errdeep"))
                            return
                        bb["builds"] = builds
                        ids = [x["id"] for x in engine_install.flavors_from(builds[0])]
                        cb.configure(values=ids)
                        if ids:
                            vv.set(engine_install.recommend_flavor(builds[0], kinds) or ids[0])
                        sf()
                        begin()

                    def work():
                        try:
                            builds, why = engine_install.list_builds(k, token=tok)
                        except Exception as e:
                            builds, why = [], texts.SET_FENG_CAT_9 % e
                        try:
                            kinds = hardware.gpu_kinds(hw)
                        except Exception:
                            kinds = set()
                        self._ui_q.put(lambda: fetched(builds, why, kinds))

                    threading.Thread(target=work, daemon=True).start()

                def do_click(_e=None, b=box, on_check=do_check, on_install=do_install,
                             on_repair=do_repair):
                    """主按钮按当前 mode 分派：检查更新 / 更新引擎 / 补全 / 取消。

                    为什么合并（W 2026-10-05；坑 280 加补全两态）：这些动作是同一件事的
                    不同阶段或现场，拆成一排按钮既占地方、又要用户自己判断该点哪个。
                    """
                    mode = b.get("mode") or "idle"
                    if mode in ("repair", "repair_body"):
                        on_repair()
                    elif mode == "ready":
                        on_install()
                    elif mode in ("installing", "repairing"):
                        if b["stop"] is not None:
                            b["stop"].set()
                    else:
                        on_check()

                # 顺序按 W 2026-10-05（2026-10-08 去掉「自动定向」后保持其余相对次序）：
                # 复制下载链接 → 手动定向 → 打开目标目录
                theme.button(bf, text=texts.UPDW_BTN_COPYLINK, width=13,
                           command=copy_link).pack(side="left", padx=(0, 6))
                theme.button(bf, text=texts.SET_FENG_A0_3, width=10,
                           command=manual_dir).pack(side="left", padx=(0, 6))
                theme.button(bf, text=texts.SET_FENG_N_10, width=13,
                           command=lambda d=dir_of, w=what, s=setting: _open_outdir(
                               d(), w, s)).pack(side="left")
                btn_act.configure(command=do_click)
                # 包一层把当轮那几个对象绑进去（`_on_pick` 定义在循环之外，只收显式参数）
                flavor_cb.bind("<<ComboboxSelected>>",
                               lambda e=None, b=box, k=key, fv=flavor_var,
                               sv=st_var, stl=st: _on_pick(b, k, fv, sv, stl))
                # 登记 inspect 出口（不是裸 _render）：进页自动定向后调它会先看半装
                # （坑 280）；首屏也走它，已指路的半装一进页就显示「补全缺失文件」
                eng_refresh[key] = _refresh_inspect
                _refresh_inspect()

            # 进页自动定向（W 2026-10-08：删掉「自动定向」按钮，换成进页自动做一次）：
            # 只处理**未就位**的引擎（已就位的一个字节都不动）；判据 / 落点与「首次打开
            # 的自动扫描」**同一处**（`engine_install.auto_locate` / `set_dir`，坑 128）。
            # 找到才写回指路 + 刷新那一行状态行（写回顺带 `_maybe_adopt_first_model`，
            # 见 `_apply_dir`）；找不到保持原提示（「还没指路…」比"没找到"更可操作），
            # 不弹窗。只读扫盘，深度 ≤3、几 ms 量级，挂在进页钩子上（主线程）付得起。
            def _auto_locate_on_enter():
                for k in ("llama", "sd"):
                    try:
                        if engine_install.configured_exe(k, self.cfg):
                            continue
                        found = engine_install.auto_locate(k, self.cfg, app_dir=APP_DIR)
                        if not found:
                            continue
                        _apply_dir(k, os.path.dirname(found))
                        rend = eng_refresh.get(k)
                        if rend is not None:
                            rend()
                    except Exception:
                        continue          # 单个引擎失败不拖累另一个，也不打断进页
            enter_hooks.setdefault("files", []).append(_auto_locate_on_enter)

        # ---- 区块 8：模型文件管理（左栏「模型文件与引擎」那一项指到这里）----
        @section("files", "files")
        def _t5(t5, r5):
            if simple:
                # 普通用户模式（2026-10-07 W 定）：管理本地模型 / 全部重新计算 /
                # 模型来源 三件 + 引擎管理那一屏（eng 区块本身不变）。
                # 「补全缺失项」与「整理模型文件夹」在高级模式；进页自动补全照旧在后台跑。
                for _txt, _cmd in ((texts.SET_FILES_L0, self.open_local_models),
                                   (texts.MDL_RECALC_BTN, self._open_recalc_dialog),
                                   (texts.MDL_POINT_BTN, self._open_model_sources)):
                    theme.button(t5, text=_txt, width=18, command=_cmd).grid(
                        row=r5["i"], column=0, columnspan=3, sticky="w", pady=4)
                    r5["i"] += 1
                enter_hooks.setdefault("files", []).append(self._auto_scan_models)
                return

            def _files_head(n_models):
                # 只留"有多少个模型"：原来把层数 / context / mmproj / 未进菜单四个计数
                # 也拼在这一行里（5 段用 ｜ 隔开），760px 的换行宽度根本兜不住，
                # 必然折成两行、第一行尾巴还参差不齐（W 2026-10-04：删掉这些冗余计数）。
                # 「进这一页会自动补全…」那半句也删了（W 2026-10-05）：自动化不需要向
                # 用户说明，底下那些按钮自己会说话。
                return texts.SET_FILES_CAT % n_models

            head_lbl = ttk.Label(t5, text=texts.SET_FILES_N,
                                 style="Dim.TLabel", wraplength=760, justify="left",
                                 font=("Microsoft YaHei UI", 9))
            head_lbl.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            r5["i"] = 1

            def _fill_files_head(lbl=head_lbl):
                # 这一行要扫一遍盘（每个 .gguf 都要读文件头判类型）：延到开页之后算（_idle_fill）
                _d2, _chat2, _i2 = scan_models(self.cfg)
                lbl.configure(text=_files_head(len(_chat2)))

            _idle_fill(t5, _fill_files_head)

            frm = ttk.Frame(t5)
            theme.button(frm, text=texts.SET_FILES_L0, width=18,
                       command=self.open_local_models).pack(side="left")
            row(t5, r5, texts.SET_FILES_A2, frm,
                texts.SET_FILES_A4)

            fr = ttk.Frame(t5)
            theme.button(fr, text=texts.MDL_RECALC_MISSING, width=18,
                       command=lambda: self._manual_scan(False)).pack(side="left", padx=(0, 6))
            theme.button(fr, text=texts.MDL_RECALC_BTN, width=18,
                       command=self._open_recalc_dialog).pack(side="left")
            row(t5, r5, texts.SET_FILES_A2_2, fr,
                texts.SET_FILES_A4_2)

            fr2 = ttk.Frame(t5)
            theme.button(fr2, text=texts.MDL_TIDY_TITLE, width=18,
                       command=self._open_tidy_dialog).pack(side="left", padx=(0, 6))
            theme.button(fr2, text=texts.MDL_POINT_BTN, width=18,
                       command=self._open_model_sources).pack(side="left")
            row(t5, r5, texts.SET_FILES_A2_3, fr2,
                texts.SET_FILES_A4_3)

            # 进「模型文件与引擎」页自动补全一次（10 分钟冷却；与「检查更新」共用
            # core.throttle 那一套判据）。手动按钮不受冷却限制（models_ui._manual_scan）。
            enter_hooks.setdefault("files", []).append(self._auto_scan_models)

        # ---- 页面容器 / 区块构建 / 锚点定位（v39：替代原来的 Notebook + _fit）----
        def _make_page(page_id):
            """建一个页面的 frame，并把它的区块标题与空内容框先摆好（便宜，只有标签）。

            标题先建、内容后建，好处是**锚点在构建之前就存在**：左栏点「服务参数」时
            哪怕那一区块还没建，也知道该滚到哪里。
            """
            f = page_frames.get(page_id)
            if f is not None:
                return f
            bg = widgets.default_bg()
            f = tk.Frame(sp.inner, background=bg)
            items = page_items[page_id]
            for idx, it in enumerate(items):
                head = tk.Frame(f, background=bg)
                head.pack(fill="x", padx=2, pady=(6 if idx == 0 else 20, 2))
                tk.Label(head, text=it["title"], background=bg, foreground=theme.c("hdr"),
                         font=("Microsoft YaHei UI", 11, "bold"), anchor="w").pack(side="left")
                widgets.HelpDot(head, it.get("help") or "").pack(side="left", padx=(8, 0))
                body = tk.Frame(f, background=bg)
                body.columnconfigure(2, weight=1)
                body.pack(fill="x", padx=2)
                # 给自检脚本一个稳定的定位方式（按 grid 行号找控件在拆框之后就会错位）
                body._sec_id = it["section"]
                heads[(page_id, it["section"])] = head
                bodies[(page_id, it["section"])] = body
            page_frames[page_id] = f
            return f

        def _build_section(page_id, sec_id):
            """按需构建区块内容（幂等：建过的不重建，控件状态也就不会丢）。"""
            key = (page_id, sec_id)
            body = bodies.get(key)
            if body is None or getattr(body, "_done", False):
                return body
            body._done = True
            build = registry.get(key)
            if build is None:
                ttk.Label(body, text=texts.SET_BUILD_SECTION_N,
                          style="ErrLit.TLabel").grid(row=0, column=0, sticky="w")
                return body
            build(body, {"i": 0})
            return body

        # 区块标题的"闪一下"同一时刻只许有一个：原来是每次点击各挂一个 1200ms 定时器，
        # 连点同页两个区块（生成参数 → 服务参数）就会两处同时黄底，用户反而不知道
        # 自己跳到哪儿了（W 2026-10-02 提的）。所以记下当前闪的标签与它的定时器 id，
        # 新的来了先撤旧定时器、先把旧的那处熄灭。代价是每次点击多一次 after_cancel
        # 与一次 configure（微秒级），不在任何热路径上。
        flashed = {"lbl": None, "timer": None}

        def _unflash(lbl):
            try:
                if lbl.winfo_exists():
                    lbl.configure(background=widgets.default_bg())
            except Exception:
                pass

        def _flash(head):
            """定位过去之后把区块标题闪一下底色：不闪的话用户不知道页面滚到了哪儿。

            **普通模式不闪**（W 2026-10-08）：那一页的区块少、层级浅，页面切换本身就够
            明显，黄底除了晃眼没有指引作用 —— W 报的"切换页面有闪烁"有一半来自它。
            高级模式页内区块多、同页跳转容易"看不出滚到哪"，黄底仍保留。
            """
            if simple:                     # 普通模式：只要"不闪"，不留残色
                if flashed["timer"] is not None:
                    try:
                        win.after_cancel(flashed["timer"])
                    except Exception:
                        pass
                    flashed["timer"] = None
                if flashed["lbl"] is not None:
                    _unflash(flashed["lbl"])
                    flashed["lbl"] = None
                return
            if flashed["timer"] is not None:
                try:
                    win.after_cancel(flashed["timer"])
                except Exception:
                    pass
                flashed["timer"] = None
            if flashed["lbl"] is not None:
                _unflash(flashed["lbl"])
                flashed["lbl"] = None
            if head is None:
                return
            lbl = [w for w in head.winfo_children() if isinstance(w, tk.Label)]
            if not lbl:
                return
            try:
                lbl[0].configure(background=theme.c("flash"))
            except Exception:
                return
            flashed["lbl"] = lbl[0]

            def _done():
                flashed["timer"] = None
                if flashed["lbl"] is not None:
                    _unflash(flashed["lbl"])
                    flashed["lbl"] = None

            try:
                flashed["timer"] = win.after(1200, _done)
            except Exception:
                _done()

        def _nav_select(item):
            page_id, sec_id = item["page"], item["section"]
            state["leaf"] = item.get("key")     # 模式切换后按它落回对应页
            # 连点左栏**当前这一项**时不再重闪（W 2026-10-03 报的"管理本地模型"按钮闪烁）：
            # 那一项下面紧跟着的就是这个区块的标题，黄底每 1200ms 重画一轮，
            # 看着就像按钮在闪。页面与目标都没动，只有滚动照做。
            same = (state["page"] == page_id and state["section"] == sec_id)
            entered = state["page"] != page_id        # 真的从别的页切过来了（不是连点）
            f = _make_page(page_id)
            if entered:
                sp.set_page(f)
                state["page"] = page_id
            # 同一页的区块**一次建齐**（W：懒加载不能只建点中的那个小标题）：
            # 只建一段的话，往下滚就是半页空白，底部「保存」也只跑得到已建区块的钩子。
            # 省下来的仍然是"别的页"的构建开销，那才是首屏卡顿的大头。
            for it in page_items[page_id]:
                _build_section(page_id, it["section"])
            state["section"] = sec_id
            # 这里不再 win.update_idletasks()：goto() 里的 _refresh() 已经做过一次全树
            # 布局（Tk 的 update_idletasks 是全应用级的，不是只管那一个控件），
            # 再来一次纯属白付 —— 实测一次全树重算 ~17ms。
            head = heads.get((page_id, sec_id))
            sp.goto(head)
            if not same:
                _flash(head)
            # 进页要做的事放最后：回调是区块里登记的，上面不先建区块它就还不存在。
            if entered:
                for fn in enter_hooks.get(page_id) or []:
                    try:
                        fn()
                    except Exception:
                        pass

        def _global_save():
            """底部「保存」= 通用参数 + **所有已建区块各自的保存逻辑**，都成功才关窗。

            钩子跑全部已建（_done）区块，不只当前页（R-116）：`_apply_settings` 的
            键写回本来就是跨页的（v 是整窗共享），页钩子只跑当前页会造成"外观页挑的
            主题色 / 服务商页改的名字 / 通用页换的优化模型"切页保存时被**静默丢弃**——
            主题色最坑：选色框一关预览已经把界面染成新色，用户认定保存好了，重启回退。
            钩子幂等、已建区块数量有限，全跑的成本可控。任一钩子不通过就不关窗。

            关窗前若 llama-server **正在运行**，问一句要不要顺手重启（坑 281）：端口 /
            模型 / GPU 层数 / context 这些参数不重启不生效，旧版另设「保存并重启服务」
            按钮，用户常只点保存、改了不自知。没在跑 / 正在启停（_svc_busy）就不问。
            """
            ok, why = self._apply_settings(v)
            if not ok:
                messagebox.showwarning(texts.SET_GLOBAL_SAVE_A0, why)
                return                          # 一个键都没写，窗也不关：改好再来
            for _items in page_items.values():
                for it in _items:
                    body = bodies.get((it["page"], it["section"]))
                    if body is None or not getattr(body, "_done", False):
                        continue
                    hook = save_hooks.get(body)
                    if hook is None:
                        continue
                    try:
                        ok, msg = hook() or (True, "")
                    except Exception as e:
                        ok, msg = False, texts.SET_GLOBAL_SAVE_CAT % e
                    if not ok:
                        messagebox.showwarning(
                            texts.SET_GLOBAL_SAVE_A0, msg or texts.SET_GLOBAL_SAVE_N)
                        return
            # 保存往往就是"第一次配好"的时刻（指了引擎 / 指了模型目录 / 勾了云端模型 /
            # 填了密钥）⇒ 立刻问一次"第一个能用的模型有了没有"，别等下一轮轮询（坑 150）
            try:
                self._maybe_adopt_first_model(force=True)
            except Exception:
                pass
            # 运行态判据与引擎安装守卫同源（本进程旗标优先，再按端口 HTTP 复核，坑 189）
            running = (not self._svc_busy
                       and (self._server_alive_flag
                            or (server_process_alive() and server_alive(self.cfg))))
            do_restart = False
            if running:
                do_restart = bool(messagebox.askyesno(
                    texts.SET_GLOBAL_SAVE_A0_2,
                    texts.SET_GLOBAL_SAVE_A1))
            win.destroy()
            if do_restart:
                self.restart_server()

        # 「用户模式」切换入口（2026-10-07 W 定，顶替原「显示全部参数」的位置）：
        # 点一下在 普通 / 高级 之间互切。切换 = 写配置 + **整窗重建** —— 配置加载、
        # 按页懒建、进页钩子（模型列表刷新、进页自动查更新…）全部重新走一遍，
        # 不在旧窗口上"就地换皮"（那才会出空白与状态错乱）。没保存的编辑会丢，
        # 与「关闭」按钮同一语义。折叠区的展开状态仍存 App._settings_ui（会话内记住）。
        def _switch_mode():
            new_simple = not simple
            self.cfg["user_mode"] = "simple" if new_simple else "advanced"
            try:
                save_config(self.cfg)
            except Exception:
                pass
            cur = state.get("leaf") or ""
            keys = {it.get("key") for it in
                    _nav_leaves(_nav_items(self._dev_mode, new_simple))}
            try:
                # 原地重建（W 2026-10-09）：位置 / 尺寸都记下，open_settings 恢复
                self._settings_geom = win.winfo_geometry()
            except Exception:
                self._settings_geom = ""
            win.destroy()
            self._settings_win = None
            self._settings_sp = None
            self.open_settings(jump=cur if cur in keys else "")

        theme.button(bar, text=texts.SET_OPEN_SETTINGS_N if simple else texts.SET_OPEN_SETTINGS_N_2,
                   command=_switch_mode).pack(side="left", padx=(2, 0))
        theme.button(bar, text=texts.SET_OPEN_SETTINGS_N_3, command=win.destroy).pack(side="right", padx=4)
        # 旧版第二个按钮「保存并重启服务」已删（坑 281）：保存时若服务在跑会自己问一句
        theme.button(bar, text=texts.SET_OPEN_SETTINGS_N_4, command=_global_save).pack(side="right")

        # 首屏默认落在第一个叶子（本地模型 → 文本模型 → 生成参数）；
        # jump 由输出栏那两个按钮传进来，指到哪个叶子就滚到哪一段
        nav.select(jump if (jump and nav.find(jump)) else leaves[0]["key"])

    def _apply_settings(self, v):
        """把这一页已建区块的输入写回 cfg → `(ok, 给用户看的一句话)`。

        数值键先整份预检（`_num_error`），不合格就**一个键都不写**并回一句人话 ——
        原来把坏值静默换成旧值，用户看到"保存了"其实没生效（这一轮改成开口）。
        """
        bad = _num_error(v)
        if bad:
            return False, bad
        c = self.cfg
        # 云端模型没有 ngl / context 概念：跳过按模型记忆，否则会把 "pid::model"
        # 这种复合 id 当文件名写进 model_ngl / model_ctx，污染配置
        local = not providers.is_cloud(c)
        for k in INT_KEYS:
            if k in ("ctx", "ngl"):
                # 服务参数页这两个框装的是"当前模型"的按模型值（预填自 ctx_for /
                # current_ngl）：只能写按模型记录（下面单独处理），**绝不写全局兜底键**
                # ——否则"什么都不改点保存"就把按模型值抄进 cfg["ctx"]/cfg["ngl"]，
                # 之后任何没有记录的新模型第一次启动就按被抬高的值拉起（排查报告
                # R-114；坑 130 当年只堵了"按模型记录被覆盖"那半边）。
                continue
            if k in v:
                try:
                    c[k] = int(str(v[k].get()).strip())
                except Exception:
                    pass
        # context / ngl 按模型记忆（主页面启动用）：**只有这一页真的建了、用户真的填过**
        # 才写记录 —— 判据必须看 v（这一页的输入），不能看 c（配置里有没有这个键，恒为真）。
        # 原来写成 `if local and "ctx" in c:`，于是随便在哪一页点「保存」都会把当前模型的
        # 层数 / context 记录改写成全局兜底值，静默抹掉自动推算的结果（坑 130）。
        # 记录键用**建页时的模型快照**（_svc_snap / _api_snap）：设置窗非模态、页只建一次，
        # 开着窗在主页切模型再保存时，框里装的还是旧模型的值，不能写到新模型头上（R-114）。
        # 数值已由 _num_error 预检（ctx/ngl 在 INT_KEYS、agent_ctx 有等价校验），
        # 这里不再 try 吞 —— 写回失败必须让 _global_save 报出来，不能"保存了但没生效"。
        if local and "ctx" in v:
            snap = str(v.get("_svc_snap") or "") or os.path.basename(c.get("model", ""))
            if snap:      # 模型名为空时不写 "" 键（排查报告 R-43）
                c.setdefault("model_ctx", {})[snap] = int(str(v["ctx"].get()).strip())
        # agent 场景 context 按模型记忆
        if local and "agent_ctx" in v:
            snap = str(v.get("_api_snap") or "") or os.path.basename(c.get("model", ""))
            if snap:
                c.setdefault("model_ctx_api", {})[snap] = int(
                    str(v["agent_ctx"].get()).strip())
        for k in FLOAT_KEYS:
            if k in v:
                try:
                    c[k] = float(str(v[k].get()).strip())
                except Exception:
                    pass
        for k in STR_KEYS:
            if k in v:
                c[k] = str(v[k].get()).strip()
        # 分辨率类（坑 183）：x/X/*/＊/×/，/全角数字都认，存进配置前统一归一成半角 x
        # ——「填入后改为 x 号显示」在保存这一刻也成立（失焦那一刻已归一过一次）。
        # 不合格的写法照原样存（发送侧回落默认档，框后已有红字警示），保存不拦。
        for k in ("img_size", "vid_size", "cloud_img_size", "cloud_video_resolution"):
            if k in v:
                _p = media.parse_size(c.get(k, ""))
                if _p:
                    c[k] = "%dx%d" % _p
        # 「model」的写回特例（坑 146）已随「服务参数」那一栏一起删除（2026-10-16）：
        # 那个入口曾经是"手填路径"时代的遗留，写回语义（留空≠切回本地、非空=切回本地）
        # 整段只服务于它；现在选模型只有主界面菜单与「模型来源…」两条正路，
        # 不经设置页保存。
        # 模型族下拉显示的是中文标签，配置里要存回 sdprofile 的家族代码；
        # 选"自动判断"存空串——别让界面文案跑到配置里去。
        for k, kind in (("img_family", "image"), ("vid_family", "video")):
            if k in v:
                code = {t: cf for cf, t in sdprofile.family_choices(kind)}.get(
                    str(v[k].get()), sdprofile.AUTO)
                c[k] = "" if code == sdprofile.AUTO else str(code)
        if "system_prompt" in v:
            c["system_prompt"] = v["system_prompt"].get("1.0", "end").rstrip("\n")
        if "show_reasoning" in v:
            c["show_reasoning"] = bool(v["show_reasoning"].get())
        # 云端「展示思考过程」独立存储（2026-10-07 W 定）：与本地那个键互不影响
        if "cloud_show_reasoning" in v:
            c["cloud_show_reasoning"] = bool(v["cloud_show_reasoning"].get())
        # 代理开关：**只有这台机器真能转发时才回写**。没有本地文本模型时那个复选框是锁着的，
        # 它的值是"偏好 and usable"= False —— 照原样回写就会把用户存过的偏好静默改成关，
        # 等他后来装好模型也不会自己变回来（§5.6 的口径：锁住的那项默认关，但不吃配置）
        if "proxy_enabled" in v and self._proxy_usable:
            c["proxy_enabled"] = bool(v["proxy_enabled"].get())
        # 「自启动」与「启用」同一把锁（坑 136 同款）：锁着的项不回写。
        # 普通用户模式整页不建 ⇒ v 里没有这两个键 ⇒ 切模式绝不会把它们关掉
        # （W 点名：开启自启动后切回普通用户模式不得自动关闭）
        if "proxy_autostart" in v and self._proxy_usable:
            c["proxy_autostart"] = bool(v["proxy_autostart"].get())
        if "show_usage" in v:
            c["show_usage"] = bool(v["show_usage"].get())
        if "cloud_file_model_decides" in v:
            c["cloud_file_model_decides"] = bool(v["cloud_file_model_decides"].get())
        # 底图重绘的自动分档开关（三档的数值走 FLOAT_KEYS 那条通用回写）
        if "img_strength_auto" in v:
            c["img_strength_auto"] = bool(v["img_strength_auto"].get())
        # ngl 按模型记忆：设置页改的是"当前模型"的层数（云端模型没有层数概念）。
        # 判据同 ctx：看 v，不看 c（坑 130）；键用建页快照、不写全局兜底（R-114）。
        if local and "ngl" in v:
            snap = str(v.get("_svc_snap") or "") or os.path.basename(c.get("model", ""))
            if snap:      # 同 ctx：空模型名不落 "" 键（R-43）
                c.setdefault("model_ngl", {})[snap] = int(str(v["ngl"].get()).strip())
        c["cfg_version"] = CFG_VERSION
        save_config(c)
        self._update_model_label()
        return True, ""
