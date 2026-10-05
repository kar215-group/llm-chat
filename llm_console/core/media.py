# -*- coding: utf-8 -*-
"""llm_console.core.media — sd-cli 命令行组装（生图 / 生视频）：**按模型族决定槽位与参数**

v0.0.3 之前这里的参数组合是围着开发机那两个模型写死的（生图固定 `--llm` + `euler` +
`--flow-shift 3`，生视频固定 MiniMax-H3 那三件套）。现在改成先由 `core.sdprofile`
认出模型族，再按那一族的形状决定：

  · 主体文件用哪个参数交出去（裸扩散权重 → `--diffusion-model`，单文件 ckpt/safetensors → `-m`）
  · 需要哪些配套文件（VAE / CLIP-L / CLIP-G / T5-XXL / LLM / mmproj / tokenizer /
    高噪段模型 / 音频件 / connectors / 运动模块）
  · 该不该指定采样器、flow-shift、flash attention、后端
  · 尺寸对齐到几的倍数、附图走 `-i`（img2img/首帧）还是 `-r`（参考图）

**已实测过的两个家族（Qwen-Image 2.1 / MiniMax-H3）的 argv 与改造前逐字一致**，
由 `_selftest/test_sd_profiles.py` 钉住；其余家族的形状来自上游 README、docs/sd.md、
docs/flux.md、docs/wan.md 与 `sd-cli --help` 的原文（开发机没实测过的，界面会如实说明）。
"""

import os
import shlex

from . import sdprofile
from .models import (IMAGE_SUBDIR, _is_mmproj, find_vl_pairs, scan_models,
                     scan_video_models, video_scan_dirs)

# 配套槽位拼进命令行的先后顺序。Qwen-Image 的实测 argv 是 vae→llm，保持不变；
# 视频侧反过来（llm→vae→audio-vae）也是为了与 MiniMax-H3 改造前的 argv 逐字相同。
_SLOT_ORDER = ("vae", "llm", "clip_l", "clip_g", "t5xxl", "clip_vision",
               "tokenizer", "connectors", "audio_vae", "audio_encoder", "motion",
               "taesd", "high_noise")
_VIDEO_SLOT_ORDER = ("llm", "vae", "audio_vae", "t5xxl", "clip_l", "clip_g",
                     "tokenizer", "connectors", "audio_encoder", "high_noise", "taesd")

# MiniMax-H3 在 CFG>1 时必须能编码出负向提示词，空串会直接失败；
# 配置里留空时用这个兜底值（内容可在 设置 → 本地模型 → 生视频（sd.cpp） 改）。
DEFAULT_VIDEO_NEG_PROMPT = ("worst quality, low quality, blurry, distorted, deformed, "
                            "watermark, text, static, jittery")


def _wh(size, fallback, multiple=1):
    """解析 "宽x高"；非法值回退默认档（用户手填坏值时别让发送链路崩掉丢输入）。

    `multiple` 是这一族要求的尺寸对齐（SD 系 8、Flux/SD3/Wan 16、LTX 32）：
    不对齐时**向上**补到最近的倍数——引擎多半直接报错，替用户改一下比失败一次好，
    而 1024x1024 / 512x512 这些常用档本来就是 8/16 的倍数，不会被改动。
    """
    try:
        w, h = str(size).lower().split("x", 1)
        w, h = int(w.strip()), int(h.strip())
        if w > 0 and h > 0:
            m = max(1, int(multiple or 1))
            if m > 1:
                w = w + (-w % m)
                h = h + (-h % m)
            return str(w), str(h)
    except Exception:
        pass
    fw, fh = fallback.split("x")
    return fw, fh


