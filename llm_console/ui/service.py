# -*- coding: utf-8 -*-
"""llm_console.ui.service — 界面 Mixin：服务启停/重启/换载、就绪等待、显存共存三选确认"""

import os
import threading
import time
from tkinter import messagebox

from .. import texts

from ..core import modelreq
from ..core.models import BLOCK_ENGINE, BLOCK_MODEL, BLOCK_STALE, chat_start_block, display_name
from ..core.params import ctx_for, current_ngl, estimate_kv_gb
from ..core.server import _reset_alive_cache, server_alive, server_process_alive, server_ready, start_server, stop_server


# 「启动服务」被闸门挡住时的两行话：同一个原因，按钮的悬停气泡说一句、动作路径被
# 别的入口踩到时在输出栏说一句。判据只有一处（`core.models.chat_start_block`），
# 措辞也只有一处，免得两个入口各念一套（坑 128）。
START_BLOCK_TIP = {
    BLOCK_ENGINE: "llama 引擎还没就位：在 设置 → 模型文件与引擎 → 引擎管理 里定向或下载。",
    BLOCK_MODEL: "这台机器上还没有可用的本地文本模型：在 设置 → 模型文件与引擎 里补全或手动定向模型。",
    BLOCK_STALE: texts.SVC_BLOCK_STALE_TIP,
}
START_BLOCK_NOTE = {
    BLOCK_ENGINE: "llama 引擎还没就位（在 设置 → 模型文件与引擎 → 引擎管理 里定向或下载）。",
    BLOCK_MODEL: "这台机器上还没有可用的本地文本模型（引擎与能聊天的 .gguf 都要就位）。",
    BLOCK_STALE: texts.SVC_BLOCK_STALE_NOTE,
}


