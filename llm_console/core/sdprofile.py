# -*- coding: utf-8 -*-
"""llm_console.core.sdprofile — sd.cpp 的"模型族适配层"。

**为什么需要这一层**：生图与生视频原来只认开发机那一对模型 —— `build_img_cmd` 把
Qwen-Image 的参数组合（`--llm` + `--flow-shift 3` + `euler` + `te=cpu`）硬写进命令行，
视频组件只认 MiniMax-H3 的张量名，`is_image_diffusion()` 还把"文本编码器"硬编码成
文件名里带 `qwen3vl`。换个模型（Flux / SDXL / SD3 / Wan / LTX / MiniMax 的其它变体）
就会传错槽位、漏编码器、或根本进不了模型列表。

这一层把三件事抽成表：**需要哪些配套文件（槽位）**、**该传哪些参数**、**能不能吃参考图**。
判据按可信度分三层，越靠前越硬：

  1. 人工覆盖（设置 → 生图 / 生视频 的「模型族」下拉，存进 `img_family` / `vid_family`）
  2. **开发机实测过的张量名标记**（只有 Qwen-Image 与 MiniMax-H3 属于这层 —— 其余家族的
     张量名没法在开发机验证，不敢拿来自动判定，见坑 52"文档写了也要看是谁写的"）
  3. 文件名线索（Flux / SDXL / Wan 这些是**上游文档与 sd-cli --help 明确写过的形状**，
     线索命中只用来选槽位与默认档位，缺件时预检会点名要什么）
  4. 都没有 → `generic`：只按通用规则把扫到的文件交出去，不加任何家族专属参数。

`generic` 不是"降级凑合"：sd.cpp 自己会按张量名认架构，所以"只喂文件、少给参数"往往
就能跑；真正需要人工指路的是配套文件放错目录、名字不规范这类情况 —— 那由
「配套文件」那几个可手填的字段和「附加参数」原样透传给 sd-cli 解决。

分层规矩：本模块属于 core/，**不得 import tkinter**（坑 36）。
"""

import os
import re

# 扩散权重支持的扩展名（sd-cli 的 --diffusion-model 收 gguf/safetensors/sft/ckpt）
DIFFUSION_EXTS = (".gguf", ".safetensors", ".sft", ".ckpt", ".pt", ".bin")
COMPANION_EXTS = (".gguf", ".safetensors", ".sft", ".ckpt", ".pt", ".bin", ".json")

AUTO = "auto"                     # 配置里表示"没人工指定，走自动识别"

# ---------------------------------------------------------------- 槽位定义
# slot -> (引擎参数, 中文名, 文件名线索（小写子串）, 是否也算"扩散主体"候选)
SLOTS = {
    "vae":          ("--vae", "图像 / 视频 VAE", ("vae", "ae")),
    "audio_vae":    ("--audio-vae", "音频 VAE", ("audio_vae", "audio-vae", "audiovae")),
    "audio_encoder": ("--audio-encoder", "音频编码器（wav2vec2）", ("wav2vec", "audio_encoder")),
    "clip_l":       ("--clip_l", "CLIP-L 文本编码器", ("clip_l", "clip-l", "cliptext_l")),
    "clip_g":       ("--clip_g", "CLIP-G 文本编码器", ("clip_g", "clip-g")),
    "clip_vision":  ("--clip_vision", "CLIP-Vision（IP-Adapter 用）", ("clip_vision", "clipvision")),
    "t5xxl":        ("--t5xxl", "T5-XXL 文本编码器", ("t5xxl", "t5-xxl", "t5_")),
    "llm":          ("--llm", "LLM 文本编码器", ("qwenvl", "qwen3vl", "mistral", "gemma", "llm")),
    "llm_vision":   ("--llm_vision", "LLM 视觉投影器（mmproj）", ("mmproj",)),
    "tokenizer":    ("--tokenizer", "tokenizer.json", ("tokenizer",)),
    "connectors":   ("--embeddings-connectors", "embeddings connectors（LTX）", ("connector",)),
    "motion":       ("--motion-module", "AnimateDiff 运动模块", ("motion", "animatediff", "mm_sd")),
    "taesd":        ("--taesd", "Tiny AutoDecoder（快但糊）", ("taesd", "tae_")),
}

