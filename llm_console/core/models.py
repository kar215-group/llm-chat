# -*- coding: utf-8 -*-
"""llm_console.core.models — 模型库：扫描、分类（聊天/生图/生视频）、**可用性（引擎 × 模型）**、mmproj 配对、简称与模糊解析、文件夹整理"""

import difflib
import os
import re

from . import engine_install, providers, sdprofile, secrets
from .config import APP_DIR
from .gguf import gguf_is_chat_capable, gguf_structure


def make_alias(filename):
    """从 GGUF 文件名自动生成简短别名（纯代码规则，不询问模型）。

    例：Qwen3.8-27B-UD-Q3_K_XL.gguf -> qwen3.8-27b-q3
        超长的社区微调名按**连字符**截到 30 字符以内。

    ⚠ 这个上限管的是**模型菜单**（点开后的列表），那里一行放得下 30 字。
    **主页面顶栏**另有更严的上限（`TOPBAR_ALIAS_MAX`），走 `short_alias()` ——
    别把顶栏的限长做到这里来，那会把菜单里的名字也一起砍短（W 2026-10-03）。
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
    # 超长别名（社区微调名）：按连字符截断，避免列表行被撑爆
    if len(s) > 30:
        cut = s[:30]
        s = cut[:cut.rfind("-")] if "-" in cut else cut
    return s or filename


# 主页面顶栏的别名上限：顶栏左边是模型名、右边挤着 5 个按钮，预算只有这么多。
# 实测（2026-10-03，DPI-aware 严格档）：顶栏右侧 5 个按钮各要 120px，左侧状态灯
# 约 80px，模型名超过约 22 字就会开始挤右侧按钮。**只作用于顶栏**。
TOPBAR_ALIAS_MAX = 22


def short_alias(name, limit=TOPBAR_ALIAS_MAX):
    """把显示名再压到 `limit` 字以内 —— **只给主页面顶栏用**。

    与 `make_alias` 分开是刻意的：菜单里一行放得下 30 字，砍短了反而认不出模型
    （W 2026-10-03）。截断规则同样是"切在连字符前"，实在切不动才加省略号。
    """
    s = str(name or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if "-" in cut[8:]:                 # 中段还有连字符：保住"家族-规模-量化"三段
        return cut[:cut.rfind("-")]
    return cut.rstrip("-") + "…"



def display_name(cfg, path):
    """页面显示名：显式别名 > 代码自动生成的简称。"""
    key = os.path.basename(path)
    alias = (cfg.get("model_aliases") or {}).get(key)
    if alias:
        return alias
    return make_alias(key)

IMAGE_SUBDIR = "生图"             # models_dir 下存放生图大模型的子文件夹名

# 生图目录里"不是模型本体"的文件名特征。原来这里写死了 `qwen3vl`（开发机那个文本编码器
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


def extra_sources(cfg):
    """「手动定向模型」加入的来源（文件夹或 `.gguf` 完整路径），只保留**仍存在**的。

    这些路径只登记在配置里（`extra_models`），**不移动任何文件**；扫描时按"额外的模型目录 /
    额外的模型文件"处理（见 `scan_models` / `video_scan_dirs`）。已删掉的项在这里滤掉，
    免得清单越攒越脏。
    """
    raw = (cfg or {}).get("extra_models")
    if not isinstance(raw, (list, tuple)):
        return []                      # 配置被写坏（不是列表）时当没有，别让整条扫描一起炸
    out = []
    for p in raw:
        p = os.path.normpath(str(p or "").strip())
        if p and p not in out and (os.path.isfile(p) or os.path.isdir(p)):
            out.append(p)
    return out


def extra_scan_dirs(cfg):
    """额外来源覆盖到的**目录**（文件夹本身，或某个 `.gguf` 所在目录），供按目录扫描的路径复用。"""
    out = []
    for p in extra_sources(cfg):
        d = p if os.path.isdir(p) else os.path.dirname(p)
        d = os.path.normpath(d)
        if d and d not in out:
            out.append(d)
    return out


def dir_has_any_gguf(path, subdirs=1):
    """这个目录（含 `subdirs` 层子目录）里有没有任何 `.gguf` —— **只看文件名，不读 GGUF 头**。

    廉价判据，给"当前指的这个模型目录还有没有必要去找别的"用：有就说明用户指的这条路
    并非空指（哪怕里面的 gguf 是视频组件 / mmproj，也该由各自的链路去认，不该整体搬走）。
    """
    p = str(path or "").strip()
    if not p or not os.path.isdir(p):
        return False
    if any(str(n).lower().endswith(".gguf") for n in _listdir(p)):
        return True
    if subdirs <= 0:
        return False
    for n in _listdir(p):
        sub = os.path.join(p, n)
        if os.path.isdir(sub) and dir_has_any_gguf(sub, subdirs - 1):
            return True
    return False


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except Exception:
        return []


def _expand_dirs(root, max_depth=3):
    """"模型来源目录"展开成要扫描的目录清单：它自己 + 子目录（深度 ≤ max_depth）。

    W 2026-10-08 报的检测缺口：手动定向的文件夹此前只扫**顶层** —— 而"每个模型一个
    子文件夹"（本体 + mmproj 同放）是产品认可的两种摆放方式之一，于是
    `D:\\llm modle\\Qwen3.6-35B\\*.gguf` 这类模型在"手动定向"路径下全漏
    （models_dir 主路径本来就扫一级子目录，两条路径行为不一致）。
    `D:\\aaa models\\<组织>\\<模型>\\*.gguf` 这类两层的目录也要认，所以深度给 3。
    过滤与 `auto_locate_models_dir` 同一口径：跳过 `.` 开头的隐藏目录与 `*.old` / `*.new`。
    """
    out = []
    root = os.path.normpath(str(root or ""))
    if not root or not os.path.isdir(root):
        return out
    out.append(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dp, dns, _fns in os.walk(root):
        if dp.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dns[:] = []                    # 到深度上限：不再往下走
        dns[:] = sorted(d for d in dns
                        if not d.startswith(".")
                        and not d.endswith((".old", ".new")))
        np = os.path.normpath(dp)
        if np != root and np not in out:
            out.append(np)
    return out


def auto_locate_models_dir(cfg, app_dir=None, max_depth=3):
    """模型目录的「自动定向」：在程序目录附近找一个**确实装着模型**的文件夹，返回路径或空串。

    为什么需要：`models_dir` 的默认值是 `<程序目录>/models`，而模型的来源只有它 + 「手动定向
    模型」那几项；用户把模型放在别的文件夹（程序目录下的 `GGUF/`、或自己另建的目录）时，
    扫一百次也扫不到。引擎那一侧有 `engine_install.auto_locate`，模型这一侧之前没有 —— 补上。

    判据（**只读文件名，不读 GGUF 头**）：在 `app_dir` 下找"直接子项里有 `.gguf`"的目录，
    优先目录名叫 `models` 的，其次离根近的、再按路径名序；深度上限 `max_depth`。
    **只读**：写不写回 `models_dir` 由调用方决定；找不到返回空串（不猜）。
    """
    base = os.path.abspath(app_dir or APP_DIR)
    if not os.path.isdir(base):
        return ""
    cur = os.path.normpath(str((cfg or {}).get("models_dir") or ""))
    cands = []
    for root, dirs, files in os.walk(base):
        rel = os.path.relpath(root, base)
        depth = 0 if rel == os.curdir else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirs[:] = []                      # 不再往下走，但这一层仍要看
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and not d.endswith((".old", ".new")))
        np = os.path.normpath(root)
        if np != cur and any(str(f).lower().endswith(".gguf") for f in files):
            cands.append((0 if os.path.basename(np).lower() == "models" else 1, depth, np))
    if not cands:
        return ""
    cands.sort()
    return cands[0][2]

# ------------------------------------------------ 视频生成链路的组件判定
VIDEO_SUBDIR = "生视频"            # models_dir 下存放视频模型的子文件夹（不存在时自动回退顶层扫描）

def video_component_role(path):
    """视频链路组件类型：'video'（扩散主体）/ 'encoder'（文本编码器）/ None（不是视频链路）。

    判定集中在 `sdprofile.video_role`：MiniMax-H3 的张量名是开发机实测过的，直接算；
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
        for dd in _expand_dirs(d):
            if dd != img_dir and dd not in out:
                out.append(dd)
    except Exception:
        pass
    # 手动定向加入的目录也按"视频组件扫描目录"处理（W 2026-10-05）；
    # 2026-10-08 起展开到子目录（深度 ≤ 3）——与文件名来源同一套递归口径
    for d2 in extra_scan_dirs(cfg):
        for dd in _expand_dirs(d2):
            if dd != img_dir and dd not in out:
                out.append(dd)
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
    # 手动定向加入的**单个文件**单独判一次（文件夹来源已在上面的目录扫描里覆盖）
    for p in extra_sources(cfg):
        if not os.path.isfile(p) or _is_mmproj(os.path.basename(p)):
            continue
        role = video_component_role(p)
        if role == "video" and p not in vids:
            vids.append(p)
        elif role == "encoder" and p not in encs:
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
    pairs = find_vl_pairs(cfg)

    def _harvest(root):
        """把一棵"模型树"收进两个清单 —— models_dir 与手动定向的来源**共用这一处**
        （W 2026-10-08：此前手动定向只扫顶层，"每个模型一个子目录"的摆法整批漏检，
        两条路径行为不一致；现在连同更深的组织嵌套一起递归，深度见 _expand_dirs）。

        每一层目录里的规则：
        · 可聊天 gguf（非 mmproj、带元数据）→ chat
        · 扩散权重（.gguf / .safetensors / .ckpt…，且不是视频链路组件）→ image
        生图目录（img_dir）除外 —— 它按"扩散主体 + 配对可看图组件"单独处理（下面一段）。
        """
        img_norm = os.path.normpath(img_dir)
        for folder in _expand_dirs(root):
            if os.path.normpath(folder) == img_norm:
                continue
            for n in _ggufs(folder):
                if _is_mmproj(n):
                    continue
                fp = os.path.join(folder, n)
                if fp in chat or fp in image:
                    continue
                if is_diffusion_file(fp):
                    # 视频链路的组件（主体 / 文本编码器）不进生图清单：它们是 kv=0 的
                    # 裸权重，拿去生图必然失败，各自归 scan_video_models 的清单
                    if video_component_role(fp) is None:
                        image.append(fp)
                elif _chat_ok(folder, n):
                    chat.append(fp)
            # 非 gguf 的扩散单文件（生图的 .safetensors / .ckpt，引擎用 -m 直接吃）
            for n in _listdir(folder):
                fp = os.path.join(folder, n)
                if not os.path.isfile(fp) or str(n).lower().endswith(".gguf"):
                    continue
                if not str(n).lower().endswith(sdprofile.DIFFUSION_EXTS):
                    continue
                if fp not in image and is_diffusion_file(fp):
                    image.append(fp)

    _harvest(d)
    # 生图目录：扩散模型 + 可配对 mmproj 的视觉组件。
    # 这里不再只盯 .gguf —— SDXL / Flux / SD3 的社区权重常常是单个 .safetensors/.ckpt，
    # 引擎用 `-m` 直接吃（见 sdprofile 的 main_flag）。配套件（vae / clip / t5 / lora …）
    # 由 is_model_file 的名字判据摘出去，带元数据的 .gguf 是语言模型（可当聊天模型）。
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
    # 手动定向加入的来源（W 2026-10-05）：文件夹按"一棵模型树"并入（与 models_dir
    # 同一套 _harvest），单文件按它自己判。**只登记、不动文件**。
    for p in extra_sources(cfg):
        if os.path.isfile(p):
            if is_diffusion_file(p):
                if video_component_role(p) is None and p not in image:
                    image.append(p)
            elif (not _is_mmproj(os.path.basename(p))
                  and str(p).lower().endswith(".gguf")
                  and gguf_is_chat_capable(p) and p not in chat):
                chat.append(p)
            continue
        _harvest(p)
    return d, chat, image

