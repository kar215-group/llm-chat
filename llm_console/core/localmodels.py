# -*- coding: utf-8 -*-
"""llm_console.core.localmodels — 本地模型的"家谱"：三组模型 + 每个扩散模型配到哪些零件

为什么要单独一层：主菜单只显示"能选的东西"，而生图 / 生视频模型各自要一串配套件
（VAE / 文本编码器 / CLIP / T5 …），其中**文本编码器是带元数据的 .gguf**，会被
`scan_models` 当成普通聊天模型列进「文本模型」组 —— 菜单于是混进一批"其实它是零件"
的条目。管理页要把这层关系摊开给人看，还要能勾选"这个零件进不进主菜单"，
所以判定集中在这一层，界面只负责渲染（同一套关系在 UI 里写两遍必然对不上）。

`hidden` 的口径只有一条：**不影响主菜单以外的任何地方**。设置页的模型清单、
扫描补全、代理的模型解析照旧看得见全部文件 —— "不进主菜单"是显示层的事，
不是把模型从磁盘上删掉，也不是让它不能经代理调用。

放在 core 层还有一条硬约束：这一层**不许 import tkinter**（坑 36，按 AST 检查）。
"""

import os

from . import media, models, sdprofile

# 配套件在管理页里的展示顺序（与命令行拼装顺序无关，纯粹按"体积大→小、常见→少见"排）
_SLOT_VIEW = ("llm", "t5xxl", "clip_l", "clip_g", "clip_vision", "llm_vision",
              "vae", "audio_vae", "audio_encoder", "tokenizer", "connectors",
              "motion", "taesd", "high_noise")


# 配套件里"是文本编码器"的那一类：只有 `llm` 槽（Qwen3VL / Qwen2.5-VL 这种 LLM）
# 才可能兼作对话模型。t5xxl / clip_* 也是文本编码器，但它们是扩散链路的 conditioner，
# 形状与 llama.cpp 要的根本不是一回事，不该出现在"可进文本菜单"的判断里。
_TEXT_ENCODER_SLOTS = ("llm",)

# 可选件里"缺了会少某个功能"的那几个才值得在管理页提一句；其余（比如单文件 SDXL 的
# VAE / CLIP 本来就烤在一体文件里）报出来就是假故障。`generic` 族整族跳过，同理。
_OPTIONAL_VIEW = ("llm_vision", "clip_vision", "taesd", "motion", "audio_vae",
                  "audio_encoder", "high_noise", "connectors")


def hidden_set(cfg):
    """主菜单里不显示的文件名集合（key = basename，与 model_ngl / model_ctx 同源）。"""
    return set(str(x) for x in ((cfg or {}).get("model_hidden") or []))


def set_hidden(cfg, names, known=None):
    """写回隐藏清单：去重 + 排序；给了 `known`（当前盘上的文件名集合）时顺手丢掉
    已经不存在的名字，清单不会因为删过/改名越攒越脏。"""
    keep = set(str(x) for x in (names or []) if x)
    if known is not None:
        keep &= set(known)
    cfg["model_hidden"] = sorted(keep)
    return cfg["model_hidden"]


def slot_name(slot):
    """槽位的短中文标签（管理页用；`sdprofile.slot_label` 那条带引擎参数名，太长）。"""
    if slot == "high_noise":
        return "高噪段扩散模型"
    info = sdprofile.SLOTS.get(slot)
    return info[1] if info else slot


def _entry(cfg, path, kind, hidden, pairs):
    base = os.path.basename(path)
    try:
        size = int(os.path.getsize(path) / 1048576)
    except OSError:
        size = 0
    return {"path": path, "base": base, "name": models.display_name(cfg, path),
            "kind": kind, "size_mb": size, "in_menu": base not in hidden,
            "mmproj": pairs.get(path) or "", "family": "", "basis": "", "note": "",
            "slots": [], "missing": [], "optional_missing": [], "companion_of": []}


