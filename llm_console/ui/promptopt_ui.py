# -*- coding: utf-8 -*-
"""llm_console.ui.promptopt_ui — 提示词优化的界面层（PromptOptMixin）。

W 2026-10-09 定的交互形态：
  · 输入框内右上角一个**小星星按钮**，悬停提示「优化提示词」；输入为空 / 有任务在跑
    时不出现。
  · 点星星 → 输入框整块换成**候选面板**：三行竖排（保守版 / 增强版 / 精炼版），
    星星被顶掉；面板右上角一个 **✕** = 回退至原始输入（进行中时 = 停止）。
  · 点某一版 → 该版文本替换输入框内容（回到普通编辑态），选择记进
    `prompt_opt_choices.json`（core 层 record_choice，只存不析）。
  · ✕ 回退后再点星星 → **二级窗口**「已有优化结果，是否再次优化？」：
    「使用已有结果」（原面板重现，零请求）/「再次优化」（三路重发）/ Esc 取消。
    缓存按（场景, 输入原文）为键：输入改过就自动视为新一轮，不弹窗。

线程纪律（铁律 7）：optimize_all 在工作线程跑，on_event 与收尾一律经 `_ui_q`
递回主线程执行；工作线程绝不碰控件。单路失败的「重试」只重发那一路
（optimize_once，需求第 3 条）。
"""

import os
import threading
import tkinter as tk
from tkinter import ttk

from ..core import localmodels, models, promptopt, providers
from ..core.config import save_config
from ..connection import optimize
from . import theme, widgets

PREVIEW_CHARS = 140      # 候选行的预览截断长度（完整文本点选后进输入框）
TIP_CHARS = 400          # 悬停气泡里最多给多少字


