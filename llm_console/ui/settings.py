# -*- coding: utf-8 -*-
"""llm_console.ui.settings — 界面 Mixin：设置窗口（v39：左侧可折叠导航 + 右侧滚动区块，
按页懒加载、同页区块一起建；字段说明走 widgets.HelpDot 悬停；底部「保存」统一跑当前页各区块的钩子）"""


import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

from ..core import (capability, codesign, cloudjobs, engine_install, hardware, providers,
                    sdprofile, secrets, textfile, updater)
from ..core.config import (APP_DIR, APP_VERSION, CFG_VERSION, DEFAULT_CONFIG,
                           FLOAT_KEYS, INT_KEYS, STR_KEYS, cloud_media_dir,
                           gen_api_key, save_config)
from ..core.models import has_local_chat, scan_models, scan_video_models
from ..core.params import ctx_for, current_ngl
from ..core.server import _query_serving_model, server_process_alive
from ..connection import cloud
from . import widgets

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
             "title": "服务参数（点「保存并重启服务」生效）",
             "help": "这些是 llama-server 的启动参数，改完必须重启服务才生效。\n"
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
                 "「手动定向模型」把文件夹或单个 .gguf 加进来。\n"
                 "整理是**先预览、后执行，只移动不删除**；判不出归属的文件保持原位。"},
        # 「模型文件管理」与「引擎管理」本来就同一页，左栏合成一项（W 2026-10-02：两项
        # 各自一行是重复劳动）。`nav_hide` 让它不在左栏成行，但区块照旧建、jump="eng"
        # 照旧滚得到（输出栏「去配置引擎」用的就是它），高亮记在「模型文件与引擎」那行。
        {"key": "eng", "label": "引擎管理", "page": "files", "section": "eng",
         "nav_hide": "files",
         "title": "引擎管理",
         "help": "这一屏解决「还没装引擎」：说清现在缺哪一个、可以选哪几档、"
                 "点一下装到程序目录的 engines 里。\n"
                 "引擎已在自己机器上、只是换了位置：用「自动定向」扫软件所在文件夹，"
                 "或用「手动定向」指到那个文件夹 —— 两个引擎与模型文件同处一个目录也认得出。\n"
                 "CUDA 档要连运行库一起下（几百 MB），所以下之前会先说清多大；"
                 "装完回「服务参数」把路径指过去。\n"
                 "「检查更新」联网查可用版本，查到新版本它自己变成「更新引擎」，"
                 "装的过程中可点「取消安装」；网络不通下不动时还有「复制下载链接」"
                 "（给的是选中档位的安装包直链）。"},
    ]},
    {"key": "g_cloud", "label": "云端模型", "children": [
        {"key": "c_prov", "label": "服务商与密钥", "page": "cloud", "section": "prov",
         "title": "服务商、密钥与模型清单",
         "help": "内置服务商只内置名称与 base_url；添加服务商**不会**把它名下所有模型塞进"
                 "主页面菜单。流程是：填信息 → 填密钥 → 测试连接 → 通过后在独立的「选择模型」"
                 "窗口里勾选要用的模型。清单拉过一次就缓存，只有点「刷新清单」才重新请求。\n"
                 "密钥单独存 secrets.json，只留在你这台机器上，不会跟配置一起被复制走。"},
        {"key": "c_text", "label": "文本模型", "page": "cloud", "section": "ctext",
         "title": "云端文本模型",
         "help": "云端文本走通用 OpenAI 兼容协议。这里的设置只影响文本对话"
                 "（生图 / 生视频在下一区块）。\n"
                 "附件会留在对话历史里**每一轮都重发**，所以预算有上限，不是能塞多塞。"},
        {"key": "c_media", "label": "生图 / 生视频", "page": "cloud", "section": "cmedia",
         "title": "云端生图 / 生视频",
         "help": "云端生图与生视频走服务商的**原生接口**，不会启动本地 sd.cpp："
                 "生图是同步请求（可以挂参考图），生视频是异步任务"
                 "（提交 → 轮询 → 下载），首帧仍要用本地链路。\n"
                 "左列是生图、右列是生视频，各自的存放目录在本列底部；"
                 "轮询间隔与等待上限两条链路共用，压在下面那条横栏里。\n"
                 "云端产物地址只活 24 小时，所以拿到就立刻下载到本地，不在云上留原图；"
                 "没来得及下载的会记进任务台账，重启后对话开头给「取回」按钮。\n"
                 "费用单价按模型填，点「成本预估算」开窗口。"},
    ]},
    {"key": "api", "label": "本地模型 API", "page": "api", "section": "api",
     "title": "本地模型 API（给 agent 或其他软件调用）",
     "help": "把**当前选中的本地模型**变成一个 OpenAI 兼容的本机地址，别的软件\n"
             "（agent、脚本、外部工具）填这个地址就能直接用它。\n"
             "只转本地模型：云端对话在应用内直连服务商，生图 / 生视频也不经这里。\n"
             "要有本地文本模型才开得起 —— 没有时这一项默认关闭。"},
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


def _nav_items(dev_mode=False):
    """左栏条目：普通用户 = NAV_SPEC；开发者模式 = 末尾多一项「开发者选项」。

    尾部追加正好落在"关于与诊断"那组后面，也就是 W 要的"关于导航栏下方"。
    """
    return list(NAV_SPEC) + ([DEV_NAV_ITEM] if dev_mode else [])


def _upd_help_text():
    """关于页「检查更新」那个 "?" 的说明。

    为什么是个函数而不是写死的字符串：末句"后台定期查"**跟着令牌变**（只有开发者模式填了
    令牌才跑），令牌换了不重算就与事实不符 —— 开发者选项那边一改令牌就回来重算它。

    W 2026-10-05：把"多久不重复查 / 限额多少"这类**内部节流**的说法全删了 —— 用户不需要
    知道我们多久问一次 GitHub、额度怎么算（那是省额度的事）。只留"它是什么、什么时候会自己
    查、查到会怎样"。后台定期查这条要留着：弹窗会在用户没动过手时冒出来，得让他知道正常。
    """
    tail = "进入本页时会自动按「正式版」查一次"
    if secrets.get_github_token():
        tail += "；开发者模式下还会在后台定期查"
    return ("检查更新：按右边选的通道向 GitHub 查最新 Release。\n"
            "正式版 = 只看正式发布的 Release；测试版 = 把预发布一起算，给最新的那个。\n"
            "查到新版本会弹「发现新版本」窗口，点「立即更新」就下载并替换本程序，"
            "替换前会再确认一次（需要关闭程序）。\n"
            "自动检查弹的窗被关掉后，同一个版本不再自动弹第二次；"
            "手动点「检查更新」永远弹。\n"
            "打开下载页 = 查到新版本时跳到它的下载页。\n" + tail + "。")


def _local_model_shown(cfg):
    """"服务参数 → model"只回显本地路径。该键是本地路径与云端复合 id `"pid::model"`
    共用的（坑 146），原样回显会把云端内部 id 摆到本地参数里。"""
    return "" if providers.is_cloud(cfg) else str(cfg.get("model", "") or "")


def _kind_of_local_model(cfg, path):
    """按扫描结果定 chat / image；扫不到返回 None，调用方保持原值（猜错类别比留错更糟）。"""
    try:
        probe = dict(cfg)
        probe["model"] = str(path or "")
        _d, chat, image = scan_models(probe)
    except Exception:
        return None
    p = os.path.normpath(str(path or ""))
    for lst, kind in ((image, "image"), (chat, "chat")):
        if any(os.path.normpath(x) == p for x in lst):
            return kind
    return None


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
        msg = "还没设置%s。" % what
        if setting:
            msg += "\n  请在 设置 → %s 里填上目录。" % setting
        messagebox.showwarning("输出目录", msg)
        return False
    try:
        os.makedirs(p, exist_ok=True)
        os.startfile(p)
        return True
    except Exception as e:
        messagebox.showwarning("输出目录", "打不开 %s：\n  %s" % (p, e))
        return False


NEW_PROVIDER_LABEL = "＋ 新建服务商…"      # 下拉里"还没建起来"那一项的标签

