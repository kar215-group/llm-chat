# -*- coding: utf-8 -*-
"""llm_console.core.media — 生成命令组装：生图（sd-cli）与生视频（sd-cli + MiniMax-H3 组件解析）"""

import os
import shlex

from .gguf import gguf_is_chat_capable
from .models import _is_mmproj, find_vl_pairs, scan_video_models, video_scan_dirs


def _wh(size, fallback):
    """解析 "宽x高"；非法值回退默认档（用户手填坏值时别让发送链路崩掉丢输入）。"""
    try:
        w, h = str(size).lower().split("x", 1)
        w, h = int(w.strip()), int(h.strip())
        if w > 0 and h > 0:
            return str(w), str(h)
    except Exception:
        pass
    fw, fh = fallback.split("x")
    return fw, fh


# 生图 VAE 可识别的扩展名（与视频 VAE 同一套）
_IMG_VAE_EXTS = (".safetensors", ".gguf", ".pt", ".ckpt")


def resolve_img_files(cfg, diffusion_path=None):
    """解析生图链路的 VAE（--vae）与文本编码器（--llm）路径。

    自动发现在 image_model_dir 里进行（与视频链路 resolve_video_files 同风格）：
      VAE        —— 文件名含 "vae"（排除 "audio"）且扩展名可识别；
      文本编码器 —— kv>0 的 .gguf（排除 mmproj 与扩散模型本身），名字排序取第一个。
    之前这两个文件名是写死的，别人的机器上放同名文件的概率为零——分发场景
    生图必然失败且无回显（生图链路没有 _vid_tail 那套失败回显）。
    找不到就返回空路径，由调用方（UI）在 Popen 前预检并给出可操作提示，
    别把不存在的路径交给引擎再吃一次"退出码 1"（坑 37/38 同一教训）。
    """
    img_dir = str(cfg.get("image_model_dir", "") or "")
    try:
        names = sorted(os.listdir(img_dir)) if img_dir and os.path.isdir(img_dir) else []
    except Exception:
        names = []
    db = os.path.basename(diffusion_path or "")

    vae = next((os.path.join(img_dir, n) for n in names
                if "vae" in n.lower() and "audio" not in n.lower()
                and os.path.splitext(n)[1].lower() in _IMG_VAE_EXTS), "")
    llm = next((os.path.join(img_dir, n) for n in names
                if n.lower().endswith(".gguf") and n != db
                and not _is_mmproj(n)
                and gguf_is_chat_capable(os.path.join(img_dir, n))), "")
    return {"vae": vae, "llm": llm}


def build_img_cmd(cfg, prompt, out_path, steps, size, diffusion_path, cfg_scale, seed,
                  init_img=None, vae=None, llm=None):
    """组装 sd-cli 生图命令行（Qwen-Image 2.1）。

    传入 init_img 时启用"参考图编辑/图生图"，参数组合由本机实测校准：
      -i <img>（参考图）+ --strength（默认 0.9，0.6 太保守几乎不改变画面）
      + --llm_vision <mmproj>（视觉投影器；语义编辑必需，缺它无法理解"换背景"这类指令）
      + CFG 提到 3.0（偏低时提示词影响力不足）
    vae/llm 显式传入时用之（UI 主链路已经预检过）；缺省时内部自动发现
    （resolve_img_files），保持老调用方的兼容。
    """
    sd = cfg.get("sd_dir", "")
    img_dir = cfg.get("image_model_dir", "")
    if vae is None or llm is None:
        found = resolve_img_files(cfg, diffusion_path)
        vae = vae or found["vae"]
        llm = llm or found["llm"]
    w, h = _wh(size, "1024x1024")
    args = [os.path.join(sd, "sd-cli.exe"),
            "--diffusion-model", diffusion_path,
            "--vae", vae,
            "--llm", llm,
            "--backend", "te=cpu,diffusion=cuda0,vae=cuda0",
            "--cfg-scale", str(cfg_scale),
            "--sampling-method", "euler", "--flow-shift", "3",
            "--diffusion-fa", "--steps", str(steps),
            "-W", w, "-H", h,
            "-s", str(seed), "-p", prompt, "-o", out_path]
    if init_img and os.path.isfile(init_img):
        # mmproj（视觉投影器）：优先按"编码器 → mmproj"配对表取；配不上时
        # 在生图目录里找第一个 mmproj-*.gguf 兜底（Q4_K_M 编码器与 F16 mmproj
        # 这类命名互不为子串、配对表本来就配不上，此前是靠写死文件名在跑）。
        vis = find_vl_pairs(cfg).get(llm) or ""
        if not (vis and os.path.isfile(vis)):
            try:
                vis = next((os.path.join(img_dir, n) for n in sorted(os.listdir(img_dir))
                            if _is_mmproj(n) and n.lower().endswith(".gguf")), "")
            except Exception:
                vis = ""
        if vis and os.path.isfile(vis):
            args += ["--llm_vision", vis]
        if cfg_scale < 3.0:
            i = args.index("--cfg-scale")
            args[i + 1] = "3.0"          # 编辑场景实测：低于 3.0 提示词影响力不足
        args += ["-i", init_img,
                 "--strength", str(cfg.get("img_strength", 0.9))]
    return args