def _folders_for(cfg, main_path, extra_dirs=(), include_models_dir=True,
                 include_image_dir=True):
    """配套文件的搜索目录：主体所在目录最优先，其次配置里指定的目录，再回退默认位置。

    两个边界都是实测换来的：
      · `include_models_dir=False` 给生图用 —— 改造前 `resolve_img_files` 只看生图目录，
        放宽到整个模型库会让"挑文本编码器"抓到顶层那堆聊天模型（它们是 kv>0 的合法
        语言模型，判据分不清"编码器"和"聊天模型"，只能靠目录边界）。
      · `include_image_dir=False` 给生视频用 —— 否则生图目录里的
        `qwen_image_2.1_vae_bf16.safetensors` 会被当成视频 VAE（坑 29）。
    """
    out = []
    if main_path:
        out.append(os.path.dirname(main_path))
    tails = list(extra_dirs)
    if include_image_dir:
        tails.append(cfg.get("image_model_dir", ""))
    if include_models_dir:
        tails.append(cfg.get("models_dir", ""))
    for d in tails:
        np = os.path.normpath(str(d or ""))
        if np and os.path.isdir(np) and np not in [os.path.normpath(x) for x in out]:
            out.append(np)
    return out


def family_of(cfg, path, kind, forced_key):
    """这一条链路用哪个模型族：配置里人工指定的 > 自动识别。→ (fid, basis, note)"""
    forced = str(cfg.get(forced_key, "") or "").strip()
    return sdprofile.detect_file(path, kind=kind, forced=forced)


# ------------------------------------------------------------------------ 生图

def img_dir_of(cfg):
    """生图目录：`image_model_dir` 优先，**留空 = `<模型目录>/生图`**。

    这条兜底**与扫描侧同一口径**（`core.models.scan_models` / `video_scan_dirs` 都是这么算的）。
    发送侧原来只读 `image_model_dir`，于是"留空"的出厂默认机器上会出现
    **菜单里看得见生图模型、一发就报「未找到生图模型」** —— 扫描侧找得到、发送侧找不到，
    两边判据分叉（坑 128 同族）。
    """
    c = cfg or {}
    base = str(c.get("models_dir") or os.path.dirname(str(c.get("model") or "")) or ".")
    return str(c.get("image_model_dir") or "") or os.path.join(base, IMAGE_SUBDIR)


def resolve_img_model_path(cfg):
    """这次生图该用哪个扩散模型 → **绝对路径**；定不出来返回空串。

    发送侧拿扩散模型的唯一出口，顺序：
      ① 菜单里选中的就是生图模型（`model_kind == "image"`，扫描给的就是绝对路径）→ 用它；
      ② `img_model_file`：绝对路径且在 → 直接用；否则按 `img_dir_of` 拼一次；**给了名字却
         找不到就返回空串**（显式设置该报错就报错，不偷偷换一个模型）；
      ③ 扫描到的第一个生图模型（`scan_models` 返回的本来就是绝对路径）。

    为什么不再"取 basename 再拼回生图目录"：模型不在那个目录里时（手动定向到别处、或
    放在模型目录顶层）那样必然找不到，报出来的却是"未找到生图模型"（坑 42 同族：拼出
    错路径 / 相对路径，症状与根因对不上）。
    """
    c = cfg or {}
    cur = str(c.get("model") or "")
    if str(c.get("model_kind") or "") == "image" and cur and os.path.isfile(cur):
        return cur
    name = str(c.get("img_model_file") or "").strip()
    if name:
        if os.path.isabs(name) and os.path.isfile(name):
            return name
        p = os.path.join(img_dir_of(c), name)
        return p if os.path.isfile(p) else ""
    try:
        _d, _chat, images = scan_models(c)
    except Exception:
        images = []
    for p in images or []:
        if os.path.isfile(p):
            return p
    return ""


