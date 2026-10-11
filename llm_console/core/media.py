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
import re

from . import config, sdprofile
from .models import (IMAGE_SUBDIR, _is_mmproj, find_vl_pairs, scan_models,
                     scan_video_models, video_scan_dirs)

# 配套槽位拼进命令行的先后顺序。Qwen-Image 的实测 argv 是 vae→llm，保持不变；
# 视频侧反过来（llm→vae→audio-vae）也是为了与 MiniMax-H3 改造前的 argv 逐字相同。
_SLOT_ORDER = ("vae", "llm", "clip_l", "clip_g", "t5xxl", "clip_vision",
               "tokenizer", "connectors", "audio_vae", "audio_encoder", "motion",
               "taesd", "high_noise")
_VIDEO_SLOT_ORDER = ("llm", "vae", "audio_vae", "t5xxl", "clip_l", "clip_g",
                     "clip_vision", "llm_vision", "motion",
                     "tokenizer", "connectors", "audio_encoder", "high_noise", "taesd")

# MiniMax-H3 在 CFG>1 时必须能编码出负向提示词，空串会直接失败；
# 配置里留空时用这个兜底值（内容可在 设置 → 本地模型 → 生视频（sd.cpp） 改）。
DEFAULT_VIDEO_NEG_PROMPT = ("worst quality, low quality, blurry, distorted, deformed, "
                            "watermark, text, static, jittery")


# 分辨率写法判读（W 2026-10-09）：设置页的写法归一与本地 / 云端发送侧共用这一份。
# 认半角 x/X 与 * ＊ × ，, （全角变体一并归一），全角数字也认，分隔符两侧空白忽略。
_SIZE_FIX = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "Ｘ": "x", "ｘ": "x", "×": "x", "＊": "*", "，": ",",
})
_SIZE_RE = re.compile(r"(\d+)\s*[x*,]\s*(\d+)")


def parse_size(text):
    """分辨率写法判读 → `(宽, 高)`，不合格返回 None。

    「832x480 / 832X480 / 832 x 480 / 832*480 / 1024×1024 / 1024＊1024 /
    ８３２ｘ４８０ / 832，480」都认；「768」这类缺分隔的、解析不出两个正整数的
    （含 0 / 负数 / 多段）都是 None。以前只认半角 x，其余写法在发送侧被静默回退
    默认档（坑 183）——现在四个分辨率输入框（本地生图 / 生视频、云端出图尺寸 /
    视频分辨率）的守卫与发送侧都走这里，判据只有这一份。
    """
    s = str(text or "").strip().lower().translate(_SIZE_FIX)
    m = _SIZE_RE.fullmatch(s)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if w > 0 and h > 0 else None


def _wh(size, fallback, multiple=1):
    """解析 "宽x高"（写法见 `parse_size`，* ，× 全角都认）；非法值回退默认档
    （用户手填坏值时别让发送链路崩掉丢输入；界面侧会在框后亮红字提示，见
    settings._size_entry —— 这里只兜底，不报错）。

    `multiple` 是这一族要求的尺寸对齐（SD 系 8、Flux/SD3/Wan 16、LTX 32）：
    不对齐时**向上**补到最近的倍数——引擎多半直接报错，替用户改一下比失败一次好，
    而 1024x1024 / 512x512 这些常用档本来就是 8/16 的倍数，不会被改动。
    """
    p = parse_size(size)
    if p:
        w, h = p
        m = max(1, int(multiple or 1))
        if m > 1:
            w = w + (-w % m)
            h = h + (-h % m)
        return str(w), str(h)
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
        raw = str(d or "").strip()
        if not raw:
            continue
        # 空串先滤掉再 normpath（R-73）：normpath("") == "."，原来会把**当前工作目录**
        # 当成搜索目录塞进去（配置留空 = 自动时不该搜 CWD）
        np = os.path.normpath(raw)
        if np and os.path.isdir(np) and np not in [os.path.normpath(x) for x in out]:
            out.append(np)
    return out


