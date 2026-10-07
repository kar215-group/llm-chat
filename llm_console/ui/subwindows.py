# -*- coding: utf-8 -*-
"""llm_console.ui.subwindows — 从「设置」派生出去的独立小窗口（Mixin）。

现在有两个：**诊断**（这台机器怎么了：程序与配置在哪儿、运行库、一键诊断、报告怎么拿出去）
与**成本估算**（云端单价按模型 × 按链路填，点「写入」即落盘）。它们的共同点：

  · **不占设置页的版面** —— 设置页是固定外框（1080x740、横向不可滚），往里塞只会挤掉
    别人；所以两者都是"按钮开一个独立 Toplevel"（同一个形状，W 2026-10-01 定的口径）。
  · **自己管自己的控件与数据**：不碰设置页的 `v` / `save_hooks`，落盘直接走 `save_config`
    （诊断那只**只在探到新的显卡信息时**补写一次，其余一律只读）。

为什么单独成模块（v41）：这两块原来在 `ui/settings.py` 里，而那个文件已经 2600+ 行，
§5.5 给的拆分判据是"某区块有了第二个调用方，或单文件涨到约 3000 行"。它们是设置页里
**唯一不依赖 `open_settings` 闭包**的两块（其余区块都牵着 `v` / `state` / `save_hooks`
那张网），抽出来零风险，也把"设置窗口"这个职责还回设置页自己。

⚠ **依赖方向**：`settings → subwindows → core`，本模块**不许** import settings
（单向，别绕回环，§9.1 的逐模块导入检查会抓）。

界面纪律与设置页一致：子线程只投 `_ui_q`（坑 54）；状态类 Label 定长（坑 92）；
按钮开窗口前先 `lift()` 已存在的那个（重开 = 抬到最前，不叠第二个）。
"""
import os
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from ..core import (crashlog, diagnose, providers, secrets, selfupdate, updater)
from ..core.config import APP_DIR, APP_VERSION, CONFIG_PATH, save_config
from . import widgets


def open_file(path, what):
    """打开一个**文件**（错误日志这类）。

    不能复用设置页那个 `_open_outdir`：它会先 `makedirs(path)`，那正好把
    `llm-chat-error.log` 变成一个同名的**空目录**，日志反而再也写不进去了。
    """
    p = str(path or "").strip()
    if not p or not os.path.isfile(p):
        messagebox.showinfo(what, "还没有这个文件：\n  %s\n\n没有过异常退出是好事。" % p)
        return False
    try:
        os.startfile(p)                     # 交给系统默认程序（.log 一般是记事本）
        return True
    except Exception as e:
        messagebox.showwarning(what, "打不开 %s：\n  %s" % (p, e))
        return False