# ---------------------------------------------------------------- 家族表
# markers：只在**开发机实测过**的家族上填，用于张量名判定。
# names：文件名线索（小写子串，命中即候选）。
# require：缺了就不能开工的槽位（预检会点名）。optional：找到就带上。
# main_flag：主体文件用哪个参数交给引擎 —— 单文件 ckpt/safetensors 走 -m，
#            裸扩散权重走 --diffusion-model（MiniMax-H3 / Qwen-Image 都是后者）。
QWEN_MARK = (b"transformer_blocks.0.attn.to_q", b"time_text_embed.timestep_embedder",
             b"img_in.weight")
H3_MARK = (b"adaln_t_table", b"audio_patch_proj.weight", b"blocks.0.attn.qkv_proj")
H3_ENC = (b"visual.blocks", b"model.embed_tokens")

FAMILIES = {
    # ---------------- 开发机实测过（argv 必须与调优前逐字一致）----------------
    "qwen-image": {
        "label": "Qwen-Image",
        "kind": "image",
        "markers": QWEN_MARK,
        "names": ("qwen_image", "qwen-image", "qwenimage"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "llm"),
        "optional": ("llm_vision", "taesd"),
        "sampler": "euler",
        "flow_shift": "3",
        "edit_cfg_min": 3.0,        # 实测：编辑场景 CFG 低于 3.0 时提示词影响力不足
        "backend": "te=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 8,
        "edit": "init",              # 附图 = -i 底图（配 --llm_vision 才懂"换背景"这类指令）
        "edit_needs": ("llm_vision",),
        "cfg_hint": "2.5（官方推荐；编辑时提到 3.0）",
        "steps_hint": "20（8 步最快）",
    },
    "minimax-h3": {
        "label": "MiniMax-H3",
        "kind": "video",
        "markers": H3_MARK,
        "enc_markers": H3_ENC,
        "names": ("minimax", "_h3", "fl2va", "ref2va", "flf2v"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "llm"),
        "optional": ("audio_vae",),
        "sampler": None,             # 引擎自己定，别硬塞（实测就是不给也能跑通的那条链路）
        "flow_shift": None,
        "backend": "te=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 8,
        "edit": "init",              # fl2va 的首帧走 -i；Ref2VA 变体才是 -r（见坑 42）
        "neg_prompt_required": True,
        "tiling": ("--vae-tiling", "--temporal-tiling"),
        "steps_hint": "20",
        "cfg_hint": "5.0",
    },
    # ---------------- 上游文档 / sd-cli --help 明确的形状（未在开发机实测）----------------
    "flux": {
        "label": "FLUX.1",
        "kind": "image",
        "markers": (b"double_blocks.0.img_attn", b"single_blocks."),   # 未开发机验证，只作辅助
        "names": ("flux1", "flux-dev", "flux-schnell", "flux1-dev", "flux1-schnell", "flux.1"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "clip_l", "t5xxl"),
        # 故意不把 llm 列为 optional：FLUX.1 的文本编码器就是 clip_l+clip_g+t5xxl，
        # 目录里通常还躺着一个 Qwen3VL/聊天 GGUF（开发机就有），"顺手带上 --llm"会把
        # 引擎引到另一套编码路径上。要用 LLM 编码器的是 FLUX.2 —— 那是另一个家族。
        "optional": ("clip_g", "taesd"),
        "sampler": "euler",
        "flow_shift": None,
        "backend": "clip=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 16,
        "edit": "ref",               # FLUX.1-Kontext 用 -r/--ref-image
        "cfg_hint": "1.0（dev/schnell 都用 1.0；schnell 4 步）",
        "steps_hint": "dev 20~50 / schnell 4",
        "docs": "上游 docs/flux.md 的示例命令行",
    },
    "flux2": {
        "label": "FLUX.2",
        "kind": "image",
        "markers": (),
        "names": ("flux2", "flux-2", "flux2-dev"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "llm"),
        "optional": ("clip_l", "t5xxl"),
        "sampler": "euler",
        "flow_shift": None,
        "backend": "te=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 16,
        "edit": "ref",
        "cfg_hint": "见发行页（--help 说 flux2 的文本编码器是 mistral-small3.2）",
        "steps_hint": "8~50",
        "docs": "sd-cli --help 的 --llm 说明与 --scheduler 列表里的 flux2",
    },
    "sd3": {
        "label": "SD3 / 3.5",
        "kind": "image",
        "markers": (b"joint_blocks.0.attn", b"mmdit_xformer"),
        "names": ("sd3", "sd-3", "stable-diffusion-3"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "clip_l", "t5xxl"),
        "optional": ("clip_g", "taesd"),
        "sampler": None,             # --help：Flux/SD3/Wan 默认就是 euler
        "flow_shift": None,          # --flow-shift 默认 auto
        "backend": "clip=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 16,
        "edit": "init",
        "cfg_hint": "4.5~7（SD3.5 medium 常给 4.5，可试 --slg-scale 2.5）",
        "steps_hint": "20~50",
    },
    "sdxl": {
        "label": "SDXL",
        "kind": "image",
        "markers": (b"label_emb", b"input_blocks.0.0.weight"),
        "names": ("sdxl", "sd_xl", "xl_base", "sd-xl", "juggernaut", "animagine"),
        "main_flag": "-m",
        "require": ("diffusion",),
        "optional": ("vae", "clip_l", "clip_g", "clip_vision", "taesd"),
        "sampler": None,
        "flow_shift": None,
        "backend": "clip=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 8,
        "edit": "init",
        "cfg_hint": "5~8",
        "steps_hint": "20~30",
        "docs": "上游 docs/sd.md：单文件用 -m，拆开的才补 --vae/--clip*",
    },
    "sd15": {
        "label": "SD 1.5 / 2.x",
        "kind": "image",
        "markers": (b"time_embedder", b"input_blocks.0.0.weight"),
        "names": ("v1-5", "v1.5", "sd-v1", "sd15", "dreamshaper", "realistic", "nitrosd"),
        "main_flag": "-m",
        "require": ("diffusion",),
        "optional": ("vae", "clip_l", "clip_g", "motion", "taesd"),
        "sampler": None,
        "flow_shift": None,
        "backend": "clip=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 8,
        "edit": "init",
        "cfg_hint": "6~8",
        "steps_hint": "20~30",
        "docs": "上游 docs/sd.md 的 -m 用法；--help：--motion-module 在 SD1.5 上开视频",
    },
    "wan": {
        "label": "Wan 2.1 / 2.2",
        "kind": "video",
        "markers": (b"patch_embedding", b"text_embedding", b"time_projection"),
        "names": ("wan", "wan2.1", "wan2.2", "ti2v", "t2v", "i2v", "vace"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "t5xxl"),
        "optional": ("tokenizer", "high_noise", "audio_vae", "audio_encoder"),
        "sampler": "euler",
        "flow_shift": None,          # 文档建议 3.0，但默认 auto 已按模型取；不硬塞
        "backend": "clip=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 16,
        "edit": "init",
        "neg_prompt_required": False,
        "tiling": ("--vae-tiling", "--temporal-tiling"),
        "cfg_hint": "3~6",
        "steps_hint": "20~40（官方基线 832x480）",
        "docs": "上游 docs/wan.md（-M vid_gen + --diffusion-model + --vae + --t5xxl；"
                "2.2 MoE 要 --high-noise-diffusion-model，5B 版不用）",
    },
    "ltx": {
        "label": "LTX-Video",
        "kind": "video",
        "markers": (b"media_args", b"transformer_blocks"),
        "names": ("ltx", "ltxv", "ltx-2"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "t5xxl"),
        "optional": ("connectors", "audio_vae"),
        "sampler": None,
        "flow_shift": None,
        "backend": "clip=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 32,
        "edit": "init",
        "tiling": ("--vae-tiling", "--temporal-tiling"),
        "docs": "sd-cli --help 的 --embeddings-connectors / --audio-vae（LTXAV）与 "
                "--scheduler 列表里的 ltx2",
    },
    "hunyuan-video": {
        "label": "HunyuanVideo",
        "kind": "video",
        "markers": (),
        "names": ("hunyuanvideo", "hunyuan-video", "hunyuan1.5"),
        "main_flag": "--diffusion-model",
        "require": ("diffusion", "vae", "clip_l", "t5xxl"),
        "optional": ("tokenizer",),
        "sampler": None,
        "flow_shift": None,
        "backend": "clip=cpu,diffusion=cuda0,vae=cuda0",
        "size_multiple": 16,
        "edit": "init",
        "tiling": ("--vae-tiling", "--temporal-tiling"),
        "docs": "上游 README 的 Video Models 列表",
    },
    "generic": {
        "label": "通用（只喂文件）",
        "kind": "image",
        "markers": (),
        "names": (),
        "main_flag": "--diffusion-model",
        "require": ("diffusion",),
        "optional": ("vae", "llm", "clip_l", "clip_g", "t5xxl", "clip_vision",
                     "llm_vision", "tokenizer", "connectors", "audio_vae", "audio_encoder",
                     "motion", "taesd"),
        "sampler": None,
        "flow_shift": None,
        "backend": "",
        "size_multiple": 8,
        "edit": "init",
        "cfg_hint": "以模型发行页为准",
        "steps_hint": "20",
    },
}