# MiniMax-H3 在 CFG>1 时必须能编码出负向提示词，空串会直接失败；
# 配置里留空时用这个兜底值（内容可在 设置 → 生视频 改）。
DEFAULT_VIDEO_NEG_PROMPT = ("worst quality, low quality, blurry, distorted, deformed, "
                            "watermark, text, static, jittery")


def _video_component_missing_label(name):
    return {"diffusion": "视频扩散主体（MiniMax-H3 量化 GGUF）",
            "llm": "文本编码器（sd-cli --llm）",
            "vae": "视频 VAE（sd-cli --vae）"}[name]

def resolve_video_files(cfg):
    """定位视频链路所需文件，返回 dict(diffusion, llm, vae, audio_vae, missing)。

    配置里填的文件名优先，留空则自动挑：扩散主体取扫描到的第一个，
    编码器优先取与主体同目录的，VAE 按"文件名含 vae"在同目录里找。
    missing 是缺项的中文说明列表，空列表才代表可以开工——视频链路组件比生图多，
    下载不全（尤其 VAE 容易漏）时必须在启动前就说清楚缺哪一项，而不是排队几分钟后失败。
    """
    vids, encs = scan_video_models(cfg)

    def _by_name(pool, want):
        w = str(want or "").strip()
        if not w:
            return ""
        if os.path.isabs(w) and os.path.isfile(w):
            return w
        for p in pool:
            if os.path.basename(p).lower() == w.lower():
                return p
        for base in filter(None, (cfg.get("video_model_dir", ""),
                                  cfg.get("models_dir", ""),
                                  cfg.get("image_model_dir", ""))):
            p = os.path.join(base, w)
            if os.path.isfile(p):
                return p
        return ""

    diffusion = _by_name(vids, cfg.get("vid_model_file")) or (vids[0] if vids else "")
    llm = _by_name(encs, cfg.get("vid_llm_file"))
    if not llm and diffusion:
        near = [p for p in encs if os.path.dirname(p) == os.path.dirname(diffusion)]
        llm = near[0] if near else (encs[0] if encs else "")

    # VAE 在视频扫描目录里找（主体所在目录优先）。生图目录不参与，避免把
    # qwen_image 的图像 VAE 当成视频 VAE；这样组件放顶层还是放「生视频」都能命中。
    folders = video_scan_dirs(cfg)
    if diffusion:
        own = os.path.normpath(os.path.dirname(diffusion))
        folders = [own] + [f for f in folders if f != own]
    vae_pool = []
    for folder in folders:
        try:
            names = sorted(os.listdir(folder))
        except Exception:
            continue
        vae_pool += [os.path.join(folder, n) for n in names if "vae" in n.lower()
                     and os.path.splitext(n)[1].lower() in
                     (".safetensors", ".gguf", ".pt", ".ckpt")]
    vae = _by_name(vae_pool, cfg.get("vid_vae_file")) or next(
        (p for p in vae_pool if "audio" not in os.path.basename(p).lower()), "")
    audio_vae = next((p for p in vae_pool
                      if "audio" in os.path.basename(p).lower()), "")

    missing = [_video_component_missing_label(k) for k in ("diffusion", "llm", "vae")
               if not {"diffusion": diffusion, "llm": llm, "vae": vae}[k]]
    return {"diffusion": diffusion, "llm": llm, "vae": vae,
            "audio_vae": audio_vae, "missing": missing}

