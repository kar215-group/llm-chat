# -*- coding: utf-8 -*-
"""llm_console.ui.chat — 界面 Mixin：对话流渲染、输入与附件、发送分派、停止与收尾"""

import base64
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, filedialog

from ..core import capability, chatlog, history, providers, textfile
from ..core.config import save_config
from ..core.models import display_name
from ..core.params import ctx_for, current_ngl
from ..core.server import start_server, stop_server
from ..connection import cloud as cloud_conn
from ..connection import cloud_media
from ..connection import stream as stream_conn
from ..connection.stream import stream_worker


def format_usage(u):
    """把 usage 拼成人话。各家字段名不同：OpenAI 是 prompt/completion_tokens，
    阿里兼容模式常见 input/output_tokens，都认；拿不到就直说，不编数字。"""
    if not isinstance(u, dict):
        return str(u)

    def pick(*names):
        for n in names:
            v = u.get(n)
            if isinstance(v, (int, float)):
                return int(v)
        return None

    inp = pick("prompt_tokens", "input_tokens")
    out = pick("completion_tokens", "output_tokens")
    tot = pick("total_tokens")
    # 思考模型的计费几乎全在 reasoning_tokens 上（DeepSeek flash/reasoner 实测如此），
    # 不显示的话用户看到的"输出"会严重低估实际花费
    detail = u.get("completion_tokens_details")
    reasoning = None
    if isinstance(detail, dict):
        v = detail.get("reasoning_tokens") or detail.get("reasoning_token")
        if isinstance(v, (int, float)):
            reasoning = int(v)
    parts = []
    if inp is not None:
        parts.append("输入 %d" % inp)
    if out is not None:
        parts.append("输出 %d" % out)
    if reasoning:
        parts.append("其中思考 %d" % reasoning)
    if tot is not None:
        parts.append("合计 %d" % tot)
    elif parts:
        parts.append("合计 %d" % ((inp or 0) + (out or 0)))
    return ("、".join(parts) + " tokens") if parts else "本次未返回用量"