# 下拉里给人挑的顺序（自动 + 常用在前）
FAMILY_ORDER = ("qwen-image", "minimax-h3", "flux", "flux2", "sd3", "sdxl", "sd15",
                "wan", "ltx", "hunyuan-video", "generic")

# 视频槽位里的"第二个扩散模型"：不在 SLOTS 里（它是主体的一部分，不是编码器）
HIGH_NOISE_FLAG = "--high-noise-diffusion-model"
HIGH_NOISE_HINT = ("high_noise", "high-noise", "highnoise", "_high")

IMAGE_ONLY = tuple(f for f, p in FAMILIES.items() if p.get("kind") == "image")
VIDEO_ONLY = tuple(f for f, p in FAMILIES.items() if p.get("kind") == "video")

# 这些家族是 DiT/Transformer 主干，`--diffusion-fa`（扩散侧 flash attention）有收益；
# SD1.5/SDXL 这类 UNet 不硬开（开发机没实测过，交给引擎默认）。
# Qwen-Image 必须在这张表里：它的 argv 是开发机实测校准的，原来就带着 --diffusion-fa。
FA_FAMILIES = {"qwen-image", "flux", "flux2", "sd3", "wan", "ltx", "hunyuan-video"}


def profile(fid):
    """家族定义；认不出来一律落到 generic（绝不因为"没登记"就拒绝跑）。"""
    p = FAMILIES.get(str(fid or "").strip(), FAMILIES["generic"])
    if "fa" not in p:
        p = dict(p)
        p["fa"] = str(fid) in FA_FAMILIES
    return p


