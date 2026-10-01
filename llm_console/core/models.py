# -*- coding: utf-8 -*-
"""llm_console.core.models — 模型库：扫描、分类（聊天/生图/生视频）、mmproj 配对、简称与模糊解析、文件夹整理"""

import difflib
import os
import re

from . import sdprofile
from .gguf import VIDEO_DIFFUSION_MARKERS, VIDEO_ENCODER_MARKERS, gguf_is_chat_capable, gguf_structure


def make_alias(filename):
    """从 GGUF 文件名自动生成简短别名（纯代码规则，不询问模型）。

    例：Qwen3.8-27B-UD-Q3_K_XL.gguf -> qwen3.8-27b-q3
        超长的社区微调名会按连字符截断到 30 字符以内。
    """
    s = filename.strip()
    if s.lower().endswith(".gguf"):
        s = s[:-5]
    s = s.lower()
    # 量化后缀压缩：q3_k_xl -> q3、iq4_xs -> iq4、q4_0 -> q4
    s = re.sub(r"\bi?q\d+_k_(?:s|m|l|xl|xxl|xs)\b",
               lambda m: m.group(0).split("_")[0], s)
    s = re.sub(r"\biq\d+_(?:xxs|xs|s|m|l|xl)\b",
               lambda m: m.group(0).split("_")[0], s)
    s = re.sub(r"\bi?q\d+_[012]\b", lambda m: m.group(0).split("_")[0], s)
    # 常见噪声词移除
    for w in ("ud-", "ggml-", "instruct", "-hf", "-i1", "-imatrix"):
        s = s.replace(w, "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    # 超长别名（社区微调名）：按连字符截到 30 字符内，避免顶栏被挤爆
    if len(s) > 30:
        cut = s[:30]
        s = cut[:cut.rfind("-")] if "-" in cut else cut
    return s or filename

def display_name(cfg, path):
    """页面显示名：显式别名 > 代码自动生成的简称。"""
    key = os.path.basename(path)
    alias = (cfg.get("model_aliases") or {}).get(key)
    if alias:
        return alias
    return make_alias(key)

IMAGE_SUBDIR = "生图"             # models_dir 下存放生图大模型的子文件夹名

# 生图目录里"不是模型本体"的文件名特征。原来这里写死了 `qwen3vl`（本机那个文本编码器
# 的名字），换一家模型就会把它当成扩散模型列进菜单。现在交给 sdprofile 的通用配套件
# 名单（vae / clip / t5 / lora / tokenizer / motion / …），判定不再围着某一个文件名转。
IMAGE_COMPONENT_PATTERNS = sdprofile.COMPANION_PATTERNS


def is_image_diffusion(filename):
    """只看文件名判断"像不像生图扩散模型"（弱判据，供 find_vl_pairs 这类场合用）。

    真正的判定在 `is_diffusion_file()` —— 它会去读 GGUF 头部：**带元数据（kv>0）的是
    语言模型**（聊天模型或 Qwen3VL 这类文本编码器），kv=0 的裸权重才是扩散主体。
    """
    b = str(filename or "")
    return (b.lower().endswith(sdprofile.DIFFUSION_EXTS)
            and sdprofile.is_model_file(b))


def is_diffusion_file(path):
    """该文件能不能当"生图扩散模型"用：扩展名对、不是配套件、且不是带元数据的语言模型。"""
    name = os.path.basename(path)
    if not is_image_diffusion(name):
        return False
    if not sdprofile.looks_like_companion(name) and gguf_is_chat_capable(path) \
            and str(name).lower().endswith(".gguf"):
        return False                    # kv>0 的 GGUF 是语言模型 / 文本编码器
    return video_component_role(path) != "video"


def _is_mmproj(name):
    """mmproj-*.gguf 是视觉投影器组件，不是独立模型，不进入模型列表。"""
    return str(name).lower().startswith("mmproj")

# ------------------------------------------------ 视频生成链路的组件判定
VIDEO_SUBDIR = "生视频"            # models_dir 下存放视频模型的子文件夹（不存在时自动回退顶层扫描）

def video_component_role(path):
    """视频链路组件类型：'video'（扩散主体）/ 'encoder'（文本编码器）/ None（不是视频链路）。

    判定集中在 `sdprofile.video_role`：MiniMax-H3 的张量名是本机实测过的，直接算；
    没实测过的家族（Wan）要额外满足"文件名带线索"，理由见那儿的注释。
    生图扩散模型同样是 kv=0 的裸权重，若不命中视频标记就返回 None，
    避免把 qwen_image / flux 之类当成视频组件。
    """
    s = gguf_structure(path)
    if not s or s[0]:
        return None
    if not s[1]:
        return None
    return sdprofile.video_role(s[1], os.path.basename(path))

def video_scan_dirs(cfg):
    """视频组件的扫描目录：配置的视频目录优先，其次模型目录顶层与其一级子目录。

    生图目录不参与（那里的 kv=0 文件是生图扩散模型，不是视频组件）。
    """
    d = cfg.get("models_dir") or os.path.dirname(cfg.get("model", "")) or "."
    vd = cfg.get("video_model_dir") or os.path.join(d, VIDEO_SUBDIR)
    img_dir = os.path.normpath(cfg.get("image_model_dir")
                               or os.path.join(d, IMAGE_SUBDIR))
    out = []
    for folder in (vd, d):
        np = os.path.normpath(folder) if folder else ""
        if np and os.path.isdir(np) and np != img_dir and np not in out:
            out.append(np)
    try:
        for n in sorted(os.listdir(d)):
            fp = os.path.normpath(os.path.join(d, n))
            if os.path.isdir(fp) and fp != img_dir and fp not in out:
                out.append(fp)
    except Exception:
        pass
    return out

def scan_video_models(cfg):
    """扫描视频链路组件，返回 (扩散主体列表, 文本编码器列表)。"""
    vids, encs = [], []
    for folder in video_scan_dirs(cfg):
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().endswith(".gguf") and not _is_mmproj(n))
        except Exception:
            continue
        for n in names:
            p = os.path.join(folder, n)
            role = video_component_role(p)
            if role == "video":
                vids.append(p)
            elif role == "encoder":
                encs.append(p)
    return vids, encs

