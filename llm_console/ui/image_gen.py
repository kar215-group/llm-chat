# -*- coding: utf-8 -*-
"""llm_console.ui.image_gen — 界面 Mixin：聊天流内生图链路（进度行、结果内嵌、取消）"""

import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

from ..core.models import scan_models
from ..core.media import build_img_cmd, resolve_img_files


class ImageGenMixin:
    """App 的聊天流内生图职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    # ---- 聊天流内生图（方案 A：页面形态不变，输出内嵌对话流）----
    def _start_chat_image(self, prompt, ref_img=None):
        sd = self.cfg.get("sd_dir", "")
        cli = os.path.join(sd, "sd-cli.exe")
        if not os.path.isfile(cli):
            self._append("\n[提示] 未找到生图引擎 %s —— 请在 设置 → 生图 里把「sd.cpp 目录」"
                         "指向你部署的 sd.cpp（内含 sd-cli.exe）。\n"
                         % (cli or "（还没填目录）"), "error")
            return
        img_file = self.cfg.get("img_model_file", "")
        if not img_file:
            _d, _c, images = scan_models(self.cfg)
            img_file = os.path.basename(images[0]) if images else ""
        diffusion = os.path.join(self.cfg.get("image_model_dir", ""), img_file) if img_file else ""
        if not img_file or not os.path.isfile(diffusion):
            self._append("\n[提示] 未找到生图模型：请把 Qwen-Image 的 .gguf 放入"
                         "「llm modle\\生图」文件夹，或在模型菜单中重新选择。\n", "error")
            return
        # VAE / 文本编码器自动发现 + Popen 前预检：缺件就地报错，别把不存在的
        # 路径交给引擎再吃一次"退出码 1"（生图链路没有失败回显，黑盒更难查）
        files = resolve_img_files(self.cfg, diffusion)
        missing = []
        if not files["llm"]:
            missing.append("文本编码器（如 Qwen3VL 的 .gguf，引擎的 --llm）")
        if not files["vae"]:
            missing.append("图像 VAE（.safetensors，引擎的 --vae）")
        if missing:
            self._append("\n[提示] 未找到生图组件：%s——请放进生图模型目录"
                         "（设置 → 生图 的「生图大模型文件夹」）。\n"
                         % "、".join(missing), "error")
            return
        try:
            steps = max(1, int(self.cfg.get("img_steps", 12) or 12))
        except Exception:
            steps = 12
        size = str(self.cfg.get("img_size", "1024x1024"))
        try:
            cfg_scale = float(self.cfg.get("img_cfg", 2.5) or 2.5)
        except Exception:
            cfg_scale = 2.5
        outdir = os.path.join(sd, "output")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, time.strftime("img_%Y%m%d_%H%M%S") + ".png")

        self._append("\n【你】\n" + prompt + "\n", "user")
        if ref_img and os.path.isfile(ref_img):
            self._append_image(ref_img, max_w=320)
            self._append("[图生图] 参考图：%s（重绘强度 %s）\n"
                         % (os.path.basename(ref_img),
                            self.cfg.get("img_strength", 0.6)), "meta")
        self._img_gen += 1
        mygen = self._img_gen
        mark = "imgprog%d" % mygen
        self.chat.mark_set(mark, "end-1c")
        self.chat.mark_gravity(mark, "left")   # 进度文字长在 mark 右侧，mark 钉在行首（gravity=right 会导致堆叠）
        self._img_mark = mark
        self._img_busy = True
        self._img_out = out
        self._t0 = time.time()
        self._saw_sampling = False
        self._saw_decode = False
        self._stop_flag = threading.Event()
        self._set_busy_ui(True)
        cmd = build_img_cmd(self.cfg, prompt, out, steps, size, diffusion, cfg_scale, -1,
                            init_img=ref_img, vae=files["vae"], llm=files["llm"])
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._img_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=flags, cwd=sd)
        except Exception as e:
            self._img_busy = False
            self._img_proc = None
            self._set_busy_ui(False)
            self._append("[错误] 启动生图引擎失败: %s\n" % e, "error")
            return
        threading.Thread(target=self._img_reader, args=(self._img_proc, mygen),
                         daemon=True).start()

    def _img_reader(self, proc, gen):
        try:
            for line in proc.stdout:
                if self._closing:
                    break
                self._img_q.put(("line", line.rstrip()))
        except Exception:
            pass
        try:
            rc = proc.wait()
        except Exception:
            rc = -1
        self._img_q.put(("exit", rc, gen))

    def _update_img_progress_line(self, text):
        mark = getattr(self, "_img_mark", None)
        if not mark:
            return
        self.chat.configure(state="normal")
        try:
            self.chat.delete(mark, "end")
            self.chat.insert(mark, "\n" + text, "meta")
        except Exception:
            pass
        self.chat.configure(state="disabled")

    def _handle_img_line(self, line):
        if self._stop_flag is not None and self._stop_flag.is_set():
            return
        elapsed = int(time.time() - self._t0) if getattr(self, "_t0", None) else 0
        if "generating image" in line:
            self._phase = "采样"
            return
        m = re.search(r"(\d+)/(\d+) - ", line)
        steps = max(1, int(self.cfg.get("img_steps", 12) or 12))
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if (not getattr(self, "_saw_decode", False)
                    and total == steps and cur <= total):
                # 只认与设定步数一致的进度（区分模型加载段与 VAE 分块）
                self._update_img_progress_line(
                    "🎨 生成中… %d/%d 步 · %ds" % (cur, total, elapsed))
            return
        if "decoding" in line and "latent" in line:
            self._saw_decode = True
            self._update_img_progress_line("🎨 解码图片… %ds" % elapsed)

    def _handle_img_exit(self, rc, gen):
        if gen != self._img_gen:
            return                      # 过期任务（已被取消并替换），忽略其回调
        elapsed = int(time.time() - self._t0) if getattr(self, "_t0", None) else 0
        self._img_busy = False
        self._img_proc = None
        self._set_busy_ui(False)
        out = getattr(self, "_img_out", "")
        cancelled = self._stop_flag is not None and self._stop_flag.is_set()
        # 清除进度行（mark 到末尾；行结构由进度文字的前导换行维持）
        mark = getattr(self, "_img_mark", None)
        if mark:
            self.chat.configure(state="normal")
            try:
                self.chat.delete(mark, "end")
            except Exception:
                pass
            self.chat.configure(state="disabled")
        if cancelled:
            self._append("🎨 [已取消生成]\n", "meta")
            self._append("────────────────\n", "meta")
            return
        if rc == 0 and out and os.path.isfile(out):
            # 关键：回显必须处于 normal 状态——disabled 的 Text 会静默丢弃
            # image_create/insert/window_create（此前图片不显示的根因）。
            # 布局：提示词行 → 进度行（原位替换）→ 图片独立成行，按钮在图片右侧同行
            self.chat.configure(state="normal")
            try:
                img = tk.PhotoImage(file=out)
                factor = max(1, round(img.width() / 500.0))
                if factor > 1:
                    img = img.subsample(factor, factor)
                self._remember_photo(out, img)             # 持引用防 GC（按路径为键）
                self.chat.insert("end", "\n", "meta")      # 图片前补换行：独立成行
                self.chat.image_create("end", image=img)
                self.chat.insert("end", "  ", "meta")      # 图片与按钮的间距
                btn = ttk.Button(self.chat, text="打开原图",
                                 command=lambda p=out: os.startfile(p))
                self.chat.window_create("end", window=btn)
                self.chat.insert("end", "  ", "meta")
                btn2 = ttk.Button(self.chat, text="打开所在文件夹",
                                  command=lambda p=out: self._open_containing(p))
                self.chat.window_create("end", window=btn2)
                self.chat.insert("end", "\n🎨 已保存：%s · 耗时 %ds\n" % (out, elapsed), "meta")
                self.chat.insert("end", "────────────────\n", "meta")
                self.chat.see("end")
            except Exception as e:
                self._append("🎨 [回显失败: %s] 图片已保存：%s\n" % (e, out), "error")
            self.chat.configure(state="disabled")
        else:
            self._append("🎨 生成失败或已取消（退出码 %s）。\n" % rc, "error")
            self._append("────────────────\n", "meta")

    def _cancel_chat_image(self):
        """立即中止当前生图任务：杀进程、世代递增（旧回调失效）、清理进度行。"""
        if self._img_proc is not None:
            try:
                self._img_proc.kill()
            except Exception:
                pass
        # 置停止标志：让仍在队列里的进度行事件被 _handle_img_line 丢弃。
        # 否则它们会继续用旧 mark 重画进度行——而 delete(mark,"end") 会连带
        # 抹掉刚写入的"已取消生成"提示，并在界面留下一条永远不消失的进度。
        if self._stop_flag is not None:
            self._stop_flag.set()
        self._img_gen += 1
        self._img_busy = False
        self._img_proc = None
        mark = getattr(self, "_img_mark", None)
        if mark:
            self.chat.configure(state="normal")
            try:
                self.chat.delete(mark, "end")
            except Exception:
                pass
            self.chat.configure(state="disabled")