def has_local_chat(cfg):
    """有没有**能转发的本地文本模型** —— 这是 API 代理能用起来的前提。

    判据是「llama 引擎就位 **且** 目录里有能聊天的模型」两件事都成立：只有权重没有引擎时，
    代理连上去会在换载那一步失败（agent 拿不到回答，而界面显示"运行中"）。
    没有本地聊天模型时这个代理就是个空壳：所以「启用本地模型 API」既默认关、也不给打开
    （判据只有这一处，界面与启动都问它；启动那条路还要过「自启动」开关，2026-10-07）。
    实测开发机（5 个模型）`scan_models` 约 9~11ms，放在启动路径上付得起。
    """
    try:
        _d, chat, _i = scan_models(cfg)
    except Exception:
        return False
    if not chat:
        return False
    try:
        return bool(engine_install.configured_exe("llama", cfg))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 「现在就能用」—— 引擎存在 × 模型存在（坑 150）
# ---------------------------------------------------------------------------
# 为什么要有这一层：本地有**三个**能力（文本 / 生图 / 生视频），只依赖**两个**引擎
# （llama.cpp 与 sd.cpp）。原来"能不能用"只认「llama 引擎 + 聊天模型」，于是只想用本地
# 生图的用户被判成"什么都没配好"：引导第一页催他下 llama 引擎、主页面挂"无可用模型"、
# 一键诊断报「两条路都还没通」。
#
# 口径只写在这里，主页面菜单、引导第一屏、一键诊断、一次性自动选中**四处共用**
# （坑 128：同一事实各写一遍，迟早各走各的偏）。一个本地模型"能用" =
# **它自己那个引擎的文件真的存在** 且模型文件在；引擎的判据走
# `engine_install.configured_exe`（一处），不另写 isfile。
KIND_ENGINE = {"chat": "llama", "image": "sd", "video": "sd", "media": "sd"}
# 直接说引擎名也认（`engine_ready(cfg, "sd")`），免得调用点把 "media" / "image" 记混。
ENGINE_ALIAS = {"llama": "llama", "sd": "sd"}
ENGINE_LABEL = {"llama": "llama 引擎", "sd": "sd 引擎"}