def label_of(fid):
    return profile(fid).get("label", fid)


def family_choices(kind=None):
    """设置页「模型族」下拉：(存的代码, 显示的中文标签)。第一项永远是"自动判断"。"""
    out = [(AUTO, "自动判断（按张量名与文件名识别）")]
    for fid in FAMILY_ORDER:
        p = FAMILIES[fid]
        if kind and p.get("kind") != kind:
            continue
        out.append((fid, p["label"]))
    return out


# ---------------------------------------------------------------- 识别
_NAME_SPLIT = re.compile(r"[^a-z0-9]+")


def _name_hit(name, hints):
    """文件名线索：把名字按非字母数字切成段再比。

    短线索（≤3 个字符，如 `ae`、`t5`）**必须整段相等**，否则 "aerospace" 会被当成
    Flux 的 VAE；长线索（`clip_l`、`qwen-image`）允许子串命中。
    比对前把线索与名字都归一（`clip_l` 与 `clip-l` 视为同一个词）。
    """
    s = _NAME_SPLIT.sub("-", str(name or "").lower())
    toks = set(s.split("-"))
    for h in hints:
        hn = _NAME_SPLIT.sub("-", str(h).strip("-").lower())
        if not hn:
            continue
        if len(hn) <= 3:
            if hn in toks:
                return True
        elif hn in s:
            return True
    return False