# 数值键的**可接受区间**：只列"填错了会静默出事"的那些 —— 端口填 99999 = 服务起不来、
# ctx 填十亿 = 启动即 OOM、步数 0 = 引擎报错。不在这张表里的键只查"是不是数字"。
# 注意 0 的语义：ngl / threads / top_k / vram_gb / ram_gb 的 0 都是"自动 / 全 CPU"，
# 必须留在区间内（别把 0 当非法值拦掉）。
_NUM_RANGE = {
    "port": (1, 65535), "proxy_port": (1, 65535),
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
            return "「%s」要填整数（现在是 %r）。" % (k, raw[:20])
        lo, hi = _NUM_RANGE.get(k, (None, None))
        if lo is not None and not (lo <= n <= hi):
            return "「%s」要填 %d ~ %d 之间的整数（现在是 %d）。" % (k, lo, hi, n)
    for k in FLOAT_KEYS:
        if k not in v:
            continue
        raw = str(v[k].get()).strip()
        try:
            f = float(raw)
        except Exception:
            return "「%s」要填数字（现在是 %r）。" % (k, raw[:20])
        lo, hi = _FLOAT_RANGE.get(k, (None, None))
        if lo is not None and not (lo <= f <= hi):
            return "「%s」要填 %.4g ~ %.4g 之间的数字（现在是 %.4g）。" % (k, lo, hi, f)
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
        return ("服务类型: OpenAI 兼容 (Chat Completions)\n"
                "Base URL: %s\n"
                "API Key:  %s\n"
                "模型名:   %s\n"
                "—— 客户端填法 ——\n"
                "在 agent 应用中选择 OpenAI Compatible / 自定义 OpenAI，逐项粘贴以上四项；\n"
                "模型名支持模糊匹配（略写、带不带 .gguf 都可命中）。"
                % (self._api_base_url(), self.cfg.get("api_key", ""),
                   self._api_model_name() or "（暂无可用聊天模型）"))

    def _gen_api_key(self):
        k = gen_api_key()
        self.cfg["api_key"] = k
        save_config(self.cfg)
        messagebox.showinfo("API Key",
                            "已生成新的 API Key：\n\n%s\n\n"
                            "注意：Key 是服务启动参数，需「重启服务」后生效。" % k)
        return k

    def _restart_proxy(self):
        self.proxy.stop()
        if not has_local_chat(self.cfg):
            self._proxy_usable = False
            self._append("\n[API] 这台机器上还没有可转发的本地文本模型，代理不起 —— "
                         "先在 设置 → 本地模型 备好引擎与模型。\n", "meta")
            return
        self._proxy_usable = True
        if self.cfg.get("proxy_enabled", True):
            self.proxy.start()
        else:
            self._append("\n[API] 代理已停用（设置中可重新启用并重启代理）。\n", "meta")

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
        win.title("设置")
        # 实测（DPI-aware，scaling≈2.0）滚动内容最宽一行需要 789px，左栏 232 + 边距
        # → 最小宽取 1080：横向不能滚，缩一点就是"右边那半截永远看不见"
        # 默认宽 1160（W 2026-10-03 要求"适当调大"）：1080 下实测内容最右到 1070、
        # 只剩 10px 余量；「模型族」那类"下拉 + 右侧状态回显"同排的版式最怕这个余量
        # （见 13坑 76）。**只加宽、不加高**：1366x768 扣任务栏约 728 可用，
        # 740 的高度已经贴边，加高会把按钮那排挤出屏幕。
        win.geometry("1160x740")
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
        state = {"page": None, "section": None}
        leaves = _nav_leaves(_nav_items(self._dev_mode))
        page_order, page_items = [], {}
        for it in leaves:
            if it["page"] not in page_order:
                page_order.append(it["page"])
                page_items[it["page"]] = []
            page_items[it["page"]].append(it)

        nav = widgets.SideNav(main, _nav_items(self._dev_mode), width=232,
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
            items = _nav_items(self._dev_mode)
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
            sess = {"show_all": False, "fold": {}}
            self._settings_ui = sess
        sess.setdefault("show_all", False)
        sess.setdefault("fold", {})

        def _changed(key):
            """这一项是不是改过出厂默认（改过的项绝不被藏）。"""
            return str(self.cfg.get(key, "")) != str(DEFAULT_CONFIG.get(key, ""))

        def _render_fold(name):
            """按当前状态摆/收这一组，并刷新标题（▸/▾ + 项数 + 已改数）。"""
            open_ = bool(sess.get("show_all")) or bool((sess.get("fold") or {}).get(name))
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
            tail = ("（%d · 已改 %d）" % (len(keys), cut)) if cut else ("（%d）" % len(keys))
            try:
                head.configure(text="%s 高级参数%s" % ("▾" if open_ else "▸", tail))
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
            lab = ttk.Label(parent, textvariable=var, foreground="#5a6a7a",
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

        def _row(parent, rows, label, widget, desc, hint="", lw=14, fold=None):
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
            """
            i = rows["i"]
            rows["i"] += 1
            bg = widgets.default_bg()
            lab = tk.Frame(parent, background=bg)
            lab.grid(row=i, column=0, sticky="w", padx=(0, 8), pady=5)
            ttk.Label(lab, text=label, width=lw, anchor="w").pack(side="left")
            widgets.HelpDot(lab, desc).pack(side="left", padx=(2, 0))
            widget.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=5)
            cell = None
            if hint:
                cell = tk.Frame(parent, background=bg)
                cell.grid(row=i, column=2, sticky="w", pady=5)
                ttk.Label(cell, text=hint, foreground="#5a5a5a",
                          font=("Microsoft YaHei UI", 9)).pack(side="left")
            if fold:
                grp = folds.setdefault(fold, [])
                for w in (lab, widget, cell):
                    if w is not None:
                        grp.append(w)
                # 登记后立刻按当前状态摆 / 收 —— 不能无条件 grid_remove：
                # 「显示全部参数」勾着时重开设置窗，这些行本来就该是展开的
                _render_fold(fold)

        def row(parent, rows, label, widget, desc, hint="", lw=14, fold=None):
            return _row(parent, rows, label, widget, desc, hint, lw, fold)

        def ent(parent, rows, key, label, desc, width=8, var=None, trace=None, hint="",
                lw=14, fold=None):
            """一行"标签 + 输入框"，说明在标签旁的 "?" 里。

            key 非空时变量登记进 v（由 _apply_settings 统一写回 cfg）；
            传 var 则用外部变量（云端服务商那几项是结构化数据，自己管保存，
            不走 _apply_settings）。trace 用于即时联动（如回显请求地址）。
            """
            if var is None:
                var = v.setdefault(key, tk.StringVar(value=str(self.cfg.get(key, ""))))
            e = ttk.Entry(parent, textvariable=var, width=width)
            if trace is not None:
                var.trace_add("write", lambda *a: trace())
            _row(parent, rows, label, e, desc, hint, lw, fold)

        # ---- 区块 1：本地文本模型 / 生成参数 ----
        @section("local_text", "gen")
        def _t1(t1, r1):
            ent(t1, r1, "temperature", "temperature",
                "采样温度（0~2）：越高输出越发散有创意，越低越稳定保守；接近 0 时几乎固定。")
            ent(t1, r1, "top_p", "top_p",
                "核采样（0~1）：只在累计概率达到 p 的候选词里抽样。")
            ent(t1, r1, "max_tokens", "max_tokens",
                "单次回复上限（token）。注意：思考过程 + 正式回答共享该额度，"
                "Qwen3 思考较长，建议 ≥4096；到上限会被截断并提示。",
                hint="思考与回答共用")
            i = r1["i"]
            r1["i"] += 1
            head = tk.Frame(t1, background=widgets.default_bg())
            head.grid(row=i, column=0, sticky="nw", padx=(0, 8), pady=5)
            ttk.Label(head, text="system_prompt", width=14, anchor="nw").pack(side="left")
            widgets.HelpDot(head, "系统提示词：给模型的人设与规则，自动放在每轮对话最前面。").pack(side="left", padx=(2, 0))
            # width 必须显式给：tk.Text 默认 80 字符，在高 DPI 下要 1080px，
            # 会把这一行顶出滚动可视区（横向不能滚，等于看不见）
            v["system_prompt"] = tk.Text(t1, height=3, width=44,
                                         font=("Microsoft YaHei UI", 9))
            v["system_prompt"].grid(row=i, column=1, columnspan=2, sticky="nsew", pady=5)
            v["system_prompt"].insert("1.0", str(self.cfg.get("system_prompt", "")))

            i = r1["i"]
            r1["i"] += 1
            v["show_reasoning"] = tk.BooleanVar(value=bool(self.cfg.get("show_reasoning", True)))
            ttk.Checkbutton(t1, text="在对话中显示模型的思考过程（reasoning，灰色斜体）",
                            variable=v["show_reasoning"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=8)

            # 「高级参数」：采样细节（top_k / 重复惩罚）与随机种子 —— 平时不动它们
            fold_gen = "local_text:gen"
            _fold_head(t1, r1, fold_gen, ("top_k", "repeat_penalty", "seed"))
            ent(t1, r1, "top_k", "top_k",
                "每一步只在概率最高的 k 个词中选取，常用 40。", fold=fold_gen)
            ent(t1, r1, "repeat_penalty", "repeat_penalty",
                "重复惩罚（通常 1.0~1.3）：大于 1 抑制复读式重复，1.0 表示关闭。",
                fold=fold_gen)
            ent(t1, r1, "seed", "seed",
                "随机种子：-1 表示随机；填固定数字可复现同一次输出。", fold=fold_gen)

        # ---- 区块 2：本地文本模型 / 服务参数 ----
        @section("local_text", "svc")
        def _t2(t2, r2):

            # 「model」只回显本地路径：cfg["model"] 是本地路径与云端复合 id 共用的键（坑 146），
            # 原样回显会让本地参数里出现云端模型；写回语义见 _apply_settings。
            v["model"] = tk.StringVar(value=_local_model_shown(self.cfg))
            ent(t2, r2, "model", "model",
                "当前**本地**模型 GGUF 完整路径（也可直接点主页模型名切换）。"
                "选中云端模型时这一栏留空；填了并保存即切回本地。", width=30)
            ent(t2, r2, "models_dir", "models_dir",
                "模型文件夹：主页模型下拉列表扫描此目录下所有 .gguf 文件。", width=30)
            # ngl：显示/修改的是"当前模型"的值（按模型分别记忆）
            v["ngl"] = tk.StringVar(value=str(current_ngl(self.cfg)))
            e_ngl = ttk.Entry(t2, textvariable=v["ngl"], width=8)
            row(t2, r2, "n-gpu-layers", e_ngl,
                "放进显存的层数（当前模型）。新模型会按显存与模型大小自动计算并按模型分别记忆；"
                "此处修改仅对当前模型生效。0 = 全部放 CPU。")
            # ctx：与 ngl 同一口径 —— 显示/修改的是"当前模型"的值（按模型记忆）。
            # 原来这里预填的是全局兜底 cfg["ctx"]，而保存又无条件写进当前模型的记录，
            # 于是"打开任意一页点保存"就会把该模型自动算出的 context 覆盖掉（坑 130）
            v["ctx"] = tk.StringVar(value=str(ctx_for(self.cfg)))
            e_ctx = ttk.Entry(t2, textvariable=v["ctx"], width=8)
            _row(t2, r2, "context (-c)", e_ctx,
                 "上下文长度（token）：容纳 系统提示 + 全部对话 + 工具定义 + 回答。"
                 "按模型分别记忆，切换模型时自动带上各自的值；新模型会按显存与内存预算"
                 "自动推算一个安全值。KV 成本差别很大：35B-A3B 约 2.5GB / 64K，"
                 "而 27B（大 head_dim）约 16GB / 64K —— 后者请适当降低，"
                 "否则占用大量内存/显存（启动时界面会显示 KV 预估）。",
                 hint="按模型记忆")
            ent(t2, r2, "port", "port",
                "API 端口，默认 8080。")
            # 这里**不放 api_key**：那是「本地模型 API」那一页的事（生成 / 复制 / 撤销都在一处），
            # 摆在服务参数里会让人以为改完要重启服务，也会和那页的只读回显对不上
            v["reasoning_mode"] = tk.StringVar(value=str(self.cfg.get("reasoning_mode", "default")))
            cb = ttk.Combobox(t2, textvariable=v["reasoning_mode"],
                              values=["default", "off", "budget"], width=8, state="readonly")
            row(t2, r2, "reasoning", cb,
                "思考模式：default 跟随模型模板；off 关闭思考（更快、不吃 max_tokens 额度）；"
                "budget 限制思考 token 数。")
            ent(t2, r2, "exe", "server 路径",
                "llama-server.exe 完整路径。", width=30)

            # 「高级参数」：硬件属性（自动探测预填，平时不用看）、线程数、
            # 思考预算（只在 reasoning=budget 时用）、附加命令行 —— 都是"配一次就不管"的，
            # 收起来；常用那几个（模型 / 层数 / 上下文 / 端口 / 思考模式 / server 路径）留在外面
            fold_svc = "local_text:svc"
            _fold_head(t2, r2, fold_svc,
                       ("gpu_name", "vram_gb", "ram_gb", "threads",
                        "reasoning_budget", "extra_args"))
            ent(t2, r2, "gpu_name", "GPU 型号",
                "显卡型号（首次启动自动探测预填，可手动修改）。", width=32, fold=fold_svc)
            ent(t2, r2, "vram_gb", "显存 (GB)",
                "显存容量：新模型 GPU 层数自动计算直接使用此值，不再临时询问系统；"
                "探测失败或多卡时可手动填写。", width=8, fold=fold_svc)
            ent(t2, r2, "ram_gb", "内存 (GB)",
                "系统内存总量（首次启动自动探测预填，可修改；目前预留展示）。", width=8,
                fold=fold_svc)
            ent(t2, r2, "threads", "threads",
                "CPU 线程数，0 = 自动。一般留 0。", fold=fold_svc)
            ent(t2, r2, "reasoning_budget", "budget tokens",
                "思考预算：reasoning=budget 时，思考最多用多少 token。", fold=fold_svc)
            ent(t2, r2, "extra_args", "extra_args",
                "附加命令行参数（高级）：空格分隔，原样追加给 llama-server。", width=24,
                fold=fold_svc)

        # ---- 区块 3：本地图像与视频 / 生图 ----
        #
        # 分层：状态行（依赖就绪与否）→ 常改的 7 项 → 「高级参数」折叠区（配套文件 + 后端 /
        # 种子 / 附加参数，共 10 项，默认收起）。折叠只挪格子不毁控件 —— 值不丢、保存钩子
        # 照跑（见 _row 的 fold 说明）；底部「显示全部参数」一勾全展开。
        @section("local_media", "img")
        def _t3(t3, r3):
            def _fill_state(var):
                """依赖就绪回显：认出几个生图模型（延后跑，要扫盘）。"""
                try:
                    _dd, _chat, imgs = scan_models(self.cfg)
                    n = len(imgs)
                except Exception:
                    n = 0
                if n:
                    var.set("生图模型：认出 %d 个" % n)
                else:
                    var.set("还没认到生图模型：先填下面两项，或去「模型文件与引擎」扫描")

            img_state = tk.StringVar(value="")
            _state_row(t3, r3, img_state, _fill_state, img_state)
            ent(t3, r3, "sd_dir", "引擎目录",
                "sd.cpp 引擎所在目录（内含 sd-cli.exe / sd-server.exe）。", width=30)
            ent(t3, r3, "image_model_dir", "生图模型文件夹",
                "扩散模型与它的配套件（VAE、文本编码器、视觉投影器 mmproj）都放这里；"
                "留空 = 模型目录下的「生图」子文件夹。", width=30)
            ent(t3, r3, "img_model_file", "默认生图模型",
                "默认选中的扩散模型文件名（.gguf / .safetensors / .ckpt 都行）。", width=30)
            # 模型族：识别结果只决定"拼哪些参数、要哪些配套件"，参数值仍来自下面这些设置
            _fam_opts = sdprofile.family_choices("image")
            _code2label = {c: t for c, t in _fam_opts}
            v["img_family"] = tk.StringVar(
                value=_code2label.get(str(self.cfg.get("img_family", "") or "").strip(),
                                      _fam_opts[0][1]))
            fam_cb = ttk.Combobox(t3, textvariable=v["img_family"], state="readonly",
                                  width=24, values=[t for _c, t in _fam_opts])
            img_note = tk.StringVar(value="")
            _fam_row = r3["i"]
            row(t3, r3, "模型族", fam_cb,
                "按权重文件里的张量名与文件名自动认这一族；认错了在这里手动指定。")
            # 「识别为…」与下拉同排、贴它右侧（W 2026-10-03）：状态回显归控件那一侧，
            # 不另起一行占版心
            ttk.Label(t3, textvariable=img_note, foreground="#5a6a7a",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=_fam_row, column=2, sticky="w", padx=(0, 10), pady=5)

            def refresh_img_note(*_a):
                """回显识别结果。内容长度固定 —— 状态类 Label 拼长文案会引发整页重排（坑 92）。"""
                code = {t: c for c, t in sdprofile.family_choices("image")}.get(
                    v["img_family"].get(), sdprofile.AUTO)
                img_dir = str(self.cfg.get("image_model_dir", "") or "")
                path = os.path.join(img_dir, str(self.cfg.get("img_model_file", "") or ""))
                fid, _basis, _ = sdprofile.detect_file(path, kind="image", forced=code)
                # 只留族名：「通用（只喂文件）」那半句是给下拉看的说明，贴进状态行
                # 会把这一行顶出右边界（坑 76）
                img_note.set("识别为：%s" % sdprofile.label_of(fid).split("（")[0])

            v["img_family"].trace_add("write", refresh_img_note)
            # 开页这次回显要读模型文件头，延到开页之后（_idle_fill）；下拉联动那次仍即时算
            _idle_fill(t3, refresh_img_note)
            ent(t3, r3, "img_steps", "默认步数",
                "默认采样步数（4~50）：少 = 快、多 = 细节更多，耗时大致与步数成正比"
                "（8 步与 20 步差两倍多）。具体到某个模型族的推荐值，看它自己页面的说明。",
                hint="8 步最快")
            ent(t3, r3, "img_size", "默认分辨率",
                "宽x高，如 1024x1024。分辨率越高越慢。会自动补到本族要求的倍数"
                "（SD 系 8 的倍数、Flux/SD3/Wan 16 的倍数），不合适的尺寸会被抬上去。",
                width=12, hint="宽x高")
            ent(t3, r3, "img_cfg", "默认 CFG",
                "提示词服从度。Qwen-Image 官方推荐 2.5；Flux dev/schnell 常给 1.0，"
                "SDXL/SD1.5 常给 6~8。切族时记得改这一档。")
            fold_img = "local_media:img"
            _fold_head(t3, r3, fold_img,
                       ("img_vae_file", "img_llm_file", "img_clip_l_file",
                        "img_clip_g_file", "img_t5_file", "img_negative",
                        "img_seed", "img_backend", "img_params_backend",
                        "img_extra_args"))
            ent(t3, r3, "img_vae_file", "VAE 文件",
                "留空 = 在本族要求的目录里自动找（按文件名含 vae / ae）。放了多个家族"
                "的权重又挑错时，在这里指名。", width=30, hint="留空=自动", fold=fold_img)
            ent(t3, r3, "img_llm_file", "LLM 编码器",
                "LLM 文本编码器的 .gguf（Qwen-Image、FLUX.2 这类要用）。CLIP 系的模型不用填。",
                width=30, hint="留空=自动", fold=fold_img)
            ent(t3, r3, "img_clip_l_file", "CLIP-L",
                "clip_l.safetensors 之类（Flux / SD3 必需）。", width=30, hint="留空=自动",
                fold=fold_img)
            ent(t3, r3, "img_clip_g_file", "CLIP-G",
                "clip_g.safetensors 之类（SDXL / Flux 用）。", width=30, hint="留空=自动",
                fold=fold_img)
            ent(t3, r3, "img_t5_file", "T5-XXL",
                "t5xxl_fp16.safetensors 之类（Flux / SD3 必需）。", width=30, hint="留空=自动",
                fold=fold_img)
            ent(t3, r3, "img_negative", "负向提示词",
                "留空 = 不传给引擎（Qwen-Image 本来就不带这一项）。SD/SDXL/Wan 这类"
                "支持负向提示词的模型可以自己填。", width=30, hint="留空=不传", fold=fold_img)
            ent(t3, r3, "img_seed", "默认种子",
                "-1 随机；固定数字可复现同一次输出。", fold=fold_img)
            ent(t3, r3, "img_backend", "组件后端",
                "sd-cli --backend。留空 = 用该族默认（LLM 系走 te=cpu,diffusion=cuda0,vae=cuda0，"
                "CLIP 系走 clip=cpu,…）。8GB 显存装不下时可以试 diffusion=cpu 或 vae=cpu。",
                width=30, hint="留空=默认", fold=fold_img)
            ent(t3, r3, "img_params_backend", "权重后端",
                "sd-cli --params-backend。留空 = 引擎 auto-fit 自己安排；显存吃紧可填 "
                "diffusion=disk 从内存/磁盘流式取权重。", width=30, hint="留空=自动",
                fold=fold_img)
            ent(t3, r3, "img_extra_args", "附加参数",
                "原样拼到命令行末尾，是「识别没覆盖到」的人工出口。例如 "
                "--scheduler karras --prediction eps 或 --taesd <路径> 做快速预览。",
                width=30, hint="可留空", fold=fold_img)

            # 「输出目录」这一行与生视频那块**同一形状**（标签 + 打开按钮 + 一句说明），
            # 两块的尾巴长得一样，扫一眼就知道哪儿开文件夹
            fr_i = ttk.Frame(t3)
            ttk.Button(fr_i, text="打开图片输出文件夹",
                       command=lambda: _open_outdir(
                           os.path.join(str(self.cfg.get("sd_dir", "") or ""), "output"),
                           "生图输出目录", "生图（sd.cpp） → 引擎目录")).pack(side="left")
            row(t3, r3, "输出目录", fr_i,
                "生成结果写在 sd.cpp\\output\\img_时间戳.png；引擎每次按需拉起，进程退出即释放显存。")
            refresh_img_note()

        # ---- 区块 4：本地图像与视频 / 生视频 ----
        #
        # 分层同生图：状态行 → 常改的 8 项（目录 / 主体 / 族 / 分辨率 / 帧数 / 帧率 / 步数 /
        # CFG）→ 「高级参数」折叠区（编码器 / VAE / MoE 高噪段 / 音频 VAE / 负向词 / 容器 /
        # 后端，共 12 项，默认收起）。
        @section("local_media", "vid")
        def _t3b(t3b, r3b):
            def _fill_state(var):
                """依赖就绪回显：扫到几个视频扩散主体（延后跑，要读 GGUF 头）。"""
                try:
                    vids, _encs = scan_video_models(self.cfg)
                    n = len(vids)
                except Exception:
                    n = 0
                if n:
                    var.set("视频模型：认出 %d 个" % n)
                else:
                    var.set("还没认到视频模型：先填下面两项，或去「模型文件与引擎」扫描")

            vid_state = tk.StringVar(value="")
            _state_row(t3b, r3b, vid_state, _fill_state, vid_state)
            ent(t3b, r3b, "video_model_dir", "视频模型文件夹",
                "视频组件存放目录：扩散主体 + 文本编码器（LLM 或 T5-XXL）+ 视频 VAE，"
                "MiniMax-H3 与 Wan 都是这套摆法。"
                "该目录不存在时会自动改扫 models_dir 顶层与各子目录，所以文件散放在模型库里也能识别。",
                width=30)
            ent(t3b, r3b, "vid_model_file", "扩散主体文件名",
                "留空 = 用扫描到的第一个视频扩散 GGUF。", width=30)
            _vf_opts = sdprofile.family_choices("video")
            _vf2code = {t: c for c, t in _vf_opts}
            v["vid_family"] = tk.StringVar(
                value=_vf2code.get(str(self.cfg.get("vid_family", "") or "").strip(),
                                   _vf_opts[0][1]))
            vfile_cb = ttk.Combobox(t3b, textvariable=v["vid_family"], state="readonly",
                                    width=24, values=[t for _c, t in _vf_opts])
            vid_note = tk.StringVar(value="")
            _vfam_row = r3b["i"]
            row(t3b, r3b, "模型族", vfile_cb,
                "同一套 sd-cli 可以跑多个视频家族；认错了在这里手动指定。")
            ttk.Label(t3b, textvariable=vid_note, foreground="#5a6a7a",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=_vfam_row, column=2, sticky="w", padx=(0, 10), pady=5)

            def refresh_vid_note(*_a):
                """回显识别结果：一行、长度固定（长文案塞进 Label 会引发整页重排，坑 92）。"""
                code = {t: c for c, t in sdprofile.family_choices("video")}.get(
                    v["vid_family"].get(), sdprofile.AUTO)
                vdir = str(self.cfg.get("video_model_dir", "") or "")
                name = str(self.cfg.get("vid_model_file", "") or "")
                path = os.path.join(vdir, name) if name else ""
                if not (path and os.path.isfile(path)):
                    try:
                        vids, _e = scan_video_models(self.cfg)
                        path = vids[0] if vids else ""
                    except Exception:
                        path = ""
                fid, _basis, _ = sdprofile.detect_file(path, kind="video", forced=code)
                vid_note.set("识别为：%s" % sdprofile.label_of(fid).split("（")[0])

            v["vid_family"].trace_add("write", refresh_vid_note)
            ent(t3b, r3b, "vid_size", "分辨率",
                "宽x高，如 512x512。视频分辨率对显存和耗时都很敏感，先小后大。", width=12)
            ent(t3b, r3b, "vid_frames", "帧数",
                "视频长度 = 帧数 ÷ 帧率。**不需要自己凑 4n+1**：引擎会自行对齐到合法帧数"
                "（例如填 17 会按 22 帧出片），估算时长也按对齐之后的算。",
                hint="引擎自动对齐")
            ent(t3b, r3b, "vid_fps", "帧率",
                "每秒帧数。MiniMax-H3 的参考视频按 24fps 组织。")
            ent(t3b, r3b, "vid_steps", "采样步数",
                "步数直接决定耗时；链路先通再逐步加大。")
            ent(t3b, r3b, "vid_cfg", "CFG",
                "提示词服从度。MiniMax-H3 用 5.0 就行；Wan 的官方区间是 3~6。"
                "大于 1 时引擎会去编码负向提示词，H3 那一族下面那栏就不能留空。")

            fold_vid = "local_media:vid"
            _fold_head(t3b, r3b, fold_vid,
                       ("vid_llm_file", "vid_vae_file", "vid_t5_file",
                        "vid_tokenizer_file", "vid_high_noise_file",
                        "vid_audio_vae_file", "vid_neg_prompt", "vid_format",
                        "vid_seed", "vid_backend", "vid_params_backend",
                        "vid_extra_args"))
            ent(t3b, r3b, "vid_llm_file", "文本编码器文件名",
                "留空 = 自动取与扩散主体配套的编码器（按文件名匹配，通常名字里带 vl / llm）。",
                width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_vae_file", "视频 VAE 文件名",
                "留空 = 在主体所在目录里按文件名含 vae 自动找（不含 audio 的那个）。",
                width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_t5_file", "T5-XXL 文件名",
                "Wan / LTX / HunyuanVideo 的文本编码器（--t5xxl）。MiniMax-H3 不用填这一项。",
                width=30, hint="留空=自动", fold=fold_vid)
            ent(t3b, r3b, "vid_tokenizer_file", "tokenizer 文件",
                "部分家族要 tokenizer.json（引擎的 --tokenizer）。留空 = 不传。",
                width=30, hint="多数不用填", fold=fold_vid)
            ent(t3b, r3b, "vid_high_noise_file", "高噪段模型",
                "Wan2.2 的 MoE 版是**两个**扩散文件（高噪段 + 低噪段），这里填高噪段那个"
                "（--high-noise-diffusion-model）。5B 版与单文件模型留空即可。",
                width=30, hint="MoE 才要", fold=fold_vid)
            ent(t3b, r3b, "vid_audio_vae_file", "音频 VAE",
                "只有想要**有声视频**才需要；没有它照样出片，只是没有声音。"
                "留空 = 按文件名含 audio + vae 自动找。", width=30, hint="留空=自动",
                fold=fold_vid)
            ent(t3b, r3b, "vid_neg_prompt", "负向提示词",
                "MiniMax-H3 在 CFG>1 时**必须能编码出负向提示词**，留空会报 "
                "failed to encode negative video prompt 并退出码 1 —— 这一族留空时代码会用"
                "内置兜底值。Wan / LTX 不要求，留空就不传给引擎。",
                width=30, hint="H3 不能留空", fold=fold_vid)
            ent(t3b, r3b, "vid_format", "输出容器",
                "webm / avi / webp（sd-cli 单文件视频输出只支持这三种）。", width=10,
                fold=fold_vid)
            ent(t3b, r3b, "vid_seed", "种子", "-1 随机。", fold=fold_vid)
            ent(t3b, r3b, "vid_backend", "组件后端",
                "sd-cli --backend：各组件跑在哪。默认把文本编码器放 CPU、扩散与 VAE 放显卡，"
                "与生图一致。", width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_params_backend", "权重后端",
                "sd-cli --params-backend：权重放哪。显存吃紧时可填 diffusion=disk 让引擎从内存/磁盘流式取权重。",
                width=30, fold=fold_vid)
            ent(t3b, r3b, "vid_extra_args", "附加参数",
                "原样拼进命令行。默认开了 --vae-tiling --temporal-tiling 分块解码来压显存。",
                width=30, fold=fold_vid)

            fr_v = ttk.Frame(t3b)
            ttk.Button(fr_v, text="打开视频输出文件夹",
                       command=lambda: _open_outdir(
                           os.path.join(str(self.cfg.get("sd_dir", "") or ""), "video"),
                           "生视频输出目录", "生视频（sd.cpp） → 引擎目录")).pack(side="left")
            row(t3b, r3b, "输出目录", fr_v,
                "生成结果写在 sd.cpp\\video\\vid_时间戳.webm；引擎每次按需拉起，进程退出即释放显存。")
            # 开页这次回显可能要扫视频目录，延到开页之后（_idle_fill）；下拉联动那次仍即时算
            _idle_fill(t3b, refresh_vid_note)

        # ---- 区块 5：本地模型 API（给 agent 调用） ----
        @section("api", "api")
        def _t4(t4, r4):

            self.api_hint_var = tk.StringVar(value="")
            api_key_var = tk.StringVar(value=str(self.cfg.get("api_key", "")))
            # 模型名回显要读一次盘（服务没在跑时走 scan_models），延到开页之后填：
            # 别挡在"点一下左栏"这一下上（_idle_fill）
            api_model_var = tk.StringVar(value="（读取中…）")

            def _fill_api_model(var=api_model_var):
                var.set(self._api_model_name() or "（暂无）")

            _idle_fill(t4, _fill_api_model)
            # 没有本地可转发的文本模型 = 这个功能开不起来：默认关、复选框锁住、顶上说明原因
            usable = has_local_chat(self.cfg)
            self._proxy_usable = usable

            running = self.proxy.running()
            ttk.Label(t4, text=("已启用 · 端口 %s" % self.cfg.get("proxy_port", 8081))
                      if running else "未启用（在本页开启后，agent 才能连上）",
                      foreground="#1a7f37" if running else "#999999",
                      font=("Microsoft YaHei UI", 10, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4["i"] = 1
            if not usable:
                ttk.Label(t4, text="这个功能要有本地文本模型才能用：先在 设置 → 本地模型 备好引擎与模型。",
                          foreground="#c01c28", wraplength=740, justify="left",
                          font=("Microsoft YaHei UI", 9)).grid(
                    row=r4["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
                r4["i"] += 1

            fr = ttk.Frame(t4)
            ttk.Button(fr, text="启动 / 重启服务（给 agent 用）", width=22,
                       command=self.on_start_restart_agent).pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="停止服务", width=10,
                       command=self.stop_server_async).pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="重启代理", width=10,
                       command=self._restart_proxy).pack(side="left")
            row(t4, r4, "服务控制", fr,
                "与主页面是同一个服务，只是按下面那个 context 启动；"
                "agent 要的模型或 context 与当前不一致时，会自动停掉再重启。")

            self.agent_ctx_var = tk.StringVar(
                value=str(ctx_for(self.cfg, agent=True)))
            row(t4, r4, "agent 上下文长度",
                ttk.Entry(t4, textvariable=self.agent_ctx_var, width=10),
                "agent 通过下面那个地址调用时，服务用这个上下文长度启动"
                "（默认 35B=131072、27B=32768；改完对下次启动生效）。")
            v["agent_ctx"] = self.agent_ctx_var

            fr = ttk.Frame(t4)
            v_bu = tk.StringVar(value=self._api_base_url())
            ttk.Entry(fr, textvariable=v_bu, width=25, state="readonly").pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(v_bu.get(), "Base URL")).pack(side="left")
            row(t4, r4, "地址与 Key", fr,
                "别的软件（agent / 脚本）填这一行：地址是本机的 OpenAI 兼容入口，"
                "Key 一起给它。不要填 8080 —— 那个是后端服务，填了会连不上。")

            fr = ttk.Frame(t4)
            ttk.Entry(fr, textvariable=api_key_var, width=20, state="readonly").pack(
                side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(api_key_var.get(), "API Key")).pack(
                side="left", padx=(0, 6))
            ttk.Button(fr, text="重新生成", width=10,
                       command=lambda: (api_key_var.set(self._gen_api_key()),
                                        self.api_hint_var.set(
                                            "已生成新 Key，重启服务后生效"))).pack(side="left")
            row(t4, r4, "API Key", fr, "鉴权密钥（随机生成）；修改后需重启服务生效。")

            fr = ttk.Frame(t4)
            ttk.Entry(fr, textvariable=api_model_var, width=25, state="readonly").pack(
                side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(api_model_var.get(), "模型名")).pack(side="left")
            row(t4, r4, "模型名", fr, "建议填写值（实时取服务加载的模型）；支持模糊匹配，略写也能命中。")

            fr = ttk.Frame(t4)
            ttk.Button(fr, text="复制完整配置（含填法说明）", width=24,
                       command=lambda: self._copy_text(self._api_config_text(), "完整配置")).pack(side="left")
            row(t4, r4, "一键复制", fr, "粘贴到任意 agent 应用的自定义模型配置即可接入。")

            ent(t4, r4, "proxy_port", "代理端口",
                "agent 接入端口（默认 8081）；改动后点「重启代理」生效，"
                "并且要同步改 agent 里填的端口——8080 是后端服务端口，不是给 agent 的。",
                width=8, hint="重启生效")
            v_px = tk.BooleanVar(value=bool(self.cfg.get("proxy_enabled", True)) and usable)
            cb_px = ttk.Checkbutton(t4, text="启用 API 代理（随程序启动）", variable=v_px,
                                    state="normal" if usable else "disabled")
            row(t4, r4, "启用代理", cb_px,
                "关闭后 agent 无法接入；改动后点「重启代理」生效。" if usable else
                "现在锁着：这台机器上还没有能转发的本地文本模型。")
            v["proxy_enabled"] = v_px

            ttk.Label(t4, textvariable=self.api_hint_var, foreground="#1a7f37",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=1, columnspan=2, sticky="w", pady=4)
            r4["i"] += 1
            ttk.Label(t4, text=("提示：agent 应用的请求通常包含系统提示与工具定义，上下文较长——"
                                "当前 context=%s，建议 ≥ 16384（在 设置 → 服务参数 调整后重启服务）。"
                                # 这一页是 agent 场景，显示 agent 那一份（与上面那个输入框同源）；
                                # 原来显示全局兜底 cfg["ctx"]，与按模型记忆的实际值对不上（坑 130）
                                % ctx_for(self.cfg, agent=True)),
                      foreground="#b58900", wraplength=740, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=0, columnspan=3, sticky="w", pady=(8, 2))
            r4["i"] += 1
            ttk.Label(t4, text=("这个代理只转发本地文本模型：生图 / 生视频不经这里调用"
                                "（云端那两条在应用内直连服务商原生接口，"
                                "本地那两条走 sd-cli；agent 用不到图像接口）。"),
                      foreground="#c01c28", wraplength=740, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=0, columnspan=3, sticky="w", pady=(8, 2))
            r4["i"] += 1

            # 底部「保存」在本页时的额外动作：端口/启停真的变了就把代理重启一次，
            # 否则"保存了却没生效"和"保存了却关窗"一样让人以为配好了
            init_proxy = (int(self.cfg.get("proxy_port", 8081) or 0),
                          bool(self.cfg.get("proxy_enabled", True)))

            def _api_hook():
                try:
                    new = (int(str(v["proxy_port"].get()).strip()), bool(v_px.get()))
                except Exception:
                    return False, "代理端口要是数字，改好再保存。"
                if new != init_proxy:
                    self._restart_proxy()
                    self.api_hint_var.set("代理已按新设置重启（端口 %s）。" % new[0])
                return True, ""

            save_hooks[t4] = _api_hook

        # ---- 区块 6：云端模型 / 服务商与密钥 ----
        @section("cloud", "prov")
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
            # 「原生接口协议」下拉里摆的是中文标签，存进配置的是代码（auto/none/aliyun/…）：
            # 域名认不出来的自建代理只能靠这一项手动指过去
            _ma_opts = [("auto", "自动判断（按域名识别）"),
                        ("none", "不接（这个服务商只用文本）")] + [
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
                    url_lbl.set("请求地址：%s"
                                % providers.chat_completions_url(
                                    providers.builtin(st["pid"])))
                else:
                    url_lbl.set("请求地址：%s"
                                % providers.chat_completions_url(
                                    {"base_url": vars_["base_url"].get()}))

            def refresh_key(*_a):
                m = secrets.mask(secrets.get_api_key(st["pid"])) if st["pid"] else ""
                key_lbl.set("该服务商已存密钥：%s" % m if m else "该服务商还没有密钥")

            def refresh_media(*_a):
                """回显"这个服务商能不能走云端生图/生视频"，省得用户猜。

                原生接口地址是**从 base_url 推**的（同族域名只差 /compatible-mode 这一段），
                推不出来时在这里说清楚，而不是等用户发一条消息换来一个看不懂的 404。
                """
                p = provider_snapshot()
                api, root = providers.media_api(p), providers.media_api_root(p)
                # 只在"这条接不了 / 要你手填"时才占一行（W 2026-10-03）；认出来了不说话
                if str(p.get("media_api") or "auto") == "none":
                    media_lbl.set("云端生图 / 生视频：这条不接（服务商只用文本对话）。")
                elif not api:
                    media_lbl.set("云端生图 / 生视频：这条不接原生接口；"
                                  "自建网关可手动选协议或填「原生接口地址」。")
                elif not root:
                    media_lbl.set("云端生图 / 生视频：请填「原生接口地址」。")
                else:
                    media_lbl.set("")

            def refresh_jobs(*_a):
                """任务台账一行的现状：产物只活 24 小时，积压要看得见。"""
                try:
                    pend = cloudjobs.unfinished()
                except Exception as e:
                    jobs_lbl.set("云端任务台账读不了：%s" % e)
                    return
                if not pend:
                    jobs_lbl.set("云端任务台账：没有等着取回的任务。")
                    return
                jobs_lbl.set("云端任务台账：%d 个结果还没落地 ｜ %s"
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
                menu_lbl.set("加入主页面的模型：%d 个（模型菜单里就出现这些）"
                             % len(st["models"]))
                cat_lbl.set("该服务商已知模型：%d 个（勾选界面里可选，缓存着不重复请求）"
                            % len(providers.catalog(
                                {"models": st["models"],
                                 "model_kinds": st["kinds"]})))

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
                editable = providers.builtin_base_editable(pid)
                # 内置服务商：名称一律不开放；地址只在"按量百炼"这类**因账号而异**的条目上
                # 开放（grid_remove 记住原位，切回别的服务商时原样还回来）
                if st["builtin"]:
                    # 只在"这一条能改地址"这个特殊情况下留一行说明（W 2026-10-03）：
                    # 字段本来已经收起，"名称与 base_url：内置"纯属重复
                    if editable:
                        built_note.configure(
                            text="这一条能改成你自己账号的专属域名：把 base_url 换成 "
                                 "https://你的WorkspaceId.cn-beijing.maas.aliyuncs.com/"
                                 "compatible-mode/v1（密钥与域名配套，不能混用）。")
                        built_note.grid()
                    else:
                        built_note.grid_remove()
                    fields.grid_remove()
                    if editable:
                        fields2.grid()
                    else:
                        fields2.grid_remove()
                else:
                    built_note.grid_remove()
                    fields.grid()
                    fields2.grid()
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
                        msg_lbl.set("保存失败：%s" % err)
                        return False, err      # 半填的新服务商：留着窗口让用户补
                    st["pid"] = base_id
                    save_config(self.cfg)
                    refresh_combo(keep=base_id)
                    if not silent:
                        msg_lbl.set("已新建服务商「%s」。" % (name or base_id))
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
                    msg_lbl.set("保存失败：provider「%s」不存在。" % st["pid"])
                    return False, "provider 不存在，可能刚被删除。"
                save_config(self.cfg)
                if not silent:
                    msg_lbl.set("已保存「%s」（主页面模型 %d 个）。"
                                % (st["pid"], len(st["models"])))
                self._update_model_label()
                return True, ""

            def ensure_saved():
                """密钥/模型选择/测试都要先有个能用的 provider id。"""
                ok, msg = commit(silent=True)
                if not ok:
                    messagebox.showwarning("先保存", msg)
                return ok

            def do_delete():
                pid = st["pid"]
                if not pid:
                    msg_lbl.set("没有选中要删除的服务商。")
                    return
                tip = ("「%s」是内置服务商，删掉后下次启动会按内置定义重新出现。"
                       % providers.provider_label_for(self.cfg, pid)) if providers.is_builtin(pid) \
                    else "删除「%s」？" % providers.provider_label_for(self.cfg, pid, vars_["name"].get())
                if not messagebox.askyesno("删除服务商",
                                           tip + "\n\n模型清单会一起删除，已存密钥也会被清掉。"):
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
                msg_lbl.set("已删除。")

            # ---------------- 密钥 ----------------
            def do_key_dialog():
                d = tk.Toplevel(win)
                d.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
                d.title("API Key")
                d.transient(win)
                pname = (providers.builtin(st["pid"]).get("name")
                         or vars_["name"].get() or st["pid"] or "新的服务商")
                ttk.Label(d, text="给「%s」填写 API Key" % pname,
                          font=("Microsoft YaHei UI", 10, "bold")).pack(
                    anchor="w", padx=14, pady=(12, 4))
                ttk.Label(d, text="密钥只写进 secrets.json，留在你这台机器上；界面与配置文件里都只显示掩码。",
                          foreground="#808080", wraplength=420, justify="left",
                          font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
                e = ttk.Entry(d, width=40, show="●")
                e.pack(padx=14, pady=10)
                tip = ttk.Label(d, textvariable=key_lbl, foreground="#808080",
                                font=("Microsoft YaHei UI", 9))
                tip.pack(anchor="w", padx=14)
                bf = ttk.Frame(d)
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
                            messagebox.showwarning("API Key", err or "先填名称与 base_url。")
                            return
                    secrets.set_api_key(st["pid"], k)
                    refresh_key()
                    msg_lbl.set("已保存「%s」的密钥。" % st["pid"])
                    d.destroy()
                    # 填完密钥可能就凑齐了"第一个可用模型"（这台机器没装任何引擎的
                    # 常见情形）⇒ 立刻切过去（坑 150）
                    try:
                        self._maybe_adopt_first_model(force=True)
                    except Exception:
                        pass

                def clear():
                    if st["pid"] and messagebox.askyesno("API Key", "清除「%s」的已存密钥？"
                                                         % st["pid"]):
                        secrets.set_api_key(st["pid"], "")
                        refresh_key()
                    d.destroy()

                ttk.Button(bf, text="清除", width=8, command=clear).pack(side="left", padx=4)
                ttk.Button(bf, text="取消", width=8, command=d.destroy).pack(side="left", padx=4)
                ttk.Button(bf, text="保存密钥", width=10, command=save).pack(side="left")
                e.focus_set()
                widgets.center_on(d, win)   # 摆到设置页正中，别落在屏幕左上角

            # ---------------- 模型选择界面 ----------------
            def open_picker(fetch=None, explain=""):
                """独立的模型勾选界面：勾中的才进主页面菜单；可手填模型名并"试一试"。"""
                if not st["pid"]:
                    messagebox.showwarning("选择模型", "先保存这个服务商，再选模型。")
                    return
                pid = st["pid"]
                known = providers.catalog({"models": st["models"],
                                           "model_kinds": st["kinds"]})
                if fetch is None:
                    # W 定的规矩：拉过就缓存，除非用户点「刷新清单」，否则不重复请求
                    fetch = not known
                d = tk.Toplevel(win)
                d.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
                d.title("选择模型 · %s"
                        % (providers.get_provider(self.cfg, pid) or {}).get("name", pid))
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
                hint_lbl = ttk.Label(head, text="勾中并点「确定」才进主页面菜单。",
                                     foreground="#808080", wraplength=520, justify="left",
                                     font=("Microsoft YaHei UI", 9))
                hint_lbl.pack(side="left")
                status = tk.StringVar(value=explain or
                                      ("表里是已知的 %d 个模型；点「刷新清单」可向接口重新索取。"
                                       % len(known)))
                st_lbl = ttk.Label(d, textvariable=status, foreground="#808080",
                                   wraplength=600, justify="left",
                                   font=("Microsoft YaHei UI", 9))
                st_lbl.pack(side="top", anchor="w", padx=12, pady=(0, 2))

                wrap = ttk.Frame(d)
                wrap.pack(side="top", fill="both", expand=True, padx=12)
                cv = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
                sb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview)
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
                _p_kind = ttk.Combobox(table, state="readonly", width=9, values=[
                    providers.KIND_LABEL[k] for k in providers.KIND_ORDER])
                _p_img = ttk.Combobox(table, state="readonly", width=9, values=[
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
                           for txt in ("模型", "图片输入", "模型类型")}
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
                    for lbl, w in ((hdr_lbl["模型"], cb), (hdr_lbl["图片输入"], ic),
                                   (hdr_lbl["模型类型"], kc)):
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
                    ic = ttk.Combobox(table, textvariable=cv_img, state="readonly", width=9,
                                      values=[capability.CHOICE_LABEL[c] for c in
                                              capability.CHOICES])
                    kc = ttk.Combobox(table, textvariable=cv_kind, state="readonly",
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
                    return "%s %s · %d 个%s" % ("▾" if open_ else "▸", key, len(models),
                                            ("（已勾 %d）" % n_on) if n_on else "")

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
                    status.set("正在向接口取模型清单…")

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
                        status.set("没能取到清单：%s\n也可以在下面填模型名，用「测试连接并加入」验证。"
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
                    status.set("接口给了 %d 个模型（新增 %d 个）。" % (len(ids), added))

                def try_add():
                    """"这名字服务端认不认"只有**对话模型**能零成本问出来。

                    生图 / 生视频的模型名走各家原生接口：拿 `chat/completions` 去试必然
                    `Model not exist`（服务端不认它是对话模型），而真正的媒体端点一试就是
                    真金白银（MiniMax 出图按张计费）。所以认出来不是文本模型时**不发那次
                    注定没意义的请求**，直接按名字收进表并说清为什么验不了。
                    """
                    m = e_new.get().strip()
                    if not m:
                        status.set("先输入模型名。")
                        return
                    if providers.guess_kind(m) != "text":
                        manual_add("这个名字看着像媒体模型，对话接口验不了它"
                                   "（原生媒体接口一试就计费）")
                        return
                    status.set("正在用「%s」试一次最小请求（服务端会点名说这个名字认不认）…" % m)

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
                        status.set("✅ %s：%s（已勾上）" % (m, text))
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
                        status.set("先输入模型名。")
                        return False
                    if m in rows:
                        status.set("表里已经有「%s」了。" % m)
                        return False
                    k = providers.guess_kind(m)
                    add_models([(m, k, True)])
                    extra = ""
                    if k != "text":
                        p = providers.get_provider(self.cfg, pid) or {}
                        if not providers.supports_media(p, k):
                            extra = "；注意：这一家还没接%s的原生接口，发送前会被拦下" \
                                % providers.KIND_LABEL[k]
                    status.set("%s已按名字加入「%s」（能力：%s；觉得不对在那一行右边改）%s。"
                               % ((why + "，") if why else "", m,
                                  providers.KIND_LABEL[k], extra))
                    e_new.delete(0, "end")
                    return True

                # 底部三件套：手填行（含刷新/验证按钮）+ 确定取消，都排在表格之前分配空间，
                # 窗口再矮也是压表格，不会把按钮挤掉
                botf = ttk.Frame(d)
                botf.pack(side="bottom", fill="x", padx=12, pady=(4, 2), before=wrap)
                ttk.Label(botf, text="模型名", foreground="#808080",
                          font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 4))
                e_new = ttk.Entry(botf, width=16)
                e_new.pack(side="left", fill="x", expand=True)
                # 回车 = 直接加入。以前这个框一个绑定都没有：敲完名字按回车什么也不会发生，
                # 看起来就是"加不进去"（W 实测报的那条）。
                e_new.bind("<Return>", lambda _e: manual_add())
                e_new.bind("<KP_Enter>", lambda _e: manual_add())
                ttk.Button(botf, text="测试连接并加入", width=14,
                           command=try_add).pack(side="left", padx=(6, 0))
                ttk.Button(botf, text="直接加入", width=10,
                           command=lambda: manual_add()).pack(side="left", padx=(6, 0))
                # 最下沿：左边两个动作按钮，右边确定/取消，一行装得下 470px 的最小宽度
                bf2 = ttk.Frame(d)
                bf2.pack(side="bottom", fill="x", padx=12, pady=(2, 12), before=botf)
                ttk.Button(bf2, text="刷新清单", width=9,
                           command=lambda: pull(True)).pack(side="left")
                ttk.Button(bf2, text="验证图片输入", width=12,
                           command=lambda: do_verify()).pack(side="left", padx=(6, 0))
                ttk.Button(bf2, text="取消", width=8, command=d.destroy).pack(side="right")
                ttk.Button(bf2, text="确定", width=10,
                           command=lambda: ok_apply(d)).pack(side="right", padx=6)

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
                        msg_lbl.set("已写入主页面模型：（没勾任何模型）")
                    else:
                        msg_lbl.set("已写入主页面模型：%d 个（%s%s）"
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
                        status.set("先勾几个模型再验证（一次最多测 5 个）。")
                        return
                    key = secrets.get_api_key(pid)
                    snap = provider_snapshot()
                    status.set("正在向服务端确认 %d 个模型的图片输入能力（每个发一次最小请求）…"
                               % len(targets))

                    def work():
                        out = []
                        for m in targets:
                            try:
                                out.append((m,) + cloud.probe_image_input(
                                    self.cfg, provider=snap, api_key=key, model=m))
                            except Exception as e:
                                out.append((m, capability.UNKNOWN, "探测出错：%s" % e))
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
                            lines.append("%s → 未确认（%s）" % (m, why))
                    status.set("验证结果：" + "；".join(lines)
                               + "。改完点「确定」才写入。")

                widgets.center_on(d, win)   # 摆到设置页正中（控件都建完了，尺寸才量得准）
                if fetch:
                    pull()

            def do_pick_models():
                open_picker()          # 打开就用缓存；要重新请求请点「刷新清单」

            def do_remove_selected():
                sel = lb.curselection()
                if not sel:
                    msg_lbl.set("先在列表里选中要移出的模型。")
                    return
                # 反查原名（列表里是缩写显示行，配置里是原名 —— 坑 131）
                m = lb_map.get(lb.get(sel[0]))
                if not m:
                    msg_lbl.set("这条没能对上配置里的模型名（列表可能是旧的）："
                                "重开一次设置窗口再试。")
                    return
                st["models"] = [x for x in st["models"] if x != m]
                commit(silent=True)
                refresh_menu_list()
                msg_lbl.set("已从主页面菜单移出「%s」（它仍留在已知模型里，可随时再勾）。"
                            % providers.short_of(m))

            # ---------------- 连通性 ----------------
            def do_test():
                if not ensure_saved():
                    return
                key = secrets.get_api_key(st["pid"])
                if not key:
                    msg_lbl.set("先按「填该服务商 API Key」存好密钥，再测连接。")
                    return
                msg_lbl.set("测试中…（用文本模型发一次 max_tokens=1 的最小请求）")
                t4b.update_idletasks()

                p = providers.get_provider(self.cfg, st["pid"]) or provider_snapshot()

                def work():
                    ok, text = cloud.probe({**p, "models": st["models"],
                                            "model_kinds": st["kinds"]}, api_key=key)
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
                                explain="连接测试通过，下面是刚从接口取到的清单。")
                        else:
                            messagebox.showwarning("连不上", text)
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
                load(pid)          # 选中"＋ 新建服务商…"时 pid 为空 → 空白新条目

            bar = ttk.Frame(t4b)
            bar.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4b["i"] += 1
            ttk.Label(bar, text="服务商：").pack(side="left")
            combo = ttk.Combobox(bar, width=24, state="readonly")
            combo.pack(side="left", padx=(0, 6))
            combo.bind("<<ComboboxSelected>>", on_pick)
            ttk.Button(bar, text="删除", width=6, command=do_delete).pack(side="left", padx=3)
            ttk.Button(bar, text="测试连接", width=10, command=do_test).pack(side="left", padx=3)

            kr = ttk.Frame(t4b)
            kr.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4b["i"] += 1
            ttk.Button(kr, text="填该服务商 API Key", width=20,
                       command=do_key_dialog).pack(side="left")
            # 原先这里还有一个「配置本地 API 地址」按钮跳去API 页（W 2026-10-03 要求移除）：
            # 云端这一页只管密钥与模型，混一个"去改本地端口"的入口只会让人以为两者相关。
            # 本地端口在左栏「本地模型 API」那一页。
            # 密钥状态单独一行：它跟着按钮排在同一行时，掩码文本会把这行撑得比
            # 可视区宽（横向不可滚 = 后面的内容看不见）
            ttk.Label(t4b, textvariable=key_lbl, foreground="#808080", wraplength=560,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            # 初始文本只占位：open_picker/load() 一进来就会按"能不能改地址"重写它，
            # 收起来时整行 grid_remove（2026-10-03 W：不再常驻一句"名称与 base_url：内置"）
            built_note = ttk.Label(t4b, text="",
                                   foreground="#808080", wraplength=640, justify="left",
                                   font=("Microsoft YaHei UI", 9))
            built_note.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            fields = ttk.Frame(t4b)      # 名称：只有自定义服务商可填
            fields.grid(row=r4b["i"], column=0, columnspan=3, sticky="w")
            sub = {"i": 0}
            ent(fields, sub, None, "名称", "服务商显示名（菜单、下拉、提示里都用它，写中文就行）；"
                                        "新建时它还兼作这条记录的索引名。",
                width=30, var=vars_["name"])
            r4b["i"] += 1

            # 地址类控件单独一个框：内置服务商里"按量百炼"的工作空间域名**因账号而异**，
            # 必须让用户自己填，所以不能跟着"内置一律不可改"一起收起
            fields2 = ttk.Frame(t4b)
            fields2.grid(row=r4b["i"], column=0, columnspan=3, sticky="w")
            sub2 = {"i": 0}
            ent(fields2, sub2, None, "base_url",
                "OpenAI 兼容根地址，带不带 /v1 都行（会自动补）。",
                width=36, var=vars_["base_url"], trace=lambda *a: (refresh_url(),
                                                                   refresh_media()))
            ent(fields2, sub2, None, "原生接口地址",
                "云端生图/生视频用的地址，留空 = 从上面那段自动推（把 /compatible-mode/v1 "
                "换成 /api/v1）。只有跨方案时才需要填，key 与域名不可混用。",
                width=36, var=vars_["media_base_url"], trace=refresh_media)
            # width=18：最长的候选串是「不接（这个服务商只用文本）」这类 12 个中日韩字，
            # 18 个"平均字宽"够摆下（24 会把这一行撑到 775px，比视口 768 还宽 → 整行被压扁）
            _ma_cb = ttk.Combobox(fields2, textvariable=vars_["media_api"], state="readonly",
                                  width=18, values=[t for _c, t in _ma_opts])
            _ma_cb.bind("<<ComboboxSelected>>", lambda *a: refresh_media())
            _row(fields2, sub2, "原生接口协议", _ma_cb,
                 "生图与生视频走哪套原生协议。按域名自动认（阿里云系 / MiniMax / 智谱 / 华为系）；"
                 "自建网关、代理或专有域名认不出来时在这里手动指一次。"
                 "选「不接」= 这个服务商只用文本对话，生图生视频会在发送前拦下。",
                 hint="自动/手动")
            r4b["i"] += 1

            ttk.Label(t4b, textvariable=media_lbl, foreground="#808080", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(2, 6))
            r4b["i"] += 1

            mf = ttk.Frame(t4b)
            mf.grid(row=r4b["i"], column=1, columnspan=2, sticky="w", pady=(2, 4))
            r4b["i"] += 1
            lb = tk.Listbox(mf, height=5, width=34, exportselection=False,
                            font=("Microsoft YaHei UI", 9))
            lb.pack(side="left")
            btns = ttk.Frame(mf)
            btns.pack(side="left", padx=(6, 0), fill="y")
            ttk.Button(btns, text="选择模型…", width=12,
                       command=do_pick_models).pack(anchor="w", pady=1)
            ttk.Button(btns, text="移出选中项", width=12,
                       command=do_remove_selected).pack(anchor="w", pady=1)
            ttk.Button(btns, text="刷新清单", width=12,
                       command=lambda: (commit(silent=True),
                                        open_picker(fetch=True))).pack(anchor="w", pady=1)

            ttk.Label(t4b, textvariable=menu_lbl, foreground="#808080", wraplength=430,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=1, columnspan=2, sticky="w")
            r4b["i"] += 1
            ttk.Label(t4b, textvariable=cat_lbl, foreground="#808080", wraplength=430,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=1, columnspan=2, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            ent(t4b, r4b, None, "超时（秒）", "单次请求超时；思考型模型建议 300 以上。",
                var=vars_["timeout"])
            ent(t4b, r4b, None, "上下文窗口",
                "该服务商模型的上下文长度（token），0 = 不声明。"
                "留 0 时文本附件按默认 %d token 折行；填上真实窗口（例如 1000000）"
                "就多带一些。注意上限还夹在 %d token：附件会留在历史里，"
                "之后每一轮都重发一遍，塞太满等于把后续每轮都变成大账单。"
                % (textfile.CLOUD_TOKEN_BUDGET, textfile.CLOUD_TOKEN_CAP),
                var=vars_["ctx"])

            i = r4b["i"]
            r4b["i"] += 1
            ttk.Checkbutton(t4b, text="启用该服务商", variable=vars_["enabled"]).grid(
                row=i, column=1, sticky="w", pady=4)

            # 「每轮显示用量」与「让云端模型自选读取区间」属于云端**文本**对话，
            # 移到本区块之后（左栏「云端模型 → 文本模型」），别混在服务商清单里。

            # ---- 云端生图 / 生视频的档位单独一页（见 _t4c）----
            # 这一页本来就到 1143px 的申请高度，已经超过屏幕可用高度；再叠 9 行
            # 就永远看不见底部了（坑 32：懒加载页要逐页量内容边界 vs 可用面积）。
            i = r4b["i"]
            r4b["i"] += 1
            ttk.Label(t4b, textvariable=jobs_lbl, foreground="#808080", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(6, 2))
            ttk.Button(t4b, text="刷新任务台账", width=14,
                       command=refresh_jobs).grid(row=i, column=2, sticky="e", pady=(6, 2))

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

            sf = ttk.Frame(t4b)
            sf.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(4, 4))
            r4b["i"] += 1
            ttk.Button(sf, text="保存本页", width=12, command=save_page).pack(side="left")
            ttk.Label(sf, textvariable=url_lbl, foreground="#808080", wraplength=430,
                      justify="left", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)
            ttk.Label(t4b, textvariable=msg_lbl, foreground="#0b57d0", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(2, 0))
            r4b["i"] += 1

            save_hooks[t4b] = lambda: commit(silent=True)
            refresh_combo()
            refresh_jobs()
        # ---- 区块 6b：云端 / 文本模型（对话行为与附件预算，跟具体服务商无关）----
        @section("cloud", "ctext")
        def _t4d(t4d, r4d):
            """云端文本对话的两项开关：用量显示 + 附件自选区间。"""
            i = r4d["i"]
            r4d["i"] += 1
            v["show_usage"] = tk.BooleanVar(value=bool(self.cfg.get("show_usage", True)))
            # 勾选框文字长，必须跨列放：占在 column=1 上会把整列撑宽，
            # 于是右侧"短摘要 + ?"那一格被 grid 挤扁（版式自检抓到过）
            ttk.Checkbutton(t4d, text="每轮结束后显示 token 用量（云端计费可见性）",
                            variable=v["show_usage"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=6)

            i = r4d["i"]
            r4d["i"] += 1
            v["cloud_file_model_decides"] = tk.BooleanVar(
                value=bool(self.cfg.get("cloud_file_model_decides", False)))
            ttk.Checkbutton(
                t4d, text="文本附件超预算时，让云端模型自己决定读哪一段",
                variable=v["cloud_file_model_decides"]).grid(
                row=i, column=0, columnspan=2, sticky="w", pady=(0, 6))
            widgets.HelpDot(t4d,
                            "打开后：附件太长时会先发一次「只要 JSON 行号」的规划请求；"
                            "模型给的范围仍会被预算与行数上限夹住，规划失败就按原预算发送，"
                            "不影响正常对话。只规划一轮，不加多轮循环（每轮都是真金白银）。"
                            "默认关。"
                            ).grid(row=i, column=2, sticky="e", pady=(0, 6))

            _row(t4d, r4d, "文本预算", ttk.Label(
                t4d, text="按服务商各自声明的上下文窗口算", foreground="#5a5a5a"),
                "云端文本的附件预算 = min(该服务商声明的上下文窗口 − max_tokens − 预留, 200000)。"
                "上限 200000 是成本护栏，不是拍脑袋：附件会留在历史里**每一轮都重发**，"
                "声明 1M 也不代表该一次塞 1M。窗口在上一区块「服务商与密钥」里按服务商填。",
                hint="上限 20 万")
        # ---- 区块 7：云端 / 生图与生视频（服务商原生接口，与本地 sd.cpp 无关）----
        @section("cloud", "cmedia")
        def _t4c(t4c, r4c):
            """云端生图 / 生视频的档位与落地目录。

            **左右两列**：左列生图、右列生视频，各自的存放目录与「打开文件夹」放在**本列底部**
            （原先目录两行夹在中间，扫一眼看不出哪条属于哪边）。
            轮询间隔与等待上限是**两条链路共用**的（生图万一回的是 task_id 也会就地转轮询），
            所以不属于任何一列，压在两列下面一条横栏里。
            费用单价不在这里填：它按模型走，点「成本预估算」开次级窗口。
            """
            cols = ttk.Frame(t4c)
            cols.grid(row=r4c["i"], column=0, columnspan=3, sticky="nw")
            r4c["i"] += 1
            col_l = ttk.Frame(cols)
            col_l.grid(row=0, column=0, sticky="nw")
            col_r = ttk.Frame(cols)
            col_r.grid(row=0, column=1, sticky="nw", padx=(30, 0))
            for f, name in ((col_l, "云端生图"), (col_r, "云端生视频")):
                ttk.Label(f, text=name, font=("Microsoft YaHei UI", 10, "bold")).grid(
                    row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
            rl, rr = {"i": 1}, {"i": 1}

            ent(col_l, rl, "cloud_img_size", "出图尺寸",
                "填「宽x高」或「宽*高」都行，发出去前会按这一家的写法换算："
                "阿里云 1024*1024、智谱与华为 1024x1024、MiniMax 换成比例（16:9）"
                "或宽高两个整数。超出这一家允许的区间时，服务端会点名报错。"
                "阿里云：qwen-image-3.0-pro 面积 512×512…2560×2560、"
                "qwen-image-max 到 1664×1664、wan2.7-image 像素 589824…16777216。",
                width=11, hint="宽x高", lw=8)
            ent(col_l, rl, "cloud_img_negative", "生图负向词",
                "选填。留空 = 不传该参数。", lw=8)
            ent(col_l, rl, "cloud_img_dir", "图片存放",
                "云端生图的落地目录；留空 = 程序目录下的 cloud_out\\images。", lw=8)
            fl = ttk.Frame(col_l)
            ttk.Button(fl, text="打开图片文件夹", width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "image"),
                           "云端生图的落地目录",
                           "云端模型 → 生图 / 生视频 → 图片存放")).pack(side="left")
            row(col_l, rl, "输出目录", fl,
                "产物一落地就在这里。云端地址只活 24 小时，本地这份是唯一的留存；"
                "目录还没生成时点一下会先建出来再打开。", lw=8)

            ent(col_r, rr, "cloud_video_resolution", "视频分辨率",
                "各家档位不一样（阿里云/MiniMax 用 720P、1080P、768P、2K 这类标签，"
                "智谱与华为用 1280x720 这种宽高）。"
                "留空 = 不传这个参数，用服务端默认（阿里云 Token Plan 的 happyhorse "
                "不传时按 1080P / 16:9 出）。不知道这一家允许什么时，留空最安全。",
                hint="留空=默认", lw=8)
            ent(col_r, rr, "cloud_video_duration", "视频时长",
                "秒。各家允许区间不同（超范围会被服务端点名报错，原话会显示在对话里）："
                "happyhorse-1.1-t2v 是 **3~15 秒**；MiniMax Hailuo 官方页是 6/10 秒、"
                "智谱 cogvideox-3 是 5/10 秒。提交前一律弹一次确认，"
                "单价填过就报金额、没填就说明以账单为准。", hint="各家不同", lw=8)
            ent(col_r, rr, "cloud_video_ratio", "画面比例",
                "例如 16:9 / 9:16 / 1:1；留空 = 不传（画面比例常由素材决定）。", lw=8)
            ent(col_r, rr, "cloud_vid_dir", "视频存放",
                "云端生视频的落地目录；留空 = 程序目录下的 cloud_out\\videos。", lw=8)
            fr = ttk.Frame(col_r)
            ttk.Button(fr, text="打开视频文件夹", width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "video"),
                           "云端生视频的落地目录",
                           "云端模型 → 生图 / 生视频 → 视频存放")).pack(side="left")
            row(col_r, rr, "输出目录", fr,
                "同上：这条链路下它是唯一留存，服务端地址 24 小时就失效。", lw=8)

            ttk.Separator(t4c).grid(row=r4c["i"], column=0, columnspan=3,
                                    sticky="we", pady=(12, 4))
            r4c["i"] += 1
            i = r4c["i"]
            r4c["i"] += 1
            tk.Label(t4c, text="生图与生视频共用", foreground="#5a5a5a",
                     font=("Microsoft YaHei UI", 9, "bold"),
                     background=widgets.default_bg()).grid(
                row=i, column=0, columnspan=3, sticky="w")
            ent(t4c, r4c, "cloud_poll_seconds", "轮询间隔",
                "秒。官方建议 15，且创建/查询/取消三个端点合计 20 QPS——"
                "调小不会让任务更快完成，只会更早撞上限流。", hint="建议 15", lw=8)
            ent(t4c, r4c, "cloud_wait_minutes", "等待上限",
                "分钟。超了就不再干等，任务留在台账里，重启后对话开头会给「取回」按钮。",
                lw=8)
            fc = ttk.Frame(t4c)
            ttk.Button(fc, text="成本预估算", width=14,
                       command=self.open_cost_window).pack(side="left")
            row(t4c, r4c, "费用单价", fc,
                "单价按**模型**填（同一家不同模型不同价，计费单位还可能一个按秒、一个按条）。"
                "填了就在提交前的确认框与对话流里报金额；没填就明说「以账单为准」，不编数字。"
                "生视频无论有没有单价都会二次确认，生图不确认但把价格打在对话流里。", lw=8)

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
                    ("LLM Chat", ("Microsoft YaHei UI", 16, "bold"), "#111111", (0, 2)),
                    ("本地模型工作台 · 版本 %s" % APP_VERSION,
                     ("Microsoft YaHei UI", 9), "#5a5a5a", (0, 2)),
                    ("llama.cpp 对话 · sd.cpp 生图生视频 · 云端服务商",
                     ("Microsoft YaHei UI", 9), "#808080", (0, 0))):
                lab = ttk.Label(names, text=txt_, foreground=fg, font=font,
                                wraplength=420, justify="left")
                lab.pack(anchor="w", pady=pady)
                if txt_.startswith("本地模型工作台"):
                    lab.bind("<Button-1>", _tap_version)

            bf = ttk.Frame(t9)
            i = r9["i"]
            r9["i"] += 1
            bf.grid(row=i, column=0, columnspan=3, sticky="w", pady=(0, 6))
            ttk.Button(bf, text="新手引导", width=12,
                       command=self.start_guide).pack(side="left", padx=(0, 6))
            ttk.Button(bf, text="诊断", width=12,
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
            ustate = tk.StringVar(value="还没检查过。")

            ust_lab = ttk.Label(t9, textvariable=ustate, foreground="#5a6a7a",
                                wraplength=560, justify="left",
                                font=("Microsoft YaHei UI", 9))

            def _say(text, color="#5a6a7a"):
                ustate.set(text)
                ust_lab.configure(foreground=color)

            def _open_page(url):
                if not url:
                    messagebox.showinfo("打开下载页", "还没有可打开的地址：先点「检查更新」。")
                    return
                try:
                    if not webbrowser.open(url):
                        raise RuntimeError("浏览器没响应")
                except Exception as e:
                    messagebox.showwarning(
                        "打开下载页", "打不开浏览器（%s）。\n把这个地址复制到浏览器里就行：\n%s"
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
                    _say("发现新版本：%s（%s）　本机：%s　发布于 %s"
                         % (info.get("name") or info["tag"],
                            info["tag"], cur, info.get("published") or "日期未知")
                         + note, "#1a7f37")
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
                    _say("已是最新：%s（%s）。"
                         % (updater.display_version(info.get("tag") or cur), ch_text_)
                         + note, "#1a7f37")
                elif state == updater.STATE_AHEAD:
                    # 报实话：不并进「已是最新」（多半是自己编的版本还没打 tag）；
                    # 彩蛋（W 2026-10-05）：本机比线上最新的还新时，文案末尾追一句
                    # 「莫非你是测试用户！」—— 只看本次返回结果，不额外去查另一个通道。
                    _say("本机 %s 比线上最新的 %s 还新（%s）。%s"
                         % (cur, updater.display_version(info.get("tag") or "？"),
                            ch_text_, updater.AHEAD_EGG) + note, "#b06000")
                else:
                    # msg 自带"检查更新失败："前缀，这里不再叠一层（2026-10-03）
                    msg = info.get("msg") or "检查更新失败：原因未知。"
                    _say(msg + note, "#b00020")
                    if dialog:
                        messagebox.showwarning("检查更新", msg)

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
                _say("正在向 GitHub 查询%s的最新版本…（本机 %s）" % (ch_text, cur))

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
                        res = (updater.STATE_ERROR, {"msg": "检查更新时出错：%s" % e})
                    self._ui_q.put(lambda: done(*res))

                threading.Thread(target=work, daemon=True).start()

            # 控件放在回调之后建：`command=名字` 是建控件那一刻就要绑定的（坑 115）。
            # 顺序按 W 2026-10-04 的要求：检查更新 → 正式版/测试版 → 打开下载页 → "?"，
            # 并且**删掉左边的「版本类型」标签** —— 那一行的内容自解释，不需要一个名词占位。
            uf = ttk.Frame(t9)
            btn_chk = ttk.Button(uf, text="检查更新", width=12, command=_do_check)
            btn_chk.pack(side="left", padx=(0, 6))
            ch_cb = ttk.Combobox(uf, textvariable=ch_var, state="readonly", width=10,
                                 values=[lab for _c, lab in updater.CHANNELS])
            ch_cb.pack(side="left", padx=(0, 6))
            btn_open = ttk.Button(uf, text="打开下载页", width=12,
                                  command=lambda: _open_page(up_url["v"]),
                                  state="disabled")
            btn_open.pack(side="left", padx=(0, 6))
            upd_help = widgets.HelpDot(uf, _upd_help_text())
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
                    _say("正在向 GitHub 查询%s的最新版本…（本机 %s）"
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
                dpi_status.set("已记住，重开程序后生效。")

            ttk.Checkbutton(df, text="高分屏清晰度", variable=dpi_var,
                            command=set_dpi).pack(side="left")
            ttk.Label(df, text="现在：%s" % ("已开" if now else "没开"),
                      foreground="#808080", font=("Microsoft YaHei UI", 9)).pack(
                side="left", padx=(10, 0))
            widgets.HelpDot(df, "屏幕缩放不是 100% 时开的：开着 = 界面按显示器的真实像素画，"
                                 "文字锐利，同一块屏幕上窗口看着比现在小一档；"
                                 "关着 = 交给 Windows 拉伸，看着大但发虚。\n"
                                 "改完要**重开程序**才生效（这一项只能冷切换）。"
                                 "远程桌面里建议关着。").pack(side="left", padx=(6, 0))
            ttk.Label(t9, textvariable=dpi_status, foreground="#808080", wraplength=360,
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
                    tok_lab.set("GitHub 令牌：已填写 " + secrets.mask(t))
                else:
                    tok_lab.set("GitHub 令牌：未填写（匿名接口 60 次/小时）")

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
                d.withdraw()                # 先藏起来，摆正了再显示（否则左上角闪一下）
                d.title("GitHub 令牌")
                ttk.Label(d, text="GitHub 令牌",
                          font=("Microsoft YaHei UI", 10, "bold")).pack(
                    anchor="w", padx=14, pady=(12, 4))
                ttk.Label(d, text="限额 60 → 5000 次/小时，更新冷却 10 分钟 → 5 秒，"
                                  "并开启后台定时检查。\n"
                                  "只写进本机 secrets.json，界面与配置文件仅显示掩码。",
                          foreground="#808080", wraplength=430, justify="left",
                          font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
                e = ttk.Entry(d, width=48, show="●")
                e.pack(padx=14, pady=10)
                ttk.Label(d, textvariable=tok_lab, foreground="#808080",
                          font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
                bf = ttk.Frame(d)
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
                    msg_lab.set("已保存 GitHub 令牌。")
                    d.destroy()

                def clear():
                    if messagebox.askyesno("GitHub 令牌", "清除已填的 GitHub 令牌？"):
                        secrets.set_github_token("")
                        _refresh()
                        _sync_upd_help()
                        self._dev_upd_start()   # 令牌没了 → 定时器自己会停
                        msg_lab.set("已清除 GitHub 令牌。")
                    d.destroy()

                ttk.Button(bf, text="清除", width=8, command=clear).pack(side="left", padx=4)
                ttk.Button(bf, text="取消", width=8, command=d.destroy).pack(side="left", padx=4)
                ttk.Button(bf, text="保存令牌", width=10, command=save).pack(side="left")
                e.focus_set()
                widgets.center_on(d, win)   # 摆到设置页正中，别落在屏幕左上角

            def reset_dev():
                t = secrets.get_github_token()
                if not messagebox.askyesno(
                        "重置开发者选项",
                        "清掉 GitHub 令牌%s，以及「已提醒过、不再弹窗」的版本记录？\n"
                        "开发者模式保持开启。"
                        % ("（当前 %s）" % secrets.mask(t) if t else "（本来就没填）")):
                    return
                secrets.set_github_token("")
                self.cfg["dev_upd_dismissed"] = ""   # 旧键：2026-10-05 前的后台弹窗记录，顺手清
                self.cfg["upd_dismissed"] = ""       # 自动弹窗的版本记录（进页自动查/后台轮询共用）
                save_config(self.cfg)
                _refresh()
                _sync_upd_help()
                self._dev_upd_start()       # 令牌清空了 → 后台定时器停掉
                msg_lab.set("已重置开发者选项。")

            def trust_sign():
                """把本程序 exe 的签名证书列入**当前用户**的受信任根（免管理员）。

                顺序：先读现在这份 exe 有没有签名 / 是否已列入 → 把危险点摆给用户确认 →
                确认后才动手。危险点是「信了这张证书 = 这台机器会信所有用它签的东西」，
                所以那一句必须原样说给用户听，不能替他跳过（`core/codesign.py` 开头有边界说明）。
                """
                info = codesign.read_cert()
                if info is None:
                    msg_lab.set("列入失败：本程序这份 exe 没有签名"
                                "（源码运行、或装的是未签名的构建）。")
                    return
                if info.get("trusted"):
                    msg_lab.set("这张签名证书已经在本机可信名单里了，不用再列一次。")
                    return
                if not messagebox.askyesno(
                        "将本软件签名列入本机可信签名",
                        "请确保该软件是从 GitHub 上直接下载的，\n"
                        "否则将签名列入可信名单是一件很危险的事。\n\n"
                        "要列入的证书：\n%s\n指纹：%s"
                        % (info.get("subject") or "（读不出主体）",
                           info.get("thumbprint") or "")):
                    return
                good, why = codesign.trust()
                if good:
                    self._codesign_info = dict(info, trusted=True)   # 下次建页不再显示这条
                    if btn_trust_user is not None:
                        btn_trust_user.pack_forget()                 # 已经信任了，这条没用了
                    msg_lab.set("已把本软件的签名列入本机可信名单（当前用户）。")
                else:
                    msg_lab.set("列入失败：%s" % why)

            def trust_sign_machine():
                """把本程序 exe 的签名证书列入**本机（所有用户）**信任根 —— 需要管理员。

                比用户级那一步重得多：影响这台机器上**所有** Windows 用户，而且要过 UAC。
                所以警告写得比用户级那条更狠（W 点名的"很严重的警告"），并把"接下来会弹
                管理员授权"提前说清楚 —— 用户在 UAC 上点取消就是放弃，不是失败。
                """
                info = codesign.read_cert()
                if info is None:
                    msg_lab.set("列入失败：本程序这份 exe 没有签名"
                                "（源码运行、或装的是未签名的构建）。")
                    return
                if info.get("trusted_machine"):
                    msg_lab.set("这张签名证书已经在本机（所有用户）可信名单里了。")
                    return
                if not messagebox.askyesno(
                        "列入系统级信任（所有用户）—— 请看清再决定",
                        "这一步会把证书写进【这台机器的所有用户】，不只是你当前这个用户。\n\n"
                        "后果：这台机器上任何人、任何程序，只要是用这张证书签名的，"
                        "Windows 都会直接信任它。\n\n"
                        "除非你确定这份软件是从 GitHub 上直接下载的、并且这台机器只由你使用，"
                        "否则不要继续。\n\n"
                        "接下来会弹系统管理员授权 —— 在那里取消就等于放弃。\n\n"
                        "要列入的证书：\n%s\n指纹：%s"
                        % (info.get("subject") or "（读不出主体）",
                           info.get("thumbprint") or "")):
                    return
                msg_lab.set("正在请求管理员授权…")
                ok, why = codesign.trust_machine()
                if ok:
                    msg_lab.set("已把本软件的签名列入本机（所有用户）可信名单。")
                else:
                    msg_lab.set("列入失败：%s" % why)

            btn_trust_user = None       # 用户级那个按钮（已信任时不建；点了成功就收起来）

            def _trusted_here():
                """当前用户是不是已经把这张签名列入信任名单了。

                读启动那次检测的结果（`App._codesign_info`）：拿不到就按"还没信任"处理 ——
                宁可能点（点进去也会被告知已信任），别把按钮藏得用户找不到。
                """
                info = getattr(self, "_codesign_info", None)
                return bool(info and info.get("trusted"))

            # 控件放在回调之后建：`command=名字` 是建控件那一刻就要绑定的（坑 115）
            i = r_dev["i"]
            r_dev["i"] += 1
            ttk.Label(t_dev, text="开发者模式已开启 · 仅本次运行有效 · "
                                  "重新进入：关于页版本号连点 5 次",
                      foreground="#5a6a7a", wraplength=720, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(0, 6))

            i = r_dev["i"]
            r_dev["i"] += 1
            # 按钮**竖直排列**（W 2026-10-05）：关闭在最上、重置在最下，两条都"始终"成立
            # —— 所以签名那条按需少建一个时，也不许挪动这两头的位置。
            brf = ttk.Frame(t_dev)
            brf.grid(row=i, column=0, columnspan=3, sticky="w", pady=(2, 6))
            ttk.Button(brf, text="关闭开发者模式",
                       command=close_dev).pack(side="top", anchor="w", pady=(0, 4))
            ttk.Button(brf, text="填写 GitHub 令牌",
                       command=fill_token).pack(side="top", anchor="w", pady=(0, 4))
            # 「本机已写入签名」就不再显示用户级那一条（W 2026-10-05）：判据用启动时那次
            # 检测的结果（`App._codesign_info`），不在这里再起一次 PowerShell 卡界面。
            if not _trusted_here():
                btn_trust_user = ttk.Button(brf, text="将本软件签名列入本机可信签名",
                                            command=trust_sign)
                btn_trust_user.pack(side="top", anchor="w", pady=(0, 4))
            ttk.Button(brf, text="将本软件签名列入系统级信任（所有用户）",
                       command=trust_sign_machine).pack(side="top", anchor="w", pady=(0, 4))
            ttk.Button(brf, text="重置开发者选项",
                       command=reset_dev).pack(side="top", anchor="w")

            for var, fg in ((tok_lab, "#5a6a7a"), (msg_lab, "#1a7f37")):
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
              · 「自动定向」= 扫软件所在文件夹（扫不到再扫当前模型目录）找出本机的引擎；
                「手动定向」= 用户自己指一个文件夹。两个引擎与模型文件同处一个目录也各认各的
                —— 判据是 `engine_install.find_exe`：**按 exe 名**找，不看目录名。
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
                 "llama.cpp（对话引擎）",
                 lambda: str((self.cfg.get("exe") or "").strip()),
                 lambda: str((self.cfg.get("exe") or "").strip()),
                 lambda: os.path.dirname(str((self.cfg.get("exe") or "").strip())) or APP_DIR,
                 "llama-server 所在目录",
                 "本地模型 → 服务参数",
                 "CUDA 版还要把 cudart 包里的运行库一起放进同一目录，否则启动时报缺 dll。"),
                ("sd",
                 "stable-diffusion.cpp（生图 / 生视频引擎）",
                 lambda: str((self.cfg.get("sd_dir") or "").strip()),
                 lambda: os.path.join(str((self.cfg.get("sd_dir") or "").strip()),
                                      engine_install.exe_name("sd")),
                 lambda: str((self.cfg.get("sd_dir") or "").strip()),
                 "sd.cpp 引擎目录",
                 "本地模型 → 生图（sd.cpp）",
                 "生图与生视频共用这一个可执行文件，只是参数不同；"
                 "同目录还要有一堆运行库 dll。"),
            ]

            def _state_of(cfg_of, exe_of):
                # 判"没指路"要看**用户填的那一项**，不能拿拼出来的文件路径判空：
                # sd 那项拼的是 os.path.join(sd_dir, 可执行文件名)，sd_dir 留空时得到
                # 相对路径 "sd-cli.exe"，非空但也不存在 —— 报"找不到文件"就冤枉人了
                # （和坑 42 里 `os.path.join("", "output")` 在 CWD 建目录是同一个根子）。
                # 四态里的"未就位 / 已就位"落在这里，"未就位"后面跟原因（W 2026-10-05）。
                if not str(cfg_of() or "").strip():
                    return "未就位：还没指路（「打开目标目录」会告诉你该放去哪儿）", "#b00020"
                exe = str(exe_of() or "")
                if os.path.isfile(exe):
                    return "已就位：" + exe, "#1a7f37"
                return "未就位：指了路但找不到该文件：" + exe, "#b00020"

            # ---- 这几个 helper **定义在循环之外**，全部显式收参。
            #
            # 为什么不能定义在循环里按默认参数绑（那正是下面注释里写的正确做法）：
            # 一旦循环内的函数**按名字**去调另一个循环内定义的函数，那个名字在运行时
            # 解析到的是**最后一次循环**的定义 —— 也就是两个引擎都去动 sd 那份。
            # 2026-10-04 真踩过：点 llama 的「检查更新」（当时叫「刷新版本」），写进状态行
            # 的是 sd 的版本行（坑 124 的同族，只是方向反过来）。
            def _say(sv, stl, text="", color="#5a5a5a"):
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
                    return "查询到新版本：还没查过可用版本（先点「检查更新」）。"
                build = engine_install.pick_build(b["builds"], fv.get())
                fl = [f for f in engine_install.flavors_from(build) if f["id"] == fv.get()]
                if not fl:
                    return "查询到新版本：%s（这个档位当前版本没有可用包）" % (
                        build.get("tag") or "？")
                mb = fl[0]["size"] / 1048576.0
                extra = "（含运行库）" if fl[0].get("extra") else ""
                return "查询到新版本：%s · 档位 %s · %.0f MB%s" % (
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
                """装之前拦一下"正在被用"的那份引擎：覆盖它等于把跑着的服务弄坏。"""
                cur = str(co() or "").strip()
                if not cur:
                    return ""
                try:
                    same = os.path.normcase(os.path.dirname(cur)) == os.path.normcase(
                        engine_install.default_dir(k))
                except Exception:
                    return ""
                if same and server_process_alive():
                    return "这份引擎正在运行，请先在主页面停止服务再安装。"
                return ""

            for key, title, cfg_of, exe_of, dir_of, what, setting, note in engines:
                i = r10["i"]
                r10["i"] += 1
                th = tk.Frame(t10, background=widgets.default_bg())
                th.grid(row=i, column=0, columnspan=3, sticky="w", pady=(10, 2))
                tk.Label(th, text=title, font=("Microsoft YaHei UI", 10, "bold"),
                         background=widgets.default_bg(), anchor="w").pack(side="left")
                widgets.HelpDot(th, "「更新引擎」会把压缩包整包解压（不要只放那一个 exe，"
                                     "同目录的运行库都要），落到程序目录下的 engines 里。\n"
                                     + note +
                                     "\n引擎本来就装在这台机器上、只是换了位置的，"
                                     "用「自动定向」按 exe 名找出来、或「手动定向」自己指，"
                                     "都不用重装。").pack(side="left", padx=(6, 0))

                # 状态行：四态文案（未就位 / 已就位 / 正在查 / 查询到新版本）**都写在这一行**，
                # 不另设提示行（W 2026-10-05）。变量挂控件保活（坑 145 ①）。
                st_var = tk.StringVar(value="")
                st = ttk.Label(t10, textvariable=st_var, wraplength=600, justify="left",
                               foreground="#5a5a5a", font=("Microsoft YaHei UI", 9))
                st.grid(row=r10["i"], column=1, columnspan=2, sticky="w", pady=(0, 2))
                r10["i"] += 1

                # 按钮分两行（W 2026-10-05 第二轮）：上一行是四个"其他按钮"（复制下载链接 /
                # 自动定向 / 手动定向 / 打开目标目录），下一行**只放**合并按钮「检查更新」——
                # 它右边挂档位下拉，**点过「检查更新」才显示**。`ScrollPage` 只竖滚不横滚
                # （坑 106），但这四个按钮按字符宽实测仍在可视区内（自检里有一条右界断言）。
                bf = ttk.Frame(t10)
                bf.grid(row=r10["i"], column=1, columnspan=2, sticky="w", pady=(2, 2))
                r10["i"] += 1
                vf = ttk.Frame(t10)
                vf.grid(row=r10["i"], column=1, columnspan=2, sticky="w", pady=(0, 6))
                r10["i"] += 1

                flavor_var = tk.StringVar(value="")
                btn_act = ttk.Button(vf, text="检查更新", width=12)
                btn_act.pack(side="left")
                # 档位下拉先建不 pack：**点过「检查更新」才 pack 出来**（W 2026-10-05）。
                # 构造时就绑 textvariable（坑 112：只 set 变量而没绑，界面是空的而数据是对的）。
                flavor_lbl = ttk.Label(vf, text="档位", foreground="#5a5a5a",
                                       font=("Microsoft YaHei UI", 9))
                flavor_cb = ttk.Combobox(vf, textvariable=flavor_var, state="readonly",
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
                #       installing（正在下载安装）。按钮文字与状态行都由 `_render` 按它刷。
                box = {"builds": [], "busy": False, "stop": None, "mode": "idle",
                       "checked": False}

                def _render(b=box, co=cfg_of, so=exe_of, sv=st_var, stl=st, btn=btn_act,
                            fv=flavor_var, k=key):
                    """按当前 mode 刷按钮文字与状态行（四态文案的唯一出口）。"""
                    mode = b.get("mode") or "idle"
                    if mode == "checking":
                        _say(sv, stl, "正在查…", "#b58900")
                        btn.configure(text="检查更新", state="disabled")
                        return
                    if mode == "ready":
                        _say(sv, stl, _flavor_line(b, k, fv))
                        btn.configure(text="更新引擎", state="normal")
                        return
                    if mode == "installing":
                        btn.configure(text="取消安装", state="normal")
                        return
                    text, color = _state_of(co, so)
                    if b.get("checked") and text.startswith("已就位"):
                        text += "（已是最新）"      # 查过且没有更新版本
                    _say(sv, stl, text, color)
                    btn.configure(text="检查更新", state="normal")

                def _apply_dir(kk, folder):
                    """把选中的文件夹写回该引擎的指路项，并把已建的输入框一起回填。

                    形状归一（llama 存 exe 路径 / sd 存目录）在 `engine_install.set_dir` 一处
                    —— **首次打开的自动扫描走的是同一个函数**，不在这里再写一遍（坑 128）。
                    回填是为了免得底部「保存」把旧值又写回去（那个页可能已经建过输入框）。
                    """
                    engine_install.set_dir(self.cfg, kk, folder)
                    save_config(self.cfg)
                    key2 = engine_install.path_key(kk)
                    var = v.get(key2) if isinstance(v, dict) else None
                    if var is not None:
                        try:
                            var.set(self.cfg[key2])
                        except Exception:
                            pass
                    # 引擎刚就位往往就是"第一个可用模型"的时刻（模型早就下好了只差引擎）
                    # ⇒ 立刻切过去，不等状态轮询（坑 150）
                    try:
                        self._maybe_adopt_first_model(force=True)
                    except Exception:
                        pass

                def do_check(_e=None, b=box, k=key, cb=flavor_cb, fv=flavor_var,
                             sv=st_var, stl=st, rf=_render, sf=_show_flavor):
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
                             rf=rf, sf=sf):
                        bb["busy"] = False
                        if not builds:
                            bb["mode"] = "idle"
                            rf()
                            _say(sv, stl, why or "没查到带本平台安装包的版本。", "#b00020")
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
                        else:
                            bb["mode"] = "idle"
                            bb["checked"] = True   # 查过且没有更新版本 → 状态行加"（已是最新）"
                        rf()

                    def work():
                        try:
                            builds, why = engine_install.list_builds(k, token=tok)
                        except Exception as e:       # 兜底：异常不许穿回 UI 线程（坑 54）
                            builds, why = [], "查可用版本时出错：%s" % e
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
                        _say(sv, stl, "安装包链接已复制，贴到浏览器地址栏就能开始下载。"
                             if p and p["files"] else "发布页链接已复制。")
                    except Exception as e:
                        messagebox.showwarning("复制链接", "复制失败（%s）。" % e)

                def auto_dir(_e=None, k=key, sv=st_var, stl=st, b=box, rf=_render,
                             ad=_apply_dir):
                    """「自动定向」：扫软件所在文件夹（再退模型目录），按 **exe 名**找出本引擎。

                    判据与落点都在 `engine_install.auto_locate` / `set_dir`（**首次打开的
                    自动扫描走同一处**）。两个引擎各调一次（同处一个目录也不会张冠李戴）；
                    找不到就把原因写进状态行，不弹窗堆噪音 —— 状态行本来就是四态文案的家。
                    """
                    if b["busy"]:
                        return
                    found = engine_install.auto_locate(k, self.cfg, app_dir=APP_DIR)
                    if not found:
                        _say(sv, stl, "未就位：没在软件目录（及模型目录）下找到 %s。"
                             % engine_install.exe_name(k), "#b00020")
                        return
                    ad(k, os.path.dirname(found))
                    b["mode"] = "idle"
                    b["checked"] = False
                    rf()

                def manual_dir(_e=None, k=key, b=box, rf=_render, ad=_apply_dir):
                    """「手动定向」：用户自己指一个文件夹（该引擎所在目录）。"""
                    folder = filedialog.askdirectory(
                        title="选择 %s 所在文件夹" % engine_install.exe_name(k),
                        parent=t10.winfo_toplevel())
                    if not folder:
                        return
                    exe = os.path.join(folder, engine_install.exe_name(k))
                    if not os.path.isfile(exe) and not messagebox.askyesno(
                            "手动定向",
                            "这个文件夹里没有 %s。\n\n仍要指到这里吗？"
                            % engine_install.exe_name(k)):
                        return
                    ad(k, folder)
                    b["mode"] = "idle"
                    b["checked"] = False
                    rf()

                def do_install(_e=None, b=box, k=key, sv=st_var, stl=st, fv=flavor_var,
                               co=cfg_of, btn=btn_act, rf=_render, ad=_apply_dir,
                               setting=setting):
                    """点「更新引擎」：选档 → 下载校验 → 解压换目录；**装完指向新那份**。"""
                    p = _plan_of(b, k, fv)
                    if not p or not p["files"]:
                        # 同一句也写进状态行：无头自检看不见 messagebox，
                        # 而"没查过就点更新"这条规矩必须能被断言（也更像这页的口径）。
                        _say(sv, stl, "还没查过可用版本 —— 先点「检查更新」，再选一个档位。",
                             "#b00020")
                        return
                    why = _guard(b, k, co)
                    if why and not messagebox.askyesno("更新引擎", why + "\n\n仍要继续吗？"):
                        return
                    dest = p["dir"]
                    tok = secrets.get_github_token()
                    b["stop"] = threading.Event()
                    b["busy"] = True
                    b["mode"] = "installing"
                    btn.configure(text="取消安装", state="normal")
                    _say(sv, stl, "准备下载 %s（%.0f MB）…"
                         % (p["files"][0]["name"], p["total"] / 1048576.0))

                    def emit(text):
                        self._ui_q.put(lambda: _say(sv, stl, text))

                    def finish(ok, why_, exe, p=p, b=b, sv=sv, stl=stl, k=k, btn=btn,
                               rf=rf, ad=ad, setting=setting):
                        b["busy"] = False
                        if not ok:
                            b["mode"] = "idle"
                            rf()
                            _say(sv, stl, why_, "#b00020")
                            messagebox.showwarning(
                                "更新引擎",
                                "%s\n\n也可以点「复制下载链接」自己下。" % why_)
                            return
                        # 记下装上的版本（下次「检查更新」据此判「查询到新版本 / 已是最新」）
                        # + **把路径指到刚装的那份**（W 2026-10-05 明确要求，见本函数口径）
                        rec = dict(self.cfg.get("engine_installed") or {})
                        rec[k] = p.get("tag") or ""
                        self.cfg["engine_installed"] = rec
                        ad(k, p.get("dir") or engine_install.default_dir(k))
                        b["mode"] = "idle"
                        b["checked"] = False
                        rf()
                        messagebox.showinfo(
                            "更新引擎",
                            "已装好并指过去：\n%s\n\n回 设置 → %s 就能用它。" % (exe, setting))

                    def work():
                        tmp = engine_install.staging_dir()
                        got, why2, exe3, files = True, "", "", []
                        try:
                            for f in p["files"]:
                                emit("正在下载 %s（%.0f MB）…"
                                     % (f["name"], f["size"] / 1048576.0))
                                dest_f = os.path.join(tmp, f["name"])
                                ok, why2 = engine_install.download(
                                    f["url"], dest_f, emit=emit, stop_flag=b["stop"],
                                    digest=f.get("digest") or "")
                                if not ok:
                                    got = False
                                    break
                                f2 = dict(f)
                                f2["path"] = dest_f
                                files.append(f2)
                            if got:
                                ok3, why3, exe3 = engine_install.install(
                                    k, files, dest, emit=emit, stop_flag=b["stop"],
                                    guard=lambda: "已取消" if b["stop"].is_set() else "")
                                got, why2 = ok3, why3
                        except Exception as e:
                            got, why2 = False, "安装时出错：%s" % e
                        finally:
                            engine_install.cleanup(tmp)
                        self._ui_q.put(lambda: finish(got, why2, exe3))

                    def work_safe():
                        try:
                            work()
                        except Exception as e:      # 兜底：异常不许穿回 UI 线程（坑 54）
                            self._ui_q.put(
                                lambda: finish(False, "安装时出错：%s" % e, ""))

                    threading.Thread(target=work_safe, daemon=True).start()

                def do_click(_e=None, b=box, on_check=do_check, on_install=do_install):
                    """「检查更新 / 更新引擎 / 取消安装」共用一个按钮：按当前 mode 分派。

                    为什么要合并（W 2026-10-05）：三个动作是同一件事的三个阶段，拆成三个按钮
                    既占地方、又要用户自己判断该点哪个。
                    """
                    mode = b.get("mode") or "idle"
                    if mode == "ready":
                        on_install()
                    elif mode == "installing":
                        if b["stop"] is not None:
                            b["stop"].set()
                    else:
                        on_check()

                # 顺序按 W 2026-10-05：复制下载链接 → 自动定向 → 手动定向 → 打开目标目录
                ttk.Button(bf, text="复制下载链接", width=13,
                           command=copy_link).pack(side="left", padx=(0, 6))
                ttk.Button(bf, text="自动定向", width=10,
                           command=auto_dir).pack(side="left", padx=(0, 6))
                ttk.Button(bf, text="手动定向", width=10,
                           command=manual_dir).pack(side="left", padx=(0, 6))
                ttk.Button(bf, text="打开目标目录", width=13,
                           command=lambda d=dir_of, w=what, s=setting: _open_outdir(
                               d(), w, s)).pack(side="left")
                btn_act.configure(command=do_click)
                # 包一层把当轮那几个对象绑进去（`_on_pick` 定义在循环之外，只收显式参数）
                flavor_cb.bind("<<ComboboxSelected>>",
                               lambda e=None, b=box, k=key, fv=flavor_var,
                               sv=st_var, stl=st: _on_pick(b, k, fv, sv, stl))
                _render()

        # ---- 区块 8：模型文件管理（左栏「模型文件与引擎」那一项指到这里）----
        @section("files", "files")
        def _t5(t5, r5):
            def _files_head(n_models):
                # 只留"有多少个模型"：原来把层数 / context / mmproj / 未进菜单四个计数
                # 也拼在这一行里（5 段用 ｜ 隔开），760px 的换行宽度根本兜不住，
                # 必然折成两行、第一行尾巴还参差不齐（W 2026-10-04：删掉这些冗余计数）。
                # 「进这一页会自动补全…」那半句也删了（W 2026-10-05）：自动化不需要向
                # 用户说明，底下那些按钮自己会说话。
                return "当前：模型 %d 个（含可看图）" % n_models

            head_lbl = ttk.Label(t5, text="当前：正在读取模型目录…",
                                 foreground="#555555", wraplength=760, justify="left",
                                 font=("Microsoft YaHei UI", 9))
            head_lbl.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            r5["i"] = 1

            def _fill_files_head(lbl=head_lbl):
                # 这一行要扫一遍盘（每个 .gguf 都要读文件头判类型）：延到开页之后算（_idle_fill）
                _d2, _chat2, _i2 = scan_models(self.cfg)
                lbl.configure(text=_files_head(len(_chat2)))

            _idle_fill(t5, _fill_files_head)

            frm = ttk.Frame(t5)
            ttk.Button(frm, text="管理本地模型…", width=18,
                       command=self.open_local_models).pack(side="left")
            row(t5, r5, "菜单与配套件", frm,
                "三组模型与各自的配套件（VAE / 文本编码器 / CLIP / T5…）摊开在一页里，"
                "可逐个勾选要不要出现在顶部模型菜单 —— 生图 / 生视频附带的"
                "「能聊天的 .gguf 编码器」常需要收起来。只改显示，不动文件。")

            fr = ttk.Frame(t5)
            ttk.Button(fr, text="补全缺失项", width=18,
                       command=lambda: self._manual_scan(False)).pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="全部重新计算", width=18,
                       command=lambda: self._manual_scan(True)).pack(side="left")
            row(t5, r5, "匹配参数", fr,
                "补全 = 只为缺记录的模型计算（不覆盖手动调整过的值）；"
                "全部重新计算 = 清空全部自动记录后重算（含覆盖手调值，会二次确认）。"
                "进这一页会自动补全一次（10 分钟内不重复）。")

            fr2 = ttk.Frame(t5)
            ttk.Button(fr2, text="整理模型文件夹", width=18,
                       command=self._open_tidy_dialog).pack(side="left", padx=(0, 6))
            ttk.Button(fr2, text="手动定向模型", width=18,
                       command=self._manual_point_model).pack(side="left")
            row(t5, r5, "文件整理", fr2,
                "把模型目录顶层散落的模型与其配对 mmproj 归入各自子文件夹"
                "（先预览、后执行；只移动不删除；名称无法判断归属的保持原位）。"
                "「手动定向模型」把别处的文件夹或单个 .gguf 加进来源 —— 只登记，不动文件。")

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
                tk.Label(head, text=it["title"], background=bg, foreground="#111111",
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
                ttk.Label(body, text="（这个区块还没接上构建函数）",
                          foreground="#c01c28").grid(row=0, column=0, sticky="w")
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
            """定位过去之后把区块标题闪一下底色：不闪的话用户不知道页面滚到了哪儿。"""
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
                lbl[0].configure(background="#fff3c4")
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

        def _global_save(restart=False):
            """底部「保存」= 通用参数 + **当前这一页里已建区块各自的保存逻辑**，都成功才关窗。

            原来只跑当前标签页那一个钩子；现在一页里挂着多个区块（云端页就有服务商 /
            文本 / 生图生视频三段），所以把**这一页已经建起来的**区块钩子按顺序都跑一遍，
            任一个不通过就不关窗 —— 保住"底部保存不会把自己刚填的东西丢掉"这条既有语义。
            """
            ok, why = self._apply_settings(v)
            if not ok:
                messagebox.showwarning("还没保存", why)
                return                          # 一个键都没写，窗也不关：改好再来
            for it in (page_items.get(state["page"]) or []):
                body = bodies.get((it["page"], it["section"]))
                if body is None or not getattr(body, "_done", False):
                    continue
                hook = save_hooks.get(body)
                if hook is None:
                    continue
                try:
                    ok, msg = hook() or (True, "")
                except Exception as e:
                    ok, msg = False, "这一区块没能保存：%s" % e
                if not ok:
                    messagebox.showwarning(
                        "还没保存", msg or "这一页有内容没通过检查，请改好再保存。")
                    return
            # 保存往往就是"第一次配好"的时刻（指了引擎 / 指了模型目录 / 勾了云端模型 /
            # 填了密钥）⇒ 立刻问一次"第一个能用的模型有了没有"，别等下一轮轮询（坑 150）
            try:
                self._maybe_adopt_first_model(force=True)
            except Exception:
                pass
            win.destroy()
            if restart:
                self.restart_server()

        # 「显示全部参数」：常驻底部按钮条（固定外框，不随滚动消失）。勾上 = 把**所有**
        # 「高级参数」折叠区一起展开（含还没建起来的页 —— 那些页建的时候会问会话状态）。
        # 状态存在 App._settings_ui（会话内记住：关掉设置窗再开还在，退程序才清），
        # **不写进配置文件** —— 它纯粹是界面偏好，不是功能设置。
        show_var = tk.BooleanVar(value=bool(sess.get("show_all")))

        def _apply_show_all():
            sess["show_all"] = bool(show_var.get())
            for _n in list(fold_heads):
                _render_fold(_n)

        ttk.Checkbutton(bar, text="显示全部参数", variable=show_var,
                        command=_apply_show_all).pack(side="left", padx=(2, 0))
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="保存并重启服务",
                   command=lambda: _global_save(restart=True)).pack(side="right", padx=4)
        ttk.Button(bar, text="保存", command=_global_save).pack(side="right")

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
            if k in v:
                try:
                    c[k] = int(str(v[k].get()).strip())
                except Exception:
                    pass
        # context / ngl 按模型记忆（主页面启动用）：**只有这一页真的建了、用户真的填过**
        # 才写记录 —— 判据必须看 v（这一页的输入），不能看 c（配置里有没有这个键，恒为真）。
        # 原来写成 `if local and "ctx" in c:`，于是随便在哪一页点「保存」都会把当前模型的
        # 层数 / context 记录改写成全局兜底值，静默抹掉自动推算的结果（坑 130）
        if local and "ctx" in v:
            try:
                c.setdefault("model_ctx", {})[
                    os.path.basename(c.get("model", ""))] = int(c["ctx"])
            except Exception:
                pass
        # agent 场景 context 按模型记忆
        if local and "agent_ctx" in v:
            try:
                c.setdefault("model_ctx_api", {})[
                    os.path.basename(c.get("model", ""))] = int(
                        str(v["agent_ctx"].get()).strip())
            except Exception:
                pass
        for k in FLOAT_KEYS:
            if k in v:
                try:
                    c[k] = float(str(v[k].get()).strip())
                except Exception:
                    pass
        for k in STR_KEYS:
            if k in v:
                c[k] = str(v[k].get()).strip()
        # 「model」不在 STR_KEYS 里，这里单独处理：留空 = 不动当前选择（否则选着云端模型时
        # 会被抹成切回本地）；非空 = 明确切到该本地模型，连带复位 model_provider（坑 146）。
        if "model" in v:
            _m = str(v["model"].get()).strip()
            if _m:
                c["model"] = _m
                c["model_provider"] = providers.LOCAL
                c["model_auto_picked"] = True   # 用户自己选的 ⇒ 别再自动接管（坑 150）
                _k = _kind_of_local_model(c, _m)
                if _k:
                    c["model_kind"] = _k
            elif not providers.is_cloud(c):
                c["model"] = ""          # 本地选中且被清空 = 用户不要这个模型了
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
        # 代理开关：**只有这台机器真能转发时才回写**。没有本地文本模型时那个复选框是锁着的，
        # 它的值是"偏好 and usable"= False —— 照原样回写就会把用户存过的偏好静默改成关，
        # 等他后来装好模型也不会自己变回来（§5.6 的口径：锁住的那项默认关，但不吃配置）
        if "proxy_enabled" in v and self._proxy_usable:
            c["proxy_enabled"] = bool(v["proxy_enabled"].get())
        if "show_usage" in v:
            c["show_usage"] = bool(v["show_usage"].get())
        if "cloud_file_model_decides" in v:
            c["cloud_file_model_decides"] = bool(v["cloud_file_model_decides"].get())
        # ngl 按模型记忆：设置页改的是"当前模型"的层数（云端模型没有层数概念）。
        # 判据同 ctx：看 v，不看 c（坑 130）
        if local and "ngl" in v:
            c.setdefault("model_ngl", {})[os.path.basename(c["model"])] = c["ngl"]
        c["cfg_version"] = CFG_VERSION
        save_config(c)
        self._update_model_label()
        return True, ""