def resolve_img_files(cfg, diffusion_path=None, family=None):
    """按模型族找齐生图需要的配套文件 → dict（含 family / basis / note）。

    兼容旧调用：返回值一直有 "vae" 与 "llm" 两个键。找不到的槽位就是空字符串，
    由调用方（UI）在 Popen 前预检并给出可操作提示 —— 别把不存在的路径交给引擎
    再吃一次"退出码 1"（坑 37/38 同一教训）。
    人工出口：`img_vae_file` / `img_llm_file` / `img_clip_l_file` / `img_clip_g_file` /
    `img_t5_file` 留空 = 自动发现；填了（文件名或绝对路径）就以它为准。
    """
    if not diffusion_path:
        # 不传主体路径时自己定（口径见 resolve_img_model_path）——别在这里再写一遍
        diffusion_path = resolve_img_model_path(cfg)
    # family 可以是 sdprofile 的三元组，也可以只是家族 id 字符串（调用方常常只有一个）
    if isinstance(family, (tuple, list)) and len(family) == 3:
        fid, basis, why = family
    elif family:
        fid, basis, why = str(family), "user", "调用方指定了模型族"
    else:
        fid, basis, why = family_of(cfg, diffusion_path, "image", "img_family")
    folders = _folders_for(cfg, diffusion_path, include_models_dir=False)
    want = {}
    for slot, key in (("vae", "img_vae_file"), ("llm", "img_llm_file"),
                      ("clip_l", "img_clip_l_file"), ("clip_g", "img_clip_g_file"),
                      ("t5xxl", "img_t5_file"), ("tokenizer", "img_tokenizer_file")):
        if str(cfg.get(key, "") or "").strip():
            want[slot] = str(cfg[key]).strip()
    files = sdprofile.discover(folders, fid, diffusion_path, want)
    files["family"] = fid
    files["basis"] = basis
    files["note"] = why
    files.setdefault("vae", "")
    files.setdefault("llm", "")
    return files


def build_img_cmd(cfg, prompt, out_path, steps, size, diffusion_path, cfg_scale, seed,
                  init_img=None, vae=None, llm=None, family=None, files=None):
    """组装 sd-cli 生图命令行（形状由模型族决定，参数值仍来自配置）。

    Qwen-Image 这条链路保持改造前的逐字顺序：
      --diffusion-model / --vae / --llm / --backend / --cfg-scale / --sampling-method
      euler / --flow-shift 3 / --diffusion-fa / --steps / -W -H / -s -p -o
      附图时再补 --llm_vision、（CFG 低于 3.0 时提到 3.0）、-i、--strength。
    其它家族：单文件权重走 `-m`；CLIP 系补 --clip_l/--clip_g/--t5xxl；不硬塞
    flow-shift 与采样器（让引擎按模型自己定），尺寸按各家的对齐值补整。
    """
    sd = cfg.get("sd_dir", "")
    if files is None:
        # 没给整份发现结果就自己发现一次（旧调用方只传 vae/llm 也能工作，
        # 但那样 clip_l/t5xxl 这些槽位就丢了，所以 UI 主链路现在传 files=found）
        found = resolve_img_files(cfg, diffusion_path, family)
        vae = vae or found.get("vae")
        llm = llm or found.get("llm")
        fid = family or found["family"]
        files = dict(found)
    else:
        files = dict(files)
        fid = family or files.get("family") or family_of(
            cfg, diffusion_path, "image", "img_family")[0]
        vae = vae or files.get("vae")
        llm = llm or files.get("llm")
    prof = sdprofile.profile(fid)
    w, h = _wh(size, "1024x1024", prof.get("size_multiple", 8))
    main_flag = prof.get("main_flag", "--diffusion-model")

    args = [os.path.join(sd, "sd-cli.exe"), main_flag, diffusion_path]
    files["diffusion"] = ""                  # 主体已经单独拼过，别再进配套循环
    if vae:
        files["vae"] = vae
    if llm:
        files["llm"] = llm
    for slot in _SLOT_ORDER:
        p = str(files.get(slot) or "")
        if p and os.path.isfile(p):
            args += [sdprofile.slot_flag(slot), p]

    bk = str(cfg.get("img_backend", "") or "").strip() or prof.get("backend", "")
    if bk:
        args += ["--backend", bk]
    pb = str(cfg.get("img_params_backend", "") or "").strip()
    if pb:
        args += ["--params-backend", pb]
    args += ["--cfg-scale", str(cfg_scale)]
    if prof.get("sampler"):
        args += ["--sampling-method", prof["sampler"]]
    if prof.get("flow_shift"):
        args += ["--flow-shift", str(prof["flow_shift"])]
    if prof.get("fa"):
        args += ["--diffusion-fa"]
    neg = str(cfg.get("img_negative", "") or "").strip()
    if neg:
        args += ["-n", neg]
    args += ["--steps", str(steps), "-W", w, "-H", h,
             "-s", str(seed), "-p", prompt, "-o", out_path]

    if init_img and os.path.isfile(init_img):
        edit = prof.get("edit", "init")
        if edit == "ref":
            # Flux Kontext / MiniMax Ref2VA 这类"参考图"模型：-r 可重复给
            args += ["-r", init_img]
        else:
            # 附图 = 底图（img2img）。LLM 编码器家族还要配视觉投影器，否则引擎
            # 读不懂"把背景换成海边"这类语义指令（开发机实测 --llm_vision 必需）。
            vis = str(files.get("llm_vision") or "")
            if prof.get("edit_needs") and not vis:
                vis = find_vl_pairs(cfg).get(llm) or ""
            if not (vis and os.path.isfile(vis)) and prof.get("edit_needs"):
                try:
                    img_dir = img_dir_of(cfg)      # 与上面同一口径（留空 = <模型目录>/生图）
                    vis = next((os.path.join(img_dir, n) for n in sorted(os.listdir(img_dir))
                                if _is_mmproj(n) and n.lower().endswith(".gguf")), "")
                except Exception:
                    vis = ""
            if vis and os.path.isfile(vis):
                args += ["--llm_vision", vis]
            minimum = prof.get("edit_cfg_min")
            if minimum and float(cfg_scale) < float(minimum):
                i = args.index("--cfg-scale")
                args[i + 1] = str(minimum)
            args += ["-i", init_img,
                     "--strength", str(cfg.get("img_strength", 0.9))]
    if str(cfg.get("img_extra_args", "")).strip():
        args += shlex.split(str(cfg["img_extra_args"]))
    return args