def family_of(cfg, path, kind, forced_key):
    """这一条链路用哪个模型族：配置里人工指定的 > 自动识别。→ (fid, basis, note)"""
    forced = str(cfg.get(forced_key, "") or "").strip()
    return sdprofile.detect_file(path, kind=kind, forced=forced)


# ------------------------------------------------------------------------ 生图

def _local_out_dir(cfg, key, sub):
    """本地产物目录的公共判据：`key` 显式值优先（相对路径按**程序目录**解析，
    别落进当前工作目录 —— 坑 42 同族），留空 = `<产物文件夹>/本地/<sub>`。"""
    d = str((cfg or {}).get(key, "") or "").strip()
    if d:
        return d if os.path.isabs(d) else os.path.join(config.APP_DIR, d)
    return os.path.join(config.output_root(cfg), "本地", sub)


def img_out_dir(cfg):
    """本地生图的**产物**目录 = `img_output_dir` 显式值，留空 = `<产物文件夹>/本地/image`。

    2026-10-06 起不再写进 sd.cpp 部署目录（原来写死 `<sd_dir>/output`）：
    引擎目录与产物混在一起既难清理，也逼着没部署 sd.cpp 的人面对一个莫名的相对路径。
    判据只写这一处，界面直接调它。
    """
    return _local_out_dir(cfg, "img_output_dir", "image")


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
    files["note"] = _note_with_manual(why, files)
    files.setdefault("vae", "")
    files.setdefault("llm", "")
    return files


def _note_with_manual(why, files):
    """识别 note 追加"人工指定没找到"的点名（R-20）：设置页回显（note_of）与
    发送侧预检都读得到——静默换件改成开口说话的那一半。"""
    mm = files.get("manual_missing") or []
    if not mm:
        return why
    add = "；".join("人工指定的%s没找到：%s" % (sdprofile.slot_name(s), w)
                   for s, w in mm)
    return (why + "｜" + add) if why else add


def plan_ref_images(refs, mode):
    """带图两档的最终取舍 → `(used, dropped, note)`。**在 Popen 之前**算好。

    命令行组装与界面回显共用这一份判断，所以"发了几张"和"说了几张"不可能对不上。

    · `subject` 主体参考 → `-r/--ref-image`，`--help` 写明 *can be used multiple times* →
      多张全部交出去。
    · `edit` 底图重绘 → `-i/--init-img <string>` 是**单值**入口 → 只认第一张，多出来的
      如实报数，不静默丢（静默丢图是这类 bug 最难查的一种）。
    """
    paths = [p for p in (refs or []) if p and os.path.isfile(p)]
    if not paths:
        return [], 0, ""
    if str(mode or "") == sdprofile.EDIT_MODE_REF:
        return paths, 0, ""
    if len(paths) > 1:
        return paths[:1], len(paths) - 1, "底图重绘一次只认一张，多出来的没送进去"
    return paths, 0, ""


# --------------------------------------------------------------- 底图重绘的重绘强度
#
# `--strength` 是"底图被加噪再重绘多少"的旋钮，也是**唯一**能防住"我想小改一下，
# 结果模型给我画了另一张图"的引擎参数（`10` §5.0、坑 25）。原来它是一个全局定值
# （`img_strength`，实测最佳 0.9），于是"变清晰""换个背景"这类小改也按 0.9 发出去。
#
# 三档 = 对"这次要改多大"的判断，取值各自一个配置键（数值来自开发机 qwen-image-2.1 实测，
# 见 `10` §5.0.2；同一张底图、同种子、20 步，"与原图平均像素差"是 0~255 的漂移量）：
#   light  小改   0.4   只动画质（变清晰 / 修复 / 抠图），漂移 15，画面几乎原样
#   mid    普通改 0.65  改属性或局部（换背景 / 去掉某物），漂移 24，构图在、身份开始漂
#   heavy  大幅   0.9   换主体 / 重画 / 换风格，漂移 59，指令这才被执行但原图也没了
# ⚠ **兜底是 heavy = `img_strength`（现状值）**：实测只有这一档真会执行提示词，
#   判不出来时替用户降到 0.65 = 既改不动指令、又把原图改坏（两头不讨好）。
STRENGTH_TIERS = ("light", "mid", "heavy")
TIER_LABEL = {"light": "小改", "mid": "普通改", "heavy": "大幅"}
TIER_DEFAULT = "heavy"
# 档位 → 取值用的配置键。heavy 复用既有键 `img_strength`：出厂值不变 ⇒ 老配置零迁移、
# 判不出的提示词行为与分档上线前逐字一致。
TIER_KEY = {"light": "img_strength_light", "mid": "img_strength_mid",
            "heavy": "img_strength"}