def scan_models(cfg):
    """扫描模型目录：返回 (目录, 聊天模型列表, 生图扩散模型列表)。

    支持两种摆放方式：顶层 .gguf，或"每个模型一个子文件夹"（模型本体 + 配套 mmproj）。
    mmproj 文件一律不作为模型（此前它被误识别成独立模型）。
    生图子文件夹单独处理：扩散模型 → 生图列表；带配套 mmproj 的组件（如 Qwen3VL-8B）
    → 并入聊天列表（可看图）。
    """
    d = cfg.get("models_dir") or os.path.dirname(cfg.get("model", "")) or "."
    img_dir = os.path.normpath(cfg.get("image_model_dir")
                               or os.path.join(d, IMAGE_SUBDIR))

    def _ggufs(folder):
        try:
            return sorted(n for n in os.listdir(folder) if n.lower().endswith(".gguf"))
        except Exception:
            return []

    def _chat_ok(folder, n):
        """进入聊天列表的条件：不是 mmproj，且 GGUF 带元数据（视频链路组件是 kv=0 的裸权重）。"""
        return not _is_mmproj(n) and gguf_is_chat_capable(os.path.join(folder, n))

    chat, image = [], []
    # 顶层 .gguf
    for n in _ggufs(d):
        if _chat_ok(d, n):
            chat.append(os.path.join(d, n))
    # 一级子目录（每个子目录视为一个模型的"家"；生图目录除外）
    try:
        subs = [n for n in sorted(os.listdir(d))
                if os.path.isdir(os.path.join(d, n))
                and os.path.normpath(os.path.join(d, n)) != img_dir]
    except Exception:
        subs = []
    for s in subs:
        folder = os.path.join(d, s)
        for n in _ggufs(folder):
            if _chat_ok(folder, n):
                chat.append(os.path.join(d, s, n))
    # 生图目录：扩散模型 + 可配对 mmproj 的视觉组件。
    # 这里不再只盯 .gguf —— SDXL / Flux / SD3 的社区权重常常是单个 .safetensors/.ckpt，
    # 引擎用 `-m` 直接吃（见 sdprofile 的 main_flag）。配套件（vae / clip / t5 / lora …）
    # 由 is_model_file 的名字判据摘出去，带元数据的 .gguf 是语言模型（可当聊天模型）。
    pairs = find_vl_pairs(cfg)
    for n in sorted(os.listdir(img_dir) if os.path.isdir(img_dir) else []):
        p = os.path.join(img_dir, n)
        if not os.path.isfile(p) or _is_mmproj(n):
            continue
        if not str(n).lower().endswith(sdprofile.DIFFUSION_EXTS):
            continue
        if video_component_role(p) == "video":
            continue
        if is_diffusion_file(p):
            image.append(p)
        elif p in pairs and gguf_is_chat_capable(p):
            chat.append(p)
    return d, chat, image