# ---------------------------------------------------------------------- 生视频
def _video_component_missing_label(name):
    return {"diffusion": "视频扩散主体（MiniMax-H3 量化 GGUF）",
            "llm": "文本编码器（sd-cli --llm）",
            "vae": "视频 VAE（sd-cli --vae）"}[name]


def resolve_video_files(cfg, diffusion_path=None):
    """定位视频链路所需文件 → dict(diffusion, llm, vae, ..., family, missing)。

    与生图同一套思路：先认族，再按那一族要求的槽位去找。三个改动点：
      · **人工指定的文件即使没被扫描识别也直接采用**（名字不规范 / 放在别的目录时，
        原来是"扫描没命中就当缺件"，用户只能改文件名，很别扭）；
      · 家族要求里带 `t5xxl` 的（Wan / LTX / HunyuanVideo）不再逼用户去找一个
        `--llm` 编码器；MiniMax-H3 仍然要求 llm + vae，与改造前一致；
      · missing 的说明按槽位生成，会点名引擎参数名。
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

    diffusion = (diffusion_path or _by_name(vids, cfg.get("vid_model_file"))
                 or (vids[0] if vids else ""))
    fid, basis, why = family_of(cfg, diffusion, "video", "vid_family")

    want = {}
    for slot, key in (("llm", "vid_llm_file"), ("vae", "vid_vae_file"),
                      ("t5xxl", "vid_t5_file"), ("tokenizer", "vid_tokenizer_file"),
                      ("high_noise", "vid_high_noise_file"),
                      ("audio_vae", "vid_audio_vae_file")):
        if str(cfg.get(key, "") or "").strip():
            want[slot] = str(cfg[key]).strip()

    folders = _folders_for(cfg, diffusion, (cfg.get("video_model_dir", ""),),
                           include_image_dir=False)
    # 视频目录没单独配时，沿用原来的扫描范围（顶层 + 一级子目录，不含生图目录）
    folders = folders + [f for f in video_scan_dirs(cfg) if f not in folders]
    files = sdprofile.discover(folders, fid, diffusion, want, encoders=encs)
    files["diffusion"] = diffusion
    if not files.get("llm") and want.get("llm"):
        # 人工指定但按目录没拼出来：给一次全盘绝对路径的机会，别偷偷换成别的文件
        w = str(want["llm"])
        if os.path.isabs(w) and os.path.isfile(w):
            files["llm"] = w
    files.setdefault("audio_vae", "")
    files["family"] = fid
    files["basis"] = basis
    files["note"] = why
    files["missing"] = sdprofile.missing_slots(fid, files)
    return files


def build_video_cmd(cfg, prompt, out_path, files, frames, fps, size, steps,
                    cfg_scale, seed, ref_img=None):
    """组装 sd-cli 生视频命令行（`-M vid_gen` 是所有视频家族的公共前提）。

    MiniMax-H3 这条链路的 argv 与改造前逐字一致，两处实测出来的硬要求都保留：
      --mode vid_gen   sd-cli 的 --mode 默认是 img_gen，用默认值跑 MiniMax-H3 会在
                       加载完权重后直接报 "MiniMax-H3 cannot be run with
                       generate_image()" 并以退出码 1 结束（2.4s，不进采样）
      -n <负向提示词>   CFG>1 时要编码负向提示词，留空会报
                       "failed to encode negative video prompt"（同样退出码 1）
    其它家族（Wan / LTX / HunyuanVideo）：负向提示词不是必需项，用户没填就不传；
    配套槽位按 sdprofile 的要求补齐（Wan 的 --t5xxl、2.2 MoE 的
    --high-noise-diffusion-model、LTX 的 --embeddings-connectors / --audio-vae）。
    首帧仍统一走 `-i`（Ref2VA 这类才用 -r，见坑 42）；分块解码参数由
    `vid_extra_args` 给出，显存吃紧的人可以自己改，代码里不替用户加戏。
    """
    sd = cfg.get("sd_dir", "")
    fid = files.get("family") or family_of(cfg, files.get("diffusion"), "video",
                                          "vid_family")[0]
    prof = sdprofile.profile(fid)
    w, h = _wh(size, "512x512", prof.get("size_multiple", 8))
    args = [os.path.join(sd, "sd-cli.exe"), "--mode", "vid_gen",
            "--diffusion-model", files["diffusion"],
            "--cfg-scale", str(cfg_scale),
            "--steps", str(steps),
            "--video-frames", str(frames), "--fps", str(fps),
            "-W", str(w), "-H", str(h),
            "-s", str(seed), "-p", prompt]
    neg = str(cfg.get("vid_neg_prompt", "") or "").strip()
    if prof.get("neg_prompt_required", True):
        args += ["-n", neg or DEFAULT_VIDEO_NEG_PROMPT]
    elif neg:
        args += ["-n", neg]
    args += ["-o", out_path]
    for slot in _VIDEO_SLOT_ORDER:
        p = str(files.get(slot) or "")
        if p and os.path.isfile(p):
            args += [sdprofile.slot_flag(slot), p]
    if prof.get("sampler"):
        args += ["--sampling-method", prof["sampler"]]
    if prof.get("flow_shift"):
        args += ["--flow-shift", str(prof["flow_shift"])]
    if prof.get("fa"):
        args += ["--diffusion-fa"]
    bk = str(cfg.get("vid_backend", "")).strip()
    if bk:
        args += ["--backend", bk]
    pb = str(cfg.get("vid_params_backend", "")).strip()
    if pb:
        args += ["--params-backend", pb]
    if ref_img and os.path.isfile(ref_img):
        # 附图 = 首帧（图生视频）。开发机主体是 fl2va 变体，用 -i/--init-img；
        # -r/--ref-image 是给 Ref2VA 变体的，用错引擎不认（实测踩中）。
        # 首帧由引擎自己 crop/resize 到 -W/-H（实测 1024x1024 → 512x512），
        # 且 fl2va 只要首帧就能跑，不需要 --end-img（那是 flf2v 的尾帧）。
        if prof.get("edit") == "ref":
            args += ["-r", ref_img]
        else:
            args += ["-i", ref_img]
    if str(cfg.get("vid_extra_args", "")).strip():
        args += shlex.split(str(cfg["vid_extra_args"]))
    return args


# 旧名字保留（外部脚本与 models_ui 的提示仍在引用）：
#   _video_component_missing_label —— 现在只作 H3 老文案的兼容入口，
#   新代码请用 sdprofile.missing_slots / slot_label。
