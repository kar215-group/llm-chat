# -*- coding: utf-8 -*-
"""llm_console.ui.app — 界面主窗口：App 外壳（布局、主轮询、状态灯、退出）与程序入口 main()"""

import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

from ..core.config import (load_config, DEFAULT_CONFIG, APP_DIR, APP_VERSION,
                           CONFIG_PATH)
from ..core import (capability, cloudjobs, codesign, config, crashlog, diagnose,
                    engine_install, providers, secrets, selfupdate, throttle, updater)
from ..core.models import (auto_locate_models_dir, dir_has_any_gguf, display_name,
                          extra_sources, first_usable, has_local_chat,
                          selected_usable)
from ..core.server import _query_serving_model, server_process_alive, server_state, stop_server
from ..connection import cloud_media
from ..connection.proxy import ProxyServer
from .dialogs import ExitDialog
from . import guide, theme, widgets
from .chat import ChatMixin
from .image_gen import ImageGenMixin
from .video_gen import VideoGenMixin
from .service import ServiceMixin
from .models_ui import ModelsMixin, NO_MODEL_LABEL, model_missing
from .settings import SettingsMixin
from .subwindows import SubWindowMixin

# 输出栏那条「[环境] 三条路都还没通」提示的文字标签：首个可用模型一出现，
# `_dismiss_env_hint` 按它把整块（连嵌进来的两个按钮）删掉（W 2026-10-08）。
ENV_HINT_TAG = "env_hint"


