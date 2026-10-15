# -*- coding: utf-8 -*-
"""llm_console.ui.chat — 界面 Mixin：对话流渲染、输入与附件、发送分派、停止与收尾"""

import base64
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from ..core import capability, chatlog, history, media, providers, textfile
from ..core.config import save_config
from ..core.models import display_name
from ..core.params import ctx_for, current_ngl
from ..core.server import start_server, stop_server
from ..connection import cloud as cloud_conn
from ..connection import cloud_media
from ..connection import stream as stream_conn
from ..connection.stream import stream_worker
from . import imgdecode
from . import theme
from . import widgets


# 「选择附件」文件框里的图片过滤器（默认档是「所有文件」，见 textfile.file_dialog_types；
# 这一个只是给"就想挑张图"的人省点滚动，不再当默认档）。
_IMG_FILTER = ("图片", "*.png *.jpg *.jpeg *.webp *.bmp")

# 附件"高危名"判据（R-113）：只拦明显的密钥 / 凭据类命名，普通文件不受影响——
# 命中先弹一句确认（弹窗文案在 `_attach_picked`）
_SENSITIVE_NAME_RE = re.compile(
    r"(^secrets?\.json$|\.env$|^\.env\.|id_rsa|id_ed25519|\.pem$|\.pfx$|\.p12$|"
    r"credential|apikey|api[_-]?key|token)", re.I)


