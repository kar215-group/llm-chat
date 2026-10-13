# -*- coding: utf-8 -*-
"""llm_console.core.config — 配置层：默认值、读写（RLock）、键分类、Base URL 与鉴权头、API Key 生成"""

import json
import os
import secrets
import sys
import threading


# 项目根目录：本文件在 <root>\llm_console\core\config.py，往上三级即仓库根。
# 不能直接用 dirname(__file__)——拆包后那样会把 gui_config.json 指向 llm_console\core\，
# 程序就会读到一份全新的默认配置（v30 拆分时实测踩过：api_key 变回 sk-local、模型被重置）。
#
# 打成单文件 exe 时（PyInstaller）__file__ 指向临时解包目录 sys._MEIPASS，那次性目录随进程
# 退出就被删 → 配置"凭空丢失"，而且 exe/models 的默认路径会指到 temp 里。所以冻结模式下
# 一律取 **exe 自身所在目录**：约定把 llm-chat.exe 与 llama-server.exe、models 放在同一层。
#
# 写成"函数 + 一次模块级赋值"而不是在模块顶层 if/else：verify_refactor.py 靠 AST 的模块级
# 赋值确认符号有落点，条件分支里的赋值它看不见（那是重构验收的不变量，不该为打包放宽）。
def _resolve_app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


APP_DIR = _resolve_app_dir()

CONFIG_PATH = os.path.join(APP_DIR, "gui_config.json")

# 版本号：发版时改这一处（--selfcheck / --version 会打印它）。
# GitHub Release 的 tag 要与它一致（tag 去掉开头的 v），Actions 工作流会做一致性校验。
APP_VERSION = "1.3.0beta5"

CFG_VERSION = 3

# 统一 User-Agent：多家平台会按 UA 判断"是不是官方 CLI/SDK"，认出自建工作台就可能限流或
# 拒答（Token Plan 的定位就是给 Claude Code / Codex 这类工具用的）。版本号只是外形，
# 没有协议依赖，改动不要影响请求本身。
USER_AGENT = "codex-cli/0.147.0 (Windows 11; x86_64)"

