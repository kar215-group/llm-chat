# -*- coding: utf-8 -*-
"""llm_console.ui.models_ui — 界面 Mixin：模型菜单与切换、模型管理页动作、后台预计算"""

import os
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

from ..core import capability, providers, secrets
from ..core.config import save_config
from ..core.hardware import detect_gpu, detect_ram_gb
from ..core.models import apply_tidy, display_name, find_vl_pairs, is_vl_model, plan_tidy, scan_models, scan_video_models
from ..core.params import auto_ctx_for_model, compute_ngl, current_ngl
from ..core.media import resolve_video_files
from ..connection.stream import request_auto_alias


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
        # 云模型按能力混排进下面三组（不再单列"云端模型"组）：文本进文本组、
        # 生图/生视频进各自组，显示一律是「模型名（云）」
        cloud = {k: providers.cloud_models(self.cfg, k) for k in providers.KIND_ORDER}
        if not chat and not images and not vids and not any(cloud.values()):
            messagebox.showinfo("模型列表", "模型文件夹里没有找到 .gguf 文件：\n%s\n\n"
                                "可在 设置 → 服务参数 → models_dir 修改目录；"
                                "生图模型放在其下的「生图」子文件夹，"
                                "视频组件放在「生视频」子文件夹；"
                                "云端模型在 设置 → 云端 API 里配置。" % d)
            return
        menu = tk.Menu(self.root, tearoff=0, font=("Microsoft YaHei UI", 10))
        cur = os.path.basename(self.cfg["model"])
        cur_id = self.cfg.get("model") if providers.is_cloud(self.cfg) else None

        def add_cloud(kind):
            """把某个能力下的云模型追加进当前分组。"""
            for pid, _pname, m in cloud[kind]:
                mark = "●  " if cur_id == providers.make_cloud_id(pid, m) else "○  "
                menu.add_command(
                    label="%s%s（云）%s" % (mark, m,
                                        "" if secrets.has_api_key(pid) else "（缺密钥）"),
                    command=lambda a=pid, b=m, c=kind: self.pick_cloud_model(a, b, c))

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
            self._append("\n[服务] 已选择生图模型：%s。；"
                         "生图无需启动服务，直接发提示词即可，聊天请切回语言模型。\n" % disp, "meta")
        elif kind == "video":
            missing = resolve_video_files(self.cfg)["missing"]
            self._append("\n[服务] 已选择生视频模型：%s%s。"
                         "生视频同样无需启动服务，直接发提示词即可；附图会作为参考图（Ref2VA），"
                         "聊天请切回语言模型。\n"
                         % (disp,
                            "（链路还缺：%s，补齐后直接发提示词就能用）" % "、".join(missing)
                            if missing else "")
                         , "meta")
        elif self._server_alive_flag:
            self._append("\n[服务] 已选择模型：%s（GPU 层数 %d）。"
                         "当前服务仍在运行原模型，点「重启服务」即可加载新模型。\n"
                         % (disp, current_ngl(self.cfg)), "meta")
        else:
            self._append("\n[服务] 已选择模型：%s（GPU 层数 %d），点「启动服务」加载，状态变为 ● 运行中 后即可对话。\n"
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
                    else "（**还没填 API Key**：设置 → 云端 API → 密钥）")
        if kind == providers.KIND_TEXT:
            note = "无需启动服务，直接发消息即可；本地服务若仍在运行不受影响。"
        else:
            note = ("云端%s还没实现（按规划在%s期），选上也不会走本地引擎——"
                    "要出图/出片请在本地模型那组里选。"
                    % ("生图" if kind == providers.KIND_IMAGE else "生视频",
                       "二" if kind == providers.KIND_IMAGE else "三"))
        self._append("\n[云端] 已选择 %s · %s%s。%s\n"
                     % (model, p.get("name") or pid, key_note, note), "meta")

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