# 一眼就不是"模型本体"的配套文件名（SDXL/Flux 的权重常是单个 .safetensors，
# 而 VAE / 编码器 / LoRA 也都是 .safetensors，只能靠名字把它们摘出去）。
# 注意 `t5` 只有 2 个字符，走"整段相等"以免误伤名字里含 t5 的模型，所以
# `t5xxl_fp16.safetensors` 这种要把 `t5xxl` / `umt5` 单独列出来才拦得住。
COMPANION_PATTERNS = ("vae", "ae", "clip", "t5", "t5xxl", "umt5", "tae", "taesd",
                      "tokenizer", "lora", "locon", "lycoris",
                      "motion", "animatediff", "mm_sd", "audio", "connector", "controlnet",
                      "control_net", "ip_adapter", "ipadapter", "pulid", "photomaker",
                      "esrgan", "upscal", "yolov", "embeddings", "unet_encoder")


def looks_like_companion(name):
    """该文件名像不像"配套组件"而不是可选择的模型本体。"""
    return _name_hit(name, COMPANION_PATTERNS) or str(name).lower().startswith("mmproj")


def is_model_file(name):
    """可当作扩散主体的文件：扩展名对、且不是配套组件。"""
    low = str(name or "").lower()
    if not low.endswith(DIFFUSION_EXTS):
        return False
    return not looks_like_companion(low)


def detect(blob=None, name="", kind=None, forced=""):
    """→ (family_id, basis, 给人看的一句依据)。basis ∈ user/measure/hint/none。

    blob 是 GGUF 头部的原始字节（`gguf_structure` 的第二个返回值）；没有就读不到标记，
    这时只剩文件名线索 —— 所以调用方要么给 blob，要么接受 generic。
    """
    forced = str(forced or "").strip()
    if forced and forced != AUTO and forced in FAMILIES:
        return forced, "user", "你在设置里指定了模型族"
    blob = blob or b""
    # 1) 张量名（只有开发机实测过的家族参与）
    if blob:
        for fid in ("qwen-image", "minimax-h3"):
            if any(m in blob for m in FAMILIES[fid]["markers"]):
                return fid, "measure", "命中开发机实测过的张量名"
    # 2) 文件名线索（按 kind 收窄，避免把 wan 的视频名认成生图家族）
    cands = []
    for fid, p in FAMILIES.items():
        if fid == "generic" or not p.get("names"):
            continue
        if kind and p.get("kind") != kind:
            continue
        if _name_hit(name, p["names"]):
            cands.append((fid, p))
    if cands:
        # 命中多个时取"线索更长"的那个（`flux1-schnell` 该归 flux，不该被 "sd" 抢走）
        best = max(cands, key=lambda x: max((len(str(h)) for h in x[1]["names"]), default=0))
        return best[0], "hint", "按文件名线索匹配（这一家的张量名开发机没实测过）"
    # 3) 兜底：generic
    return "generic", "none", "认不出模型族，按通用方式只喂文件"