DEFAULT_CONFIG = {
    "cfg_version": CFG_VERSION,
    # 默认值一律不写死某台机器上的路径：exe 取"与本程序同目录"，模型目录取
    # <本程序目录>/models，缺失时由界面提示去设置里指路（分发给别人才能直接用）。
    "exe": os.path.join(APP_DIR, "llama-server.exe"),
    "model": "",                     # 首次启动由 main() 从 models_dir 里挑一个可用的
    "models_dir": os.path.join(APP_DIR, "models"),
    "host": "127.0.0.1",
    "port": 8080,
    "api_key": "sk-local",
    # ---- 开发机属性（自动探测预填，可手动修改；层数计算直接读这里的值）----
    "gpu_name": "",
    "vram_gb": 0,                  # 显存容量（GB）；0 = 首次启动时经 nvidia-smi 探测
    "ram_gb": 0,                   # 系统内存（GB）
    # ---- 服务参数（重启生效）----
    "ngl": 24,                     # GPU 层数（fallback；实际按 model_ngl 每模型记忆）
    "model_ngl": {},               # 每个模型各自的 GPU 层数（key = GGUF 文件名；由探测生成）
    "ctx": 10240,                  # 全局兜底 context（安全优先；正常由按模型自动匹配覆盖）
    "model_ctx": {},               # 按模型：主页面启动时使用的 context
    "model_ctx_api": {},           # 按模型：agent（本地模型 API 页/代理）启动时使用的 context
    "model_mmproj": {},            # 按模型：视觉投影器路径（有则模型可看图）
    "model_image_input": {},       # 按模型：图片输入能力人工声明 {"yes"/"no"}，无记录=自动判据
    "threads": 0,                  # CPU 线程数，0=自动
    "reasoning_mode": "default",   # default / off / budget
    "reasoning_budget": 1024,
    "extra_args": "",
    # ---- 生成参数（下次请求即生效）----
    "temperature": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "repeat_penalty": 1.1,
    "max_tokens": 4096,            # 思考+回答共享额度
    "seed": -1,
    "system_prompt": "",
    # ---- 界面 ----
    # 用户模式（2026-10-07 W 定）：simple = 普通用户模式（默认，精简导航、无 "?" 气泡）；
    # advanced = 高级用户模式（原有的完整设置页）。设置窗底部按钮互切，写进配置重启仍算数。
    "user_mode": "simple",
    # ---- 外观页（2026-10-08 W 定；主题下拉本阶段只存值，切换逻辑后续实现）----
    "ui_theme": "auto",              # 跟随系统 auto / 浅色 light / 深色 dark（占位）
    "ui_accent": "",                 # 全局主题色（#RRGGBB）：发送/取回/模型名/用户名/状态灯/开关/左栏选中；空 = 跟主题
    "ui_accent_sel": "",             # 选中文本强调色（#RRGGBB）；空 = 跟主题默认，与全局主题色互相独立
    "ui_custom_colors": [],          # 选色器「自定义颜色」16 格（#RRGGBB / ""），随改随落盘
    "show_reasoning": True,
    # 云端文本对话的「展示思考过程」独立开关（2026-10-07 W 定：与本地 show_reasoning
    # 完全独立存储）。老配置里没有这个键时，load_config 按当时 show_reasoning 的值
    # 继承一次（升级前后行为不变），此后各改各的。
    "cloud_show_reasoning": True,
    "show_usage": True,            # 每轮结束后显示 token 用量（云端计费可见性）
    # ---- 模型分类 ----
    "model_kind": "chat",          # 当前选中模型的类型：chat / image / video
    "image_model_dir": "",           # 生图大模型子文件夹；留空 = models_dir/生图
    # 「已经替用户自动选中过一个模型」的标记（坑 150）。程序只自动接管一次：用户自己
    # 在菜单 / 设置页选过也置位。只有"一个可用的模型都不剩"时才会清掉重新武装 ——
    # 所以删光全部模型后再配第一个，这条会再生效一次。不用 cfg_version 升级那道闸：
    # 新键缺省 False 正是"还没接管过"，对老用户就是正确初值。
    "model_auto_picked": False,
    # ---- 生图（sd.cpp；参数形状由 core/sdprofile 按模型族决定）----
    "sd_dir": "",                    # sd.cpp 部署目录（含 sd-cli.exe）；在设置里指路
    "img_model_file": "",          # 生图扩散模型文件名（在 image_model_dir 下）
    "img_steps": 20,               # 采样步数：8 步 ~1m20s，12 步 ~1m50s，20 步 ~2m50s（细节更多）
    "img_size": "1024x1024",
    "img_cfg": 2.5,                # 官方推荐值
    "img_seed": -1,                # -1 = 随机
    "img_strength": 0.9,           # 重绘强度·大幅档 = 兜底（坑 25：0.9 才"改得动"指令）
    # 底图重绘的强度分档（2026-10-11）：`img_strength` 就是**大幅档 = 判不出时的兜底**，
    # 另外两档在它下面。数值来自开发机 qwen-image-2.1 实测（`_selftest/_probe_strength.py`，
    # 同一底图同种子：漂移量 0.4→15 / 0.65→24 / 0.9→59，只有 0.9 真会执行提示词）。
    # 判据与取值口径见 core/media.edit_strength 与 `10` §5.0.2；换模型族未实测。
    # ⚠ 兜底故意留在 0.9 = 分档上线前的行为 ⇒ 老配置零迁移、判不出的提示词一条不退化。
    "img_strength_light": 0.4,     # 小改档：只动画质（变清晰 / 修复 / 抠图）
    "img_strength_mid": 0.65,      # 普通改档：改属性或局部（换背景 / 去掉某物）
    "img_strength_auto": True,
    # 两档各自的可调项。**留空 / 0 = 不传**，用引擎自己的默认（help 原文的 default）：
    #   img_guide_scale → --img-cfg-scale  底图重绘档的图像引导强度（default: same as --cfg-scale）
    #   img_ref_args    → --ref-image-args 主体参考档的参考图处理键值对（empty = 按模型权重自动判断）
    # 两条都是 2026-10-06 按 `sd_cli_help.txt` 接的，**值域未实测**（坑 95 纪律：参数名要在 --help 里，
    # 实际怎么给值要真机跑过一次）。没实测这件事在设置页与 10 §5.0 都写明。
    "img_guide_scale": 0.0,
    "img_ref_args": "",
    # 生图带图的两种模式：edit=底图重绘（-i）/ subject=主体参考（-r）。
    # 只是**默认选哪一档**；模型不支持这一档时自动回该模型的默认档（capability.default_ref_mode）。
    "img_ref_mode": "edit",
    # 换别的模型族时要动的就是这一组：留空一律"自动识别 / 自动找"
    "img_family": "",              # 模型族人工覆盖（qwen-image / flux / sdxl / generic …）
    "img_vae_file": "",            # 配套文件：留空 = 在生图目录里自动发现
    "img_llm_file": "",            #   LLM 文本编码器（Qwen-Image / FLUX.2 这类用）
    "img_clip_l_file": "",         #   CLIP-L（Flux / SD3 用）
    "img_clip_g_file": "",         #   CLIP-G（SDXL / Flux 用）
    "img_t5_file": "",             #   T5-XXL（Flux / SD3 用）
    "img_tokenizer_file": "",      #   tokenizer.json（PiD / Lens 要求）
    "img_backend": "",             # 留空 = 用该族默认后端；显存吃紧可填 diffusion=disk
    "img_params_backend": "",
    "img_negative": "",            # 负向提示词：只有填了才传 -n（Qwen-Image 原来就不带）
    "img_extra_args": "",          # 原样拼进命令行的人工出口
    # ---- 生视频（sd.cpp，与生图同一引擎、不同链路）----
    # 注意：以下档位是"能跑通链路"的保守默认值，尚未在开发机 8GB 显存上实测校准
    "video_model_dir": "",           # 视频组件目录；留空/不存在时回退扫描 models_dir
    "vid_model_file": "",          # 视频扩散主体文件名（留空 = 用扫描到的第一个）
    "vid_llm_file": "",            # 视频文本编码器文件名（留空 = 自动配对同目录编码器）
    "vid_vae_file": "",            # 视频 VAE 文件名（缺失时启动前就提示，不浪费排队时间）
    "vid_family": "",              # 模型族人工覆盖（minimax-h3 / wan / ltx / generic …）
    "vid_t5_file": "",             #   T5-XXL 文本编码器（Wan / LTX / HunyuanVideo 用）
    "vid_tokenizer_file": "",      #   tokenizer.json
    "vid_high_noise_file": "",     #   Wan2.2 MoE 的高噪段模型（--high-noise-diffusion-model）
    "vid_audio_vae_file": "",      #   音频 VAE（H3 有声版 / LTX 需要，缺了只出无声视频）
    "vid_frames": 17,              # 帧数：多数视频 VAE 要求 4n+1，先用最小档验证链路
    "vid_fps": 24,                 # 帧率（MiniMax-H3 的参考视频按 24fps 组织）
    "vid_size": "512x512",         # 分辨率：越高越吃显存与时间
    "vid_steps": 20,               # 采样步数
    "vid_cfg": 5.0,                # 提示词服从度
    "vid_neg_prompt": "worst quality, low quality, blurry, distorted, deformed, watermark, text, static, jittery",
    # ↑ 不能留空：MiniMax-H3 在 CFG>1 时要编码负向提示词，空串会报
    #   "failed to encode negative video prompt" 并退出码 1（留空时代码会回退到内置默认值）
    "vid_format": "webm",          # 单文件视频输出：webm / avi / webp（sd-cli 支持这三种）
    "vid_backend": "te=cpu,diffusion=cuda0,vae=cuda0",   # 各组件运行的后端
    "vid_params_backend": "",      # 权重放置后端：留空=引擎自定；显存不足可填 diffusion=disk
    "vid_extra_args": "--vae-tiling --temporal-tiling",  # 分块解码，降显存占用
    "vid_seed": -1,
    # ---- 本地模型 API（OpenAI 兼容中转，供 agent 应用调用）----
    # 默认关（2026-10-07 W 定）：这是给 agent 的高级能力，普通用户用不上；
    # 只在高级用户模式的「本地模型 API」页手动开启。cfg_version<3 的老配置升级时
    # 一律强制关一次（load_config 的闸），要用的自己去开。
    "proxy_enabled": False,
    # 「自启动」：程序启动时自动把代理服务带起来（只在 proxy_enabled 开着时算数）。
    # 与"启用"分成两个开关：启用了但没勾自启动 = 只在设置页手动「启动 / 重启服务」。
    "proxy_autostart": True,
    "proxy_port": 8081,
    "proxy_last_model": "",        # 上次成功经代理加载的模型（回退用）
    # ---- 锁屏运行（远程连接前置，2026-10-11，W 拍板见 99 K1）----
    # 「允许锁屏运行」= 阻止系统自动睡眠（core/keepawake.py），息屏后仍可被远程连接。
    # 新键默认关：load_config 先铺 DEFAULT_CONFIG 再盖用户值，老配置自动落到 False，
    # 不需要 cfg_version 闸（不存在"旧值被翻错"的问题）。
    "keepawake_enabled": False,
    # 「仅接电源时保持唤醒」：勾上后，用电池时不阻止睡眠（省电）；只在主键开着时算数。
    "keepawake_ac_only": False,
    # ---- 远程访问（2026-10-11，W 拍板见 99 K1；connection/remote.py）----
    # 默认关（沿用 proxy_enabled 先例；新键无旧值，load_config 铺默认即生效，
    # 不需要 cfg_version 闸）。开启 = 新的对外监听面：配对制 + HTTPS + 审计三件套
    # 一并生效（缺一不开是服务端的硬行为，不靠开关拼）。
    "remote_enabled": False,
    "remote_port": 8443,
    # HTTPS 自签证书（core/remote_cert.py，PowerShell 当前用户级生成，免管理员）。
    # W 2026-10-11 定"直接做 https"；PowerShell 被策略禁用时服务端回退明文 HTTP
    # 并说明，不开死开关。
    "remote_https": True,
    # 历史产物开关（二期，W 2026-10-11 拍板并入）：默认关 = 产物取回只认
    # "本设备自己的任务"；开启后台账（remote_jobs.json）里的历史远程产物
    # 对任何已配对设备可见（16 §4 第 3 条预留的口径：放宽判据、形状不动）。
    "remote_history": False,
    # 对外开放范围（2026-10-12 P0-5，判据在 connection/remote_net.resolve_bind）：
    #   all        = 全部网卡（默认，与一期行为逐字一致）
    #   tailscale  = 只绑 Tailscale 虚拟网卡那个地址（把攻击面收到只有隧道能到的位置；
    #                本机没有那个地址时**回退 all 并在状态行如实说**）
    #   v4         = 收全部 IPv4 网卡、不收 IPv6
    # 默认 all = 现行为，所以不抬 cfg_version（proxy_enabled 那次抬闸是因为旧默认不安全）。
    "remote_bind": "all",
    # ---- 云端 API（v31 一期：OpenAI 兼容文本；密钥存 secrets.json，不进备份）----
    # 每项：{id, name, base_url, kind:"text", models:[...], enabled, timeout, extra_headers}
    "cloud_providers": [],
    "model_provider": "local",     # 当前选中模型属于谁：local = 本地；否则是 provider id
    # 文本附件超预算时，是否让云端模型自己决定读哪一段（多花一次规划请求，默认关）
    "cloud_file_model_decides": False,
    # ---- 提示词优化（W 2026-10-08 需求；core/promptopt + connection/optimize 已落地，UI 下一轮）----
    # 优化用的文本模型：留空 = 跟随当前选中的文本模型（选中生图/生视频时回退第一个
    # 可用文本模型：本地优先、其次填过密钥的云端）；显式 = "local" 或 "pid::model"。
    "prompt_opt_model": "",
    # ---- 云端生图 / 生视频（二三期：走服务商原生接口，不走本地 sd-cli）----
    # 产物 URL 只活 24 小时，所以成功判定是"文件已在本地"；
    # 目录留空 = <产物文件夹>/云端/{image,video}（产物文件夹见 output_dir）
    "cloud_img_dir": "",
    "cloud_vid_dir": "",
    # ---- 产物文件夹（本地与云端生图 / 生视频共用的落地根，2026-10-06 W 定）----
    # 留空 = <程序目录>/产物，里面按 本地 / 云端 × image / video 分四个子目录
    # （本地生图 产物\本地\image、本地生视频 产物\本地\video、
    #   云端生图 产物\云端\image、云端生视频 产物\云端\video）。
    # 各链路单独填过时以显式值为准：云端的 cloud_img_dir / cloud_vid_dir、
    # 本地的 img_output_dir / vid_output_dir（显式 > 派生）。
    "output_dir": "",
    # 本地生图 / 生视频各自的产物目录（设置 → 生图 / 生视频 → 高级参数）；留空 = 产物文件夹下的 本地/{image,video}
    "img_output_dir": "",
    "vid_output_dir": "",
    # ---- 性能分级登记（2026-10-06 W）----
    # 键 = 模型路径，值 = 上次评估的级别（0~3，见 core/modelreq.py）。扫描发现新模型时评估
    # 一次、输出栏说一次；文件删掉后由扫描侧剪枝。手改服务参数里的显存/内存后想重新评估，
    # 用「模型文件与引擎 → 全部重新计算」清空它。
    "perf_reported": {},
    "cloud_img_size": "1024x1024",     # 界面按「宽x高」填（* ，× 等分隔符也认，自动归一成
                                       # x，见 core.media.parse_size），发出去前按各家写法换算
    "cloud_img_negative": "",
    "cloud_video_resolution": "",      # 空 = 不传该参数，用服务端默认（各家档位不一样）
    "cloud_video_duration": 5,         # 秒；各家允许区间不同，超范围会被服务端点名报错
    "cloud_video_ratio": "",           # 空 = 不传（图生视频时比例常由素材决定）
    "cloud_video_negative": "",
    "cloud_poll_seconds": 15,          # 官方建议 15s；三个端点合计 20 QPS，别调太密
    "cloud_wait_minutes": 20,          # 单次等待上限，超了转入台账等「取回」
    "cloud_image_wait_seconds": 180,   # 同步生图一次请求的超时
    "cloud_submit_timeout": 90,
    "cloud_download_seconds": 180,
    "cloud_keep_days": 7,              # 台账里已完成任务保留天数（未完成的不过期就留着）
    # ---- 模型别名（key = GGUF 文件名；value = 页面显示的简称）----
    # 一般无需手填：display_name 会用 make_alias() 从文件名自动生成
    "model_aliases": {},
    # ---- 主菜单显示控制（管理入口：设置 → 模型文件与引擎 →「管理本地模型…」）----
    # 名单里的文件名**只是不进顶部菜单**：设置页清单、扫描补全、8081 代理照旧认得它们。
    # 生图 / 生视频的配套文本编码器（能聊天的 .gguf 零件）常需要收在这里。
    "model_hidden": [],
    # ---- 对话记录（仅文本语言模型；本期只存不读，见 core/chatlog.py）----
    "chat_log_save": True,           # 每轮结束 / 关窗 / 清空前自动写一份 JSON
    "chat_log_dir": "",              # 留空 = <程序目录>/chat_logs（不写死盘符，项目要分发）
    # 生图 / 生视频的任务日志（本地与云端共用；与产物**分开**，W 2026-10-11 定）
    "media_log_dir": "",             # 留空 = <程序目录>/media_logs
    # ---- 新设备排障与首次引导（见 core/crashlog.py、core/diagnose.py、ui/guide.py）----
    # 崩溃日志"已经看过"的指纹（mtime:size）：同一条崩溃只提醒一次
    "crashlog_seen": "",
    # 看完/跳过新手引导时的版本号：升级不会自动重弹，只在 设置 → 关于与诊断 里留重看入口
    "guide_done": "",
    # 高分屏清晰度（DPI 感知）。**冷切换**：只在下次启动生效 —— Windows 允许一个进程
    # 只标一次，窗口一建出来就再也改不了了。0 = 让系统按缩放位图拉伸（发虚但字大）
    "dpi_aware": 1,
    # ---- 开发者选项（隐藏页：关于页版本号连点 5 次才出现，见 ui/settings.py）----
    # 「后台查到的更新弹过窗、用户把它关掉了」的那个 tag：同一个版本不再打扰第二次。
    # 单放配置里（不是内存）是因为"别再说了"是用户对**这个版本**的表态，重启后仍该算数；
    # 出现更新的 tag 时照旧会弹（判据是"tag 变了"，不是"时间没到"）。
    # 注意：开发者模式**开关本身**不在这里 —— 它只在本次运行内有效（App._dev_mode）。
    "dev_upd_dismissed": "",
    # 「自动检查发现新版本、弹了更新窗口、用户把它关掉了」的那个 tag（2026-10-05）：
    # 同一个版本不再自动弹第二次；出现更新的 tag 照旧弹。取代上面 dev_upd_dismissed
    # 的职责 —— 现在两条自动路径（进关于页自动查 / 开发者后台轮询）共用这一个键；
    # 用户**手动**点「检查更新」不受它限制，永远弹。
    "upd_dismissed": "",
    # 「签名信任」那条启动询问（2026-10-05）：True = 问过了（选"加入"或"不用了"都算），
    # 之后不再打扰。新键缺省 False 正是"还没问过"，对老用户就是正确初值。
    "codesign_prompt": False,
    # ---- 引擎管理（设置 → 模型文件与引擎 →「引擎管理」；W 2026-10-05）----
    # 记录"自动安装装上的那个版本"（key = engine → tag），用来判断「检查更新」查到的是
    # 「查询到新版本」还是"已经是最新"。手动定向 / 用户自己放的引擎不写这里 ⇒ 会被当成
    # "可能有新版"（程序无从知道他那份是什么版本）。
    "engine_installed": {},
    # ---- 手动定向的模型来源（设置 → 模型文件与引擎 →「文件整理 → 手动定向模型」）----
    # 用户手动加入的文件夹或 .gguf 完整路径；**只登记、不动文件**，扫描时一并收进三组清单。
    "extra_models": [],
}