def engine_ready(cfg, kind):
    """某个能力对应的引擎是不是**真的就位**（配置指路 + 那个可执行文件存在）。

    `kind` 用本模块那套词表（chat / image / video）；`"media"` 是生图与生视频的合称
    （两者共用 sd.cpp，判据相同），也可以直接写引擎名 `"llama"` / `"sd"`。
    """
    k = str(kind or "")
    key = KIND_ENGINE.get(k) or ENGINE_ALIAS.get(k)
    if not key:
        return False
    try:
        return bool(engine_install.configured_exe(key, cfg))
    except Exception:
        return False


def usable_local(cfg):
    """本地三类"现在就能用"的模型 → `{"dir", "chat", "image", "video"}`。

    引擎没就位的那一类给**空列表**（不是"全部照给、界面上自己判断"）—— 判据与展示都在
    这一处，调用方拿到的就是可以直接显示的清单。

    只管主页面菜单与自动选中这两件显示层的事：设置页的模型清单、扫描补全、8081 代理的
    模型解析照旧认得全部文件（沿用 `localmodels.hidden_set` 那条"不进主菜单只是显示层
    的事、不等于这个模型不存在"的口径）。
    """
    try:
        d, chat, images = scan_models(cfg)
    except Exception:
        d, chat, images = str((cfg or {}).get("models_dir") or ""), [], []
    try:
        vids, _encs = scan_video_models(cfg)
    except Exception:
        vids = []
    ok = {"chat": engine_ready(cfg, "chat"),
          "image": engine_ready(cfg, "image"),
          "video": engine_ready(cfg, "video")}
    return {"dir": d,
            "chat": list(chat) if ok["chat"] else [],
            "image": list(images) if ok["image"] else [],
            "video": list(vids) if ok["video"] else []}


