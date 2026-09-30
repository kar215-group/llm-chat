# -*- coding: utf-8 -*-
"""llm_console.ui.app — 界面主窗口：App 外壳（布局、主轮询、状态灯、退出）与程序入口 main()"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

from ..core.config import (load_config, DEFAULT_CONFIG, APP_DIR, APP_VERSION,
                           CONFIG_PATH)
from ..core import providers
from ..core.models import display_name
from ..core.server import _query_serving_model, server_process_alive, server_state, stop_server
from ..connection.proxy import ProxyServer
from .dialogs import ExitDialog, ImageDialog
from .chat import ChatMixin
from .image_gen import ImageGenMixin
from .video_gen import VideoGenMixin
from .service import ServiceMixin
from .models_ui import ModelsMixin
from .settings import SettingsMixin


class App(ChatMixin, ImageGenMixin, VideoGenMixin, ServiceMixin, ModelsMixin, SettingsMixin):
    """主窗口外壳：布局、主轮询、状态灯、退出；其余职责分散在各 Mixin。"""

    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.history = []            # 多轮对话（不含 system）
        self._busy = False           # 正在生成回复
        self._svc_busy = False       # 服务启动/停止操作进行中（防重入）
        self._closing = False
        self._server_alive_flag = False
        self._server_ready_flag = False
        self._stop_flag = None
        self._settings_win = None
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
        self._q = queue.Queue()      # 流式输出队列
        self._sq = queue.Queue()     # 状态队列
        self._ui_q = queue.Queue()   # "请在主线程执行"的回调队列（子线程调 root.after 会炸）
        # 本轮输出的分段状态（_do_send 里会重置；此处先初始化，避免
        # 任何异常路径下 _poll 收到残留消息时报 AttributeError）
        self._cur_reasoning = []
        self._cur_content = []
        self._reasoning_started = False
        self._content_started = False
        self.api_hint_var = None     # API 连接页的"已复制"提示
        self._last_usage = None      # 本轮云端/本地返回的 token 用量（收尾时显示）

        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self._build_topbar()
        self._build_chat()
        self._build_inputbar()

        # OpenAI 兼容中转：供 agent 应用接入（127.0.0.1:proxy_port）
        self.proxy = ProxyServer(cfg, note_fn=lambda m: self._sq.put(("note", m)))
        if cfg.get("proxy_enabled", True):
            self.proxy.start()

        threading.Thread(target=self._status_loop, daemon=True).start()
        threading.Thread(target=self._precompute_ngl, daemon=True).start()
        root.after(80, self._poll)
        self.input.focus_set()

    # ---- 布局 ----
    def _build_topbar(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.status_var = tk.StringVar(value="○ 检查中…")
        self.status_label = tk.Label(top, textvariable=self.status_var,
                                     fg="#999999",
                                     font=("Microsoft YaHei UI", 10, "bold"))
        self.status_label.pack(side="left")

        # 模型名：可点击，右侧带下箭头（"顺时针旋转90度的>"），点开切换菜单
        self.model_var = tk.StringVar(value="")
        self.model_btn = tk.Button(
            top, textvariable=self.model_var, command=self.show_model_menu,
            relief="flat", bd=0, highlightthickness=0, padx=2, pady=0,
            bg="SystemButtonFace", fg="#0b57d0",
            activebackground="SystemButtonFace", activeforeground="#0b57d0",
            cursor="hand2", font=("Microsoft YaHei UI", 10, "bold"))
        self.model_btn.pack(side="left", padx=(4, 6))
        self._update_model_label()

        # 右侧按钮组（pack side=right 自右向左排列）
        # （「🎨 生图」独立窗口已废弃：生图统一在主聊天流进行，见 model_kind=image 分支；
        #   ImageDialog / open_image_dialog 代码保留备用，不再有入口调用）
        ttk.Button(top, text="设置", command=self.open_settings).pack(side="right", padx=3)
        self.stop_svc_btn = ttk.Button(top, text="停止服务",
                                       command=self.stop_server_async, state="disabled")
        self.stop_svc_btn.pack(side="right", padx=3)
        self.start_btn = ttk.Button(top, text="启动服务", command=self.on_start_restart)
        self.start_btn.pack(side="right", padx=3)
        self.stop_gen_btn = ttk.Button(top, text="停止生成",
                                       command=self.stop_generate, state="disabled")
        self.stop_gen_btn.pack(side="right", padx=3)
        self.clear_btn = ttk.Button(top, text="清空对话", command=self.clear_chat)
        self.clear_btn.pack(side="right", padx=3)

        # 按当前模型类型初始化按钮状态（生图模型：启动按钮即刻置灰）
        self._render_status(False, False)

    def _build_chat(self):
        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, padx=10)
        self.chat = scrolledtext.ScrolledText(
            mid, state="disabled", wrap="word", relief="flat",
            # height 只是"最小请求高度"，不是显示高度：实际靠 expand 撑满。
            # 默认 20 行（约 460px）加上输入区 63px、顶栏 26px 就超过窗口最小高度 520px，
            # pack 会按入列顺序分配，最后入列的输入区被饿掉——窗口一缩输入框就没了（W 报）。
            # 现在给小一点的最小值，收缩时先压输出区，输入区保持固定。
            background="#ffffff", height=6,
            font=("Microsoft YaHei UI", 10))
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("user", foreground="#0b57d0",
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_configure("assistant", foreground="#1f1f1f")
        self.chat.tag_configure("thinking", foreground="#8f8f8f",
                                font=("Microsoft YaHei UI", 9, "italic"))
        self.chat.tag_configure("error", foreground="#c01c28")
        self.chat.tag_configure("meta", foreground="#a8a8a8",
                                font=("Microsoft YaHei UI", 9))
        # 启动时按当前模型类型给出引导
        name = display_name(self.cfg, self.cfg["model"])
        if self.cfg.get("model_kind") == "image":
            self._append("本地对话台已就绪。\n"
                         "当前模型：%s —— 生图无需启动服务，直接在下方输入提示词发送即可。\n"
                         "点击顶部模型名可切换模型；回车发送，Shift+回车换行。\n"
                         "生成中可随时点「停止生成」。\n"
                         "────────────────────\n" % name, "meta")
        elif self.cfg.get("model_kind") == "video":
            self._append("本地对话台已就绪。\n"
                         "当前模型：%s —— 生视频同样无需启动服务，直接输入提示词发送即可；"
                         "附图会作为参考图。\n"
                         "出片耗时取决于显卡（默认档位 512×512 / 20 步，通常要几分钟），"
                         "生成中可随时点「停止生成」。\n"
                         "点击顶部模型名可切换模型；回车发送，Shift+回车换行。\n"
                         "────────────────────\n" % name, "meta")
        else:
            self._append("本地对话台已就绪。\n"
                         "当前模型：%s（语言模型）——点「启动服务」加载，"
                         "状态变为 ● 运行中 后即可对话。\n"
                         "点击顶部模型名可切换模型；回车发送，Shift+回车换行。\n"
                         "生成中可随时点「停止生成」，已生成内容会保留。\n"
                         "新放入模型文件夹的模型会被自动识别，并按显存自动计算 GPU 层数。\n"
                         "────────────────────\n" % name, "meta")

    def _build_inputbar(self):
        # 附件预览条（默认隐藏；有附图或文本文件时 pack 到输入区上方）
        self.attach_frame = ttk.Frame(self.root)
        self.attach_thumb = tk.Label(self.attach_frame, relief="groove")
        self.attach_thumb.pack(side="left", padx=(0, 8))
        self.attach_name = ttk.Label(self.attach_frame, text="", foreground="#555555",
                                     wraplength=560, justify="left")
        self.attach_name.pack(side="left")
        ttk.Button(self.attach_frame, text="移除附件",
                   command=self.clear_attachment).pack(side="left", padx=8)
        self._attach_photo = None      # 缩略图引用（防 GC）
        self._attached_image = None    # 待发送图片路径
        self._attached_file = None     # 待发送文本附件（read_document + 预算截取的成品块）

        bot = ttk.Frame(self.root)
        # side="bottom" 让输入区在分配空间时先于可伸缩的聊天区被满足：
        # 窗口再小也是压缩输出区，输入框不会再消失
        bot.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
        self.input = tk.Text(bot, height=3, font=("Microsoft YaHei UI", 10),
                             relief="flat", highlightthickness=1,
                             highlightbackground="#cccccc")
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<Shift-Return>", self._on_shift_return)

        btns = ttk.Frame(bot)
        btns.pack(side="left", fill="y", padx=(6, 0))
        self.send_btn = ttk.Button(btns, text="发送", command=self.send_message, width=10)
        self.send_btn.pack(fill="both", expand=True)
        self.attach_btn = ttk.Button(btns, text="📎 附件", command=self.pick_image, width=10)
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

    def _open_containing(self, path):
        """在资源管理器中打开文件所在文件夹并选中该文件。"""
        subprocess.run(["explorer", "/select,", os.path.normpath(path)])

    # ---- 状态线程 ----
    def _status_loop(self):
        while not self._closing:
            # 先做毫秒级本地进程检查：进程不在则无需（也避免）等待 HTTP 超时
            if not server_process_alive():
                alive, ready = False, False
            else:
                alive, ready = server_state(self.cfg)
                # 同步"服务实际加载的模型"（外部 vbs 启动等场景也能对上）
                if ready and self._serving_model is None:
                    name = _query_serving_model(self.cfg)
                    if name:
                        self._serving_model = name
            self._sq.put(("status", alive, ready))
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
                print("[界面回调异常] %s: %s" % (type(e).__name__, e))
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
                    if self.cfg.get("show_reasoning", True):
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
                # after 链（断了就是界面永久假死）；打印但不中断
                try:
                    self._append("[内部错误] 对话事件处理异常: %s\n" % e, "error")
                except Exception:
                    print("[对话事件处理异常] %s: %s" % (type(e).__name__, e))
        self._flush_stream(pending)

        while True:
            try:
                item = self._img_q.get_nowait()
            except queue.Empty:
                break
            try:
                if item[0] == "line":
                    self._handle_img_line(item[1])
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
                    print("[状态事件处理异常] %s: %s" % (type(e).__name__, e))

        if not self._closing:
            self.root.after(80, self._poll)

    def _render_status(self, alive, ready):
        """渲染状态灯 + 状态驱动的按钮样式。"""
        cloud = providers.is_cloud(self.cfg)
        if cloud:
            # 只写"云端就绪"：服务商名已经在那边的模型按钮上了（「模型名（云）」），
            # 这里再拼一遍会长到把模型按钮顶出顶栏（W 报的显示问题）
            self.status_var.set("☁ 云端就绪")
            self.status_label.configure(fg="#0b57d0")
        elif ready:
            self.status_var.set("● 运行中 (端口 %s)" % self.cfg.get("port"))
            self.status_label.configure(fg="#1a7f37")
        elif alive:
            self.status_var.set("◐ 模型加载中…")
            self.status_label.configure(fg="#b58900")
        else:
            self.status_var.set("○ 未运行")
            self.status_label.configure(fg="#999999")

        if self._svc_busy:
            self.start_btn.configure(state="disabled")
            self.stop_svc_btn.configure(state="disabled")
        elif cloud or self.cfg.get("model_kind") in ("image", "video"):
            # 云端 / 生图 / 生视频都不需要聊天服务：启动按钮置灰（点击无效、不弹窗）；
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

    # ---- 设置窗口 ----
    def open_image_dialog(self):
        win = getattr(self, "_img_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        self._img_win = ImageDialog(self.root, self.cfg)

    def on_close(self):
        # 场景 0：生图/生视频任务进行中 —— 立即中止（快速关闭也能即时停止任务）
        if self._img_busy:
            self._cancel_chat_image()
        if self._vid_busy:
            self._cancel_chat_video()
        # 场景 1：服务已就绪 —— 二选弹窗（可取消关闭）
        if self._server_alive_flag:
            dlg = ExitDialog(self.root)
            self.root.wait_window(dlg)
            if dlg.result in (None, "cancel"):
                return                    # 取消：什么都不做，窗口继续运行
            stop_server()                 # "stop"：停止服务并退出
            self._kill_proc()
        # 场景 2：启动/重启/换载进行中 —— 中止启动流程，不留残留进程
        elif self._svc_busy:
            self._abort_startup()
        # 场景 3：空闲 —— 直接退出
        self._closing = True
        self.proxy.stop()
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
    cfg = load_config()
    # 默认值不指向任何一台具体机器上的文件（分发给别人时才有意义）：
    # 没配模型、或配的模型文件不在，就从模型目录里挑一个能聊天的顶上。
    # 云端模型的 cfg["model"] 是 "pid::model" 复合 id，不是文件路径——isfile 对它
    # 必为 False，若不先排除会把用户选中的云模型每次启动都静默换成本地模型。
    if (not providers.is_cloud(cfg)) and (
            not cfg.get("model") or not os.path.isfile(str(cfg.get("model", "")))):
        from ..core.models import scan_models
        _d, chat, _i = scan_models(cfg)
        if chat:
            cfg["model"] = chat[0]
            cfg["model_kind"] = "chat"
            cfg["model_provider"] = providers.LOCAL
    root = tk.Tk()
    root.title("LLM 本地对话台 - llama.cpp")
    root.geometry("880x660")
    root.minsize(720, 520)
    app = App(root, cfg)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
