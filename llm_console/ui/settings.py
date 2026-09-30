# -*- coding: utf-8 -*-
"""llm_console.ui.settings — 界面 Mixin：设置窗口（7 个标签页懒加载 + 尺寸自适应）、API 连接页、云端 API 页"""

import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, font as tkfont

from ..core import capability, providers, secrets, textfile
from ..core.config import CFG_VERSION, FLOAT_KEYS, INT_KEYS, STR_KEYS, gen_api_key, save_config
from ..core.models import scan_models
from ..core.params import ctx_for, current_ngl
from ..core.server import _query_serving_model, server_process_alive
from ..connection import cloud


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
        win.geometry("1020x760")
        win.minsize(900, 680)
        win.transient(self.root)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=12, pady=(12, 4))
        v = {}

        tabs = []                     # [(占位 frame, 构建函数)]：切到哪页才建哪页
        save_hooks = {}               # 页 frame → callable()→(ok, 说明)：底部「保存」要一起跑的页

        def tab(title):
            """注册一个标签页：先占位，用户切换过去时才构建页内控件。

            设置页共 7 页、约 200 个控件；本机每个 ttk 控件创建+布局约 1.3ms，
            一次性全建约 280ms（打开时能感到卡顿）。改为按需构建后，打开设置
            只建首屏（约 1/7），其余在切页时瞬时补建。
            干净进程实测各页首次构建：生成参数 1ms、模型管理 74ms、生图 127ms、
            云端 API 176ms、API 连接 209ms、服务参数 261ms、生视频 282ms。
            """
            def deco(build):
                ph = ttk.Frame(nb, padding=(14, 10))
                ph.columnconfigure(2, weight=1)
                nb.add(ph, text=title)
                tabs.append((ph, build))
                return build
            return deco

        def row(parent, rows, label, widget, desc):
            i = rows["i"]
            rows["i"] += 1
            ttk.Label(parent, text=label, width=14, anchor="w").grid(
                row=i, column=0, sticky="w", padx=(0, 8), pady=5)
            widget.grid(row=i, column=1, sticky="w", padx=(0, 12), pady=5)
            ttk.Label(parent, text=desc, foreground="#808080", wraplength=400,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=i, column=2, sticky="w", pady=5)

        def ent(parent, rows, key, label, desc, width=8, var=None, trace=None):
            """一行"标签 + 输入框 + 说明"。

            key 非空时变量登记进 v（由 _apply_settings 统一写回 cfg）；
            传 var 则用外部变量（云端 API 页的 provider 字段是结构化数据，
            自己管保存，不走 _apply_settings）。trace 用于即时联动（如回显请求地址）。
            """
            if var is None:
                var = v.setdefault(key, tk.StringVar(value=str(self.cfg.get(key, ""))))
            e = ttk.Entry(parent, textvariable=var, width=width)
            if trace is not None:
                var.trace_add("write", lambda *a: trace())
            row(parent, rows, label, e, desc)

        # ---- tab1 生成参数 ----
        @tab(" 生成参数（保存即生效） ")
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
                "Qwen3 思考较长，建议 ≥4096；到上限会被截断并提示。")
            ent(t1, r1, "seed", "seed",
                "随机种子：-1 表示随机；填固定数字可复现同一次输出。")

            i = r1["i"]
            r1["i"] += 2
            ttk.Label(t1, text="system_prompt", width=14, anchor="nw").grid(
                row=i, column=0, sticky="nw", padx=(0, 8), pady=5)
            v["system_prompt"] = tk.Text(t1, height=3, font=("Microsoft YaHei UI", 9))
            v["system_prompt"].grid(row=i, column=1, columnspan=2, sticky="ew", pady=5)
            v["system_prompt"].insert("1.0", str(self.cfg.get("system_prompt", "")))
            ttk.Label(t1, text="系统提示词：给模型的人设与规则，自动放在每轮对话最前面。",
                      foreground="#808080", wraplength=560, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(row=i + 1, column=1,
                                                           columnspan=2, sticky="w")

            i = r1["i"]
            r1["i"] += 1
            v["show_reasoning"] = tk.BooleanVar(value=bool(self.cfg.get("show_reasoning", True)))
            ttk.Checkbutton(t1, text="在对话中显示模型的思考过程（reasoning，灰色斜体）",
                            variable=v["show_reasoning"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=8)

        # ---- tab2 服务参数 ----
        @tab(" 服务参数（保存后点「重启服务」生效） ")
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
                "当前模型 GGUF 完整路径（也可直接点主页模型名切换）。", width=42)
            ent(t2, r2, "models_dir", "models_dir",
                "模型文件夹：主页模型下拉列表扫描此目录下所有 .gguf 文件。", width=42)
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
                "此类模型请适当降低，否则占用大量内存/显存（启动时界面会显示 KV 预估）。")
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
                "llama-server.exe 完整路径。", width=42)

        # ---- tab3 生图 ----
        @tab(" 生图（sd.cpp / Qwen-Image 2.1） ")
        def _t3(t3, r3):
            ent(t3, r3, "sd_dir", "引擎目录",
                "sd.cpp 引擎所在目录（内含 sd-cli.exe / sd-server.exe）。", width=42)
            ent(t3, r3, "image_model_dir", "生图模型文件夹",
                "生图大模型统一存放处（llm modle\\生图）：扩散模型、文本编码器、VAE、视觉编码器都在这里。", width=42)
            ent(t3, r3, "img_model_file", "默认生图模型",
                "生图窗口默认选中的扩散模型文件名（如 qwen_image_2.1-Q6_K.gguf）。", width=36)
            ent(t3, r3, "img_steps", "默认步数",
                "默认采样步数（4~50）：8 步 ~1m20s，12 步 ~1m50s，20 步 ~2m50s；少=快，多=细节更多。")
            ent(t3, r3, "img_size", "默认分辨率",
                "宽x高，如 1024x1024。分辨率越高越慢。", width=12)
            ent(t3, r3, "img_cfg", "默认 CFG",
                "提示词服从度，官方推荐 2.5。")
            ent(t3, r3, "img_seed", "默认种子",
                "-1 随机；固定数字可复现同一次输出。")

            ttk.Button(t3, text="打开输出文件夹",
                       command=lambda: os.startfile(os.path.join(
                           self.cfg.get("sd_dir", ""), "output"))).grid(
                row=r3["i"], column=1, sticky="w", pady=5)
            r3["i"] += 1

        # ---- tab3b 生视频（sd.cpp / MiniMax-H3）----
        @tab(" 生视频（sd.cpp / MiniMax-H3） ")
        def _t3b(t3b, r3b):
            ent(t3b, r3b, "video_model_dir", "视频模型文件夹",
                "视频组件存放目录（放 MiniMax-H3 的扩散主体 + 文本编码器 + 视频 VAE）。"
                "该目录不存在时会自动改扫 models_dir 顶层与各子目录，所以文件散放在模型库里也能识别。",
                width=42)
            ent(t3b, r3b, "vid_model_file", "扩散主体文件名",
                "留空 = 用扫描到的第一个视频扩散 GGUF。", width=42)
            ent(t3b, r3b, "vid_llm_file", "文本编码器文件名",
                "留空 = 自动取与扩散主体配套的编码器（按文件名匹配，通常名字里带 vl / llm）。", width=42)
            ent(t3b, r3b, "vid_vae_file", "视频 VAE 文件名",
                "留空 = 在主体所在目录里按文件名含 vae 自动找（不含 audio 的那个）。", width=42)
            ent(t3b, r3b, "vid_size", "分辨率",
                "宽x高，如 512x512。视频分辨率对显存和耗时都很敏感，先小后大。", width=12)
            ent(t3b, r3b, "vid_frames", "帧数",
                "视频长度 = 帧数 ÷ 帧率。多数视频 VAE 要求 4n+1 帧（17 / 33 / 49…）。")
            ent(t3b, r3b, "vid_fps", "帧率",
                "每秒帧数。MiniMax-H3 的参考视频按 24fps 组织。")
            ent(t3b, r3b, "vid_steps", "采样步数",
                "步数直接决定耗时；链路先通再逐步加大。")
            ent(t3b, r3b, "vid_cfg", "CFG",
                "提示词服从度。大于 1 时引擎会去编码负向提示词，所以下面那栏不能留空。")
            ent(t3b, r3b, "vid_neg_prompt", "负向提示词",
                "**不能为空**：MiniMax-H3 在 CFG>1 时必须编码负向提示词，留空会报 "
                "failed to encode negative video prompt 并以退出码 1 结束（代码里有兜底默认值）。",
                width=42)
            ent(t3b, r3b, "vid_format", "输出容器",
                "webm / avi / webp（sd-cli 单文件视频输出只支持这三种）。", width=10)
            ent(t3b, r3b, "vid_seed", "种子", "-1 随机。")
            ent(t3b, r3b, "vid_backend", "组件后端",
                "sd-cli --backend：各组件跑在哪。默认把文本编码器放 CPU、扩散与 VAE 放显卡，"
                "与生图一致。", width=42)
            ent(t3b, r3b, "vid_params_backend", "权重后端",
                "sd-cli --params-backend：权重放哪。显存吃紧时可填 diffusion=disk 让引擎从内存/磁盘流式取权重。",
                width=42)
            ent(t3b, r3b, "vid_extra_args", "附加参数",
                "原样拼进命令行。默认开了 --vae-tiling --temporal-tiling 分块解码来压显存。",
                width=42)

            fr_v = ttk.Frame(t3b)
            ttk.Button(fr_v, text="打开视频输出文件夹",
                       command=lambda: os.startfile(os.path.join(
                           self.cfg.get("sd_dir", ""), "video"))).pack(side="left")
            row(t3b, r3b, "输出目录", fr_v,
                "生成结果写在 sd.cpp\\video\\vid_时间戳.webm；引擎每次按需拉起，进程退出即释放显存。")

        # ---- tab4 API 连接（供 agent 调用） ----
        @tab(" API 连接（供 agent 调用） ")
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
            ttk.Entry(fr, textvariable=v_bu, width=30, state="readonly").pack(side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(v_bu.get(), "Base URL")).pack(side="left")
            row(t4, r4, "Base URL", fr, "agent 应用填写此地址（本代理）；不要填 8080（那是后端服务）。")

            fr = ttk.Frame(t4)
            ttk.Entry(fr, textvariable=api_key_var, width=30, state="readonly").pack(
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
            ttk.Entry(fr, textvariable=api_model_var, width=30, state="readonly").pack(
                side="left", padx=(0, 6))
            ttk.Button(fr, text="复制", width=6,
                       command=lambda: self._copy_text(api_model_var.get(), "模型名")).pack(side="left")
            row(t4, r4, "模型名", fr, "建议填写值（实时取服务加载的模型）；支持模糊匹配，略写也能命中。")

            fr = ttk.Frame(t4)
            ttk.Button(fr, text="复制完整配置（含填法说明）", width=28,
                       command=lambda: self._copy_text(self._api_config_text(), "完整配置")).pack(side="left")
            row(t4, r4, "一键复制", fr, "粘贴到任意 agent 应用的自定义模型配置即可接入。")

            ent(t4, r4, "proxy_port", "代理端口",
                "agent 接入端口（默认 8081）；改动后点「重启代理」生效。", width=8)
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
            ttk.Label(t4, text="生图：暂未提供 OpenAI 兼容接口（后续评估兼容格式）。",
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

        # ---- tab4b 云端 API ----
        @tab(" 云端 API ")
        def _t4b(t4b, r4b):
            """云端服务商页。

            约定（v33）：内置服务商只内置"名称 + base_url"；添加服务商**不会**自动把
            它名下所有模型塞进主页面菜单。流程是 填信息 → 填密钥 → 测试连接 →
            通过后弹出独立的「模型选择」界面，由用户勾选要用的模型；
            拉到的清单缓存在 provider.model_kinds 里，只有点「刷新模型」才重新请求。
            """
            ttk.Label(t4b, text=(
                "云端文本对话走通用 OpenAI 兼容协议。已内置的服务商不用填名称与地址"
                "（跨计费方案的地址写错只会换来一次鉴权失败，所以不开放修改）；"
                "要接其它服务商，在上方选「＋ 新建 provider」。"
                "密钥单独存 secrets.json，**不进备份**。"),
                foreground="#808080", wraplength=680, justify="left",
                font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 10))
            r4b["i"] += 1

            st = {"pid": "", "builtin": False, "models": [], "kinds": {}, "cands": []}
            vars_ = {}
            for k in ("name", "base_url", "timeout", "ctx"):
                vars_[k] = tk.StringVar()
            vars_["enabled"] = tk.BooleanVar(value=True)
            url_lbl = tk.StringVar(value="")
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
                             "ctx": vars_["ctx"].get()})
                return providers.normalize_provider(base)

            def refresh_url(*_a):
                if st["builtin"]:
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

            def refresh_menu_list(_e=None):
                lb.delete(0, "end")
                for m in st["models"]:
                    lb.insert("end", "%s · %s" % (providers.KIND_LABEL[
                        providers.model_kind_of({"model_kinds": st["kinds"]}, m)], m))
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
                vars_["enabled"].set(bool((p or {}).get("enabled", True)) if p else True)
                # 内置服务商：名称与地址不开放填写，整块收起（grid_remove 会记住原位）
                if st["builtin"]:
                    built_note.grid()
                    fields.grid_remove()
                else:
                    built_note.grid_remove()
                    fields.grid()
                refresh_url()
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
                        "ctx": vars_["ctx"].get()})
                    if not ok:
                        msg_lbl.set("保存失败：%s" % err)
                        return False, err      # 半填的新服务商：留着窗口让用户补
                    st["pid"] = base_id
                    save_config(self.cfg)
                    refresh_combo(keep=base_id)
                    if not silent:
                        msg_lbl.set("已新建服务商「%s」。" % base_id)
                    self._update_model_label()
                    return True, ""
                data = {"name": vars_["name"].get(), "base_url": vars_["base_url"].get(),
                        "models": list(st["models"]), "model_kinds": dict(st["kinds"]),
                        "enabled": vars_["enabled"].get(),
                        "timeout": vars_["timeout"].get(),
                        "ctx": vars_["ctx"].get()}
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
                       % pid) if providers.is_builtin(pid) else "删除「%s」？" % pid
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
                e = ttk.Entry(d, width=52, show="●")
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
                """本页的密钥是给云端用的；本机 API 地址在「API 连接」页配。"""
                for i in range(len(nb.tabs())):
                    if "API 连接" in nb.tab(i, "text"):
                        nb.select(i)
                        return
                msg_lbl.set("没有「API 连接」页。")

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
                hint_lbl = ttk.Label(head, text="勾中并点「确定」才进主页面菜单；「图片输入」一改即生效",
                                     foreground="#808080", wraplength=520, justify="left",
                                     font=("Microsoft YaHei UI", 9))
                hint_lbl.pack(side="left")
                status = tk.StringVar(value=explain or
                                      ("表里是已知的 %d 个模型；点「刷新清单」可向接口重新索取。"
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
                rc = {"i": 0}
                cells = {}                      # 模型名 → (勾选框, 图片输入下拉, 类型下拉)
                sizes = {"col0": 300, "need0": 200}   # 模型列：当前宽 / 名字真正需要的宽
                _fnt = tkfont.Font(family="Microsoft YaHei UI", size=9)

                def shorten(name, px):
                    """按列宽截断显示名；rows 的键仍是完整模型名，写配置不会错。

                    ttk.Checkbutton 在这个 Tk 版本上没有 anchor/justify/wraplength，
                    只能从"显示"这一侧解决：量着列宽裁。
                    """
                    px = max(px, 24)
                    if _fnt.measure(name) <= px:
                        return name
                    cut = name
                    while cut and _fnt.measure(cut + "…") > px:
                        cut = cut[:-1]
                    return (cut or name[:1]) + "…"

                def label_px():
                    return sizes["col0"] - CK_PAD - 4

                def relabel():
                    for m, (c, _i, _k) in cells.items():
                        c.configure(text=shorten(m, label_px()))

                def align_hdr():
                    base = hdr.winfo_rootx()
                    if base <= 0 or not cells:
                        return
                    cb, ic, kc = next(iter(cells.values()))
                    for lbl, w in ((hdr_lbl["模型"], cb), (hdr_lbl["图片输入"], ic),
                                   (hdr_lbl["模型类型"], kc)):
                        lbl.place(x=max(w.winfo_rootx() - base, 0), y=1)

                def on_resize(_e=None):
                    """按画布实际宽度定列宽：模型列"够用就好"，两个下拉列定宽。

                    以前只设 inner 宽度，长模型名会把右侧两列挤出容器（W 报的"溢出"）；
                    模型列拉满整行又会让名字和下拉之间空一大截，所以按最长的名字收口，
                    多出来的宽度交给第 4 列空白。
                    """
                    avail = max(cv.winfo_width() - 4, 300)
                    sizes["col0"] = max(min(sizes["need0"], avail - 2 * (COL_W + GAP)), 110)
                    cv.itemconfigure(inner, width=avail)
                    table.columnconfigure(0, minsize=sizes["col0"])
                    inner_w = max(320, d.winfo_width() - 30)
                    st_lbl.configure(wraplength=inner_w)
                    hint_lbl.configure(wraplength=inner_w)
                    relabel()
                    cv.update_idletasks()
                    align_hdr()
                    bb = cv.bbox("all")
                    if bb:
                        cv.configure(scrollregion=bb)
                cv.bind("<Configure>", on_resize)

                def add_row(m, kind, checked):
                    if m in rows:
                        return
                    # 名字比现在的列宽还长 → 整表重新收口（列宽到顶时 shorten 会接手截断）
                    need = CK_PAD + _fnt.measure(m) + 10
                    if need > sizes["need0"]:
                        sizes["need0"] = need
                        if need > sizes["col0"]:
                            on_resize()
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
                    i = rc["i"]
                    rc["i"] += 1
                    cb = ttk.Checkbutton(table, text=shorten(m, label_px()), variable=kv)
                    cb.grid(row=i, column=0, sticky="w", pady=1)
                    ic = ttk.Combobox(table, textvariable=cv_img, state="readonly", width=9,
                                      values=[capability.CHOICE_LABEL[c] for c in
                                              capability.CHOICES])
                    ic.grid(row=i, column=1, sticky="w", padx=(GAP, 0))
                    kc = ttk.Combobox(table, textvariable=cv_kind, state="readonly",
                                      width=9,
                                      values=[providers.KIND_LABEL[k] for k in
                                              providers.KIND_ORDER])
                    kc.grid(row=i, column=2, sticky="w", padx=(GAP, 0))
                    # 一改即落盘：不依赖用户点「确定」（见 declare_now 的说明）
                    cv_img.trace_add("write", lambda *a: declare_now(m, cv_img.get()))
                    cells[m] = (cb, ic, kc)
                    rows[m] = [kv, cv_kind, cv_img]
                    table.update_idletasks()
                    align_hdr()
                    bb = cv.bbox("all")
                    if bb:
                        cv.configure(scrollregion=bb)

                for m in known:
                    add_row(m, providers.model_kind_of({"model_kinds": st["kinds"]}, m),
                            m in st["models"])
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
                    for m in ids:
                        if m not in rows:
                            add_row(m, providers.guess_kind(m), m in st["models"])
                            added += 1
                    # 顶部说明已经讲过"勾中并点确定"，这里只报接口给了多少，不重复
                    status.set("接口给了 %d 个模型（新增 %d 个）。" % (len(ids), added))

                def try_add():
                    m = e_new.get().strip()
                    if not m:
                        status.set("先输入模型名。")
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
                        add_row(m, providers.guess_kind(m), True)
                        status.set("✅ %s：%s（已勾上）" % (m, text))
                    else:
                        status.set("❌ %s：%s" % (m, text))

                # 底部三件套：手填行（含刷新/验证按钮）+ 确定取消，都排在表格之前分配空间，
                # 窗口再矮也是压表格，不会把按钮挤掉
                botf = ttk.Frame(d)
                botf.pack(side="bottom", fill="x", padx=12, pady=(4, 2), before=wrap)
                ttk.Label(botf, text="模型名", foreground="#808080",
                          font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 4))
                e_new = ttk.Entry(botf, width=16)
                e_new.pack(side="left", fill="x", expand=True)
                ttk.Button(botf, text="试一试并加入", width=12,
                           command=try_add).pack(side="left", padx=(6, 0))
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
                    msg_lbl.set("已写入主页面模型：%s" % ("、".join(picked) or "（没勾任何模型）"))
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
                ids = [p["id"] for p in providers.list_providers(self.cfg, enabled_only=False)]
                combo["values"] = ids + ["＋ 新建 provider"]
                target = keep if keep in ids else (ids[0] if ids else "")
                combo.set(target or "＋ 新建 provider")
                load(target)

            def on_pick(_e=None):
                sel = combo.get()
                load("" if sel not in [p["id"] for p in
                                       providers.list_providers(self.cfg, enabled_only=False)]
                     else sel)

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
            ttk.Label(kr, textvariable=key_lbl, foreground="#808080",
                      font=("Microsoft YaHei UI", 9)).pack(side="left", padx=8)

            built_note = ttk.Label(t4b, text="名称与 base_url：内置（只需填密钥、选模型）。",
                                   foreground="#808080", wraplength=640, justify="left",
                                   font=("Microsoft YaHei UI", 9))
            built_note.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(0, 6))
            r4b["i"] += 1

            fields = ttk.Frame(t4b)
            fields.grid(row=r4b["i"], column=0, columnspan=3, sticky="w")
            sub = {"i": 0}
            ent(fields, sub, None, "名称", "服务商显示名；新建时用它生成索引，起个短的英文名更好用。",
                width=30, var=vars_["name"])
            ent(fields, sub, None, "base_url", "OpenAI 兼容根地址，带不带 /v1 都行（会自动补）。",
                width=52, var=vars_["base_url"], trace=refresh_url)
            r4b["i"] += 1

            mf = ttk.Frame(t4b)
            mf.grid(row=r4b["i"], column=1, columnspan=2, sticky="w", pady=(2, 4))
            r4b["i"] += 1
            lb = tk.Listbox(mf, height=5, width=56, exportselection=False,
                            font=("Microsoft YaHei UI", 9))
            lb.pack(side="left")
            btns = ttk.Frame(mf)
            btns.pack(side="left", padx=(6, 0), fill="y")
            ttk.Button(btns, text="选择模型…", width=14,
                       command=do_pick_models).pack(anchor="w", pady=1)
            ttk.Button(btns, text="移出选中项", width=14,
                       command=do_remove_selected).pack(anchor="w", pady=1)
            ttk.Button(btns, text="刷新清单", width=14,
                       command=lambda: (commit(silent=True),
                                        open_picker(fetch=True))).pack(anchor="w", pady=1)

            ttk.Label(t4b, textvariable=menu_lbl, foreground="#808080",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=1, columnspan=2, sticky="w")
            r4b["i"] += 1
            ttk.Label(t4b, textvariable=cat_lbl, foreground="#808080",
                      font=("Microsoft YaHei UI", 9)).grid(
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

            i = r4b["i"]
            r4b["i"] += 1
            v["show_usage"] = tk.BooleanVar(value=bool(self.cfg.get("show_usage", True)))
            ttk.Checkbutton(t4b, text="每轮结束后显示 token 用量（云端计费可见性）",
                            variable=v["show_usage"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=6)

            i = r4b["i"]
            r4b["i"] += 1
            v["cloud_file_model_decides"] = tk.BooleanVar(
                value=bool(self.cfg.get("cloud_file_model_decides", False)))
            ttk.Checkbutton(
                t4b, text="文本附件超预算时，让云端模型自己决定读哪一段"
                          "（多一次规划请求，默认关）",
                variable=v["cloud_file_model_decides"]).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=(0, 6))
            ttk.Label(t4b, text=("打开后：附件太长时会先发一次"
                                 "「只要 JSON 行号」的规划请求，模型给的范围仍会被预算与行数上限夹住；"
                                 "规划失败就按原预算发送，不影响正常对话。"),
                      foreground="#808080", wraplength=680, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=i + 1, column=0, columnspan=3, sticky="w", pady=(20, 2))

            sf = ttk.Frame(t4b)
            sf.grid(row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(4, 4))
            r4b["i"] += 1
            ttk.Button(sf, text="保存本页", width=12,
                       command=lambda: (commit(), refresh_combo(keep=st["pid"]))).pack(side="left")
            ttk.Label(sf, textvariable=url_lbl, foreground="#808080",
                      font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)
            ttk.Label(t4b, textvariable=msg_lbl, foreground="#0b57d0", wraplength=680,
                      justify="left", font=("Microsoft YaHei UI", 9)).grid(
                row=r4b["i"], column=0, columnspan=3, sticky="w", pady=(2, 0))
            r4b["i"] += 1

            save_hooks[t4b] = lambda: commit(silent=True)
            refresh_combo()
        # ---- tab5 模型管理 ----
        @tab(" 模型管理 ")
        def _t5(t5, r5):
            _dd, _cc, _ii = scan_models(self.cfg)
            _n_rec = len(self.cfg.get("model_ngl") or {})
            _n_ctx = len(self.cfg.get("model_ctx") or {})
            _n_proj = len(self.cfg.get("model_mmproj") or {})
            ttk.Label(t5, text=("当前：模型 %d 个（含可看图）｜ 层数记录 %d ｜ context 记录 %d ｜ mmproj 记录 %d\n"
                                "打开软件时会自动补全缺失项；下方可手动触发，或整理文件结构。"
                                % (len(_cc), _n_rec, _n_ctx, _n_proj)),
                      foreground="#555555", wraplength=760, justify="left",
                      font=("Microsoft YaHei UI", 9)).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            r5["i"] = 1

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

        fit = {"w": 1020, "h": 760, "applied": None}

        def _fit():
            """按"已构建页面的最大需求尺寸"撑开窗口，只增不减。

            标签页是懒加载的：末尾那次尺寸计算原本只看得到首屏，后建的高页面
            （生视频 15 行 ≈ 660px）会被固定的 760 高度裁掉底部，宽度同理受
            6 个标签标题排成一行的总宽限制。所以每构建完一页都重新量一次，
            上限收在屏幕尺寸内（窗口比屏幕高会让底部按钮永远点不到）。
            尺寸没变时不调 geometry()：geometry 会触发整棵控件树重排，
            切页时白付一次布局开销。
            """
            win.update_idletasks()
            scr_w, scr_h = win.winfo_screenwidth(), win.winfo_screenheight()
            fit["w"] = min(max(fit["w"], nb.winfo_reqwidth() + 60), scr_w - 40)
            fit["h"] = min(max(fit["h"], nb.winfo_reqheight() + 130), scr_h - 80)
            if fit["applied"] != (fit["w"], fit["h"]):
                fit["applied"] = (fit["w"], fit["h"])
                win.geometry("%dx%d" % (fit["w"], fit["h"]))

        def _build_tab(i):
            """按需构建第 i 页（幂等；已建过的页不会重复构建）。"""
            ph, build = tabs[i]
            if getattr(ph, "_built", False):
                return
            ph._built = True
            build(ph, {"i": 0})
            _fit()

        def _on_tab_changed(_e=None):
            try:
                i = nb.index("current")
            except Exception:
                return
            _build_tab(i)
            tabs[i][0].update_idletasks()

        nb.bind("<<NotebookTabChanged>>", _on_tab_changed)
        _build_tab(0)                 # 首屏立即构建并可见

        def _global_save(restart=False):
            """底部「保存」= 通用参数 + **当前这一页自己的保存逻辑**，都成功才关窗。

            以前这里只跑 _apply_settings（生成参数/服务参数那几页的字段），
            站在云端页点保存会把自己刚填的东西丢掉还照样关窗——现在把每页的
            保存回调注册进 save_hooks，底部按钮统一调，页面自己那套逻辑不变。
            """
            self._apply_settings(v)
            cur = None
            try:
                cur = nb.nametowidget(nb.tabs()[nb.index("current")])
            except Exception:
                pass
            hook = save_hooks.get(cur)
            if hook is not None:
                try:
                    ok, msg = hook() or (True, "")
                except Exception as e:
                    ok, msg = False, "这一页没能保存：%s" % e
                if not ok:
                    messagebox.showwarning("还没保存", msg or "这一页有内容没通过检查，请改好再保存。")
                    return
            win.destroy()
            if restart:
                self.restart_server()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="保存并重启服务",
                   command=lambda: _global_save(restart=True)).pack(side="right", padx=4)
        ttk.Button(bar, text="保存", command=_global_save).pack(side="right")

        # 兜底：按已构建内容的实际需求撑开窗口（切页时 _build_tab 会再量一次）
        _fit()

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