def _shorten_name(name, keep=10):
    """瓷砖上的文件名大幅缩短：保住扩展名好认类型，正文折叠，全名进悬停气泡。"""
    name = str(name or "")
    if len(name) <= keep:
        return name
    stem, ext = os.path.splitext(name)
    room = max(2, keep - len(ext) - 1)          # 1 是给「…」的
    return stem[:room] + "…" + ext


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
                    "要放开：设置 → 云端模型 → 服务商与密钥 →「选择模型…」→ 把「图片输入」改成"
                    "「支持」，或点「验证图片输入」发一次最小请求让服务端自己回答。")
        return ("当前模型不支持图片输入（%s）。\n"
                "换成本地配了视觉投影器的模型（如 Qwen3VL-8B），"
                "或在 设置 → 云端模型 → 服务商与密钥 →「选择模型…」里把「图片输入」改为「支持」。"
                % res["note"])

    def pick_image(self):
        """📎 入口：按扩展名分派；路由规则全模式一致（W 2026-10-07 裁决）。

        函数名沿用 pick_image 是因为工具栏、测试与既有调用都指它。
        生图/生视频模式差的只是图片附件的语义（参考图/首帧）；文本附件与聊天模式
        同走文档通路，发送时把内容并进提示词（见 _doc_merge）。

        文件框**默认放开所有文件类型**（W 2026-10-08）：选完之后才按扩展名分派 ——
        图片走图片通路，其余走文档通路；系统读不了的格式（PDF / 老 .doc / 不认识的
        扩展名）由 `textfile.read_document` 在挂附件那一刻弹出对应提示，不在挑选时拦。

        **Ctrl 多选（W 2026-10-13）**：文件框换成 askopenfilenames，逐份走同一条
        分派 —— 语义与"连着单选多次"逐字等价（多选返回的是文件框显示顺序，不是
        点选顺序；参考图"第一张"以显示序为准）。聊天 / 生视频仍只发送最后一张
        （现状规则），状态行会照实说（见 _refresh_attach_note）。
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
            # 文件框默认档 = 「所有文件」（W 2026-10-08）：任何类型都允许选，
            # 读不了的在挂附件那一刻给提示（走 _attach_picked → read_document）。
            picked = filedialog.askopenfilenames(
                title=("选择首帧图片或文本附件（图片作为图生视频的第一帧）"
                       if kind == "video" else "选择参考图或文本附件"),
                filetypes=(list(textfile.file_dialog_types())
                           + [_IMG_FILTER]))
            for p in picked or ():
                self._attach_picked(p)
            return
        picked = filedialog.askopenfilenames(
            title="选择附件（图片或文本文件）",
            filetypes=(list(textfile.file_dialog_types())
                       + ([_IMG_FILTER] if not cloud else [])))
        for p in picked or ():
            # 高危文件名确认与生图/生视频分支同一道（R-113；坑 255）：
            # 聊天分支（尤其云端）原来直接挂附件，id_rsa/.env 一类文件无确认进上下文
            if not self._confirm_sensitive(p):
                return
            k = textfile.kind_of(p)
            if k == "image":
                res = capability.resolve(self.cfg)
                if res["verdict"] != capability.YES:
                    # 判据说"不行/没确认"时不硬拦到底：给一次人工放开的机会。
                    # 自动判据会有假阴性（投影器放在别处、文件名不规范、云端根本没能力字段），
                    # 这种时候让用户说了算，比让他去设置页绕一圈快。
                    # 多选口径：说"不"就这一批都不再问（同单选时的 return）；说"是"
                    # 记为该模型支持后，后面几张走上面的快路不再弹。
                    if not messagebox.askyesno(
                            "图片输入",
                            self._no_image_hint(res) +
                            "\n\n仍要按「支持图片输入」处理这个模型吗？\n"
                            "（选「是」= 记为该模型支持，之后不再拦；"
                            "可在 设置 → 云端模型 → 服务商与密钥 →「选择模型…」里改回「自动判断」）"):
                        return
                    capability.set_choice(self.cfg, capability.YES)
                    save_config(self.cfg)
                    self._append("\n[模型] 已把「%s」标记为支持图片输入。\n"
                                 % capability.model_key(self.cfg), "meta")
                    self._update_model_label()
                self.set_attachment(p)
            else:
                self.set_file_attachment(p)

    def _confirm_sensitive(self, p):
        """高危文件名（疑似密钥/凭据）挂附件前确认一道；返回 False = 用户取消。

        聊天与生图/生视频两个选果分支共用同一道（坑 255）：确认弹窗只认文件名、
        与后续按图片/文档分派无关。多选时一张说"不"就取消整批（与既有口径一致）。
        """
        base = os.path.basename(str(p or ""))
        if not _SENSITIVE_NAME_RE.search(base):
            return True
        return bool(messagebox.askyesno(
            "这个文件可能含密钥",
            "「%s」看起来可能包含密钥 / 凭据。\n"
            "继续发送会把文件内容交给模型处理（云端模型还会上传到服务商）。\n\n"
            "确定继续吗？" % base))

    def _attach_picked(self, p):
        """📎 选果的统一路由：图片进参考图列表，其余走文档附件（全模式同一规则）。

        以前生图/生视频模式把选果整个塞进图片列表：选到 .txt / .md 时瓷砖给
        "画不出"的占位样图、发送时当参考图喂给引擎/云端必然出错（坑 158）。

        高危文件名先过一道确认（R-113，W 2026-10-10 批；聊天分支同一道见
        `_confirm_sensitive`，坑 255）：一次误点会把 API Key / token 送进上下文
        （云端模型还会上传到服务商，不可撤回）。
        """
        if not self._confirm_sensitive(p):
            return
        if textfile.kind_of(p) == "image":
            self.set_attachment(p)
        else:
            self.set_file_attachment(p)

    def _doc_merge(self, prompt, doc):
        """生图/生视频发送：把文本附件并进提示词，并在流里留一行 📄（同 _do_send 文档支口径）。

        由各发送入口在预检通过之后调用（坑 133 同纪律）：预检失败不留多余行、
        也不留半截合并的提示词。
        """
        if not doc:
            return prompt
        self._append("📄 已并进提示词 %s｜共 %d 行 / %d 字｜%s｜编码 %s\n"
                     % (doc["name"], doc["total_lines"], doc["chars"],
                        ("发出前 %d 行" % len(doc["kept"])) if doc["dropped"]
                        else "全文", doc["encoding"]), "meta")
        return textfile.render_block(doc["name"], doc["kept"], doc["total_lines"],
                                     doc["chars"], doc["dropped"], doc["encoding"],
                                     question=prompt, budget=doc.get("budget") or 0)

    def set_attachment(self, path):
        """挂一张待发送的图片：追加进列表（顺序即发送顺序），单张视图记"最新一张"。

        缩略图不在这里画：瓷砖由 _rebuild_attach_strip 按当前列表整批画，
        增 / 删 / 换走的都是同一条代码路径，不会留下过期预览。
        预览失败也照旧不拦（坑 132）：Tk 8.6 只认 PNG / 静态 GIF，JPEG / WEBP / BMP
        画不出来时瓷砖退到占位样图（ui/file_icons），而发送走的是**文件路径 / 原始字节**
        （本地引擎的 `-i`、云端参考图的 data URL、聊天模型的 base64），不受影响。
        """
        if not os.path.isfile(path):
            messagebox.showwarning("图片", "这个文件不在了：\n%s" % path)
            return
        if path not in self._attached_images:
            self._attached_images.append(path)
        self._attached_image = path
        self._attached_file = None    # 图与文档不同时挂：发送语义本就互斥，条的形态跟齐
        self._show_attach_bar()

    def set_file_attachment(self, path):
        """文本/Word 附件：读出来、按本次上下文的 token 预算先截好，再挂在附件条上。

        预算：本地按当前模型的 ctx（ctx 是按模型记忆的）− max_tokens − 预留；
        云端取一个保守固定值（各家上下文长度不同，程序无从得知）。
        本地服务在跑时用 /tokenize 精确核验一次（服务没跑就只用保守估算，不为此等 TCP 超时）。
        文档单份（W 2026-10-06 确认）：新挂的文档替换旧的，瓷砖里至多一块；多图才横排。
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
        # 单张视图与图片列表一并清掉：条上只剩这一块文档瓷砖
        self._attached_image = None
        self._attached_images = []
        self._show_attach_bar()

    def _show_attach_bar(self):
        """把附件条贴到输入区上方，并重画瓷砖与右侧模式区。

        用 side="bottom"（而不是 before=输入区）：它和输入区同属"固定外框"，
        要在可伸缩的聊天区之前分到空间，否则窗口一矮就先看不见附件条。
        """
        self.attach_frame.pack(side="bottom", fill="x", padx=10, pady=(2, 0))
        self._rebuild_attach_strip()
        self._refresh_ref_mode_zone()

    def clear_attachment(self):
        """清空附件并收起附件条（发送后、瓷砖删空、image_gen 等外部模块都会调）。"""
        self._attached_image = None
        self._attached_images = []
        self._attached_file = None
        self._tile_photos = []
        self._attach_note_var.set("")
        self.attach_note.grid_remove()
        self._rebuild_attach_strip()
        self.attach_frame.pack_forget()

    # ---- 附件条：横向瓷砖 ----
    def _rebuild_attach_strip(self):
        """按当前状态整批重建瓷砖：图片瓷砖（发送顺序）+ 至多一块文档瓷砖。

        不做差分：瓷砖至多个位数，重建就是十几个控件的事，比维护"哪块变了"便宜，
        也漏不掉旧预览 / 旧气泡。重建后滑到最右 —— 新挂的那块在右边。
        """
        inner = self._tile_inner
        for w in inner.winfo_children():
            w.destroy()
        self._tile_photos = []
        px = self._tile_px
        entries = [("image", p) for p in list(self._attached_images or [])]
        if self._attached_file:
            entries.append(("doc", str(self._attached_file.get("path") or "")))
        for i, (kind, path) in enumerate(entries):
            self._build_attach_tile(inner, i, kind, path, px)
        self.attach_canvas.configure(scrollregion=self.attach_canvas.bbox("all"))
        self.attach_canvas.xview_moveto(1.0)
        self._refresh_attach_note()

    def _build_attach_tile(self, inner, i, kind, path, px):
        """一块瓷砖：预览（或占位样图）在上、缩短文件名在下、右上角 ×、悬停看全名。"""
        tile = tk.Frame(inner, background=theme.c("bg"), highlightthickness=1,
                        highlightbackground=theme.c("border"), padx=4, pady=4)
        tile.grid(row=0, column=i, padx=(0, 6), pady=2)
        photo, fallback = self._tile_preview(kind, path, px)
        if photo is not None:
            self._tile_photos.append(photo)         # 持引用防 GC（同 _remember_photo 的教训）
        prev = tk.Label(tile, image=photo if photo is not None else "",
                        width=px, height=px, background=theme.c("bg"))
        prev.pack()
        name = self._tile_name(kind, path)
        lab = ttk.Label(tile, text=_shorten_name(name), foreground=theme.c("label"),
                        font=("Microsoft YaHei UI", 9))
        lab.pack()
        tip = lambda k=kind, p=path, f=fallback: self._tile_tip(k, p, f)
        for w in (tile, prev, lab):
            widgets.bind_tip(w, tip)
            # 点一下 = 打开涂抹遮罩页（只在本地底图重绘档；其余档在状态行说一句为什么），
            # 按住拖动仍是左右滑动 —— 两者靠"这次按下移动了多少像素"区分，
            # 所以要把这块瓷砖的身份带进拖动处理器。
            w.bind("<Button-1>", lambda e, k=kind, p=path: self._strip_drag_start(e, k, p))
            w.bind("<B1-Motion>", self._strip_drag_move)
            w.bind("<ButtonRelease-1>", self._strip_drag_end)
            w.bind("<MouseWheel>", self._strip_wheel)
        tk.Button(tile, text="×", relief="flat", bd=0, takefocus=0, padx=2,
                  background=theme.c("bg"), activebackground=theme.c("bg"),
                  foreground=theme.c("muted"), activeforeground=theme.c("error"),
                  font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2",
                  command=lambda k=kind, p=path: self._drop_attach_tile(k, p)
                  ).place(relx=1.0, rely=0.0, anchor="ne")

    def _tile_name(self, kind, path):
        if kind == "doc":
            return str((self._attached_file or {}).get("name")
                       or os.path.basename(path or ""))
        return os.path.basename(path or "")

    def _tile_preview(self, kind, path, px):
        """瓷砖预览 → (PhotoImage 或 None, 是否占位图)。

        经 imgdecode.photo：Pillow 优先（全部格式 LANCZOS 真缩略图），缺席或
        画不出来（JPEG / WEBP / BMP，坑 132）退 Tk 原生；再不行用一张内嵌样图
        兜底（ui/file_icons，开发机上由 tools/make_file_icons.py 烧进去），
        条上不再出现"没有预览"的字样。
        """
        if kind == "image":
            img = imgdecode.photo(path, px)
            if img is not None:
                return img, False
            return self._file_icon("IMG", px), True
        return self._file_icon("DOC", px), False

    def _file_icon(self, which, px):
        """内嵌样图 → PhotoImage（按主题选色：深色下用 `*_DARK` 浅线版，坑 185）。

        IMG 占位图没有 DARK 版（它对深色底的观感留待有反馈再说），取不到就回
        浅色版。`tk.PhotoImage(data=)` 加载失败（TclError）时瓷砖空着 —— 这份 Tk
        没编进 PNG 支持，别为它崩掉整条。
        """
        from . import file_icons
        big = "_BIG" if px >= 96 else ""
        dark = "_DARK" if theme.mode() == "dark" else ""
        b64 = getattr(file_icons, "%s_PNG_B64%s%s" % (which, big, dark), None)
        if b64 is None:
            b64 = getattr(file_icons, "%s_PNG_B64%s" % (which, big), None)
        if b64 is None:
            return None
        try:
            return tk.PhotoImage(data=b64)
        except tk.TclError:
            return None

    def _tile_tip(self, kind, path, fallback):
        """悬停内容：完整文件名 + 体量/行数 —— 条上的名字是缩短过的，全名在这里。"""
        if kind == "doc":
            meta = self._attached_file or {}
            parts = [str(meta.get("name") or os.path.basename(path or ""))]
            if meta:
                parts.append("%d 行 / %d 字｜编码 %s"
                             % (meta.get("total_lines", 0), meta.get("chars", 0),
                                meta.get("encoding", "")))
                if meta.get("dropped"):
                    parts.append("超出预算，只带前 %d 行（详见条下状态行）"
                                 % len(meta.get("kept") or []))
            return "\n".join(parts)
        parts = [os.path.basename(path or "")]
        try:
            parts.append("%d KB" % max(1, os.path.getsize(path) // 1024))
        except Exception:
            pass
        if fallback:
            parts.append("这个格式在这里画不出预览（不影响发送）")
        return "\n".join(parts)

    def _drop_attach_tile(self, kind, path):
        """点瓷砖上的 ×：图片从列表里摘一张（单张视图跟着走），文档直接清；空了整条收起。"""
        if kind == "doc":
            self._attached_file = None
        else:
            if path in self._attached_images:
                self._attached_images.remove(path)
            if self._attached_image == path:
                self._attached_image = (self._attached_images[-1]
                                        if self._attached_images else None)
        if not self._attached_images and not self._attached_file:
            self.clear_attachment()
        else:
            self._rebuild_attach_strip()

    def _strip_drag_start(self, e, kind="", path=""):
        self._strip_drag_x = e.x_root
        self._strip_moved = 0
        # 这块瓷砖的身份：松手时若一步没挪（点击），就按它打开涂抹遮罩页。
        # 空白处按下时 kind=""/path="" → 那一下点击什么都不做。
        self._strip_tile = (kind, path) if (kind and path) else None
        try:
            self.attach_canvas.configure(cursor="fleur")
        except Exception:
            pass

    def _strip_drag_move(self, e):
        """按住拖动 = 左右滑动（xscrollincrement=1，units 即像素）。"""
        if self._strip_drag_x is None:
            return
        dx = e.x_root - self._strip_drag_x
        self._strip_drag_x = e.x_root
        self._strip_moved += abs(int(dx))
        if dx:
            self.attach_canvas.xview_scroll(int(-dx), "units")

    def _strip_drag_end(self, _e):
        moved, tile = self._strip_moved, getattr(self, "_strip_tile", None)
        self._strip_drag_x = None
        self._strip_moved = 0
        self._strip_tile = None
        try:
            self.attach_canvas.configure(cursor="")
        except Exception:
            pass
        # 点击（没挪）落在图片瓷砖上 → 涂抹遮罩。阈值 4px：手抖一两像素仍算点击。
        if tile and moved <= 4 and tile[0] == "image":
            self._open_mask_editor(tile[1])

    def _strip_wheel(self, e):
        """滚轮在瓷砖条上 = 横向滑动（这条没有纵向内容）；没溢出时不吞事件。"""
        try:
            left, right = self.attach_canvas.xview()
        except Exception:
            return None
        if left <= 0.0 and right >= 1.0:
            return None                     # 没东西可滑，滚轮还给页面
        self.attach_canvas.xview_scroll(-40 if (getattr(e, "delta", 0) or 0) > 0 else 40,
                                        "units")
        return "break"

    # ---- 附件条右侧：本次带图两档选择 ----

    def _ref_family(self):
        """模式判定用的模型族：云端不需要；本地按发送侧同一口径（resolve_img_files）。"""
        if providers.is_cloud(self.cfg):
            return None
        try:
            d = media.resolve_img_model_path(self.cfg)
            if not d:
                return None
            return media.resolve_img_files(self.cfg, d)["family"]
        except Exception:
            return None

    def _effective_ref_mode(self):
        """本次真正会用的档：手选的支持才作数，否则回该模型的默认档（同 default_ref_mode）。"""
        fam = self._ref_family()
        modes = capability.resolve_modes(self.cfg, fam)
        want = str(self._img_ref_mode or "").strip()
        if want in capability.MODES and modes.get(want, {}).get("ok"):
            return want
        return capability.default_ref_mode(self.cfg, fam)

    def _refresh_ref_mode_zone(self):
        """刷新右侧边界区域：仅生图模型显示；不支持的档置灰 + 悬停给原因与依据。

        置灰不隐藏是 W 2026-10-06 的裁决。调用时机：附件条弹出、模型切换
        （models_ui._update_model_label 末尾）、两档选择变化。
        """
        zone = getattr(self, "refmode_zone", None)
        if zone is None:
            return          # 顶栏先于输入区构建（_update_model_label 那时还没有它）
        if str(self.cfg.get("model_kind") or "") != "image":
            zone.grid_remove()
            return
        zone.grid()
        fam = self._ref_family()
        modes = capability.resolve_modes(self.cfg, fam)
        want = str(self._img_ref_mode or "").strip()
        if want not in capability.MODES or not modes.get(want, {}).get("ok"):
            self._img_ref_mode = ""     # 手选值只在模型支持时生效，否则回默认档
            want = capability.default_ref_mode(self.cfg, fam)
        self._ref_mode_var.set(want)
        for m, rb in (getattr(self, "_refmode_radios", None) or {}).items():
            info = modes.get(m) or {}
            rb.configure(state="normal" if info.get("ok") else "disabled")
            widgets.bind_tip(rb, lambda m=m: self._refmode_tip(m, modes.get(m) or {}))
        # 「重绘强度」只在底图重绘档出现：主体参考根本不传 --strength（自检钉着），
        # 云端生图也没有这个入参 —— 摆着就是让人去拧一个不生效的旋钮。
        row = getattr(self, "_strength_row", None)
        if row is not None:
            if want == capability.MODE_EDIT and not providers.is_cloud(self.cfg):
                lab = next((c for t, c in self._strength_choices
                            if t == self._img_strength_tier), self._strength_choices[0][1])
                self._strength_var.set(lab)
                row.pack(anchor="w", padx=8, pady=(0, 6))
            else:
                row.pack_forget()
        self._refresh_attach_note()

    def _on_strength_pick(self):
        """附件条「重绘强度」下拉：本次任务的档位（"" = 自动按提示词判）。

        与 `_img_ref_mode` 同一口径：会话内跟着这次发送走，**不写配置文件**
        （要长期固定就关 设置 → 本地模型 → 生图（sd.cpp） 里的自动分档，
        并把「重绘强度」填成想要的值）。
        """
        label = str(self._strength_var.get() or "")
        self._img_strength_tier = next(
            (t for t, lab in self._strength_choices if lab == label), "")

    def _strength_tip(self):
        c = self.cfg
        return ("底图重绘时把原图改多少：越高越不像原图，自动＝按提示词判改动幅度"
                "（小改 %s / 普通改 %s / 大幅 %s，在 设置 → 本地模型 → 生图（sd.cpp） 改）"
                % (media.strength_value(c, "light"), media.strength_value(c, "mid"),
                   media.strength_value(c, "heavy")))

    def _refmode_tip(self, mode, info):
        """一句话：置灰时说清"为什么不行"，可用时说清"这一档是干什么的"。"""
        return info.get("why") or capability.modes_note(mode)

    def _on_ref_mode_pick(self):
        self._img_ref_mode = self._ref_mode_var.get()
        # 状态行（多图取舍）与强度行的显隐都跟着档位变 —— 两件都在 `_refresh_ref_mode_zone`
        # 里（它会顺手刷状态行）。原来只刷状态行，而强度行的显隐判在那个函数里 ——
        # 用户手点「主体参考」后那一行会一直挂着，要等换模型/重挂附件才消失
        # （W 2026-10-11 报"选主体参考时是不是可以隐去"）。
        self._refresh_ref_mode_zone()

    def _refresh_attach_note(self):
        """附件条状态行：预算截断 / 底图重绘多图取舍。状态信息留行内，不挪悬停（ADR §5.2）。"""
        var = getattr(self, "_attach_note_var", None)
        if var is None:
            return
        note = ""
        meta = self._attached_file
        # 「底图重绘 + 生图模型」= 遮罩真会被带上的前提（与发送侧同一套判据）
        img_edit = (str(self.cfg.get("model_kind") or "") == "image"
                    and self._effective_ref_mode() == capability.MODE_EDIT)
        if meta and meta.get("dropped"):
            note = ("超出本次上下文预算（约 %d token），先只带前 %d 行；"
                    "云端模型可在 设置 → 云端模型 → 文本模型 打开「让它自己决定读哪些行」。"
                    % (meta.get("budget", 0), len(meta.get("kept") or [])))
        elif img_edit and len(self._attached_images or []) > 1:
            # 与发送侧共用同一份取舍判断（media.plan_ref_images），说的和发的不会对不上
            note = media.plan_ref_images(self._attached_images,
                                         capability.MODE_EDIT)[2]
        elif len(self._attached_images or []) > 1:
            # 多选之后"挂了却只用一张"的两处要照实说（W 2026-10-13 逐字批准）：
            # 聊天同一条消息只带最后一张、图生视频只用最后一张作首帧。
            # 生图模式（本地 / 云端）的多图取舍在上一支或发送侧说，不在这里重复。
            _kind = str(self.cfg.get("model_kind") or "")
            if _kind == "video":
                note = ("图生视频只用最后一张作首帧（已挂 %d 张）"
                        % len(self._attached_images))
            elif _kind == "chat":
                note = ("聊天一次只发送最后一张（已挂 %d 张）"
                        % len(self._attached_images))
        if self._attached_images and img_edit and not providers.is_cloud(self.cfg):
            # 已涂遮罩要看得见（遮罩是"看不见的输入"，不显示就成了静默行为）。
            # 状态行是"当前状态"不是"说明"，所以留在行内（ADR §5.2 判据④）。
            # ① 判据收紧到发送侧那一套：云端生图链路根本不读 `_masks`，切到云端
            #    模型还说"已涂遮罩"就是界面说谎（与坑 212"回显一个值、发另一个值"同族）；
            # ② 与上面那句多图取舍**并排显示**，不再互斥 —— 原来 `if not note` 让
            #    "只认一张"把遮罩那句挤掉，多图时涂了遮罩也一个字不说。
            mp = self._mask_path_of(self._attached_images[0])
            if mp and os.path.isfile(mp):
                mn = "已涂遮罩：只重绘涂抹区，其余保持不变（点图片可改）"
                note = (note + "｜" + mn) if note else mn
        var.set(note)
        if note:
            self.attach_note.grid()
        else:
            self.attach_note.grid_remove()

    def _remember_photo(self, path, img):
        """持引用防 GC；只保留最近 60 张（同路径覆盖，防长会话内存无限增长）。

        ⚠ 淘汰 = 丢掉 Python 引用 → Tk 侧那份图随之销毁，**还显示在对话流里的
        旧图会变空白**。所以上限给得宽（60 张 500px 缩略图约几十 MB），且清空
        对话时整表清掉（`clear_chat`，R-80）。
        """
        self._img_photos[path] = img
        while len(self._img_photos) > 60:
            self._img_photos.pop(next(iter(self._img_photos)))
        return img

    def _append_image(self, path, max_w=320):
        """在聊天流末尾内嵌一张缩略图（normal 状态下插入）。"""
        img = imgdecode.photo(path, max_w)
        if img is None:
            # 只是"这个格式画不出来"（Pillow 缺席 / 文件损坏），**不是发送失败**：
            # 图已经按请求发出去了，所以这里用 meta 说一句，别标成红色错误（坑 132）
            self._append("（这张图在对话里不显示预览：%s）\n" % os.path.basename(path),
                         "meta")
            return
        try:
            self._remember_photo(path, img)
            self.chat.configure(state="normal")
            self.chat.image_create("end", image=img)
            self.chat.insert("end", "\n", "meta")
            self.chat.see("end")
        finally:
            self.chat.configure(state="disabled")

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
        # 闸门判 `_any_busy()`（含远程那头），不再各查各的四个标志：远程生图与本地
        # 生图抢同一张卡，两份 sd-cli 并发 = 显存不够、两边一起失败（99 K1 的双向
        # 互斥；坑 19"四个忙碌标志各查各的，迟早漏一个"就是这一处漏的）。
        if self._any_busy():
            # 用"中止"而不是"取消"：云端任务进入运行态后取消不掉，「停止生成」在那边
            # 只是"不再等它"（_cloud_cancel_task 会把实情说出来），这里别先给错承诺。
            if self._img_busy:
                self._append("\n[提示] 生图进行中：点「停止生成」中止当前任务，或等它完成。\n",
                             "meta")
            elif self._vid_busy:
                self._append("\n[提示] 生视频进行中：点「停止生成」中止当前任务，或等它完成。\n",
                             "meta")
            elif self._remote_busy():
                # 主页面那个「停止生成」停不掉远程任务（它只管本机这一轮），
                # 所以指清出口在哪，别让人对着一个按了没反应的按钮试。
                #  kinds 由 `_remote_busy_kind` 现算：手机那头可能是出图也可能是对话。
                self._append("\n[提示] 手机端有一个远程%s在进行：等它完成，"
                             "或在 设置 → 远程访问 点「停止远程任务」。\n"
                             % self._remote_busy_kind(), "meta")
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            # 只挂附件不打字绝不能彻底静默（R-14）：_do_send 里为"纯图片/纯文件消息"
            # 写的三处兜底（"请描述这张图片。"/"（图片）"/"见附件"）都是按这个场景
            # 设计的，却被"空文本直接 return"挡成死代码——用户只会以为按钮坏了。
            # 生图 / 生视频必须要提示词；普通聊天文本与附件至少要有一样。
            if self.cfg.get("model_kind") in ("image", "video"):
                self._append("\n[提示] 生图/生视频需要提示词：先写一句描述再发送。\n",
                             "meta")
                return
            if not (self._attached_image or self._attached_images
                    or self._attached_file):
                self._append("\n[提示] 还没有发送任何内容：先写点什么，或用 📎 挂一张图/"
                             "一个文本文件（只发附件也可以）。\n", "meta")
                return
            # 有附件：空文本照发（正文由 _do_send 的兜底接手）
        kind = self.cfg.get("model_kind")
        if providers.is_cloud(self.cfg) and kind in ("image", "video"):
            # 云端生图 / 生视频：走服务商原生接口，**完全不碰本地引擎**，也就自然
            # 不占显存——所以这里不走 _confirm_shared_vram 那套三选弹窗。
            # 费用确认（只有生视频）放在清空输入框之前：用户选"不"的时候一个字都不该丢。
            err = providers.validate_for_send(self.cfg)
            refs = list(self._attached_images or [])
            ref = refs[0] if refs else ""
            if not err and refs:
                # 最后一道：📎 入口已经按 capability 判过一次，但中途可能换了服务商，
                # 而且"这张图这一家收不收（模式 / 格式 / 体积 / 张数）"只有接口侧知道。
                v = capability.resolve(self.cfg)
                err = ((v["note"] if v["verdict"] != capability.YES else "")
                       or cloud_media.ref_images_error(
                           providers.current_provider(self.cfg), refs,
                           mode=self._img_ref_mode or ""))
            if err:
                self._append("\n[云端] %s%s\n" % (err, "" if not refs else
                                                  "\n  图片已保留，不会丢。"), "error")
                return
            if kind == "video" and not self._confirm_cloud_video_spend():
                return
            # 输入框与附件的清空由 _start_cloud_* 在各自预检（check + 建目录）通过后做，
            # 与本地链路同一条纪律（坑 133）
            if kind == "image":
                self._start_chat_image(text, ref_imgs=refs, ref_mode=self._img_ref_mode or "",
                                       doc=self._attached_file)
            else:
                # 云端生视频只做文生：选图入口在模型被判"支持看图"时会放行
                # （同一份 capability 云端生图也要用），发送侧必须有这道闸——
                # 原来图片被静默丢弃还照计全费（坑 256）。附件原样保留，
                # 切回本地生视频模型即可直接重发。
                if refs:
                    self._append("\n[云端] 云端生视频目前只支持文生视频，"
                                 "首帧图片不会被发送（图片已保留，不会丢）。\n"
                                 "  要带首帧，请在模型菜单里切回本地的〔生视频〕模型。\n",
                                 "error")
                    return
                self._start_chat_video(text, doc=self._attached_file)
            return
        if self.cfg.get("model_kind") == "image":
            # 方案 A：聊天输入即生图提示词，结果内嵌到聊天流，页面形态不变。
            # ⚠ "生成中再发一条 = 中止旧的再开新的"是**旧注释的死代码**：本函数开头
            # 的忙碌守卫（_img_busy 等）已经先一步拦住并给了提示（R-89），
            # 这里到不了。要改成"打断重发"语义得先动那个守卫，别只改注释。
            choice = self._confirm_shared_vram("生图")
            if choice == "cancel":
                return                          # 输入内容与附图原样保留，什么都没发生
            if choice == "stop":
                self._release_vram()
            # 输入框与附件的清空挪进 _start_chat_image：**预检通过之后**才消费。
            # 摆在这里先清的话，"没找到引擎 / 缺配套件"这类失败会把刚写的提示词和附图
            # 一起吃掉，用户在界面上再也找不回来（坑 133）
            self._start_chat_image(text, ref_imgs=list(self._attached_images or []),
                                 ref_mode=self._img_ref_mode or "",
                                 doc=self._attached_file)
            return
        if self.cfg.get("model_kind") == "video":
            # 与生图同一套形态：输入即提示词，进度原位刷新，结果行带回调按钮。
            # 忙碌守卫在函数开头（见上面 image 分支同款说明，R-89）
            choice = self._confirm_shared_vram("生视频")
            if choice == "cancel":
                return
            if choice == "stop":
                self._release_vram()
            # 同上：清空由 _start_chat_video 在预检通过后做（坑 133）
            self._start_chat_video(text, ref_img=self._attached_image,
                                   doc=self._attached_file)
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
            self._opt_star_refresh()
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
            # 换载前过一遍"必闪退"档确认（2026-10-06）：点否就不动服务、输入原样保留。
            if not self._confirm_fatal_perf(self.cfg["model"]):
                return
            self._pending_text = text
            self.input.delete("1.0", "end")
            self._opt_star_refresh()
            self._begin_svc()
            self.send_btn.configure(state="disabled")
            self._append("\n[服务] 正在加载新模型 %s（GPU 层数 %d），就绪后自动发送你的消息…\n"
                         % (display_name(self.cfg, self.cfg["model"]),
                            current_ngl(self.cfg)), "meta")

            def work():
                stop_server(self.cfg)
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

            def _guarded():
                try:
                    work()
                except Exception as e:
                    # 顶层兜底（R-30）：stop_server / _wait_ready 意外抛错原来会直接
                    # 打死线程——reload_done 永不到达、按钮永久灰、_pending_text 悬挂。
                    # 这里保证终态事件必达：reload_done(False) 的处理会复位 _svc_busy
                    # 并把暂存原文放回输入框（"你的消息已放回输入框"），一个字都不丢。
                    # （_svc_busy 属 ServiceMixin，这里不越界写它——结构检查第 [3] 段）
                    try:
                        self._finish_svc(False, "[服务] 换载模型中断：%s" % e, True)
                    except Exception:
                        pass
                    self._sq.put(("reload_done", False))
            threading.Thread(target=_guarded, daemon=True).start()
            return
        self.input.delete("1.0", "end")
        self._opt_star_refresh()
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
        self._log_turn_start = len(self.history)   # 本轮在 history 里的起点（收尾时按它判要不要记）
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
        # 本轮走哪条链路（本地 / 云端）：「展示思考过程」两个开关独立存储，
        # _poll 渲染时按它分流（2026-10-07 W 定）。忙碌守卫保证一轮中间换不了模型。
        self._turn_cloud = bool(providers.is_cloud(self.cfg))
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
    def _log_records(self):
        """对话记录要写的那份消息 = `history` 去掉"模型没答上来"的整轮。

        `history` 本身一个字都不动：它是下一轮请求的上下文，用户重发时那条提问照旧带着。
        被收窄的只有落盘那一份 —— 记录的含义是"与文本模型正常完成的交互"（W 2026-10-13）。
        没排除过任何轮次时直接返回原列表，不另拷一份。
        """
        if not self._log_drop:
            return self.history
        return [m for i, m in enumerate(self.history) if i not in self._log_drop]

    def _drop_log_file(self):
        """收回本会话已经落盘、但现在没内容可存的那份记录（只删自己写的那个文件）。

        要删的理由很具体：提问在**发出那一刻**就先存了一份（保住断电 / 强杀时不丢问题），
        如果这一轮最终没换来模型一个字，那份"只有提问、没有回答"的文件就成了错误记录——
        正是 W 这次要清掉的东西（开发机 chat_logs 里躺着的那几份 `["user"]` 全是它）。
        删不掉就多留一份旧文件，不抛异常：这条挂在每轮收尾与关窗路径上（约束②）。
        """
        p = self._sess_path
        self._sess_path = ""
        self._sess_saved = False
        self._announced_path = ""
        self._log_key = None
        if not p:
            return
        try:
            os.remove(p)
        except Exception:
            pass

    def _save_chat_log(self, quiet=False):
        """把当前会话写成一整份 JSON（只含有模型回复的轮次）→ (是否已保存, 给用户看的一句话)。

        五条约束：
          ① 生图 / 生视频不存（成果本来就是文件）；
          ② **永不抛异常** —— 它挂在每轮结束与关窗路径上，存不进去只少一份记录，
             不能把发送或退出带崩；
          ③ 整份覆盖重写而不是追加，天然幂等，也不需要"崩溃截断恢复"那套；
          ④ 内容没变就直接返回（`_log_key` 是 (条数, 末条长度) 的廉价签名）。
             连点发送时同一份内容会被三个触发点各敲一次，省掉重复写盘。
             签名按**收窄后**的那份算，否则"只删掉一条被排除的提问"这种变化不会被写盘。
          ⑤ 收窄后一条都不剩 → 把先前那份收掉（见 `_drop_log_file`）。
        """
        if self.cfg.get("model_kind") != "chat" or not chatlog.enabled(self.cfg):
            return False, ""
        recs = self._log_records()
        if not recs:
            self._drop_log_file()
            return False, ""
        key = (len(recs), len(str(recs[-1].get("content") or "")))
        if self._sess_saved and key == self._log_key:
            # 内容没变：不重复写盘，但"说一次保存在哪儿"的账还是要结（可能上次是静默那次）
            if quiet or self._announced_path == self._sess_path:
                return True, ""
            self._announced_path = self._sess_path
            return True, "对话记录已存：%s" % self._sess_path
        if not self._sess_id:
            self._sess_id = chatlog.new_id()
        p, err = chatlog.save(self.cfg, self._sess_id, recs,
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
        reasoning = "".join(self._cur_reasoning).strip()
        if content:
            self.history.append({"role": "assistant", "content": content})
        elif reasoning:
            # 只有【思考】、没有正文（max_tokens 被思考链吃满 finish_reason=length、
            # 流断在思考段、停在思考段）：模型同样吐过字，这一轮算正常交互、照进
            # 记录——"收尾原因不是判据"也覆盖这种半截（W 2026-10-13 口径，坑 259）。
            # history 是下一轮请求的上下文，落这份思考文本与"真实发生过的输出"一致。
            self.history.append({"role": "assistant", "content": reasoning})
        else:
            # 对话记录只收"模型答过话"的轮次（W 2026-10-13 定）：模型一个字都没吐
            # （请求失败、连不上、异常、空回答）才把这一整轮——连那条提问——从记录里排除，
            # 免得落一份"只有提问没有回答"的假记录。
            self._log_drop.add(self._log_turn_start)
        usage, self._last_usage = self._last_usage, None
        if usage and not error and self.cfg.get("show_usage", True):
            # 云端文本填过单价（设置 → 云端文本模型 → 成本估算）就在用量行尾附一句
            # 预估费用；没填什么都不加 —— 不编数字（与 media_price_note 同一口径）
            cost = ""
            if self._turn_cloud:
                try:
                    p = providers.current_provider(self.cfg)
                    sp = providers.split_cloud_id(self.cfg.get("model", ""))
                    cost = providers.text_cost_note(p, sp[1] if sp else "", usage)
                except Exception:
                    cost = ""
            self._append("\n〔用量〕%s%s\n" % (format_usage(usage), cost), "meta")
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
        self._log_drop = set()        # 排除清单跟着新会话清零（下标是从 0 数的）
        self._log_turn_start = 0
        self.clear_attachment()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self._img_photos.clear()      # 对话区已清：图片引用整表释放（R-80，不 clear 会一直占着）
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

    # ---- 本地模型 API（代理）辅助 ----
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