# 线索表。**只在底图重绘档生效**，所以"清晰 / 放大"这类纯文生图里也常见的词不会污染那边。
# 裁决顺序（`match_strength_tier`）：明示保结构 → 小改；重画/换风格 → 大幅；
# 再质感修复 → 小改；再属性/局部 → 普通改；都没有 → 大幅（兜底）。
# 一句话取向：**"别改坏"优先于"改到位"** —— W 报的原始症状就是小改被重绘成另一张图。
_ST_KEEP = ("保持不变", "保持原样", "保持原图", "保持构图", "保持主体", "主体不变",
            "人物不变", "背景不变", "其余不变", "其它不变", "其他不变", "剩下的不变",
            "不要改变", "不要改", "别改变", "不要动", "都别动", "别动", "不动",
            "原样", "只改", "只换", "仅改", "保留原", "维持原",
            "keep unchanged", "keep the same", "unchanged", "don't change",
            "do not change", "keep everything", "everything else stays",
            "rest unchanged", "keep composition", "same composition", "preserve",
            "only change", "nothing else")
# 只动画质 / 修图：主体与构图都要留
_ST_LIGHT = (
    "变清晰", "更清晰", "清晰一点", "清晰化", "提高清晰度", "清晰度", "高清", "超清",
    "锐化", "去模糊", "消除模糊", "去雾", "变清楚", "去噪", "降噪", "减少噪点",
    "去颗粒", "噪点", "修复", "翻新", "老照片", "提高分辨率", "超分", "放大",
    "补光", "调亮", "变亮", "调色", "校色", "白平衡", "上色", "增强", "画质",
    "美颜", "磨皮", "去瑕疵", "修补", "补全",
    "sharpen", "deblur", "unblur", "denoise", "reduce noise", "upscale",
    "super resolution", "enhance quality", "restore", "old photo", "colorize",
)
# 改属性 / 局部增删：构图要留，但确实动了画面内容
_ST_MID = (
    "抠图", "抠出来", "提取主体", "去背景", "去掉背景", "移除背景", "删除背景",
    "换背景", "背景换", "换个背景", "透明背景", "换成透明", "透明底", "去底",
    "白底", "纯白背景", "纯色背景",
    "remove background", "background removal", "cut out", "cutout",
    "transparent background", "white background",
    "去掉", "去除", "删掉", "移除", "擦除", "去水印", "去字幕", "去马赛克",
    "去反光", "去阴影", "加上", "添加一个", "补一个",
    "remove watermark", "remove object", "erase", "inpaint", "touch up",
)
_ST_HEAVY = (
    "重新画", "重画", "再画一张", "改画", "大幅修改", "大改", "彻底改", "完全改成",
    "换成另一种", "换个风格", "换风格", "改风格", "风格化", "动漫风", "变成动漫",
    "卡通风", "卡通化", "油画", "水彩", "水墨", "像素风", "手绘", "素描", "插画风",
    "赛博朋克", "3d渲染", "三维渲染", "换成夜景", "变成夜景", "换个场景", "换场景",
    "变成另一个人", "换个脸", "换装", "变成年轻", "变成老年", "以这张为参考",
    "参考这张重新", "重新生成",
    "redraw", "repaint", "re-imagine", "reimagine", "restyle", "stylize",
    "anime style", "cartoon style", "oil painting", "watercolor", "pixel art",
    "in the style of", "convert to", "transform into", "different scene",
    "another person", "change the style", "cyberpunk", "3d render",
    "anime", "cartoon", "concept art",
)