class ServiceMixin:
    """App 的服务生命周期与显存共存确认职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    # ---- 本地生成前的显存共存确认 ----
    def _confirm_shared_vram(self, action):
        """8GB 显存上 llama-server 常驻与本地生成争抢显存，启动前问一次。

        返回 "go"（保持服务开启继续）/ "stop"（先停服务再生成）/ "cancel"（放弃本次）。
        用 askyesnocancel：No 走"停服务"，而右上角叉与 Cancel 一样返回 None——
        正好满足"点叉 = 取消本次生成"。服务没在跑时直接返回 go，不加一次点击。
        """
        # 判活分两级：本程序记的旗标（HTTP 验证过、端口准确）优先；旗标说没在跑而
        # 进程表里有 llama-server.exe 时，再按**本配置的端口**用 HTTP 确认一次 ——
        # 映像名判活分不清"别人挂在别的端口上的实例"，直接弹窗会把无辜实例卷进
        # "先停止服务"（排查报告 R-01 的上游放大器）。这次 HTTP 最多等 2 秒，
        # 只在"外部真有 llama-server 在跑"的少见现场才付。
        alive = self._server_alive_flag
        if not alive and server_process_alive():
            alive = server_alive(self.cfg)
        if not alive:
            return "go"
        ans = messagebox.askyesnocancel(
            texts.SVC_VRAM_TITLE,
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
        self._append(texts.SVC_RELEASING, "meta")
        try:
            stop_server(self.cfg)
            time.sleep(1.0)
        except Exception as e:
            self._append(texts.SVC_STOP_FAIL % e, "error")
        _reset_alive_cache()
        self._server_alive_flag = False
        self._server_ready_flag = False
        self._serving_model = None
        self._render_status(False, False)
        self._append(texts.SVC_RELEASED, "meta")

    # ---- 性能分级（2026-10-06 W）：加入时的分级提示在 models_ui 扫描侧；
    #      启动这条线只拦「必闪退」档（3 级），弹一次确认、可坚持继续 ----
    def _confirm_fatal_perf(self, path, kind="text", files=None):
        """启动 / 发送前的可运行性确认：评估当前配置带不带得动，3 级才弹窗。

        每模型每进程只弹一次（`_perf_warned`）；评估不出来（读不到头 / 硬件没探到）
        不拦 —— 缺件那些预检各自管各自的，这里只回答"装不装得下"。
        """
        try:
            need = modelreq.assess(self.cfg, path, kind, files=files)
            lvl = modelreq.grade(need, self.cfg.get("vram_gb") or 0,
                                 self.cfg.get("ram_gb") or 0) if need else 0
        except Exception:
            return True
        if lvl < modelreq.LEVEL_FATAL or not path or path in self._perf_warned:
            return True
        self._perf_warned.add(path)
        return messagebox.askyesno(
            texts.SVC_PERF_TITLE,
            texts.SVC_PERF_CONFIRM
            % (modelreq.LEVEL_TEXT[lvl], need["detail"]))

    # ---- 服务控制 ----
    def _start_gate_block(self, action=texts.SVC_START):
        """启动引擎前的闸门：这台机器现在能不能把 llama-server 拉起来。

        判据只有一处（`core.models.chat_start_block` = 有可用的本地文本模型 + 选中的那个
        文件还在），顶栏按钮的置灰与悬停气泡问的是同一条。动作路径也要问：坑 128 例②
        就是"入口查了、启动那条没查"——代理照常在 8081 空跑。这里兜住的是按钮置灰之外
        的入口（设置页那个 agent 按钮、竞态、以后新加的调用点）。
        `action` 只影响那句话说的是"未启动"还是"未重启"：重启同样要把服务拉起来，
        拦下来才不会把本来在跑的服务停成一个空档。
        """
        blk = chat_start_block(self.cfg)
        if not blk:
            return False
        self._append(texts.SVC_NOT_STARTED
                     % (action, START_BLOCK_NOTE.get(blk, START_BLOCK_NOTE[BLOCK_MODEL])),
                     "error")
        return True

    def _launch_server(self, ctx):
        """启动 llama-server 并把 Popen 句柄写回 `_proc`；起不来返回 None。

        **`_proc` 的唯一写入口**（2026-10-17 收敛）：启动 / 重启 / 聊天换载三处原来各自
        抄一遍 `try: self._proc = start_server(...) except: _finish_svc(...)`，于是同一
        个字段有两个 owner（结构检查器 G4 白名单 `_proc` 那条"语义共享、形态灰色"的由来）。
        现在 chat 只管"起来了就等就绪"，Popen 与失败收尾都留在 ServiceMixin 里 ——
        失败时 `_finish_svc` 已经复位按钮，调用方只需补自己那条终态事件。
        """
        try:
            self._proc = start_server(self.cfg, ctx)
        except Exception as e:
            self._finish_svc(False, texts.SVC_START_FAIL_RAW + str(e), True)
            return None
        return self._proc

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
                # 同一性复核（坑 275）：预检 bind 与本进程 bind 之间存在 TOCTOU，
                # 极端情况下 /health 的 200 可能来自**占了同一端口的别的实例**。
                # 本进程 PID 在监听名单里才算真就绪；反查工具失灵（空集）不拦。
                try:
                    from ..core.server import _listening_pids
                    listeners = _listening_pids(int(self.cfg.get("port") or 0))
                    if listeners and getattr(self._proc, "pid", None) not in listeners:
                        self._sq.put(("note",
                                      "端口 %d 被另一个服务进程占用（响应不是刚启动的这个），"
                                      "本次启动未接管该端口。"
                                      % int(self.cfg.get("port") or 0)))
                        return False
                except Exception:
                    pass
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

    def _svc_guard(self, fn):
        """服务类 worker 的顶层兜底（R-30）：body 里任何一步意外抛错（stop_server /
        start_server / _wait_ready…），都必须走一次 _finish_svc 复位 _svc_busy 并如实
        开口——原来三个 worker 体没有顶层 try，一抛就是「启动/停止/重启/换载」四个
        按钮永久置灰，会话等于废掉，还要重启程序才能恢复。"""
        def wrapped():
            try:
                fn()
            except Exception as e:
                try:
                    self._finish_svc(False, texts.SVC_INTERRUPTED % e, True)
                except Exception:
                    self._svc_busy = False      # _finish_svc 自身再出事也绝不卡死按钮
        return wrapped

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
        if self._start_gate_block(texts.SVC_RESTART if self._server_alive_flag else texts.SVC_START):
            return
        if self._server_alive_flag:
            self.restart_server()
        else:
            self.start_server_async()

    def start_server_async(self, agent=False):
        if self._svc_busy or self._server_alive_flag:
            return
        if self._start_gate_block():
            return
        if not self._confirm_fatal_perf(self.cfg.get("model", "")):
            return
        self._begin_svc()
        # 线程启动前的 setup 段兜底（2026-10-11 排查报告）：_svc_busy 一旦置位，
        # 中间任何一步意外抛错都必须走一次 _finish_svc，否则按钮永久置灰。
        # worker 本体另有 _svc_guard 兜；这里只护"还没来得及开线程"的窗口。
        try:
            self._append("\n[服务] 正在启动 %s（GPU 层数 %d），加载约需十几秒到几分钟…\n"
                         % (display_name(self.cfg, self.cfg["model"]), current_ngl(self.cfg)), "meta")
            ctx = ctx_for(self.cfg, agent=agent)
            kv = estimate_kv_gb(self.cfg, ctx=ctx)
            if kv:
                scene = "agent" if agent else texts.SVC_SCENE_MAIN
                warn = (texts.SVC_VRAM_HIGH
                        if kv["vram"] > 4 else "")
                self._append(texts.SVC_KV_EST
                             % (scene, ctx, kv["total"], kv["vram"], kv["ram"], warn), "meta")

            def work():
                launched = os.path.basename(self.cfg["model"])
                if self._launch_server(ctx) is None:
                    return
                ok = self._wait_ready()
                self._finish_svc(ok, texts.SVC_READY
                                 if ok else
                                 "[服务] 启动失败或超时——请检查模型路径与参数（设置 → 服务参数）。",
                                 not ok, launched=launched)
            threading.Thread(target=self._svc_guard(work), daemon=True).start()
        except Exception as e:
            self._finish_svc(False, texts.SVC_INTERRUPTED % e, True)

    def stop_server_async(self):
        if self._svc_busy or not self._server_alive_flag:
            return
        self._begin_svc()
        try:
            self._append(texts.SVC_STOPPING, "meta")

            def work():
                killed = stop_server(self.cfg)
                if killed:
                    self._finish_svc(False, texts.SVC_STOPPED)
                else:
                    # 如实说"没动手"：句柄与端口反查都没找到目标时，一个进程都没杀（R-01）
                    self._finish_svc(False,
                                     "[服务] 没有找到监听端口 %d 的服务进程（未做任何操作）。"
                                     % int(self.cfg.get("port") or 0))
            threading.Thread(target=self._svc_guard(work), daemon=True).start()
        except Exception as e:
            self._finish_svc(False, texts.SVC_INTERRUPTED % e, True)

    def restart_server(self, agent=False):
        if self._svc_busy:
            return
        if not self._confirm_fatal_perf(self.cfg.get("model", "")):
            return
        self._begin_svc()
        try:
            self._append(texts.SVC_RESTARTING
                         % ("agent" if agent else texts.SVC_SCENE_MAIN), "meta")

            def work():
                stop_server(self.cfg)
                time.sleep(1)
                if self._closing:
                    return                  # 关闭中：不再启动新服务
                launched = os.path.basename(self.cfg["model"])
                if self._launch_server(ctx_for(self.cfg, agent=agent)) is None:
                    return
                ok = self._wait_ready()
                self._finish_svc(ok, texts.SVC_RESTARTED
                                 if ok else
                                 "[服务] 启动失败或超时——请检查模型路径与参数（设置 → 服务参数）。",
                                 not ok, launched=launched)
            threading.Thread(target=self._svc_guard(work), daemon=True).start()
        except Exception as e:
            self._finish_svc(False, texts.SVC_INTERRUPTED % e, True)

    def on_start_restart_agent(self):
        """本地模型 API 页的服务按钮：以 agent 场景的 context 启动/重启。"""
        if self._svc_busy or self.cfg.get("model_kind") == "image":
            return
        if self._start_gate_block(texts.SVC_RESTART if self._server_alive_flag else texts.SVC_START):
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
        stop_server(self.cfg)             # 端口反查兜底（进程可能已在监听端口）
