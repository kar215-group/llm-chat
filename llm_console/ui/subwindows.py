# -*- coding: utf-8 -*-
"""llm_console.ui.subwindows — 从「设置」派生出去的独立小窗口（Mixin）。

现在有两个：**诊断**（这台机器怎么了：程序与配置在哪儿、运行库、一键诊断、报告怎么拿出去）
与**成本预估算**（云端单价按模型填，点「写入」即落盘）。它们的共同点：

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
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from ..core import crashlog, diagnose, providers
from ..core.config import APP_DIR, CONFIG_PATH, save_config
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
    """App 的「诊断」与「成本预估算」两个独立窗口（Mixin）；self._xxx 经 MRO 解析。"""

    def open_cost_window(self, parent=None):
        """「成本预估算」次级窗口：单价**按模型**填，点「写入单价」立即落盘。

        为什么不跟设置窗口底部那个「保存」共用一次提交：单价是"给某一次生成估费用"的独立
        事实，跟"这一页别的档位改不改"没关系（同坑 61 —— 声明与提交不是一件事）。这里点
        「写入」就 save_config 落地，关窗不隐式保存任何东西，所以顶部那行话把这件事写明了。

        为什么单价挂模型不挂服务商：同一家下 happyhorse-1.0 与 1.1 不同价，MiniMax 的 H3
        按秒、Hailuo 按条 —— 连**计费单位**都是模型属性，挂在服务商上算出来的就是错价。

        模型下拉只列**已勾进主页面菜单**的媒体模型（W 定的口径）：没进菜单的模型本来发不出去，
        给它定价没有意义；媒体模型大多不在各家 /models 清单里，要先进 选择模型 → 直接加入。
        """
        host = parent or self.root
        cfg = self.cfg
        win = tk.Toplevel(host)
        win.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
        win.title("成本预估算")
        win.geometry("620x470")
        win.minsize(560, 420)
        win.transient(host)
        ttk.Label(win, text="单价按模型记，只用于提交前的费用预估。不填就在确认框与对话流里"
                            "明说「以账单为准」，不编数字。点「写入单价」立即生效，"
                            "不需要到底部「保存」。",
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
        unit_var = tk.StringVar(value="秒")
        status_var = tk.StringVar(value="")
        _pl = providers.provider_labels(cfg)

        combo_p = ttk.Combobox(body, state="readonly", width=30,
                               values=[lb for lb, _p in _pl])
        combo_m = ttk.Combobox(body, state="readonly", width=30,
                               textvariable=model_var, values=[])
        combo_u = ttk.Combobox(body, state="readonly", width=8, textvariable=unit_var,
                               values=list(providers.PRICE_UNITS))
        # 已填清单用 Text 而不是"拼全部模型名"的 Label（坑 92：状态类 Label 必须定长）
        lst = tk.Text(body, height=8, width=46, font=("Microsoft YaHei UI", 9),
                      state="disabled", wrap="none")

        def refresh_list():
            got = providers.price_table(providers.get_provider(cfg, pid_var.get()))
            lst.configure(state="normal")
            lst.delete("1.0", "end")
            lst.insert("1.0", "（这家一个都没填）" if not got else "")
            for m in sorted(got):
                lst.insert("end", "%s    %g 元/%s\n" % (m, got[m]["price"], got[m]["unit"]))
            lst.configure(state="disabled")

        def load_price():
            """选中模型就把已填的单价与单位顶上来；没填过按能力给个起始单位。"""
            p = providers.get_provider(cfg, pid_var.get()) or {}
            per, unit = providers.price_of(p, model_var.get())
            if per > 0:
                price_var.set("%g" % per)
                unit_var.set(unit)
            else:
                price_var.set("")
                unit_var.set(providers.default_unit(
                    model_var.get(), providers.model_kind_of(p, model_var.get())))

        def refresh_models():
            ms = providers.media_menu_models(providers.get_provider(cfg, pid_var.get()))
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
                status_var.set("这家还没有勾进菜单的生图 / 生视频模型："
                               "先去「选择模型」里加进来（媒体模型名要用「直接加入」）。")
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
            if not providers.set_price(cfg, pid, model, per, unit):
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