def _strength_rx(hints):
    """线索表 → 一条编译好的正则。拉丁线索必须**整词**命中，
    否则 "hd" 会在 "shadow" 里、"erase" 会在 "restored" 里冒出来；
    中文没有词边界，按子串算。拉丁词之间允许夹冠词
    （"remove background" 要能命中 "remove the background"）。"""
    parts = []
    for h in hints:
        hh = str(h).lower()
        if re.fullmatch(r"[a-z0-9 .'-]+", hh):
            toks = [re.escape(t) for t in hh.split() if t]
            body = r"\s+(?:the\s+|an\s+|a\s+)?".join(toks)
            parts.append(r"(?<![a-z0-9])%s(?![a-z0-9])" % body)
        else:
            parts.append(re.escape(hh))
    return re.compile("|".join(parts), re.I)


_ST_RX_KEEP = _strength_rx(_ST_KEEP)
_ST_RX_LIGHT = _strength_rx(_ST_LIGHT)
_ST_RX_MID = _strength_rx(_ST_MID)
_ST_RX_HEAVY = _strength_rx(_ST_HEAVY)


def match_strength_tier(prompt):
    """提示词 → `(档位, 命中的线索, 是否也带"大幅重绘"线索)`。只判档，不取值。

    顺序：明示保结构 → 小改；重画/换风格 → 大幅；质感修复 → 小改；属性/局部 → 普通改；
    都没有 → 大幅（兜底）。第三条返回值用在"明说保持不变、又要换风格"那种自相矛盾的
    提示词上：照样按小改走（别改坏优先），但要把这层矛盾说出来，不能静默判档。
    """
    t = str(prompt or "")
    if not t.strip():
        return TIER_DEFAULT, "", False
    heavy = _ST_RX_HEAVY.search(t)
    hit = _ST_RX_KEEP.search(t)
    if hit:
        return "light", hit.group(0), bool(heavy)
    if heavy:
        return "heavy", heavy.group(0), False
    hit = _ST_RX_LIGHT.search(t)
    if hit:
        return "light", hit.group(0), False
    hit = _ST_RX_MID.search(t)
    if hit:
        return "mid", hit.group(0), False
    return TIER_DEFAULT, "", False


def strength_value(cfg, tier):
    """某一档要发给引擎的数值（不可用 / 越界时回落到出厂默认，不发给引擎一个 0）。"""
    key = TIER_KEY.get(str(tier or "").strip(), TIER_KEY[TIER_DEFAULT])
    try:
        val = float(cfg.get(key))
    except (TypeError, ValueError):
        val = float(config.DEFAULT_CONFIG.get(key, 0.9))
    if not (0.0 < val <= 1.0):
        val = float(config.DEFAULT_CONFIG.get(key, 0.9))
    return val


def edit_strength(cfg, prompt, tier=""):
    """底图重绘这次真正要发的 `--strength` → `(档位, 数值, 界面上那一句短说明)`。

    · `tier` 非空 = 用户在附件条按次指定了档（"" = 交给提示词判）；
    · `img_strength_auto` = False ⇒ 不看提示词，恒用 `img_strength`（固定档）。
    界面回显与 `build_img_cmd` 读的是同一个函数、同一份入参，所以"说出去的强度"
    与"拼进命令行的强度"不可能对不上（与 `plan_ref_images` 同一条纪律）。
    说明只留一句（W 2026-10-11 定：主界面不堆依据与实测细节）。
    """
    tier = str(tier or "").strip()
    if tier not in STRENGTH_TIERS:
        tier = ""
    if not bool(cfg.get("img_strength_auto", True)):
        if not tier:
            return TIER_DEFAULT, strength_value(cfg, TIER_DEFAULT), "固定档"
    elif not tier:
        tier, hit, clash = match_strength_tier(prompt)
        why = "自动·%s" % TIER_LABEL[tier]
        if clash and tier == "light":
            why += "（提示词也要重画）"
        elif not hit and tier == TIER_DEFAULT:
            why += "（判不出改动幅度）"
        return tier, strength_value(cfg, tier), why
    return tier, strength_value(cfg, tier), "手动指定·%s" % TIER_LABEL[tier]