class ChatMixin:
    """App 的对话流与输入职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    # ---- 附件（图片 / 文本文件）----
    def _vl_ok(self):
        """能不能收图：走 core/capability 的统一判定（人工声明 > 自动判据）。"""
        return capability.can_take_image(self.cfg)

    def _no_image_hint(self, res):
        """把"为什么现在不能发图"说清楚——尤其区分"确认不支持"与"还没确认"。"""
        if res["verdict"] == capability.UNKNOWN:
            return ("这个模型的看图能力还没确认：云端的 /models 不带能力字段，光看名字不算数。\n\n"
                    "要放开：设置 → 云端模型 → 服务商与密钥 → 选择模型 → 把「图片输入」改成"
                    "「支持」，或点「验证图片输入」发一次最小请求让服务端自己回答。")
        return ("当前模型不支持图片输入（%s）。\n"
                "换成本地配了视觉投影器的模型（如 Qwen3VL-8B），"
                "或在 设置 → 云端模型 → 服务商与密钥 → 选择模型 里把「图片输入」改为「支持」。"
                % res["note"])

    def pick_image(self):
        """📎 入口：聊天模型可带图片或文本文件；生图/生视频模式只认图片（附件语义不同）。

        函数名沿用 pick_image 是因为工具栏、测试与既有调用都指它；现在它按扩展名分派。
        """
        kind = self.cfg.get("model_kind")
        cloud = providers.is_cloud(self.cfg)
        if kind in ("image", "video"):
            if cloud:
                # 云端生图能不能附图由 capability 判（判据 = 这一家的原生接口有没有
                # 参考图字段）；生视频仍然只接文生。放行时不再重复弹说明，直接给文件框。
                if capability.resolve(self.cfg)["verdict"] != capability.YES:
                    messagebox.showinfo("云端生图 / 生视频",
                                        capability.resolve(self.cfg)["note"] +
                                        "\n\n要带参考图或首帧，请在模型菜单里切回本地的"
                                        "〔生图〕〔生视频〕模型（走 sd.cpp 那条链路）。")
                    return
            # 生图模式：附图 = 参考图/底图；生视频模式：附图 = 首帧。
            # 这两类走引擎/接口的图像输入通路，跟"聊天模型能不能看图"是两回事，
            # 所以不查聊天那条判据 —— 之前视频模式误查过一次，报"当前模型不支持看图"。
            # 标题不写"底图"：云端各家把这张图当底图还是当主体参考不一样
            # （providers.ref_image_mode），具体语义在发送后的回显里说。
            p = filedialog.askopenfilename(
                title=("选择首帧图片（作为图生视频的第一帧）" if kind == "video"
                       else "选择参考图"),
                filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")])
            if p:
                self.set_attachment(p)
            return
        p = filedialog.askopenfilename(
            title="选择附件（图片或文本文件）",
            filetypes=([("图片", "*.png *.jpg *.jpeg *.webp *.bmp")]
                       if not cloud else [])
                      + list(textfile.file_dialog_types()))
        if not p:
            return
        k = textfile.kind_of(p)
        if k == "image":
            res = capability.resolve(self.cfg)
            if res["verdict"] != capability.YES:
                # 判据说"不行/没确认"时不硬拦到底：给一次人工放开的机会。
                # 自动判据会有假阴性（投影器放在别处、文件名不规范、云端根本没能力字段），
                # 这种时候让用户说了算，比让他去设置页绕一圈快。
                if messagebox.askyesno(
                        "图片输入",
                        self._no_image_hint(res) +
                        "\n\n仍要按「支持图片输入」处理这个模型吗？\n"
                        "（选「是」= 记为该模型支持，之后不再拦；"
                        "可在 设置 → 云端模型 → 服务商与密钥 → 选择模型 里改回「自动判断」）"):
                    capability.set_choice(self.cfg, capability.YES)
                    save_config(self.cfg)
                    self._append("\n[模型] 已把「%s」标记为支持图片输入。\n"
                                 % capability.model_key(self.cfg), "meta")
                    self._update_model_label()
                    self.set_attachment(p)
                return
            self.set_attachment(p)
        else:
            self.set_file_attachment(p)

    def set_attachment(self, path):
        """挂一张待发送的图片。缩略图只影响"好不好看"，**不影响能不能发**。

        Tk 8.6 的 PhotoImage 只认 PNG / 静态 GIF：JPEG / BMP / WEBP 一律报
        "couldn't recognize data in image file"。而发送走的是**文件路径 / 原始字节**
        （本地引擎的 `-i`、云端参考图的 data URL、聊天模型的 base64），这些格式都发得出去
        —— 所以预览失败只降级成"没有缩略图"，绝不拦下（坑 132；以前是弹个警告就 return，
        于是照片根本挂不上，而下游其实全都支持）。
        """
        if not os.path.isfile(path):
            messagebox.showwarning("图片", "这个文件不在了：\n%s" % path)
            return
        thumb = None
        try:
            img = tk.PhotoImage(file=path)
            f = max(1, round(img.width() / 96.0))
            if f > 1:
                img = img.subsample(f, f)
            thumb = img
        except Exception:
            thumb = None                 # 画不出来而已，别拦（坑 132）
        self._attach_photo = thumb
        self._attached_image = path
        self._attached_file = None
        self.attach_thumb.configure(image=thumb or "")
        if thumb is not None:
            self.attach_thumb.pack(side="left", padx=(0, 8), before=self.attach_name)
        else:
            self.attach_thumb.pack_forget()
        self.attach_name.configure(text="%s（%d KB）%s"
                                   % (os.path.basename(path),
                                      max(1, os.path.getsize(path) // 1024),
                                      "" if thumb is not None
                                      else " · 这个格式在这里没有预览（不影响发送）"))
        self._show_attach_bar()

    def set_file_attachment(self, path):
        """文本/Word 附件：读出来、按本次上下文的 token 预算先截好，再挂在附件条上。

        预算：本地按当前模型的 ctx（ctx 是按模型记忆的）− max_tokens − 预留；
        云端取一个保守固定值（各家上下文长度不同，程序无从得知）。
        本地服务在跑时用 /tokenize 精确核验一次（服务没跑就只用保守估算，不为此等 TCP 超时）。
        """
        doc = textfile.read_document(path)
        if not doc.get("ok"):
            messagebox.showwarning("附件", doc.get("error") or "这个文件读不了。")
            return
        cloud = providers.is_cloud(self.cfg)
        budget = textfile.budget_tokens(self.cfg, cloud=cloud)
        counter = None if cloud else (lambda t: stream_conn.count_tokens(self.cfg, t))
        kept, used, dropped = textfile.fit_lines(doc["lines"], budget, counter=counter)
        self._attached_file = {"path": path, "name": doc["name"], "lines": doc["lines"],
                               "total_lines": len(doc["lines"]), "chars": doc["chars"],
                               "encoding": doc["encoding"], "kept": kept,
                               "dropped": dropped, "budget": budget,
                               "cloud": cloud, "doc": doc["doc"]}
        self._attached_image = None
        self._attach_photo = None
        self.attach_thumb.configure(image="")
        note = ""
        if dropped:
            note = ("\n超出本次上下文预算（约 %d token），先只带前 %d 行；"
                    "云端模型可在 设置 → 云端模型 → 文本模型 打开「让它自己决定读哪些行」。"
                    % (budget, len(kept)))
        self.attach_name.configure(
            text="📄 %s｜%d 行 / %d 字｜编码 %s%s"
                 % (doc["name"], len(doc["lines"]), doc["chars"], doc["encoding"], note))
        self._show_attach_bar()

    def _show_attach_bar(self):
        """把附件条贴到输入区上方。

        用 side="bottom"（而不是 before=输入区）：它和输入区同属"固定外框"，
        要在可伸缩的聊天区之前分到空间，否则窗口一矮就先看不见附件条。
        """
        self.attach_frame.pack(side="bottom", fill="x", padx=10, pady=(2, 0))

    def clear_attachment(self):
        self._attached_image = None
        self._attached_file = None
        self._attach_photo = None
        self.attach_thumb.configure(image="")
        self.attach_name.configure(text="")
        self.attach_frame.pack_forget()

    def _remember_photo(self, path, img):
        """持引用防 GC；只保留最近 30 张（同路径覆盖，防长会话内存无限增长）。"""
        self._img_photos[path] = img
        while len(self._img_photos) > 30:
            self._img_photos.pop(next(iter(self._img_photos)))
        return img

    def _append_image(self, path, max_w=320):
        """在聊天流末尾内嵌一张缩略图（normal 状态下插入）。"""
        try:
            img = tk.PhotoImage(file=path)
            f = max(1, round(img.width() / float(max_w)))
            if f > 1:
                img = img.subsample(f, f)
            self._remember_photo(path, img)
            self.chat.configure(state="normal")
            self.chat.image_create("end", image=img)
            self.chat.insert("end", "\n", "meta")
            self.chat.see("end")
            self.chat.configure(state="disabled")
        except Exception:
            # 只是"这个格式画不出来"（Tk 只认 PNG/GIF），**不是发送失败**：
            # 图已经按请求发出去了，所以这里用 meta 说一句，别标成红色错误（坑 132）
            self._append("（这张图在对话里不显示预览：%s）\n" % os.path.basename(path),
                         "meta")

    # ---- 文本渲染 ----
    def _append(self, text, tag):
        self.chat.configure(state="normal")
        self.chat.insert("end", text, tag)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    # ---- 输入事件 ----
    def _on_return(self, _event):
        if not self._busy:
            self.send_message()
        return "break"

    def _on_shift_return(self, _event):
        self.input.insert("insert", "\n")
        return "break"

    def send_message(self):
        if self._busy or self._svc_busy or self._img_busy or self._vid_busy:
            # 用"中止"而不是"取消"：云端任务进入运行态后取消不掉，「停止生成」在那边
            # 只是"不再等它"（_cloud_cancel_task 会把实情说出来），这里别先给错承诺。
            if self._img_busy:
                self._append("\n[提示] 生图进行中：点「停止生成」中止当前任务，或等它完成。\n",
                             "meta")
            elif self._vid_busy:
                self._append("\n[提示] 生视频进行中：点「停止生成」中止当前任务，或等它完成。\n",
                             "meta")
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        kind = self.cfg.get("model_kind")
        if providers.is_cloud(self.cfg) and kind in ("image", "video"):
            # 云端生图 / 生视频：走服务商原生接口，**完全不碰本地引擎**，也就自然
            # 不占显存——所以这里不走 _confirm_shared_vram 那套三选弹窗。
            # 费用确认（只有生视频）放在清空输入框之前：用户选"不"的时候一个字都不该丢。
            err = providers.validate_for_send(self.cfg)
            ref = self._attached_image or ""
            if not err and ref:
                # 最后一道：📎 入口已经按 capability 判过一次，但中途可能换了服务商，
                # 而且"这张图这一家收不收（格式 / 体积 / 张数）"只有接口侧知道。
                v = capability.resolve(self.cfg)
                err = ((v["note"] if v["verdict"] != capability.YES else "")
                       or cloud_media.ref_images_error(
                           providers.current_provider(self.cfg), [ref]))
            if err:
                self._append("\n[云端] %s%s\n" % (err, "" if not ref else
                                                  "\n  图片已保留，不会丢。"), "error")
                return
            if kind == "video" and not self._confirm_cloud_video_spend():
                return
            # 输入框与附件的清空由 _start_cloud_* 在各自预检（check + 建目录）通过后做，
            # 与本地链路同一条纪律（坑 133）
            if kind == "image":
                self._start_chat_image(text, ref_img=ref or None)
            else:
                self._start_chat_video(text)
            return
        if self.cfg.get("model_kind") == "image":
            # 方案 A：聊天输入即生图提示词，结果内嵌到聊天流，页面形态不变。
            # 生成中再次发送 = 立即中止当前任务并开始新任务（参照 Llama 重启语义）
            if self._img_busy:
                self._cancel_chat_image()
            choice = self._confirm_shared_vram("生图")
            if choice == "cancel":
                return                          # 输入内容与附图原样保留，什么都没发生
            if choice == "stop":
                self._release_vram()
            # 输入框与附件的清空挪进 _start_chat_image：**预检通过之后**才消费。
            # 摆在这里先清的话，"没找到引擎 / 缺配套件"这类失败会把刚写的提示词和附图
            # 一起吃掉，用户在界面上再也找不回来（坑 133）
            self._start_chat_image(text, ref_img=self._attached_image)
            return
        if self.cfg.get("model_kind") == "video":
            # 与生图同一套形态：输入即提示词，进度原位刷新，结果行带回调按钮
            if self._vid_busy:
                self._cancel_chat_video()
            choice = self._confirm_shared_vram("生视频")
            if choice == "cancel":
                return
            if choice == "stop":
                self._release_vram()
            # 同上：清空由 _start_chat_video 在预检通过后做（坑 133）
            self._start_chat_video(text, ref_img=self._attached_image)
            return
        if providers.is_cloud(self.cfg):
            # 云端模型没有"启动服务 / 换载"概念：先做可操作校验，然后直接发。
            # 校验放在删输入框之前——失败时用户的文字一个字都不丢。
            err = providers.validate_for_send(self.cfg)
            if err:
                self._append("\n[云端] %s\n" % err, "error")
                return
            if self._attached_image and not capability.can_take_image(self.cfg):
                # 云端只认"用户声明支持"或"实测收下过图"：各家多模态字段不一致，
                # 光凭名字像视觉模型就放行，多半换来一个看不懂的 400。
                # 附件保留不清除，声明/验证过之后直接再发一次即可。
                self._append("\n[云端] %s\n图片已保留，处理完再发一次就行。\n"
                             % self._no_image_hint(capability.resolve(self.cfg)), "meta")
            self.input.delete("1.0", "end")
            self._do_send(text, allow_image=capability.can_take_image(self.cfg))
            return
        if not self._server_ready_flag:
            self._append("\n[提示] 服务未就绪：请先点「启动服务」，等状态变为 ● 运行中 再发送。\n", "error")
            return
        want = os.path.basename(self.cfg["model"])
        serving = self._serving_model
        if serving is not None and serving != want:
            # 选中的模型与当前服务加载的不一致：自动换载，就绪后自动发送。
            # 对话历史保存在本程序里，每次请求都会全量携带——
            # 换模型不会丢失上下文，新模型能读到之前全部对话。
            self._pending_text = text
            self.input.delete("1.0", "end")
            self._begin_svc()
            self.send_btn.configure(state="disabled")
            self._append("\n[服务] 正在加载新模型 %s（GPU 层数 %d），就绪后自动发送你的消息…\n"
                         % (display_name(self.cfg, self.cfg["model"]),
                            current_ngl(self.cfg)), "meta")

            def work():
                stop_server()
                time.sleep(1)
                if self._closing:
                    return                  # 关闭中：不再启动新服务
                launched = os.path.basename(self.cfg["model"])
                try:
                    self._proc = start_server(self.cfg, ctx_for(self.cfg))
                except Exception as e:
                    self._finish_svc(False, "[服务] 启动失败: " + str(e), True)
                    self._sq.put(("reload_done", False))
                    return
                ok = self._wait_ready()
                self._finish_svc(ok, "[服务] 新模型 %s 就绪，继续对话（历史上下文已随消息携带）。"
                                 % display_name(self.cfg, self.cfg["model"])
                                 if ok else
                                 "[服务] 加载失败或超时——请检查模型与参数（设置 → 服务参数）。",
                                 not ok, launched=launched)
                self._sq.put(("reload_done", ok))
            threading.Thread(target=work, daemon=True).start()
            return
        self.input.delete("1.0", "end")
        self._do_send(text)

    def _do_send(self, text, allow_image=True):
        """常规发送：本地服务就绪且模型一致，或已选定云端模型时才会走到这里。

        allow_image=False（云端聊天模型没被确认支持看图）时忽略附件且**不清除**它——
        用户声明/验证过能力或切回本地 VL 模型后可以直接再发一次，不用重新选图。
        若已附图（当前模型可看图），按 OpenAI 多模态格式发送
        （content 数组 + image_url base64），并在聊天流内嵌缩略图。
        """
        img_path = self._attached_image if allow_image else None
        file_meta = self._attached_file
        used_file = False
        if img_path and os.path.isfile(img_path):
            try:
                with open(img_path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
                ext = (os.path.splitext(img_path)[1].lstrip(".").lower() or "png")
                if ext == "jpg":
                    ext = "jpeg"
                content = [{"type": "text", "text": text or "请描述这张图片。"},
                           {"type": "image_url",
                            "image_url": {"url": "data:image/%s;base64,%s" % (ext, b64)}}]
            except Exception as e:
                self._append("\n[错误] 读取图片失败：%s\n" % e, "error")
                return
            self._append("\n【你】\n" + (text or "（图片）") + "\n", "user")
            self._append_image(img_path, max_w=320)
            user_msg = {"role": "user", "content": content}
        elif file_meta:
            # 文本附件：正文 = 用户的话 + 包在 <file> 里的文件内容（本地与云端都走这条）
            used_file = True
            block = textfile.render_block(
                file_meta["name"], file_meta["kept"], file_meta["total_lines"],
                file_meta["chars"], file_meta["dropped"], file_meta["encoding"],
                question=text, budget=file_meta["budget"])
            self._append("\n【你】\n%s\n" % (text or "（见附件）"), "user")
            self._append("📄 已附上 %s｜共 %d 行 / %d 字｜%s｜编码 %s\n"
                         % (file_meta["name"], file_meta["total_lines"], file_meta["chars"],
                            ("发出前 %d 行" % len(file_meta["kept"])) if file_meta["dropped"]
                            else "全文", file_meta["encoding"]), "meta")
            user_msg = {"role": "user", "content": block}
        else:
            self._append("\n【你】\n" + text + "\n", "user")
            user_msg = {"role": "user", "content": text}
        if allow_image or used_file:
            self.clear_attachment()      # 云端不带图时保留附件，方便切回本地 VL 模型再发

        # 请求体里的消息一律经 core/history 拼（system 在最前、历史全量携带、最后是这一条）：
        # 那是"哪些消息进请求体"的唯一出口，上下文压缩将来就插在那儿，别在别处再拼一份
        msgs = history.request_messages(self.cfg, self.history, user_msg)
        self.history.append(user_msg)
        # 先把"问题"落盘再开线程：连点发送、生成中途强杀 / 断电，用户那句都不会丢。
        # 静默保存 —— "存在哪儿"那句话留到轮末再说，免得回答还没出来先刷一行日志
        self._save_chat_log(quiet=True)

        self._busy = True
        self._stop_flag = threading.Event()
        self._cur_reasoning = []
        self._cur_content = []
        self._reasoning_started = False
        self._content_started = False
        self._set_busy_ui(True)
        self._last_usage = None
        # 本地走 llama-server 的 SSE，云端走 OpenAI 兼容端点；两者推的事件完全一致，
        # 所以 _poll / _flush_stream / _finish_turn 这些渲染逻辑一行都不用改
        args = (self.cfg, msgs, self._q, self._stop_flag)
        plan = (file_meta and file_meta.get("cloud") and file_meta["dropped"]
                and bool(self.cfg.get("cloud_file_model_decides")))
        if plan:
            # 云端 + 文件被截断 + 用户开了那个开关 → 先让模型说它要读哪一段（详见 cloud.py）
            target = cloud_conn.chat_stream_with_file
            args = args + (dict(file_meta, question=text),)
        else:
            target = cloud_conn.chat_stream if providers.is_cloud(self.cfg) else stream_worker
        threading.Thread(target=target, args=args, daemon=True).start()

    def stop_generate(self):
        # 聊天流生图中：取消生图（杀引擎进程，显存即释放）
        if self._img_busy:
            # 云端任务自己会说清楚"取消掉了"还是"只是不再等它"，这里就别再补一句
            # "已取消生成"——那种说法在任务仍在云端运行时是假的。
            if not self._cancel_chat_image():
                self._append("🎨 [已取消生成]\n", "meta")
                self._append("────────────────\n", "meta")
            self._set_busy_ui(False)
            return
        # 聊天流生视频中：同一套取消语义
        if self._vid_busy:
            if not self._cancel_chat_video():
                self._append("🎬 [已取消生成]\n", "meta")
                self._append("────────────────\n", "meta")
            self._set_busy_ui(False)
            return
        if self._busy and self._stop_flag is not None and not self._stop_flag.is_set():
            self._stop_flag.set()
            self.stop_gen_btn.configure(state="disabled", text="正在停止…")

    # ---- 对话记录落盘（仅文本模型；本期只存不读，形状见 core/chatlog.py）----
    def _save_chat_log(self, quiet=False):
        """把当前会话整份写盘 → (是否已保存, 给用户看的一句话)。

        四条约束：
          ① 生图 / 生视频不存（成果本来就是文件）；
          ② **永不抛异常** —— 它挂在每轮结束与关窗路径上，存不进去只少一份记录，
             不能把发送或退出带崩；
          ③ 整份覆盖重写而不是追加，天然幂等，也不需要"崩溃截断恢复"那套；
          ④ 内容没变就直接返回（`_log_key` 是 (条数, 末条长度) 的廉价签名）。
             连点发送时同一份内容会被三个触发点各敲一次，省掉重复写盘。
        """
        if self.cfg.get("model_kind") != "chat" or not chatlog.enabled(self.cfg):
            return False, ""
        if not self.history:
            return False, ""
        key = (len(self.history), len(str(self.history[-1].get("content") or "")))
        if self._sess_saved and key == self._log_key:
            # 内容没变：不重复写盘，但"说一次保存在哪儿"的账还是要结（可能上次是静默那次）
            if quiet or self._announced_path == self._sess_path:
                return True, ""
            self._announced_path = self._sess_path
            return True, "对话记录已存：%s" % self._sess_path
        if not self._sess_id:
            self._sess_id = chatlog.new_id()
        p, err = chatlog.save(self.cfg, self._sess_id, self.history,
                              model=self.cfg.get("model", ""),
                              provider=self.cfg.get("model_provider", "local"))
        if err:
            return False, "对话记录没能写入（%s）：目录可在 gui_config.json 的 " \
                          "chat_log_dir 里改。" % err
        self._sess_path = p
        self._log_key = key
        self._sess_saved = True
        # "已存到哪儿"这句话一份文件只说一次；静默那次（发送时）把机会留给轮末
        if quiet:
            return True, ""
        if self._announced_path == p:
            return True, ""
        self._announced_path = p
        return True, "对话记录已存：%s" % p

    def _finish_turn(self, error=False):
        self._busy = False
        self._stop_flag = None
        self._set_busy_ui(False)
        content = "".join(self._cur_content).strip()
        if content:
            self.history.append({"role": "assistant", "content": content})
        usage, self._last_usage = self._last_usage, None
        if usage and not error and self.cfg.get("show_usage", True):
            self._append("\n〔用量〕%s\n" % format_usage(usage), "meta")
        # 每轮结束落一次盘：断电 / 强杀 / 关窗都只丢"正在跑的这一轮"
        _, note = self._save_chat_log()
        if note:
            self._append("\n[记录] %s\n" % note, "meta")
        if not error:
            self._append("\n\n────────────────\n", "meta")
        self.input.focus_set()

    def clear_chat(self):
        # 四个忙碌标志一个都不能漏（生视频那条以前没拦：生成中清空会把进度行所在
        # 的对话区清掉，结果行再贴到清空后的开头，看着像"任务丢了"）
        if self._busy or self._svc_busy or self._img_busy or self._vid_busy:
            return
        if not messagebox.askyesno("清空对话", "确定清空当前对话记录？"):
            return
        # 先存再清：清空不该顺手毁掉一份已经聊出来的记录
        self._save_chat_log()
        kept = self._sess_path
        self.history = []
        self._sess_id = ""            # 下一次对话算新会话，另起一份文件
        self._sess_saved = False
        self._sess_path = ""
        self._announced_path = ""
        self._log_key = None
        self.clear_attachment()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self._append("对话已清空。%s\n"
                     % (("原记录已存：%s" % kept) if kept else ""), "meta")

    def _flush_stream(self, chunks):
        """把本轮收到的流式片段合并后一次性写入（一次 insert + 一次滚动）。

        性能关键：Text.see() 每次调用都触发一次滚动重排，逐块调用时成本随
        积压量超线性增长——实测 1000 块逐块写入要 3.5 秒（界面冻结），
        合并成一次写入只要 7ms。因此每轮轮询只滚动一次。
        """
        if not chunks:
            return
        self.chat.configure(state="normal")
        for tag, text in chunks:
            self.chat.insert("end", text, tag)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    # ---- API 连接（代理）辅助 ----
    def _copy_text(self, text, label):
        """复制到剪贴板并在 API 页给出轻提示。"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception:
            pass
        if self.api_hint_var is not None:
            self.api_hint_var.set("已复制：%s" % label)
