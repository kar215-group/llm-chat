# -*- coding: utf-8 -*-
"""llm_console.ui.dialogs — 独立对话框：退出确认、（已废弃但保留的）独立生图窗口"""

import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

from ..core.config import save_config
from ..core.models import scan_models
from ..core.media import build_img_cmd


class ExitDialog(tk.Toplevel):
    """模态二选：停止服务并退出 / 取消（继续运行）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("关闭 LLM 对话台")
        self.resizable(False, False)
        self.result = None

        ttk.Label(self, text="后台模型服务正在运行。",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(padx=28, pady=(18, 4))
        ttk.Label(self, text="关闭程序会同时停止后台服务。", foreground="#666666").pack(
            padx=28, pady=(0, 14))
        box = ttk.Frame(self)
        box.pack(padx=28, pady=(0, 14))
        ttk.Button(box, text="停止服务并退出", width=16,
                   command=lambda: self._done("stop")).pack(side="left", padx=4)
        ttk.Button(box, text="取消", width=10,
                   command=lambda: self._done("cancel")).pack(side="left", padx=4)
        ttk.Label(self, text="「取消」返回主窗口，程序与服务继续运行。Esc 也可取消。",
                  foreground="#999999",
                  font=("Microsoft YaHei UI", 9)).pack(padx=28, pady=(0, 14))

        self.bind("<Escape>", lambda e: self._done("cancel"))
        self.protocol("WM_DELETE_WINDOW", lambda: self._done("cancel"))
        self.transient(parent)
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 3)
        self.geometry("+%d+%d" % (x, y))
        self.grab_set()

    def _done(self, result):
        self.result = result
        self.destroy()

class ImageDialog(tk.Toplevel):
    """生图窗口：调用 sd.cpp（Qwen-Image 2.1）按需生成并预览。

    设计：sd-cli 按需启动（生成完自动退出、显存自动释放），
    与 llama-server 聊天服务可共存（流式模式显存占用 ~2GB）。
    """

    QUANTS = ["Q6_K", "Q5_0"]     # （保留说明用途）常用量化档位
    SIZES = ["1024x1024", "1024x768", "768x1024", "768x768", "512x512"]

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.cfg = cfg
        self._proc = None
        self._closing = False
        self._t0 = None
        self._photo = None            # 持引用防 GC
        self._out_path = None
        self._steps = 12
        self._phase = "准备"
        self._q = queue.Queue()

        self.title("生图 - Qwen-Image 2.1")
        self.geometry("800x860")
        self.minsize(720, 760)
        self.transient(parent)

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(top, text="提示词（支持中英文，可要求图内渲染文字）",
                  anchor="w").pack(fill="x")
        self.prompt = tk.Text(top, height=5, font=("Microsoft YaHei UI", 10),
                              relief="flat", highlightthickness=1,
                              highlightbackground="#cccccc")
        self.prompt.pack(fill="x", pady=(2, 4))

        grid = ttk.Frame(self)
        grid.pack(fill="x", padx=12, pady=4)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        self.v_steps = tk.StringVar(value=str(self.cfg.get("img_steps", 12)))
        self.v_size = tk.StringVar(value=str(self.cfg.get("img_size", "1024x1024")))
        self.v_cfg = tk.StringVar(value=str(self.cfg.get("img_cfg", 2.5)))
        self.v_seed = tk.StringVar(value=str(self.cfg.get("img_seed", -1)))
        _d, _chat, images = scan_models(self.cfg)
        self._image_models = images
        img_names = [os.path.basename(p) for p in images]
        saved = self.cfg.get("img_model_file", "")
        if saved not in img_names and img_names:
            saved = img_names[0]
        self.v_model_file = tk.StringVar(value=saved)

        ttk.Label(grid, text="步数", anchor="w").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 4))
        ttk.Spinbox(grid, from_=4, to=50, textvariable=self.v_steps, width=6).grid(
            row=0, column=1, sticky="w")
        ttk.Label(grid, text="分辨率", anchor="w").grid(row=0, column=2, sticky="w", pady=3, padx=(12, 4))
        ttk.Combobox(grid, textvariable=self.v_size, values=self.SIZES,
                     width=12, state="readonly").grid(row=0, column=3, sticky="w")
        ttk.Label(grid, text="CFG", anchor="w").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 4))
        ttk.Entry(grid, textvariable=self.v_cfg, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(grid, text="种子", anchor="w").grid(row=1, column=2, sticky="w", pady=3, padx=(12, 4))
        ttk.Entry(grid, textvariable=self.v_seed, width=12).grid(row=1, column=3, sticky="w")
        ttk.Label(grid, text="生图模型", anchor="w").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 4))
        if img_names:
            ttk.Combobox(grid, textvariable=self.v_model_file, values=img_names,
                         width=30, state="readonly").grid(row=2, column=1, columnspan=3, sticky="w")
        else:
            ttk.Label(grid, text="（未找到生图模型）", foreground="#c01c28").grid(
                row=2, column=1, columnspan=3, sticky="w")
        ttk.Label(self, text="步数少 = 出图快（8 步 ~1m20s，12 步 ~1m50s，20 步细节更多）；"
                             "Q6_K 质量优先 / Q5_0 更省显存；CFG 2.5 为官方推荐；种子 -1 随机。"
                             "生图模型放在「llm modle\\生图」文件夹，可从模型菜单切换或在设置里管理。",
                  foreground="#808080", wraplength=740, justify="left",
                  font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=12, pady=(0, 4))

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=4)
        self.gen_btn = ttk.Button(bar, text="生成", command=self._start, width=10)
        self.gen_btn.pack(side="left", padx=3)
        self.cancel_btn = ttk.Button(bar, text="取消生成", command=self._cancel,
                                     state="disabled", width=10)
        self.cancel_btn.pack(side="left", padx=3)
        ttk.Button(bar, text="打开输出文件夹", width=14,
                   command=self._open_outdir).pack(side="left", padx=3)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.status_var = tk.StringVar(value="就绪。输入提示词后点「生成」。")
        ttk.Label(self, textvariable=self.status_var, foreground="#555555",
                  wraplength=750, justify="left").pack(fill="x", padx=12, pady=4)

        self.preview = ttk.Label(self, text="（生成结果预览）", anchor="center",
                                 relief="groove")
        self.preview.pack(fill="both", expand=True, padx=12, pady=(4, 4))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=12, pady=(2, 10))
        self.open_btn = ttk.Button(bottom, text="打开原图", command=self._open_img,
                                   state="disabled", width=12)
        self.open_btn.pack(side="right", padx=4)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll)

    # ---- 生成 ----
    def _start(self):
        if self._proc is not None:
            return
        sd = self.cfg.get("sd_dir", "")
        cli = os.path.join(sd, "sd-cli.exe")
        prompt = self.prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showinfo("生图", "请先输入提示词。", parent=self)
            return
        if not os.path.isfile(cli):
            messagebox.showwarning("生图", "未找到生图引擎：\n%s\n\n"
                                   "在 设置 → 生图 里把「sd.cpp 目录」指向你部署的 sd.cpp"
                                   "（该目录需包含 sd-cli.exe）。" % (cli or "（还没填目录）"),
                                   parent=self)
            return
        try:
            steps = max(1, int(float(self.v_steps.get() or 12)))
        except Exception:
            steps = 12
        try:
            cfg_scale = float(self.v_cfg.get() or 2.5)
        except Exception:
            cfg_scale = 2.5
        try:
            seed = int(float(self.v_seed.get() or -1))
        except Exception:
            seed = -1
        size = self.v_size.get()
        img_file = self.v_model_file.get()
        if not img_file:
            messagebox.showwarning("生图", "没有可用的生图模型。\n"
                                   "请把 Qwen-Image 的 .gguf 放到「生图」模型文件夹。", parent=self)
            return
        diffusion_path = os.path.join(self.cfg.get("image_model_dir", ""), img_file)
        if not os.path.isfile(diffusion_path):
            messagebox.showwarning("生图", "生图模型文件不存在：\n%s" % diffusion_path, parent=self)
            return

        outdir = os.path.join(sd, "output")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, time.strftime("img_%Y%m%d_%H%M%S") + ".png")

        # 记住本次设置
        self.cfg["img_model_file"] = img_file
        self.cfg["img_steps"] = steps
        self.cfg["img_size"] = size
        self.cfg["img_cfg"] = cfg_scale
        self.cfg["img_seed"] = seed
        save_config(self.cfg)

        cmd = build_img_cmd(self.cfg, prompt, out, steps, size, diffusion_path,
                            cfg_scale, seed)
        self._out_path = out
        self._steps = steps
        self._saw_sampling = False
        self._saw_decode = False
        self._t0 = time.time()
        self._phase = "加载模型"
        self.gen_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.progress.configure(value=0, maximum=steps)
        self.status_var.set("启动引擎、加载模型…（权重载入约 30~60 秒）")
        self.preview.configure(image="", text="（生成中…）")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=flags, cwd=sd)
        except Exception as e:
            self._proc = None
            self.status_var.set("启动失败: %s" % e)
            self._reset_buttons()
            return
        threading.Thread(target=self._reader, args=(self._proc,),
                         daemon=True).start()

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                if self._closing:
                    break
                self._q.put(("line", line.rstrip()))
        except Exception:
            pass
        try:
            rc = proc.wait()
        except Exception:
            rc = -1
        self._q.put(("exit", rc))

    def _cancel(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _open_img(self):
        if self._out_path and os.path.isfile(self._out_path):
            os.startfile(self._out_path)

    def _open_outdir(self):
        sd = self.cfg.get("sd_dir", "")
        outdir = os.path.join(sd, "output")
        os.makedirs(outdir, exist_ok=True)
        os.startfile(outdir)

    def _show_preview(self, path):
        try:
            img = tk.PhotoImage(file=path)
        except Exception as e:
            self.status_var.set("预览失败: %s（原图已保存）" % e)
            return
        factor = max(1, round(img.width() / 500.0))
        if factor > 1:
            try:
                img = img.subsample(factor, factor)
            except Exception:
                pass
        self._photo = img
        self.preview.configure(image=img, text="")

    def _reset_buttons(self):
        self.gen_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    # ---- 轮询 ----
    def _poll(self):
        if self._closing:
            return
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "line":
                    self._handle_line(val)
                elif kind == "exit":
                    self._handle_exit(val)
        except queue.Empty:
            pass
        if self._proc is not None and self._t0 is not None:
            self.status_var.set("%s · 已耗时 %ds" % (self._phase,
                                                     int(time.time() - self._t0)))
        self.after(120, self._poll)

    def _handle_line(self, line):
        if "generating image" in line:
            self._saw_sampling = True
            self._phase = "采样中"
            self.progress.configure(maximum=self._steps)
            return
        if "decoding" in line and "latent" in line:
            self._saw_decode = True
            self._phase = "解码图片"
            return
        m = re.search(r"(\d+)/(\d+) - ", line)
        if m and self._saw_sampling and not self._saw_decode:
            cur, total = int(m.group(1)), int(m.group(2))
            self.progress.configure(maximum=total, value=cur)
            self._phase = "采样中 %d/%d 步" % (cur, total)
        elif m and self._saw_decode:
            self._phase = "解码图片（分块）"
        elif "save result image" in line:
            self._phase = "保存"
        elif "Version:" in line:
            self._phase = "加载模型"

    def _handle_exit(self, rc):
        elapsed = int(time.time() - self._t0) if getattr(self, "_t0", None) else 0
        proc, self._proc = self._proc, None
        self._reset_buttons()
        if self._closing:
            return
        if rc == 0 and self._out_path and os.path.isfile(self._out_path):
            self.progress.configure(value=self.progress["maximum"])
            self.status_var.set("完成 · 耗时 %d 秒 · 已保存 %s" % (elapsed, self._out_path))
            self.open_btn.configure(state="normal")
            self._show_preview(self._out_path)
        elif rc == 0:
            self.status_var.set("完成但未找到输出文件，请检查输出目录。")
        else:
            self.progress.configure(value=0)
            self.status_var.set("已取消或生成失败（退出码 %s）。可调整参数后重试。" % rc)

    def _on_close(self):
        if self._proc is not None:
            if not messagebox.askyesno("生图", "图片还在生成中，取消生成并关闭窗口？",
                                       parent=self):
                return
            try:
                self._proc.kill()
            except Exception:
                pass
        self._closing = True
        self.destroy()
