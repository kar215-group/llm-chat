# -*- coding: utf-8 -*-
"""llm_console.ui.models_ui — 界面 Mixin：模型菜单与切换、模型管理页动作、后台预计算"""

import os
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

from ..core import capability, localmodels, providers, secrets
from ..core.config import save_config
from ..core.hardware import detect_gpu, detect_ram_gb
from ..core.models import apply_tidy, display_name, find_vl_pairs, is_vl_model, plan_tidy, scan_models, scan_video_models
from ..core.params import auto_ctx_for_model, compute_ngl, current_ngl
from ..core.media import resolve_video_files
from ..connection.stream import request_auto_alias
from . import widgets


class ModelsMixin:
    """App 的模型菜单、切换与管理页职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    def _update_model_label(self):
        if providers.is_cloud(self.cfg):
            # 云端模型没有 GGUF 文件，显示名来自注册表，形如「deepseek-flash（云）」
            self.model_var.set(providers.display_of_cloud(self.cfg, self.cfg.get("model", ""))
                               + "  ▾")
            return
        kind = self.cfg.get("model_kind", "chat")
        if kind in ("image", "video"):
            # 类别在模型菜单的分组标题里已经写明（—— 生图模型 ——），按钮上不再重复标注；
            # W 提的：它与云模型的「（云）」后缀视觉上重复
            tag = ""
        else:
            # 聊天模型：把"能不能收图"标出来。判据走 core/capability——
            # 以前只看有没有配对的 mmproj，用户手工声明支持（或云端实测过）时标签不改，
            # 现在三种来源都会反映；"未确认"单独标出来，免得和"确认不支持"混为一谈。
            v = capability.resolve(self.cfg)["verdict"]
            tag = {capability.YES: "", capability.NO: "（纯文本）",
                   capability.UNKNOWN: "（看图未确认）"}[v]
        self.model_var.set(display_name(self.cfg, self.cfg["model"]) + tag + "  ▾")

    # ---- 模型切换 ----
    def show_model_menu(self):
        d, chat, images = scan_models(self.cfg)
        vids, _ens = scan_video_models(self.cfg)
        # 主菜单的显示过滤：`model_hidden` 只影响这里 —— 设置页的模型清单、扫描补全、
        # 8081 代理的模型解析照旧认得全部文件（"不进菜单"不等于"这个模型不存在"）。
        hid = localmodels.hidden_set(self.cfg)
        _n0 = len(chat) + len(images) + len(vids)

        def _vis(p):
            return os.path.basename(p) not in hid

        chat, images, vids = [x for x in chat if _vis(x)], \
            [x for x in images if _vis(x)], [x for x in vids if _vis(x)]
        n_hid = _n0 - (len(chat) + len(images) + len(vids))
        # 云模型按能力混排进下面三组（不再单列"云端模型"组）：文本进文本组、
        # 生图/生视频进各自组，显示一律是「模型名（云）」
        cloud = {k: providers.cloud_models(self.cfg, k) for k in providers.KIND_ORDER}
        if not chat and not images and not vids and not any(cloud.values()):
            if n_hid:
                # 文件在，只是都被"不进主菜单"勾掉了 —— 这跟目录是空的完全是两回事
                messagebox.showinfo("模型列表", "本地模型都在，但都被「不进主菜单」勾掉了：\n"
                                            "去 设置 → 模型文件管理 → 管理本地模型… 里勾回来。")
                return
            messagebox.showinfo("模型列表", "模型文件夹里没有找到 .gguf 文件：\n%s\n\n"
                                "可在 设置 → 服务参数 → models_dir 修改目录；"
                                "生图模型放在其下的「生图」子文件夹，"
                                "视频组件放在「生视频」子文件夹；"
                                "云端模型在 设置 → 云端模型 → 服务商与密钥 里配置。" % d)
            return
        menu = tk.Menu(self.root, tearoff=0, font=("Microsoft YaHei UI", 10))
        cur = os.path.basename(self.cfg["model"])
        cur_id = self.cfg.get("model") if providers.is_cloud(self.cfg) else None

        def add_cloud(kind):
            """把某个能力下的云模型追加进当前分组。

            两条 v40 的规则：
            1) 显示一律用 `short_labels()` 的缩写（标识符仍是原始名，配置里存原名）；
               **按服务商各批一次**，这样不同服务商重名的模型不会被互相顶成长名字。
            2) 模型数超过 FOLD_AT 的服务商，名字前缀一致的收进折叠组。tk.Menu 的
               cascade 天生就是"默认收起、指上去才展开"，不用自己画；组里含着当前
               模型时把 ● 点**在组名上**，否则收起状态下用户看不出自己在用哪一组。
            """
            order, per = [], {}
            for pid, _pname, m in cloud[kind]:
                if pid not in per:
                    per[pid] = []
                    order.append(pid)
                per[pid].append(m)
            for pid in order:
                p = providers.get_provider(self.cfg, pid) or {}
                labels = providers.short_labels(per[pid])
                many = len(p.get("models") or []) > providers.FOLD_AT
                # at=1：只要前缀下凑得出 2 个成员就收起来（分组与否由"这个服务商
                # 整体模型多不多"决定，不是由这一能力下面有几个决定）
                groups, flat = providers.fold_groups(
                    per[pid], at=1 if many else providers.FOLD_AT)

                def item(parent, m):
                    mark = "●  " if cur_id == providers.make_cloud_id(pid, m) else "○  "
                    parent.add_command(
                        label="%s%s（云）%s" % (mark, labels.get(m, m),
                                            "" if secrets.has_api_key(pid)
                                            else "（缺密钥）"),
                        command=lambda a=pid, b=m, c=kind: self.pick_cloud_model(a, b, c))

                def cascade(key, members, rest=False):
                    """一个折叠组（tk.Menu 的 cascade 天生"默认收起"）。

                    rest=True 是"其他"：凑不成同族组的零散模型统一收进来，顶层不再平铺
                    一堆零丁条目（W 提的）。当前模型在其中时把 ● 点在组名上。
                    """
                    sub = tk.Menu(menu, tearoff=0, font=("Microsoft YaHei UI", 10))
                    for m in members:
                        item(sub, m)
                    hit = any(cur_id == providers.make_cloud_id(pid, m) for m in members)
                    menu.add_cascade(
                        label="%s%s · %d 个 ▸" % ("●  " if hit else "", key, len(members)),
                        menu=sub)

                if many and flat:
                    cascade(providers.REST_KEY, flat, rest=True)
                    flat = []
                for m in flat:
                    item(menu, m)
                for g in groups:
                    cascade(g["key"], g["models"])

        if chat or cloud[providers.KIND_TEXT]:
            menu.add_command(label="—— 文本模型 ——", state="disabled")
            _pairs = find_vl_pairs(self.cfg)
            _rec = self.cfg.get("model_mmproj") or {}
            for p in chat:
                b = os.path.basename(p)
                mark = "●  " if b == cur else "○  "
                # 逐行走统一判据：有配对投影器 / 用户声明过 / 实测过，都算"能看图"
                v = capability.resolve_key(self.cfg, b)["verdict"]
                menu.add_command(label=mark + display_name(self.cfg, p)
                                 + ("" if v == capability.YES else
                                    "（纯文本）" if v == capability.NO else "（看图未确认）"),
                                 command=lambda pp=p: self.pick_model(pp))
            add_cloud(providers.KIND_TEXT)
        menu.add_separator()
        menu.add_command(label="—— 生图模型 ——", state="disabled")
        if images or cloud[providers.KIND_IMAGE]:
            for p in images:
                b = os.path.basename(p)
                mark = "●  " if b == cur else "○  "
                menu.add_command(label=mark + display_name(self.cfg, p),
                                 command=lambda pp=p: self.pick_model(pp))
            add_cloud(providers.KIND_IMAGE)
        else:
            menu.add_command(label="（无）", state="disabled")
        menu.add_separator()
        menu.add_command(label="—— 生视频模型 ——", state="disabled")
        if vids or cloud[providers.KIND_VIDEO]:
            for p in vids:
                b = os.path.basename(p)
                mark = "●  " if b == cur else "○  "
                menu.add_command(label=mark + display_name(self.cfg, p),
                                 command=lambda pp=p: self.pick_model(pp))
            add_cloud(providers.KIND_VIDEO)
        else:
            menu.add_command(label="（无：把视频组件的 .gguf 放进模型目录下的「生视频」文件夹）",
                             state="disabled")
        if n_hid:
            # 一句就够：让人知道菜单是被过滤过的、去哪儿放开（否则"我的模型不见了"
            # 只会让人以为程序扫错了目录）
            menu.add_separator()
            menu.add_command(label="另有 %d 个未列入 · 设置 → 模型文件管理 里可放开" % n_hid,
                             state="disabled")
        try:
            menu.tk_popup(self.model_btn.winfo_rootx(),
                          self.model_btn.winfo_rooty() + self.model_btn.winfo_height())
        finally:
            menu.grab_release()

    def pick_model(self, path):
        """切换模型：纯选择操作——只更新配置与界面显示，绝不加载。

        加载只发生在用户点「启动服务 / 重启服务」时（start/restart 读
        cfg["model"]）。这样切换永远即时完成、不阻塞界面。
        """
        if os.path.basename(path) == os.path.basename(self.cfg["model"]):
            return
        if self._busy:
            messagebox.showinfo("切换模型", "正在生成回复，请等本轮结束再切换模型。")
            return
        disp = display_name(self.cfg, path)
        # 类型判定：以"是否出现在对应扫描列表"为准（视频组件 → 生视频；生图目录内的
        # 扩散模型 → 生图），其余是语言模型；生图目录里的视觉组件（如 Qwen3VL-8B）
        # 与普通模型一样属于文本模型
        _d, _chat, _images = scan_models(self.cfg)
        _vids, _ens = scan_video_models(self.cfg)
        npath = os.path.normpath(path)
        kind = ("video" if npath in [os.path.normpath(x) for x in _vids] else
                "image" if npath in [os.path.normpath(x) for x in _images] else "chat")
        # 同步、无阻塞：写配置（毫秒级）+ 刷新顶部显示，到此为止
        self.cfg["model"] = path
        self.cfg["model_kind"] = kind
        self.cfg["model_provider"] = providers.LOCAL    # 切回本地：清掉云端归属
        save_config(self.cfg)
        self._update_model_label()
        self._render_status(self._server_alive_flag, self._server_ready_flag)
        if kind == "image":
            self._append("\n[服务] 已选择生图模型：%s。无需启动服务，直接发提示词和"
                         "参考图（可选）即可；聊天请切回语言模型。\n" % disp, "meta")
        elif kind == "video":
            missing = resolve_video_files(self.cfg)["missing"]
            self._append("\n[服务] 已选择生视频模型：%s%s。无需启动服务，直接发提示词和"
                         "首帧（可选）即可；聊天请切回语言模型。\n"
                         % (disp, "（还缺：%s）" % "、".join(missing) if missing else ""),
                         "meta")
        elif self._server_alive_flag:
            self._append("\n[服务] 已选择模型：%s（GPU 层数 %d）。服务仍在跑原模型，"
                         "点「重启服务」即可加载。\n"
                         % (disp, current_ngl(self.cfg)), "meta")
        else:
            self._append("\n[服务] 已选择模型：%s（GPU 层数 %d）。点「启动服务」加载，"
                         "状态变 ● 运行中 后即可对话。\n"
                         % (disp, current_ngl(self.cfg)), "meta")

    def pick_cloud_model(self, pid, model, kind=None):
        """切换到云端模型：与本地一样是**纯选择操作**（只写配置 + 刷新显示，不发请求）。

        云端没有"加载"概念，所以不需要启动服务、不影响正在运行的本地 llama-server。
        `kind` 是菜单分组给出的能力（text/image/video）；不传就回查注册表。
        """
        mid = providers.make_cloud_id(pid, model)
        if providers.is_cloud(self.cfg) and self.cfg.get("model") == mid:
            return
        if self._busy:
            messagebox.showinfo("切换模型", "正在生成回复，请等本轮结束再切换模型。")
            return
        self.cfg["model"] = mid
        self.cfg["model_provider"] = pid
        kind = kind or providers.model_kind_of(
            providers.get_provider(self.cfg, pid), model)
        # 能力 → 本地那套 model_kind 词表：text=chat、image=video 之外不新增第三种
        self.cfg["model_kind"] = {"text": "chat", "image": "image",
                                  "video": "video"}[kind]
        save_config(self.cfg)
        self._update_model_label()
        self._render_status(self._server_alive_flag, self._server_ready_flag)
        p = providers.get_provider(self.cfg, pid) or {}
        key_note = ("" if secrets.has_api_key(pid)
                    else "（还没填 API Key：设置 → 云端模型 → 服务商与密钥 → 密钥）")
        if kind == providers.KIND_TEXT:
            note = "无需启动服务，直接发消息即可；不影响正在运行的本地服务。"
        else:
            what = "生图" if kind == providers.KIND_IMAGE else "生视频"
            if not providers.supports_media(p, kind):
                note = "「%s」没有可用的原生%s接口，发送前会拦下；可改选本地那组。" % (
                    p.get("name") or pid, what)
            elif kind == providers.KIND_VIDEO:
                # 只留一条不能省的：先建单 + 按秒计费 + 运行中取消不掉。
                # 轮询/下载/24 小时/首帧这些细节都在设置页与 README，别在选模型时罗列
                note = "异步任务，按秒计费，开始运行后就取消不了（发送前确认一次费用）。"
            else:
                note = "无需启动服务，直接发提示词和参考图（可选）即可。"
        self._append("\n[云端] 已选择 %s · %s%s。%s\n"
                     % (providers.short_of(model), p.get("name") or pid, key_note, note),
                     "meta")

    # ---- 本地模型统一管理（设置 → 模型文件管理 →「管理本地模型…」）----
    def open_local_models(self, parent=None):
        """把本地三组模型与各自的配套件摊开，并勾选"进不进顶部菜单"。

        形态与云端「选择模型」一致：可折叠分组 + 每行一个勾选框；一组里超过 FOLD_AT
        个就按名字前缀堆叠，凑不成组的零散条目收进「其他」。比云端多出的一层是"家谱"：
        生图 / 生视频主体下面缩进列出它**实际配到的零件**，其中能聊天的 .gguf 零件
        （Qwen-Image 的 LLM 编码器这类）带自己的勾选框 —— 同一个 BooleanVar，
        所以"文本模型"区与主体下面那两处显示永远同步，不会各勾各的。

        这里只改显示归属，不碰文件：取消勾选的模型仍在设置页清单里、仍可被 8081 代理解析。
        """
        host = parent or self.root
        inv = localmodels.inventory(self.cfg)
        hid = localmodels.hidden_set(self.cfg)
        cur_base = os.path.basename(os.path.normpath(str(self.cfg.get("model", "") or "")))
        win = tk.Toplevel(host)
        win.title("管理本地模型")
        win.geometry("640x560")
        win.minsize(520, 400)
        win.transient(host)

        vars_ = {}

        def var(base):
            if base not in vars_:
                vars_[base] = tk.BooleanVar(value=base not in hid)
            return vars_[base]

        def clip(s, n=34):
            """行标签要短：勾选框 + 右侧说明挤在同一行里，横向不可滚（ScrollPage 只竖滚），
            名字太长就会把说明推出窗口外（真机量到 909 > 640）。"""
            return s if len(s) <= n else s[:n - 1] + "…"

        # ---- 顶部说明（两行，定长：状态类 Label 拼长文案会引发整页重排，见坑 92）----
        head = ttk.Frame(win)
        head.pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(head, wraplength=480, justify="left",
                  text="取消勾选 = 不在顶部模型菜单里出现；文件不动，"
                       "设置页清单与 8081 代理照旧认得它。").pack(anchor="w")
        ttk.Label(head, text="模型目录：%s" % (inv["dir"] or "（未设置）"),
                  foreground="#555555").pack(anchor="w")

        # ---- 底部按钮：先 pack 到底部，中间内容再矮也只压列表 ----
        bot = ttk.Frame(win)
        bot.pack(side="bottom", fill="x", padx=12, pady=(4, 10))
        note = tk.StringVar(value="")
        ttk.Label(bot, textvariable=note, foreground="#5a6a7a").pack(side="left")

        def apply():
            localmodels.set_hidden(self.cfg, [b for b, v in vars_.items() if not v.get()],
                                   known=list(vars_.keys()))
            save_config(self.cfg)
            self._update_model_label()
            win.destroy()

        def hide_companions():
            got = [e for e in inv["chat"] if e.get("companion_of")]
            for e in got:
                var(e["base"]).set(False)
            note.set("已把 %d 个配套编码器移出菜单（点「确定」生效）" % len(got)
                     if got else "没有发现配套编码器")

        ttk.Button(bot, text="确定", command=apply).pack(side="right", padx=(6, 0))
        ttk.Button(bot, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(bot, text="把配套编码器移出菜单",
                   command=hide_companions).pack(side="right")

        page = widgets.ScrollPage(win)
        body = ttk.Frame(page.inner)
        page.set_page(body)

        def row(parent, base, label, info, indent=0):
            """一行：勾选框 + 右侧短说明。返回那行的 Frame（折叠时按它 grid_remove）。"""
            f = ttk.Frame(parent)
            ttk.Checkbutton(f, text=label, variable=var(base),
                            command=lambda b=base: sync_note()).pack(
                side="left", padx=(indent, 0))
            if info:
                # 当前选中的模型单独标出来：它被移出菜单后顶栏仍显示它，得让人看出来
                ttk.Label(f, text=("* 当前  " if base == cur_base else "") + info,
                          foreground="#7a7a7a").pack(side="right")
            return f

        def sync_note():
            n = sum(1 for v in vars_.values() if not v.get())
            note.set("将隐藏 %d 个（点「确定」生效）" % n if n else "")

        def sub_rows(parent, e, row_no):
            """生图 / 生视频主体下面的零件行（缩进一格）。

            只有"会出现在聊天列表里"的零件才给勾选框（`slot["menu"]`）：VAE / CLIP /
            mmproj 这些本来就不是可选模型，给个框只会让人以为勾上它就能在菜单里选到。
            是文本编码器又加载不了的（H3 那种 kv=0 裸权重）就在行尾标"不可对话"，
            不再多解释 —— 想知道为什么自己去看文件头，界面不是讲义（W 明确要求）。
            """
            for s in e["slots"]:
                txt = "%s：%s" % (s["label"], clip(s["base"], 30))
                if s.get("menu"):
                    f = row(parent, s["base"], clip(txt), "配套编码器", indent=18)
                    f.grid(row=row_no, column=0, sticky="w")
                    row_no += 1
                    continue
                if s.get("no_chat"):
                    txt += "（不可对话）"
                f = ttk.Frame(parent)
                ttk.Label(f, text="○ " + clip(txt, 46),
                          foreground="#a15c00" if s.get("no_chat") else "#6a6a6a").pack(
                    side="left", padx=(18, 0))
                f.grid(row=row_no, column=0, sticky="w")
                row_no += 1
            # 可选件缺失 = 不影响出图出片、只是少个功能：弱提示，不进主页面拦截
            for o in e.get("optional_missing") or []:
                f = ttk.Frame(parent)
                ttk.Label(f, text="○ 可选：%s" % clip(o["label"], 20),
                          foreground="#8a8a8a").pack(side="left", padx=(18, 0))
                f.grid(row=row_no, column=0, sticky="w")
                row_no += 1
            # 必需件缺失 = 现在就用不了：主页面发提示词时也会拦下并点名，这里同步列出来
            for miss in e["missing"][:3]:
                f = ttk.Frame(parent)
                ttk.Label(f, text="○ 缺：%s" % clip(miss, 30),
                          foreground="#c01c28").pack(side="left", padx=(18, 0))
                f.grid(row=row_no, column=0, sticky="w")
                row_no += 1
            return row_no

        def section(title, entries, kind):
            """一个能力分组：组头可点（折叠），组内超过 FOLD_AT 个就按前缀堆叠。"""
            if not entries:
                return
            box = ttk.Frame(body)
            box.pack(fill="x", pady=(8, 0))
            state = {"open": True}
            n_hid = sum(1 for e in entries if not var(e["base"]).get())
            head_lbl = tk.Label(box, cursor="hand2", anchor="w",
                                font=("Microsoft YaHei UI", 10, "bold"),
                                bg=widgets.default_bg(self.root))
            head_lbl.pack(fill="x")
            grid = ttk.Frame(box)
            grid.pack(fill="x")
            tail = ttk.Frame(box)          # 占位：折叠后再展开时把 grid 收回它前面
            tail.pack(fill="x")

            def head_text():
                return "%s %s · %d 个%s" % (
                    "▾" if state["open"] else "▸", title, len(entries),
                    "（隐藏 %d）" % n_hid if n_hid else "")

            head_lbl.configure(text=head_text())

            def toggle(_e=None):
                state["open"] = not state["open"]
                if state["open"]:
                    grid.pack(fill="x", before=tail)
                else:
                    grid.pack_forget()
                head_lbl.configure(text=head_text())

            head_lbl.bind("<Button-1>", toggle)

            def draw_entries(parent, items):
                """把一批条目画进 parent 的 grid（行号自己数：主体行 + 它的零件行）。"""
                r = 0
                for e in items:
                    f = row(parent, e["base"], clip(e["name"]), _entry_info(e))
                    f.grid(row=r, column=0, sticky="we")
                    r += 1
                    if kind != "chat":
                        r = sub_rows(parent, e, r)
                return r

            names = [e["base"] for e in entries]
            groups, flat = [], entries
            if len(entries) > providers.FOLD_AT:
                gs, fl = providers.fold_groups(names, at=providers.FOLD_AT)
                by = {e["base"]: e for e in entries}
                groups = [{"key": g["key"], "items": [by[m] for m in g["models"]]}
                          for g in gs]
                flat = [by[m] for m in fl]
                if flat:
                    groups.append({"key": providers.REST_KEY, "items": flat})
                    flat = []
            r = draw_entries(grid, flat)
            for g in groups:
                gf = ttk.Frame(grid)
                gf.grid(row=r, column=0, sticky="we")
                r += 1
                gl = tk.Label(gf, cursor="hand2", anchor="w",
                              font=("Microsoft YaHei UI", 9),
                              bg=widgets.default_bg(self.root))
                gl.pack(fill="x")
                inner = ttk.Frame(gf)
                opened = {"v": False}

                def draw_group(_e=None, gl=gl, inner=inner, g=g, opened=opened):
                    if opened["v"]:
                        inner.pack_forget()
                        opened["v"] = False
                        gl.configure(text="▸ %s · %d 个" % (g["key"], len(g["items"])))
                        return
                    for w in inner.winfo_children():
                        w.destroy()
                    draw_entries(inner, g["items"])
                    inner.pack(fill="x")
                    opened["v"] = True
                    gl.configure(text="▾ %s · %d 个" % (g["key"], len(g["items"])))

                gl.configure(text="▸ %s · %d 个" % (g["key"], len(g["items"])))
                gl.bind("<Button-1>", draw_group)
                if g["key"] == providers.REST_KEY:
                    draw_group()      # 「其他」默认展开：里面本来就是要露面的零散模型
            sync_note()

        def _entry_info(e):
            bits = []
            if e["size_mb"]:
                bits.append("%.1f GB" % (e["size_mb"] / 1024.0)
                            if e["size_mb"] >= 1024 else "%d MB" % e["size_mb"])
            if e["kind"] == "chat":
                bits.append("看图" if e["mmproj"] else "纯文本")
                if e.get("companion_of"):
                    bits.append("配套：%s" % "、".join(
                        clip(x, 18) for x in e["companion_of"][:1]))
            elif e["kind"] in ("image", "video"):
                bits.append(e["family"] or "通用")
                # 两类缺失分开写：必需件缺 = 现在就用不了；可选件缺 = 只是少个功能
                if e["missing"]:
                    bits.append("缺 %d 件·用不了" % len(e["missing"]))
                elif e.get("optional_missing"):
                    bits.append("可选缺 %d" % len(e["optional_missing"]))
            return "｜".join(bits)

        section("文本模型", inv["chat"], "chat")
        section("生图模型", inv["image"], "image")
        section("生视频模型", inv["video"], "video")
        sync_note()
        return win

    # ---- 本机属性 + 新模型 GPU 层数自动计算 ----
    def _ensure_hw_info(self):
        """补齐缺失的本机属性（GPU 型号/显存/内存）；配置里已有值则不问系统。"""
        changed = False
        if not self.cfg.get("vram_gb") or not self.cfg.get("gpu_name"):
            name, vram = detect_gpu()
            if vram > 0 and not self.cfg.get("vram_gb"):
                self.cfg["vram_gb"] = vram
                changed = True
            if name and not self.cfg.get("gpu_name"):
                self.cfg["gpu_name"] = name
                changed = True
        if not self.cfg.get("ram_gb"):
            r = detect_ram_gb()
            if r:
                self.cfg["ram_gb"] = r
                changed = True
        if changed:
            save_config(self.cfg)

    def _precompute_ngl(self):
        """打开页面后的后台自动补全（与手动「扫描并补全缺失项」共用同一实现）。"""
        time.sleep(1.5)   # 等界面稳定，避免和状态线程首探抢 IO
        self._scan_and_fill(force=False)

    # ---- 模型目录扫描与自动配置补全（自动线程 / 手动按钮共用）----
    def _scan_and_fill(self, force=False):
        """扫描模型目录，补全/重算 ngl、主页面 ctx、agent ctx、mmproj 记录。

        force=False：只补缺（手动改过的记录不覆盖）——打开页面时自动执行；
        force=True ：调用方已清空相应记录，全部重算（手动「全部重新计算」）。
        """
        self._ensure_hw_info()
        vram = float(self.cfg.get("vram_gb", 0) or 0)
        if not vram:
            self._sq.put(("note",
                          "未获取到显存容量（nvidia-smi 不可用），新模型将沿用当前 GPU 层数设置；"
                          "可在 设置 → 服务参数 → 显存(GB) 手动填写。"))
            return
        _d, chat, _images = scan_models(self.cfg)
        # 自动配对可看图模型（mmproj）
        for mp, proj in find_vl_pairs(self.cfg).items():
            mb = os.path.basename(mp)
            if force or mb not in (self.cfg.get("model_mmproj") or {}):
                self.cfg.setdefault("model_mmproj", {})[mb] = proj
                save_config(self.cfg)      # 锁在 save_config 内部，勿再外包
                self._sq.put(("note",
                              "已识别可看图模型 %s（自动配对视觉投影器）。"
                              % display_name(self.cfg, mp)))
        for p in chat:
            if self._closing:
                return
            b = os.path.basename(p)
            need_ngl = force or b not in (self.cfg.get("model_ngl") or {})
            need_ctx = force or (b not in (self.cfg.get("model_ctx") or {})
                                 or b not in (self.cfg.get("model_ctx_api") or {}))
            if not (need_ngl or need_ctx):
                continue                       # 已有记录（含手动改过的）不覆盖
            # 先算并写入 ngl（ctx 匹配依赖该记录），再算 ctx
            if need_ngl:
                res = compute_ngl(self.cfg, p, vram)
                if res:
                    self.cfg.setdefault("model_ngl", {})[b] = res["ngl"]
                    save_config(self.cfg)
                    self._sq.put(("ngl", b, res))
                else:
                    self._sq.put(("note",
                                  "模型 %s 无法解析 GGUF 元数据，GPU 层数沿用当前设置"
                                  "（可手动在设置里指定）。" % display_name(self.cfg, p)))
            if need_ctx:
                ctxs = auto_ctx_for_model(self.cfg, p)
                if ctxs:
                    self.cfg.setdefault("model_ctx", {})[b] = ctxs["main"]
                    self.cfg.setdefault("model_ctx_api", {})[b] = ctxs["agent"]
                    save_config(self.cfg)
                    self._sq.put(("note",
                                  "已为模型 %s 匹配 context：主页面 %d / agent %d"
                                  "（可在设置中调整）。"
                                  % (display_name(self.cfg, p),
                                     ctxs["main"], ctxs["agent"])))

    # ---- 模型管理（手动操作；自动线程与手动按钮共用 _scan_and_fill）----
    def _manual_scan(self, force=False):
        if force:
            if not messagebox.askyesno(
                    "全部重新计算",
                    "将清空所有模型的 层数/context/mmproj 自动记录并重新计算，\n"
                    "手动调整过的值也会被覆盖。确定继续？"):
                return
            for k in ("model_ngl", "model_ctx", "model_ctx_api", "model_mmproj"):
                self.cfg[k] = {}
            save_config(self.cfg)
        self._append("\n[模型管理] 开始%s扫描模型目录…\n"
                     % ("重新" if force else "补全"), "meta")
        threading.Thread(target=self._scan_and_fill, args=(force,),
                         daemon=True).start()

    def _reindex_after_tidy(self):
        """整理移动后：清理失效记录并按新位置重新配对 mmproj。"""
        c = self.cfg
        rec = c.get("model_mmproj") or {}
        c["model_mmproj"] = {k: v for k, v in rec.items() if os.path.isfile(v)}
        for mp, proj in find_vl_pairs(c).items():
            c["model_mmproj"].setdefault(os.path.basename(mp), proj)
        save_config(c)
        self._update_model_label()

    def _open_tidy_dialog(self):
        if self._svc_busy or self._busy or self._img_busy:
            messagebox.showinfo("整理模型文件夹",
                                "当前有任务正在进行（生成/生图/服务操作），\n"
                                "请等任务结束、服务停止后再整理文件。")
            return
        moves, unpaired = plan_tidy(self.cfg)
        if not moves and not unpaired:
            messagebox.showinfo("整理模型文件夹",
                                "未发现需要整理的文件：\n"
                                "顶层没有散落的模型（或它们已在各自文件夹内）。")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("整理模型文件夹 - 预览")
        dlg.geometry("780x520")
        dlg.transient(self.root)
        txt = tk.Text(dlg, font=("Consolas", 9), wrap="none")
        txt.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        out = []
        if moves:
            out.append("将创建子文件夹并移动以下文件（同盘操作，瞬时完成）：\n")
            for src, dst, kind in moves:
                out.append("  [%-18s] %s" % (kind, os.path.basename(src)))
                out.append("  %22s -> %s\\\n" % ("", os.path.basename(dst)))
        if unpaired:
            out.append("\n以下 mmproj 名称无法判断归属，将保持原位（请手动处理）：\n")
            for p in unpaired:
                out.append("  " + os.path.basename(p))
        txt.insert("1.0", "\n".join(out))
        txt.configure(state="disabled")
        bar = ttk.Frame(dlg)
        bar.pack(fill="x", padx=10, pady=(0, 10))

        def do_exec():
            ok, fails = apply_tidy(moves)
            c = self.cfg
            for src, dst, kind in moves:
                new = os.path.join(dst, os.path.basename(src))
                sb = os.path.basename(src)
                if os.path.normpath(c.get("model", "")) == os.path.normpath(src):
                    c["model"] = new
                if sb in (c.get("model_mmproj") or {}):
                    c["model_mmproj"][sb] = new
            save_config(c)
            self._reindex_after_tidy()
            dlg.destroy()
            msg = "整理完成：移动 %d 个文件。" % ok
            if fails:
                msg += "\n\n未成功 %d 个：\n" % len(fails)
                msg += "\n".join("  %s（%s）" % (os.path.basename(s), r)
                                 for s, r in fails[:10])
            messagebox.showinfo("整理模型文件夹", msg)
            self._append("\n[模型管理] 整理完成：移动 %d 个文件%s。\n"
                         % (ok, ("，失败 %d 个" % len(fails)) if fails else ""),
                         "meta")

        ttk.Button(bar, text="执行", command=do_exec).pack(side="right", padx=4)
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right")

    # ---- 模型自动命名 ----
    def _auto_alias_thread(self, path):
        """【暂时没用，后续可能启用】向模型请求别名；现由 make_alias() 代码生成。"""
        alias = request_auto_alias(self.cfg, path)
        if alias:
            self.cfg.setdefault("model_aliases", {})[os.path.basename(path)] = alias
            save_config(self.cfg)
            self._sq.put(("alias", os.path.basename(path), alias))
        else:
            self._sq.put(("note", "模型自动命名失败，该模型将继续显示完整文件名。"))