class PromptOptMixin:
    """提示词优化：星星入口 + 候选面板 + 已有结果二级窗。方法一律 _opt_ 前缀。"""

    # ---------------------------------------------------------------- 建件
    def _build_opt_star(self):
        """在 _build_inputbar 末尾调用（字段出生点，铁律 3 / J5 口径）。"""
        self._opt_bot = self.input.master          # 输入框与右侧按钮列的容器
        self._opt_btns = self.send_btn.master      # 面板换回输入框时要 pack(before=它)
        self._opt_panel = None                     # 候选面板（None = 没开）
        self._opt_rows = {}                        # strategy -> 行控件与状态
        self._opt_head_var = None
        self._opt_x_btn = None
        self._opt_cache = None                     # {"key": (场景, 原文), "out": 结果}
        self._opt_scenario = ""
        self._opt_original = ""                    # 面板对应的原始输入（✕ 回退用）
        self._opt_stop = None                      # 本轮的 threading.Event
        self._opt_busy = False
        # 星星：输入框（tk.Text）的孩子，place 钉在它内右上角 —— 不随文本滚动，
        # 也不占布局（Text 本身没有"右上角挂件"位，place 覆盖是侵入最小的做法）。
        # 2026-10-09 W 定：**只显示星星本体、不套按钮框、做小**（tk.Label 而非
        # theme.button——那个会带描边/贴图；悬停反应仍是「优化提示词」气泡）。
        self._opt_star = tk.Label(self.input, text="★", cursor="hand2",
                                  font=("Microsoft YaHei UI", 10),
                                  foreground=theme.c("accent"),
                                  background=theme.c("bg"))
        theme.tint(self._opt_star, fg="accent", bg="bg")   # 切主题跟着重涂
        self._opt_star.bind("<Button-1>", lambda _e: self._opt_star_clicked())
        self._opt_star_visible = False             # place/place_forget 的去重状态
        widgets.bind_tip(self._opt_star, "优化提示词")
        self.input.bind("<KeyRelease>", self._opt_on_key, add="+")

    # ---------------------------------------------------------------- 星星显隐
    def _opt_on_key(self, _event=None):
        self._opt_star_refresh()

    def _opt_star_refresh(self):
        """显示规则：输入非空 且 面板没开 且 优化没在跑 且 无其它任务（_any_busy）。

        挂在 KeyRelease 与 _render_status（约 3 秒一拍）两处：前者管打字，后者兜底
        一切程序化改动（发送清空 / _pending_text 回填 / 切模型）。只读 Text + 布尔
        比较，变了才 place/place_forget（坑 172 的"没变就别动布局"纪律）。
        """
        if not hasattr(self, "_opt_star"):
            return
        try:
            has_text = bool(self.input.get("1.0", "end").strip())
        except Exception:
            return                                 # 收尾阶段控件已销毁
        show = has_text and self._opt_panel is None and not self._opt_busy \
            and not self._any_busy()
        if show == self._opt_star_visible:
            return
        self._opt_star_visible = show
        if show:
            # 贴着框边（W 2026-10-09：不再撑 padx，文本与星星都往框沿靠）
            self._opt_star.place(in_=self.input, relx=1.0, x=-4, y=1, anchor="ne")
        else:
            self._opt_star.place_forget()

    # ---------------------------------------------------------------- 自动选上（W 2026-10-09 定）
    def _opt_fill_prompt_model(self):
        """第一次有文本模型时自动把「提示词优化」的模型选上；**只在为空时动一次**。

        口径与设置页「通用」下拉一致：本地文本模型优先（菜单可见的），否则取
        菜单里第一个云端文本模型。用户选过（下拉保存过 / 手改配置）之后永远不
        覆盖 —— 判据就是 cfg 值非空。调用点：`_maybe_adopt_first_model`
        （"第一个可用模型配好"的共用事件，事件钩子与状态轮询兜底都走那里）。
        """
        try:
            if str(self.cfg.get("prompt_opt_model", "") or "").strip():
                return False
            pick = ""
            try:
                use = models.usable_local(self.cfg).get("chat") or []
                hid = localmodels.hidden_set(self.cfg)
                if [p for p in use if os.path.basename(p) not in hid]:
                    pick = "local"
            except Exception:
                pick = ""
            if not pick:
                for pid, _pname, m in providers.cloud_models(self.cfg,
                                                             providers.KIND_TEXT):
                    pick = providers.make_cloud_id(pid, m)
                    break
            if not pick:
                return False
            self.cfg["prompt_opt_model"] = pick
            try:
                save_config(self.cfg)
            except Exception:
                pass
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- 入口点击
    def _opt_star_clicked(self):
        if self._opt_busy or self._opt_panel is not None or self._any_busy():
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        scen = promptopt.resolve_scenario(
            self.cfg.get("model_kind"),
            bool(self._attached_images or self._attached_image),
            self._effective_ref_mode()
            if str(self.cfg.get("model_kind") or "") == "image" else "")
        if self._opt_cache and self._opt_cache["key"] == (scen, text):
            action = self._opt_ask_reuse()
            if action == "reuse":
                self._opt_reopen_cached()
                return
            if action != "again":
                return                             # Esc / 关窗 = 什么都不做
        self._opt_run(scen, text)

    # ---------------------------------------------------------------- 二级窗
    def _opt_ask_reuse(self):
        """「已有优化结果，是否再次优化？」→ "reuse" / "again" / None（取消）。"""
        win = tk.Toplevel(self.root)
        win.title("提示词优化")
        win.resizable(False, False)
        win.withdraw()
        win.transient(self.root)
        out = {"v": None}

        def done(v):
            out["v"] = v
            win.destroy()

        ttk.Label(win, text="已有优化结果，是否再次优化？",
                  font=("Microsoft YaHei UI", 10)).pack(padx=24, pady=(18, 12))
        box = ttk.Frame(win)
        box.pack(padx=24, pady=(0, 16))
        theme.button(box, text="使用已有结果", width=14, bootstyle="primary",
                     command=lambda: done("reuse")).pack(side="left", padx=4)
        theme.button(box, text="再次优化", width=10,
                     command=lambda: done("again")).pack(side="left", padx=4)
        win.bind("<Escape>", lambda e: done(None))
        win.protocol("WM_DELETE_WINDOW", lambda: done(None))
        widgets.center_on(win, self.root, size=(340, 130))
        win.grab_set()
        self.root.wait_window(win)
        return out["v"]

    # ---------------------------------------------------------------- 跑一轮
    def _opt_run(self, scen, text):
        self._opt_busy = True
        self._opt_original = text
        self._opt_scenario = scen
        self._opt_stop = threading.Event()
        self._opt_open_panel()
        self._opt_head("正在优化提示词…（三路并行）")
        self.send_btn.configure(state="disabled")
        self.attach_btn.configure(state="disabled")
        self._opt_star_refresh()

        def worker(stop=self._opt_stop):
            try:
                out = optimize.optimize_all(
                    self.cfg, text, scenario=scen, stop_flag=stop,
                    on_event=lambda s, st, d: self._ui_q.put(
                        lambda s=s, st=st: self._opt_evt(s, st)))
            except Exception as e:      # 兜底：任何意外都要把界面从"优化中"放回来
                out = {"ok": False, "scenario": scen, "model": "", "results": [],
                       "elapsed_ms": 0,
                       "error": "优化出错：%s: %s" % (type(e).__name__, e)}
            self._ui_q.put(lambda: self._opt_done(out))

        threading.Thread(target=worker, daemon=True, name="promptopt-ui").start()

    # ---------------------------------------------------------------- 面板
    def _opt_open_panel(self):
        """输入框整块换成候选面板（同一格位置，右侧「发送/附件」列保持可见但禁用）。"""
        panel = tk.Frame(self._opt_bot, highlightthickness=1,
                         highlightbackground=theme.c("border"),
                         background=theme.c("bg"))
        theme.tint(panel, bg="bg")
        self._opt_panel = panel

        head = tk.Frame(panel, background=theme.c("bg"))
        theme.tint(head, bg="bg")
        head.pack(fill="x", padx=8, pady=(5, 0))
        self._opt_head_var = tk.StringVar(value="")
        hl = tk.Label(head, textvariable=self._opt_head_var,
                      background=theme.c("bg"), foreground=theme.c("muted"),
                      font=("Microsoft YaHei UI", 9))
        theme.tint(hl, fg="muted", bg="bg")
        hl.pack(side="left")
        self._opt_x_btn = theme.button(head, text="×", width=3,
                                       command=self._opt_close_clicked)
        self._opt_x_btn.pack(side="right")
        widgets.bind_tip(self._opt_x_btn,
                         lambda: "停止优化" if self._opt_busy else "回退至原始输入")

        self._opt_rows = {}
        for s in promptopt.STRATEGIES:
            row = tk.Frame(panel, background=theme.c("bg"))
            theme.tint(row, bg="bg")
            row.pack(fill="x", padx=8, pady=(3, 4))
            top = tk.Frame(row, background=theme.c("bg"))
            theme.tint(top, bg="bg")
            top.pack(fill="x")
            name = tk.Label(top, text=promptopt.STRATEGY_LABEL[s],
                            background=theme.c("bg"), foreground=theme.c("accent"),
                            font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2")
            theme.tint(name, fg="accent", bg="bg")
            name.pack(side="left")
            stat = tk.Label(top, text="", background=theme.c("bg"),
                            foreground=theme.c("muted"),
                            font=("Microsoft YaHei UI", 9))
            theme.tint(stat, bg="bg")
            stat.pack(side="left", padx=6)
            retry = theme.button(top, text="重试", width=5,
                                 command=lambda s=s: self._opt_retry_one(s))
            # 重试按钮只在失败行出现（先建好、不摆）
            prev = tk.Label(row, text="", wraplength=600, justify="left", anchor="w",
                            background=theme.c("bg"), foreground=theme.c("body"),
                            font=("Microsoft YaHei UI", 10), cursor="hand2")
            theme.tint(prev, fg="body", bg="bg")
            prev.pack(fill="x", pady=(1, 0))
            for w in (name, prev):
                w.bind("<Button-1>", lambda _e, s=s: self._opt_pick(s))
            self._opt_rows[s] = {"frame": row, "stat": stat, "prev": prev,
                                 "retry": retry, "ok": False, "full": "",
                                 "suspect": False}

        def _wrap(ev):
            if ev.widget is not panel:
                return
            w = max(240, ev.width - 40)
            for r in self._opt_rows.values():
                try:
                    r["prev"].configure(wraplength=w)
                except Exception:
                    pass
        panel.bind("<Configure>", _wrap)

        self.input.pack_forget()
        panel.pack(side="left", fill="both", expand=True, before=self._opt_btns)

    def _opt_head(self, text):
        if self._opt_head_var is not None:
            self._opt_head_var.set(text)

    def _opt_close_panel(self):
        if self._opt_panel is not None:
            try:
                self._opt_panel.destroy()
            except Exception:
                pass
        self._opt_panel = None
        self._opt_rows = {}
        self._opt_busy = False
        self._opt_stop = None
        try:
            self.input.pack(side="left", fill="both", expand=True,
                            before=self._opt_btns)
        except Exception:
            pass                                   # 收尾阶段容器已拆

    # ---------------------------------------------------------------- 进度与结果
    def _opt_evt(self, strategy, state):
        """on_event 的主线程侧（经 _ui_q 递进来）。只更新那一行的状态字。"""
        row = self._opt_rows.get(strategy)
        if row is None or self._opt_panel is None:
            return
        if state == "sent":
            row["stat"].configure(text="请求中…", foreground=theme.c("muted"))
        elif state == "done":
            row["stat"].configure(text="")
        elif state == "error":
            row["stat"].configure(text="失败", foreground=theme.c("error"))
        elif state == "stopped":
            row["stat"].configure(text="已停止", foreground=theme.c("muted"))

    def _opt_done(self, out):
        self._opt_busy = False
        self._opt_stop = None
        if self._opt_panel is None:                # 用户已 ✕ 停止：面板不复活、不缓存
            return
        self._opt_scenario = out.get("scenario") or self._opt_scenario
        if out.get("results"):
            self._opt_cache = {"key": (self._opt_scenario, self._opt_original),
                               "out": out}
        ok_n = sum(1 for r in out.get("results") or [] if r.get("ok"))
        if ok_n:
            self._opt_head("点选一个版本替换输入；✕ 回退至原始输入（%s）"
                           % (out.get("model") or ""))
        else:
            self._opt_head("三路都失败了：%s" % (out.get("error") or "原因见各行"))
        for r in out.get("results") or []:
            self._opt_render_row(r.get("strategy"), r)
        # 发送/附件保持禁用：面板还开着，输入框藏在它后面，放开「发送」会把
        # 看不见的原文发出去。恢复只发生在面板收起时（_opt_pick / _opt_close_clicked）
        self._opt_star_refresh()

    @staticmethod
    def _opt_preview(text, n=PREVIEW_CHARS):
        text = str(text or "")
        return text if len(text) <= n else text[:n] + "…"

    def _opt_render_row(self, strategy, r):
        row = self._opt_rows.get(strategy)
        if row is None or not r:
            return
        stat, prev, retry = row["stat"], row["prev"], row["retry"]
        if r.get("ok"):
            row["ok"] = True
            row["full"] = r.get("text") or ""
            row["suspect"] = bool(r.get("suspect"))
            retry.pack_forget()
            prev.configure(text=self._opt_preview(row["full"]),
                           foreground=theme.c("body"), cursor="hand2")
            widgets.bind_tip(prev, lambda s=strategy: self._opt_tip_for(s))
            notes = []
            if r.get("suspect"):
                notes.append("疑似偏离原意")
            if r.get("thinking_seen"):
                notes.append("模型未关思考")
            stat.configure(text=" · ".join(notes),
                           foreground=theme.c("warn") if notes else theme.c("muted"))
            if r.get("suspect"):
                widgets.bind_tip(
                    stat, lambda r=r: "可能丢了原文关键信息：%s"
                    % ("、".join(r.get("missing") or []) or "（未列出）"))
        else:
            row["ok"] = False
            row["full"] = ""
            prev.configure(text=r.get("error") or "请求失败",
                           foreground=theme.c("error"), cursor="")
            stat.configure(text="已停止" if r.get("stopped") else "失败",
                           foreground=theme.c("muted") if r.get("stopped")
                           else theme.c("error"))
            if not r.get("stopped"):
                retry.pack(side="right")

    def _opt_tip_for(self, strategy):
        row = self._opt_rows.get(strategy) or {}
        full = str(row.get("full") or "")
        return full if len(full) <= TIP_CHARS else full[:TIP_CHARS] + "…"

    # ---------------------------------------------------------------- 选定 / 回退
    def _opt_pick(self, strategy):
        row = self._opt_rows.get(strategy)
        if row is None or not row.get("ok") or self._opt_busy:
            return
        out = (self._opt_cache or {}).get("out") or {}
        self.input.delete("1.0", "end")
        self.input.insert("1.0", row["full"])
        promptopt.record_choice(self._opt_scenario or out.get("scenario", ""),
                                strategy, model=out.get("model", ""),
                                suspect=bool(row.get("suspect")))
        self._opt_close_panel()
        self._set_busy_ui(self._busy)              # 面板收了才把发送/附件放回来
        self._opt_star_refresh()
        self.input.focus_set()

    def _opt_close_clicked(self):
        """×：优化中 = 停止并收起（不算选择、不记账）；出结果后 = 回退至原始输入。"""
        if self._opt_busy:
            if self._opt_stop is not None:
                self._opt_stop.set()
            self._opt_close_panel()
            self._set_busy_ui(self._busy)
            self._opt_star_refresh()
            return
        out = (self._opt_cache or {}).get("out") or {}
        promptopt.record_choice(self._opt_scenario or out.get("scenario", ""),
                                promptopt.CHOICE_ORIGINAL,
                                model=out.get("model", ""))
        self._opt_close_panel()
        self._set_busy_ui(self._busy)
        self._opt_star_refresh()
        self.input.focus_set()

    def _opt_reopen_cached(self):
        """「使用已有结果」：原面板重现，一个请求都不发。"""
        cache = self._opt_cache or {}
        out = cache.get("out")
        if not out:
            return
        self._opt_original = cache["key"][1]
        self._opt_scenario = out.get("scenario") or self._opt_scenario
        self._opt_open_panel()
        self.send_btn.configure(state="disabled")
        self.attach_btn.configure(state="disabled")
        ok_n = sum(1 for r in out.get("results") or [] if r.get("ok"))
        if ok_n:
            self._opt_head("点选一个版本替换输入；× 回退至原始输入（%s）"
                           % (out.get("model") or ""))
        else:
            self._opt_head("三路都失败了：%s" % (out.get("error") or "原因见各行"))
        for r in out.get("results") or []:
            self._opt_render_row(r.get("strategy"), r)
        self._opt_star_refresh()

    # ---------------------------------------------------------------- 单路重试
    def _opt_retry_one(self, strategy):
        if self._opt_busy or self._opt_panel is None:
            return
        out = (self._opt_cache or {}).get("out")
        if not out:
            return
        row = self._opt_rows.get(strategy)
        if row is None:
            return
        text = self._opt_original
        scen = self._opt_scenario or out.get("scenario") or "text"
        self._opt_busy = True
        self._opt_stop = threading.Event()
        row["stat"].configure(text="请求中…", foreground=theme.c("muted"))
        row["retry"].pack_forget()
        row["prev"].configure(text="", foreground=theme.c("body"))
        self.send_btn.configure(state="disabled")

        def worker(stop=self._opt_stop):
            try:
                target, err = optimize.resolve_target(self.cfg)
                if target is None:
                    r = {"ok": False, "text": "", "error": err, "stopped": False,
                         "thinking_seen": False, "suspect": False, "missing": []}
                else:
                    msgs = promptopt.build_messages(scen, text, strategy)
                    r = optimize.optimize_once(
                        target, msgs, promptopt.TEMPERATURE[strategy],
                        promptopt.plan_max_tokens(text, strategy), stop)
                    if r.get("ok"):
                        fid = promptopt.fidelity_check(text, r.get("text") or "")
                        r["suspect"], r["missing"] = fid["suspect"], fid["missing"]
                    r["strategy"] = strategy
            except Exception as e:
                r = {"ok": False, "text": "", "stopped": False, "thinking_seen": False,
                     "suspect": False, "missing": [],
                     "error": "重试出错：%s: %s" % (type(e).__name__, e)}
            self._ui_q.put(lambda: self._opt_retry_done(strategy, r))

        threading.Thread(target=worker, daemon=True,
                         name="promptopt-retry").start()

    def _opt_retry_done(self, strategy, r):
        self._opt_busy = False
        self._opt_stop = None
        if self._opt_panel is None:
            return
        out = (self._opt_cache or {}).get("out")
        if out:
            for i, old in enumerate(out.get("results") or []):
                if old.get("strategy") == strategy:
                    out["results"][i] = r
                    break
        self._opt_render_row(strategy, r)
        # 面板仍开着：发送/附件保持禁用（口径同 _opt_done），收起时才恢复
        self._opt_star_refresh()
