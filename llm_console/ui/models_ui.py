# -*- coding: utf-8 -*-
"""llm_console.ui.models_ui — 界面 Mixin：模型菜单与切换、模型管理页动作、进页后台补全与手动定向"""

import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

from ..core import capability, localmodels, modelreq, providers, secrets
from ..core.config import save_config
from ..core.hardware import detect_gpu, detect_ram_gb
from ..core.models import (ENGINE_LABEL, apply_tidy, display_name, engine_ready,
                          find_vl_pairs, first_usable, plan_tidy, scan_models,
                          scan_video_models, selected_usable, short_alias,
                          usable_local)
from ..core.params import auto_ctx_for_model, compute_ngl, current_ngl
from ..core.media import resolve_video_files
from ..connection.stream import request_auto_alias
from . import widgets

# 「没选上模型」在按钮上的说法。以前这种情况跟着能力判据写成「（纯文本）」，
# 用户读到的是"这个模型只能纯文本"，而真相是"一个模型都还没得选"。
NO_MODEL_LABEL = "（无可用模型）"


def model_missing(cfg):
    """「根本没选上模型」的判据 —— 与 `main()` 启动时"从目录里挑一个能聊的顶上"同一条。

    顶栏按钮与启动欢迎语两个入口都问它（坑 128：判据只写在一个入口里，另一个迟早走偏）。
    """
    cur = str(cfg.get("model") or "")
    if providers.is_cloud(cfg):
        return not cur            # 云端是 "pid::model" 复合 id，没有文件可言
    return not cur or not os.path.isfile(cur)


