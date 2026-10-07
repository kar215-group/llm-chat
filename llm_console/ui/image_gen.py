# -*- coding: utf-8 -*-
"""llm_console.ui.image_gen — 界面 Mixin：聊天流内生图链路（进度行、结果内嵌、取消）"""

import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

from ..core import capability, cloudjobs, config, providers, sdprofile
from ..core.media import (build_img_cmd, img_out_dir, plan_ref_images,
                          resolve_img_files, resolve_img_model_path)
from ..connection import cloud_media
from . import imgdecode


class ImageGenMixin:
    """App 的聊天流内生图职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    # ---- 聊天流内生图（方案 A：页面形态不变，输出内嵌对话流）----
    def _start_chat_image(self, prompt, ref_img=None, ref_imgs=None, ref_mode="",
                          doc=None):
        """带图两档都从这里进：`ref_mode` 空 = 按当前模型的默认档。

        `ref_imgs` 是多图列表；旧的单张 `ref_img` 仍能用（内部并入列表）。
        `doc` = 文本附件（set_file_attachment 的成品块）：回显后并进提示词再发
        （W 2026-10-07 裁决，合并与 📄 行见 chat._doc_merge）。
        """
        # 云端生图走服务商原生接口（二期），不启动本地 sd-cli：两套链路在入口就分开，
        # 免得拿云端模型名去喂本地引擎（那是 v32 拦下的那个错）。
        if providers.is_cloud(self.cfg):
            return self._start_cloud_image(prompt, ref_imgs=ref_imgs or ([ref_img] if ref_img else []),
                                           ref_mode=ref_mode, doc=doc)
        sd = self.cfg.get("sd_dir", "")
        cli = os.path.join(sd, "sd-cli.exe")
        if not os.path.isfile(cli):
            self._append("\n[提示] 未找到生图引擎 %s —— 请在 设置 → 本地模型 → 生图（sd.cpp） 里把「sd.cpp 目录」"
                         "指向你部署的 sd.cpp（内含 sd-cli.exe）。\n"
                         % (cli or "（还没填目录）"), "error")
            return
        # 扩散模型一律走统一出口（绝对路径）：菜单选中 → 默认生图模型 → 扫描到的第一个。
        # 原来这里是"取 basename 再拼回 image_model_dir"，模型不在那个目录就必然报未找到
        # （出厂默认 `image_model_dir` 为空时必坏，见 core/media.resolve_img_model_path）。
        diffusion = resolve_img_model_path(self.cfg)
        if not diffusion:
            self._append("\n[提示] 未找到生图模型：把扩散权重（.gguf / .safetensors / .ckpt）放进"
                         "生图模型目录，或在模型菜单里重新选一个。\n"
                         "  目录在 设置 → 本地模型 → 生图（sd.cpp） 的「生图模型文件夹」里改。\n", "error")
            return
        # 配套文件由模型族决定（Qwen-Image 要 LLM+VAE，Flux 要 clip_l+t5xxl+VAE，SDXL
        # 单文件就够了），所以按 sdprofile 的 require 预检；缺件就地报错，别把不存在
        # 的路径交给引擎再吃一次"退出码 1"（生图链路没有失败回显，黑盒更难查）。
        files = resolve_img_files(self.cfg, diffusion)
        missing = sdprofile.missing_slots(files["family"], files)
        if missing:
            self._append("\n[提示] 这个生图模型（识别为 %s）缺配套文件：%s\n"
                         "  放进生图模型目录，或在 设置 → 本地模型 → 生图（sd.cpp） 的「配套文件」里指名。\n"
                         % (sdprofile.label_of(files["family"]), "、".join(missing)), "error")
            return
        # 带图两档：**在 Popen 之前**定档并判这一族走不走得通。认不出族 / 这一族没这条通路
        # 就地报错，一个字都不丢 —— 别把命令拼好再让引擎吃一次"退出码 1"。
        refs = [p for p in (list(ref_imgs or []) + ([ref_img] if ref_img else []))
                if p and os.path.isfile(p)]
        modes = capability.resolve_modes(self.cfg, files["family"])
        mode = str(ref_mode or "").strip()
        if refs and mode and not modes.get(mode, {}).get("ok"):
            self._append("\n[提示] %s\n" % modes[mode]["why"], "error")
            return
        mode = mode or capability.default_ref_mode(self.cfg, files["family"])
        used, dropped, drop_note = plan_ref_images(refs, mode)
        if not refs and mode:
            mode = ""                      # 纯文生图不带模式，省得回显与日志里出现无意义的档位
        # "必闪退"档启动确认（2026-10-06）：摆在消费输入框之前，点否一个字都不丢（坑 133）
        if not self._confirm_fatal_perf(diffusion, "image", files=files):
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
        # 种子跟着设置页的 img_seed 走（-1 = 随机）。不用 `or -1` 兜底：0 是合法种子，
        # 会被 falsy 判断吞成随机（与云端生图路径同一教训）。
        try:
            seed = int(self.cfg.get("img_seed", -1))
        except Exception:
            seed = -1
        outdir = img_out_dir(self.cfg)   # <产物文件夹>/本地/image（2026-10-06 起）
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, time.strftime("img_%Y%m%d_%H%M%S") + ".png")

        # 预检都过了才消费输入框与附件：清空摆在调用方（send_message）的话，
        # "没找到引擎 / 缺配套件"这类失败会把提示词与参考图一起吃掉（坑 133）
        self.input.delete("1.0", "end")
        self.clear_attachment()
        self._append("\n【你】\n" + prompt + "\n", "user")
        prompt = self._doc_merge(prompt, doc)
        for p in used:
            self._append_image(p, max_w=320)
        if used:
            # 措辞一律问 capability（模式名只有那一处定义），不自己写"图生图"
            self._append("[%s] 参考图 %d 张%s\n"
                         % (capability.MODE_LABEL.get(mode, "带图"), len(used),
                            "" if not drop_note else "（%s）" % drop_note), "meta")
            if mode == capability.MODE_EDIT:
                self._append("　重绘强度 %s\n" % self.cfg.get("img_strength", 0.6), "meta")
        self._img_gen += 1
        mygen = self._img_gen
        mark = "imgprog%d" % mygen
        self.chat.mark_set(mark, "end-1c")
        self.chat.mark_gravity(mark, "left")   # 进度文字长在 mark 右侧，mark 钉在行首（gravity=right 会导致堆叠）
        self._img_mark = mark
        self._img_busy = True
        self._img_out = out
        # 每次任务重新开始：末尾缓冲、云端日志与"这一次是不是云端"都要归零
        self._img_tail = []
        self._img_extra_paths = []
        self._img_log = ""
        self._cloud_img = False
        self._t0 = time.time()
        self._saw_decode = False
        self._stop_flag = threading.Event()
        self._set_busy_ui(True)
        cmd = build_img_cmd(self.cfg, prompt, out, steps, size, diffusion, cfg_scale, seed,
                            ref_mode=mode, ref_images=used, files=files)
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

    # ---- 云端生图（二期：服务商原生接口，按 providers.media_api 分派）----
    def _start_cloud_image(self, prompt, ref_img=None, ref_imgs=None, ref_mode="",
                           doc=None):
        provider = providers.current_provider(self.cfg)
        sp = providers.split_cloud_id(self.cfg.get("model", ""))
        model = sp[1] if sp else ""
        # 预检在提交前做完：云端一次请求就是真金白银/套餐额度，别把"缺密钥"这种
        # 问题发给服务端再解析它的 401（与本地链路"Popen 前预检"是同一条纪律）
        err = cloud_media.check(self.cfg, provider, model, "image")
        if err:
            self._append("\n[云端生图] %s\n" % err, "error")
            return
        outdir = config.cloud_media_dir(self.cfg, "image")
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self._append("\n[云端生图] 存放目录建不出来：%s\n"
                         "  目录可在 设置 → 云端模型 → 生图 / 生视频 里改。\n" % e, "error")
            return
        out = os.path.join(outdir, time.strftime("img_%Y%m%d_%H%M%S") + ".png")
        # 预检（check + 建目录）都过了才消费输入框与附件（坑 133，同本地链路）
        self.input.delete("1.0", "end")
        self.clear_attachment()
        self._append("\n【你】\n" + prompt + "\n", "user")
        prompt = self._doc_merge(prompt, doc)
        # 参考图在发送前已由 chat.send_message 过一遍 capability + ref_images_error（含档位
        # 校验），这里只负责显示回对话流并交给 worker（缩略图与本地生图同一套形态）。
        refs = [p for p in (list(ref_imgs or []) + ([ref_img] if ref_img else []))
                if p and os.path.isfile(p)]
        mode = ""                       # 纯文生图不带档位，省得回显与日志里出现无意义的它
        if refs:
            # 档位：选了且这家支持就用，否则回这家默认档（各家默认档不同，别替它决定）
            mode = str(ref_mode or "").strip()
            if not (mode and providers.supports_ref_mode(provider, mode)):
                mode = providers.ref_image_mode(provider)
            for p in refs:
                self._append_image(p, max_w=320)
            self._append("[云端生图] %s：%d 张（%s）\n"
                         % (capability.MODE_LABEL.get(mode, "参考图"), len(refs),
                            os.path.basename(refs[0])
                            + ("等" if len(refs) > 1 else "")), "meta")
        # 费用就近打在对话流里，而且必须打在**提交之前**：生图不弹二次确认（同步、
        # 单次几分钱，每次都问只会让人不看内容按回车），但那不等于不告诉。
        # 单价按模型查，没填就明说"以账单为准"，不编数字。
        self._append("[云端生图] 模型 %s · %s\n"
                     % (model or "（未选）",
                        providers.media_price_note(provider, providers.KIND_IMAGE,
                                                   0, model)), "meta")
        gen, q, stop = self._cloud_begin("image")
        self._cloud_pid = str(provider["id"])
        self._img_out = out
        self._img_log = out + ".log"
        self._update_img_progress_line("☁ 云端生图中… %s（%s）"
                                       % (providers.short_of(model),
                                          provider.get("name") or ""))
        threading.Thread(target=self._cloud_image_worker,
                         args=(provider, model, prompt, out, gen, q, stop),
                         kwargs={"refs": refs, "ref_mode": mode}, daemon=True).start()

    def _cloud_image_worker(self, provider, model, prompt, dest, gen, q, stop_flag,
                            tid="", refs=None, ref_mode=""):
        """子线程：只往队列里投事件，绝不碰控件（坑 11/54 的铁律）。

        `tid` 非空 = 从台账取回旧任务，**不再提交一次**（重复提交就是重复扣钱）。
        """
        cur = {"tid": str(tid or "")}      # 当前任务的 task_id（提交后才拿得到）

        def persist(status, raw=None):
            if cur["tid"]:
                cloudjobs.update_job(cur["tid"], status=str(status))

        def note_tid(t):
            """一拿到 task_id 就落台账 —— **在轮询之前**。

            异步化的生图（服务端回了 task_id）原来要等整轮轮询结束、worker 收尾时才
            写台账，中间这段（可能几十分钟）只要断电 / 强杀，这条结果就再也取不回来
            （产物地址只活 24 小时，开发机 23:30 断电）。生视频那条链路一直是提交即登记，
            这里补齐同一条纪律（坑 136）。
            """
            t = str(t or "").strip()
            if not t or cur["tid"]:
                return
            cur["tid"] = t
            # 取消链路要找得到它：主线程「停止生成」读 self._cloud_tid 才能对 PENDING
            # 窗口内的任务发起真取消（生视频链路同款 worker 写回，见 video_gen 的
            # _cloud_video_worker）。不写回的话，已提交的任务点停止会被当成
            # "还没提交出去"——文案与事实不符，还错过唯一取消得掉的窗口。
            # 乱序兜底沿用既有纪律：新任务 begin / 收尾都会再清一次 _cloud_tid。
            self._cloud_tid = t
            if not cloudjobs.get_job(t):
                cloudjobs.add_job(t, "image", provider["id"],
                                  provider.get("name", ""), model, prompt, dest)

        try:
            seed = int(self.cfg.get("img_seed", -1))    # 不用 or：0 是合法种子
        except Exception:
            seed = -1
        if cur["tid"]:
            # 取回旧任务：不再提交一次，只查 + 下载
            res = cloud_media.wait_task(self.cfg, provider, cur["tid"], dest, kind="image",
                                        emit=q.put, stop_flag=stop_flag,
                                        persist=persist, model=model)
        else:
            res = cloud_media.generate_image(
                self.cfg, provider, model, prompt, dest, emit=q.put,
                stop_flag=stop_flag,
                negative=str(self.cfg.get("cloud_img_negative", "") or ""),
                size=str(self.cfg.get("cloud_img_size", "") or ""), seed=seed,
                ref_images=list(refs or []),
                ref_mode=str(ref_mode or ""),
                on_task_id=note_tid, persist=persist)
        paths = list(res.get("paths") or [])
        # 真实落地路径可能与服务端给的扩展名一致而与我们的默认值不同（.png vs .jpg），
        # 所以把主图路径回写：_handle_img_exit 是拿 os.path.isfile(_img_out) 判成功的。
        # 这里只写普通属性、不碰控件，读它的是主线程的 exit 处理（在事件入队之后）。
        if paths:
            self._img_out = paths[0]
        # 多张产物：主图走原有的内嵌回显，其余的只列路径（一次塞四张进聊天区会把界面撑爆）
        self._img_extra_paths = paths[1:]
        new_tid = cur["tid"] or str(res.get("task_id") or "")
        if new_tid:
            note_tid(new_tid)              # 兜底：上面没走到（同步路径直接给 url）就不补
            if paths:
                cloudjobs.mark_done(new_tid, paths=paths, urls=res.get("urls"),
                                    seconds=res.get("seconds", 0))
            else:
                # 保留"还在等 / 超时"这类真实状态（取回时按它决定显示"取回"还是"继续等"），
                # 只把真出错的那种折成 unknown
                st = str(res.get("status") or "")
                cloudjobs.update_job(
                    new_tid, status=st if st in cloudjobs.OPEN_STATUS else "unknown",
                    error=str(res.get("error") or "")[:600])
        if not paths:
            q.put(("line", "[ERROR] %s" % (res.get("error") or "云端生图失败")))
        q.put(("exit", 0 if paths else 1, gen))

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
        # 滚动保留输出末尾：进度行是 delete(mark,"end") 整行清除的，引擎/服务端的报错
        # 会跟着一起被删掉，界面只剩"退出码 1"（坑 40，生视频链路已经吃过一次）。
        if line.strip():
            self._img_tail.append(line.strip()[:220])
            if len(self._img_tail) > 12:
                del self._img_tail[0]
        if self._stop_flag is not None and self._stop_flag.is_set():
            return
        elapsed = int(time.time() - self._t0) if getattr(self, "_t0", None) else 0
        low = line.lower()
        if "generating image" in line:
            return      # 采样开始行：没有进度数字可报，原样跳过（进度靠下面的 n/m 行）
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
        elif "error" in low or "failed" in low or "out of memory" in low:
            # 报错当场固化在进度行上，别等它被进度行一起删掉
            self._update_img_progress_line("🎨 %s" % line.strip()[:160])

    def _handle_img_exit(self, rc, gen):
        if gen != self._img_gen:
            return                      # 过期任务（已被取消并替换），忽略其回调
        elapsed = int(time.time() - self._t0) if getattr(self, "_t0", None) else 0
        # 这一轮是不是云端，必须在 _cloud_reset 之前读走：那个函数会把标记清掉，
        # 读晚了失败文案就会把云端的结果说成"引擎没有交付文件"（生视频那侧顺序是对的）
        cloud = bool(getattr(self, "_cloud_img", False))
        self._img_busy = False
        self._img_proc = None
        self._cloud_reset("image")
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
                # 缩略图单独一层：imgdecode.photo 不抛（Pillow 优先，Tk 8.6 画不出
                # JPEG / WEBP / BMP、或 Pillow 缺席时回 None）——那只是"没有预览"，
                # **已保存 + 两个打开按钮照给**（坑 132）
                thumb = None
                img = imgdecode.photo(out, 500)
                if img is not None:
                    thumb = self._remember_photo(out, img)     # 持引用防 GC（按路径为键）
                self.chat.insert("end", "\n", "meta")      # 图片前补换行：独立成行
                if thumb is not None:
                    self.chat.image_create("end", image=thumb)
                    self.chat.insert("end", "  ", "meta")      # 图片与按钮的间距
                btn = ttk.Button(self.chat, text="打开原图",
                                 command=lambda p=out: os.startfile(p))
                self.chat.window_create("end", window=btn)
                self.chat.insert("end", "  ", "meta")
                btn2 = ttk.Button(self.chat, text="打开所在文件夹",
                                  command=lambda p=out: self._open_containing(p))
                self.chat.window_create("end", window=btn2)
                self.chat.insert("end", "\n🎨 已保存：%s · 耗时 %ds\n" % (out, elapsed), "meta")
                extra = [p for p in (getattr(self, "_img_extra_paths", []) or [])
                         if p and os.path.isfile(p)]
                if extra:
                    self.chat.insert("end", "🎨 同批还有 %d 张：%s\n"
                                     % (len(extra), "、".join(os.path.basename(p)
                                                              for p in extra)), "meta")
                self.chat.insert("end", "────────────────\n", "meta")
                self.chat.see("end")
            except Exception as e:
                self._append("🎨 [回显失败: %s] 图片已保存：%s\n" % (e, out), "error")
            self.chat.configure(state="disabled")
        else:
            # 与生视频同一套失败回显：本地是"退出码 + 引擎末几行"，云端是"服务端原话"
            src = "云端" if cloud else "引擎"
            # 前导换行：进度行是 delete(mark,"end") 整行删掉的，连它自带的那个换行一起没了，
            # 不加回来这行就会粘在上一行尾巴上
            self._append("\n🎨 生成失败（%s）。\n"
                         % ("退出码 %s" % rc if rc else "%s 没有交付文件" % src), "error")
            tail = [x for x in (getattr(self, "_img_tail", []) or []) if x][-6:]
            self._append("%s最后输出：\n  " % src + "\n  ".join(tail) + "\n" if tail
                         else "（没有可用的输出）\n", "error")
            log = getattr(self, "_img_log", "")
            if log and os.path.isfile(log):
                self._append("完整日志：%s\n" % log, "meta")
            self._append("────────────────\n", "meta")

    def _cancel_chat_image(self):
        """立即中止当前生图任务：杀进程、世代递增（旧回调失效）、清理进度行。

        返回 True = 这是云端任务，取消的结果**已由 _cloud_cancel_task 如实说过**，
        调用方（stop_generate）就别再补一句"已取消生成"——云端任务在 RUNNING 时
        根本取消不掉，那样说会把用户骗过去（文档 §12.3 ⑥）。
        """
        cloud = bool(getattr(self, "_cloud_img", False))
        tid = getattr(self, "_cloud_tid", "")
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
        self._cloud_img = False
        self._cloud_tid = ""      # 清掉这一轮的 tid：留着会让下一次云端任务误取消它
        mark = getattr(self, "_img_mark", None)
        if mark:
            self.chat.configure(state="normal")
            try:
                self.chat.delete(mark, "end")
            except Exception:
                pass
            self.chat.configure(state="disabled")
        if cloud:
            return self._cloud_cancel_task(tid, "image")
        return False