def _norm_model_name(s):
    """模型 / mmproj 文件名归一化：小写、非字母数字→连字符、
    并剥离连续结尾的量化/精度/格式后缀（如 -Q4_K_M.gguf / -F16.gguf）。"""
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    s = re.sub(r"(?:-(?:gguf|f16|f32|bf16|q\d-k-m|q\d-k-s|q\d-k|q\d-0|q\d-1|iq\d-xs))+$",
               "", s)
    return s.strip("-")

def find_vl_pairs(cfg):
    """扫描模型目录（顶层 + 每个一级子目录），返回 {可看图模型完整路径: mmproj 完整路径}。

    配对规则：同目录内 mmproj-*.gguf 与模型文件名，去掉 mmproj 前缀与量化/精度后缀
    归一化后相等或互为包含即视为配对。
    """
    d = cfg.get("models_dir") or os.path.dirname(cfg.get("model", "")) or "."
    img_dir = cfg.get("image_model_dir") or os.path.join(d, IMAGE_SUBDIR)
    folders = [d]
    try:
        for n in sorted(os.listdir(d)):
            fp = os.path.join(d, n)
            if os.path.isdir(fp):
                folders.append(fp)
    except Exception:
        pass
    if (os.path.isdir(img_dir)
            and os.path.normpath(img_dir) not in [os.path.normpath(f) for f in folders]):
        folders.append(img_dir)
    pairs = {}
    for folder in folders:
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().endswith(".gguf"))
        except Exception:
            continue
        projs = [n for n in names if n.lower().startswith("mmproj")]
        for m in names:
            # 跳过 mmproj 与"真扩散主体"。这里必须用带结构判据的 is_diffusion_file：
            # 纯名字的 is_image_diffusion 认不出 Qwen3VL 这类文本编码器（它没有元数据以外
            # 的特征），一旦把它跳掉，mmproj 配对就整条断掉 —— 生图的 --llm_vision
            # 和"能不能看图"都靠这张表（原来靠文件名写死才碰巧没出问题）。
            if m.lower().startswith("mmproj") or is_diffusion_file(os.path.join(folder, m)):
                continue
            if not gguf_is_chat_capable(os.path.join(folder, m)):
                continue
            mn = _norm_model_name(m)
            for pj in projs:
                pn = _norm_model_name(pj)
                if pn.startswith("mmproj"):
                    pn = pn[len("mmproj"):].strip("-")
                if pn and (pn == mn or pn in mn or mn in pn):
                    pairs[os.path.join(folder, m)] = os.path.join(folder, pj)
                    break
    return pairs

def is_vl_model(cfg, path):
    """该模型是否可看图（有 mmproj 记录，或同目录存在可自动配对的 mmproj）。"""
    b = os.path.basename(path)
    proj = (cfg.get("model_mmproj") or {}).get(b)
    if proj and os.path.isfile(proj):
        return True
    return path in find_vl_pairs(cfg)