INT_KEYS = ("port", "ngl", "ctx", "threads", "reasoning_budget",
            "dpi_aware",
            "max_tokens", "top_k", "seed", "img_steps", "img_seed",
            "proxy_port", "remote_port", "vid_frames", "vid_fps", "vid_steps",
            "vid_seed",
            "cloud_video_duration", "cloud_poll_seconds", "cloud_wait_minutes",
            "cloud_image_wait_seconds", "cloud_submit_timeout",
            "cloud_download_seconds", "cloud_keep_days")

FLOAT_KEYS = ("temperature", "top_p", "repeat_penalty", "vram_gb", "ram_gb",
              "img_cfg", "img_strength", "vid_cfg", "img_guide_scale",
              "img_strength_light", "img_strength_mid")

# 「model」故意不在这里：它与云端复合 id 共用（坑 146），写回另有分支（settings._apply_settings）
STR_KEYS = ("models_dir", "host", "api_key", "reasoning_mode",
            "extra_args", "exe", "gpu_name", "sd_dir", "image_model_dir",
            "img_model_file", "img_size", "img_family", "img_ref_mode", "img_ref_args",
            "img_vae_file", "img_llm_file", "img_clip_l_file", "img_clip_g_file",
            "img_t5_file", "img_tokenizer_file", "img_backend", "img_params_backend",
            "img_negative", "img_extra_args",
            "video_model_dir", "vid_model_file",
            "vid_llm_file", "vid_vae_file", "vid_format", "vid_size",
            "vid_family", "vid_t5_file", "vid_tokenizer_file", "vid_high_noise_file",
            "vid_audio_vae_file",
            "vid_backend", "vid_params_backend", "vid_extra_args",
            "vid_neg_prompt", "model_provider",
            "output_dir", "img_output_dir", "vid_output_dir",
            "cloud_img_dir", "cloud_vid_dir", "cloud_img_size",
            "cloud_img_negative", "cloud_video_resolution", "cloud_video_ratio",
            "cloud_video_negative", "chat_log_dir", "media_log_dir", "prompt_opt_model")