def cloud_usable(cfg, kind):
    """云端某能力下"真的发得出去"的模型：[(provider_id, 模型名)]。

    判据 = 服务商启用 **且** 那家填过密钥。没密钥的模型选上去也发不出去，不该算"可用"
    （`_cloud_ready` 是同一口径的"有没有一家就绪"版）。
    """
    out = []
    try:
        listed = providers.cloud_models(cfg, kind)
    except Exception:
        listed = []
    for pid, _name, m in listed:
        try:
            if secrets.has_api_key(pid):
                out.append((pid, m))
        except Exception:
            pass
    return out


def first_usable(cfg):
    """第一个"现在就能用"的模型 → `{"id", "kind", "provider"}`；一个都没有返回 None。

    顺序：本地文本 → 本地生图 → 本地生视频 → 云端文本 → 云端生图 → 云端生视频。
    本地排前面是因为它不要密钥、没有配额与费用（与原 `main()` 里"从目录里挑一个能聊的
    顶上"的偏好一致，只是把"能聊的"扩成"能用的"）。
    """
    loc = usable_local(cfg)
    for kind in ("chat", "image", "video"):
        if loc[kind]:
            return {"id": loc[kind][0], "kind": kind, "provider": providers.LOCAL}
    for kind, cloud_kind in (("chat", providers.KIND_TEXT),
                             ("image", providers.KIND_IMAGE),
                             ("video", providers.KIND_VIDEO)):
        got = cloud_usable(cfg, cloud_kind)
        if got:
            pid, model = got[0]
            return {"id": providers.make_cloud_id(pid, model), "kind": kind,
                    "provider": pid}
    return None