def _fill_slots(cfg, e, files, family, chat_bases=()):
    """把 resolve_* 的结果摊成零件行，并记下缺件（必需件）与可选件缺失（弱提示）。

    三个 flag 各管一件事，别混：
      · `chat`    —— 这块 GGUF **带元数据**，llama-server 真能当语言模型加载；
      · `encoder` —— 它在链路里扮演**文本编码器**（`llm` 槽）。是文本编码器 ≠ 能聊天：
                      MiniMax-H3 的 `qwen3vl_32b_minimax_h3` 是 kv=0 的裸权重容器，
                      2026-10-01 直接喂 llama-server 实测报 `unknown model architecture: ''`
                      → 只能当零件，不能选成对话模型；界面只标"不可对话"三个字（`no_chat`），
                        别在这儿写长篇解释；
      · `menu`    —— 该不该给它"进不进文本菜单"的勾选框。
    """
    e["family"] = sdprofile.label_of(family)
    e["basis"] = files.get("basis") or ""
    e["note"] = files.get("note") or ""
    prof = sdprofile.profile(family)
    for slot in _SLOT_VIEW:
        p = str(files.get(slot) or "")
        if not p or not os.path.isfile(p):
            continue
        is_gguf = p.lower().endswith(".gguf")
        chat = is_gguf and models.gguf_is_chat_capable(p)
        enc = slot in _TEXT_ENCODER_SLOTS and is_gguf
        e["slots"].append({"slot": slot, "label": slot_name(slot), "path": p,
                           "base": os.path.basename(p), "chat": chat, "encoder": enc,
                           "menu": chat and os.path.basename(p) in set(chat_bases),
                           "no_chat": bool(enc and not chat)})
    e["missing"] = files.get("missing") or sdprofile.missing_slots(family, files)
    have = set(s["slot"] for s in e["slots"])
    e["optional_missing"] = [] if family in (sdprofile.AUTO, "generic") else [
        {"slot": x, "label": slot_name(x)}
        for x in (prof.get("optional") or ()) if x in _OPTIONAL_VIEW and x not in have]


def inventory(cfg):
    """→ {"dir", "chat", "image", "video", "companion_of"}：本地模型全貌（一次扫盘）。

    `companion_of` = {配套文本编码器的 basename: [它服务的主体模型 basename, …]}：
    这些 .gguf 同时也在 `scan_models` 的聊天列表里（它们确实能聊天），管理页要靠这张
    表把它们标成"某某的配套编码器"，并决定它进不进主菜单。
    """
    _d, chat, images = models.scan_models(cfg)
    vids, _encs = models.scan_video_models(cfg)
    pairs = models.find_vl_pairs(cfg)
    hid = hidden_set(cfg)
    out = {"dir": cfg.get("models_dir", "") or "", "chat": [], "image": [],
           "video": [], "companion_of": {}}
    chat_bases = set(os.path.basename(p) for p in chat)
    for p in chat:
        out["chat"].append(_entry(cfg, p, "chat", hid, pairs))
    for p in images:
        e = _entry(cfg, p, "image", hid, pairs)
        try:
            files = media.resolve_img_files(cfg, p)
            _fill_slots(cfg, e, files, files.get("family") or "generic", chat_bases)
        except Exception:
            pass                      # 认族 / 找件失败不该让整页打不开，主体仍然列出来
        out["image"].append(e)
    for p in vids:
        e = _entry(cfg, p, "video", hid, pairs)
        try:
            files = media.resolve_video_files(cfg, p)
            _fill_slots(cfg, e, files, files.get("family") or "generic", chat_bases)
        except Exception:
            pass
        out["video"].append(e)
    # 配套文本编码器：只算"确实会出现在聊天列表里"的零件（mmproj 那种从来不是模型）
    for grp in ("image", "video"):
        for e in out[grp]:
            for s in e["slots"]:
                if s.get("menu"):
                    out["companion_of"].setdefault(s["base"], []).append(e["base"])
    for e in out["chat"]:
        e["companion_of"] = out["companion_of"].get(e["base"]) or []
    return out


def companion_names(inv):
    """"一键把配套编码器移出主菜单"要用的名字：只挑**确实是零件**的那些。

    已经在主菜单里被用户单独勾过的（`in_menu` 为 False 但本来就没勾）不重复处理；
    反过来，正在用的那个模型不会被移出去 —— 移完顶栏就显示一个不在清单里的名字了。
    """
    return [e["base"] for e in (inv.get("chat") or [])
            if e.get("companion_of") and e.get("in_menu")]
