# -*- coding: utf-8 -*-
"""llm_console.ui.settings — 界面 Mixin：设置窗口（v39：左侧可折叠导航 + 右侧滚动区块，
按页懒加载、同页区块一起建；字段说明走 widgets.HelpDot 悬停；底部「保存」统一跑当前页各区块的钩子）"""


import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, font as tkfont

from ..core import capability, cloudjobs, providers, sdprofile, secrets, textfile
from ..core.config import (CFG_VERSION, FLOAT_KEYS, INT_KEYS, STR_KEYS,
                           cloud_media_dir, gen_api_key, save_config)
from ..core.models import scan_models, scan_video_models
from ..core.params import ctx_for, current_ngl
from ..core.server import _query_serving_model, server_process_alive
from ..connection import cloud
from . import widgets

# ---------------------------------------------------------------------------
# 左栏导航（v39）：组 → 子组 → 叶子，叶子指向"哪个页面的哪个区块"。
#
# 为什么叶子比页面多：本地文本模型的「生成参数」和「服务参数」其实**在同一页里**
# （两者都是 llama-server 的事，分两页只是来回跳），但服务参数那一段很长，
# 左栏单列一项点过去就直接定位过去 —— 这也是给"以后配置项更多"留的位置。
# title / help 是给页面用的：title 是区块大标题，help 是标题旁 "?" 里的那段说明。
# ---------------------------------------------------------------------------
NAV_SPEC = [
    {"key": "g_local", "label": "本地模型", "children": [
        {"key": "g_ltext", "label": "文本模型", "children": [
            {"key": "gen", "label": "生成参数", "page": "local_text", "section": "gen",
             "title": "生成参数（保存即生效）",
             "help": "这一页改的是采样参数，下一次请求就生效，不用重启服务。\n"
                     "思考型模型的「思考过程」和「正式回答」共用 max_tokens 那一份额度，"
                     "思考写得长就会挤掉回答。"},
            {"key": "svc", "label": "服务参数", "page": "local_text", "section": "svc",
             "title": "服务参数（点「保存并重启服务」生效）",
             "help": "这些是 llama-server 的启动参数，改完必须重启服务才生效。\n"
                     "GPU 层数（ngl）与上下文（ctx）都是**按模型分别记忆**的：切换模型时"
                     "自动带上各自的值，这里改的是当前选中模型的那一份。"},
        ]},
        {"key": "g_lmedia", "label": "图像与视频模型", "children": [
            {"key": "img", "label": "生图（sd.cpp）", "page": "local_media", "section": "img",
             "title": "生图（sd.cpp）",
             "help": "本地生图走 sd.cpp 引擎：sd-cli 按需拉起、进程退出就释放显存，所以每次"
                     "生成都要重载一次权重。\n图生图要哪些配套件由模型族决定（Qwen-Image 要"
                     "视觉投影器 mmproj，FLUX 用 Kontext 变体），缺件在发送前就会点名说缺什么。"},
            {"key": "vid", "label": "生视频（sd.cpp）", "page": "local_media", "section": "vid",
             "title": "生视频（sd.cpp）",
             "help": "本地生视频与生图**共用同一个 sd-cli.exe**，只是多了视频参数。\n"
                     "一般要三件套权重：扩散主体、文本编码器、视频 VAE，**VAE 最容易漏下**，"
                     "缺任一项在发送前就会点名说缺什么，不会让你白排队。\n"
                     "出片耗时取决于显卡与档位，生成中可随时点「停止生成」。"},
        ]},
        {"key": "files", "label": "模型文件管理", "page": "files", "section": "files",
         "title": "模型文件管理",
         "help": "扫描模型目录、补全每个模型缺的层数 / 上下文 / 视觉投影器记录，"
                 "以及把散落的模型文件整理成「一个模型一个文件夹」。\n"
                 "整理是**先预览、后执行，只移动不删除**；判不出归属的文件保持原位。"},
    ]},
    {"key": "g_cloud", "label": "云端模型", "children": [
        {"key": "c_prov", "label": "服务商与密钥", "page": "cloud", "section": "prov",
         "title": "服务商、密钥与模型清单",
         "help": "内置服务商只内置名称与 base_url；添加服务商**不会**把它名下所有模型塞进"
                 "主页面菜单。流程是：填信息 → 填密钥 → 测试连接 → 通过后在独立的「选择模型」"
                 "窗口里勾选要用的模型。清单拉过一次就缓存，只有点「刷新清单」才重新请求。\n"
                 "密钥单独存 secrets.json，**不进备份、不进仓库**。"},
        {"key": "c_text", "label": "文本模型", "page": "cloud", "section": "ctext",
         "title": "云端文本模型",
         "help": "云端文本走通用 OpenAI 兼容协议。这里的设置只影响文本对话"
                 "（生图 / 生视频在下一区块）。\n"
                 "附件会留在对话历史里**每一轮都重发**，所以预算有上限，不是能塞多塞。"},
        {"key": "c_media", "label": "生图 / 生视频", "page": "cloud", "section": "cmedia",
         "title": "云端生图 / 生视频",
         "help": "云端生图与生视频走服务商的**原生接口**，不会启动本地 sd.cpp："
                 "生图是同步请求（可以挂参考图），生视频是异步任务"
                 "（提交 → 轮询 → 下载），首帧仍要用本地链路。\n"
                 "云端产物地址只活 24 小时，所以拿到就立刻下载到本地，不在云上留原图；"
                 "没来得及下载的会记进任务台账，重启后对话开头给「取回」按钮。"},
    ]},
    {"key": "api", "label": "API 连接", "page": "api", "section": "api",
     "title": "API 连接（供 agent 调用）",
     "help": "把「当前选中的本地模型」暴露成一个 OpenAI 兼容的本机端点，给 agent 或其他"
             "软件直接调用。\n这个代理**只转本地模型**：云端对话在应用内直连服务商，"
             "生图 / 生视频不经这里。"},
]


def _nav_leaves(items=None, out=None):
    """把导航树拍平成叶子列表（页面顺序、区块归属都由它推）。"""
    out = out if out is not None else []
    for it in (items if items is not None else NAV_SPEC):
        kids = it.get("children")
        if kids:
            _nav_leaves(kids, out)
        else:
            out.append(it)
    return out


def _open_outdir(path, what, setting=""):
    """打开一个输出目录：还没生成就先建，没配就点名该去哪儿配。

    原来是裸 `os.startfile(...)`：目录不存在时它抛 OSError，而 Tk 只把回调异常打到
    stderr（--windowed 的 exe 连 stderr 都没有）——用户点了按钮什么都没发生，
    看着像按钮坏了（与坑 88 同族的"根因在 stderr"症状）。
    另外 `sd_dir` 留空时 `os.path.join("", "output")` 会得到相对路径 "output"，
    那会在**当前工作目录**下建一个 output 并打开它，比报错更糟。
    """
    p = str(path or "").strip()
    if not p or not os.path.isabs(p):
        msg = "还没设置%s。" % what
        if setting:
            msg += "\n  请在 设置 → %s 里填上目录。" % setting
        messagebox.showwarning("输出目录", msg)
        return False
    try:
        os.makedirs(p, exist_ok=True)
        os.startfile(p)
        return True
    except Exception as e:
        messagebox.showwarning("输出目录", "打不开 %s：\n  %s" % (p, e))
        return False


NEW_PROVIDER_LABEL = "＋ 新建服务商…"      # 下拉里"还没建起来"那一项的标签


def provider_labels(cfg):
    """服务商下拉的显示项 → `[(标签, pid)]`。

    界面上只摆人看得懂的名字（中文优先，DeepSeek / Kimi / MiniMax 这类品牌名保留英文），
    **不再把 `deepseek`、`aliyun-token-plan` 这种代码 id 摊到屏幕上**——原来两处下拉
    用的是 id 与「id — 名称」，用户看到的是英文代号，认不出哪家是哪家。
    只有两个服务商重名时（自定义条目同名很常见）才在标签里补 id 区分，否则标签→pid
    的反查会有歧义。
    """
    rows = providers.list_providers(cfg, enabled_only=False)
    names = [str(p.get("name") or p["id"]) for p in rows]
    dup = {n for n in names if names.count(n) > 1}
    out = []
    for p, n in zip(rows, names):
        out.append(("%s（%s）" % (n, p["id"]) if n in dup else n, p["id"]))
    return out


def provider_label_for(cfg, pid, fallback=""):
    """某个服务商给用户看的名字（删除确认、状态回显这类单点场合用）。"""
    for label, one in provider_labels(cfg):
        if one == pid:
            return label
    return (providers.builtin(pid).get("name") or fallback or pid or "")


