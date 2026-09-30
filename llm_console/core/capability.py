# -*- coding: utf-8 -*-
"""llm_console.core.capability — "这个模型能不能收图"的统一判定层（v35）。

三类模型的判据完全不同，所以集中在这一层，界面上只消费一个结论 + 一句依据：

  聊天模型（本地）  决定性判据是**有没有视觉投影器（mmproj）**：没有就真的收不了图，
                   这条可靠；但投影器文件名不规范 / 放在别处时会漏判 → 允许人工覆盖。
  聊天模型（云端）  /models 没有统一的能力字段，只能按名字启发式**猜**；
                   所以"猜出来的支持"不算数，要么用户明确声明，要么发一次真请求验证。
  生图模型          附图 = 参考图/底图（图生图），由 sd.cpp 的 -i 决定 → 视为支持。
  生视频模型        能不能给首帧取决于权重变体：fl2va / flf2v / i2v / ref2va 都有图像输入通路，
                   纯 t2va / t2v 没有 → 按变体标记判，允许人工覆盖。

结论三态：yes / no / unknown；外加 basis 说明是谁下的结论，UI 才好决定要不要拦住用户。
"""

import os
import re

AUTO = ""          # 存进配置里表示"没人工指定，走自动判据"
YES = "yes"
NO = "no"
UNKNOWN = "unknown"
CHOICES = (AUTO, YES, NO)
CHOICE_LABEL = {AUTO: "自动判断", YES: "支持", NO: "不支持"}
LABEL_CHOICE = {v: k for k, v in CHOICE_LABEL.items()}

# 名字里带这些基本就是视觉模型（只是线索，不足以单独定论）
_NAME_HINT = re.compile(r"(vl[-_]?|[-_]vl\b|vision|ocr|image[-_]?in|multimodal|"
                        r"gemini|4o\b|4\.1-mini|-v[0-9]+[-_]?vl)", re.I)
# 视频权重里有图像输入通路的变体标记
_VIDEO_IMG_IN = re.compile(r"(fl2va?|flf2v|i2v|ref2va?|rv2v|s2v|va2va)", re.I)
_VIDEO_TEXT_ONLY = re.compile(r"(t2va?|t2v\b|text2video)", re.I)


def model_key(cfg):
    """能力表用的 key：本地 = GGUF 文件名，云端 = "pid::model" 复合 id。"""
    from . import providers
    if providers.is_cloud(cfg):
        return str(cfg.get("model", ""))
    return os.path.basename(str(cfg.get("model", "") or ""))


def get_choice(cfg, key=None):
    """人工声明的三态（AUTO/YES/NO）；没记录就是 AUTO。"""
    key = key or model_key(cfg)
    return (cfg.get("model_image_input") or {}).get(key) or AUTO


def set_choice(cfg, choice, key=None):
    key = key or model_key(cfg)
    table = cfg.setdefault("model_image_input", {})
    choice = choice if choice in CHOICES else AUTO
    if choice == AUTO:
        table.pop(key, None)
    else:
        table[key] = choice
    return choice


def auto_detect(cfg):
    """自动判据 → (verdict, basis, 给人看的一句话)。verdict ∈ yes/no/unknown。"""
    from . import providers
    from .models import is_vl_model
    kind = str(cfg.get("model_kind") or "chat")
    key = model_key(cfg)
    if kind == "image":
        return YES, "engine", "生图走引擎的参考图通路（-i），可以附图"
    if kind == "video":
        if _VIDEO_IMG_IN.search(key):
            return YES, "variant", "该视频权重是带图像输入通路的变体（fl2va / flf2v / i2v 这类）"
        if _VIDEO_TEXT_ONLY.search(key):
            return NO, "variant", "该视频权重是纯文生视频变体（t2v / t2va），没有首帧输入通路"
        return UNKNOWN, "variant", "认不出这个视频权重的变体，不能确定能不能给首帧"
    if providers.is_cloud(cfg):
        if _NAME_HINT.search(key):
            return UNKNOWN, "name-hint", ("名字看着像视觉模型，但云端的 /models 不带能力字段，"
                                          "不能只凭名字放行")
        return UNKNOWN, "none", "云端能力位各家不统一，需要你声明或实测一次"
    if is_vl_model(cfg, cfg.get("model", "")):
        return YES, "mmproj", "找到配对的视觉投影器（mmproj），可以看图"
    return NO, "mmproj", "没找到配对的视觉投影器（mmproj），本地模型收不了图"



def resolve(cfg):
    """最终结论 → dict(verdict, basis, note, explicit)。人工声明永远优先。"""
    choice = get_choice(cfg)
    if choice == YES:
        return {"verdict": YES, "basis": "user", "explicit": True,
                "note": "你指定为支持图片输入"}
    if choice == NO:
        return {"verdict": NO, "basis": "user", "explicit": True,
                "note": "你指定为不支持图片输入"}
    v, basis, note = auto_detect(cfg)
    return {"verdict": v, "basis": basis, "explicit": False, "note": note}


def resolve_key(cfg, key, kind=None):
    """按指定模型（文件名或 "pid::model"）判，供模型菜单逐行标注用。"""
    from . import providers
    probe = dict(cfg)
    probe["model_kind"] = kind or probe.get("model_kind", "chat")
    if providers.is_cloud(cfg):
        probe["model"] = key if providers.CLOUD_SEP in str(key) \
            else providers.make_cloud_id(cfg.get("model_provider"), key)
    else:
        probe["model"] = key
    return resolve(probe)


def can_take_image(cfg):
    """能不能真的把图发出去：unknown 一律按不行处理（但界面要说明是"没确认"而不是"不支持"）。"""
    return resolve(cfg)["verdict"] == YES


def label_of(res):
    """给界面用的一行结论。"""
    v = res["verdict"]
    who = "（你指定的）" if res.get("explicit") else ""
    if v == YES:
        return "支持看图" + who
    if v == NO:
        return "不支持看图" + who
    return "看图能力未确认" + who