def detect_file(path, kind=None, forced="", name=None):
    """读文件头部做识别 → (family, basis, 说明)。

    头部只有 `kv=0`（裸权重容器）时才拿得到张量名 blob，这与 `gguf_structure` 的读法
    一致：有元数据的 GGUF 是语言模型 / 文本编码器，本来也不该被当成扩散主体判家族。
    读不动文件（不在盘上、非 GGUF、单文件 ckpt/safetensors）时只按文件名判 —— 所以
    返回的 basis 可能是 "hint"/"none"，界面要把这一点如实显示出来，别装作是实测结论。
    """
    from .gguf import gguf_structure
    blob = b""
    try:
        s = gguf_structure(path)
        if s is not None and not s[0]:
            blob = s[1]
    except Exception:
        blob = b""
    return detect(blob, name if name is not None else os.path.basename(path or ""),
                  kind, forced)


# 没在开发机实测过的视频家族，其张量名只能当**辅助**：要求"文件名带线索 + 至少 N 个标记
# 命中"才算视频主体。原因是 `patch_embedding` 这类名字在 PixArt / Flux 等图模型里也能见到，
# 单凭张量名会把生图模型抢进视频列表。判错方向的代价是"这个视频模型进不了菜单"
# ——那还有 设置 → 生视频 的手填文件名 这条出口救，比"生图模型莫名其妙消失"轻得多。
UNVERIFIED_VIDEO = (
    ((b"patch_embedding", b"text_embedding", b"time_projection",
      b"blocks.0.self_attn.qkv"), 2, ("wan", "ti2v", "t2v", "i2v", "vace")),
)


def video_role(blob, name=""):
    """kv=0 的裸权重在视频链路里的角色 → 'video'（扩散主体）/ 'encoder' / None。

    MiniMax-H3 的两组标记（`VIDEO_DIFFUSION_MARKERS` / `H3_ENC`）是开发机实测过的，直接算；
    其余家族见 `UNVERIFIED_MARKERS` 的额外门槛。LTX / HunyuanVideo 连张量名都没验证过，
    这里**不猜** —— 用设置里的手填文件名走人工出口。
    """
    from .gguf import VIDEO_DIFFUSION_MARKERS
    if not blob:
        return None
    if any(m in blob for m in VIDEO_DIFFUSION_MARKERS):
        return "video"
    if all(m in blob for m in H3_ENC):
        return "encoder"
    for marks, need, hints in UNVERIFIED_VIDEO:
        if _name_hit(name, hints) and sum(1 for m in marks if m in blob) >= need:
            return "video"
    return None


# ---------------------------------------------------------------- 配套文件发现
def _is_audio(name):
    b = str(name).lower()
    return "audio" in b


def _cands(folder, hints, must_not=(), exts=COMPANION_EXTS, recursive=False):
    """按"文件名里带这些线索"列候选（新改的在前，同宽度按名字排）。"""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    names = []
    if recursive:
        for root, _dirs, fs in os.walk(folder):
            for n in fs:
                names.append((n, os.path.join(root, n)))
    else:
        for n in sorted(os.listdir(folder)):
            fp = os.path.join(folder, n)
            if os.path.isfile(fp):
                names.append((n, fp))
    for n, fp in names:
        low = n.lower()
        if not low.endswith(exts):
            continue
        if any(x in low for x in must_not):
            continue
        if hints and not _name_hit(n, hints):
            continue
        out.append(fp)
    return out


def _common_prefix(a, b):
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return i