def plan_tidy(cfg):
    """生成"模型文件夹整理"计划（只对名称可明确配对的文件动手）。

    返回 (moves, unpaired)：
      moves    —— [(src, dst_dir, 类型)] 建议移动的文件及其目标文件夹
      unpaired —— 无法判断归属的 mmproj（一律不动，交用户手动处理）
    规则：仅处理 models_dir 顶层散落的模型与其配对 mmproj（生图目录不参与），
    把"模型 + 它的 mmproj"归入以模型名（去扩展名）命名的子文件夹。
    """
    d = cfg.get("models_dir") or os.path.dirname(cfg.get("model", "")) or "."
    try:
        names = sorted(n for n in os.listdir(d) if n.lower().endswith(".gguf"))
    except Exception:
        return [], []
    # 视频链路组件（kv=0 的裸权重）不参与整理：它们不属于"模型 + mmproj"这套组织方式，
    # 由视频功能按目录自行识别，整理时保持原位。
    models = [n for n in names
              if not _is_mmproj(n) and gguf_is_chat_capable(os.path.join(d, n))]
    projs = [n for n in names if _is_mmproj(n)]
    moves, unpaired, paired = [], list(projs), set()
    for m in models:
        mn = _norm_model_name(m)
        for pj in list(unpaired):
            pn = _norm_model_name(pj)
            if pn.startswith("mmproj"):
                pn = pn[len("mmproj"):].strip("-")
            if pn and (pn == mn or pn in mn or mn in pn):
                folder = os.path.join(d, os.path.splitext(m)[0])
                moves.append((os.path.join(d, m), folder, "模型"))
                moves.append((os.path.join(d, pj), folder, "mmproj"))
                unpaired.remove(pj)
                paired.add(m)
                break
    for m in models:
        if m not in paired:
            moves.append((os.path.join(d, m),
                          os.path.join(d, os.path.splitext(m)[0]),
                          "模型（未找到 mmproj）"))
    return moves, unpaired

def apply_tidy(moves):
    """执行整理计划（同盘移动，瞬时完成）；返回 (成功数, 失败清单)。"""
    ok, fails = 0, []
    for src, dst_dir, _ in moves:
        try:
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(src))
            if os.path.exists(dst):
                fails.append((src, "目标已存在，跳过"))
                continue
            os.rename(src, dst)
            ok += 1
        except Exception as e:
            fails.append((src, str(e)))
    return ok, fails

def clean_alias(text):
    """【暂时没用，后续可能启用】从模型回复中清洗别名。"""
    if not text:
        return None
    line = text.strip().splitlines()[0].strip()
    line = line.strip("\"'` ")
    m = re.search(r"[a-z0-9][a-z0-9\-\.]{3,23}", line.lower())
    if not m:
        return None
    alias = m.group(0).strip("-.")
    return alias or None

def resolve_model(cfg, name, chat=None):
    """把请求里的模型名解析为聊天模型列表中的一项（模糊/重定向匹配）。

    依次：精确文件名 → 去 .gguf → 别名表 → 大小写不敏感子串 → 相似度兜底。
    无匹配返回 None（调用方回退：上次模型 → 列表第一个）。
    chat：调用方若已 scan_models 过可直接传入，省一次重复扫盘
    （proxy 的 ensure_model 每请求都会走到这里）。
    """
    if chat is None:
        _d, chat, _img = scan_models(cfg)
    if not chat:
        return None
    keys = {os.path.basename(p): p for p in chat}

    def _norm(s):
        return re.sub(r"[\s_.]+", "-", str(s).lower()).strip("-")

    n = _norm(name)
    if not n or n in ("local", "default", "auto"):
        return None
    # 1) 精确文件名（忽略大小写/后缀）
    for b, p in keys.items():
        if _norm(b) == n or _norm(b.removesuffix(".gguf")) == n:
            return p
    # 2) 别名表（model_aliases: 文件名 -> 简称）
    aliases = cfg.get("model_aliases") or {}
    for b, p in keys.items():
        if _norm(aliases.get(b, "")) == n:
            return p
    # 2b) 页面显示简称（display_name，兼容动态生成的简称）
    for b, p in keys.items():
        if _norm(display_name(cfg, p)) == n:
            return p
    # 3) 归一化子串（空格/下划线/点统一为连字符，双向，取最短候选）
    cands = []
    for b, p in keys.items():
        bl = _norm(b.removesuffix(".gguf"))
        if n in bl or bl in n:
            cands.append((len(bl), p))
    if cands:
        return sorted(cands, key=lambda x: x[0])[0][1]
    # 4) 局部相似度兜底（滑动窗口，对"短名 vs 长文件名"友好）
    def _partial(a, b):
        if len(a) > len(b):
            a, b = b, a
        w = max(1, len(a))
        best = 0.0
        for i in range(max(1, len(b) - w + 1)):
            best = max(best, difflib.SequenceMatcher(None, a, b[i:i + w]).ratio())
        return best
    best, score = None, 0.0
    for b, p in keys.items():
        r = _partial(n, _norm(b.removesuffix(".gguf")))
        if r > score:
            best, score = p, r
    return best if score >= 0.8 else None