class SettingsMixin:
    """App 的设置窗口与 API 连接页职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    def _api_base_url(self):
        return "http://127.0.0.1:%s/v1" % self.cfg.get("proxy_port", 8081)

    def _api_model_name(self):
        """建议填写的模型名。

        性能关键：先做毫秒级的本地进程检查，进程存在才查 /v1/models（HTTP）；
        否则直接回退模型列表。此前无条件发 HTTP，服务停止时主线程
        要等 ~2 秒 TCP 超时（设置页打开卡顿的根因）。
        """
        if server_process_alive():
            m = _query_serving_model(self.cfg)
            if m:
                return m
        _d, chat, _img = scan_models(self.cfg)
        return os.path.basename(chat[0]) if chat else ""

    def _api_config_text(self):
        return ("服务类型: OpenAI 兼容 (Chat Completions)\n"
                "Base URL: %s\n"
                "API Key:  %s\n"
                "模型名:   %s\n"
                "—— 客户端填法 ——\n"
                "在 agent 应用中选择 OpenAI Compatible / 自定义 OpenAI，逐项粘贴以上四项；\n"
                "模型名支持模糊匹配（略写、带不带 .gguf 都可命中）。"
                % (self._api_base_url(), self.cfg.get("api_key", ""),
                   self._api_model_name() or "（暂无可用聊天模型）"))

    def _gen_api_key(self):
        k = gen_api_key()
        self.cfg["api_key"] = k
        save_config(self.cfg)
        messagebox.showinfo("API Key",
                            "已生成新的 API Key：\n\n%s\n\n"
                            "注意：Key 是服务启动参数，需「重启服务」后生效。" % k)
        return k

    def _restart_proxy(self):
        self.proxy.stop()
        if self.cfg.get("proxy_enabled", True):
            self.proxy.start()
        else:
            self._append("\n[API] 代理已停用（设置中可重新启用并重启代理）。\n", "meta")

    def open_settings(self):
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("设置")
        # 实测（DPI-aware，scaling≈2.0）滚动内容最宽一行需要 789px，左栏 232 + 边距
        # → 默认与最小宽都取 1080：横向不能滚，缩一点就是"右边那半截永远看不见"
        win.geometry("1080x740")
        win.minsize(1080, 600)
        win.transient(self.root)

        # ---- 固定外框 ----
        # 底部按钮条**先**按 side="bottom" 入列：pack 按入列顺序分配空间，先入列的保住自己，
        # 窗口缩小时被压的应该是右侧内容区，而不是「保存」（同主页输入区的教训，坑 56）。
        bar = ttk.Frame(win)
        bar.pack(side="bottom", fill="x", padx=12, pady=(4, 10))
        main = ttk.Frame(win)
        main.pack(side="top", fill="both", expand=True, padx=(10, 10), pady=(10, 0))

        v = {}
        save_hooks = {}            # 区块 frame → callable()→(ok, 说明)：底部「保存」要一起跑
        registry = {}              # (page, section) → 构建函数
        bodies = {}                # (page, section) → 区块内容 frame（已建则复用）
        heads = {}                 # (page, section) → 区块标题 frame（锚点）
        page_frames = {}           # page → 页面 frame
        state = {"page": None, "section": None}
        leaves = _nav_leaves()
        page_order, page_items = [], {}
        for it in leaves:
            if it["page"] not in page_order:
                page_order.append(it["page"])
                page_items[it["page"]] = []
            page_items[it["page"]].append(it)

        nav = widgets.SideNav(main, NAV_SPEC, width=232,
                              on_select=lambda it: _nav_select(it))
        sp = widgets.ScrollPage(main)

        def section(page_id, sec_id):
            """注册一个区块的构建函数（构建粒度是**页**，注册粒度是区块）。

            分成两段是为了：左栏每个叶子指向"哪页哪段"，点下去能滚到那一段的标题；
            但真正建内容时把**这一页的所有区块一起建**（见 _nav_select）——
            只建一段会得到半页空白 + 保存漏钩子。
            仍然按页懒建的理由是本机每个 ttk 控件创建+布局约 1.3ms，五页一次全建约 280ms 会卡。
            """
            def deco(build):
                registry[(page_id, sec_id)] = build
                return build
            return deco

        def _row(parent, rows, label, widget, desc, hint=""):
            """一行：标签 / 控件 / （短摘要 + "?" 悬停说明）。

            v39 起灰色长说明**不再内联**（它把窗口撑到 1020 宽、还把版面切成三段），
            改成 "?" 悬停；只有真正要"填之前就知道"的约束留在外面（hint，≤14 字）。
            """
            i = rows["i"]
            rows["i"] += 1
            bg = widgets.default_bg()
            ttk.Label(parent, text=label, width=14, anchor="w").grid(
                row=i, column=0, sticky="w", padx=(0, 8), pady=5)
            widget.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=5)
            cell = tk.Frame(parent, background=bg)
            cell.grid(row=i, column=2, sticky="w", pady=5)
            if hint:
                ttk.Label(cell, text=hint, foreground="#5a5a5a",
                          font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 4))
            widgets.HelpDot(cell, desc).pack(side="left")

        def row(parent, rows, label, widget, desc, hint=""):
            return _row(parent, rows, label, widget, desc, hint)

        def ent(parent, rows, key, label, desc, width=8, var=None, trace=None, hint=""):
            """一行"标签 + 输入框 + ? 提示"。

            key 非空时变量登记进 v（由 _apply_settings 统一写回 cfg）；
            传 var 则用外部变量（云端服务商那几项是结构化数据，自己管保存，
            不走 _apply_settings）。trace 用于即时联动（如回显请求地址）。
            """
            if var is None:
                var = v.setdefault(key, tk.StringVar(value=str(self.cfg.get(key, ""))))
            e = ttk.Entry(parent, textvariable=var, width=width)
            if trace is not None:
                var.trace_add("write", lambda *a: trace())
            _row(parent, rows, label, e, desc, hint)

        # ---- 区块 1：本地文本模型 / 生成参数 ----
        @section("local_text", "gen")
        def _t1(t1, r1):
            ent(t1, r1, "temperature", "temperature",
                "采样温度（0~2）：越高输出越发散有创意，越低越稳定保守；接近 0 时几乎固定。")
            ent(t1, r1, "top_p", "top_p",
                "核采样（0~1）：只在累计概率达到 p 的候选词里抽样。")
            ent(t1, r1, "top_k", "top_k",
                "每一步只在概率最高的 k 个词中选取，常用 40。")
            ent(t1, r1, "repeat_penalty", "repeat_penalty",
                "重复惩罚（通常 1.0~1.3）：大于 1 抑制复读式重复，1.0 表示关闭。")
            ent(t1, r1, "max_tokens", "max_tokens",
                "单次回复上限（token）。注意：思考过程 + 正式回答共享该额度，"
                "Qwen3 思考较长，建议 ≥4096；到上限会被截断并提示。",
                hint="思考与回答共用")
            ent(t1, r1, "seed", "seed",
                "随机种子：-1 表示随机；填固定数字可复现同一次输出。")

            i = r1["i"]
            r1["i"] += 1
            head = tk.Frame(t1, background=widgets.default_bg())
            head.grid(row=i, column=0, sticky="nw", padx=(0, 8), pady=5)
            ttk.Label(head, text="system_prompt", width=14, anchor="nw").pack(side="left")
            widgets.HelpDot(head, "系统提示词：给模型的人设与规则，自动放在每轮对话最前面。").pack(side="left", padx=(2, 0))
            # width 必须显式给：tk.Text 默认 80 字符，在高 DPI 下要 1080px，
            # 会把这一行顶出滚动可视区（横向不能滚，等于看不见）
            v["system_prompt"] = tk.Text(t1, height=3, width=44,
                                         font=("Microsoft YaHei UI", 9))
            v["system_prompt"].grid(row=i, column=1, columnspan=2, sticky="nsew", pady=5)
            v["system_prompt"].insert("1.0", str(self.cfg.get("system_prompt", "")))

            i = r1["i"]
            r1["i"] += 1
            v["show_reasoning"] = tk.BooleanVar(value=bool(self.cfg.get("show_reasoning", True)))
            ttk.Checkbutton(t1, text="在对话中显示模型的思考过程（reasoning，灰色斜体）",
                            variable=v["show_reasoning"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=8)

        # ---- 区块 2：本地文本模型 / 服务参数 ----
        @section("local_text", "svc")
        def _t2(t2, r2):

            # 本机属性：自动探测预填，存配置；GPU 层数计算直接使用这里的显存值
            ent(t2, r2, "gpu_name", "GPU 型号",
                "显卡型号（首次启动自动探测预填，可手动修改）。", width=32)
            ent(t2, r2, "vram_gb", "显存 (GB)",
                "显存容量：新模型 GPU 层数自动计算直接使用此值，不再临时询问系统；"
                "探测失败或多卡时可手动填写。", width=8)
            ent(t2, r2, "ram_gb", "内存 (GB)",
                "系统内存总量（首次启动自动探测预填，可修改；目前预留展示）。", width=8)

            ent(t2, r2, "model", "model",
                "当前模型 GGUF 完整路径（也可直接点主页模型名切换）。", width=30)
            ent(t2, r2, "models_dir", "models_dir",
                "模型文件夹：主页模型下拉列表扫描此目录下所有 .gguf 文件。", width=30)
            # ngl：显示/修改的是"当前模型"的值（按模型分别记忆）
            v["ngl"] = tk.StringVar(value=str(current_ngl(self.cfg)))
            e_ngl = ttk.Entry(t2, textvariable=v["ngl"], width=8)
            row(t2, r2, "n-gpu-layers", e_ngl,
                "放进显存的层数（当前模型）。新模型会按显存与模型大小自动计算并按模型分别记忆；"
                "此处修改仅对当前模型生效。0 = 全部放 CPU。")
            ent(t2, r2, "ctx", "context (-c)",
                "上下文长度（token）：容纳 系统提示 + 全部对话 + 工具定义 + 回答。"
                "默认 65536（agent 应用请求较长，建议 ≥32768）；模型原生支持更高可继续上调。"
                "注意 KV 成本：35B-A3B 约 2.5GB / 64K，而 27B（大 head_dim）约 16GB / 64K——"
                "此类模型请适当降低，否则占用大量内存/显存（启动时界面会显示 KV 预估）。",
                hint="按模型记忆")
            ent(t2, r2, "threads", "threads",
                "CPU 线程数，0 = 自动。一般留 0。")
            ent(t2, r2, "port", "port",
                "API 端口，默认 8080。")
            ent(t2, r2, "api_key", "api_key",
                "接口鉴权密钥；客户端调用需携带。留空则不鉴权。", width=16)
            v["reasoning_mode"] = tk.StringVar(value=str(self.cfg.get("reasoning_mode", "default")))
            cb = ttk.Combobox(t2, textvariable=v["reasoning_mode"],
                              values=["default", "off", "budget"], width=8, state="readonly")
            row(t2, r2, "reasoning", cb,
                "思考模式：default 跟随模型模板；off 关闭思考（更快、不吃 max_tokens 额度）；"
                "budget 限制思考 token 数。")
            ent(t2, r2, "reasoning_budget", "budget tokens",
                "思考预算：reasoning=budget 时，思考最多用多少 token。")
            ent(t2, r2, "extra_args", "extra_args",
                "附加命令行参数（高级）：空格分隔，原样追加给 llama-server。", width=24)
            ent(t2, r2, "exe", "server 路径",
                "llama-server.exe 完整路径。", width=30)

        # ---- 区块 3：本地图像与视频 / 生图 ----
        @section("local_media", "img")
        def _t3(t3, r3):
            ent(t3, r3, "sd_dir", "引擎目录",
                "sd.cpp 引擎所在目录（内含 sd-cli.exe / sd-server.exe）。", width=30)
            ent(t3, r3, "image_model_dir", "生图模型文件夹",
                "扩散模型与它的配套件（VAE、文本编码器、视觉投影器 mmproj）都放这里；"
                "留空 = 模型目录下的「生图」子文件夹。", width=30)
            ent(t3, r3, "img_model_file", "默认生图模型",
                "默认选中的扩散模型文件名（.gguf / .safetensors / .ckpt 都行）。", width=30)
            # 模型族：识别结果只决定"拼哪些参数、要哪些配套件"，参数值仍来自下面这些设置
            _fam_opts = sdprofile.family_choices("image")
            _code2label = {c: t for c, t in _fam_opts}
            v["img_family"] = tk.StringVar(
                value=_code2label.get(str(self.cfg.get("img_family", "") or "").strip(),
                                      _fam_opts[0][1]))
            fam_cb = ttk.Combobox(t3, textvariable=v["img_family"], state="readonly",
                                  width=24, values=[t for _c, t in _fam_opts])
            img_note = tk.StringVar(value="")
            row(t3, r3, "模型族", fam_cb,
                "程序会按权重文件里的张量名与文件名自动认这一族需要哪些配套件、该传什么参数。"
                "认错了（比如社区改过名）就在这里手动指定；选「通用」= 只把扫到的文件喂给引擎，"
                "不附加任何家族专属参数。", hint="一般用自动")
            ttk.Label(t3, textvariable=img_note, foreground="#5a6a7a",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r3["i"], column=1, columnspan=2, sticky="w", pady=(0, 4))
            r3["i"] += 1

            def refresh_img_note(*_a):
                """回显识别结果。内容长度固定 —— 状态类 Label 拼长文案会引发整页重排（坑 92）。"""
                code = {t: c for c, t in sdprofile.family_choices("image")}.get(
                    v["img_family"].get(), sdprofile.AUTO)
                img_dir = str(self.cfg.get("image_model_dir", "") or "")
                path = os.path.join(img_dir, str(self.cfg.get("img_model_file", "") or ""))
                fid, basis, _ = sdprofile.detect_file(path, kind="image", forced=code)
                tail = {"measure": "张量名实测过", "user": "你手动指定",
                        "hint": "按名字猜的", "none": "认不出，走通用"}.get(
                    basis, "")
                img_note.set("识别为：%s（%s）" % (sdprofile.label_of(fid), tail or "自动"))

            v["img_family"].trace_add("write", refresh_img_note)
            ent(t3, r3, "img_vae_file", "VAE 文件",
                "留空 = 在本族要求的目录里自动找（按文件名含 vae / ae）。放了多个家族"
                "的权重又挑错时，在这里指名。", width=30, hint="留空=自动")
            ent(t3, r3, "img_llm_file", "LLM 编码器",
                "LLM 文本编码器的 .gguf（Qwen-Image、FLUX.2 这类要用）。CLIP 系的模型不用填。",
                width=30, hint="留空=自动")
            ent(t3, r3, "img_clip_l_file", "CLIP-L",
                "clip_l.safetensors 之类（Flux / SD3 必需）。", width=30, hint="留空=自动")
            ent(t3, r3, "img_clip_g_file", "CLIP-G",
                "clip_g.safetensors 之类（SDXL / Flux 用）。", width=30, hint="留空=自动")
            ent(t3, r3, "img_t5_file", "T5-XXL",
                "t5xxl_fp16.safetensors 之类（Flux / SD3 必需）。", width=30, hint="留空=自动")
            ent(t3, r3, "img_steps", "默认步数",
                "默认采样步数（4~50）：Qwen-Image 在本机 8 步 ~1m20s、20 步 ~2m50s；"
                "少=快，多=细节更多。别的模型族看各家文档。",
                hint="8 步最快")
            ent(t3, r3, "img_size", "默认分辨率",
                "宽x高，如 1024x1024。分辨率越高越慢。会自动补到本族要求的倍数"
                "（SD 系 8 的倍数、Flux/SD3/Wan 16 的倍数），不合适的尺寸会被抬上去。",
                width=12, hint="宽x高")
            ent(t3, r3, "img_cfg", "默认 CFG",
                "提示词服从度。Qwen-Image 官方推荐 2.5；Flux dev/schnell 常给 1.0，"
                "SDXL/SD1.5 常给 6~8。切族时记得改这一档。")
            ent(t3, r3, "img_negative", "负向提示词",
                "留空 = 不传给引擎（Qwen-Image 本来就不带这一项）。SD/SDXL/Wan 这类"
                "支持负向提示词的模型可以自己填。", width=30, hint="留空=不传")
            ent(t3, r3, "img_seed", "默认种子",
                "-1 随机；固定数字可复现同一次输出。")
            ent(t3, r3, "img_backend", "组件后端",
                "sd-cli --backend。留空 = 用该族默认（LLM 系走 te=cpu,diffusion=cuda0,vae=cuda0，"
                "CLIP 系走 clip=cpu,…）。8GB 显存装不下时可以试 diffusion=cpu 或 vae=cpu。",
                width=30, hint="留空=默认")
            ent(t3, r3, "img_params_backend", "权重后端",
                "sd-cli --params-backend。留空 = 引擎 auto-fit 自己安排；显存吃紧可填 "
                "diffusion=disk 从内存/磁盘流式取权重。", width=30, hint="留空=自动")
            ent(t3, r3, "img_extra_args", "附加参数",
                "原样拼到命令行末尾，是「识别没覆盖到」的人工出口。例如 "
                "--scheduler karras --prediction eps 或 --taesd <路径> 做快速预览。",
                width=30, hint="可留空")

            ttk.Button(t3, text="打开输出文件夹",
                       command=lambda: _open_outdir(
                           os.path.join(str(self.cfg.get("sd_dir", "") or ""), "output"),
                           "生图输出目录", "生图（sd.cpp） → 引擎目录")).grid(
                row=r3["i"], column=1, sticky="w", pady=5)
            r3["i"] += 1
            refresh_img_note()

        # ---- 区块 4：本地图像与视频 / 生视频 ----
        @section("local_media", "vid")
        def _t3b(t3b, r3b):
            ent(t3b, r3b, "video_model_dir", "视频模型文件夹",
                "视频组件存放目录：扩散主体 + 文本编码器（LLM 或 T5-XXL）+ 视频 VAE，"
                "MiniMax-H3 与 Wan 都是这套摆法。"
                "该目录不存在时会自动改扫 models_dir 顶层与各子目录，所以文件散放在模型库里也能识别。",
                width=30)
            ent(t3b, r3b, "vid_model_file", "扩散主体文件名",
                "留空 = 用扫描到的第一个视频扩散 GGUF。", width=30)
            ent(t3b, r3b, "vid_llm_file", "文本编码器文件名",
                "留空 = 自动取与扩散主体配套的编码器（按文件名匹配，通常名字里带 vl / llm）。", width=30)
            ent(t3b, r3b, "vid_vae_file", "视频 VAE 文件名",
                "留空 = 在主体所在目录里按文件名含 vae 自动找（不含 audio 的那个）。", width=30)
            _vf_opts = sdprofile.family_choices("video")
            _vf2code = {t: c for c, t in _vf_opts}
            v["vid_family"] = tk.StringVar(
                value=_vf2code.get(str(self.cfg.get("vid_family", "") or "").strip(),
                                   _vf_opts[0][1]))
            vfile_cb = ttk.Combobox(t3b, textvariable=v["vid_family"], state="readonly",
                                    width=24, values=[t for _c, t in _vf_opts])
            vid_note = tk.StringVar(value="")
            row(t3b, r3b, "模型族", vfile_cb,
                "同一套 sd-cli 可以跑多个视频家族（MiniMax-H3 / Wan 2.1-2.2 / LTX / "
                "HunyuanVideo）。认族决定「要哪些配套件、要不要负向提示词、尺寸对齐到几」。"
                "认错了就在这里手动指定；选通用则只把找到的文件交给引擎。", hint="一般用自动")
            ttk.Label(t3b, textvariable=vid_note, foreground="#5a6a7a",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r3b["i"], column=1, columnspan=2, sticky="w", pady=(0, 4))
            r3b["i"] += 1

            def refresh_vid_note(*_a):
                """回显识别结果：一行、长度固定（长文案塞进 Label 会引发整页重排，坑 92）。"""
                code = {t: c for c, t in sdprofile.family_choices("video")}.get(
                    v["vid_family"].get(), sdprofile.AUTO)
                vdir = str(self.cfg.get("video_model_dir", "") or "")
                name = str(self.cfg.get("vid_model_file", "") or "")
                path = os.path.join(vdir, name) if name else ""
                if not (path and os.path.isfile(path)):
                    try:
                        vids, _e = scan_video_models(self.cfg)
                        path = vids[0] if vids else ""
                    except Exception:
                        path = ""
                fid, basis, _ = sdprofile.detect_file(path, kind="video", forced=code)
                tail = {"measure": "张量名实测过", "user": "你手动指定",
                        "hint": "按名字猜的", "none": "认不出，走通用"}.get(
                    basis, "")
                vid_note.set("识别为：%s（%s）" % (sdprofile.label_of(fid), tail or "自动"))

            v["vid_family"].trace_add("write", refresh_vid_note)
            ent(t3b, r3b, "vid_t5_file", "T5-XXL 文件名",
                "Wan / LTX / HunyuanVideo 的文本编码器（--t5xxl）。MiniMax-H3 不用填这一项。",
                width=30, hint="留空=自动")
            ent(t3b, r3b, "vid_tokenizer_file", "tokenizer 文件",
                "部分家族要 tokenizer.json（引擎的 --tokenizer）。留空 = 不传。",
                width=30, hint="多数不用填")
            ent(t3b, r3b, "vid_high_noise_file", "高噪段模型",
                "Wan2.2 的 MoE 版是**两个**扩散文件（高噪段 + 低噪段），这里填高噪段那个"
                "（--high-noise-diffusion-model）。5B 版与单文件模型留空即可。",
                width=30, hint="MoE 才要")
            ent(t3b, r3b, "vid_audio_vae_file", "音频 VAE",
                "想要有声视频才需要（本机没下过这个文件，缺了只出无声视频）。"
                "留空 = 按文件名含 audio + vae 自动找。", width=30, hint="留空=自动")
            ent(t3b, r3b, "vid_size", "分辨率",
                "宽x高，如 512x512。视频分辨率对显存和耗时都很敏感，先小后大。", width=12)
            ent(t3b, r3b, "vid_frames", "帧数",
                "视频长度 = 帧数 ÷ 帧率。**不需要自己凑 4n+1**：引擎会自行对齐到合法帧数"
                "（实测填 17 会提示 align video frames from 17 to 22），估算时长按对齐后的算。",
                hint="引擎自动对齐")
            ent(t3b, r3b, "vid_fps", "帧率",
                "每秒帧数。MiniMax-H3 的参考视频按 24fps 组织。")
            ent(t3b, r3b, "vid_steps", "采样步数",
                "步数直接决定耗时；链路先通再逐步加大。")
            ent(t3b, r3b, "vid_cfg", "CFG",
                "提示词服从度。MiniMax-H3 实测 5.0 可用；Wan 的文档区间是 3~6。"
                "大于 1 时引擎会去编码负向提示词，H3 那一族下面那栏就不能留空。")
            ent(t3b, r3b, "vid_neg_prompt", "负向提示词",
                "MiniMax-H3 在 CFG>1 时**必须能编码出负向提示词**，留空会报 "
                "failed to encode negative video prompt 并退出码 1 —— 这一族留空时代码会用"
                "内置兜底值。Wan / LTX 不要求，留空就不传给引擎。",
                width=30, hint="H3 不能留空")
            ent(t3b, r3b, "vid_format", "输出容器",
                "webm / avi / webp（sd-cli 单文件视频输出只支持这三种）。", width=10)
            ent(t3b, r3b, "vid_seed", "种子", "-1 随机。")
            ent(t3b, r3b, "vid_backend", "组件后端",
                "sd-cli --backend：各组件跑在哪。默认把文本编码器放 CPU、扩散与 VAE 放显卡，"
                "与生图一致。", width=30)
            ent(t3b, r3b, "vid_params_backend", "权重后端",
                "sd-cli --params-backend：权重放哪。显存吃紧时可填 diffusion=disk 让引擎从内存/磁盘流式取权重。",
                width=30)
            ent(t3b, r3b, "vid_extra_args", "附加参数",
                "原样拼进命令行。默认开了 --vae-tiling --temporal-tiling 分块解码来压显存。",
                width=30)

            fr_v = ttk.Frame(t3b)
            ttk.Button(fr_v, text="打开视频输出文件夹",
                       command=lambda: _open_outdir(
                           os.path.join(str(self.cfg.get("sd_dir", "") or ""), "video"),
                           "生视频输出目录", "生视频（sd.cpp） → 引擎目录")).pack(side="left")
            row(t3b, r3b, "输出目录", fr_v,
                "生成结果写在 sd.cpp\\video\\vid_时间戳.webm；引擎每次按需拉起，进程退出即释放显存。")
            refresh_vid_note()

        # ---- 区块 5：API 连接（供 agent 调用） ----
        @section("api", "api")
        def _t4(t4, r4):

            self.api_hint_var = tk.StringVar(value="")
            api_key_var = tk.StringVar(value=str(self.cfg.get("api_key", "")))
            api_model_var = tk.StringVar(value=self._api_model_name() or "（暂无）")

            running = self.proxy.running()
            ttk.Label(t4, text=("代理运行中 · 端口 %s" % self.cfg.get("proxy_port", 8081))
                      if running else "代理未运行（可在此页启用并重启代理）",
                      foreground="#1a7f37" if running else "#999999",
                      font=("Microsoft YaHei UI", 10, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4["i"] = 1

            fr = ttk.Frame(t4)
            ttk.Button(fr, text="启动 / 重启服务（agent 场景）", width=22,
                       command=self.on_start_restart_agent).pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="停止服务", width=10,
                       command=self.stop_server_async).pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="重启代理", width=10,
                       command=self._restart_proxy).pack(side="left")
            row(t4, r4, "服务控制", fr,
                "与主页面是同一个服务，但以 agent 场景的 context 启动（见下）；"
                "agent 请求的模型/context 与当前不符时会自动停止并重启服务。")

            self.agent_ctx_var = tk.StringVar(
                value=str(ctx_for(self.cfg, agent=True)))
            row(t4, r4, "agent context",
                ttk.Entry(t4, textvariable=self.agent_ctx_var, width=10),
                "本模型在 agent 场景（API 连接页/代理自动拉起）启动时使用的 context；"
                "保存后对下次启动生效（默认：35B=131072，27B=32768）。")
            v["agent_ctx"] = self.agent_ctx_var

            fr = ttk.Frame(t4)
            v_bu = tk.StringVar(value=self._api_base_url())
            ttk.Entry(fr, textvariable=v_bu, width=25, state="readonly").pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(v_bu.get(), "Base URL")).pack(side="left")
            row(t4, r4, "Base URL", fr, "agent 应用填写此地址（本代理）；不要填 8080（那是后端服务）。")

            fr = ttk.Frame(t4)
            ttk.Entry(fr, textvariable=api_key_var, width=20, state="readonly").pack(
                side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(api_key_var.get(), "API Key")).pack(
                side="left", padx=(0, 6))
            ttk.Button(fr, text="重新生成", width=10,
                       command=lambda: (api_key_var.set(self._gen_api_key()),
                                        self.api_hint_var.set(
                                            "已生成新 Key，重启服务后生效"))).pack(side="left")
            row(t4, r4, "API Key", fr, "鉴权密钥（随机生成）；修改后需重启服务生效。")

            fr = ttk.Frame(t4)
            ttk.Entry(fr, textvariable=api_model_var, width=25, state="readonly").pack(
                side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(api_model_var.get(), "模型名")).pack(side="left")
            row(t4, r4, "模型名", fr, "建议填写值（实时取服务加载的模型）；支持模糊匹配，略写也能命中。")

            fr = ttk.Frame(t4)
            ttk.Button(fr, text="复制完整配置（含填法说明）", width=24,
                       command=lambda: self._copy_text(self._api_config_text(), "完整配置")).pack(side="left")
            row(t4, r4, "一键复制", fr, "粘贴到任意 agent 应用的自定义模型配置即可接入。")

            ent(t4, r4, "proxy_port", "代理端口",
                "agent 接入端口（默认 8081）；改动后点「重启代理」生效，"
                "并且要同步改 agent 里填的端口——8080 是后端服务端口，不是给 agent 的。",
                width=8, hint="重启生效")
            v_px = tk.BooleanVar(value=bool(self.cfg.get("proxy_enabled", True)))
            cb_px = ttk.Checkbutton(t4, text="启用 API 代理（随程序启动）", variable=v_px)
            row(t4, r4, "启用代理", cb_px, "关闭后 agent 无法接入；改动后点「重启代理」生效。")
            v["proxy_enabled"] = v_px

            ttk.Label(t4, textvariable=self.api_hint_var, foreground="#1a7f37",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=1, columnspan=2, sticky="w", pady=4)
            r4["i"] += 1
            ttk.Label(t4, text=("提示：agent 应用的请求通常包含系统提示与工具定义，上下文较长——"
                                "当前 context=%s，建议 ≥ 16384（在 设置 → 服务参数 调整后重启服务）。"
                                % self.cfg.get("ctx")),
                      foreground="#b58900", wraplength=740, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=0, columnspan=3, sticky="w", pady=(8, 2))
            r4["i"] += 1
            ttk.Label(t4, text=("这个代理只转发**本地文本模型**：生图 / 生视频不经这里调用"
                                "（云端那两条在应用内直连服务商原生接口，"
                                "本地那两条走 sd-cli；agent 用不到图像接口）。"),
                      foreground="#c01c28", wraplength=740, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=0, columnspan=3, sticky="w", pady=(8, 2))
            r4["i"] += 1
            ttk.Label(t4, text="远程连接（预留）：未来可经隧道（frp / Cloudflare Tunnel）+ 强鉴权 + IP 白名单"
                               "对外提供极小规模服务；本页结构已按可扩展方式组织。",
                      foreground="#999999", wraplength=740, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4["i"], column=0, columnspan=3, sticky="w", pady=(2, 6))

            # 底部「保存」在本页时的额外动作：端口/启停真的变了就把代理重启一次，
            # 否则"保存了却没生效"和"保存了却关窗"一样让人以为配好了
            init_proxy = (int(self.cfg.get("proxy_port", 8081) or 0),
                          bool(self.cfg.get("proxy_enabled", True)))

            def _api_hook():
                try:
                    new = (int(str(v["proxy_port"].get()).strip()), bool(v_px.get()))
                except Exception:
                    return False, "代理端口要是数字，改好再保存。"
                if new != init_proxy:
                    self._restart_proxy()
                    self.api_hint_var.set("代理已按新设置重启（端口 %s）。" % new[0])
                return True, ""

            save_hooks[t4] = _api_hook

        # ---- 区块 6：云端模型 / 服务商与密钥 ----
        @section("cloud", "prov")
        def _t4b(t4b, r4b):
            """云端服务商区块。页面开头那段说明在 NAV_SPEC 的 help 里（标题旁的 "?"）。

            约定（v33）：内置服务商只内置"名称 + base_url"；添加服务商**不会**自动把
            它名下所有模型塞进主页面菜单。流程是 填信息 → 填密钥 → 测试连接 →
            通过后弹出独立的「模型选择」界面，由用户勾选要用的模型；
            拉到的清单缓存在 provider.model_kinds 里，只有点「刷新模型」才重新请求。
            """

            st = {"pid": "", "builtin": False, "models": [], "kinds": {}, "cands": []}
            vars_ = {}
            for k in ("name", "base_url", "timeout", "ctx", "media_base_url"):
                vars_[k] = tk.StringVar()
            # 「原生接口协议」下拉里摆的是中文标签，存进配置的是代码（auto/none/aliyun/…）：
            # 域名认不出来的自建代理只能靠这一项手动指过去
            _ma_opts = [("auto", "自动判断（按域名识别）"),
                        ("none", "不接（这个服务商只用文本）")] + [
                (c, providers.MEDIA_LABEL[c]) for c in providers.MEDIA_APIS
                if c not in ("auto", "none")]
            _ma_text = {t: c for c, t in _ma_opts}
            _ma_code = {c: t for c, t in _ma_opts}
            vars_["media_api"] = tk.StringVar(value=_ma_code["auto"])
            vars_["enabled"] = tk.BooleanVar(value=True)
            url_lbl = tk.StringVar(value="")
            media_lbl = tk.StringVar(value="")
            jobs_lbl = tk.StringVar(value="")
            key_lbl = tk.StringVar(value="")
            cat_lbl = tk.StringVar(value="")
            msg_lbl = tk.StringVar(value="")
            menu_lbl = tk.StringVar(value="")

            def provider_snapshot():
                """把页面上正在编辑的内容拼成 provider 结构（可能还没保存到配置里）。"""
                p = providers.get_provider(self.cfg, st["pid"]) if st["pid"] else None
                base = dict(p) if p else {}
                base.update({"id": st["pid"], "name": vars_["name"].get() or
                             (providers.builtin(st["pid"]).get("name") or st["pid"]),
                             "base_url": vars_["base_url"].get(),
                             "models": list(st["models"]),
                             "model_kinds": dict(st["kinds"]),
                             "enabled": vars_["enabled"].get(),
                             "timeout": vars_["timeout"].get(),
                             "ctx": vars_["ctx"].get(),
                             "media_base_url": vars_["media_base_url"].get(),
                             "media_api": _ma_text.get(vars_["media_api"].get(), "auto")})
                return providers.normalize_provider(base)

            def refresh_url(*_a):
                if st["builtin"] and not providers.builtin_base_editable(st["pid"]):
                    url_lbl.set("内置请求地址：%s（不可改）"
                                % providers.chat_completions_url(
                                    providers.builtin(st["pid"])))
                else:
                    url_lbl.set("实际请求地址：%s"
                                % providers.chat_completions_url(
                                    {"base_url": vars_["base_url"].get()}))

            def refresh_key(*_a):
                m = secrets.mask(secrets.get_api_key(st["pid"])) if st["pid"] else ""
                key_lbl.set("该服务商已存密钥：%s" % m if m else "该服务商还没有密钥")

            def refresh_media(*_a):
                """回显"这个服务商能不能走云端生图/生视频"，省得用户猜。

                原生接口地址是**从 base_url 推**的（同族域名只差 /compatible-mode 这一段），
                推不出来时在这里说清楚，而不是等用户发一条消息换来一个看不懂的 404。
                """
                p = provider_snapshot()
                api, root = providers.media_api(p), providers.media_api_root(p)
                if str(p.get("media_api") or "auto") == "none":
                    media_lbl.set("云端生图/生视频：按这里的设置这条不接（这个服务商只用文本对话）。")
                elif not api:
                    media_lbl.set("云端生图/生视频：认不出这个域名的原生接口。目前接了 %s；"
                                  "自建网关/代理可以在下面手动选协议，或填「原生接口地址」。"
                                  % "、".join(providers.MEDIA_LABEL[a]
                                             for a in providers.MEDIA_APIS
                                             if a not in ("auto", "none")))
                elif not root:
                    media_lbl.set("云端生图/生视频：原生接口地址推不出来，请在下面填一项。")
                else:
                    media_lbl.set("云端生图/生视频：走 %s，根地址 %s"
                                  % (providers.MEDIA_LABEL.get(api, api), root))

            def refresh_jobs(*_a):
                """任务台账一行的现状：产物只活 24 小时，积压要看得见。"""
                try:
                    pend = cloudjobs.unfinished()
                except Exception as e:
                    jobs_lbl.set("云端任务台账读不了：%s" % e)
                    return
                if not pend:
                    jobs_lbl.set("云端任务台账：没有等着取回的任务。")
                    return
                jobs_lbl.set("云端任务台账：%d 个结果还没落地 ｜ %s"
                             % (len(pend),
                                "；".join(cloudjobs.describe(j) for j in pend[:3])))

            def refresh_menu_list(_e=None):
                lb.delete(0, "end")
                # 显示缩写（与主页面菜单同一个规则），配置里存的仍是原名
                lab = providers.short_labels(st["models"])
                for m in st["models"]:
                    lb.insert("end", "%s · %s" % (providers.KIND_LABEL[
                        providers.model_kind_of({"model_kinds": st["kinds"]}, m)],
                        lab.get(m, m)))
                menu_lbl.set("加入主页面的模型：%d 个（模型菜单里就出现这些）"
                             % len(st["models"]))
                cat_lbl.set("该服务商已知模型：%d 个（勾选界面里可选，缓存着不重复请求）"
                            % len(providers.catalog(
                                {"models": st["models"],
                                 "model_kinds": st["kinds"]})))

            def load(pid):
                st["pid"] = pid
                st["builtin"] = providers.is_builtin(pid)
                p = providers.get_provider(self.cfg, pid) if pid else None
                st["models"] = list((p or {}).get("models") or [])
                st["kinds"] = dict((p or {}).get("model_kinds") or {})
                vars_["name"].set((p or {}).get("name", ""))
                vars_["base_url"].set((p or {}).get("base_url", ""))
                vars_["timeout"].set(str((p or {}).get("timeout", 600)))
                vars_["ctx"].set(str((p or {}).get("ctx", 0) or 0))
                vars_["media_base_url"].set((p or {}).get("media_base_url", "") or "")
                _ma = str((p or {}).get("media_api") or "auto")
                vars_["media_api"].set(_ma_code.get(_ma, _ma_code["auto"]))
                vars_["enabled"].set(bool((p or {}).get("enabled", True)) if p else True)
                editable = providers.builtin_base_editable(pid)
                # 内置服务商：名称一律不开放；地址只在"按量百炼"这类**因账号而异**的条目上
                # 开放（grid_remove 记住原位，切回别的服务商时原样还回来）
                if st["builtin"]:
                    built_note.configure(
                        text=("名称：内置。这一条的 base_url 预置的是公共域名；如果你的密钥属于"
                              "某个业务空间，把下面 base_url 换成形如 "
                              "https://你的WorkspaceId.cn-beijing.maas.aliyuncs.com/"
                              "compatible-mode/v1 的专属域名（key 与域名不可混用）。")
                        if editable else
                        "名称与 base_url：内置（只需填密钥、选模型）。")
                    built_note.grid()
                    fields.grid_remove()
                    if editable:
                        fields2.grid()
                    else:
                        fields2.grid_remove()
                else:
                    built_note.grid_remove()
                    fields.grid()
                    fields2.grid()
                refresh_url()
                refresh_media()
                refresh_key()
                refresh_menu_list()
                msg_lbl.set("")

            def commit(silent=False):
                """把页面当前内容写进配置；返回 (ok, 说明)。底部「保存」也走这里。"""
                if not st["pid"]:
                    name = vars_["name"].get().strip()
                    burl = vars_["base_url"].get().strip()
                    if not name and not burl and not st["models"]:
                        return True, ""      # 这页压根没填，没什么要保存的，别拦着关窗
                    nid = (name or "cloud").replace(" ", "-")
                    base_id, n = nid, 2
                    while providers.get_provider(self.cfg, base_id):
                        base_id = "%s%d" % (nid, n)
                        n += 1
                    ok, err = providers.add_provider(self.cfg, {
                        "id": base_id, "name": name, "base_url": burl,
                        "models": list(st["models"]), "model_kinds": dict(st["kinds"]),
                        "enabled": vars_["enabled"].get(),
                        "timeout": vars_["timeout"].get(),
                        "ctx": vars_["ctx"].get(),
                        "media_base_url": vars_["media_base_url"].get(),
                        "media_api": _ma_text.get(vars_["media_api"].get(), "auto")})
                    if not ok:
                        msg_lbl.set("保存失败：%s" % err)
                        return False, err      # 半填的新服务商：留着窗口让用户补
                    st["pid"] = base_id
                    save_config(self.cfg)
                    refresh_combo(keep=base_id)
                    if not silent:
                        msg_lbl.set("已新建服务商「%s」。" % (name or base_id))
                    self._update_model_label()
                    return True, ""
                data = {"name": vars_["name"].get(), "base_url": vars_["base_url"].get(),
                        "models": list(st["models"]), "model_kinds": dict(st["kinds"]),
                        "enabled": vars_["enabled"].get(),
                        "timeout": vars_["timeout"].get(),
                        "ctx": vars_["ctx"].get(),
                        "media_base_url": vars_["media_base_url"].get(),
                        "media_api": _ma_text.get(vars_["media_api"].get(), "auto")}
                if not providers.update_provider(self.cfg, st["pid"], data):
                    msg_lbl.set("保存失败：provider「%s」不存在。" % st["pid"])
                    return False, "provider 不存在，可能刚被删除。"
                save_config(self.cfg)
                if not silent:
                    msg_lbl.set("已保存「%s」（主页面模型 %d 个）。"
                                % (st["pid"], len(st["models"])))
                self._update_model_label()
                return True, ""

            def ensure_saved():
                """密钥/模型选择/测试都要先有个能用的 provider id。"""
                ok, msg = commit(silent=True)
                if not ok:
                    messagebox.showwarning("先保存", msg)
                return ok

            def do_delete():
                pid = st["pid"]
                if not pid:
                    msg_lbl.set("没有选中要删除的服务商。")
                    return
                tip = ("「%s」是内置服务商，删掉后下次启动会按内置定义重新出现。"
                       % provider_label_for(self.cfg, pid)) if providers.is_builtin(pid) \
                    else "删除「%s」？" % provider_label_for(self.cfg, pid, vars_["name"].get())
                if not messagebox.askyesno("删除服务商",
                                           tip + "\n\n模型清单会一起删除，已存密钥也会被清掉。"):
                    return
                providers.remove_provider(self.cfg, pid)
                secrets.set_api_key(pid, "")
                if self.cfg.get("model_provider") == pid:
                    # 正在用的云端模型被删了：回到本地模型，避免发送时才发现 provider 不存在
                    self.cfg["model_provider"] = providers.LOCAL
                    _d, chat, _i = scan_models(self.cfg)
                    if chat:
                        self.cfg["model"] = chat[0]
                    self._update_model_label()
                    self._render_status(self._server_alive_flag, self._server_ready_flag)
                save_config(self.cfg)
                refresh_combo()
                msg_lbl.set("已删除。")

            # ---------------- 密钥 ----------------
            def do_key_dialog():
                d = tk.Toplevel(win)
                d.title("API Key")
                d.transient(win)
                pname = (providers.builtin(st["pid"]).get("name")
                         or vars_["name"].get() or st["pid"] or "新的服务商")
                ttk.Label(d, text="给「%s」填写 API Key" % pname,
                          font=("Microsoft YaHei UI", 10, "bold")).pack(
                    anchor="w", padx=14, pady=(12, 4))
                ttk.Label(d, text="密钥只写进 secrets.json（不进备份）；界面与配置文件里都只显示掩码。",
                          foreground="#808080", wraplength=420, justify="left",
                          font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
                e = ttk.Entry(d, width=40, show="●")
                e.pack(padx=14, pady=10)
                tip = ttk.Label(d, textvariable=key_lbl, foreground="#808080",
                                font=("Microsoft YaHei UI", 9))
                tip.pack(anchor="w", padx=14)
                bf = ttk.Frame(d)
                bf.pack(padx=14, pady=(6, 14), anchor="e")

                def save():
                    k = e.get().strip()
                    if not k:
                        e.delete(0, "end")
                        d.destroy()
                        return
                    if not st["pid"]:
                        # 还没建起来的服务商：先按页面上填的名称/地址把它建起来，
                        # 密钥要有 id 才能落进 secrets.json
                        ok, err = commit(silent=True)
                        if not ok:
                            messagebox.showwarning("API Key", err or "先填名称与 base_url。")
                            return
                    secrets.set_api_key(st["pid"], k)
                    refresh_key()
                    msg_lbl.set("已保存「%s」的密钥。" % st["pid"])
                    d.destroy()

                def clear():
                    if st["pid"] and messagebox.askyesno("API Key", "清除「%s」的已存密钥？"
                                                         % st["pid"]):
                        secrets.set_api_key(st["pid"], "")
                        refresh_key()
                    d.destroy()

                ttk.Button(bf, text="清除", width=8, command=clear).pack(side="left", padx=4)
                ttk.Button(bf, text="取消", width=8, command=d.destroy).pack(side="left", padx=4)
                ttk.Button(bf, text="保存密钥", width=10, command=save).pack(side="left")
                e.focus_set()

            def do_local_api():
                """本页的密钥是给云端用的；本机 API 地址在「API 连接」区块配。"""
                nav.select("api")

            # ---------------- 模型选择界面 ----------------
            def open_picker(fetch=None, explain=""):
                """独立的模型勾选界面：勾中的才进主页面菜单；可手填模型名并"试一试"。"""
                if not st["pid"]:
                    messagebox.showwarning("选择模型", "先保存这个服务商，再选模型。")
                    return
                pid = st["pid"]
                known = providers.catalog({"models": st["models"],
                                           "model_kinds": st["kinds"]})
                if fetch is None:
                    # W 定的规矩：拉过就缓存，除非用户点「刷新清单」，否则不重复请求
                    fetch = not known
                d = tk.Toplevel(win)
                d.title("选择模型 · %s"
                        % (providers.get_provider(self.cfg, pid) or {}).get("name", pid))
                d.geometry("560x520+140+140")
                d.minsize(500, 380)
                d.transient(win)
                HDR = ("Microsoft YaHei UI", 9, "bold")
                rows = {}                       # 模型名 → [勾选 Var, 能力 Var, 图片输入 Var]

                def declare_now(m, label):
                    """改「图片输入」下拉即写声明并落盘。

                    以前只有点「确定」才生效：从「取消」或右上角 X 出去时，刚标好的「支持」
                    被静默丢掉，回到主页面照样被问"能不能看图"（W 报的 bug）。
                    能力声明与"勾哪些进菜单"不是同一件事，不该共用同一次提交。
                    """
                    capability.set_choice(self.cfg,
                                          capability.LABEL_CHOICE.get(label, capability.AUTO),
                                          providers.make_cloud_id(pid, m))
                    save_config(self.cfg)
                    self._update_model_label()
                # 顶部只放说明文字，按钮一律挪到底部：窄窗口里左右对撞会互相盖住
                head = ttk.Frame(d)
                head.pack(side="top", fill="x", padx=12, pady=(10, 2))
                hint_lbl = ttk.Label(head, text="勾中并点「确定」才进主页面菜单；「图片输入」一改即生效。"
                                     "手填名字后按回车＝直接加入；「试一试」只对对话模型有意义",
                                     foreground="#808080", wraplength=520, justify="left",
                                     font=("Microsoft YaHei UI", 9))
                hint_lbl.pack(side="left")
                status = tk.StringVar(value=explain or
                                      ("表里是已知的 %d 个模型；点「刷新清单」可向接口重新索取。"
                                       "模型多时按名字前缀收成折叠组，点组名展开。"
                                       % len(known)))
                st_lbl = ttk.Label(d, textvariable=status, foreground="#808080",
                                   wraplength=600, justify="left",
                                   font=("Microsoft YaHei UI", 9))
                st_lbl.pack(side="top", anchor="w", padx=12, pady=(0, 2))

                wrap = ttk.Frame(d)
                wrap.pack(side="top", fill="both", expand=True, padx=12)
                cv = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
                sb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview)
                cv.configure(yscrollcommand=sb.set)
                sb.pack(side="right", fill="y")
                cv.pack(side="left", fill="both", expand=True)
                table = ttk.Frame(cv)
                inner = cv.create_window((0, 0), window=table, anchor="nw")
                # 滚轮（W 报的"选择模型页面好像不能用鼠标滚动"）：绑在这个窗口的顶层 +
                # 指针在列表区内才响应，所以不会把滚轮从表头/状态行或别的窗口抢走
                widgets.attach_wheel(cv)

                # 列宽先量后用：拍脑袋的数字若比控件真实宽度小，grid 会把列撑开，
                # 表头就和下面每行的下拉错位（这正是"比例失衡"的观感来源）
                _p_kind = ttk.Combobox(table, state="readonly", width=9, values=[
                    providers.KIND_LABEL[k] for k in providers.KIND_ORDER])
                _p_img = ttk.Combobox(table, state="readonly", width=9, values=[
                    capability.CHOICE_LABEL[c] for c in capability.CHOICES])
                _p_chk = ttk.Checkbutton(table, text="")
                table.update_idletasks()
                COL_W = max(_p_kind.winfo_reqwidth(), _p_img.winfo_reqwidth())
                CK_PAD = _p_chk.winfo_reqwidth()      # 勾选方框 + 内边距
                for _p in (_p_kind, _p_img, _p_chk):
                    _p.destroy()
                GAP = 6

                # ---- 表头钉在滚动区外面：行滚多少，标题都留在原位 ----
                # 标题不用 grid，照着第一行三个控件的真实 x 摆（place）：
                # 网格那点内部边距猜不准，量出来的位置才不会让表头和列错开
                hdr = ttk.Frame(d, height=20)
                hdr.pack(side="top", fill="x", padx=12, before=wrap)
                hdr_lbl = {txt: ttk.Label(hdr, text=txt, font=HDR)
                           for txt in ("模型", "图片输入", "模型类型")}
                for txt in hdr_lbl:
                    hdr_lbl[txt].place(x=0, y=1)
                # 表头框高度按真实字号给：写死的数字在高 DPI 下会把标题切一半
                hdr.configure(height=max(l.winfo_reqheight() for l in hdr_lbl.values()) + 2)
                ttk.Separator(d, orient="horizontal").pack(
                    side="top", fill="x", padx=12, pady=(2, 3), before=wrap)

                table.columnconfigure(1, minsize=COL_W)
                table.columnconfigure(2, minsize=COL_W)
                table.columnconfigure(3, weight=1)   # 富余宽度交给这列空白，两列下拉贴着名字
                cells = {}                      # 模型名 → (勾选框, 图片输入下拉, 类型下拉)
                sizes = {"col0": 300, "need0": 200}   # 模型列：当前宽 / 名字真正需要的宽
                _fnt = tkfont.Font(family="Microsoft YaHei UI", size=9)
                labels = {}        # 模型名 → 缩写显示名（每次并进来新模型都整体重算，见 add_models）
                spec = []          # 显示顺序：[("hdr", 组键) | ("row", 模型名), ...]
                folds = {}         # 组键 → {"models": [...], "open": bool, "hdr": Label}

                _short_cache = {}        # (名字, 列宽) → 截断后的显示名；随窗口销毁

                def shorten(name, px):
                    """按列宽截断显示名；rows 的键仍是完整模型名，写配置不会错。

                    ttk.Checkbutton 在这个 Tk 版本上没有 anchor/justify/wraplength，
                    只能从"显示"这一侧解决：量着列宽裁。

                    两处提速（原来是逐字符往回退，一个 50 字的名字要问字体 50 次；
                    而 relabel() 每次拖动/加行都对全表重跑一遍）：
                      · 二分找截断点：log2(50)≈6 次字体度量，不是 50 次；
                      · 按 (名字, 列宽) 缓存：列宽没变就一次都不问字体。
                    缓存只活在这个窗口里、键含列宽，所以不存在"改了列宽还用旧值"。
                    """
                    px = max(px, 24)
                    key = (name, px)
                    hit = _short_cache.get(key)
                    if hit is not None:
                        return hit
                    if _fnt.measure(name) <= px:
                        _short_cache[key] = name
                        return name
                    lo, hi = 0, len(name)      # 最大的 k 使 name[:k]+"…" 放得下；0=一个都放不下
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if _fnt.measure(name[:mid] + "…") <= px:
                            lo = mid
                        else:
                            hi = mid - 1
                    out = (name[:lo] if lo else name[:1]) + "…"
                    _short_cache[key] = out
                    return out

                def label_px():
                    return sizes["col0"] - CK_PAD - 4

                def relabel():
                    """勾选框上的文字 = 缩写名，再按列宽做像素级截断（两道都过一遍）。"""
                    for m, (c, _i, _k) in cells.items():
                        c.configure(text=shorten(labels.get(m, m), label_px()))

                def align_hdr():
                    base = hdr.winfo_rootx()
                    if base <= 0 or not cells:
                        return
                    # 优先拿"真的显示出来的第一行"量；整页都收起时才临时挂回一行量一次
                    # （表头必须跟列对齐，而列位置只有格子里有真实控件时才量得出来）
                    cb = ic = kc = None
                    for ent in spec:
                        if ent[0] != "row":
                            continue
                        c = cells.get(ent[1])
                        if c and c[0].winfo_ismapped():
                            cb, ic, kc = c
                            break
                    was = cb is not None
                    if not was:
                        cb, ic, kc = next(iter(cells.values()))
                        for w in (cb, ic, kc):
                            w.grid()
                        table.update_idletasks()
                    for lbl, w in ((hdr_lbl["模型"], cb), (hdr_lbl["图片输入"], ic),
                                   (hdr_lbl["模型类型"], kc)):
                        lbl.place(x=max(w.winfo_rootx() - base, 0), y=1)
                    if not was:
                        for w in (cb, ic, kc):
                            w.grid_remove()

                def on_resize(_e=None):
                    """按画布实际宽度定列宽：模型列"够用就好"，两个下拉列定宽。

                    以前只设 inner 宽度，长模型名会把右侧两列挤出容器（W 报的"溢出"）；
                    模型列拉满整行又会让名字和下拉之间空一大截，所以按最长的名字收口，
                    多出来的宽度交给第 4 列空白。

                    宽度早退：<Configure> 在拖动窗口时每秒触发几十次，而**纵向**拖动
                    根本不改变宽度 —— 那时候 itemconfigure/columnconfigure（会让整表
                    480 个控件重排）+ relabel（全表字体度量）全是白做的，实测拖动
                    135ms/帧（≈7fps）。所以三个输入都没变就直接返回。
                    need0 必须进指纹：add_models 收到更长的名字后会直接调本函数，
                    那时宽度没变但列宽该放宽。
                    """
                    avail = max(cv.winfo_width() - 4, 300)
                    inner_w = max(320, d.winfo_width() - 30)
                    sig = (avail, inner_w, sizes["need0"])
                    if sizes.get("sig") == sig:
                        return
                    sizes["sig"] = sig
                    sizes["col0"] = max(min(sizes["need0"], avail - 2 * (COL_W + GAP)), 110)
                    cv.itemconfigure(inner, width=avail)
                    table.columnconfigure(0, minsize=sizes["col0"])
                    st_lbl.configure(wraplength=inner_w)
                    hint_lbl.configure(wraplength=inner_w)
                    relabel()
                    cv.update_idletasks()
                    align_hdr()
                    bb = cv.bbox("all")
                    if bb:
                        cv.configure(scrollregion=bb)
                cv.bind("<Configure>", on_resize)

                def _mk_vars(m, kind, checked):
                    """建这一行的三个变量（只建一次；重排/折叠都不碰它们，状态自然留住）。"""
                    kv = tk.BooleanVar(value=bool(checked))
                    cv_kind = tk.StringVar(value=providers.KIND_LABEL[kind])
                    # 图片输入三态：自动判断 / 支持 / 不支持。云端 /models 不带能力字段，
                    # 只能由用户声明或实测一次；本地投影器判据可靠，默认停在"自动判断"。
                    try:
                        declared = capability.get_choice(
                            self.cfg, providers.make_cloud_id(pid, m))
                    except Exception:
                        declared = capability.AUTO
                    cv_img = tk.StringVar(
                        value=capability.CHOICE_LABEL.get(declared,
                                                          capability.CHOICE_LABEL[capability.AUTO]))
                    # 一改即落盘：不依赖用户点「确定」（见 declare_now 的说明）
                    cv_img.trace_add("write", lambda *a: declare_now(m, cv_img.get()))
                    rows[m] = [kv, cv_kind, cv_img]

                def _mk_widgets(m):
                    kv, cv_kind, cv_img = rows[m]
                    cb = ttk.Checkbutton(table, text=shorten(labels.get(m, m), label_px()),
                                         variable=kv)
                    ic = ttk.Combobox(table, textvariable=cv_img, state="readonly", width=9,
                                      values=[capability.CHOICE_LABEL[c] for c in
                                              capability.CHOICES])
                    kc = ttk.Combobox(table, textvariable=cv_kind, state="readonly",
                                      width=9,
                                      values=[providers.KIND_LABEL[k] for k in
                                              providers.KIND_ORDER])
                    cells[m] = (cb, ic, kc)

                def layout_spec():
                    """决定显示顺序：模型多的服务商按名字前缀收组。组头落在"组里第一个成员
                    原来所在的那一行"，成员紧跟组头之后 —— 不这样排就看不出组是从哪冒出来的。
                    **成员必须也进 spec**：regrid 是照 spec 逐行排格子的，只写组头的话展开时
                    没有控件被 grid 回来（第一版就漏在这儿）。

                    凑不成组的零散模型不再一条条平铺在顶上，而是统一收进末尾的「其他」组
                    （与主页面菜单同一套口径，W 提的）。收起 ≠ 藏起来：行照常建、勾选照常算，
                    `ok_apply` 收的是 `rows` 里的变量，跟折不折没关系。
                    """
                    names = list(rows)
                    many = len(names) > providers.FOLD_AT
                    groups, flat = providers.fold_groups(
                        names, at=1 if many else providers.FOLD_AT)
                    if many and flat:
                        groups = groups + [{"key": providers.REST_KEY, "models": list(flat)}]
                        flat = []
                    owner, members_of = {}, {}
                    for g in groups:
                        members_of[g["key"]] = list(g["models"])
                        for m in g["models"]:
                            owner[m] = g["key"]
                    out, emitted = [], set()
                    for m in names:
                        k = owner.get(m)
                        if k is None:
                            out.append(("row", m, [m]))       # 没建组的（含模型本来就少的）平铺
                            continue
                        if k in emitted:
                            continue                          # 成员已在组头后面排过
                        emitted.add(k)
                        out.append(("hdr", k, members_of[k]))
                        out += [("row", mm, [mm]) for mm in members_of[k]]
                    return out

                def owner_map():
                    out = {}
                    for ent in spec:
                        if ent[0] == "hdr":
                            for m in ent[2]:
                                out[m] = ent[1]
                    return out

                def toggle(key):
                    f = folds.get(key)
                    if f is None:
                        return
                    f["open"] = not f["open"]
                    f["hdr"].configure(text=group_text(key, f["models"], f["open"]))
                    regrid()

                def group_text(key, models, open_):
                    """组名带上"里面有没有我当前勾中的"：收起时也能看见这组有几个要用的。"""
                    n_on = sum(1 for m in models if rows[m][0].get())
                    return "%s %s · %d 个%s" % ("▾" if open_ else "▸", key, len(models),
                                            ("（已勾 %d）" % n_on) if n_on else "")

                def sync_folds():
                    for ent in spec:
                        if ent[0] != "hdr":
                            continue
                        key, models = ent[1], ent[2]
                        f = folds.get(key)
                        if f is None:
                            lbl = tk.Label(table, text="", cursor="hand2",
                                           font=("Microsoft YaHei UI", 9, "bold"),
                                           background=widgets.default_bg(), anchor="w")
                            # 「其他」默认**展开**：它里面本来就是要露面的零散模型，
                            # 收起来等于在这份"选哪些进主页面"的清单里把它们藏了。
                            # （主页面菜单那边是 tk.Menu 的 cascade，天生收起，不冲突）
                            f = folds[key] = {"open": key == providers.REST_KEY,
                                              "hdr": lbl, "models": []}
                            lbl.bind("<Button-1>", lambda e, k=key: toggle(k))
                        f["models"] = list(models)
                        f["hdr"].configure(text=group_text(key, models, f["open"]))

                def regrid():
                    """按 spec + 折叠状态重排 grid 行号：**只挪格子不重建控件**。
                    成员一律先 grid 到它该在的行、再按折叠状态 `grid_remove()` ——
                    这样收起的行**也记得自己的格子配置**：`grid()` 不带参数恢复原位
                    （表头对齐要临时挂回一行来量坐标），配置没被赋过的话会全部落到
                    row0/col0，量回来的就是第一列的 x，表头跟着整排错位。"""
                    own = owner_map()
                    i = 0
                    for ent in spec:
                        if ent[0] == "hdr":
                            folds[ent[1]]["hdr"].grid(row=i, column=0, columnspan=4,
                                                      sticky="w", pady=(7, 1))
                            i += 1
                            continue
                        cb, ic, kc = cells[ent[1]]
                        cb.grid(row=i, column=0, sticky="w", pady=1)
                        ic.grid(row=i, column=1, sticky="w", padx=(GAP, 0), pady=1)
                        kc.grid(row=i, column=2, sticky="w", padx=(GAP, 0), pady=1)
                        if own.get(ent[1]) and not folds[own[ent[1]]]["open"]:
                            for w in (cb, ic, kc):
                                w.grid_remove()     # 行号照样占着：展开时原位回来
                        i += 1
                    table.update_idletasks()
                    bb = cv.bbox("all")
                    if bb:
                        cv.configure(scrollregion=bb)
                    align_hdr()
                    return i

                def add_models(items):
                    """items = [(模型名, 能力, 是否勾中)]。新增 + 重算缩写 + 重排整表。

                    缩写只在**可能撞名**时才整批重算：`short_labels` 最多跑 4 轮、
                    每轮对全表做一次正则压缩，加一个模型就把 119 个名字重压一遍是白付的。
                    判据是 short_labels 自己的规则——"撞车才逐级放宽"：新名字的最简形式
                    既不撞已有标签、彼此也不撞时，放宽档数不需要变，其余行的标签必然原样。
                    一旦撞了就整批重算（保持"批内不重名"这条不变量，见坑 84）。
                    """
                    fresh = [m for m, _k, _c in items if m not in rows]
                    for m, k, c in items:
                        if m not in rows:
                            _mk_vars(m, k, c)
                    if not labels:
                        labels.update(providers.short_labels(list(rows)))   # 首次建表
                    else:
                        cand = providers.short_labels(fresh) if fresh else {}
                        used = set(labels.values())
                        if len(set(cand.values())) != len(cand) or any(v in used for v in cand.values()):
                            labels.clear()
                            labels.update(providers.short_labels(list(rows)))
                        else:
                            labels.update(cand)
                    need = max([CK_PAD + _fnt.measure(labels[m]) + 10 for m in rows],
                               default=200)
                    grew = need > sizes["need0"]
                    sizes["need0"] = max(sizes["need0"], need)
                    for m in fresh:
                        _mk_widgets(m)
                    spec[:] = layout_spec()
                    sync_folds()
                    regrid()
                    if grew:
                        on_resize()       # 列宽到顶时 shorten 接手按像素截断

                add_models([(m, providers.model_kind_of(
                    {"model_kinds": st["kinds"]}, m), m in st["models"]) for m in known])
                on_resize()

                def pull(force=False):
                    """向接口拉 /models；拉到的补进列表（勾选状态保留）。"""
                    if not force and not fetch:
                        return
                    status.set("正在向接口取模型清单…")

                    snap = provider_snapshot()          # Tk 变量只能在主线程读
                    key = secrets.get_api_key(pid)

                    def work():
                        okk, ids, text = cloud.list_models(snap, api_key=key)
                        self._ui_q.put(lambda: got(okk, ids, text))
                    threading.Thread(target=work, daemon=True).start()

                def got(okk, ids, text):
                    if not d.winfo_exists():
                        return
                    if not okk:
                        status.set("没能取到清单：%s\n也可以在下面填模型名，用「试一试并加入」验证。"
                                   % text)
                        return
                    added = 0
                    new = []
                    for m in ids:
                        if m not in rows:
                            new.append((m, providers.guess_kind(m), m in st["models"]))
                            added += 1
                    if new:
                        add_models(new)     # 一次并进来：整批重算缩写 + 重排一次
                    # 顶部说明已经讲过"勾中并点确定"，这里只报接口给了多少，不重复
                    status.set("接口给了 %d 个模型（新增 %d 个）。" % (len(ids), added))

                def try_add():
                    """"这名字服务端认不认"只有**对话模型**能零成本问出来。

                    生图 / 生视频的模型名走各家原生接口：拿 `chat/completions` 去试必然
                    `Model not exist`（服务端不认它是对话模型），而真正的媒体端点一试就是
                    真金白银（MiniMax 出图按张计费）。所以认出来不是文本模型时**不发那次
                    注定没意义的请求**，直接按名字收进表并说清为什么验不了。
                    """
                    m = e_new.get().strip()
                    if not m:
                        status.set("先输入模型名。")
                        return
                    if providers.guess_kind(m) != "text":
                        manual_add("这个名字看着像媒体模型，对话接口验不了它"
                                   "（原生媒体接口一试就计费）")
                        return
                    status.set("正在用「%s」试一次最小请求（服务端会点名说这个名字认不认）…" % m)

                    snap = provider_snapshot()          # 同上：主线程取值，子线程只用数据
                    key = secrets.get_api_key(pid)

                    def work():
                        okk, text = cloud.try_model(snap, api_key=key, model=m)
                        self._ui_q.put(lambda: tried(okk, m, text))
                    threading.Thread(target=work, daemon=True).start()

                def tried(okk, m, text):
                    if not d.winfo_exists():
                        return
                    if okk:
                        add_models([(m, providers.guess_kind(m), True)])
                        status.set("✅ %s：%s（已勾上）" % (m, text))
                    else:
                        status.set("❌ %s：%s" % (m, text))

                def manual_add(why=""):
                    """生图 / 生视频的模型**不在** /models 清单里，也不能拿文本请求去试跑
                    （服务端会因"这个模型不能聊天"拒掉）。v40 接了 MiniMax/智谱/华为的
                    原生媒体接口后这条路必须开：按名字直接收进表，能力按名字给默认值，
                    用户在那一行右侧的下拉里改。`why` 是"为什么没走验证"的说明。
                    """
                    m = e_new.get().strip()
                    if not m:
                        status.set("先输入模型名。")
                        return False
                    if m in rows:
                        status.set("表里已经有「%s」了。" % m)
                        return False
                    k = providers.guess_kind(m)
                    add_models([(m, k, True)])
                    extra = ""
                    if k != "text":
                        p = providers.get_provider(self.cfg, pid) or {}
                        if not providers.supports_media(p, k):
                            extra = "；注意：这一家还没接%s的原生接口，发送前会被拦下" \
                                % providers.KIND_LABEL[k]
                    status.set("%s已按名字加入「%s」（能力：%s；觉得不对在那一行右边改）%s。"
                               % ((why + "，") if why else "", m,
                                  providers.KIND_LABEL[k], extra))
                    e_new.delete(0, "end")
                    return True

                # 底部三件套：手填行（含刷新/验证按钮）+ 确定取消，都排在表格之前分配空间，
                # 窗口再矮也是压表格，不会把按钮挤掉
                botf = ttk.Frame(d)
                botf.pack(side="bottom", fill="x", padx=12, pady=(4, 2), before=wrap)
                ttk.Label(botf, text="模型名", foreground="#808080",
                          font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 4))
                e_new = ttk.Entry(botf, width=16)
                e_new.pack(side="left", fill="x", expand=True)
                # 回车 = 直接加入。以前这个框一个绑定都没有：敲完名字按回车什么也不会发生，
                # 看起来就是"加不进去"（W 实测报的那条）。
                e_new.bind("<Return>", lambda _e: manual_add())
                e_new.bind("<KP_Enter>", lambda _e: manual_add())
                ttk.Button(botf, text="试一试并加入", width=12,
                           command=try_add).pack(side="left", padx=(6, 0))
                ttk.Button(botf, text="直接加入", width=10,
                           command=lambda: manual_add()).pack(side="left", padx=(6, 0))
                # 最下沿：左边两个动作按钮，右边确定/取消，一行装得下 470px 的最小宽度
                bf2 = ttk.Frame(d)
                bf2.pack(side="bottom", fill="x", padx=12, pady=(2, 12), before=botf)
                ttk.Button(bf2, text="刷新清单", width=9,
                           command=lambda: pull(True)).pack(side="left")
                ttk.Button(bf2, text="验证图片输入", width=12,
                           command=lambda: do_verify()).pack(side="left", padx=(6, 0))
                ttk.Button(bf2, text="取消", width=8, command=d.destroy).pack(side="right")
                ttk.Button(bf2, text="确定", width=10,
                           command=lambda: ok_apply(d)).pack(side="right", padx=6)

                def ok_apply(dlg):
                    picked, kinds = [], {}
                    for m, (kv, kvar, _ivar) in rows.items():
                        k = [x for x in providers.KIND_ORDER
                             if providers.KIND_LABEL[x] == kvar.get()][0]
                        kinds[m] = k
                        if kv.get():
                            picked.append(m)
                    apply_declarations()
                    st["models"] = picked
                    st["kinds"].update(kinds)
                    commit(silent=True)      # save_config 顺带把 model_image_input 落盘
                    refresh_menu_list()
                    # 别把勾中的模型名全拼进这个 wraplength=680 的 Label：119 个原始名
                    # 是 3440 字，实测把 Label 撑到 1948px 高、逼整个滚动区重排，
                    # 点「确定」后的整窗重绘要 739ms（只报数量+头两个 = 18ms）。
                    # 完整清单本来就在上面那个 Listbox 里，一条都没少。
                    # 缩写直接复用 add_models 已经算好的 labels，不再重算一遍。
                    if not picked:
                        msg_lbl.set("已写入主页面模型：（没勾任何模型）")
                    else:
                        msg_lbl.set("已写入主页面模型：%d 个（%s%s）"
                                    % (len(picked),
                                       "、".join(labels.get(m, m) for m in picked[:2]),
                                       " …" if len(picked) > 2 else ""))
                    dlg.destroy()

                def apply_declarations():
                    """把每行的「图片输入」三态写进配置；选"自动判断"= 删记录、回到判据。"""
                    for m, (_kv, _kvar, ivar) in rows.items():
                        choice = capability.LABEL_CHOICE.get(ivar.get(), capability.AUTO)
                        capability.set_choice(self.cfg, choice,
                                              providers.make_cloud_id(pid, m))

                def do_verify():
                    """实测：发一张 1x1 图问服务端收不收。比按名字猜可靠，但要花一次最小请求。"""
                    targets = [m for m, (kv, _k, _i) in rows.items() if kv.get()][:5]
                    if not targets:
                        status.set("先勾几个模型再验证（一次最多测 5 个）。")
                        return
                    key = secrets.get_api_key(pid)
                    snap = provider_snapshot()
                    status.set("正在向服务端确认 %d 个模型的图片输入能力（每个发一次最小请求）…"
                               % len(targets))

                    def work():
                        out = []
                        for m in targets:
                            try:
                                out.append((m,) + cloud.probe_image_input(
                                    self.cfg, provider=snap, api_key=key, model=m))
                            except Exception as e:
                                out.append((m, capability.UNKNOWN, "探测出错：%s" % e))
                        self._ui_q.put(lambda: verified(out))
                    threading.Thread(target=work, daemon=True).start()

                def verified(results):
                    if not d.winfo_exists():
                        return
                    lines = []
                    for m, v, why in results:
                        if v in (capability.YES, capability.NO):
                            lab = capability.CHOICE_LABEL[v]
                            rows[m][2].set(lab)
                            lines.append("%s → %s" % (m, lab))
                        else:
                            lines.append("%s → 未确认（%s）" % (m, why))
                    status.set("验证结果：" + "；".join(lines)
                               + "。改完点「确定」才写入。")

                if fetch:
                    pull()

            def do_pick_models():
                open_picker()          # 打开就用缓存；要重新请求请点「刷新清单」

            def do_remove_selected():
                sel = lb.curselection()
                if not sel:
                    msg_lbl.set("先在列表里选中要移出的模型。")
                    return
                m = lb.get(sel[0]).split(" · ", 1)[1]
                st["models"] = [x for x in st["models"] if x != m]
                commit(silent=True)
                refresh_menu_list()
                msg_lbl.set("已从主页面菜单移出「%s」（它仍留在已知模型里，可随时再勾）。" % m)

            # ---------------- 连通性 ----------------
            def do_test():
                if not ensure_saved():
                    return
                key = secrets.get_api_key(st["pid"])
                if not key:
                    msg_lbl.set("先按「填该服务商 API Key」存好密钥，再测连接。")
                    return
                msg_lbl.set("测试中…（用文本模型发一次 max_tokens=1 的最小请求）")
                t4b.update_idletasks()

                p = providers.get_provider(self.cfg, st["pid"]) or provider_snapshot()

                def work():
                    ok, text = cloud.probe({**p, "models": st["models"],
                                            "model_kinds": st["kinds"]}, api_key=key)
                    def after():
                        if not win.winfo_exists():
                            return          # 设置窗口已关：别弹孤立的对话框
                        msg_lbl.set(("✅ " if ok else "❌ ") + text)
                        if ok and messagebox.askyesno(
                                "连接可用", "连通正常。\n\n现在选择要加入主页面的模型吗？"
                                "（没勾的不会出现在模型菜单里）"):
                            open_picker(explain="连接测试通过。下面用缓存的清单；要重新向接口取，点「刷新清单」。")
                        elif not ok:
                            messagebox.showwarning("连不上", text)
                    self._ui_q.put(after)
                threading.Thread(target=work, daemon=True).start()

            def refresh_combo(keep=None):
                """下拉只列显示名，pid 留在背后用（把代码 id 摆到界面上，用户认不出哪家）。"""
                pairs = provider_labels(self.cfg)
                combo["values"] = [lb for lb, _p in pairs] + [NEW_PROVIDER_LABEL]
                by_pid = {p: lb for lb, p in pairs}
                pid = keep if keep in by_pid else (pairs[0][1] if pairs else "")
                combo.set(by_pid.get(pid, NEW_PROVIDER_LABEL))
                load(pid)

            def on_pick(_e=None):
                sel = combo.get()
                pid = {lb: p for lb, p in provider_labels(self.cfg)}.get(sel, "")
                load(pid)          # 选中"＋ 新建服务商…"时 pid 为空 → 空白新条目

            bar = ttk.Frame(t4b)
            bar.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4b["i"] += 1
            ttk.Label(bar, text="服务商：").pack(side="left")
            combo = ttk.Combobox(bar, width=24, state="readonly")
            combo.pack(side="left", padx=(0, 6))
            combo.bind("<<ComboboxSelected>>", on_pick)
            ttk.Button(bar, text="删除", width=6, command=do_delete).pack(side="left", padx=3)
            ttk.Button(bar, text="测试连接", width=10, command=do_test).pack(side="left", padx=3)

            kr = ttk.Frame(t4b)
            kr.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 8))
            r4b["i"] += 1
            ttk.Button(kr, text="填该服务商 API Key", width=20,
                       command=do_key_dialog).pack(side="left")
            ttk.Button(kr, text="配置本机 API 地址", width=18,
                       command=do_local_api).pack(side="left", padx=6)
            # 密钥状态单独一行：它跟着按钮排在同一行时，掩码文本会把这行撑得比
            # 可视区宽（横向不可滚 = 后面的内容看不见）
            ttk.Label(t4b, textvariable=key_lbl, foreground="#808080", wraplength=560,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            built_note = ttk.Label(t4b, text="名称与 base_url：内置（只需填密钥、选模型）。",
                                   foreground="#808080", wraplength=640, justify="left",
                                   font=("Microsoft YaHei UI", 9))
            built_note.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            fields = ttk.Frame(t4b)      # 名称：只有自定义服务商可填
            fields.grid(row=r4b["i"], column=0, columnspan=3, sticky="w")
            sub = {"i": 0}
            ent(fields, sub, None, "名称", "服务商显示名（菜单、下拉、提示里都用它，写中文就行）；"
                                        "新建时它还兼作这条记录的索引名。",
                width=30, var=vars_["name"])
            r4b["i"] += 1

            # 地址类控件单独一个框：内置服务商里"按量百炼"的工作空间域名**因账号而异**，
            # 必须让用户自己填，所以不能跟着"内置一律不可改"一起收起
            fields2 = ttk.Frame(t4b)
            fields2.grid(row=r4b["i"], column=0, columnspan=3, sticky="w")
            sub2 = {"i": 0}
            ent(fields2, sub2, None, "base_url",
                "OpenAI 兼容根地址，带不带 /v1 都行（会自动补）。",
                width=36, var=vars_["base_url"], trace=lambda *a: (refresh_url(),
                                                                   refresh_media()))
            ent(fields2, sub2, None, "原生接口地址",
                "云端生图/生视频用的地址，留空 = 从上面那段自动推（把 /compatible-mode/v1 "
                "换成 /api/v1）。只有跨方案时才需要填，key 与域名不可混用。",
                width=36, var=vars_["media_base_url"], trace=refresh_media)
            # width=18：最长的候选串是「不接（这个服务商只用文本）」这类 12 个中日韩字，
            # 18 个"平均字宽"够摆下（24 会把这一行撑到 775px，比视口 768 还宽 → 整行被压扁）
            _ma_cb = ttk.Combobox(fields2, textvariable=vars_["media_api"], state="readonly",
                                  width=18, values=[t for _c, t in _ma_opts])
            _ma_cb.bind("<<ComboboxSelected>>", lambda *a: refresh_media())
            _row(fields2, sub2, "原生接口协议", _ma_cb,
                 "生图与生视频走哪套原生协议。按域名自动认（阿里云系 / MiniMax / 智谱 / 华为系）；"
                 "自建网关、代理或专有域名认不出来时在这里手动指一次。"
                 "选「不接」= 这个服务商只用文本对话，生图生视频会在发送前拦下。",
                 hint="自动/手动")
            r4b["i"] += 1

            ttk.Label(t4b, textvariable=media_lbl, foreground="#808080", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(2, 6))
            r4b["i"] += 1

            mf = ttk.Frame(t4b)
            mf.grid(row=r4b["i"], column=1, columnspan=2, sticky="w", pady=(2, 4))
            r4b["i"] += 1
            lb = tk.Listbox(mf, height=5, width=34, exportselection=False,
                            font=("Microsoft YaHei UI", 9))
            lb.pack(side="left")
            btns = ttk.Frame(mf)
            btns.pack(side="left", padx=(6, 0), fill="y")
            ttk.Button(btns, text="选择模型…", width=12,
                       command=do_pick_models).pack(anchor="w", pady=1)
            ttk.Button(btns, text="移出选中项", width=12,
                       command=do_remove_selected).pack(anchor="w", pady=1)
            ttk.Button(btns, text="刷新清单", width=12,
                       command=lambda: (commit(silent=True),
                                        open_picker(fetch=True))).pack(anchor="w", pady=1)

            ttk.Label(t4b, textvariable=menu_lbl, foreground="#808080", wraplength=430,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=1, columnspan=2, sticky="w")
            r4b["i"] += 1
            ttk.Label(t4b, textvariable=cat_lbl, foreground="#808080", wraplength=430,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=1, columnspan=2, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            ent(t4b, r4b, None, "超时（秒）", "单次请求超时；思考型模型建议 300 以上。",
                var=vars_["timeout"])
            ent(t4b, r4b, None, "上下文窗口",
                "该服务商模型的上下文长度（token），0 = 不声明。"
                "留 0 时文本附件按默认 %d token 折行；填上真实窗口（例如 1000000）"
                "就多带一些。注意上限还夹在 %d token：附件会留在历史里，"
                "之后每一轮都重发一遍，塞太满等于把后续每轮都变成大账单。"
                % (textfile.CLOUD_TOKEN_BUDGET, textfile.CLOUD_TOKEN_CAP),
                var=vars_["ctx"])

            i = r4b["i"]
            r4b["i"] += 1
            ttk.Checkbutton(t4b, text="启用该服务商", variable=vars_["enabled"]).grid(
                row=i, column=1, sticky="w", pady=4)

            # 「每轮显示用量」与「让云端模型自选读取区间」属于云端**文本**对话，
            # 移到本区块之后（左栏「云端模型 → 文本模型」），别混在服务商清单里。

            # ---- 云端生图 / 生视频的档位单独一页（见 _t4c）----
            # 这一页本来就到 1143px 的申请高度，已经超过屏幕可用高度；再叠 9 行
            # 就永远看不见底部了（坑 32：懒加载页要逐页量内容边界 vs 可用面积）。
            i = r4b["i"]
            r4b["i"] += 1
            ttk.Label(t4b, textvariable=jobs_lbl, foreground="#808080", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(6, 2))
            ttk.Button(t4b, text="刷新任务台账", width=14,
                       command=refresh_jobs).grid(row=i, column=2, sticky="e", pady=(6, 2))

            def save_page():
                """「保存本页」= 通用档位 + 本页 provider 字段，和底部「保存」同一条路。

                以前只走 commit()，站在这页改了云端档位再点「保存本页」会被静默丢掉
                （底部按钮才写回），是个说不通的差别。
                """
                self._apply_settings(v)
                commit()
                refresh_combo(keep=st["pid"])

            sf = ttk.Frame(t4b)
            sf.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(4, 4))
            r4b["i"] += 1
            ttk.Button(sf, text="保存本页", width=12, command=save_page).pack(side="left")
            ttk.Label(sf, textvariable=url_lbl, foreground="#808080", wraplength=430,
                      justify="left", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)
            ttk.Label(t4b, textvariable=msg_lbl, foreground="#0b57d0", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(2, 0))
            r4b["i"] += 1

            save_hooks[t4b] = lambda: commit(silent=True)
            refresh_combo()
            refresh_jobs()
        # ---- 区块 6b：云端 / 文本模型（对话行为与附件预算，跟具体服务商无关）----
        @section("cloud", "ctext")
        def _t4d(t4d, r4d):
            """云端文本对话的两项开关：用量显示 + 附件自选区间。"""
            i = r4d["i"]
            r4d["i"] += 1
            v["show_usage"] = tk.BooleanVar(value=bool(self.cfg.get("show_usage", True)))
            # 勾选框文字长，必须跨列放：占在 column=1 上会把整列撑宽，
            # 于是右侧"短摘要 + ?"那一格被 grid 挤扁（版式自检抓到过）
            ttk.Checkbutton(t4d, text="每轮结束后显示 token 用量（云端计费可见性）",
                            variable=v["show_usage"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=6)

            i = r4d["i"]
            r4d["i"] += 1
            v["cloud_file_model_decides"] = tk.BooleanVar(
                value=bool(self.cfg.get("cloud_file_model_decides", False)))
            ttk.Checkbutton(
                t4d, text="文本附件超预算时，让云端模型自己决定读哪一段",
                variable=v["cloud_file_model_decides"]).grid(
                row=i, column=0, columnspan=2, sticky="w", pady=(0, 6))
            widgets.HelpDot(t4d,
                            "打开后：附件太长时会先发一次「只要 JSON 行号」的规划请求；"
                            "模型给的范围仍会被预算与行数上限夹住，规划失败就按原预算发送，"
                            "不影响正常对话。只规划一轮，不加多轮循环（每轮都是真金白银）。"
                            "默认关。"
                            ).grid(row=i, column=2, sticky="e", pady=(0, 6))

            _row(t4d, r4d, "文本预算", ttk.Label(
                t4d, text="按服务商各自声明的上下文窗口算", foreground="#5a5a5a"),
                "云端文本的附件预算 = min(该服务商声明的上下文窗口 − max_tokens − 预留, 200000)。"
                "上限 200000 是成本护栏，不是拍脑袋：附件会留在历史里**每一轮都重发**，"
                "声明 1M 也不代表该一次塞 1M。窗口在上一区块「服务商与密钥」里按服务商填。",
                hint="上限 20 万")
        # ---- 区块 7：云端 / 生图与生视频（服务商原生接口，与本地 sd.cpp 无关）----
        @section("cloud", "cmedia")
        def _t4c(t4c, r4c):
            """云端生图 / 生视频的档位与落地目录（页面说明在标题旁的 "?" 里）。"""

            ent(t4c, r4c, "cloud_img_dir", "图片存放",
                "云端生图的落地目录；留空 = 程序目录下的 cloud_out\\images。")
            ent(t4c, r4c, "cloud_vid_dir", "视频存放",
                "云端生视频的落地目录；留空 = 程序目录下的 cloud_out\\videos。")
            fr_out = ttk.Frame(t4c)
            ttk.Button(fr_out, text="打开图片文件夹", width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "image"),
                           "云端生图的落地目录",
                           "云端模型 → 生图 / 生视频 → 图片存放")).pack(side="left")
            ttk.Button(fr_out, text="打开视频文件夹", width=14,
                       command=lambda: _open_outdir(
                           cloud_media_dir(self.cfg, "video"),
                           "云端生视频的落地目录",
                           "云端模型 → 生图 / 生视频 → 视频存放")).pack(side="left", padx=(6, 0))
            row(t4c, r4c, "输出目录", fr_out,
                "产物一落地就在这里。云端地址只活 24 小时，本地这份是唯一的留存；"
                "目录还没生成时点一下会先建出来再打开。")
            ent(t4c, r4c, "cloud_img_size", "出图尺寸",
                "填「宽x高」或「宽*高」都行，发出去前会按这一家的写法换算："
                "阿里云 1024*1024、智谱与华为 1024x1024、MiniMax 换成比例（16:9）"
                "或宽高两个整数。档位不对服务端会点名报错。"
                "实测阿里云区间：qwen-image-3.0-pro 面积 512×512…2560×2560、"
                "qwen-image-max 到 1664×1664、wan2.7-image 像素 589824…16777216。",
                hint="宽x高")
            ent(t4c, r4c, "cloud_video_resolution", "视频分辨率",
                "各家档位不一样（阿里云/MiniMax 用 720P、1080P、768P、2K 这类标签，"
                "智谱与华为用 1280x720 这种宽高）。"
                "留空 = 不传这个参数，用服务端默认——实测 Token Plan 的 happyhorse "
                "不传时给 1080P/16:9。不知道允许值时留空最安全。",
                hint="留空=默认")
            ent(t4c, r4c, "cloud_video_duration", "视频时长",
                "秒。各家允许区间不同（超范围会被服务端点名报错，原话会显示在对话里）："
                "实测 happyhorse-1.1-t2v 是 **3~15 秒**；官方页里 MiniMax Hailuo 是 6/10 秒、"
                "智谱 cogvideox-3 是 5/10 秒。按秒计费的服务商，"
                "提交前会先弹一次费用确认。", hint="各家区间不同")
            ent(t4c, r4c, "cloud_video_ratio", "画面比例",
                "例如 16:9 / 9:16 / 1:1；留空 = 不传（画面比例常由素材决定）。")
            ent(t4c, r4c, "cloud_img_negative", "生图负向词",
                "选填。留空 = 不传该参数。")
            ent(t4c, r4c, "cloud_poll_seconds", "轮询间隔",
                "秒。官方建议 15，且创建/查询/取消三个端点合计 20 QPS——"
                "调小不会让任务更快完成，只会更早撞上限流。", hint="建议 15")
            ent(t4c, r4c, "cloud_wait_minutes", "等待上限",
                "分钟。超了就不再干等，任务留在台账里，重启后对话开头会给「取回」按钮。")

            # ---- 费用单价（只在提交前显示预估用；不填就明说不知道，不编数字）----
            cands = providers.list_providers(self.cfg)
            ttk.Separator(t4c).grid(row=r4c["i"], column=0, columnspan=3,
                                    sticky="we", pady=(12, 8))
            r4c["i"] += 1
            pi_var = tk.StringVar(value="0")
            ps_var = tk.StringVar(value="0")
            # 下拉里同样只显示名字（原来写的是「id — 名称」，第一眼看去全是英文代号）
            _pp = [(lb, p) for lb, p in provider_labels(self.cfg)
                   if p in {x["id"] for x in cands}]
            _pp_back = {lb: p for lb, p in _pp}
            price_combo = ttk.Combobox(
                t4c, state="readonly", width=30,
                values=[lb for lb, _p in _pp])

            def _price_pid():
                return _pp_back.get(price_combo.get(), "")

            row(t4c, r4c, "服务商", price_combo,
                "单价写进这个服务商的记录，只用于提交前的费用预估。")
            ent(t4c, r4c, None, "元/张", "生图单价（元）。不知道就留 0，界面会明说"
                                         "「单价未填，费用以账单为准」，不会编一个数出来。",
                width=8, var=pi_var)
            ent(t4c, r4c, None, "元/秒", "生视频按秒计费的单价（元/秒）。", width=8, var=ps_var)
            price_lbl = tk.StringVar(value="")

            def pick_price_provider(*_a):
                pid = _price_pid()
                p = providers.get_provider(self.cfg, pid) or {}
                pi_var.set(str(p.get("price_per_image") or 0))
                ps_var.set(str(p.get("price_per_second") or 0))
                price_lbl.set("读到了「%s」的单价。" % p.get("name") if p else "没找到这个服务商。")

            price_combo.bind("<<ComboboxSelected>>", pick_price_provider)

            def save_price():
                pid = _price_pid()
                if not pid:
                    price_lbl.set("先选一个服务商。")
                    return
                try:
                    a = max(0.0, float(str(pi_var.get()).strip() or 0))
                    b = max(0.0, float(str(ps_var.get()).strip() or 0))
                except Exception:
                    price_lbl.set("单价要填数字（元），留 0 表示不知道。")
                    return
                if providers.update_provider(self.cfg, pid, {"price_per_image": a,
                                                             "price_per_second": b}):
                    save_config(self.cfg)
                    price_lbl.set("已写入单价：%.3f 元/张、%.3f 元/秒。" % (a, b))
                else:
                    price_lbl.set("这个服务商不在了，没写进去。")

            pf = ttk.Frame(t4c)
            pf.grid(row=r4c["i"], column=1, columnspan=2, sticky="w", pady=(2, 4))
            r4c["i"] += 1
            ttk.Button(pf, text="写入单价", width=12, command=save_price).pack(side="left")
            ttk.Label(pf, textvariable=price_lbl, foreground="#808080", wraplength=380,
                      justify="left", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=8)
            if cands:
                price_combo.current(0)
                pick_price_provider()

        # ---- 区块 8：模型文件管理 ----
        @section("files", "files")
        def _t5(t5, r5):
            _dd, _cc, _ii = scan_models(self.cfg)
            _n_rec = len(self.cfg.get("model_ngl") or {})
            _n_ctx = len(self.cfg.get("model_ctx") or {})
            _n_proj = len(self.cfg.get("model_mmproj") or {})
            _n_hid = len(self.cfg.get("model_hidden") or {})
            ttk.Label(t5, text=("当前：模型 %d 个（含可看图）｜ 层数记录 %d ｜ context 记录 %d ｜ mmproj 记录 %d ｜ 未进菜单 %d\n"
                                "打开软件时会自动补全缺失项；下方可手动触发，或整理文件结构。"
                                % (len(_cc), _n_rec, _n_ctx, _n_proj, _n_hid)),
                      foreground="#555555", wraplength=760, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            r5["i"] = 1

            frm = ttk.Frame(t5)
            ttk.Button(frm, text="管理本地模型…", width=18,
                       command=self.open_local_models).pack(side="left")
            row(t5, r5, "菜单与配套件", frm,
                "三组模型与各自的配套件（VAE / 文本编码器 / CLIP / T5…）摊开在一页里，"
                "可逐个勾选要不要出现在顶部模型菜单 —— 生图 / 生视频附带的"
                "「能聊天的 .gguf 编码器」常需要收起来。只改显示，不动文件。")

            fr = ttk.Frame(t5)
            ttk.Button(fr, text="扫描并补全缺失项", width=18,
                       command=lambda: self._manual_scan(False)).pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="全部重新计算（覆盖）", width=18,
                       command=lambda: self._manual_scan(True)).pack(side="left")
            row(t5, r5, "重新扫描", fr,
                "补全 = 只为缺记录的模型计算（不覆盖手动调整过的值）；"
                "重新计算 = 清空全部自动记录后重算（含覆盖手调值，会二次确认）。")

            fr2 = ttk.Frame(t5)
            ttk.Button(fr2, text="整理模型文件夹", width=18,
                       command=self._open_tidy_dialog).pack(side="left")
            row(t5, r5, "文件整理", fr2,
                "把模型目录顶层散落的模型与其配对 mmproj 归入各自子文件夹"
                "（先预览、后执行；只移动不删除；名称无法判断归属的保持原位）。")

        # ---- 页面容器 / 区块构建 / 锚点定位（v39：替代原来的 Notebook + _fit）----
        def _make_page(page_id):
            """建一个页面的 frame，并把它的区块标题与空内容框先摆好（便宜，只有标签）。

            标题先建、内容后建，好处是**锚点在构建之前就存在**：左栏点「服务参数」时
            哪怕那一区块还没建，也知道该滚到哪里。
            """
            f = page_frames.get(page_id)
            if f is not None:
                return f
            bg = widgets.default_bg()
            f = tk.Frame(sp.inner, background=bg)
            items = page_items[page_id]
            for idx, it in enumerate(items):
                head = tk.Frame(f, background=bg)
                head.pack(fill="x", padx=2, pady=(6 if idx == 0 else 20, 2))
                tk.Label(head, text=it["title"], background=bg, foreground="#111111",
                         font=("Microsoft YaHei UI", 11, "bold"), anchor="w").pack(side="left")
                widgets.HelpDot(head, it.get("help") or "").pack(side="left", padx=(8, 0))
                body = tk.Frame(f, background=bg)
                body.columnconfigure(2, weight=1)
                body.pack(fill="x", padx=2)
                # 给自检脚本一个稳定的定位方式（按 grid 行号找控件在拆框之后就会错位）
                body._sec_id = it["section"]
                heads[(page_id, it["section"])] = head
                bodies[(page_id, it["section"])] = body
            page_frames[page_id] = f
            return f

        def _build_section(page_id, sec_id):
            """按需构建区块内容（幂等：建过的不重建，控件状态也就不会丢）。"""
            key = (page_id, sec_id)
            body = bodies.get(key)
            if body is None or getattr(body, "_done", False):
                return body
            body._done = True
            build = registry.get(key)
            if build is None:
                ttk.Label(body, text="（这个区块还没接上构建函数）",
                          foreground="#c01c28").grid(row=0, column=0, sticky="w")
                return body
            build(body, {"i": 0})
            return body

        def _unflash(lbl):
            try:
                if lbl.winfo_exists():
                    lbl.configure(background=widgets.default_bg())
            except Exception:
                pass

        def _flash(head):
            """定位过去之后把区块标题闪一下底色：不闪的话用户不知道页面滚到了哪儿。"""
            if head is None:
                return
            lbl = [w for w in head.winfo_children() if isinstance(w, tk.Label)]
            if not lbl:
                return
            try:
                lbl[0].configure(background="#fff3c4")
                win.after(1200, lambda: _unflash(lbl[0]))
            except Exception:
                pass

        def _nav_select(item):
            page_id, sec_id = item["page"], item["section"]
            f = _make_page(page_id)
            if state["page"] != page_id:
                sp.set_page(f)
                state["page"] = page_id
            # 同一页的区块**一次建齐**（W：懒加载不能只建点中的那个小标题）：
            # 只建一段的话，往下滚就是半页空白，底部「保存」也只跑得到已建区块的钩子。
            # 省下来的仍然是"别的页"的构建开销，那才是首屏卡顿的大头。
            for it in page_items[page_id]:
                _build_section(page_id, it["section"])
            state["section"] = sec_id
            # 这里不再 win.update_idletasks()：goto() 里的 _refresh() 已经做过一次全树
            # 布局（Tk 的 update_idletasks 是全应用级的，不是只管那一个控件），
            # 再来一次纯属白付 —— 实测一次全树重算 ~17ms。
            head = heads.get((page_id, sec_id))
            sp.goto(head)
            _flash(head)

        def _global_save(restart=False):
            """底部「保存」= 通用参数 + **当前这一页里已建区块各自的保存逻辑**，都成功才关窗。

            原来只跑当前标签页那一个钩子；现在一页里挂着多个区块（云端页就有服务商 /
            文本 / 生图生视频三段），所以把**这一页已经建起来的**区块钩子按顺序都跑一遍，
            任一个不通过就不关窗 —— 保住"底部保存不会把自己刚填的东西丢掉"这条既有语义。
            """
            self._apply_settings(v)
            for it in (page_items.get(state["page"]) or []):
                body = bodies.get((it["page"], it["section"]))
                if body is None or not getattr(body, "_done", False):
                    continue
                hook = save_hooks.get(body)
                if hook is None:
                    continue
                try:
                    ok, msg = hook() or (True, "")
                except Exception as e:
                    ok, msg = False, "这一区块没能保存：%s" % e
                if not ok:
                    messagebox.showwarning(
                        "还没保存", msg or "这一页有内容没通过检查，请改好再保存。")
                    return
            win.destroy()
            if restart:
                self.restart_server()

        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="保存并重启服务",
                   command=lambda: _global_save(restart=True)).pack(side="right", padx=4)
        ttk.Button(bar, text="保存", command=_global_save).pack(side="right")

        nav.select(leaves[0]["key"])      # 首屏：本地模型 → 文本模型 → 生成参数

    def _apply_settings(self, v):
        c = self.cfg
        # 云端模型没有 ngl / context 概念：跳过按模型记忆，否则会把 "pid::model"
        # 这种复合 id 当文件名写进 model_ngl / model_ctx，污染配置
        local = not providers.is_cloud(c)
        for k in INT_KEYS:
            if k in v:
                try:
                    c[k] = int(str(v[k].get()).strip())
                except Exception:
                    pass
        # context 按模型记忆（主页面启动用）：写入当前模型的记录
        if local and "ctx" in c:
            try:
                c.setdefault("model_ctx", {})[
                    os.path.basename(c.get("model", ""))] = int(c["ctx"])
            except Exception:
                pass
        # agent 场景 context 按模型记忆
        if local and "agent_ctx" in v:
            try:
                c.setdefault("model_ctx_api", {})[
                    os.path.basename(c.get("model", ""))] = int(
                        str(v["agent_ctx"].get()).strip())
            except Exception:
                pass
        for k in FLOAT_KEYS:
            if k in v:
                try:
                    c[k] = float(str(v[k].get()).strip())
                except Exception:
                    pass
        for k in STR_KEYS:
            if k in v:
                c[k] = str(v[k].get()).strip()
        # 模型族下拉显示的是中文标签，配置里要存回 sdprofile 的家族代码；
        # 选"自动判断"存空串——别让界面文案跑到配置里去。
        for k, kind in (("img_family", "image"), ("vid_family", "video")):
            if k in v:
                code = {t: cf for cf, t in sdprofile.family_choices(kind)}.get(
                    str(v[k].get()), sdprofile.AUTO)
                c[k] = "" if code == sdprofile.AUTO else str(code)
        if "system_prompt" in v:
            c["system_prompt"] = v["system_prompt"].get("1.0", "end").rstrip("\n")
        if "show_reasoning" in v:
            c["show_reasoning"] = bool(v["show_reasoning"].get())
        if "proxy_enabled" in v:
            c["proxy_enabled"] = bool(v["proxy_enabled"].get())
        if "show_usage" in v:
            c["show_usage"] = bool(v["show_usage"].get())
        if "cloud_file_model_decides" in v:
            c["cloud_file_model_decides"] = bool(v["cloud_file_model_decides"].get())
        # ngl 按模型记忆：设置页改的是"当前模型"的层数（云端模型没有层数概念）
        if local and isinstance(c.get("ngl"), int):
            c.setdefault("model_ngl", {})[os.path.basename(c["model"])] = c["ngl"]
        c["cfg_version"] = CFG_VERSION
        save_config(c)
        self._update_model_label()