class ModelsMixin:
    """App 的模型菜单、切换与管理页职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    def _maybe_adopt_first_model(self, force=False):
        """第一个"现在就能用"的模型配好时，主页面**立即**切过去（一次性，坑 150）。

        判据只有一处：`core.models.selected_usable` / `first_usable`
        （本地 = 对应引擎的文件真的存在 × 模型在；云端 = 服务商启用且填过密钥）。
        四个要点：
          · **只在"当前选中不可用"时动手** —— 已经在用一个能用的模型就一个字节都不改。
          · **一次性**：`cfg["model_auto_picked"]` 记下"已经替用户选过一次"（用户自己
            在菜单里选过同样置位），之后程序不再插手。
          · **"删光全部模型"会重新武装**：一个可用的都没有时把标记清掉，所以用户清空
            模型目录后再配第一个模型，这条会再生效一次（W 口径）。
          · **正在忙就跳过**（生成 / 生图 / 生视频 / 服务操作）：换模型不能插进任务中间，
            下一轮再来。

        `force=True` 给"用户刚做完某个动作"的调用点（设置页保存、引擎装完、手动定向
        模型）—— 那几处要的是**立刻**响应，不等冷却。`force=False` 是状态轮询里的安全网，
        带冷却（`core.throttle`），免得每 3 秒白扫一次盘（坑 4 的通用纪律）。
        """
        th = getattr(self, "_auto_pick", None)
        if not force:
            if th is not None and not th.due():
                return
            # 便宜的先问：一个引擎都没就位、也没填密钥 ⇒ 不可能有可用模型，别白扫盘
            if not (engine_ready(self.cfg, "llama") or engine_ready(self.cfg, "sd")
                    or any(secrets.has_api_key(p.get("id"))
                            for p in (self.cfg.get("cloud_providers") or [])
                            if (p or {}).get("enabled", True))):
                return
        if self._busy or self._svc_busy or self._img_busy or self._vid_busy:
            return
        if th is not None:
            th.mark()
        if self.cfg.get("model_auto_picked"):
            # 已接管过。绝大多数轮询在这里就收工（`selected_usable` 走快路径，不扫盘）。
            if selected_usable(self.cfg):
                return
            if first_usable(self.cfg):
                return                # 还有别的可用模型，但用户已经选过 ⇒ 不插手
            # 一个可用的都不剩 = 用户把模型删光了 ⇒ 重新武装，下一个配好的会再切一次
            self.cfg.pop("model_auto_picked", None)
            save_config(self.cfg)
            return
        if selected_usable(self.cfg):
            self.cfg["model_auto_picked"] = True     # 已经有一个能用的了
            return
        got = first_usable(self.cfg)
        if not got:
            return                                   # 还没有可用的：等下一轮（标记不置位）
        # 三类都存**绝对路径**（与 `pick_model` 同一形状）：`media.resolve_*_model_path`
        # 判的是 `os.path.isfile(cfg["model"])`，存文件名会让生图 / 生视频直接报"未找到模型"
        self.cfg["model"] = got["id"]
        self.cfg["model_kind"] = got["kind"]
        self.cfg["model_provider"] = got["provider"]
        self.cfg["model_auto_picked"] = True
        save_config(self.cfg)
        self._update_model_label()
        self._render_status(self._server_alive_flag, self._server_ready_flag)
        if providers.is_cloud(self.cfg):
            name = providers.display_of_cloud(self.cfg, got["id"])
        else:
            name = display_name(self.cfg, got["id"])
        what = {"chat": "点「启动服务」加载后即可对话",
                "image": "无需启动服务，直接发提示词和参考图（可选）即可",
                "video": "无需启动服务，直接发提示词和首帧（可选）即可"}[got["kind"]]
        self._append("\n[模型] 已自动选中第一个可用的模型：%s —— %s。\n"
                     "（换模型点顶部模型名；这条自动选择只发生一次，删光全部模型后才会再来一次。）\n"
                     % (name, what), "meta")

    def _update_model_label(self):
        # 附件条右侧「带图方式」区跟着模型走：仅生图模型显示、置灰态随家族/服务商刷新。
        # 放在所有分支之前 —— 从生图切回聊天时也必须把那块区域收起来
        # （方法内部有 getattr 守卫，顶栏先于输入区构建时调用是空操作）。
        self._refresh_ref_mode_zone()
        cur = str(self.cfg.get("model") or "")
        if model_missing(self.cfg):
            # 「（纯文本）」是**能力**标注，只有真选上了模型才谈得上能力：一个 .gguf 都没有
            # （或配置指的文件已不在 / 云端没挑模型）时写它，用户读到的是"这模型不能看图"
            self.model_var.set(NO_MODEL_LABEL + "  ▾")
            return
        if providers.is_cloud(self.cfg):
            # 云端模型没有 GGUF 文件，显示名来自注册表，形如「deepseek-flash（云）」
            self.model_var.set(providers.display_of_cloud(self.cfg, cur) + "  ▾")
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
        # 顶栏限长只在这一处生效（core.models.short_alias）：模型菜单那边仍用完整的
        # display_name —— 菜单一行放得下 30 字，砍短反而认不出模型（W 2026-10-03）。
        self.model_var.set(short_alias(display_name(self.cfg, cur)) + tag + "  ▾")

    # ---- 模型切换 ----
    def show_model_menu(self):
        # 引擎没就位的那一组**不列**（坑 150）：菜单里的每个条目都得是真能发出去的 ——
        # 列一条选上去就跑不了的模型，比不列更糟。判据与"自动选中第一个可用模型"、
        # 引导第一屏、一键诊断共用 `core.models.usable_local` 一处（坑 128）。
        # 只管主菜单：设置页的模型清单、扫描补全、8081 代理的模型解析照旧认得全部文件
        # （沿用 `localmodels.hidden_set` 那条"显示层的事"口径）。
        use = usable_local(self.cfg)
        d = use["dir"]
        # 用户自己勾掉的「不进主菜单」是另一回事：那要算进「另有 N 个未列入」，
        # 而"这台机器上引擎还没装"不是用户的选择，不占那个名额。
        hid = localmodels.hidden_set(self.cfg)
        _n0 = len(use["chat"]) + len(use["image"]) + len(use["video"])

        def _vis(p):
            return os.path.basename(p) not in hid

        chat = [x for x in use["chat"] if _vis(x)]
        images = [x for x in use["image"] if _vis(x)]
        vids = [x for x in use["video"] if _vis(x)]
        n_hid = _n0 - (len(chat) + len(images) + len(vids))
        # 云模型按能力混排进下面三组（不再单列"云端模型"组）：文本进文本组、
        # 生图/生视频进各自组，显示一律是「模型名（云）」
        cloud = {k: providers.cloud_models(self.cfg, k) for k in providers.KIND_ORDER}
        if not chat and not images and not vids and not any(cloud.values()):
            if n_hid:
                # 文件在，只是都被"不进主菜单"勾掉了 —— 这跟目录是空的完全是两回事
                messagebox.showinfo("模型列表", "本地模型都在，但都被「不进主菜单」勾掉了：\n"
                                            "去 设置 → 本地模型 → 模型文件与引擎 里的「管理本地模型…」勾回来。")
                return
            if not (engine_ready(self.cfg, "chat") or engine_ready(self.cfg, "media")):
                # 一个引擎都没就位：先说引擎，别让用户以为模型没扫到
                messagebox.showinfo(
                    "模型列表",
                    "还没就位的引擎：\n· %s（本地对话）\n· %s（本地生图 / 生视频）\n\n"
                    "去 设置 → 本地模型 → 模型文件与引擎 点「检查更新 → 更新引擎」装一份，"
                    "或用「自动定向」指到本机已有的那一份；\n"
                    "只想用云端：在 设置 → 云端模型 → 服务商与密钥 填密钥，"
                    "再到「选择模型」里勾上要用那几个。"
                    % (ENGINE_LABEL["llama"], ENGINE_LABEL["sd"]))
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
        # 分组里一项都没有时那句提示：引擎没装要说清是引擎的事（不然用户去翻模型目录）
        no_llm = "" if engine_ready(self.cfg, "chat") else "：还没就位 %s" % ENGINE_LABEL["llama"]
        no_sd = "" if engine_ready(self.cfg, "sd") else "：还没就位 %s" % ENGINE_LABEL["sd"]
        no_vid = ("（无：把视频组件的 .gguf 放进模型目录下的「生视频」文件夹）"
                  if not no_sd else "（无%s）" % no_sd)

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

        # 三组一律给出组头（原来文本组没内容时整组消失，用户看不到"这台机器缺什么"）：
        # 空组用一行「（无：还没就位 …）」占位，与生图 / 生视频那两组同一形状。
        menu.add_command(label="—— 文本模型 ——", state="disabled")
        if chat or cloud[providers.KIND_TEXT]:
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
        else:
            menu.add_command(label="（无%s）" % no_llm, state="disabled")
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
            menu.add_command(label="（无%s）" % no_sd, state="disabled")
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
            menu.add_command(label=no_vid, state="disabled")
        if n_hid:
            # 一句就够：让人知道菜单是被过滤过的、去哪儿放开（否则"我的模型不见了"
            # 只会让人以为程序扫错了目录）
            menu.add_separator()
            menu.add_command(label="另有 %d 个未列入 · 设置 → 模型文件与引擎 里可放开" % n_hid,
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
        if self._any_busy():
            # 四标志统一判（坑 19）：生图 / 生视频 / 服务操作期间切模型会让
            # cfg["model"] 与在跑任务、顶栏显示三方错位（文案 2026-10-07 已报 W 复核）
            messagebox.showinfo("切换模型",
                                "当前有任务正在进行（生成 / 生图 / 生视频 / 服务操作），\n"
                                "请等任务结束再切换模型。")
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
        self.cfg["model_auto_picked"] = True   # 用户自己选的 ⇒ 记下"别再自动接管"
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
        if self._any_busy():
            messagebox.showinfo("切换模型",
                                "当前有任务正在进行（生成 / 生图 / 生视频 / 服务操作），\n"
                                "请等任务结束再切换模型。")
            return
        self.cfg["model"] = mid
        self.cfg["model_provider"] = pid
        self.cfg["model_auto_picked"] = True   # 用户自己选的 ⇒ 记下"别再自动接管"
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

    # ---- 本地模型统一管理（设置 → 模型文件与引擎 →「管理本地模型…」）----
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
        win.withdraw()          # 先藏起来，摆正了再显示（否则左上角闪一下）
        win.title("管理本地模型")
        # 780 宽（W 2026-10-06）：640 时"勾选框 + 文件名 + 右侧说明"这种长行被压得
        # 只剩截断；窗口行有 fit_text 兜底（<Configure> 按新宽度重裁），加宽即恢复全文
        win.geometry("780x560")
        win.minsize(600, 400)
        win.transient(host)

        vars_ = {}

        def var(base):
            if base not in vars_:
                vars_[base] = tk.BooleanVar(value=base not in hid)
            return vars_[base]

        # ---- 顶部说明（定长：状态类 Label 拼长文案会引发整页重排，见坑 92）----
        head = ttk.Frame(win)
        head.pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(head, text="不勾选则不显示于主页面").pack(anchor="w")
        dir_lbl = ttk.Label(head, text="模型目录：%s" % (inv["dir"] or "（未设置）"),
                            foreground="#555555")
        dir_lbl.pack(anchor="w", fill="x")
        # 路径只占一行的话，长了就被窗口右沿硬切掉（无滚动条）：跟着可用宽度换行
        head.bind("<Configure>",
                  lambda e: dir_lbl.configure(wraplength=max(e.width - 24, 120)))

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

        # ---- 行标签按**窗口实际宽度**裁，不再是固定字数（坑 106 / 坑 92）----
        # 勾选框那行的名字与右侧说明挤在同一行、横向不可滚（ScrollPage 只竖滚），只能从
        # "显示"这一侧解决。原来一律 clip(34) 字：窗口拉大也照样是省略号 —— 现在量着可用
        # 像素裁，窗口一宽就少截 / 不截（与设置页那份云端模型清单同一个做法）。
        _font = tkfont.nametofont("TkDefaultFont")
        _probe = ttk.Checkbutton(win, text="")
        win.update_idletasks()
        _CK_PAD = _probe.winfo_reqwidth()   # 勾选框本体（指示器 + 内边距），不含文字
        _probe.destroy()
        _PADX = 34                          # ScrollPage.set_page 给内容的左右留白 16 + 18
        fits = []                           # [(控件, 完整文本, 右侧要留出的像素)]
        _last_w = {"v": -1}                 # 上一次裁过的画布宽度（尺寸没变就不重画，坑 121）

        def fit_text(s, avail):
            if avail <= 24 or _font.measure(s) <= avail:
                return s
            lo, hi = 0, len(s)           # 最大的 k 使 s[:k] + "…" 放得下；0 = 一个都放不下
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _font.measure(s[:mid] + "…") <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            return (s[:lo] if lo else s[:1]) + "…"

        def fit(w, full, reserved=0):
            """登记一段文本：窗口拉宽时它跟着少截 / 不截。"""
            if _last_w["v"] > 1:        # 已经量过宽度 → 后加进来的行当场就裁对
                w.configure(text=fit_text(full, _last_w["v"] - reserved))
            fits.append((w, full, reserved))

        def fit_all():
            avail = page.canvas.winfo_width()
            if avail < 60:              # 还没量到真宽度（未映射时是 1）：等第一次 <Configure>
                return
            alive = []
            for w, full, reserved in fits:
                try:
                    if not w.winfo_exists():
                        continue
                except Exception:
                    continue
                alive.append((w, full, reserved))
                w.configure(text=fit_text(full, avail - reserved))
            fits[:] = alive

        def on_resize(_e=None):
            w = page.canvas.winfo_width()
            if w == _last_w["v"]:
                return
            _last_w["v"] = w
            fit_all()

        page.canvas.bind("<Configure>", on_resize, add="+")

        def row(parent, base, label, info, indent=0):
            """一行：勾选框 + 右侧短说明。返回那行的 Frame（折叠时按它 grid_remove）。"""
            f = ttk.Frame(parent)
            cb = ttk.Checkbutton(f, text=label, variable=var(base),
                                 command=lambda b=base: sync_note())
            cb.pack(side="left", padx=(indent, 0))
            reserved = _PADX + _CK_PAD + indent
            if info:
                # 当前选中的模型单独标出来：它被移出菜单后顶栏仍显示它，得让人看出来
                txt = ("* 当前  " if base == cur_base else "") + info
                ttk.Label(f, text=txt, foreground="#7a7a7a").pack(side="right")
                reserved += _font.measure(txt) + 8
            fit(cb, label, reserved)
            return f

        def plain(parent, text, color, row_no, indent=18):
            """没有勾选框的说明行（零件 / 可选件 / 缺件），同样按宽度裁。"""
            f = ttk.Frame(parent)
            lbl = ttk.Label(f, text=text, foreground=color)
            lbl.pack(side="left", padx=(indent, 0))
            fit(lbl, text, _PADX + indent + 6)
            f.grid(row=row_no, column=0, sticky="w")
            return row_no + 1

        def sync_note():
            n = sum(1 for v in vars_.values() if not v.get())
            note.set("将隐藏 %d 个，点「确定」生效" % n if n else "")

        def sub_rows(parent, e, row_no):
            """生图 / 生视频主体下面的零件行（缩进一格）。

            只有"会出现在聊天列表里"的零件才给勾选框（`slot["menu"]`）：VAE / CLIP /
            mmproj 这些本来就不是可选模型，给个框只会让人以为勾上它就能在菜单里选到。
            是文本编码器又加载不了的（H3 那种 kv=0 裸权重）就在行尾标"不可对话"，
            不再多解释 —— 想知道为什么自己去看文件头，界面不是讲义（W 明确要求）。
            """
            for s in e["slots"]:
                txt = "%s：%s" % (s["label"], s["base"])
                if s.get("menu"):
                    f = row(parent, s["base"], txt, "配套编码器", indent=18)
                    f.grid(row=row_no, column=0, sticky="w")
                    row_no += 1
                    continue
                if s.get("no_chat"):
                    txt += "（不可对话）"
                row_no = plain(parent, "○ " + txt,
                               "#a15c00" if s.get("no_chat") else "#6a6a6a", row_no)
            # 可选件缺失 = 不影响出图出片、只是少个功能：弱提示，不进主页面拦截
            for o in e.get("optional_missing") or []:
                row_no = plain(parent, "○ 可选：%s" % o["label"], "#8a8a8a", row_no)
            # 必需件缺失 = 现在就用不了：主页面发提示词时也会拦下并点名，这里同步列出来
            for miss in e["missing"][:3]:
                row_no = plain(parent, "○ 缺：%s" % miss, "#c01c28", row_no)
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
                    f = row(parent, e["base"], e["name"], _entry_info(e))
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
                # 看图 / 纯文本走**统一判据**（与顶栏、模型菜单同一个入口），不再只看
                # find_vl_pairs 的配对结果：投影器名字里不带模型名时（`mmproj-BF16.gguf`）
                # 配对判据可能认不出来，而配置里那条 model_mmproj 是认得的 —— 两处说法
                # 打架就是这么来的（坑 128）。真机 W 的 Qwen3.8-27B 原来在这里被写成纯文本。
                verdict = capability.resolve_key(self.cfg, e["base"], kind="chat")["verdict"]
                bits.append({capability.YES: "看图",
                             capability.NO: "纯文本"}.get(verdict, "看图未确认"))
                if e.get("companion_of"):
                    bits.append("配套：%s" % "、".join(e["companion_of"][:1]))
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
        widgets.center_on(win, host)   # 摆到触发它的窗口正中，别落在屏幕左上角
        return win

    # ---- 该机器硬件属性 + 新模型 GPU 层数自动计算 ----
    def _ensure_hw_info(self):
        """补齐缺失的硬件属性（GPU 型号/显存/内存）；配置里已有值则不问系统。"""
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

    def _auto_scan_models(self):
        """进「模型文件与引擎」页时自动**补全缺失项**一次（与手动按钮共用 `_scan_and_fill`）。

        W 2026-10-05：原来这是启动时跑一次（`_precompute_ngl`），现在改成"进页跑一次 + 10 分钟
        冷却"——**判据与冷却读数只在 core.throttle 一处**（与「检查更新」的进页冷却共用那套），
        这是本项的会话状态（挂在 App 上，关掉设置窗再开仍在）。
        手动点「补全缺失项」不受冷却限制（用户亲手点的动作不该被节流拦住）。
        """
        th = getattr(self, "_model_scan", None)
        if th is None or not th.due():
            return
        th.mark()
        # 不在主页面预告/汇报（W 2026-10-06：例行输出不上对话区）；失败类提示仍保留
        threading.Thread(target=self._scan_and_fill, args=(False, True),
                         daemon=True).start()

    # ---- 模型目录扫描与自动配置补全（自动线程 / 手动按钮共用 _scan_and_fill）----
    def _scan_and_fill(self, force=False, quiet=False):
        """扫描模型目录，补全/重算 ngl、主页面 ctx、agent ctx、mmproj 记录。

        force=False：只补缺（手动改过的记录不覆盖）——进页自动 / 手动「补全缺失项」都走它；
        force=True ：调用方已清空相应记录，全部重算（手动「全部重新计算」）。
        quiet=True ：**自动路径**（进页触发）—— 成功类提示一律不上主页面输出栏
        （W 2026-10-06），只有失败（元数据解析不了 / 显存没探到）才开口；手动路径
        照旧汇报，用户点了按钮就该有反馈。
        """
        self._ensure_hw_info()
        vram = float(self.cfg.get("vram_gb", 0) or 0)
        if not vram:
            # 纯核显 / 没探到独显是**正常状态**不是故障（W 2026-10-06）：照常补全，
            # GPU 层数按 0（纯 CPU）推算、ctx 由内存预算推导（compute_ngl / auto_ctx
            # 同一口径）。手动路径提一句，自动路径不出声（例行输出不上对话区）。
            if not quiet:
                self._sq.put(("note",
                              "未探测到显存容量：新模型按纯 CPU（GPU 层数 0）推算；"
                              "有独立显卡时可在 设置 → 服务参数 → 显存(GB) 手动填写。"))
        _d, chat, images = scan_models(self.cfg)
        # 补全结果先攒在内存里，最后**一次性**落盘：原来每补一项就 save_config 一次
        # （首跑 N 个模型 = 2N+1 次写盘，每次一份 .tmp + os.replace）。这一步跑在后台
        # 线程里，整批算完只要十几毫秒，所以"合并成一次写"既少占磁盘、也少和主线程的
        # 保存抢锁（坑 135）。中途要提前退出（关窗）时也必须先把已有改动落盘。
        _dirty = {"v": False}

        def _flush():
            if _dirty["v"]:
                _dirty["v"] = False
                save_config(self.cfg)

        # 自动配对可看图模型（mmproj）
        for mp, proj in find_vl_pairs(self.cfg).items():
            mb = os.path.basename(mp)
            if force or mb not in (self.cfg.get("model_mmproj") or {}):
                self.cfg.setdefault("model_mmproj", {})[mb] = proj
                _dirty["v"] = True
                if not quiet:
                    self._sq.put(("note",
                                  "已识别可看图模型 %s（自动配对视觉投影器）。"
                                  % display_name(self.cfg, mp)))
        for p in chat:
            if self._closing:
                _flush()
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
                    _dirty["v"] = True
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
                    _dirty["v"] = True
                    if not quiet:
                        self._sq.put(("note",
                                      "已为模型 %s 匹配 context：主页面 %d / agent %d"
                                      "（可在设置中调整）。"
                                      % (display_name(self.cfg, p),
                                         ctxs["main"], ctxs["agent"])))

        # ---- 性能分级（2026-10-06 W）：扫描发现的新模型评估一次，级别记进
        # perf_reported（文件删掉的剪枝掉）。**评估结果不进输出栏**（W 第二轮）——
        # 目前唯一的可见出口是启动 / 发送前 `_confirm_fatal_perf` 的 3 级弹窗（现场重算），
        # 登记表为将来的查看界面备数据；评估读不到头也登记为 0，别每次扫描都重试坏文件。
        try:
            vids2, _enc2 = scan_video_models(self.cfg)
        except Exception:
            vids2 = []
        paths_now = set(chat) | set(images) | set(vids2)
        ram = float(self.cfg.get("ram_gb", 0) or 0)
        try:
            _reports, new_seen = modelreq.evaluate_new(
                self.cfg, chat=chat, images=images, vids=vids2,
                vram_gb=vram, ram_gb=ram, seen=self.cfg.get("perf_reported"))
        except Exception:
            new_seen = dict(self.cfg.get("perf_reported") or {})
        for gone in [p for p in new_seen if p not in paths_now]:
            new_seen.pop(gone, None)
        if new_seen != (self.cfg.get("perf_reported") or {}):
            self.cfg["perf_reported"] = new_seen
            _dirty["v"] = True
        _flush()

    # ---- 模型管理（手动操作；自动线程与手动按钮共用 _scan_and_fill）----
    def _manual_scan(self, force=False):
        if force:
            if not messagebox.askyesno(
                    "全部重新计算",
                    "将清空所有模型的 层数/context/mmproj 自动记录并重新计算，\n"
                    "手动调整过的值也会被覆盖。确定继续？"):
                return
            for k in ("model_ngl", "model_ctx", "model_ctx_api", "model_mmproj",
                      "perf_reported"):
                self.cfg[k] = {}
            save_config(self.cfg)
        self._append("\n[模型管理] 开始%s…\n"
                     % ("全部重新计算模型参数" if force else "补全缺失项"), "meta")
        threading.Thread(target=self._scan_and_fill, args=(force,),
                         daemon=True).start()

    # ---- 手动定向模型（登记额外来源，不动文件；W 2026-10-05）----
    def _manual_point_model(self):
        """「手动定向模型」：选一个文件夹或一个模型文件加入来源。

        文件夹按"一个目录"并入扫描（里面能聊天的 .gguf 与生图 / 生视频权重一起收），
        单文件按它自己的类型收进对应清单。**只登记路径、不移动文件**（判据在
        `models.extra_sources`，扫描侧 `scan_models` / `video_scan_dirs` 会认）。
        """
        if self._any_busy():
            messagebox.showinfo("手动定向模型",
                                "当前有任务正在进行（生成 / 生图 / 生视频 / 服务操作），\n"
                                "请等任务结束、服务停止后再加入模型来源。")
            return
        host = self.root
        w = getattr(self, "_settings_win", None)
        try:
            if w is not None and w.winfo_exists():
                host = w
        except Exception:
            host = self.root
        d = tk.Toplevel(host)
        d.withdraw()                    # 先藏起来，摆正了再显示（否则左上角闪一下）
        d.title("手动定向模型")
        ttk.Label(d, text="加入一个模型来源（只登记路径，不移动文件）",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        ttk.Label(d, text="文件夹：把里面能聊天的 .gguf 与生图 / 生视频权重一起收进来；\n"
                          "单个文件：按它自己的类型收进对应清单。",
                  foreground="#808080", justify="left",
                  font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)

        def _pick_dir():
            p = filedialog.askdirectory(title="选择模型文件夹", parent=d)
            if p:
                d.destroy()
                self._register_extra_model(p)

        def _pick_file():
            p = filedialog.askopenfilename(
                title="选择模型文件", parent=d,
                filetypes=[("模型文件", "*.gguf *.safetensors *.sft *.ckpt *.pt *.bin"),
                           ("全部文件", "*.*")])
            if p:
                d.destroy()
                self._register_extra_model(p)

        bf = ttk.Frame(d)
        bf.pack(padx=14, pady=(10, 14), anchor="e")
        ttk.Button(bf, text="选择文件夹…", command=_pick_dir).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="选择模型文件…", command=_pick_file).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="取消", command=d.destroy).pack(side="left")
        widgets.center_on(d, host)
        d.deiconify()

    def _register_extra_model(self, path):
        """把选中的路径写进 `extra_models` 并立刻补一次参数（用户亲手加的动作不受冷却限制）。"""
        p = os.path.normpath(str(path or "").strip())
        if not p:
            return
        lst = list(self.cfg.get("extra_models") or [])
        if p in lst:
            messagebox.showinfo("手动定向模型", "这个来源已经在列表里了：\n%s" % p)
            return
        lst.append(p)
        self.cfg["extra_models"] = lst
        save_config(self.cfg)
        self._append("\n[模型管理] 已加入模型来源：%s\n" % p, "meta")
        messagebox.showinfo("手动定向模型",
                            "已加入：\n%s\n\n文件没有移动 —— 扫描与模型菜单会认它。" % p)
        threading.Thread(target=self._scan_and_fill, args=(False,), daemon=True).start()
        # 加了来源就可能凑齐了"第一个可用模型" ⇒ 立刻切过去（坑 150）
        try:
            self._maybe_adopt_first_model(force=True)
        except Exception:
            pass

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
        if self._any_busy():
            messagebox.showinfo("整理模型文件夹",
                                "当前有任务正在进行（生成 / 生图 / 生视频 / 服务操作），\n"
                                "请等任务结束、服务停止后再整理文件。")
            return
        moves, unpaired = plan_tidy(self.cfg)
        if not moves and not unpaired:
            messagebox.showinfo("整理模型文件夹",
                                "未发现需要整理的文件：\n"
                                "顶层没有散落的模型（或它们已在各自文件夹内）。")
            return
        dlg = tk.Toplevel(self.root)
        dlg.withdraw()          # 同上
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
        widgets.center_on(dlg, self.root)   # 摆到主窗口正中，别落在屏幕左上角

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