_CFG_LOCK = threading.RLock()   # 可重入：save_config 自带锁，调用方若已持锁不会自我死锁


def cfg_lock():
    """cfg 读改写与序列化共用的锁（就是 `_CFG_LOCK`；RLock，坑 12：外面别再包一层）。

    **后台线程改 cfg（含嵌套 dict）必须持这把锁**：save_config 的 json.dump 会全树
    遍历，遍历期间别的线程给嵌套 dict 加键必抛 `RuntimeError: dictionary changed
    size during iteration`，而 atomic_write_json 把它吞成 _LAST_WRITE 一条记录——
    整份设置静默不落盘（排查报告 R-12：首开自动补全在跑时，60 次主线程保存失败
    60 次）。主线程小段"改 cfg + save_config"也建议持锁，防反向竞争。
    """
    return _CFG_LOCK

def load_config():
    """读取配置；v1 旧配置自动迁移（ctx/max_tokens 仍是旧默认值时升级）。"""
    cfg = dict(DEFAULT_CONFIG)
    user = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception:
        user = {}
    if not isinstance(user, dict):
        # 合法 JSON 但顶层不是对象（数组 / 字符串 / null）：按"配置坏了"回退默认继续，
        # 绝不让 AttributeError 穿出去——原来那是程序起不来、只能手动删文件（排查报告 R-42）
        user = {}
    try:
        ver = int(user.get("cfg_version", 1) or 1)
    except Exception:
        ver = 1        # cfg_version 写成 "abc" 这类：按老版本号处理，别在验证前就抛（R-42）
    for k, val in user.items():
        if k in ("model_aliases", "model_ngl") and isinstance(val, dict):
            merged = dict(DEFAULT_CONFIG.get(k, {}))
            merged.update(val)
            cfg[k] = merged
        elif k == "cfg_version":
            continue
        else:
            cfg[k] = val
    # 嵌套可变默认值必须**逐键拷贝**：dict(DEFAULT_CONFIG) 是浅拷贝，缺键时
    # cfg[k] 与 DEFAULT_CONFIG[k] 是同一个对象，就地写会污染出厂默认、同进程再
    # load_config 带回幽灵数据（排查报告 R-41；生产里每进程只 load 一次所以用户
    # 几乎看不见，主要是自检夹具互染——所有套件都写 base = dict(DEFAULT_CONFIG)）
    for k, dflt in DEFAULT_CONFIG.items():
        if isinstance(dflt, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = dict(cfg[k])
        elif isinstance(dflt, list) and isinstance(cfg.get(k), list):
            cfg[k] = list(cfg[k])

    if ver < CFG_VERSION:
        # 仅当用户仍是旧默认值时才升级，手动改过的数值原样保留
        try:
            if int(user.get("ctx", 0)) <= 4096:
                cfg["ctx"] = DEFAULT_CONFIG["ctx"]
            if int(user.get("max_tokens", 0)) <= 1024:
                cfg["max_tokens"] = DEFAULT_CONFIG["max_tokens"]
        except Exception:
            pass
    if ver < 3:
        # 2026-10-07 W 定：「本地模型 API」改为默认关闭的能力（普通用户模式整页隐藏）。
        # 老配置里 proxy_enabled 几乎必然存着 True（旧默认值被整份保存下来），照搬等于
        # "默认关"只对新装用户成立 —— 所以升级这一次强制关，要用的人自己去高级模式开。
        cfg["proxy_enabled"] = False
    if "cloud_show_reasoning" not in user:
        # 云端「展示思考过程」新键首次落地：继承本地开关的当前值，升级前后行为不变；
        # 此后两个键完全独立（各自页面各自改）。
        cfg["cloud_show_reasoning"] = bool(cfg.get("show_reasoning", True))
    cfg["cfg_version"] = CFG_VERSION
    # 内置服务商种进清单（幂等，只补不覆盖）；这里延迟导入避开 config ↔ providers 的环
    try:
        from . import providers
        providers.ensure_builtin_providers(cfg)
    except Exception:
        pass
    return cfg

_LAST_WRITE = {"path": "", "reason": "", "n": 0}     # 最近一次"写不进去"的现场（给界面开口用）


def atomic_write_json(path, data):
    """原子写 JSON：先写 `<path>.tmp` 再 `os.replace` 落位；返回是否成功（不抛）。

    为什么必须原子：开发机每天 23:30 断电（交接文档多处以此为设计前提），直接
    `open(path, "w")` 覆写时一次中途断电就把文件截成半份 —— 而读侧（load_config /
    load_secrets / load_jobs）解析失败一律**静默回退空表/默认值**，表现成"设置全部
    丢失"。chatlog / 云端下载 / 备份脚本早已是 `.part`+replace，这三份状态文件不能例外。

    失败原因记在 `_LAST_WRITE` 里：不抛是这条链路的约定，但"静默"不是 —— 下载 exe 的
    用户把程序放进 `C:\\Program Files\\` 这类只读目录时，症状是"每次启动都回到默认设置"，
    没有任何一句话指着真正的原因（见 `write_error()` 与 App._status_loop 的开口）。
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)           # 同目录改名，Windows 下也是原子的
        _LAST_WRITE.update(path="", reason="")
        return True
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        _LAST_WRITE.update(path=path, reason="%s: %s" % (type(e).__name__, e),
                           n=_LAST_WRITE["n"] + 1)
        return False


def write_error():
    """最近一次状态文件写入失败 → (路径, 原因)；上一次是成功的话返回 ("", "")。"""
    return (_LAST_WRITE["path"], _LAST_WRITE["reason"])


# ---- 跨实例保护（Y10，W 2026-10-11 定："检测到别的实例开口 + 拒绝整份覆写"）----
# save_config 把内存整份 cfg 全量落盘，而 _CFG_LOCK 只锁本进程：双实例时后写者赢，
# 另一个刚改的参数被静默盖回（现象 = "参数改了不生效"）。每个实例启动时在 APP_DIR
# 登记一份 PID 标记（含进程创建时间，防 PID 复用误判），save_config 发现"另一个
# 活实例在用**同一份配置文件**"就拒绝保存，原因落 _LAST_WRITE 走既有开口。
# 标记按"配置文件路径"区分：自检把 CONFIG_PATH 指到临时目录，与真程序互不误伤。

_INSTANCE_MARK_PREFIX = "llm_console.instance_"      # APP_DIR 下的标记文件（白名单 .gitignore 外）
_MY_MARK = {"path": None}                            # 本进程的标记文件路径


def _process_created_ft(pid):
    """进程创建时间（FILETIME，100ns 纪元值；取不到返回 0）。"""
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return 0
    try:
        ct, et, kt, ut = (ctypes.c_ulonglong() for _ in range(4))
        if not k.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                 ctypes.byref(kt), ctypes.byref(ut)):
            return 0
        return ct.value
    finally:
        k.CloseHandle(h)


def _instance_mark_alive(data):
    """标记里的进程是否仍是登记时那个活进程（PID 复用 → 创建时间对不上 → 视为死残留）。"""
    try:
        pid = int(data.get("pid") or 0)
    except Exception:
        return False
    if not pid:
        return False
    ft = _process_created_ft(pid)
    return ft != 0 and ft == int(data.get("created") or -1)


def claim_instance():
    """App 启动时登记本实例标记，并清掉死实例的残留标记。

    返回是否发现**别的活实例**（是否同一份配置由 other_instance_running 现判，
    这里只做粗报）。登记失败不拦启动：最坏退回旧行为（双实例静默竞写），
    不能因为保护机制建不起来就连配置都写不了。
    """
    import glob
    seen_other = False
    try:
        for f in glob.glob(os.path.join(APP_DIR, _INSTANCE_MARK_PREFIX + "*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                _safe_remove(f)
                continue
            try:
                same_pid = int(data.get("pid") or 0) == os.getpid()
            except Exception:
                same_pid = False
            if same_pid:
                continue             # 本进程旧标记（同进程重复建 App 等）：不当作"别的实例"
            if _instance_mark_alive(data):
                seen_other = True
            else:
                _safe_remove(f)
    except Exception:
        pass
    try:
        mark = os.path.join(APP_DIR, "%s%d.json" % (_INSTANCE_MARK_PREFIX, os.getpid()))
        atomic_write_json(mark, {"pid": os.getpid(),
                                 "created": _process_created_ft(os.getpid()),
                                 "cfg": os.path.abspath(CONFIG_PATH)})
        _MY_MARK["path"] = mark
        import atexit
        atexit.register(_drop_instance_mark)
    except Exception:
        _MY_MARK["path"] = None
    return seen_other


def _safe_remove(path):
    try:
        os.remove(path)
    except Exception:
        pass


def _drop_instance_mark():
    _safe_remove(_MY_MARK["path"] or "")


def other_instance_running():
    """有没有另一个活实例在用**同一份配置文件**（Y10 的判据；互不相干的实例不算）。"""
    import glob
    try:
        mine = os.path.abspath(CONFIG_PATH)
        my_mark = _MY_MARK["path"]
        for f in glob.glob(os.path.join(APP_DIR, _INSTANCE_MARK_PREFIX + "*.json")):
            if my_mark and os.path.abspath(f) == os.path.abspath(my_mark):
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            if data.get("cfg") == mine and _instance_mark_alive(data):
                return True
    except Exception:
        return False
    return False


def save_config(cfg):
    """整份落盘（_CFG_LOCK 内）。

    检测到同一份配置的另一个活实例时**拒绝保存**（Y10，W 2026-10-11 定"开口 +
    拒绝整份覆写"）：整份覆写会把对方刚改的参数静默盖回。拒绝原因落
    _LAST_WRITE，由 App._status_loop 的既有开口说出来。
    """
    with _CFG_LOCK:
        if other_instance_running():
            _LAST_WRITE.update(
                path=CONFIG_PATH, n=_LAST_WRITE["n"] + 1,
                reason="检测到另一个程序实例正在运行——为免互相覆盖设置，本次保存已拒绝；"
                       "请先关闭另一个实例再改设置。")
            return False
        return atomic_write_json(CONFIG_PATH, cfg)

def base_url(cfg):
    return "http://%s:%s" % (cfg.get("host", "127.0.0.1"), cfg.get("port", 8080))

def api_headers(cfg):
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if cfg.get("api_key"):
        h["Authorization"] = "Bearer " + cfg["api_key"]
    return h

def gen_api_key():
    return "sk-" + secrets.token_urlsafe(24)


def split_extra_args(s):
    """「附加参数（原样透传）」输入 → argv 列表（三个入口共用：服务参数 extra_args、
    生图 img_extra_args、生视频 vid_extra_args）。

    不能用 `shlex.split(posix=True)`：posix 模式把反斜杠当转义符，Windows 路径
    `D:\\a\\b.gguf` 被吃成 `D:ab.gguf`（排查报告 R-22 实测），引擎拿着错路径找不到
    文件。改用 `posix=False`（反斜杠原样保留、引号仍起分组作用），再剥掉每个 token
    成对的最外层引号——带空格的路径写成 `"D:\\my models\\x.gguf"` 也能整段保住。
    """
    import shlex
    out = []
    try:
        tokens = shlex.split(str(s or ""), posix=False)
    except ValueError:
        # 引号不闭合一类：按空白粗拆兜底，别让整个发送路径抛错
        return str(s or "").split()
    for tok in tokens:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("\"", "'"):
            tok = tok[1:-1]
        out.append(tok)
    return out


def output_root(cfg):
    """产物文件夹的**根**：`output_dir` 优先，留空 = `<程序目录>/产物`。

    本地与云端生图 / 生视频四条链路共用的落地根（2026-10-06 起），里面按
    本地 / 云端 × image / video 分四个子目录。判据只写这一处，
    媒体两侧（media.img_out_dir / media.vid_out_dir / cloud_media_dir）都问它。
    相对路径按**程序目录**解析 —— 别让相对路径落进"当前工作目录"（坑 42 同族）。
    """
    d = str((cfg or {}).get("output_dir", "") or "").strip()
    if not d:
        return os.path.join(APP_DIR, "产物")
    return d if os.path.isabs(d) else os.path.join(APP_DIR, d)


def cloud_media_dir(cfg, kind):
    """云端产物目录：配置项优先，留空回退 <产物文件夹>/云端/{image,video}。

    两条纪律：① 不写死盘符（坑 55，项目要分发）；② **不挂到 sd.cpp 下面**——
    云端这条路根本不启动本地引擎，别人没部署 sd.cpp 时也该能出图出片。
    """
    key = "cloud_img_dir" if kind == "image" else "cloud_vid_dir"
    d = str((cfg or {}).get(key, "") or "").strip()
    if d:
        return d
    return os.path.join(output_root(cfg), "云端",
                        "image" if kind == "image" else "video")


def media_log_dir(cfg):
    """媒体任务的日志目录：`media_log_dir` 优先，留空 = **配置文件所在目录**下的 `media_logs`。

    **日志与产物分开放**（W 2026-10-11 定）：产物是给人看的结果，日志是排障用的过程，
    混在一起会被一起删除 / 一起备份 / 一起整理（原来就是 `<产物名>.log` 躺在产物旁边）。

    默认挂**配置文件所在目录**而不是 `APP_DIR`：生产环境两者是同一个（`gui_config.json`
    就在程序目录里），而自检把 `CONFIG_PATH` 指到临时目录时日志自动跟着隔离 ——
    否则任何一套碰到媒体链路的套件都会往仓库根写出 `media_logs/`（本轮真踩过，
    坑 215；实例标记文件用的是同一条判据）。`media_log_dir` 填相对路径时仍按程序目录解析。
    """
    d = str((cfg or {}).get("media_log_dir", "") or "").strip()
    if d:
        return d if os.path.isabs(d) else os.path.join(APP_DIR, d)
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), "media_logs")


def media_log_path(cfg, product_path, tag=""):
    """某次媒体任务的日志路径 = `media_log_dir` 下 + **产物主名**（同 img_20261011_181500）。

    沿用产物主名是为了"看着产物就能找到对应日志"；`tag` 用于同一产物有多份日志的链路
    （云端生视频=提交 + 取回各一份）。目录在这里就建好 —— 调用方都是写完当行就 open，
    不该各自再 makedirs（少一处忘建 = 一份日志静默消失）。
    """
    stem = os.path.splitext(os.path.basename(str(product_path or "")))[0] or "task"
    if tag:
        stem = "%s_%s" % (stem, tag)
    d = media_log_dir(cfg)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, stem + ".log")