def selected_usable(cfg):
    """当前选中的模型是不是"现在就能用"（云端看密钥，本地看引擎 + 文件还在）。

    主页面顶栏「（无可用模型）」与自动选中那一步都问它 —— 与 `first_usable` 同一套判据，
    所以"顶栏说有模型"和"程序肯自动切过去"永远不会各说各话（坑 128）。

    **带一条快路径**：选中的文件还在、且它那一类对应的引擎就位，就直接答 True ——
    不扫盘。兜底轮询每 10 秒问一次，每次都去读一遍 GGUF 头是白花的开销（坑 4 的通用
    纪律）；快路径答 False 时才走完整的 `usable_local`（真正可疑的现场才付这个代价）。
    """
    cur = str((cfg or {}).get("model") or "")
    if not cur:
        return False
    if providers.is_cloud(cfg):
        pid, model = providers.split_cloud_id(cur) or ("", "")
        if not (pid and model):
            return False
        try:
            return bool(secrets.has_api_key(pid))
        except Exception:
            return False
    if os.path.isfile(cur) and engine_ready(cfg, (cfg or {}).get("model_kind") or "chat"):
        return True
    loc = usable_local(cfg)
    want = os.path.normcase(os.path.normpath(cur))
    return any(os.path.normcase(os.path.normpath(p)) == want
               for kind in ("chat", "image", "video") for p in loc[kind])


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
    归一化后相等或互为包含即视为配对；名字里不带模型信息的投影器（`mmproj-BF16.gguf`）
    在同目录只有一个能聊天的模型时也认给它。
    """
    d = cfg.get("models_dir") or os.path.dirname(cfg.get("model", "")) or "."
    img_dir = cfg.get("image_model_dir") or os.path.join(d, IMAGE_SUBDIR)
    folders = _expand_dirs(d)
    if (os.path.isdir(img_dir)
            and os.path.normpath(img_dir) not in [os.path.normpath(f) for f in folders]):
        folders.append(img_dir)
    # 手动定向加入的目录也要参与 mmproj 配对（否则那些目录里的可看图模型认不出投影器）；
    # 2026-10-08 起展开到子目录 —— 与 scan_models / video_scan_dirs 同一套递归口径
    for d2 in extra_scan_dirs(cfg):
        for dd in _expand_dirs(d2):
            if dd not in [os.path.normpath(f) for f in folders]:
                folders.append(dd)
    pairs = {}
    for folder in folders:
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().endswith(".gguf"))
        except Exception:
            continue

        def _proj_tail(pj):
            """投影器名字里"属于哪个模型"的那一段：剥掉 mmproj 前缀与量化/精度后缀。
            剥完是空的 = 这个名字一点模型信息都没带（`mmproj-BF16.gguf` 就是这种）。"""
            pn = _norm_model_name(pj)
            if pn.startswith("mmproj"):
                pn = pn[len("mmproj"):].strip("-")
            return pn

        # 同目录里"能当聊天模型"的文件。跳过 mmproj 与"真扩散主体"：必须用带结构判据的
        # is_diffusion_file —— 纯名字的 is_image_diffusion 认不出 Qwen3VL 这类文本编码器
        # （它没有元数据以外的特征），一旦把它跳掉，mmproj 配对就整条断掉 —— 生图的
        # --llm_vision 和"能不能看图"都靠这张表（原来靠文件名写死才碰巧没出问题）。
        chats = []
        for m in names:
            if m.lower().startswith("mmproj"):
                continue
            fp = os.path.join(folder, m)
            if is_diffusion_file(fp) or not gguf_is_chat_capable(fp):
                continue
            chats.append(m)

        projs = [n for n in names if n.lower().startswith("mmproj")]
        for m in chats:
            mn = _norm_model_name(m)
            for pj in projs:
                pn = _proj_tail(pj)
                if pn and (pn == mn or pn in mn or mn in pn):
                    pairs[os.path.join(folder, m)] = os.path.join(folder, pj)
                    break
        # 名字里没有模型信息的投影器（`mmproj-BF16.gguf` 这种）无法按名字归属。只有当
        # **整个目录里只有一个能聊天的模型**、且它还没配上投影器时，才认这个投影器是它的；
        # 有多个候选就无从判断，宁可留空让人在设置页手工指定，也不能猜。
        # （真机 W 的 Qwen3.8-27B 就是"模型名 + mmproj-BF16"这个形态，原来一律判成纯文本。）
        unnamed = [pj for pj in projs if not _proj_tail(pj)]
        if len(unnamed) == 1:
            left = [m for m in chats if os.path.join(folder, m) not in pairs]
            if len(left) == 1:
                pairs[os.path.join(folder, left[0])] = os.path.join(folder, unnamed[0])
    return pairs

def is_vl_model(cfg, path):
    """该模型是否可看图（有 mmproj 记录，或同目录存在可自动配对的 mmproj）。"""
    b = os.path.basename(path)
    proj = (cfg.get("model_mmproj") or {}).get(b)
    if proj and os.path.isfile(proj):
        return True
    return path in find_vl_pairs(cfg)

# ---------------------------------------------------------------------------
# 「一键整理模型文件夹」（2026-10-08 W 定，整段重写）
# ---------------------------------------------------------------------------
# 结构：<根目录>\model\<文本模型|生图模型|生视频模型>\
#   · 类内**平铺**（不搞"每个模型一个子夹"）；
#   · **共用同一文本编码器的多个模型**（如 qwen-image 2.1 的两个量化版本）合成同一个
#     子文件夹，子夹名取模型名公共前缀；那台编码器与配套 VAE 一并放进去（编码器自己的
#     mmproj 也跟上 —— 参考图编辑的 `--llm_vision` 靠它）。
# 边界（W 定"暂仅处理该场景"）：只处理"模型都放在同一个目录"的情形 —— models_dir 里
# 没有模型就看「手动定向来源」；检测到**多个**装着模型的目录（模型分散在多个文件夹）
# 时一个文件都不动，由界面给出说明。
# 纪律：只移动不删除；名称无法判断归属的 mmproj 保持原位（交用户手动处理）。
TIDY_DIR = "model"
TIDY_CLASS_DIR = {"chat": "文本模型", "image": "生图模型", "video": "生视频模型"}

_BAD_FS_CHARS = '<>:"/\\|?*'


def _safe_name(s):
    """文件夹名净化：Windows 不允许的字符换成下划线（模型名里本来极少出现）。"""
    return "".join("_" if c in _BAD_FS_CHARS else c for c in str(s or "")).strip(" .")


def _tidy_files(root):
    """整理候选项：root 下（含子目录，深度口径与 `_expand_dirs` 一致）的模型类文件。

    廉价初筛（只看扩展名）：`.gguf` + 扩散权重那几种（safetensors / sft / ckpt / pt / bin）。
    真正的角色判定在后面（读 GGUF 头 / 认族），认不出的不动。
    """
    out = []
    for folder in _expand_dirs(root):
        for n in _listdir(folder):
            fp = os.path.join(folder, n)
            if os.path.isfile(fp) and str(n).lower().endswith(sdprofile.DIFFUSION_EXTS):
                out.append(fp)
    return out


def _tidy_root(cfg):
    r""""模型所在目录"：整理只认**唯一一个**装着模型的目录。

    → (root, status, roots)
      · status="ok"（唯一根，root 有效）；
      · status="empty"（没有任何装着模型的目录 —— 没东西可整理）；
      · status="scattered"（两个以上装着模型的目录 —— "模型分散在多个文件夹"这个
        特殊情况，一个文件都不动，界面照实列出检测到的目录）。
    判据顺序（W 2026-10-08 定）：models_dir 里有模型就用它；没有就看「手动定向来源」
    （文件夹本身，或单个 .gguf 所在目录）。内层根并入外层 —— 用户同时登记了
    `D:\models` 与 `D:\models\生图` 时不当作"分散"。
    """
    roots = []

    def _add(p):
        np = os.path.normpath(str(p or ""))
        if np and os.path.isdir(np) and _tidy_files(np) and np not in roots:
            roots.append(np)

    d = str((cfg or {}).get("models_dir") or "").strip()
    if os.path.isdir(d):
        _add(d)
    for p in extra_sources(cfg):
        if os.path.isdir(p):
            _add(p)
        elif os.path.isfile(p):
            _add(os.path.dirname(p))       # 单文件来源：它所在的目录
    keep = []
    for r in sorted(roots, key=len):       # 外层在前：内层根并入外层
        # 按 normcase 比：Windows 上 D:\Models 与 d:\models 是同一个目录，
        # 不归一就会被当成"两个来源"、误报"分散"
        rc = os.path.normcase(r)
        if not any(rc == os.path.normcase(k)
                   or rc.startswith(os.path.normcase(k) + os.sep) for k in keep):
            keep.append(r)
    if not keep:
        return "", "empty", []
    if len(keep) > 1:
        return "", "scattered", keep
    return keep[0], "ok", keep


def _tidy_group_name(models_, enc=""):
    """共享编码器那一组子文件夹的名字：模型名（去扩展名）的公共前缀，切在最后一个
    分隔符上（`qwen_image_2.1-Q5_0` + `-Q6_K` → `qwen_image_2.1`）。前缀太短看不清
    就退到编码器名，再不行给个通用名（正常现场走不到这两步）。"""
    stems = [os.path.splitext(os.path.basename(p))[0] for p in models_]
    pre = os.path.commonprefix(stems) if stems else ""
    cut = max([pre.rfind(c) for c in ("-", "_", ".", " ")] or [-1])
    if cut >= 3:
        pre = pre[:cut]
    pre = _safe_name(pre.strip("-_."))
    if len(pre) >= 3:
        return pre
    e = _safe_name(os.path.splitext(os.path.basename(enc or ""))[0])
    return e or "共享编码器组"


def plan_tidy(cfg):
    """生成"模型文件夹整理"计划 → (moves, unpaired, info)。

    moves    —— [(src, dst_dir, 标签)]：要移动的文件与目标目录（标签给预览窗分行）
    unpaired —— 名称无法判断归属的 mmproj（一律不动，交用户手动处理）
    info     —— {"status", "root", "roots"}（见 `_tidy_root`）
    结构与边界见本节开头的注释；角色判定复用扫描 / 发送链路同一批判据
    （`is_diffusion_file` / `video_component_role` / `find_vl_pairs` / `media.resolve_*`），
    这里的"认"与菜单、发送侧永远是一个说法。
    """
    root, status, roots = _tidy_root(cfg)
    if status != "ok":
        return [], [], {"status": status, "root": "", "roots": roots}
    model_dir = os.path.join(root, TIDY_DIR)
    files = _tidy_files(root)
    fs = set(os.path.normcase(os.path.normpath(p)) for p in files)
    pairs = find_vl_pairs(cfg)

    # media 在模块级 import 会成环（media → models）⇒ 函数内局部 import：调用发生在
    # 运行期，那时两个模块都已就绪（capability.py 同款做法）。
    from . import media

    def _in_root(p):
        return os.path.normcase(os.path.normpath(str(p or ""))) in fs

    # ---- 1) 角色分派：文本（可聊天 gguf）/ 生图扩散 / 生视频扩散 ----
    # ⚠ 顺序有讲究：视频角色必须先判 —— `is_diffusion_file` 是**生图**的判据
    # （"能不能当生图扩散模型用"），它会把视频主体与视频编码器都排除掉
    # （前者 `... != "video"` 为假，后者 kv=0 走不到语言模型那条排除）。
    text_models, img_models, vid_models = [], [], []
    for fp in files:
        if _is_mmproj(os.path.basename(fp)):
            continue
        role = video_component_role(fp)
        if role == "video":
            vid_models.append(fp)
            continue
        if role == "encoder":
            continue                       # 视频文本编码器：配套件，随主体走
        if is_diffusion_file(fp):
            img_models.append(fp)          # 生图扩散主体（含 .safetensors / .ckpt 单文件）
            continue
        if str(fp).lower().endswith(".gguf") and gguf_is_chat_capable(fp):
            text_models.append(fp)

    # ---- 2) 生图 / 生视频：认配套件（与发送链路同一处判据），按"文本编码器"分组 ----
    def _comps(model, kind):
        try:
            got = (media.resolve_img_files(cfg, model) if kind == "image"
                   else media.resolve_video_files(cfg, model)) or {}
        except Exception:
            return []                      # 认族失败不拦整理：该模型按"没有配套件"处理
        out = []
        for slot, val in got.items():
            p = str(val or "")
            if slot in ("diffusion", "family", "basis", "note", "missing") or not p:
                continue
            if os.path.isfile(p) and _in_root(p):
                out.append((os.path.normpath(p), slot))
        return out

    moves, claimed = [], set()

    def _claim(src, dst, label):
        """登记一条移动（同一个文件只登记一次；已经在目标目录里就不再动）。"""
        key = os.path.normcase(os.path.normpath(src))
        if key in claimed:
            return
        claimed.add(key)
        if os.path.normcase(os.path.normpath(os.path.dirname(src))) \
                == os.path.normcase(os.path.normpath(dst)):
            return                         # 已就位
        moves.append((src, dst, label))

    # ---- 3) 生图 / 生视频分组：**共用同一文本编码器的模型并进同一个组** ----
    comps_by_model = {}
    groups = {"image": [], "video": []}        # [(成员列表, 编码器路径), …]
    for kind, models_ in (("image", img_models), ("video", vid_models)):
        by_enc, order = {}, []
        for m in sorted(models_, key=lambda x: os.path.basename(x).lower()):
            comps = _comps(m, kind)
            comps_by_model[m] = comps
            enc = next((p for p, slot in comps if slot == "llm"), "")
            key = os.path.normcase(os.path.normpath(enc or m))
            if key not in by_enc:
                by_enc[key] = {"enc": enc, "members": []}
                order.append(key)
            by_enc[key]["members"].append(m)
        for key in order:
            groups[kind].append((by_enc[key]["members"], by_enc[key]["enc"]))

    # 编码器（可聊天的 gguf 自己会进"文本模型"清单）不该被当成文本模型搬走 ——
    # 它已经是生图 / 生视频那一组里的配套件了
    companion_paths = {os.path.normcase(os.path.normpath(p))
                       for grps in groups.values()
                       for members, _enc in grps
                       for m in members
                       for p, _slot in comps_by_model.get(m, [])}
    text_models = [m for m in text_models
                   if os.path.normcase(os.path.normpath(m)) not in companion_paths]

    # ---- 4) 文本模型（+ 配对 mmproj）→ model\文本模型\ ----
    t_dir = os.path.join(model_dir, TIDY_CLASS_DIR["chat"])
    for m in sorted(text_models, key=lambda x: os.path.basename(x).lower()):
        pj = pairs.get(m)
        if pj and _in_root(pj):
            _claim(m, t_dir, TIDY_CLASS_DIR["chat"])
            _claim(os.path.normpath(pj), t_dir, "mmproj")
        else:
            _claim(m, t_dir, "%s（未找到 mmproj）" % TIDY_CLASS_DIR["chat"])

    # ---- 5) 生图 / 生视频 → 各自类目录（≥2 个成员共用编码器的组进同一个子夹）----
    for kind, grps in groups.items():
        base = os.path.join(model_dir, TIDY_CLASS_DIR[kind])
        for members, enc in grps:
            target = (os.path.join(base, _tidy_group_name(members, enc))
                      if len(members) >= 2 else base)
            for m in members:
                _claim(m, target, TIDY_CLASS_DIR[kind])
                for p, slot in comps_by_model.get(m, []):
                    if slot == "llm":
                        _claim(p, target, "文本编码器")
                        # 编码器自己的 mmproj（参考图编辑 --llm_vision 要用的那个）也进这一组
                        pj = pairs.get(p)
                        if pj and _in_root(pj):
                            _claim(os.path.normpath(pj), target, "mmproj")
                    else:
                        _claim(p, target, sdprofile.slot_name(slot))

    # ---- 6) 认不出归属的 mmproj：保持原位，列出交用户手动处理 ----
    unpaired = [p for p in files
                if _is_mmproj(os.path.basename(p))
                and os.path.normcase(os.path.normpath(p)) not in claimed]
    return moves, unpaired, {"status": "ok", "root": root, "roots": roots}

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