class SubWindowMixin:
    """App 的「诊断」与「成本估算」两个独立窗口（Mixin）；self._xxx 经 MRO 解析。"""

    def open_cost_window(self, kind=None, parent=None):
        """「成本估算」次级窗口：单价**按模型 × 按链路**填，点「写入单价」立即落盘。

        2026-10-07 W 定：原来云端生图 / 生视频共用一个「成本预估算」（一张表、单位
        秒/张/条混着），现在按链路拆开 —— 文本（元/千token）/ 生图（元/张）/
        生视频（元/秒、元/条）各开各的窗、各查各的表（providers._PRICE_KEY），
        计费单位下拉也只列本链路认得的那几个。本地链路不产生 API 费用，没有这个入口。

        为什么不跟设置窗口底部那个「保存」共用一次提交：单价是"给某一次生成估费用"的独立
        事实，跟"这一页别的档位改不改"没关系（同坑 61 —— 声明与提交不是一件事）。这里点
        「写入」就 save_config 落地，关窗不隐式保存任何东西，所以顶部那行话把这件事写明了。

        为什么单价挂模型不挂服务商：同一家下 happyhorse-1.0 与 1.1 不同价，MiniMax 的 H3
        按秒、Hailuo 按条 —— 连**计费单位**都是模型属性，挂在服务商上算出来的就是错价。

        模型下拉只列**已勾进主页面菜单**的本链路模型（W 定的口径）：没进菜单的模型本来发不出去，
        给它定价没有意义；媒体模型大多不在各家 /models 清单里，要先进 选择模型 → 直接加入。
        """
        kind = kind if kind in providers.KINDS else providers.KIND_IMAGE
        _kind_name = providers.KIND_LABEL[kind]
        host = parent or self.root
        cfg = self.cfg
        win = tk.Toplevel(host)
        win.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
        win.title("成本估算 · 云端%s" % _kind_name)
        win.geometry("620x470")
        win.minsize(560, 420)
        win.transient(host)
        _unit_words = {providers.KIND_TEXT: "元/千token（输入 + 输出合并按这一个均价估）",
                       providers.KIND_IMAGE: "元/张",
                       providers.KIND_VIDEO: "元/秒 或 元/条（同一家两种都有，按模型选）"}
        _effect_words = {
            providers.KIND_TEXT: "填了就在每轮「用量」行尾附一句预估费用",
            providers.KIND_IMAGE: "填了就在提交前把价格打进对话流",
            providers.KIND_VIDEO: "填没填都会在提交前弹一次确认，填了就报金额"}
        ttk.Label(win, text="云端%s的单价按模型记，只用于费用预估（%s）。"
                            "不填就明说「以账单为准」，不编数字。%s。"
                            "点「写入单价」立即生效，不需要到底部「保存」。"
                  % (_kind_name, _unit_words[kind], _effect_words[kind]),
                  wraplength=580, justify="left", font=("Microsoft YaHei UI", 9)).pack(
            side="top", fill="x", padx=14, pady=(12, 4))
        body = ttk.Frame(win)
        body.pack(side="top", fill="both", expand=True, padx=14, pady=(4, 12))
        rows = {"i": 0}

        def lrow(label, widget, colspan=2):
            i = rows["i"]
            rows["i"] += 1
            ttk.Label(body, text=label, width=12, anchor="w").grid(
                row=i, column=0, sticky="nw", padx=(0, 8), pady=5)
            widget.grid(row=i, column=1, columnspan=colspan, sticky="w", pady=5)

        pid_var = tk.StringVar(value="")
        model_var = tk.StringVar(value="")
        price_var = tk.StringVar(value="")
        unit_var = tk.StringVar(value=providers.default_unit(kind=kind))
        status_var = tk.StringVar(value="")
        _pl = providers.provider_labels(cfg)

        combo_p = ttk.Combobox(body, state="readonly", width=30,
                               values=[lb for lb, _p in _pl])
        combo_m = ttk.Combobox(body, state="readonly", width=30,
                               textvariable=model_var, values=[])
        # 计费单位只列**本链路**认得的（拆分的另一半：单位跟着链路走，不再混在一格）
        combo_u = ttk.Combobox(body, state="readonly", width=8, textvariable=unit_var,
                               values=list(providers.UNITS_BY_KIND[kind]))
        # 已填清单用 Text 而不是"拼全部模型名"的 Label（坑 92：状态类 Label 必须定长）
        lst = tk.Text(body, height=8, width=46, font=("Microsoft YaHei UI", 9),
                      state="disabled", wrap="none")

        def refresh_list():
            got = providers.price_table(providers.get_provider(cfg, pid_var.get()), kind)
            lst.configure(state="normal")
            lst.delete("1.0", "end")
            lst.insert("1.0", "（这家一个都没填）" if not got else "")
            for m in sorted(got):
                lst.insert("end", "%s    %g 元/%s\n" % (m, got[m]["price"], got[m]["unit"]))
            lst.configure(state="disabled")

        def load_price():
            """选中模型就把已填的单价与单位顶上来；没填过按链路给个起始单位。"""
            p = providers.get_provider(cfg, pid_var.get()) or {}
            per, unit = providers.price_of(p, model_var.get(), kind)
            if per > 0:
                price_var.set("%g" % per)
                unit_var.set(unit)
            else:
                price_var.set("")
                unit_var.set(providers.default_unit(kind=kind))

        def refresh_models():
            p = providers.get_provider(cfg, pid_var.get())
            ms = (providers.text_menu_models(p) if kind == providers.KIND_TEXT
                  else providers.media_menu_models(p, kind))
            combo_m.configure(values=ms)
            model_var.set(model_var.get() if model_var.get() in ms
                          else (ms[0] if ms else ""))
            load_price()
            return ms

        def pick_provider(*_a):
            for lb, pid in _pl:
                if lb == combo_p.get():
                    pid_var.set(pid)
                    break
            else:
                pid_var.set("")
            if not refresh_models():
                status_var.set(
                    "这家还没有勾进菜单的%s模型：先去「选择模型」里加进来%s。"
                    % (_kind_name,
                       "（媒体模型名要用「直接加入」）"
                       if kind != providers.KIND_TEXT else ""))
            else:
                status_var.set("")
            refresh_list()

        def write_price(clear=False):
            pid, model = pid_var.get(), model_var.get()
            if not pid or not model:
                status_var.set("先把服务商和模型都选上。")
                return
            per, unit = 0.0, ""
            if not clear:
                try:
                    per = float(str(price_var.get()).strip())
                except Exception:
                    status_var.set("单价要填数字（元）。要清掉就点「清除该模型单价」。")
                    return
                if per <= 0:
                    status_var.set("单价要大于 0。要清掉就点「清除该模型单价」。")
                    return
                unit = unit_var.get()
            if not providers.set_price(cfg, pid, model, per, unit, kind):
                status_var.set("这个服务商不在了，没写进去。")
                return
            save_config(cfg)
            refresh_list()
            status_var.set("已清除「%s」的单价。" % model if per <= 0
                           else "已写入「%s」%.4g 元/%s。" % (model, per, unit))

        def pick_model(*_a):
            load_price()
            status_var.set("")

        combo_p.bind("<<ComboboxSelected>>", pick_provider)
        combo_m.bind("<<ComboboxSelected>>", pick_model)
        lrow("服务商", combo_p)
        lrow("模型", combo_m)
        lrow("单价（元）", ttk.Entry(body, textvariable=price_var, width=10))
        lrow("计费单位", combo_u)
        lrow("已填的模型", lst)
        btns = ttk.Frame(body)
        lrow("", btns)
        ttk.Button(btns, text="写入单价", width=12,
                   command=lambda: write_price(False)).pack(side="left")
        ttk.Button(btns, text="清除该模型单价", width=14,
                   command=lambda: write_price(True)).pack(side="left", padx=(8, 0))
        ttk.Label(btns, textvariable=status_var, foreground="#808080", wraplength=230,
                  justify="left", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(10, 0))

        if _pl:
            combo_p.current(0)
            pick_provider()
        else:
            status_var.set("还没有可用的服务商。")
        widgets.center_on(win, host)   # 摆到触发它的设置页正中，别落在屏幕左上角
        return win

    def open_diag_window(self, parent=None):
        """「诊断」次级页面：这份程序是哪来的、这台机器缺什么、报告怎么拿出去。

        从「关于」页搬进来（W 2026-10-01 定）：关于页回答"这是什么软件"，这一页回答
        "我这台机器怎么了"。前者是给所有人看的一屏，后者是一堆路径 + 一份能贴走的报告，
        混在一起时真正要的「新手引导」按钮被挤到第十行下面。

        窗口宽度按**实测**定：标签列 8 字（最长那项「程序所在文件夹」7 字）+ 结果框
        52 字符 ≈ 572px，加两边 14 的边距要 685px 以上 —— 原来在 802px 的内容区里
        用 14 字标签列，换到自己的窗口里不重算就会顶出右边界（坑 113 同族）。
        """
        host = parent or self.root
        if self._diag_win is not None:
            try:
                if self._diag_win.winfo_exists():
                    self._diag_win.lift()
                    return self._diag_win
            except Exception:
                self._diag_win = None

        win = tk.Toplevel(host)
        win.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
        win.title("诊断")
        # 6 行路径 + 16 行结果框 + 一排四个按钮：实测 660 高会把按钮那排裁掉半截
        # （截图量出来的，不是估的），给到 740 才全露出来
        win.geometry("760x740")
        win.minsize(700, 560)
        win.transient(host)
        self._diag_win = win

        body = ttk.Frame(win)
        body.pack(side="top", fill="both", expand=True, padx=14, pady=(12, 12))
        body.columnconfigure(2, weight=1)
        rows = {"i": 0}

        def infolab(label, value):
            i = rows["i"]
            rows["i"] += 1
            ttk.Label(body, text=label, width=8, anchor="w").grid(
                row=i, column=0, sticky="nw", padx=(0, 8), pady=3)
            ttk.Label(body, text=str(value), wraplength=560, justify="left",
                      foreground="#5a5a5a").grid(row=i, column=1, columnspan=2,
                                                 sticky="w", pady=3)

        frozen = bool(getattr(sys, "frozen", False))
        infolab("运行方式", "下载的 exe（双击即用）" if frozen
                else "源码运行（python / pythonw）")
        infolab("程序位置", APP_DIR)
        infolab("配置文件", CONFIG_PATH)
        infolab("密钥文件", os.path.join(APP_DIR, "secrets.json"))
        infolab("运行库", "%s%s · Tk %s" % ("自带 Python " if frozen else "Python ",
                                            sys.version.split()[0], tk.TkVersion))
        infolab("错误日志", crashlog.log_path() if os.path.isfile(crashlog.log_path())
                else "还没有异常记录")

        status = tk.StringVar(value="")
        box = tk.Text(body, height=16, width=52, font=("Microsoft YaHei UI", 9),
                      state="normal", wrap="word")
        box.insert("1.0", "点「一键诊断」检查这台机器：引擎在不在、模型放对没有、"
                          "目录能不能写、端口有没有被占。\n"
                          "这一步只读本地信息 —— 不联网、不启动推理引擎、不碰显卡。")
        box.configure(state="disabled")
        i = rows["i"]
        rows["i"] += 1
        ttk.Label(body, text="诊断结果", width=8, anchor="w").grid(
            row=i, column=0, sticky="nw", padx=(0, 8), pady=(10, 3))
        box.grid(row=i, column=1, columnspan=2, sticky="w")

        def show(text):
            if not box.winfo_exists():
                return                      # 窗口可能已经关了：结果没人接就丢掉
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", text)
            box.configure(state="disabled")

        def do_diag():
            """诊断放线程里跑（nvidia-smi 最坏会等 10 秒，不能冻住界面），
            结果经 _ui_q 回主线程画 —— 子线程绝不碰控件（坑 54）。"""
            status.set("正在检查…")
            cfg = dict(self.cfg)
            mine = bool(getattr(self, "proxy", None) and
                        getattr(self.proxy, "httpd", None))
            srv = bool(getattr(self, "_proc", None))

            def merge_gpu():
                """把这一趟探到的显卡信息**合并**回活的 cfg（只补空的那两项）。

                不能在诊断线程里 `save_config(快照)`：快照是点击那一刻的，nvidia-smi
                最坏 10 秒才回来，那期间用户保存的设置会被整份盖回去（P1 数据丢失）。
                """
                if not str(self.cfg.get("gpu_name", "") or "") and cfg.get("gpu_name"):
                    self.cfg["gpu_name"] = cfg["gpu_name"]
                    self.cfg["vram_gb"] = cfg.get("vram_gb", 0)
                    save_config(self.cfg)

            def work():
                try:
                    rs = diagnose.run_checks(cfg, proxy_running=mine, server_running=srv)
                    f, w = diagnose.summary(rs)
                    head = ("共 %d 项：%s\n\n" % (
                        len(rs),
                        "没发现阻塞问题（%d 项提醒）" % w if not f
                        else "%d 项需要处理、%d 项提醒" % (f, w)))
                    txt = head + diagnose.render_text(rs)
                    self._ui_q.put(lambda: (show(txt), merge_gpu(), status.set(
                        "有 %d 项需要处理" % f if f else "没发现阻塞问题")))
                except Exception as e:
                    msg = "诊断没跑完：%s: %s" % (type(e).__name__, e)
                    self._ui_q.put(lambda: status.set(msg))
            threading.Thread(target=work, daemon=True).start()

        def copy_report():
            body_txt = box.get("1.0", "end").strip()
            try:
                win.clipboard_clear()
                win.clipboard_append(body_txt)
                status.set("诊断报告已复制到剪贴板，贴给别人就能求助。")
            except Exception as e:
                status.set("复制失败（%s）——可以改用「保存诊断报告」。" % e)

        def save_report():
            body_txt = box.get("1.0", "end").strip()
            path = os.path.join(APP_DIR, "diagnose.txt")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body_txt + "\n")
                status.set("已保存：%s" % path)
            except Exception as e:
                status.set("保存失败：%s: %s" % (type(e).__name__, e))

        bf = ttk.Frame(body)
        i = rows["i"]
        rows["i"] += 1
        bf.grid(row=i, column=1, columnspan=2, sticky="w", pady=(8, 0))
        for text, fn in (("一键诊断", do_diag), ("复制诊断报告", copy_report),
                         ("保存诊断报告", save_report),
                         ("打开错误日志", lambda: open_file(crashlog.log_path(), "错误日志"))):
            ttk.Button(bf, text=text, command=fn).pack(side="left", padx=(0, 6))
        ttk.Label(body, textvariable=status, foreground="#808080", wraplength=520,
                  justify="left", font=("Microsoft YaHei UI", 9)).grid(
            row=rows["i"], column=1, columnspan=2, sticky="w", pady=(6, 0))
        widgets.center_on(win, host)   # 摆到触发它的设置页正中，别落在屏幕左上角
        return win

    def open_update_window(self, info, auto=False, parent=None):
        """「发现新版本」次级窗口（自替换更新，2026-10-05 W 点名）：提示 → 「立即更新」
        下载（进度条，可取消）→ sha256 对上、解压、新版 `--version` 烟测通过 →
        提示"要关闭程序" → 确认后换 exe、自动打开新版。

        `auto=True` = 自动检查（进关于页自动查 / 开发者后台轮询）触发的弹窗：用户把它
        关掉（X 或「暂不」）就把 tag 记进 `cfg["upd_dismissed"]`，同一个版本不再自动弹
        第二次，出现更新的 tag 照旧弹；手动点「检查更新」进来的（`auto=False`）不受这份
        记录限制。单实例：已经开着就抬到最前，不叠第二个（与诊断窗同款纪律）。

        下载在子线程、结果一律经 `_ui_q` 回主线程（坑 54）；窗口可能被人手点 X ——
        每个 UI 回调都先问 `winfo_exists`，暂存目录的清理在 `_close` 与 worker 各兜一边，
        谁先到谁清，绝不留半个下载目录。
        """
        info = dict(info or {})
        tag = str(info.get("tag") or "")
        ch = info.get("channel") or updater.CHANNEL_STABLE
        host = parent or self.root
        if getattr(self, "_upd_win", None) is not None:
            try:
                if self._upd_win.winfo_exists():
                    self._upd_win.lift()
                    return self._upd_win
            except Exception:
                self._upd_win = None

        win = tk.Toplevel(host)
        win.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
        win.title("发现新版本")
        W, H = 620, 520         # 一版正式公告约 18 行，560x430 装不下（滚动条是兜底不是常态）
        win.geometry("%dx%d" % (W, H))
        win.minsize(560, 440)
        win.transient(host)
        self._upd_win = win

        cfg = self.cfg
        exe_here = selfupdate.cur_exe_path()   # 源码运行 = None：没有 exe 可换
        # 动手前的复核基准（CAS）：另一个实例可能在这扇窗开着的几分钟里已经把程序换过了，
        # 真到「确认并更新」那一步要再对一遍，别把刚装好的新版又当旧版翻一遍。
        expect = selfupdate.exe_stamp(exe_here) if exe_here else None
        stop_flag = threading.Event()
        state = {"stage": "prompt", "staging": "", "exe": "", "url": ""}

        top = ttk.Frame(win)
        top.pack(side="top", fill="x", padx=14, pady=(12, 4))
        head_lab = ttk.Label(top, text="发现新版本：%s" % (info.get("name") or tag),
                             font=("Microsoft YaHei UI", 11, "bold"))
        head_lab.pack(side="top", anchor="w")
        ver_lab = ttk.Label(
            top, text="本机 %s → 新版 %s　·　发布于 %s　·　%s"
            % (updater.display_version(info.get("current") or APP_VERSION),
               updater.display_version(tag),
               info.get("published") or "日期未知",
               updater.channel_label(ch)),
            foreground="#5a6a7a", font=("Microsoft YaHei UI", 9),
            wraplength=524, justify="left")
        ver_lab.pack(side="top", anchor="w", pady=(2, 0))

        body = ttk.Frame(win)
        body.pack(side="top", fill="both", expand=True, padx=14, pady=(4, 4))
        nfr = ttk.Frame(body)
        nfr.pack(side="top", fill="both", expand=True)
        notes = tk.Text(nfr, height=7, font=("Microsoft YaHei UI", 9), wrap="char")
        notes_sb = ttk.Scrollbar(nfr, command=notes.yview)
        notes.configure(yscrollcommand=notes_sb.set)
        # 滚动条先 pack：Text 带 expand 会把整条 cavity 吃掉，后 pack 的滚动条只剩 1x1
        notes_sb.pack(side="right", fill="y")
        notes.pack(side="left", fill="both", expand=True)
        notes.tag_configure("sec", font=("Microsoft YaHei UI", 8))

        def _show_notes(txt):
            """公告写进框里。小节名单独挂 `sec` 标签（比正文小一号）。

            ⚠ `state="disabled"` 的 Text 会**静默吞掉** `insert`（坑 159）：先开写、
            写完再关，顺序反了框就永远是空的。
            """
            lines = str(txt or "").split("\n")
            notes.configure(state="normal")
            notes.delete("1.0", "end")
            for i, ln in enumerate(lines):
                seg = ln + ("\n" if i + 1 < len(lines) else "")
                if ln.strip() in updater.NOTE_SECTIONS:
                    notes.insert("end", seg, "sec")
                else:
                    notes.insert("end", seg)
            notes.configure(state="disabled")
            notes.see("1.0")

        _show_notes(info.get("notes") or "（这个 Release 没写说明）")

        bar_var = tk.DoubleVar(value=0.0)
        bar = ttk.Progressbar(body, maximum=100.0, variable=bar_var, length=320)
        status = tk.StringVar(value="下载用的是 GitHub 发布包，先校验再替换，"
                                   "替换前会再确认一次。")
        st_lab = ttk.Label(body, textvariable=status, foreground="#5a6a7a",
                           wraplength=520, justify="left",
                           font=("Microsoft YaHei UI", 9))
        st_lab.pack(side="top", anchor="w", pady=(8, 0))
        bar.pack(side="top", anchor="w", pady=(6, 0))
        bar.pack_forget()       # 进下载阶段才显示；提示阶段放着只是占一行

        if exe_here is None:
            ttk.Label(body, text="这是源码运行：自动更新只对下载的 exe 生效，"
                                 "更新请 git pull。", foreground="#b06000",
                      font=("Microsoft YaHei UI", 9)).pack(
                side="top", anchor="w", pady=(6, 0))

        btns = ttk.Frame(win)
        btns.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        btn_go = ttk.Button(btns, text="立即更新", width=12)
        btn_go.pack(side="right")
        btn_stop = ttk.Button(btns, text="暂不", width=10)
        btn_stop.pack(side="right", padx=(0, 8))
        if exe_here is None:
            btn_go.configure(state="disabled")

        # ---- 人工出口（2026-10-06）：自动更新这条路失败时，手动那条永远走得通。
        # 国内直连 objects.githubusercontent.com 常失败 —— 窗口里得有一个不用猜的出路。
        def _release_page_url():
            return (str(info.get("url") or "") or
                    updater.PAGE_RELEASES % updater.REPO)

        def _open_release_page():
            url = _release_page_url()
            try:
                import webbrowser
                if webbrowser.open(url):
                    return
                raise RuntimeError("浏览器没响应")
            except Exception as e:
                messagebox.showinfo(
                    "打开发布页",
                    "浏览器没打开（%s）。\n把这个地址粘贴到浏览器里也一样：\n%s" % (e, url))

        def _copy_link():
            url = state.get("url") or _release_page_url()
            try:
                win.clipboard_clear()
                win.clipboard_append(url)
                status.set("已复制%s，可以在浏览器或下载工具里取回来：%s"
                           % ("安装包直链" if state.get("url") else "发布页地址", url))
                st_lab.configure(foreground="#5a6a7a")
            except Exception as e:
                status.set("复制失败（%s）—— 地址：%s" % (e, url))

        ttk.Button(btns, text="打开发布页", width=11,
                   command=_open_release_page).pack(side="left")
        ttk.Button(btns, text="复制下载链接", width=13,
                   command=_copy_link).pack(side="left", padx=(8, 0))

        def _record_dismiss():
            """自动弹的这扇窗被关掉 = 用户对**这个版本**说"别再弹"（落配置，重启仍算数）。"""
            if auto and tag and tag != str(cfg.get("upd_dismissed", "") or ""):
                cfg["upd_dismissed"] = tag
                try:
                    save_config(cfg)
                except Exception:
                    pass

        def _cleanup_staging():
            if state["staging"]:
                selfupdate.cleanup(state["staging"])
                state["staging"] = ""

        def _close():
            """窗口被手点 X：下载中也一样 —— 先停下载、清暂存，本机版本不动。"""
            stop_flag.set()
            _cleanup_staging()
            _record_dismiss()
            try:
                win.destroy()
            except Exception:
                pass
        win.protocol("WM_DELETE_WINDOW", _close)

        def _restore_prompt(why, color="#b00020"):
            """一次尝试结束（多半是失败）：清暂存、回到提示态。窗口可能已关。"""
            _cleanup_staging()
            state["stage"] = "prompt"
            if not win.winfo_exists():
                return
            bar.pack_forget()
            bar_var.set(0.0)
            status.set(why)
            st_lab.configure(foreground=color)
            btn_go.configure(text="立即更新",
                             state="normal" if exe_here else "disabled")
            btn_stop.configure(text="暂不", state="normal")

        def _dl_progress(txt, pct):
            if not win.winfo_exists():
                return
            status.set(txt)
            if pct is not None:
                bar_var.set(pct)

        def _dl_ready(staging, exe_path):
            state["staging"] = staging
            state["exe"] = exe_path
            state["stage"] = "confirm"
            if not win.winfo_exists():
                _cleanup_staging()      # 窗口没等到这一刻就关了：包不留下
                return
            status.set("下载完成，校验通过。更新需要关闭当前程序"
                       "（正在运行的服务也会一并停止），然后自动打开新版本。")
            st_lab.configure(foreground="#1a7f37")
            btn_go.configure(text="确认并更新", state="normal")
            btn_stop.configure(text="取消", state="normal")

        def _apply():
            """确认并更新：换 exe → 拉新 → 写哨兵 → 正常退出收尾。

            全程握着跨进程更新闸（try_lock）：两份程序同时"确认并更新"时，后点的
            会被闸住，不会把先点那份刚装好的新版当旧版改名（2026-10-06）。
            拉新失败要**回滚**（删掉放歪的新 exe、旧版改回原名）再报错 —— 顺序不能倒：
            先回滚后报错，用户看到的错误落地的就是"一切照旧"的状态。
            """
            state["stage"] = "apply"
            btn_go.configure(state="disabled")
            btn_stop.configure(state="disabled")
            status.set("正在替换程序文件…")
            lock, lwhy = selfupdate.try_lock(exe_here)
            if lock is None:
                if lwhy == "locked":
                    _restore_prompt("另一个正在运行的实例在更新。请先关掉它，"
                                    "或等它更新完重开程序。")
                else:
                    _restore_prompt("建不了更新锁（%s）—— 程序所在文件夹多半写不进去。"
                                    "把本程序挪到可写的普通文件夹再试，或到发布页手动下载。"
                                    % lwhy)
                return
            try:
                ok, why = selfupdate.apply_update(state["exe"], exe_here, expect=expect)
                if not ok:
                    _restore_prompt(why)
                    return
                ok2, why2 = selfupdate.launch(exe_here)
                if not ok2:
                    selfupdate.rollback(exe_here)
                    _restore_prompt("新程序没能启动（%s），已回滚到旧版，本机版本没有动。"
                                    % why2)
                    return
                selfupdate.mark_updated(exe_here)
                _cleanup_staging()
                self.on_close(force=True)   # 正常退出收尾（停服务 / 存对话记录），不再弹二选
            finally:
                selfupdate.release_lock(lock, exe_here)

        def _retarget(dl):
            """点「立即更新」重新拉清单时，线上可能已经又发了新版 —— 窗口写的版本
            必须跟着实际要装的对上（2026-10-06），别让用户"装的是 A′、看到的是 A"。"""
            if not win.winfo_exists():
                return
            head_lab.configure(text="发现新版本：%s" % (dl.get("name") or dl.get("tag") or tag))
            ver_lab.configure(
                text="本机 %s → 新版 %s　·　发布于 %s　·　%s\n"
                     "（弹窗之后线上又发了新版本，现在装的是上面这个最新的）"
                % (updater.display_version(info.get("current") or APP_VERSION),
                   updater.display_version(dl.get("tag") or tag),
                   dl.get("published") or "日期未知",
                   updater.channel_label(ch)))
            if dl.get("notes"):
                _show_notes(dl["notes"])    # 公告也得跟着换成实际要装的那一版

        def _start_download():
            state["stage"] = "download"
            btn_go.configure(state="disabled")
            btn_stop.configure(text="取消下载", state="normal")
            bar.pack(side="top", anchor="w", pady=(6, 0))
            bar_var.set(0.0)
            st_lab.configure(foreground="#5a6a7a")
            status.set("正在向 GitHub 查询安装包…")

            def work():
                try:
                    # 与关于页共用同一份 ETag 缓存：能弹到这一步，多半刚查过 → 304，不吃额度
                    dl = selfupdate.find_download(
                        ch, token=secrets.get_github_token(),
                        cache=getattr(self, "_upd_check", {}).get("cache"))
                except Exception as e:  # UpdaterError / SelfUpdateError 都已是人话
                    self._ui_q.put(lambda w=str(e): _restore_prompt(w))
                    return
                state["url"] = str(dl.get("url") or "")   # 人工出口的「复制下载链接」用
                # 磁盘空间先看一眼：zip 落 %TEMP%（暂存里 zip + 解出的 exe 约两份），
                # 新 exe 还要进程序目录 —— 两处都可能满，满了更新必然半途而废（2026-10-06）
                need = int(dl.get("size") or 0)
                if need > 0:
                    import tempfile
                    for where, d, factor in (
                            ("临时文件夹（%TEMP%）", tempfile.gettempdir(), 2.2),
                            ("程序所在文件夹", os.path.dirname(exe_here or "") or None, 1.2)):
                        ok_d, free = selfupdate.disk_ok(d, int(need * factor))
                        if not ok_d:
                            self._ui_q.put(lambda w=(
                                "磁盘空间不够：装这次更新大约要 %.0f MB，%s 只剩 %.0f MB。"
                                "清出一些空间再试，或到发布页手动下载。"
                                % (need * factor / 1048576.0, where, free / 1048576.0)):
                                _restore_prompt(w))
                            return
                if dl.get("tag") and dl["tag"] != tag:
                    self._ui_q.put(lambda d=dict(dl): _retarget(d))
                staging = selfupdate.staging_dir()
                zip_path = os.path.join(staging, "update.zip")

                def emit(txt):
                    # 下载器的 emit 只给文字；进度百分比从"（a / b MB）"里认出来，
                    # 认不出就不动进度条（进度是锦上添花，文字才是承诺）
                    m = re.search(r"（([\d.]+) / ([\d.]+) MB）", txt)
                    pct = None
                    if m:
                        total = max(float(m.group(2)), 0.1)
                        pct = min(100.0, float(m.group(1)) / total * 100.0)
                    self._ui_q.put(lambda t=txt, p=pct: _dl_progress(t, p))

                ok, why = selfupdate.download_zip(dl["url"], zip_path, emit=emit,
                                                  stop_flag=stop_flag,
                                                  digest=dl["digest"])
                if not ok:
                    selfupdate.cleanup(staging)
                    self._ui_q.put(lambda w=("已取消下载。" if stop_flag.is_set() else why):
                                   _restore_prompt(w))
                    return
                exe_path, werr = selfupdate.extract_exe(zip_path, staging)
                if exe_path:
                    good, wsmoke = selfupdate.smoke_test(exe_path, dl["tag"])
                    if not good:
                        exe_path, werr = None, wsmoke
                if not exe_path:
                    selfupdate.cleanup(staging)
                    self._ui_q.put(lambda w=werr: _restore_prompt(w))
                    return
                self._ui_q.put(lambda s=staging, e=exe_path: _dl_ready(s, e))

            threading.Thread(target=work, daemon=True).start()

        def _stop():
            if state["stage"] == "download":
                stop_flag.set()         # worker 收到就停：删 .part、清目录、回"已取消"
                status.set("正在取消…")
            elif state["stage"] == "confirm":
                # 下载完又不想装：清暂存回提示态，本机版本没动
                _cleanup_staging()
                state["stage"] = "prompt"
                bar.pack_forget()
                bar_var.set(0.0)
                status.set("已取消，本机版本没有动。下载的文件已清掉。")
                st_lab.configure(foreground="#5a6a7a")
                btn_go.configure(text="立即更新",
                                 state="normal" if exe_here else "disabled")
                btn_stop.configure(text="暂不", state="normal")
            else:
                _close()

        def _go():
            if state["stage"] == "prompt":
                _start_download()
            elif state["stage"] == "confirm":
                _apply()

        btn_go.configure(command=_go)
        btn_stop.configure(command=_stop)
        widgets.center_on(win, host, size=(W, H))   # 尺寸显式交给它，原因见坑 161
        return win