def build_img_cmd(cfg, prompt, out_path, steps, size, diffusion_path, cfg_scale, seed,
                  init_img=None, vae=None, llm=None, family=None, files=None,
                  ref_mode="", ref_images=None, strength=None, mask=None):
    """组装 sd-cli 生图命令行（形状由模型族决定，参数值仍来自配置）。

    Qwen-Image 这条链路保持改造前的逐字顺序：
      --diffusion-model / --vae / --llm / --backend / --cfg-scale / --sampling-method
      euler / --flow-shift 3 / --diffusion-fa / --steps / -W -H / -s -p -o
      附图时再补 --llm_vision、（CFG 低于 3.0 时提到 3.0）、-i、--strength。
    其它家族：单文件权重走 `-m`；CLIP 系补 --clip_l/--clip_g/--t5xxl；不硬塞
    flow-shift 与采样器（让引擎按模型自己定），尺寸按各家的对齐值补整。

    `strength` = 底图重绘档这次要发的 `--strength`；留 None = 在这里按提示词自动分档
    （`edit_strength`）。界面侧（`ui/image_gen.py`）先算好再传进来，回显与命令行同源。
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

    # 带图的两档模式：`edit` 底图重绘走 -i（单张），`subject` 主体参考走 -r（可重复给）。
    # `ref_mode` 留空 = 按这一族的默认档（`edit` 字段），所以实测链路的 argv 逐字不变。
    _refs = [p for p in (list(ref_images or []) + ([init_img] if init_img else []))
             if p and os.path.isfile(p)]
    if _refs:
        _mode = str(ref_mode or "").strip() or prof.get("edit", sdprofile.EDIT_MODE_INIT)
        _used, _dropped, _note = plan_ref_images(_refs, _mode)
        # 视觉投影器：**两档共用，别只挂在 -i 那条路上**。qwen-image 这类 LLM 编码器家族
        # 不配 mmproj 就"读不懂图" —— 实测（2026-10-06，qwen_image_2.1-Q5_0 + Qwen3VL-8B）：
        # `-r` 不给 --llm_vision → 引擎报 "no vision weights detected" 并在 5.2s 内 rc=1；
        # 同一命令补上 --llm_vision → rc=0、出图 1.98MB（355s）。所以它跟"用哪个参数传图"无关。
        # `-i` 那条路的 argv 与实测链路逐字不变（位置也仍在 -i 之前），自检钉住。
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
        if _mode == sdprofile.EDIT_MODE_REF:
            # 主体参考：Flux Kontext / MiniMax Ref2VA 这类，--help 写明 -r 可重复给
            for _p in _used:
                args += ["-r", _p]
            # 参考图"怎么处理"由这一档专属：--ref-image-args 是键值对串，
            # help 写明 empty = 按模型权重自动判断 → 留空就不给这个参数。
            _ra = str(cfg.get("img_ref_args", "") or "").strip()
            if _ra:
                args += ["--ref-image-args", _ra]
        else:
            # 底图重绘（img2img / inpaint）：LLM 编码器家族要配视觉投影器（同上，两档都要）
            minimum = prof.get("edit_cfg_min")
            if minimum and float(cfg_scale) < float(minimum):
                i = args.index("--cfg-scale")
                args[i + 1] = str(minimum)
            if strength is None:
                _t, strength, _w = edit_strength(cfg, prompt)
            args += ["-i", _used[0], "--strength", str(strength)]
            # 用户涂的遮罩（白 = 要重绘）：只挂底图重绘档 —— 实测不带遮罩时这份权重
            # 不执行"只换背景/只改衣物"这类指令，带上遮罩遮罩外基本逐像素不动（坑 214）。
            # 主体参考档（-r）不吃遮罩，所以只在 else 这条分支里拼。
            if mask and os.path.isfile(str(mask)):
                args += ["--mask", str(mask)]
            # 图像引导强度是"inpaint / image edit 模型"专用的那一路 --help 原文：
            #   --img-cfg-scale <float>  default: same as --cfg-scale
            # 与上面"把 --cfg-scale 抬到 edit_cfg_min"是**两件事**（那个管文本提示词的影响力，
            # 这个管图像条件的影响力）。<= 0 = 不传，用引擎默认（同 --cfg-scale）。
            try:
                _gs = float(cfg.get("img_guide_scale", 0) or 0)
            except Exception:
                _gs = 0.0
            if _gs > 0:
                args += ["--img-cfg-scale", str(_gs)]
    if str(cfg.get("img_extra_args", "")).strip():
        # config.split_extra_args：posix shlex 会吃掉 Windows 路径的反斜杠（R-22）
        args += config.split_extra_args(cfg["img_extra_args"])
    return args


# ---------------------------------------------------------------------- 生视频

def vid_out_dir(cfg):
    """本地生视频的**产物**目录 = `vid_output_dir` 显式值，留空 = `<产物文件夹>/本地/video`。"""
    return _local_out_dir(cfg, "vid_output_dir", "video")


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
    files["note"] = _note_with_manual(why, files)
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
    # 只有**显式登记** neg_prompt_required 的家族才吃那个内置兜底（R-69）：
    # 原默认值是 True，ltx / hunyuan / generic 这些没登记的家族被硬塞 MiniMax 的
    # 负向词（与函数 docstring"不替用户加戏"相反）——h3 自己在 profile 里写了 True，
    # 不受影响；wan 写了 False。
    if neg:
        args += ["-n", neg]
    elif prof.get("neg_prompt_required", False):
        args += ["-n", DEFAULT_VIDEO_NEG_PROMPT]
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
    bk = str(cfg.get("vid_backend", "")).strip() or str(prof.get("backend", "") or "").strip()
    if bk:
        # 清空 = 回落**该族默认后端**（与生图侧同一口径，R-70）：原来清空就是
        # 彻底不传 --backend，引擎全落默认设备（大模型可能直接显存不够）
        args += ["--backend", bk]
    pb = str(cfg.get("vid_params_backend", "")).strip()
    if pb:
        args += ["--params-backend", pb]
    if ref_img and os.path.isfile(ref_img):
        # 附图 = 首帧（图生视频）。开发机主体是 fl2va 变体，用 -i/--init-img；
        # -r/--ref-image 是给 Ref2VA 变体的，用错引擎不认（实测踩中）。
        # 首帧由引擎自己 crop/resize 到 -W/-H（实测 1024x1024 → 512x512），
        # 且 fl2va 只要首帧就能跑，不需要 --end-img（那是 flf2v 的尾帧）。
        if prof.get("edit") == sdprofile.EDIT_MODE_REF:
            args += ["-r", ref_img]
        else:
            args += ["-i", ref_img]
    if str(cfg.get("vid_extra_args", "")).strip():
        # 同 img_extra_args：走 split_extra_args，反斜杠路径不被吃（R-22）
        args += config.split_extra_args(cfg["vid_extra_args"])
    return args


# 旧名字保留（外部脚本与 models_ui 的提示仍在引用）：
#   _video_component_missing_label —— 现在只作 H3 老文案的兼容入口，
#   新代码请用 sdprofile.missing_slots / slot_label。