def build_video_cmd(cfg, prompt, out_path, files, frames, fps, size, steps,
                    cfg_scale, seed, ref_img=None):
    """组装 sd-cli 生视频命令行（MiniMax-H3，与生图同引擎不同链路）。

    与生图一致的取舍：--backend 把文本编码器放 CPU、扩散与 VAE 放显卡；
    分块解码（--vae-tiling / --temporal-tiling）压显存。
    本机实测（512×512 / 20 步 / 17→22 帧）：总 235.8s，采样约 9.9 s/it，VAE 解码 15.2s，
    总参数 28.45GB（VRAM 4.98 + RAM 23.47），不 OOM。
    附图时走 -i（首帧，图生视频），与生图的"附图=参考图"语义对齐。

    两个必须显式给的参数（都是本机实测踩出来的，缺一个就退出码 1）：
      --mode vid_gen   sd-cli 默认 img_gen，MiniMax-H3 会直接拒绝 generate_image()
      -n <负向提示词>   CFG>1 时要编码负向提示词，留空会报
                       "failed to encode negative video prompt"
    """
    sd = cfg.get("sd_dir", "")
    w, h = _wh(size, "512x512")
    neg = str(cfg.get("vid_neg_prompt", "") or "").strip() or DEFAULT_VIDEO_NEG_PROMPT
    args = [os.path.join(sd, "sd-cli.exe"),
            # 必须显式切到视频模式：sd-cli 的 --mode 默认是 img_gen，
            # 用默认值跑 MiniMax-H3 会在加载完权重后直接报
            # "MiniMax-H3 cannot be run with generate_image()" 并以退出码 1 结束（2.4s，不进采样）
            "--mode", "vid_gen",
            "--diffusion-model", files["diffusion"],
            "--cfg-scale", str(cfg_scale),
            "--steps", str(steps),
            "--video-frames", str(frames), "--fps", str(fps),
            "-W", str(w), "-H", str(h),
            "-s", str(seed), "-p", prompt, "-n", neg, "-o", out_path]
    if files.get("llm"):
        args += ["--llm", files["llm"]]
    if files.get("vae"):
        args += ["--vae", files["vae"]]
    if files.get("audio_vae"):
        args += ["--audio-vae", files["audio_vae"]]
    bk = str(cfg.get("vid_backend", "")).strip()
    if bk:
        args += ["--backend", bk]
    pb = str(cfg.get("vid_params_backend", "")).strip()
    if pb:
        args += ["--params-backend", pb]
    if ref_img and os.path.isfile(ref_img):
        # 附图 = 首帧（图生视频）。本机主体是 fl2va 变体，用 -i/--init-img；
        # -r/--ref-image 是给 Ref2VA 变体的，用错引擎不认（实测踩中）。
        # 首帧由引擎自己 crop/resize 到 -W/-H（实测 1024x1024 → 512x512），
        # 且 fl2va 只要首帧就能跑，不需要 --end-img（那是 flf2v 的尾帧）。
        args += ["-i", ref_img]
    if str(cfg.get("vid_extra_args", "")).strip():
        args += shlex.split(str(cfg["vid_extra_args"]))
    return args
