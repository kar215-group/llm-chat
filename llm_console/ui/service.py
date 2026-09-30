# -*- coding: utf-8 -*-
"""llm_console.ui.service — 界面 Mixin：服务启停/重启/换载、就绪等待、显存共存三选确认"""

import os
import threading
import time
from tkinter import ttk, scrolledtext, messagebox, filedialog

from ..core.models import display_name
from ..core.params import ctx_for, current_ngl, estimate_kv_gb
from ..core.server import _reset_alive_cache, server_alive, server_process_alive, server_ready, start_server, stop_server


class ServiceMixin:
    """App 的服务生命周期与显存共存确认职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    # ---- 本地生成前的显存共存确认 ----
    def _confirm_shared_vram(self, action):
        """8GB 显存上 llama-server 常驻与本地生成争抢显存，启动前问一次。

        返回 "go"（保持服务开启继续）/ "stop"（先停服务再生成）/ "cancel"（放弃本次）。
        用 askyesnocancel：No 走"停服务"，而右上角叉与 Cancel 一样返回 None——
        正好满足"点叉 = 取消本次生成"。服务没在跑时直接返回 go，不加一次点击。
        """
        if not (self._server_alive_flag or server_process_alive()):
            return "go"
        ans = messagebox.askyesnocancel(
            "显存共存确认",
            "%s需要占用显存，而 llama-server 当前仍在运行（常驻约 3GB）。\n\n"
            "  是 —— 保持服务开启，直接开始%s\n"
            "        （显存可能不够导致生成失败，聊天服务不受影响）\n\n"
            "  否 —— 先停止服务，再%s\n"
            "        （对话历史保存在本程序里不会丢，之后点「启动服务」可重新加载）\n\n"
            "  取消 / 关闭本窗口 —— 放弃本次%s，什么都不改\n"
            % (action, action, action, action))
        if ans is None:
            return "cancel"
        return "go" if ans else "stop"

    def _release_vram(self):
        """同步停止 llama-server 释放显存（用户在共存确认里选「否」时）。

        走同步路径是因为生图/生视频紧接着就要 Popen，必须先把显存腾出来；
        taskkill + 1s 等待与模型换载路径用的是同一套做法。
        """
        self._append("[服务] 正在停止 llama-server 以释放显存…\n", "meta")
        try:
            stop_server()
            time.sleep(1.0)
        except Exception as e:
            self._append("[服务] 停止失败：%s\n" % e, "error")
        _reset_alive_cache()
        self._server_alive_flag = False
        self._server_ready_flag = False
        self._serving_model = None
        self._render_status(False, False)
        self._append("[服务] 已停止，显存已释放。\n", "meta")

    # ---- 服务控制 ----
    def _begin_svc(self):
        """进入服务操作状态：相关按钮禁用，防止重入。"""
        self._svc_busy = True
        self.start_btn.configure(state="disabled")
        self.stop_svc_btn.configure(state="disabled")
        self.send_btn.configure(state="disabled")   # 服务操作期间暂不接受发送
        self.clear_btn.configure(state="disabled")

    def _wait_ready(self, timeout_loops=160):
        """等待服务就绪；大模型加载可到 1~3 分钟，期间定期汇报、崩溃快败。"""
        noted = -8
        for i in range(timeout_loops):
            if self._closing:
                return False         # 窗口已关闭：立刻中止等待，交由清理流程杀进程
            if server_ready(self.cfg):
                return True
            # 进程已退出（崩溃 / 参数不兼容）→ 快速失败，不傻等满上限
            if i >= 6 and not server_alive(self.cfg):
                self._sq.put(("note",
                              "服务进程已退出（可能是显存不足或该模型与当前 llama.cpp 不兼容）。"))
                return False
            if i - noted >= 12:               # 约每 18~20 秒汇报一次进度
                noted = i
                self._sq.put(("note", "仍在加载模型…（大模型首次加载可能需要 1~3 分钟）"))
            time.sleep(1.5)
        return False

    def _finish_svc(self, ok, note, is_error=False, launched=None):
        """服务操作完成：带回最新真实状态并解锁按钮。

        launched = 本次操作实际加载的模型文件名快照（启动/重启/换载时传入）。
        操作期间用户可能又切换了选中模型，用快照而非 cfg["model"] 才能
        保证 _serving_model 与服务真实加载的模型一致。
        """
        alive = server_alive(self.cfg)
        ready = bool(ok) and alive and server_ready(self.cfg)
        self._sq.put(("svc_done", alive, ready, note, is_error, launched))

    def on_start_restart(self):
        # 生图模型：按钮已置灰，此处静默返回（不弹窗）
        if self._svc_busy or self.cfg.get("model_kind") == "image":
            return
        if self._server_alive_flag:
            self.restart_server()
        else:
            self.start_server_async()

    def start_server_async(self, agent=False):
        if self._svc_busy or self._server_alive_flag:
            return
        self._begin_svc()
        self._append("\n[服务] 正在启动 %s（GPU 层数 %d，模型加载约需 10 秒~3 分钟，取决于模型大小）…\n"
                     % (display_name(self.cfg, self.cfg["model"]), current_ngl(self.cfg)), "meta")
        ctx = ctx_for(self.cfg, agent=agent)
        kv = estimate_kv_gb(self.cfg, ctx=ctx)
        if kv:
            scene = "agent" if agent else "主页面"
            warn = ("（显存部分偏高，若启动失败请降低 context 或 GPU 层数）"
                    if kv["vram"] > 4 else "")
            self._append("[服务] %s场景 context=%d，KV cache 预估：共 %.1fGB（显存 %.1f / 内存 %.1f）%s\n"
                         % (scene, ctx, kv["total"], kv["vram"], kv["ram"], warn), "meta")

        def work():
            launched = os.path.basename(self.cfg["model"])
            try:
                self._proc = start_server(self.cfg, ctx)
            except Exception as e:
                self._finish_svc(False, "[服务] 启动失败: " + str(e), True)
                return
            ok = self._wait_ready()
            self._finish_svc(ok, "[服务] 就绪，可以开始对话。"
                             if ok else
                             "[服务] 启动失败或超时——请检查模型路径与参数（设置 → 服务参数）。",
                             not ok, launched=launched)
        threading.Thread(target=work, daemon=True).start()

    def stop_server_async(self):
        if self._svc_busy or not self._server_alive_flag:
            return
        self._begin_svc()
        self._append("\n[服务] 正在停止…\n", "meta")

        def work():
            stop_server()
            self._finish_svc(False, "[服务] 服务已停止。")
        threading.Thread(target=work, daemon=True).start()

    def restart_server(self, agent=False):
        if self._svc_busy:
            return
        self._begin_svc()
        self._append("\n[服务] 正在重启（%s场景，应用最新服务参数）…\n"
                     % ("agent" if agent else "主页面"), "meta")

        def work():
            stop_server()
            time.sleep(1)
            if self._closing:
                return                      # 关闭中：不再启动新服务
            launched = os.path.basename(self.cfg["model"])
            try:
                self._proc = start_server(self.cfg, ctx_for(self.cfg, agent=agent))
            except Exception as e:
                self._finish_svc(False, "[服务] 启动失败: " + str(e), True)
                return
            ok = self._wait_ready()
            self._finish_svc(ok, "[服务] 重启完成，可以继续对话。"
                             if ok else
                             "[服务] 启动失败或超时——请检查模型路径与参数（设置 → 服务参数）。",
                             not ok, launched=launched)
        threading.Thread(target=work, daemon=True).start()

    def on_start_restart_agent(self):
        """API 连接页的服务按钮：以 agent 场景的 context 启动/重启。"""
        if self._svc_busy or self.cfg.get("model_kind") == "image":
            return
        if self._server_alive_flag:
            self.restart_server(agent=True)
        else:
            self.start_server_async(agent=True)

    # ---- 退出 ----
    def _kill_proc(self):
        """杀掉本程序 Popen 出的服务进程（中止加载中的启动）。"""
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        _reset_alive_cache()

    def _abort_startup(self):
        """关闭窗口时中止进行中的启动/重启/换载：杀掉待定进程，防残留。"""
        self._closing = True              # 让 _wait_ready 快速退出
        self._kill_proc()
        stop_server()                     # taskkill 兜底（进程可能已在监听端口）