class App(ChatMixin, ImageGenMixin, VideoGenMixin, ServiceMixin, ModelsMixin, SettingsMixin,
          SubWindowMixin):
    """主窗口外壳：布局、主轮询、状态灯、退出；其余职责分散在各 Mixin。"""

    def __init__(self, root, cfg, first_run=False):
        self.root = root
        self.cfg = cfg
        # 首跑判定由 main() 传进来（它得在 load_config 之前才知道配置文件原本存不存在）
        self._first_run = bool(first_run)
        # 遮罩引导：同一时刻只允许一层（重复打开会把遮罩叠遮罩，鼠标点哪儿都不通）
        self._guide = None
        # 状态线程用来去重"配置写不进去"这条提醒（同一份错误只说一次）
        self._write_err_seen = ""
        self.history = []            # 多轮对话（不含 system）
        # 对话记录（core/chatlog）：会话号在第一条消息时才生成，
        # 免得"打开又关掉"留一堆空文件；_sess_path 是它落盘后的路径，
        # _announced_path = "这份已经在界面上说过一次了"，_log_key = 内容签名（防重复写盘）
        self._sess_id = ""
        self._sess_path = ""
        self._sess_saved = False
        self._announced_path = ""
        self._log_key = None
        self._busy = False           # 正在生成回复
        self._svc_busy = False       # 服务启动/停止操作进行中（防重入）
        self._closing = False
        self._server_alive_flag = False
        self._server_ready_flag = False
        self._stop_flag = None
        self._settings_win = None
        self._settings_nav = None       # 设置窗口的左栏（输出栏的按钮要 jump 到某个叶子）
        self._settings_sp = None        # 设置窗口的滚动内容区（页面栈；"当前页"判据走它）
        # 设置页的会话内界面状态：{"fold": {区块名: 展开}}
        # 挂在 App 上（不是窗口上）→ 关掉设置窗再开，展开状态还在；程序一退就没了。
        # （原来的 "show_all" 随底部「显示全部参数」一起移除，2026-10-07 W 定：
        #   那个位置换成了「用户模式」切换按钮）
        self._settings_ui = {"fold": {}}
        # 遮罩引导开着时为 True：顶栏的「启动服务」等按钮平时按模型类型显隐（生图 /
        # 生视频 / 云端时不出现），但引导的步骤目标指着它们 —— 引导期间一律强制可见，
        # 关掉再按当前模型恢复（不然 SpotlightGuide 量一个没映射的控件，洞和面板全错位，
        # 坑 162 ③）。
        self._guide_active = False
        # 开机那条「[环境] 三条路都还没通」提示有没有插进输出栏（插过才谈得上收掉）。
        # 记状态是为了让 3 秒一轮的探活不必每次去做 tag_ranges 查询；首个可用模型
        # 一出现就由 `_dismiss_env_hint` 把它整块删掉（W 2026-10-08）。
        self._env_hint_ins = False
        # 顶栏上一次的显隐状态（show_start/show_stop/show_status 三元组）：
        # 状态队列每 3 秒叫一次 `_render_status`，而"整排重放"要 6~12 ms —— 状态没变
        # 就直接跳过（W 2026-10-08，见 `_layout_topbar`）。
        self._topbar_state = None
        # 本轮对话是不是云端链路：「展示思考过程」本地与云端是两个独立开关
        # （cfg["show_reasoning"] / cfg["cloud_show_reasoning"]），_poll 渲染时按它分流。
        # 在 _do_send 开线程前置位；忙碌守卫保证一轮中间不会换链路。
        self._turn_cloud = False
        # 「检查更新」的会话内状态，同样挂 App（关掉设置窗再开，冷却与上次结果都还在）：
        #   at     = 上次真发起检查的墙钟时刻（time.time()；进页自动查的 10 分钟冷却看它）
        #   busy   = 有请求正在飞（互斥，别叠第二个请求）
        #   last   = 上次结果 `(状态, 详情)` —— 冷却期进页拿它回显，不会看起来像"刚查过"
        #   cache  = core.updater.Cache()，上次响应的 ETag 与结果（304 就复用，省限流额度）
        #   render = 当前关于页那行状态的渲染函数（窗口关掉后指向死控件，靠 winfo_exists 兜）
        self._upd_check = {"at": 0.0, "busy": False, "last": None,
                           "cache": updater.Cache(), "render": None, "help": None}
        # 「进 模型文件与引擎 页自动补全缺失项」的节流闸（W 2026-10-05）：与「检查更新」的
        # 进页冷却共用 core.throttle 那套判据（同一处），只是冷却时长固定 10 分钟。
        # 挂 App（不是窗口上）→ 关掉设置窗再开仍在，与 _upd_check 同一条口径。
        self._model_scan = throttle.AutoThrottle(throttle.AUTO_SCAN_COOLDOWN)
        # 「第一个可用的模型配好了就切过去」那条兜底轮询的冷却（坑150）。真正让主页面
        # "立即响应"的是事件钩子（设置页保存 / 引擎装完 / 手动定向模型，都是 force=True），
        # 这一处只是安全网：用户在程序外面放了模型、或密钥是别的进程写进去的。
        self._auto_pick = throttle.AutoThrottle(throttle.AUTO_PICK_COOLDOWN)
        # 开发者模式（W 2026-10-04）：入口是"关于页那行版本号连点 5 次"，见 ui/settings.py。
        # **只在本次运行内有效**（W 点名：重启后回到普通界面）—— 所以它在这儿、不进配置文件；
        # 而开发者选项里填的**东西**（GitHub 令牌等）是持久的，关掉这个模式也不清。
        self._dev_mode = False
        self._dev_tap = {"count": 0, "at": 0.0}    # 连点计数：上一次点击的时刻 + 已连了几下
        self._dev_upd = {"timer": None}            # 后台查更新的 after id（有令牌才不是 None）
        # 签名信任（W 2026-10-05）：启动问一次；`info` 是那次的检测结果，开发者选项那页
        # 靠它决定"用户级那个按钮还要不要显示"（已经信任就不显示）。
        self._codesign = {"started": False}
        self._codesign_info = None
        # 性能分级（2026-10-06 W）：启动前"必闪退"警告每模型只弹一次的登记（本进程内）
        self._perf_warned = set()
        self._diag_win = None         # 「诊断」次级页面（设置 → 关于与诊断 的按钮开的，放路径与一键诊断）
        self._upd_win = None          # 「发现新版本」次级窗口（自替换更新，2026-10-05；单实例）
        self._alias_tried = set()    # （备用）已尝试向模型请求别名的模型
        self._serving_model = None   # 当前服务实际加载的模型文件名（None=未知/未运行）
        self._pending_text = None    # 换载期间暂存的消息，就绪后自动发送
        self._proc = None            # 本次操作 Popen 出的服务进程（关闭时可中止）
        self._img_busy = False       # 聊天流生图进行中
        self._img_proc = None        # 生图子进程（聊天流模式）
        self._img_q = queue.Queue()  # 生图输出队列
        self._img_seq = 0
        self._img_photos = {}        # 内嵌预览图引用（防 GC）
        self._img_out = None
        self._t0 = None              # 当前生图开始时间戳
        self._img_gen = 0            # 生图任务世代（取消/重发时递增，旧回调失效）
        # 生视频（与生图同引擎不同链路，字段命名一律 _vid_ 前缀，避免与生图共用状态）
        self._vid_busy = False       # 聊天流生视频进行中
        self._vid_proc = None        # 生视频子进程
        self._vid_q = queue.Queue()  # 生视频输出队列
        self._vid_gen = 0            # 生视频任务世代
        self._vid_mark = None        # 进度行 mark
        self._vid_out = None         # 本次视频输出路径
        self._vid_t0 = None          # 本次生视频开始时间戳
        self._vid_saw_decode = False # 是否已进入解码阶段（进度行文案切换）
        self._vid_tail = []          # 引擎输出的滚动末尾（失败时回显原因，否则只剩一个退出码）
        self._vid_phase = "load"     # load / sample：区分权重加载与采样后的进度条，避免文案乱报
        self._vid_log = ""           # 本次任务的引擎日志路径（产物同名 + .log）
        # ---- 云端生图 / 生视频（二三期）----
        # 队列与进度行完全复用上面两套，差别只在**进度来自轮询而不是 stdout**，
        # 以及"取消"分两种：PENDING 能真取消，RUNNING 只是不再等它（任务照跑、计费照算）。
        self._cloud_img = False      # 本次生图走的是云端
        self._cloud_vid = False      # 本次生视频走的是云端
        self._cloud_tid = ""         # 当前云端任务 id（异步任务取消 / 取回要用）
        self._cloud_pid = ""         # 当前云端任务属于哪个 provider（取消时要按它取密钥）
        self._img_tail = []          # 生图侧的输出末尾：以前只有视频链路有，失败就只剩一个退出码（坑 40）
        self._img_log = ""           # 本次云端生图的请求/回包日志路径
        self._img_extra_paths = []   # 一次回多张时，除主图之外的落地路径
        self._q = queue.Queue()      # 流式输出队列
        self._sq = queue.Queue()     # 状态队列
        self._ui_q = queue.Queue()   # "请在主线程执行"的回调队列（子线程调 root.after 会炸）
        # 本轮输出的分段状态（_do_send 里会重置；此处先初始化，避免
        # 任何异常路径下 _poll 收到残留消息时报 AttributeError）
        self._cur_reasoning = []
        self._cur_content = []
        self._reasoning_started = False
        self._content_started = False
        self.api_hint_var = None     # 本地模型 API 页的"已复制"提示
        self._last_usage = None      # 本轮云端/本地返回的 token 用量（收尾时显示）

        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self._build_topbar()
        self._build_chat()
        self._build_inputbar()
        # Tk 找的是**控件身上的** report_callback_exception（`self._root().…`），而 App 不是
        # tk.Tk 的子类 —— 不挂这一行，方法定义了也永远不会被叫到：Tk 回调里炸的东西
        # 全都不落错误日志，而那是 `--windowed` exe 用户唯一的痕迹（A/B 实测过：
        # 不挂时日志空白，挂上才有那段 traceback）
        root.report_callback_exception = self.report_callback_exception

        # OpenAI 兼容中转：供 agent 应用接入（127.0.0.1:proxy_port）。
        # 两道闸都过才随程序自动起（2026-10-07 W 定）：
        #   proxy_enabled   = 功能启用（默认关，只在高级用户模式的「本地模型 API」页手动开）
        #   proxy_autostart = 「自启动」开关（启用后还要不要开机自动带起来）
        # 没有本地可转发的文本模型也不起 —— 代理空跑着也只是让 agent 连上来拿不到回答。
        self.proxy = ProxyServer(cfg, note_fn=lambda m: self._sq.put(("note", m)))
        self._proxy_usable = has_local_chat(cfg)
        _proxy_want = bool(cfg.get("proxy_enabled")) and bool(cfg.get("proxy_autostart", True))
        if _proxy_want and self._proxy_usable:
            self.proxy.start()
        elif _proxy_want:
            # 不起代理必须说一声：否则 agent 那边是"连不上"，用户在这儿什么线索都没有
            self._sq.put(("note", "API 代理没有启动：这台机器上还没有可转发的本地文本模型。"
                                  "备好引擎与模型后在 设置 → 本地模型 API 里启用。"))

        threading.Thread(target=self._status_loop, daemon=True).start()
        # 不再在启动时自动补全模型参数（W 2026-10-05）：改由「进 模型文件与引擎 页」按
        # 10 分钟冷却触发一次（见 ui/models_ui._auto_scan_models + settings 的 files 进页钩子）。
        root.after(80, self._poll)
        self._dev_upd_start()        # 上次运行填过 GitHub 令牌 → 后台查更新这就接上
        self.input.focus_set()
        self._offer_cloud_recovery()
        # 「上次异常退出」弹窗已按 W 2026-10-08 裁定砍除（普通用户用不上、日志
        # 残留时反复弹窗纯打扰）：崩溃仍照常落盘（crashlog），排查入口在
        # 设置 → 关于与诊断 →「诊断」的「打开错误日志」。
        # 首次打开（首次安装 / 升级后首次打开）先扫一遍引擎与模型，再考虑催办与引导 ——
        # 顺序有讲究：引导第一屏的缺件清单与输出栏那句催办都读"引擎在不在"，
        # 扫描（尤其是自动定向）跑在它们前面，用户已经配好的那份才会被判成"已配置"。
        self._first_open_scan()
        self._offer_engine_hint()
        # 签名信任：**第一次问一次**（W 2026-10-05）。首跑先让引导说完，不叠浮层。
        if not self._first_run:
            self._codesign_offer_start()

    def report_callback_exception(self, exc, val, tb):
        """Tk 回调里抛出的异常：先落进崩溃日志，再照原样打一份。

        Tk 自己只会 print，而 `--windowed` 的 exe 没有控制台（stdout/stderr 都是 DEVNULL），
        那些异常等于没发生过 —— 坑 93 那次"用户点什么都没反应、界面上零痕迹"就是这么来的。
        """
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            crashlog.note(text)
        except Exception:
            pass
        try:
            sys.stderr.write(text + "\n")
        except Exception:
            pass

    # ---- 签名信任：启动时问一次（W 2026-10-05）----
    def _codesign_offer_start(self):
        """检测本机签名的信任状态，并在**还没问过**时问一次要不要列入。

        为什么是"问一次"而不是自动加入：本程序是**分发**给别人的，它没法判断自己是不是
        被重打包的副本 —— 静默写入等于替用户给"任何一份自称 LLM Chat 的东西"发一张长期
        通行证（边界见 `05` §4.1 与 `99` F5）。所以把成本压到"一次点击"，判断权留在用户手里。

        检测走后台线程（PowerShell 起一次要几百毫秒），结果回主线程走 `_ui_q`（坑 54）。
        两条进来：非首跑在启动流程里直接调；**首跑是等遮罩引导收尾后才调**（W 2026-10-05，
        两个浮层叠在一起很糟）。`started` 保证只跑一次。

        结果存进 `self._codesign_info`：开发者选项那页靠它决定"用户级那个按钮还显示不显示"。
        """
        if codesign.self_exe() is None:
            return                       # 源码运行：没有签名可谈
        if self._codesign.get("started"):
            return
        self._codesign["started"] = True

        def work():
            try:
                info, why = codesign.read_cert_detail()
            except Exception:            # 兜底：异常不许穿回 UI 线程（坑 54）
                info, why = None, ""
            self._ui_q.put(lambda: self._codesign_notify(info, why))

        threading.Thread(target=work, daemon=True).start()

    def _codesign_notify(self, info, why=""):
        """后台结果回主线程：先记下状态，再决定要不要问（问完记账，不再打扰）。"""
        self._codesign_info = info       # 可能为 None（没签名 / 读不出来）
        if not info:
            if why:
                # 这台机器**判断不了**（PowerShell 被禁 / 策略拦截）也得说出来 ——
                # 一律静默的话，用户既收不到询问、也收不到"判不了"，两条路全断（2026-10-06）。
                # 只留一行说明，不弹窗：环境受限的机器多半也弹不出什么结果。
                self._append("[签名] 这台机器判断不了本程序的签名状态（%s）。"
                             "想手动处理：设置 → 关于与诊断 → 连点 5 下版本号，"
                             "在开发者选项里操作。\n" % why, "meta")
            return                       # 没签名（why 为空）/ 读不出来：不问也不记账
        if info.get("trusted"):
            return                       # 已经信任：什么都不用做，也不记账
        if self.cfg.get("codesign_prompt"):
            return                       # 问过了（确认或取消都算）
        if self._closing:
            return
        self.cfg["codesign_prompt"] = True
        remembered = config.save_config(self.cfg)     # 先记账：点"取消"同样不再问
        if not remembered:
            # 记账失败的出口（2026-10-06）：只读目录里"问过了"记不住，就会天天重弹。
            # 至少把原因说明白 —— 信任动作本身写进系统证书库，与配置文件无关。
            self._append("[签名] 配置写不进去（%s），这条询问记不住，下次启动还会再问。"
                         "可在 设置 → 关于与诊断 →「诊断」里查原因。\n"
                         % (config.write_error()[1] or "原因未知"), "meta")
        if not messagebox.askyesno(
                "把本软件签名列入本机可信名单",
                "当前本软件签名未进入本机可信名单，自动更新功能可能无法使用，"
                "请确认将签名写入本机可信名单\n\n"
                "请确保该软件是从 GitHub 上直接下载的，\n"
                "否则将签名列入可信名单是一件很危险的事\n\n"
                "如取消，后续仍可点击 5 下版本号进入开发者模式进行授权"):
            return
        ok, why = codesign.trust()
        if ok:
            self._codesign_info = dict(info, trusted=True)
            self._append("[签名] 已把本软件的签名列入本机可信名单（当前用户）。\n", "meta")
        else:
            self._append("[签名] 列入可信名单失败：%s\n" % why, "meta")

    def _guide_missing(self):
        """引导第一屏那份缺件清单。

        走 `diagnose.guide_missing`（廉价判据：引擎在不在 / 有没有可聊的模型 / 云端有没有
        密钥），**不跑整套 run_checks** —— 那套会真试写目录、bind 端口、逐模型算 ngl，
        而这里是主线程，一点「新手引导」就冻一下。
        首跑与「设置 → 关于与诊断 → 新手引导」**共用这一处**，所以重看引导看到的
        第一屏跟第一次打开时是同一份（W 2026-10-02：不该一边是"环境无问题"一边是缺件）。
        """
        try:
            return diagnose.guide_missing(self.cfg)
        except Exception:
            return []                    # 判据坏了就放欢迎页，别把引导一起拖死

    def _is_first_open(self):
        """这次打开算不算"首次"（首次安装，**或升级后首次打开**）—— 首次扫描问这里。

        判据 = "配置文件以前不存在"（`self._first_run`）**或** `guide_done` 不是当前版本
        （与新手引导同一处口径：`guide_done` 在看过/跳过引导时写当前 `APP_VERSION`）。
        首次扫描用这个**并集**（升级后也扫一遍）；而"要不要自动弹引导"用它的**一半**
        —— 只在真正的首跑弹，升级不重弹（见 `_first_run_flow`）。
        """
        return bool(self._first_run) or str(self.cfg.get("guide_done", "")) != APP_VERSION

    def _first_open_scan(self):
        """首次打开扫一遍引擎与模型（W 2026-10-05 第二轮）。

        引擎：两份**都没就位**时按「自动定向」的判据找一次并写回指路 —— 判据 / 落点就是
        `engine_install.auto_locate` / `set_dir`，与界面那个「自动定向」按钮**同一个函数**。
        模型目录：当前指的目录里**一个 `.gguf` 都没有**、又没手动定向来源时，按
        `models.auto_locate_models_dir` 找一个装着模型的目录写回（廉价判据：只看文件名）。
        这两步**同步**做：引导第一屏的缺件判据下一刻就要算，异步的话会显示成"还没配好"。
        用户已经指过路、只是文件不在时**不擅自改**（指路是他自己填的）。

        模型参数：起后台线程补全缺失项（`_auto_scan_models`，带 10 分钟冷却，与手动按钮、
        进页钩子共用同一处判据）。任何一步失败都不许拖住启动。
        """
        if not self._is_first_open():
            return
        try:
            changed = False
            for key in ("llama", "sd"):
                if engine_install.configured_exe(key, self.cfg):
                    continue                    # 已经就位，一个字都不动
                found = engine_install.auto_locate(key, self.cfg, app_dir=APP_DIR)
                if found:
                    engine_install.set_dir(self.cfg, key, os.path.dirname(found))
                    changed = True
            if not extra_sources(self.cfg) and not dir_has_any_gguf(
                    self.cfg.get("models_dir")):
                found_dir = auto_locate_models_dir(self.cfg, app_dir=APP_DIR)
                if found_dir:
                    self.cfg["models_dir"] = found_dir
                    changed = True
            if changed:
                config.save_config(self.cfg)
        except Exception:
            pass
        # 扫完可能刚把引擎 / 模型目录指到用户已经放好的那份 ⇒ 立刻问一次"第一个能用的
        # 模型有了没有"，别等引导第一屏或状态轮询（坑 150）。
        try:
            self._maybe_adopt_first_model(force=True)
        except Exception:
            pass
        try:
            self._auto_scan_models()
        except Exception:
            pass

    def _first_run_flow(self):
        """首跑：放一遍遮罩引导。

        只在"这次真的是首跑"（配置文件之前不存在）且没看过当前版本的引导时自动放；
        升级不重弹 —— 重看入口常驻 设置 → 关于与诊断。
        **自动弹只给"真正的首跑"这一条路**：配置文件之前不存在 **且** 没看过当前版本的
        引导（`_is_first_open()` 里那半"升级后首次打开"只触发首次扫描，不重弹引导）。
        诊断**不探显卡**：首跑时配置里还没有 GPU 信息，探一次最坏要等 nvidia-smi
        的 10 秒超时，而那件事跟"缺不缺引擎和模型"无关（坑 4：外部命令别挡在界面上）。
        """
        if not self._first_run:
            return
        if str(self.cfg.get("guide_done", "")) == APP_VERSION:
            return
        self.start_guide()

    def start_guide(self, missing=None):
        """放一遍遮罩引导（首跑自动放；之后由 设置 → 关于与诊断 的按钮重看）。

        **先关掉设置窗口**：引导挖的洞对准的是主窗口上的控件，隔着一个盖住大半屏的设置
        窗口去放，用户看到的就是"遮罩压在设置页上、被指着的东西根本不在洞里"（W 报的）。
        关掉等于点「关闭」—— 没保存的编辑会丢，这点与设置窗口一贯的行为一致。
        「诊断」次级页面同理：它还开着就会盖在遮罩上面，所以一起关掉。
        """
        if missing is None:
            missing = self._guide_missing()
        for attr in ("_settings_win", "_diag_win"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    if w.winfo_exists():
                        w.destroy()
                except Exception:
                    pass
                setattr(self, attr, None)
        # 设置窗销毁后它的滚动区句柄必须一起清掉 —— 否则输出栏的「去配置引擎」
        # 之类跳转还会往一个已销毁的页面栈上select（is_current 判据会拿到野对象）
        self._settings_sp = None
        if self._guide is not None and self._guide.alive():
            self._guide.close()        # 不叠两层：重看 = 关掉旧的再开

        def _done():
            self.cfg["guide_done"] = APP_VERSION
            try:
                config.save_config(self.cfg)
            except Exception:
                pass
            self._guide = None
            # 引导期间顶栏是强制全显的（步骤目标指着「启动服务」与状态灯）；
            # 收尾后按当前模型恢复按需显隐
            self._guide_active = False
            try:
                self._render_status(self._server_alive_flag, self._server_ready_flag)
            except Exception:
                pass
            # 遮罩引导收尾之后才问签名（W 2026-10-05）：首跑那次两个浮层叠在一起很糟，
            # 所以首跑不在启动流程里问，留到这儿。内部有"只跑一次 + 问过就不再问"的判据。
            try:
                self._codesign_offer_start()
            except Exception:
                pass

        try:
            self.root.lift()           # 主窗口先回到最前，遮罩才有东西可盖
            # 引导步骤的目标是 start_btn / status_label 这些：生图 / 生视频 / 云端场景下
            # 它们本来被藏起来了，引导期间一律强制可见，不然洞和面板量到的是没映射的控件
            self._guide_active = True
            self._render_status(self._server_alive_flag, self._server_ready_flag)
            self._guide = widgets.SpotlightGuide(self.root, guide.steps(self, missing),
                                                 on_close=_done)
        except Exception as e:
            self._guide = None
            self._guide_active = False
            self._render_status(self._server_alive_flag, self._server_ready_flag)
            messagebox.showwarning("新手引导",
                                   "引导没能打开（%s: %s），界面照常可用。"
                                   % (type(e).__name__, e))

    # ---- 布局 ----
    def _build_topbar(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.status_var = tk.StringVar(value="○ 检查中…")
        self.status_label = tk.Label(top, textvariable=self.status_var,
                                     fg=theme.c("muted"),
                                     font=("Microsoft YaHei UI", 10, "bold"))
        self.status_label.pack(side="left")

        # 模型名：可点击，右侧带下箭头（"顺时针旋转90度的>"），点开切换菜单
        self.model_var = tk.StringVar(value="")
        self.model_btn = tk.Button(
            top, textvariable=self.model_var, command=self.show_model_menu,
            relief="flat", bd=0, highlightthickness=0, padx=2, pady=0,
            bg=theme.c("bg"), fg=theme.c("accent"),
            activebackground=theme.c("bg"), activeforeground=theme.c("accent"),
            cursor="hand2", font=("Microsoft YaHei UI", 10, "bold"))
        # ⚠ 这里**不要**给模型名按钮加固定 width：试过 width=30，顶栏需求从 904 涨到
        # 1120，右侧「清空对话」被压到 1px 直接消失（2026-10-03 实测）。
        # 顶栏宽度是按「模型名不超过 models.TOPBAR_ALIAS_MAX 字」来保证的，
        # 限长在 models.short_alias() 里做，不在这里钉死宽度。
        self.model_btn.pack(side="left", padx=(4, 6))
        self._update_model_label()

        # 右侧按钮组（pack side=right 自右向左排列）
        # （原「🎨 生图」独立窗口已废弃：生图统一在主聊天流进行，见 model_kind=image 分支；
        #   ImageDialog / open_image_dialog 已于 2026-10-03 作为死代码从仓库版本移除，
        #   原实现本地留存于 D:\tmp\deadcode_ImageDialog_20261003.py.txt）
        # 配色规范（W 定）：顶栏一律中性黑字描边（可点击=黑、禁用=灰、悬停/按下
        # 灰底区分），不用彩色 —— 全部走默认 Round.Secondary.TButton，不给 bootstyle。
        self.btn_settings = theme.button(top, command=self.open_settings,
                                         style="Round.Icon.TButton",
                                         image=self._gear_photo())
        self.btn_settings.pack(side="right", padx=3)
        widgets.bind_tip(self.btn_settings, "设置")
        self.stop_svc_btn = theme.button(top, text="停止服务",
                                         command=self.stop_server_async, state="disabled")
        self.stop_svc_btn.pack(side="right", padx=3)
        self.start_btn = theme.button(top, text="启动服务", command=self.on_start_restart)
        self.start_btn.pack(side="right", padx=3)
        self.stop_gen_btn = theme.button(top, text="停止生成",
                                         command=self.stop_generate, state="disabled")
        self.stop_gen_btn.pack(side="right", padx=3)
        self.clear_btn = theme.button(top, text="清空对话", command=self.clear_chat)
        self.clear_btn.pack(side="right", padx=3)

        # 按当前模型类型初始化按钮状态（生图模型：启动按钮即刻置灰）
        self._render_status(False, False)

    def _gear_photo(self):
        """「设置」齿轮图标：ui/gear_icon 内嵌 b64 → tk.PhotoImage（22px 档）。

        master 显式给 self.root（多 root 的自检夹具里，默认 root 可能不是本窗）；
        图引用挂 root 防 GC；b64 解码一次仅 ~1KB，加载零感知。
        """
        import base64
        from . import gear_icon
        photo = tk.PhotoImage(
            master=self.root, data=base64.b64decode(gear_icon.GEAR_PNG_B64))
        if not hasattr(self.root, "_gear_icon"):
            self.root._gear_icon = photo
        return self.root._gear_icon

    def _build_chat(self):
        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, padx=10)
        # 聊天区 = tk.Text + 圆角 ttk 滚动条（替代 ScrolledText：其内置滚动条是
        # tk 原生件、主题管不到）。先 pack 滚动条再 pack 带 expand 的 Text
        # （坑 160 同一条 pack 饥饿纪律），self.chat 的类型与全部用法不变。
        self.chat = tk.Text(
            mid, state="disabled", wrap="word", relief="flat", bd=0,
            highlightthickness=0,
            # height 只是"最小请求高度"，不是显示高度：实际靠 expand 撑满。
            # 默认 20 行（约 460px）加上输入区 63px、顶栏 26px 就超过窗口最小高度 520px，
            # pack 会按入列顺序分配，最后入列的输入区被饿掉——窗口一缩输入框就没了（W 报）。
            # 现在给小一点的最小值，收缩时先压输出区，输入区保持固定。
            height=6,
            font=("Microsoft YaHei UI", 10),
            **theme.text_kw(fallback_bg="#ffffff"))
        self.chat_scroll = theme.scroll(mid, command=self.chat.yview)
        self.chat.configure(yscrollcommand=self.chat_scroll.set)
        self.chat_scroll.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_configure("user", foreground=theme.c("accent"),
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_configure("assistant", foreground=theme.c("body"))
        self.chat.tag_configure("thinking", foreground=theme.c("muted"),
                                font=("Microsoft YaHei UI", 9, "italic"))
        self.chat.tag_configure("error", foreground=theme.c("error"))
        self.chat.tag_configure("meta", foreground=theme.c("muted"),
                                font=("Microsoft YaHei UI", 9))
        # 启动时按当前模型类型给出引导：只说"这一步怎么用"，细节留给选模型时那一句
        # 与设置页 / README（W 的要求：字数变少，不逐条罗列细节）
        # 「根本没选上模型」「本地纯文本模型」「云端直连模型」是三件事，判据与顶栏共用
        # （`model_missing`，坑 128：同一事实的两个入口各写一遍迟早走偏）。原来这里无条件
        # 拼 display_name → 空模型时打成「当前模型：（语言模型）」，云端模型则打出
        # 「t-wl::qwen3-plus」这种内部 id，还叫用户去点「启动服务」（云端根本不用启动）。
        cloud = providers.is_cloud(self.cfg)
        missing = model_missing(self.cfg)
        if missing:
            name = NO_MODEL_LABEL
        elif cloud:
            name = providers.display_of_cloud(self.cfg, self.cfg.get("model", ""))
        else:
            name = display_name(self.cfg, self.cfg["model"])
        kind = self.cfg.get("model_kind")
        if kind in ("image", "video") and not missing:
            # 附图在生图那边是参考图、在生视频这边是**首帧**（-i/--init-img，开发机主体是
            # fl2va 变体，写成"参考图"是坑 42 的老错）；云端生视频没有首帧入参，所以那条不承诺
            extra = {"image": "和参考图（可选）",
                     "video": "" if cloud else "和首帧（可选）"}[kind]
            self._append("本地对话台已就绪。\n"
                         "当前模型：%s —— 无需启动服务，直接发提示词%s即可。\n"
                         "点顶部模型名切换模型；回车发送，Shift+回车换行；"
                         "生成中可点「停止生成」。\n"
                         "────────────────────\n" % (name, extra), "meta")
        else:
            if missing:
                what = ("模型目录里放一个 .gguf 就能选上"
                        if not cloud else "在 设置 → 云端模型 里挑一个就能选上")
            elif cloud:
                what = "云端模型直连服务商，不用启动服务，发送后稍等一会儿就有回答"
            else:
                what = "点「启动服务」加载，状态变 ● 运行中 后即可对话"
            self._append("本地对话台已就绪。\n"
                         "当前模型：%s —— %s。\n"
                         "点顶部模型名切换模型；回车发送，Shift+回车换行；"
                         "「停止生成」会保留已生成的内容。\n"
                         "────────────────────\n"
                         % (name if (missing or cloud) else name + "（语言模型）", what),
                         "meta")

    def _build_inputbar(self):
        # 附件条（默认隐藏；有附图或文本文件时 pack 到输入区上方）。
        # v1.0.9 形态：左 = 横向瓷砖条（预览在上、缩短文件名在下，拖动/滚轮左右滑动），
        # 右 = 边界区域（本次发送的两档选择，仅生图模型显示），底下横贯一条状态行
        # （预算截断 / 多图取舍 —— 状态信息不挪悬停，与 ADR §5.2 判据④同口径）。
        self.attach_frame = ttk.Frame(self.root)
        self.attach_frame.columnconfigure(0, weight=1)

        # -- 左：横向瓷砖条。canvas + 内框，瓷砖在内框里从左往右排，溢出可滑动 --
        self._tile_px = 48          # 瓷砖预览边长；高 DPI 屏用 96 档（Tk 的图不跟缩放，同 app_logo 的教训）
        try:
            if float(self.root.tk.call("tk", "scaling")) >= 1.75:
                self._tile_px = 96
        except Exception:
            pass
        strip = tk.Frame(self.attach_frame, background=widgets.default_bg())
        strip.grid(row=0, column=0, sticky="we")
        self.attach_canvas = tk.Canvas(strip, height=self._tile_px + 40,
                                       highlightthickness=0, xscrollincrement=1,
                                       background=widgets.default_bg())
        self.attach_canvas.pack(side="left", fill="both", expand=True)
        self._tile_inner = tk.Frame(self.attach_canvas, background=widgets.default_bg())
        self.attach_canvas.create_window((0, 0), window=self._tile_inner, anchor="nw")
        self._tile_inner.bind("<Configure>",
                              lambda _e: self.attach_canvas.configure(
                                  scrollregion=self.attach_canvas.bbox("all")))
        self._strip_drag_x = None   # 拖动滑动的锚点（x_root）；None = 没在拖
        for w in (self.attach_canvas, self._tile_inner):
            w.bind("<Button-1>", self._strip_drag_start)
            w.bind("<B1-Motion>", self._strip_drag_move)
            w.bind("<ButtonRelease-1>", self._strip_drag_end)
            w.bind("<MouseWheel>", self._strip_wheel)

        # -- 右：边界区域，本次发送的两档选择（仅生图模型；不支持的档置灰不隐藏，W 2026-10-06）--
        self.refmode_zone = tk.Frame(self.attach_frame, relief="groove", bd=1,
                                     background=widgets.default_bg())
        self.refmode_zone.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        tk.Label(self.refmode_zone, text="带图方式", background=widgets.default_bg(),
                 foreground=theme.c("label"),
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=8, pady=(4, 0))
        self._ref_mode_var = tk.StringVar(value="")
        self._refmode_radios = {}
        for m in capability.MODES:
            rb = theme.radio(self.refmode_zone, text=capability.MODE_LABEL[m],
                             variable=self._ref_mode_var, value=m,
                             command=self._on_ref_mode_pick, bootstyle="primary")
            rb.pack(anchor="w", padx=8, pady=(0, 3))
            self._refmode_radios[m] = rb
        self.refmode_zone.grid_remove()   # 非生图模型不显示；_refresh_ref_mode_zone 决定

        # -- 状态行（空时 grid_remove）--
        self._attach_note_var = tk.StringVar(value="")
        self.attach_note = ttk.Label(self.attach_frame, textvariable=self._attach_note_var,
                                     foreground=theme.c("muted"),
                                     font=("Microsoft YaHei UI", 9))
        self.attach_note.grid(row=1, column=0, columnspan=2, sticky="w")
        self.attach_note.grid_remove()

        self._tile_photos = []       # 瓷砖预览图引用（防 GC；每次重建瓷砖整批换新）
        self._attached_image = None  # 待发送图片路径（单张视图：附件条与既有调用都读它）
        self._attached_images = []   # 待发送图片路径列表（生图带图两档都要多张；顺序即发送顺序）
        self._img_ref_mode = ""      # 本次生图带图用哪一档（""=按本模型默认档；右侧区域可显式选）
        self._attached_file = None   # 待发送文本附件（read_document + 预算截取的成品块）；单文档，新挂替换旧的

        bot = ttk.Frame(self.root)
        # side="bottom" 让输入区在分配空间时先于可伸缩的聊天区被满足：
        # 窗口再小也是压缩输出区，输入框不会再消失
        bot.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
        self.input = tk.Text(bot, height=3,
                             # width 必须显式给小值，理由与上面聊天区给小 height 同一条（坑 56）：
                             # tk.Text 默认 width=80 字符，在 200% 缩放的机器上（Tk scaling≈2.0）
                             # 请求宽度就到 966px，比 880 窗口的空腔还宽 —— pack 把整条 cavity
                             # 给了输入框，右边那列「发送 / 📎 附件」直接被挤成 1x1 不可见，
                             # 用户看到的就是"没有发送按钮"。实际宽度靠 expand 撑，不靠这个值。
                             width=20, font=("Microsoft YaHei UI", 10),
                             relief="flat", highlightthickness=1,
                             highlightbackground=theme.c("border"),
                             **theme.text_kw())
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<Shift-Return>", self._on_shift_return)

        btns = ttk.Frame(bot)
        btns.pack(side="left", fill="y", padx=(6, 0))
        self.send_btn = theme.button(btns, text="发送", command=self.send_message,
                                     width=10, bootstyle="primary")
        self.send_btn.pack(fill="both", expand=True)
        self.attach_btn = theme.button(btns, text="📎 附件", command=self.pick_image,
                                       width=10, bootstyle="secondary-outline")
        self.attach_btn.pack(fill="x", pady=(4, 0))

    # ---- 对话 ----
    def _set_busy_ui(self, busy):
        """按"是否有生成任务在跑 / 是否在做服务操作"刷新输入区按钮。

        busy = LLM 生成中；self._img_busy = 生图进行中；self._vid_busy = 生视频进行中；
        self._svc_busy = 服务操作中。
        - 停止按钮：任一生成任务在跑都必须可用（此前只看 busy，导致服务操作
          回调 _set_busy_ui(False) 会把生图中的停止按钮误置灰）；
        - 发送 / 清空：任一任务在跑或服务操作中都禁用。
        """
        running = bool(busy) or self._img_busy or self._vid_busy
        blocked = running or self._svc_busy
        self.send_btn.configure(state="disabled" if blocked else "normal",
                                text="生成中…" if busy else "发送")
        self.stop_gen_btn.configure(state="normal" if running else "disabled",
                                    text="停止生成")
        self.clear_btn.configure(state="disabled" if blocked else "normal")

    def _any_busy(self):
        """任一任务在进行（生成 / 生图 / 生视频 / 服务操作）→ True。忙碌的统一判据。

        与 _set_busy_ui 的 blocked 同一口径。"任务在跑时不该动 cfg / 引擎"的入口
        （切模型、手动指向、整理对话框、发送、清空）一律判它，别各自只盯 _busy
        （坑 19：四个忙碌标志各查各的，迟早漏一个）。
        """
        return bool(self._busy or self._img_busy or self._vid_busy or self._svc_busy)

    def _open_containing(self, path):
        """在资源管理器中打开文件所在文件夹并选中该文件。"""
        subprocess.run(["explorer", "/select,", os.path.normpath(path)])

    # ---- 云端生图 / 生视频：进度行、任务取回、取消语义 ----
    def _cloud_begin(self, kind):
        """开一条进度行并置忙碌标志（与本地引擎链路共用同一套队列与渲染）。

        返回 (gen, out_q, stop_flag)。进度行三件套与本地链路一致：mark 定在 "end-1c"、
        gravity=left、进度文字自带前导换行——漏掉任何一条都会让进度行堆叠（坑 10）。
        """
        if kind == "image":
            self._img_gen += 1
            mark, q, gen = "imgprog%d" % self._img_gen, self._img_q, self._img_gen
            self._img_mark = mark
            self._img_busy, self._cloud_img = True, True
            self._img_tail, self._img_extra_paths = [], []
            self._img_log = ""
            self._t0 = time.time()
        else:
            self._vid_gen += 1
            mark, q, gen = "vidprog%d" % self._vid_gen, self._vid_q, self._vid_gen
            self._vid_mark = mark
            self._vid_busy, self._cloud_vid = True, True
            self._vid_tail, self._vid_saw_decode = [], False
            self._vid_phase = "sample"
            self._vid_t0 = time.time()
        # 新任务一律从"还没有 task_id"开始：上一轮云端任务被取消时 _cloud_reset 不会
        # 跑到，残留的旧 tid 会被这一轮的「停止生成」误当成自己的任务去取消。
        # 真实 tid 由 worker 提交成功后写回（见 _cloud_image_worker/_cloud_video_worker）。
        self._cloud_tid = ""
        self.chat.mark_set(mark, "end-1c")
        self.chat.mark_gravity(mark, "left")
        self._stop_flag = threading.Event()
        self._set_busy_ui(True)
        return gen, q, self._stop_flag

    def _cloud_reset(self, kind):
        """任务收尾时清掉"这一次是云端"的标记，免得本地链路误用云端取消分支。"""
        if kind == "image":
            self._cloud_img = False
        else:
            self._cloud_vid = False
        self._cloud_tid = ""

    def _offer_cloud_recovery(self):
        """启动时列出没落地的云端任务，给「取回」按钮。

        为什么要这个：产物 URL 只活 24 小时，而任务一旦提交就停在服务商那边——
        断电、关窗、点"停止等待"都不会让它消失。没有这个入口，那份结果就再也拿不回来了。
        """
        try:
            jobs = cloudjobs.unfinished()
            cloudjobs.prune(int(self.cfg.get("cloud_keep_days", 7) or 7))
        except Exception as e:
            crashlog.note("[云端台账读取失败] %s" % e)
            return
        if not jobs:
            return
        self.chat.configure(state="normal")
        try:
            self.chat.insert("end", "\n[云端] 有 %d 个云端任务的结果还没落地"
                             "（产物地址只活 24 小时）：\n" % len(jobs), "meta")
            for j in jobs[:6]:
                self.chat.insert("end", "  " + cloudjobs.describe(j) + "\n", "meta")
                if cloudjobs.expired(j):
                    continue
                if str(j.get("status")) in ("pending", "running", "unknown", "submitted"):
                    text = "继续等"
                else:
                    text = "取回"
                btn = theme.button(self.chat, text=text,
                                   command=lambda x=j: self._recover_job(x),
                                   bootstyle="primary-outline")
                self.chat.window_create("end", window=btn)
                self.chat.insert("end", "  ", "meta")
            self.chat.insert("end", "\n", "meta")
            self.chat.see("end")
        except Exception as e:
            crashlog.note("[云端任务列表异常] %s" % e)
        finally:
            self.chat.configure(state="disabled")

    def _offer_engine_hint(self):
        """三条路都不通时，在输出栏留一行说明 + 几个直达按钮。

        为什么不靠引导说完就完：引导走完人就关了，"这台机器还差一步"得在**他下次打开
        程序时还在**（W 2026-10-02 验收原话："知道有地方但不会立刻去"）。
        只要**任意一条**路通了就不催 —— 只想用云端的人不缺东西，催他下引擎是噪音；
        只想用本地生图的人也不缺 llama 引擎（坑 150：原来这里只认 llama + 对话模型，
        装着 sd 引擎和生图模型的用户照样被催一遍）。

        **首个可用模型一出现就自动收掉**（W 2026-10-08）：整块文字带 `ENV_HINT_TAG`
        标签，`_dismiss_env_hint` 按标签删除（嵌在里面的按钮一并销毁）。留着的话，
        用户已经配好引擎、模型也加载完了，输出栏顶上还挂着一句"三条路都还没通"。
        """
        try:
            q = diagnose.quick_paths(self.cfg)
        except Exception:
            return                      # 提示坏了不能把窗口开不成
        if q["usable"]:
            return
        if self._env_hint_ins:
            return                      # 已经有一条在输出栏里了，别叠第二条
        self.chat.configure(state="normal")
        try:
            self.chat.insert("end", "\n[环境] 这台机器上三条路都还没通：\n",
                             ("meta", ENV_HINT_TAG))
            for m in q["missing"]:
                self.chat.insert("end", "  · %s\n" % m, ("meta", ENV_HINT_TAG))
            btn = theme.button(self.chat, text="去配置引擎",
                               command=lambda: self.open_settings(jump="eng"),
                               bootstyle="primary-outline")
            self.chat.window_create("end", window=btn)
            self.chat.insert("end", "  ", ("meta", ENV_HINT_TAG))
            btn2 = theme.button(self.chat, text="填云端密钥",
                                command=lambda: self.open_settings(jump="c_prov"),
                                bootstyle="secondary-outline")
            self.chat.window_create("end", window=btn2)
            self.chat.insert("end", "\n", ("meta", ENV_HINT_TAG))
            self._env_hint_ins = True
            self.chat.see("end")
        except Exception as e:
            crashlog.note("[环境提示渲染失败] %s" % e)
        finally:
            self.chat.configure(state="disabled")

    def _dismiss_env_hint(self):
        """把开机那条「[环境] 三条路都还没通」提示从输出栏收掉（W 2026-10-08）。

        触发点 = "已经有一个能用的模型"的每个出口：`_maybe_adopt_first_model` 里判成
        可用的四条分支 + 服务加载就绪（`_handle_status` 的 status 分支，即"模型加载
        完成"）。那条提示说的是"这台机器还差一步"，差的那步补上了就该消失。
        """
        if not self._env_hint_ins:
            return
        self._env_hint_ins = False
        try:
            ranges = self.chat.tag_ranges(ENV_HINT_TAG)
        except Exception:
            return
        if not ranges:
            return                      # 用户清过对话：内容连着标签一起没了
        self.chat.configure(state="normal")
        try:
            # 删除整段带标签文字：两个按钮是嵌在这段区间里的（window_create），
            # 删除它们所在位置的字符时 Tk 一并销毁嵌入窗。
            self.chat.delete(ranges[0], ranges[-1])
        except Exception as e:
            crashlog.note("[环境提示移除失败] %s" % e)
        finally:
            self.chat.configure(state="disabled")

    def _recover_job(self, job):
        """取回一个云端任务：查状态 → 已完成就下载，还在跑就继续轮询。"""
        tid = str(job.get("task_id") or "")
        kind = "image" if str(job.get("kind")) == "image" else "video"
        if self._img_busy or self._vid_busy or self._busy:
            self._append("\n[云端] 现在正忙，等当前任务结束后再取回。\n", "meta")
            return
        if cloudjobs.expired(job):
            self._append("\n[云端] 这个任务超过 24 小时了，产物地址已失效，取不回来。\n",
                         "error")
            return
        provider = providers.get_provider(self.cfg, job.get("provider_id"))
        if not provider:
            self._append("\n[云端] 取不回 %s：provider「%s」已经不在了。\n"
                         % (tid[:12], job.get("provider_id")), "error")
            return
        dest = str(job.get("dest") or "")
        if not dest:
            dest = os.path.join(config.cloud_media_dir(self.cfg, kind),
                                time.strftime(("img_" if kind == "image" else "vid_")
                                              + "%Y%m%d_%H%M%S")
                                + (".png" if kind == "image" else ".mp4"))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        except Exception as e:
            self._append("\n[云端] 存放目录建不出来：%s\n" % e, "error")
            return
        gen, q, stop = self._cloud_begin(kind)
        self._cloud_pid = str(provider["id"])
        self._cloud_tid = tid
        if kind == "image":
            self._img_out = dest
            self._img_log = dest + ".log"
        else:
            self._vid_out = dest
            self._vid_log = dest + ".log"
        self._append("\n[云端] 取回 task_id=%s…（查状态，已完成就直接下载）\n"
                     % tid[:16], "meta")
        threading.Thread(target=self._cloud_video_worker if kind == "video"
                         else self._cloud_image_worker,
                         args=(provider, str(job.get("model") or ""),
                               str(job.get("prompt") or ""), dest, gen, q, stop, tid),
                         daemon=True).start()

    def _cloud_cancel_task(self, tid, kind):
        """对云端任务发起取消。**只有排队中(PENDING)取消得掉**，运行中会被服务端拒。

        这里绝不当成"已取消"：任务仍在跑、仍会计费，文案必须如实（文档 §12.3 ⑥）。
        结果回主线程走 _ui_q（子线程直接碰控件会炸，坑 54）。
        """
        icon = "🎬" if kind == "video" else "🎨"
        if not tid:
            self._append("%s [已停止等待] 任务还没提交出去，不会再有结果。\n" % icon, "meta")
            return True
        provider = providers.get_provider(self.cfg, self._cloud_pid) or \
            providers.current_provider(self.cfg)

        def work():
            if provider is None:
                msg = "取消不了：找不到对应的服务商。任务可能还在云端跑，之后可在对话里点「取回」。"
            else:
                # MiniMax 的 v1/v2 两套轮询地址按模型名选，取消也要用同一个模型去认
                job = cloudjobs.get_job(tid) or {}
                _ok, msg = cloud_media.cancel(provider, tid,
                                              str(job.get("model") or ""))
            self._ui_q.put(lambda: self._append("%s %s\n────────────────\n" % (icon, msg),
                                                 "meta"))
        threading.Thread(target=work, daemon=True).start()
        return True

    # ---- 状态线程 ----
    def _status_loop(self):
        probe_broken = False
        was_ready = False
        while not self._closing:
            try:
                # 先做毫秒级本地进程检查：进程不在则无需（也避免）等待 HTTP 超时
                if not server_process_alive():
                    alive, ready = False, False
                else:
                    alive, ready = server_state(self.cfg)
                    # 同步"服务实际加载的模型"：除了"还不知道"（外部 vbs 启动），
                    # **服务从没就绪变成就绪的那一刻**也要重查一次。只看
                    # `_serving_model is None` 会漏掉"服务被别人换过模型"——最典型的是
                    # agent 经 8081 请求了另一个模型（代理会停旧服务、按它要的模型重载），
                    # 那时界面仍记着旧模型，用户切回旧模型再发消息会被"看起来一致"骗过、
                    # 实际由新模型回答（坑 134）。只在就绪边沿查一次，稳态不增加请求。
                    if ready and (self._serving_model is None or not was_ready):
                        name = _query_serving_model(self.cfg)
                        if name and name != self._serving_model:
                            changed = self._serving_model is not None
                            self._serving_model = name
                            if changed:
                                self._sq.put(("serving", name))
                was_ready = ready
            except Exception as e:
                # 判活现在是**硬抛**语义（进程表快照失败时不静默退回 tasklist，见
                # core/server._probe_snapshot）：这里必须接住并说出来，否则常驻线程直接
                # 死掉、状态灯冻在最后一个值上 —— 那才是最坏的静默失败。
                # 只报一次然后退避到 30s，免得每 3 秒刷一条同样的话。
                if not probe_broken:
                    probe_broken = True
                    self._sq.put(("note",
                                  "[服务] 状态探测不可用（%s: %s）——状态灯暂停更新；"
                                  "「启动/停止服务」仍可直接用。"
                                  % (type(e).__name__, e)))
                time.sleep(30)
                continue
            probe_broken = False
            self._sq.put(("status", alive, ready))
            # 状态文件写不进去（只读目录 / 磁盘满）以前是彻底静默的：症状是"每次启动都回到
            # 默认设置"，没人会想到去查目录权限。atomic_write_json 的"不抛"约定保留，但要说出来
            # —— 同一份错误只说一次（签名变了才再说），走既有的 note 通道，不弹窗打断。
            path, reason = config.write_error()
            sig = "%s|%s" % (path, reason)
            if path and sig != self._write_err_seen:
                self._write_err_seen = sig
                self._sq.put(("note",
                              "[配置] 写不进去：%s（%s）—— 设置不会保存。"
                              "把本程序换到一个可写的文件夹，或在 设置 → 关于与诊断 里点「一键诊断」。"
                              % (os.path.basename(path), reason)))
            time.sleep(3)

    # ---- 主循环轮询 ----
    def _poll(self):
        # 子线程不能直接碰控件，也不能跨线程 root.after（主线程不在 mainloop 时会抛
        # RuntimeError）：统一把"要做的事"塞进 _ui_q，由这里在主线程执行。
        for _ in range(32):
            try:
                fn = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                # 不能因为一个回调炸掉就把 after 链断了；但也不能静默吞掉（排查过同类问题）
                crashlog.note("[界面回调异常] %s: %s" % (type(e).__name__, e))
        # 流式输出：先收集成批（连续同类自然合并），处理完队列后一次性写入
        pending = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                kind, text = item
                if kind == "reasoning":
                    # 「展示思考过程」本地与云端是两个独立开关（2026-10-07 W 定）：
                    # 按本轮链路分流（_turn_cloud 在 _do_send 开线程前置位）
                    if self.cfg.get("cloud_show_reasoning" if self._turn_cloud
                                    else "show_reasoning", True):
                        if not self._reasoning_started:
                            pending.append(("thinking", "\n【思考】\n"))
                            self._reasoning_started = True
                        pending.append(("thinking", text))
                        self._cur_reasoning.append(text)
                elif kind == "content":
                    if self._reasoning_started and not self._content_started:
                        pending.append(("assistant", "\n\n【回答】\n"))
                    self._content_started = True
                    pending.append(("assistant", text))
                    self._cur_content.append(text)
                elif kind == "usage":
                    self._last_usage = text          # 收尾时统一显示，避免打断正文流
                elif kind == "retry":
                    pending.append(("meta", "\n[云端] %s\n" % text))
                elif kind == "fileplan":
                    pending.append(("meta", "\n[附件] %s\n" % text))
                elif kind in ("done", "stopped", "error"):
                    self._flush_stream(pending)      # 先落已收到的正文，再处理收尾
                    pending = []
                    if kind == "done":
                        self._finish_turn()
                        if str(text) == "length":
                            self._append("[提示] 本轮回复被 max_tokens 上限截断"
                                         "（思考过程 + 正式回答共享该额度，"
                                         "思考较长时会挤掉回答）。\n"
                                         "解决：设置 → 生成参数 调大 max_tokens；或 服务参数 把 "
                                         "reasoning 设为 off（不思考，直接回答）。\n", "error")
                    elif kind == "stopped":
                        self._append("\n\n[已停止生成——已生成内容已保留，"
                                     "补充新信息后可直接继续发送]", "meta")
                        self._finish_turn()
                    else:
                        self._append("\n\n[错误] " + str(text), "error")
                        self._finish_turn(error=True)
            except Exception as e:
                # 关键健壮性：与生图/生视频分支同一铁律——任何异常都不得中断
                # after 链（断了就是界面永久假死）；落崩溃日志但不中断
                try:
                    self._append("[内部错误] 对话事件处理异常: %s\n" % e, "error")
                except Exception:
                    crashlog.note("[对话事件处理异常] %s: %s" % (type(e).__name__, e))
        self._flush_stream(pending)

        while True:
            try:
                item = self._img_q.get_nowait()
            except queue.Empty:
                break
            try:
                if item[0] == "line":
                    self._handle_img_line(item[1])
                elif item[0] == "progress":
                    # 云端生图：进度来自轮询/下载，没有 stdout 可解析，直接给整行文案
                    self._update_img_progress_line(item[1])
                elif item[0] == "exit":
                    self._handle_img_exit(item[1], item[2])   # exit 事件带 (rc, gen)
            except Exception as e:
                # 关键健壮性：生图 UI 更新异常不得中断主轮询（否则界面假死）
                try:
                    self._append("[内部错误] 生图进度处理异常: %s\n" % e, "error")
                except Exception:
                    pass

        while True:
            try:
                item = self._vid_q.get_nowait()
            except queue.Empty:
                break
            try:
                if item[0] == "line":
                    self._handle_vid_line(item[1])
                elif item[0] == "progress":
                    # 云端生视频：同生图，轮询事件直接给整行进度文案
                    self._update_vid_progress_line(item[1])
                elif item[0] == "exit":
                    self._handle_vid_exit(item[1], item[2])
            except Exception as e:
                # 同生图：任何异常都必须被吃掉，不能让 after 链断掉（界面永久假死）
                try:
                    self._append("[内部错误] 生视频进度处理异常: %s\n" % e, "error")
                except Exception:
                    pass

        while True:
            try:
                item = self._sq.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_status(item)
            except Exception as e:
                # 同上：状态事件异常也不得打断 after 链（界面假死）
                try:
                    self._append("[内部错误] 状态事件处理异常: %s\n" % e, "error")
                except Exception:
                    crashlog.note("[状态事件处理异常] %s: %s" % (type(e).__name__, e))

        if not self._closing:
            self.root.after(80, self._poll)

    # ---- 开发者选项：后台查更新（只在填了 GitHub 令牌时才跑）----
    def _dev_upd_start(self):
        """按"有没有 GitHub 令牌"决定后台查更新的定时器该不该跑。幂等：改完令牌直接调。

        为什么按令牌分岔：匿名接口每 IP 每小时只给 60 次，后台每 10 分钟问一次就要占掉
        6 次 —— 用户自己点「检查更新」的额度会因此变少，而这条后台行为根本不是他要的。
        带了令牌（5000 次/小时）才谈得上"顺手替他一直盯着"（W 2026-10-04 的口径）。
        """
        self._dev_upd_stop()
        if not secrets.get_github_token():
            return
        try:
            self._dev_upd["timer"] = self.root.after(
                updater.POLL_EVERY * 1000, self._dev_upd_tick)
        except Exception:
            self._dev_upd["timer"] = None    # 窗口正在拆：没有定时器可挂，不是错误

    def _dev_upd_stop(self):
        """把待触发的定时器撤掉（没挂过 / 已经触发过都是空操作）。"""
        t = self._dev_upd.get("timer")
        self._dev_upd["timer"] = None
        if t is None:
            return
        try:
            self.root.after_cancel(t)
        except Exception:
            pass

    def _dev_upd_tick(self):
        """到点了：查一次（在子线程），顺手把下一次排上。"""
        self._dev_upd["timer"] = None
        token = secrets.get_github_token()
        if not token:               # 令牌在这期间被清掉了：不再续排
            return
        cur = APP_VERSION
        # 与关于页**共用同一个 cache**：那边刚查过的话，这儿带 If-None-Match 拿个 304
        # 就够了 —— 不计额度、也不用重新解析（见 core/updater.fetch_releases）
        cache = self._upd_check["cache"]

        def work():
            try:
                res = updater.check_update(cur, updater.CHANNEL_STABLE, cache=cache,
                                           token=token)
            except Exception as e:          # 兜底：异常不许穿回 UI 线程（坑 54）
                res = (updater.STATE_ERROR, {"msg": "检查更新时出错：%s" % e})
            self._ui_q.put(lambda: self._dev_upd_notify(*res))

        threading.Thread(target=work, daemon=True).start()

    def _dev_upd_notify(self, state, info):
        """后台结果 → 真查到新版本才弹更新窗口（2026-10-05 起是能一键更新的那扇）。

        "同一版本只打扰一次"靠 `cfg["upd_dismissed"]`：判据是 **tag 变了没有**，不是时间，
        所以出现更新的版本时照旧会弹。**记录动作在窗口关闭时做**（用户亲手关掉才算数），
        这里只负责判与弹；手动点「检查更新」不受这份记录限制，永远弹。
        """
        tag = str((info or {}).get("tag") or "")
        if state == updater.STATE_UPDATE and tag and tag != str(
                self.cfg.get("upd_dismissed", "") or ""):
            self.open_update_window(info, auto=True)
        self._dev_upd_start()       # 续排下一次

    def _layout_topbar(self, show_start, show_stop, show_status):
        """按可见性重排顶栏（2026-10-07 W 定：无用按钮不出现，而不是置灰摆着）。

        右侧那排全是 `side="right"` 的 pack：pack_forget 再 pack 会排到队尾，
        所以每次都按固定顺序整排重放 —— 显隐只影响"谁在场"，不影响相对顺序。
        状态灯在左侧、model_btn 之前，重新入列要用 `before=` 钉回原位。

        **显隐状态没变就直接返回**（W 2026-10-08）：`_render_status` 每 3 秒被状态
        队列叫一次，而"整排重放"实测要 6~12 ms —— 状态没变时那是纯浪费，拖动窗口
        时撞上还会多一次顿挫。layout 只由这三个开关（+ 引导期的 `_guide_active`，
        它已经在入参里被折算进三个开关）决定，所以比一下上次的元组就够。
        """
        key = (bool(show_start), bool(show_stop), bool(show_status))
        if key == self._topbar_state:
            return
        self._topbar_state = key
        for w in (self.btn_settings, self.stop_svc_btn, self.start_btn,
                  self.stop_gen_btn, self.clear_btn):
            w.pack_forget()
        self.btn_settings.pack(side="right", padx=3)
        if show_stop:
            self.stop_svc_btn.pack(side="right", padx=3)
        if show_start:
            self.start_btn.pack(side="right", padx=3)
        self.stop_gen_btn.pack(side="right", padx=3)
        self.clear_btn.pack(side="right", padx=3)
        if show_status:
            self.status_label.pack(side="left", before=self.model_btn)
        else:
            self.status_label.pack_forget()

    def _render_status(self, alive, ready):
        """渲染状态灯 + 状态驱动的按钮样式与显隐。

        显隐规则（2026-10-07 W 定）：选中**本地生图 / 生视频或云端模型**时
        「启动服务」不出现（那条链路根本不经过它，置灰摆着只会让人以为要点）；
        「停止服务」只在文本服务真的还在跑时出现（一键释放显存的出口要留着）；
        状态灯云端常显「☁ 云端就绪」，本地生图 / 生视频在服务没跑时一起藏。
        遮罩引导期间（`_guide_active`）一律强制可见 —— 引导步骤的目标指着它们。
        """
        cloud = providers.is_cloud(self.cfg)
        media_local = (not cloud) and self.cfg.get("model_kind") in ("image", "video")
        if cloud:
            # 只写"云端就绪"：服务商名已经在那边的模型按钮上了（「模型名（云）」），
            # 这里再拼一遍会长到把模型按钮顶出顶栏（W 报的显示问题）
            self.status_var.set("☁ 云端就绪")
            self.status_label.configure(fg=theme.c("accent"))
        elif ready:
            self.status_var.set("● 运行中 (端口 %s)" % self.cfg.get("port"))
            self.status_label.configure(fg=theme.c("ok"))
        elif alive:
            self.status_var.set("◐ 模型加载中…")
            self.status_label.configure(fg=theme.c("warn"))
        else:
            self.status_var.set("○ 未运行")
            self.status_label.configure(fg=theme.c("muted"))

        self._layout_topbar(
            show_start=(not cloud and not media_local) or self._guide_active,
            show_stop=bool(alive) or self._guide_active,
            show_status=cloud or bool(alive) or not media_local or self._guide_active)

        if self._svc_busy:
            self.start_btn.configure(state="disabled")
            self.stop_svc_btn.configure(state="disabled")
        elif cloud or media_local:
            # 云端 / 生图 / 生视频都不需要聊天服务：启动按钮即使可见（引导期）也置灰；
            # 停止按钮仍按实际服务状态（若旧聊天服务还在运行可停掉）
            self.start_btn.configure(text="启动服务", state="disabled")
            self.stop_svc_btn.configure(state="normal" if alive else "disabled")
        else:
            self.start_btn.configure(text="重启服务" if alive else "启动服务",
                                     state="normal")
            self.stop_svc_btn.configure(state="normal" if alive else "disabled")

    def _handle_status(self, item):
        tag = item[0]
        if tag == "status":
            self._server_alive_flag = item[1]
            self._server_ready_flag = item[2]
            self._render_status(item[1], item[2])
            if item[2]:
                # 服务把模型加载完成 = 本地这条链路已经通了（也可能是换载完成，
                # 幂等、没插过提示时是空操作）⇒ 收掉开机那条"[环境] 都没通"。
                self._dismiss_env_hint()
            # 兜底：第一个"能用的模型"配好了就切过去（带冷却，判据在 core.models）。
            # 真正让它"立即响应"的是事件钩子；这一处只兜"程序外面发生的变化"（坑 150）。
            try:
                self._maybe_adopt_first_model()
            except Exception as e:
                crashlog.note("[自动选中模型异常] %s: %s" % (type(e).__name__, e))
        elif tag == "serving":
            # 服务实际加载的模型变了（常见来源：agent 经 8081 让代理换了模型）。
            # 顶栏跟着刷新 + 说一句，免得"顶栏写着 A、回答其实来自 B"（坑 134）
            self._update_model_label()
            self._append("\n[服务] 服务现在加载的是 %s"
                         "（可能由 agent 经代理切换；再发消息会按当前选中的模型自动换载）。\n"
                         % display_name(self.cfg, str(item[1])), "meta")
        elif tag == "svc_done":
            _, alive, ready, note, is_error, launched = item
            self._svc_busy = False
            self._server_alive_flag = alive
            self._server_ready_flag = ready
            # 用"本次操作实际加载的模型快照"维护状态（操作期间用户可能又切换了选中模型）
            self._serving_model = launched if (ready and launched) else None
            self._update_model_label()          # 修复：切换模型后立即刷新显示名
            self._render_status(alive, ready)
            self._set_busy_ui(self._busy)       # 恢复发送按钮等服务操作后的状态
            self._append(str(note) + "\n", "error" if is_error else "meta")
        elif tag == "reload_done":
            # 换载完成：就绪则自动把暂存的消息发出去（上下文随消息全量携带）
            ok = item[1]
            self._svc_busy = False
            self._server_alive_flag = ok
            self._server_ready_flag = ok
            self._serving_model = (os.path.basename(self.cfg["model"])
                                   if ok else None)
            self._render_status(ok, ok)
            self.send_btn.configure(state="normal", text="发送")
            pending = self._pending_text
            self._pending_text = None
            if ok and pending:
                self._do_send(pending)
            elif pending:
                self.input.insert("1.0", pending)
                self._append("[提示] 加载失败，你的消息已放回输入框，可稍后重试。\n", "error")
        elif tag == "note":
            self._append("\n[服务] " + str(item[1]) + "\n", "meta")
        elif tag == "ngl":
            _, key, res = item
            self._append("\n[服务] 新模型 %s（共 %d 层）：按显存 %.1fGB 与模型大小估算，"
                         "显存可容纳约 %d 层，已自动设置 GPU 层数 %d（可在设置中微调）。\n"
                         % (display_name(self.cfg, key), res["n_layers"],
                            res["vram"], res["ngl_max"], res["ngl"]), "meta")
        elif tag == "alias":
            _, key, alias = item
            if os.path.basename(self.cfg["model"]) == key:
                self._update_model_label()
            self._append("\n[服务] 模型别名已生成：%s\n" % alias, "meta")

    def on_close(self, force=False):
        # 遮罩引导先关掉：它是 overrideredirect 的无边框窗，留着会在退出流程里挡住鼠标
        if getattr(self, "_guide", None) is not None:
            try:
                self._guide.close()
            except Exception:
                pass
            self._guide = None
        # 场景 0：生图/生视频任务进行中 —— 立即中止（快速关闭也能即时停止任务）
        if self._img_busy:
            self._cancel_chat_image()
        if self._vid_busy:
            self._cancel_chat_video()
        # 场景 1：服务已就绪 —— 二选弹窗（可取消关闭）。`force=True`（自替换更新的
        # 「确认并更新」）跳过弹窗：用户刚在更新窗口里确认过"要关闭程序"，再问一遍是折磨。
        if self._server_alive_flag:
            if force:
                stop_server()             # 直接停服务并退出
                self._kill_proc()
            else:
                dlg = ExitDialog(self.root)
                self.root.wait_window(dlg)
                if dlg.result in (None, "cancel"):
                    return                # 取消：什么都不做，窗口继续运行
                stop_server()             # "stop"：停止服务并退出
                self._kill_proc()
        # 场景 2：启动/重启/换载进行中 —— 中止启动流程，不留残留进程
        elif self._svc_busy:
            self._abort_startup()
        # 场景 3：空闲 —— 直接退出
        # 关窗前把对话记录补一份：中途没走到 _finish_turn 的内容（只有用户话）也留住
        self._save_chat_log()
        self._closing = True
        self.proxy.stop()
        self._dev_upd_stop()          # 撤掉后台查更新的定时器，别让它对着拆到一半的根窗打
        self.root.destroy()


def _say(msg):
    """往控制台写一行；--windowed 的 exe 没有真实 stdout，这时静默跳过就行。"""
    out = sys.stdout
    if out is None:
        return
    try:
        out.write(msg + "\n")
        out.flush()
    except Exception:
        pass


def selfcheck():
    """打包产物自检：只回答"这份 exe 能不能跑起来"，不开窗口、不碰用户数据。

    CI 里靠**退出码**判定（--windowed 下没有控制台，打印只是给人看的）：
    0 = 路径解析与内置服务商都在位；非 0 = 打包漏了东西或 APP_DIR 指错了地方。
    """
    bad = []
    # 自检只读：配置文件还没生成（首次运行）时按默认值判，不去创建它。
    # 内置服务商是 load_config 幂等种进去的，所以这条路也要自己种一遍，否则会假失败。
    first_run = not os.path.isfile(CONFIG_PATH)
    if first_run:
        cfg = dict(DEFAULT_CONFIG)
        providers.ensure_builtin_providers(cfg)
    else:
        cfg = load_config()
    # 冻结模式下配置必须落在 exe 同目录，指到 _MEIPASS 临时目录就是打包路径错了
    if getattr(sys, "frozen", False) and not os.path.normcase(
            CONFIG_PATH).startswith(os.path.normcase(APP_DIR)):
        bad.append("CONFIG_PATH 不在 APP_DIR 下：%s / %s" % (APP_DIR, CONFIG_PATH))
    ids = {p.get("id") for p in cfg.get("cloud_providers", [])}
    for need in ("deepseek", "aliyun-token-plan"):
        if need not in ids:
            bad.append("内置服务商缺失：%s" % need)
    _say("LLM Chat %s  frozen=%s" % (APP_VERSION, bool(getattr(sys, "frozen", False))))
    _say("APP_DIR       = %s" % APP_DIR)
    _say("CONFIG_PATH   = %s%s" % (CONFIG_PATH,
                                   "（还不存在，首次运行时创建）" if first_run else ""))
    _say("cloud providers = %s" % "、".join(sorted(x for x in ids if x)))
    for line in bad:
        _say("SELFCHECK FAIL " + line)
    _say("SELFCHECK OK" if not bad else "SELFCHECK FAILED")
    return 0 if not bad else 1


def main():
    if "--selfcheck" in sys.argv[1:]:
        sys.exit(selfcheck())
    if "--version" in sys.argv[1:]:
        _say("LLM Chat %s" % APP_VERSION)
        return
    crashlog.install()               # 未捕获异常先落盘再走默认处理（--windowed 没有控制台）
    # 上一次自替换更新留下的收尾（.updating 旧版 + 哨兵）就交给新 exe 的第一次启动清：
    # 放 main() 而不是 App.__init__ —— 与界面无关；源码运行 cur_exe_path() 是 None，
    # finish_pending 直接空操作，只有冻结的 exe 才有旧账可清（2026-10-05）。
    selfupdate.finish_pending(selfupdate.cur_exe_path())
    first_run = not os.path.isfile(CONFIG_PATH)
    cfg = load_config()
    # 默认值不指向任何一台具体机器上的文件（分发给别人时才有意义）：
    # 当前选中的模型"不能用"（没配 / 文件不在 / 对应引擎没就位 / 云端没密钥）时，
    # 就从"现在就能用"的模型里挑一个顶上 —— 判据与主页面那套**同一处**
    # （`core.models.selected_usable` / `first_usable`，坑 128 / 坑 150）。
    # 启动这一次先顶上；"配好第一个可用模型就立即切过去"由运行期的
    # `ModelsMixin._maybe_adopt_first_model` 负责（那才是那个一次性的自动接管）。
    if not selected_usable(cfg):
        got = first_usable(cfg)
        if got:
            cfg["model"] = got["id"]
            cfg["model_kind"] = got["kind"]
            cfg["model_provider"] = got["provider"]
            cfg["model_auto_picked"] = True
    root = tk.Tk()
    # 主页面主题层（ttkbootstrap 试验分支）：挂主题要在 root 建好之后、App 构造之前
    # —— ttkbootstrap 的 Style 无参构造绑默认 root。ttkbootstrap 没安装时 apply()
    # 是空操作，主页面回落到原生外观（ui/theme.py 的降级闸）。
    theme.apply(root)
    root.title("LLM 本地对话台 - llama.cpp")
    # 1080x700 是量出来的，不是拍的（DPI-aware 严格档实测，含"模型名占满 22 字"的情况）：
    # 顶栏右侧 5 个按钮各要 120px，左侧状态灯 + 模型名合计要 904px。
    # 880 宽时客户区只剩 860 →「清空对话」被压扁到 76px、「设置」右缘超出 3px；
    # 1000 宽能全露但只剩 76px 余量，而状态灯文字会变长（如"● 运行中 · 已加载…"），
    # 余量太窄仍会被挤。1080 给顶栏 1060，留 156px。
    # 高度 700 照顾 1366x768 的本：扣掉任务栏约 728 可用高度，700 刚好放得下。
    root.geometry("1080x700")
    root.minsize(720, 520)
    widgets.set_app_icon(root)
    app = App(root, cfg, first_run=first_run)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    # 首跑引导等界面画完再放（要量控件的真实位置来挖洞），400ms 足够首帧落地
    root.after(400, app._first_run_flow)
    root.mainloop()