def rank_pool(pool, family, pbase):
    """给同一个目录里的多个候选排序：**先挑"像给这个模型配套"的那个**。

    一个目录里放了好几套家族的文件是很常见的（开发机生视频目录里就同时有 H3 的 VAE 和
    Wan 的 VAE）。只按文件名排序会抓到隔壁家族的组件 —— 引擎不会报错，只会用错 VAE
    出一堆糊片。排序依据：① 命中本家族名字线索的优先；② 与主体文件名公共前缀长的优先；
    ③ 名字本身。**找不到匹配本家族的候选时才退回原来的选择**，所以单家族目录不受影响。
    """
    hints = tuple(profile(family).get("names") or ())
    base = str(pbase or "").lower()

    def key(p):
        n = os.path.basename(p).lower()
        return (0 if hints and _name_hit(n, hints) else 1,
                -_common_prefix(n, base), n)
    return sorted(pool, key=key)


def discover(folders, family, diffusion="", want=None, encoders=None):
    """在若干目录里找齐这一族需要的配套文件 → {slot: 路径}。

    folders 按优先级排（主体所在目录放最前，那里最可能配套）。`want` 是人工指定的
    {slot: 文件名或绝对路径}，命中就用它 —— 这是"名字不规范 / 放在别处"的出口。
    `encoders` 是调用方已经认出来的**视频文本编码器**候选（kv=0 的裸权重，如
    MiniMax-H3 那个 `qwen3vl_32b_minimax_h3`）：这类文件没有 GGUF 元数据，
    `_pick_llm` 的"kv>0 才算编码器"判据抓不到它，必须由上层把候选递进来，
    否则视频链路会去抓一个真正的聊天模型当文本编码器（顶层模型库里全是聊天模型）。
    """
    prof = profile(family)
    pbase = os.path.basename(diffusion or "")
    files = {"diffusion": diffusion}
    want = dict(want or {})
    encoders = list(encoders or [])

    def _pick(slot, extra_hints=(), must_not=(), exts=COMPANION_EXTS, recursive=False):
        # 1) 人工指定优先（支持绝对路径 / 只给文件名）
        w = str(want.get(slot, "") or "").strip()
        if w:
            if os.path.isabs(w) and os.path.isfile(w):
                files[slot] = w
                return
            for folder in filter(None, folders):
                p = os.path.join(folder, w)
                if os.path.isfile(p):
                    files[slot] = p
                    return
        hints = tuple(SLOTS[slot][2]) + tuple(extra_hints)
        for folder in folders:
            pool = _cands(folder, hints, must_not=must_not, exts=exts, recursive=recursive)
            pool = [p for p in pool if os.path.basename(p) != pbase]
            if pool:
                files[slot] = rank_pool(pool, family, pbase)[0]
                return

    slots = list(prof.get("require", ())) + list(prof.get("optional", ()))
    for slot in slots:
        if slot == "diffusion":
            continue
        if slot not in SLOTS:
            if slot == "high_noise":
                _pick_high_noise(files, folders, pbase, want)
            continue
        ban = () if slot == "audio_vae" else ("audio",)
        # VAE 家族内部要互相让开：audio_vae / taesd 都不是主 VAE
        if slot == "vae":
            ban = ("audio", "tae")
        if slot == "llm":
            _pick_llm(files, folders, pbase, want.get("llm"), encoders)
            continue
        _pick(slot, must_not=ban)
    # llm_vision：mmproj 一律按"文件名以 mmproj 开头"找（与聊天侧同一判据）
    if "llm_vision" in slots and not files.get("llm_vision"):
        for folder in folders:
            pool = [p for p in _cands(folder, ("mmproj",), exts=(".gguf",))
                    if os.path.basename(p).lower().startswith("mmproj")]
            if pool:
                files["llm_vision"] = pool[0]
                break
    return files


