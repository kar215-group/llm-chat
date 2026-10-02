# -*- coding: utf-8 -*-
"""llm_console.ui.video_gen — 界面 Mixin：聊天流内生视频链路（进度行、结果行 + 打开按钮、取消）"""

import os
import re
import subprocess
import threading
import time
from tkinter import ttk, messagebox

from ..core import cloudjobs, config, providers
from ..core.media import build_video_cmd, resolve_video_files
from ..connection import cloud_media


class VideoGenMixin:
    """App 的聊天流内生视频职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    # ---- 聊天流内生视频（与生图同一套形态：进度原位刷新、可取消、结果行进对话流）----
    def _start_chat_video(self, prompt, ref_img=None):
        # 云端生视频走异步任务（三期）：提交 → 轮询 → 下载，完全不碰本地引擎。
        if providers.is_cloud(self.cfg):
            return self._start_cloud_video(prompt)
        sd = self.cfg.get("sd_dir", "")
        cli = os.path.join(sd, "sd-cli.exe")
        if not os.path.isfile(cli):
            self._append("\n[提示] 未找到生视频引擎 %s（生图与生视频共用 sd.cpp）—— "
                         "请在 设置 → 生视频 里把「sd.cpp 目录」指向你部署的 sd.cpp，"
                         "并确认该版本支持视频生成。\n"
                         % (cli or "（还没填目录）"), "error")
            return
        files = resolve_video_files(self.cfg)
        if files["missing"]:
            self._append("\n[提示] 视频链路组件不全，缺：\n  · %s\n\n"
                         "把文件放进「%s」后重新发一次即可（文件名可在 设置 → 生视频 里指定）。"
                         "扩散主体、文本编码器、视频 VAE 缺一不可，VAE 最容易漏下。"
                         % ("\n  · ".join(files["missing"]),
                            self.cfg.get("video_model_dir") or "生视频"), "error")
            return
        try:
            frames = max(1, int(self.cfg.get("vid_frames", 17) or 17))
        except Exception:
            frames = 17
        try:
            fps = max(1, int(self.cfg.get("vid_fps", 24) or 24))
        except Exception:
            fps = 24
        try:
            steps = max(1, int(self.cfg.get("vid_steps", 20) or 20))
        except Exception:
            steps = 20
        try:
            cfg_scale = float(self.cfg.get("vid_cfg", 5.0) or 5.0)
        except Exception:
            cfg_scale = 5.0
        size = str(self.cfg.get("vid_size", "512x512")).strip() or "512x512"
        if "x" not in size.lower():
            size = "512x512"
        ext = str(self.cfg.get("vid_format", "webm")).strip().lstrip(".").lower()
        if ext not in ("webm", "avi", "webp"):
            ext = "webm"          # sd-cli 的单文件视频输出只认这三种容器

        outdir = os.path.join(sd, "video")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, time.strftime("vid_%Y%m%d_%H%M%S") + "." + ext)

        # 预检都过了才消费输入框与附件（坑 133，同生图链路）
        self.input.delete("1.0", "end")
        self.clear_attachment()
        self._append("\n【你】\n" + prompt + "\n", "user")
        if ref_img and os.path.isfile(ref_img):
            self._append_image(ref_img, max_w=320)
            self._append("[图生视频] 首帧：%s（引擎会自动裁剪缩放到 %s）\n"
                         % (os.path.basename(ref_img), size), "meta")
        self._vid_gen += 1
        mygen = self._vid_gen
        mark = "vidprog%d" % mygen
        self.chat.mark_set(mark, "end-1c")
        self.chat.mark_gravity(mark, "left")   # 与生图进度行同一套：mark 钉行首，否则 delete 删不掉旧进度
        self._vid_mark = mark
        self._vid_busy = True
        self._vid_out = out
        self._vid_t0 = time.time()
        self._vid_saw_decode = False
        self._vid_tail = []
        self._vid_phase = "load"
        self._stop_flag = threading.Event()
        self._set_busy_ui(True)
        cmd = build_video_cmd(self.cfg, prompt, out, files, frames, fps, size,
                              steps, cfg_scale, -1, ref_img=ref_img)
        # 每次任务留一份完整引擎日志（与产物同名 + .log）：命令行 + 引擎原话 + 退出码
        self._vid_log = out + ".log"
        self._cloud_vid = False
        self._cloud_tid = ""
        try:
            with open(self._vid_log, "w", encoding="utf-8", errors="replace") as lf:
                lf.write("=== argv ===\n")
                for i, a in enumerate(cmd):
                    lf.write("[%2d] %s\n" % (i, a))
                lf.write("=== cwd=%s ｜ out=%s ｜ 提交于 %s ===\n\n"
                         % (sd, out, time.strftime("%Y-%m-%d %H:%M:%S")))
                lf.flush()
        except Exception:
            self._vid_log = ""
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._vid_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=flags, cwd=sd)
        except Exception as e:
            self._vid_busy = False
            self._vid_proc = None
            self._set_busy_ui(False)
            self._append("[错误] 启动生视频引擎失败: %s\n" % e, "error")
            return
        threading.Thread(target=self._vid_reader,
                         args=(self._vid_proc, mygen, self._vid_log, self._vid_t0),
                         daemon=True).start()

    # ---- 云端生视频（三期：异步任务 提交 → 轮询 → 下载）----
    def _confirm_cloud_video_spend(self):
        """提交前把档位与费用摆出来问一次——云端生视频是**先建单后交付**的。

        为什么只拦生视频不拦生图：一条十几秒的视频按秒计费可以到几十元，而且任务一旦
        进入运行态就取消不掉；生图是同步的、单次几分钱量级，每次都弹窗只会让人练成
        不看内容直接按回车（生图的费用改成就近打在对话流里，见 image_gen）。

        **无论单价填没填都要问**：单价没填时这句改成"以服务商账单为准"，
        不能因为算不出钱就省掉这次确认 —— 算不出钱恰恰是最该问的情况。
        """
        provider = providers.current_provider(self.cfg) or {}
        sp = providers.split_cloud_id(self.cfg.get("model", ""))
        model = sp[1] if sp else ""
        try:
            dur = int(self.cfg.get("cloud_video_duration", 5) or 5)
        except Exception:
            dur = 5
        res = str(self.cfg.get("cloud_video_resolution", "") or "").strip() or "（服务端默认）"
        return messagebox.askyesno(
            "云端生视频",
            "要提交一条云端视频任务吗？\n\n"
            "  模型：%s\n  时长：%d 秒\n  分辨率：%s\n  费用：%s\n\n"
            "注意：任务开始运行后就取消不掉了，只能不再等它——"
            "结果之后仍可以在对话里点「取回」拿回来。\n"
            "确认提交吗？" % (model or "（未选模型）",
                             dur, res,
                             providers.media_price_note(provider, providers.KIND_VIDEO,
                                                        dur, model)))

    def _start_cloud_video(self, prompt):
        provider = providers.current_provider(self.cfg)
        sp = providers.split_cloud_id(self.cfg.get("model", ""))
        model = sp[1] if sp else ""
        err = cloud_media.check(self.cfg, provider, model, "video")
        if err:
            self._append("\n[云端生视频] %s\n" % err, "error")
            return
        outdir = config.cloud_media_dir(self.cfg, "video")
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self._append("\n[云端生视频] 存放目录建不出来：%s\n"
                         "  目录可在 设置 → 云端模型 → 生图 / 生视频 里改。\n" % e, "error")
            return
        # 扩展名只是默认值：真实落地名由产物 URL 决定（服务端给的是 MP4）
        out = os.path.join(outdir, time.strftime("vid_%Y%m%d_%H%M%S") + ".mp4")
        # 预检（check + 建目录 + 费用确认已在调用方）都过了才消费输入框与附件（坑 133）
        self.input.delete("1.0", "end")
        self.clear_attachment()
        self._append("\n【你】\n" + prompt + "\n", "user")
        gen, q, stop = self._cloud_begin("video")
        self._cloud_pid = str(provider["id"])
        self._vid_out = out
        self._vid_log = out + ".log"
        self._update_vid_progress_line("☁ 提交云端任务… %s（%s）"
                                       % (providers.short_of(model),
                                          provider.get("name") or ""))
        threading.Thread(target=self._cloud_video_worker,
                         args=(provider, model, prompt, out, gen, q, stop),
                         daemon=True).start()

    def _cloud_video_worker(self, provider, model, prompt, dest, gen, q, stop_flag,
                            tid=""):
        """子线程：提交 / 轮询 / 下载，事件只往队列里投（坑 11/54 的铁律）。

        `tid` 非空 = 从台账取回旧任务，**不再提交一次**——重复提交就是重复扣钱。
        """
        def _int(key, dft):
            try:
                return int(self.cfg.get(key, dft) or dft)
            except Exception:
                return dft
        log_path = dest + ".log"
        if not tid:
            r = cloud_media.submit_video(
                self.cfg, provider, model, prompt,
                resolution=str(self.cfg.get("cloud_video_resolution", "") or ""),
                duration=_int("cloud_video_duration", 5),
                ratio=str(self.cfg.get("cloud_video_ratio", "") or ""),
                negative=str(self.cfg.get("cloud_video_negative", "") or ""),
                seed=_int("vid_seed", -1), log_path=log_path,
                title="云端生视频提交 %s" % model)
            if not r["ok"]:
                q.put(("line", "[ERROR] 提交失败：%s" % r["error"]))
                q.put(("exit", 1, gen))
                return
            tid = r["task_id"]
            cloudjobs.add_job(tid, "video", provider["id"], provider.get("name", ""),
                              model, prompt, dest,
                              resolution=str(self.cfg.get("cloud_video_resolution", "") or ""),
                              duration=_int("cloud_video_duration", 5))
        # 取消与取回都要找得到它：这个属性由主线程的 _cancel_chat_video 读
        self._cloud_tid = tid

        def persist(status, raw=None):
            cloudjobs.update_job(tid, status=status)

        res = cloud_media.wait_task(self.cfg, provider, tid, dest, kind="video",
                                    emit=q.put, stop_flag=stop_flag, persist=persist,
                                    model=model)
        paths = list(res.get("paths") or [])
        if paths:
            self._vid_out = paths[0]    # 真实扩展名由服务端 URL 决定，_handle_vid_exit 要按它判成功
            cloudjobs.mark_done(tid, paths=paths, urls=res.get("urls"),
                                seconds=res.get("seconds", 0))
        else:
            status = str(res.get("status") or "failed")
            cloudjobs.update_job(tid, status=status,
                                 error=str(res.get("error") or "")[:600])
            q.put(("line", "[ERROR] %s" % (res.get("error") or "云端生视频失败")))
        q.put(("exit", 0 if paths else 1, gen))

    def _vid_reader(self, proc, gen, log_path, t0):
        # 完整引擎输出落盘：内存里的 _vid_tail 只有 12 行，够回答"为什么失败"，
        # 不够回答"引擎说自己成功了却没写出文件"这类问题（开发机踩过，见 §11 第 2 条）。
        # 日志路径走参数而不是 self._vid_log：取消后立刻重开一次任务时，旧线程会把
        # 自己的输出追加进新任务的日志里。
        log = None
        try:
            # 行缓冲：应用被强杀时也要留下"引擎最后说了什么"，否则 8KB 缓冲里的内容全丢
            log = open(log_path, "a", encoding="utf-8", errors="replace",
                       buffering=1) if log_path else None
        except Exception:
            log = None
        t_start = t0 or time.time()
        try:
            for line in proc.stdout:
                if self._closing:
                    break
                if log is not None:
                    try:
                        log.write(line if line.endswith("\n") else line + "\n")
                    except Exception:
                        pass
                self._vid_q.put(("line", line.rstrip()))
        except Exception:
            pass
        try:
            rc = proc.wait()
        except Exception:
            rc = -1
        if log is not None:
            try:
                log.write("\n=== 退出码 %s ｜ 耗时 %.1fs ===\n" % (rc, time.time() - self._vid_t0))
                log.close()
            except Exception:
                pass
        self._vid_q.put(("exit", rc, gen))

    def _update_vid_progress_line(self, text):
        mark = getattr(self, "_vid_mark", None)
        if not mark:
            return
        self.chat.configure(state="normal")
        try:
            self.chat.delete(mark, "end")
            self.chat.insert(mark, "\n" + text, "meta")   # 前导换行自带：维持"进度只占一行"的行结构
        except Exception:
            pass
        self.chat.configure(state="disabled")

    def _handle_vid_line(self, line):
        # 滚动保留引擎输出末尾：失败时进度行会被整行删掉，没有这份记录就只剩一个
        # "退出码 1"，用户无从判断原因（开发机踩过：--mode / 负向提示词两处报错都看不见）
        if line.strip():
            self._vid_tail.append(line.strip()[:220])
            if len(self._vid_tail) > 12:
                del self._vid_tail[0]
        if self._stop_flag is not None and self._stop_flag.is_set():
            return
        elapsed = int(time.time() - self._vid_t0) if getattr(self, "_vid_t0", None) else 0
        low = line.lower()
        m = re.search(r"(\d+)/(\d+) - ", line)
        try:
            steps = max(1, int(self.cfg.get("vid_steps", 20) or 20))
        except Exception:
            steps = 20
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if not getattr(self, "_vid_saw_decode", False) and total == steps and cur <= total:
                self._vid_phase = "sample"
                self._update_vid_progress_line(
                    "🎬 生成中… %d/%d 步 · %ds" % (cur, total, elapsed))
            elif total != steps:
                # 权重加载（如 551/551、8/8）与采样后的附加阶段（如 9/9）也打印 x/y，
                # 都不是采样进度：只报阶段与耗时，**绝不把原始进度条 |####…| 倒进聊天区**
                self._update_vid_progress_line(
                    "🎬 %s… %d/%d · %ds"
                    % ("后处理" if getattr(self, "_vid_phase", "load") == "sample"
                       else "加载模型权重", cur, total, elapsed))
            return
        if "decoding" in low or "saving video" in low or "writing" in low:
            self._vid_saw_decode = True
            self._update_vid_progress_line("🎬 解码并封装视频… %ds" % elapsed)
        elif "out of memory" in low or "oom" in low or "error" in low or "failed" in low:
            # 引擎报错不再只留在进度行里：直接固化显示，避免用户以为还在跑
            self._update_vid_progress_line("🎬 引擎输出：%s" % line.strip()[:120])

    def _handle_vid_exit(self, rc, gen):
        if gen != self._vid_gen:
            return                      # 过期任务（已取消并被新任务替换），忽略其回调
        elapsed = int(time.time() - self._vid_t0) if getattr(self, "_vid_t0", None) else 0
        cloud = bool(getattr(self, "_cloud_vid", False))
        self._vid_busy = False
        self._vid_proc = None
        self._cloud_reset("video")
        self._set_busy_ui(False)
        out = getattr(self, "_vid_out", "")
        cancelled = self._stop_flag is not None and self._stop_flag.is_set()
        mark = getattr(self, "_vid_mark", None)
        if mark:
            self.chat.configure(state="normal")
            try:
                self.chat.delete(mark, "end")
            except Exception:
                pass
            self.chat.configure(state="disabled")
        if cancelled:
            self._append("🎬 [已取消生成]\n", "meta")
            self._append("────────────────\n", "meta")
            return
        if rc == 0 and out and os.path.isfile(out):
            # 视频无法内嵌显示（tk.PhotoImage 只认 PNG/静态 GIF，且项目坚持零第三方依赖），
            # 所以结果形态与生图对齐但换成按钮行：聊天流里给位置 + 两个入口
            self.chat.configure(state="normal")
            try:
                self.chat.insert("end", "\n🎬 视频已生成 · 耗时 %ds\n" % elapsed, "meta")
                btn = ttk.Button(self.chat, text="打开视频",
                                 command=lambda p=out: os.startfile(p))
                self.chat.window_create("end", window=btn)
                self.chat.insert("end", "  ", "meta")
                btn2 = ttk.Button(self.chat, text="打开所在文件夹",
                                  command=lambda p=out: self._open_containing(p))
                self.chat.window_create("end", window=btn2)
                self.chat.insert("end", "\n🎬 已保存：%s（%.1f MB）\n"
                                 % (out, os.path.getsize(out) / 1048576.0), "meta")
                self.chat.insert("end", "────────────────\n", "meta")
                self.chat.see("end")
            except Exception as e:
                self._append("🎬 [回显失败: %s] 视频已保存：%s\n" % (e, out), "error")
            self.chat.configure(state="disabled")
        else:
            # 退出码 0 却没有文件，和真的失败是两回事：分开说，否则排查方向会被带偏。
            # 云端那条把"引擎"换成"云端"，因为这时候原话来自服务商的回包而不是 stdout。
            src = "云端" if cloud else "引擎"
            why = ("%s正常返回但没有写出文件" % src if rc == 0 else "退出码 %s" % rc)
            # 前导换行：进度行被整行删掉时连它自带的换行一起没了（与生图同一处修）
            self._append("\n🎬 生成失败（%s）。\n" % why, "error")
            tail = [x for x in self._vid_tail if x][-6:]
            self._append("%s最后输出：\n  " % src + "\n  ".join(tail) + "\n" if tail
                         else "（没有可用来判断原因的输出）\n", "error")
            log = getattr(self, "_vid_log", "")
            if log and os.path.isfile(log):
                self._append("完整日志：%s\n" % log, "meta")
            self._append("────────────────\n", "meta")

    def _cancel_chat_video(self):
        """立即中止当前生视频任务：杀进程（引擎退出即释放显存）、世代递增、清理进度行。

        云端任务返回 True：取消结果由 _cloud_cancel_task 如实说过了——运行中的云端任务
        根本取消不掉，绝不能在这里写一句"已取消生成"糊弄过去（文档 §12.3 ⑥）。
        """
        cloud = bool(getattr(self, "_cloud_vid", False))
        tid = getattr(self, "_cloud_tid", "")
        if self._vid_proc is not None:
            try:
                self._vid_proc.kill()
            except Exception:
                pass
        # 与生图同样的坑：队列里残留的进度行会重画进度并连带删掉"已取消"提示
        if self._stop_flag is not None:
            self._stop_flag.set()
        self._vid_gen += 1
        self._vid_busy = False
        self._vid_proc = None
        self._cloud_vid = False
        self._cloud_tid = ""      # 同生图：残留的旧 tid 会被下一轮云端任务误取消
        mark = getattr(self, "_vid_mark", None)
        if mark:
            self.chat.configure(state="normal")
            try:
                self.chat.delete(mark, "end")
            except Exception:
                pass
            self.chat.configure(state="disabled")
        if cloud:
            return self._cloud_cancel_task(tid, "video")
        return False