def _pick_llm(files, folders, pbase, w, encoders=None):
    """LLM 文本编码器的取法，按可信度从高到低三档：

    1. 人工指定（`vid_llm_file` / `img_llm_file`，支持绝对路径或只给文件名）；
    2. **调用方递进来的编码器候选**：视频家族（MiniMax-H3）的文本编码器是 `kv=0` 的
       裸权重，没有 GGUF 元数据，下面那套"kv>0 才算语言模型"的判据抓不到它 ——
       必须由扫描层把认出来的 encoder 角色传进来，否则会抓到模型库顶层的聊天模型上；
    3. 目录里"带元数据、又不是 mmproj、名字不像组件"的 .gguf —— Qwen-Image 的
       Qwen3VL 编码器就是这一档（与改造前 resolve_img_files 的判据一致）。
    """
    from .gguf import gguf_is_chat_capable
    if w:
        if os.path.isabs(w) and os.path.isfile(w):
            files["llm"] = w
            return
        for folder in filter(None, folders):
            p = os.path.join(folder, w)
            if os.path.isfile(p):
                files["llm"] = p
                return
    pool = [p for p in list(encoders or [])
            if p and os.path.isfile(p) and os.path.basename(p) != pbase]
    if pool:
        own = os.path.dirname(files.get("diffusion") or "")
        pool.sort(key=lambda p: 0 if os.path.dirname(p) == own else 1)
        files["llm"] = pool[0]
        return
    for folder in folders:
        for n in sorted(os.listdir(folder) if os.path.isdir(folder) else []):
            p = os.path.join(folder, n)
            low = n.lower()
            if not low.endswith(".gguf") or low.startswith("mmproj"):
                continue
            if os.path.basename(p) == pbase:
                continue
            if _name_hit(n, ("vae", "clip", "t5", "lora", "tae")):
                continue
            if gguf_is_chat_capable(p):
                files["llm"] = p
                return


def _pick_high_noise(files, folders, pbase, want):
    """Wan2.2 MoE 的高噪段模型：名字里带 high（noise）的那个扩散文件。"""
    if want:
        w = str(want)
        if os.path.isabs(w) and os.path.isfile(w):
            files["high_noise"] = w
            return
    for folder in folders:
        pool = _cands(folder, HIGH_NOISE_HINT, exts=DIFFUSION_EXTS)
        pool = [p for p in pool if os.path.basename(p) != pbase]
        if pool:
            files["high_noise"] = pool[0]
            return


# ---------------------------------------------------------------- 缺件预检
def slot_flag(slot):
    """槽位对应的引擎参数名（`high_noise` 不在 SLOTS 里，它是主体的第二段）。"""
    if slot == "high_noise":
        return HIGH_NOISE_FLAG
    return SLOTS[slot][0]


def slot_label(slot):
    if slot == "diffusion":
        return "扩散主体模型文件"
    if slot == "high_noise":
        return "高噪段扩散模型（Wan2.2 MoE 那两个文件里的 high-noise 那个）"
    return "%s（引擎参数 %s）" % (SLOTS[slot][1], SLOTS[slot][0])


def missing_slots(family, files):
    """按家族的 require 列缺项 → 中文说明列表（空列表才代表可以开工）。"""
    prof = profile(family)
    out = []
    for slot in prof.get("require", ()):
        if slot == "diffusion":
            if not files.get("diffusion"):
                out.append(slot_label("diffusion"))
            continue
        if not files.get(slot):
            out.append(slot_label(slot))
    return out


def note_of(family, files):
    """给设置页回显的一行识别结果：家族 + 各槽位命中了什么文件名。"""
    prof = profile(family)
    got = []
    for slot in ("vae", "llm", "clip_l", "clip_g", "t5xxl", "llm_vision", "tokenizer",
                 "high_noise", "audio_vae", "connectors", "motion"):
        p = files.get(slot)
        if p:
            name = ("高噪段模型" if slot == "high_noise"
                    else SLOTS[slot][1].split("（")[0])
            got.append("%s=%s" % (name, os.path.basename(p)))
    basis = files.get("basis") or ""
    tail = "" if basis in ("", "measure", "user") else "（按文件名猜的，没在开发机实测过）"
    return "模型族：%s%s｜%s" % (prof["label"], tail,
                                "、".join(got) if got else "还没找到配套文件")
