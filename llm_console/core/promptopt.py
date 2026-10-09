# -*- coding: utf-8 -*-
"""llm_console.core.promptopt — 提示词优化：数据与判据层（零网络、零 tkinter）。

W 2026-10-08 需求：用户在输入框写了提示词后，可把它发给一个指定的文本模型，
并行重写为「保守 / 增强 / 精炼」三个版本，用户三选一（也可回退原始输入）。
UI 属后续轮；本模块与 connection/optimize.py（请求层）是功能本体。

三条设计事实（出处见 `11-云端接入.md` §9 与 `15-ADR与决策记录.md`）：
  · 三路请求的消息必须**前缀一致、仅末尾策略指令不同**（策略指令拼在 user 消息
    末尾）——为的是命中模型侧 prompt cache。DeepSeek 官方页
    （api-docs.deepseek.com/guides/kv_cache）写明缓存构建需要数秒、示例里同前缀
    的头两次并发请求都不命中：**首轮三并发大概率全 miss，受益的是"失败只重试
    那一路"与短时间内的再次优化**。阿里云隐式缓存另有 ≥1024 token 前缀的门槛
    （help.aliyun.com/zh/model-studio/context-cache）。
  · 不合并成"一次请求出三个版本"：关思考的模型在一次生成里容易偷懒、三个版本
    趋同（W 的需求原文口径）。
  · 保真度是首要要求：保守版不得改变原意，增强版只许适度推测。所以返回前做
    一次轻量校验（fidelity_check）——关键实体丢失的候选标「疑似偏离原意」，
    最终裁决权在用户。

模板与策略指令的文案 = W 2026-10-08 提供的 demo **原文照抄**（需求第 2 条授权
"按需修改"，本轮验证未产生修改必要）。它们是发给模型的提示词，也是自检钉住的
字面——要改先过 W（铁律 14 的口径）。
"""

import json
import os
import re
import threading
import time

from . import textfile
from .config import APP_DIR, atomic_write_json

# ---------------------------------------------------------------------------
# 7 个入口场景（需求第 2 条的表：入口 × 图角色 → system prompt）
# group 决定「增强版」用哪条策略指令：静态组 = 文本/文生图/图生图，
# 动态组 = 文生视频/图生视频（要多管动作幅度、节奏与运镜）。
# ---------------------------------------------------------------------------
STATIC = "static"
DYNAMIC = "dynamic"

SCENARIOS = {
    "text": {
        "label": "文本",
        "group": STATIC,
        "system": "你是文本提示词优化器。只输出重写后的提示词本体，禁止任何解释、前言、引号、编号或列表标记；禁止添加原提示词中不存在的要求。",
    },
    "t2i": {
        "label": "文生图",
        "group": STATIC,
        "system": "你是图像提示词优化器。只输出重写后的提示词本体，禁止任何解释、前言、引号、编号或列表标记；禁止添加原提示词中不存在的画面元素。",
    },
    "t2v": {
        "label": "文生视频",
        "group": DYNAMIC,
        "system": "你是视频提示词优化器。只输出重写后的提示词本体，禁止任何解释、前言、引号、编号或列表标记；禁止添加原提示词中不存在的画面元素、动作或镜头。",
    },
    "i2i_edit": {
        "label": "图生图·底图重绘",
        "group": STATIC,
        "system": '你是图像重绘提示词优化器。输入图片已确定画面内容，文字只负责描述改动。只输出重写后的提示词本体，禁止解释、前言、引号或编号；明确"改什么、保留什么"，不重复描述图中画面，不擅自改动未提及的部分。',
    },
    "i2i_subject": {
        "label": "图生图·主体参考",
        "group": STATIC,
        "system": "你是图像生成提示词优化器。输入图片仅作主体形象或风格参考，文字负责描述目标画面。只输出重写后的提示词本体，禁止解释、前言、引号或编号；保留参考图的参考维度，其余画面内容由文字完整描述。",
    },
    "i2v_edit": {
        "label": "图生视频·底图重绘",
        "group": DYNAMIC,
        "system": '你是视频生成提示词优化器。输入图片已确定画面基础，文字负责描述重绘效果与动态。只输出重写后的提示词本体，禁止解释、前言、引号或编号；明确"保留什么、改成什么"，不重复描述画面，不新增图中不存在的主体或物体，不添加原提示词中不存在的镜头或动作。',
    },
    "i2v_subject": {
        "label": "图生视频·主体参考",
        "group": DYNAMIC,
        "system": "你是视频生成提示词优化器。输入图片仅作主体形象或风格参考，文字负责描述目标场景与动态。只输出重写后的提示词本体，禁止解释、前言、引号或编号；保留参考图的参考维度，补全场景、动作与运镜（运镜仅在原提示词已有镜头需求时整理）。",
    },
}


def resolve_scenario(kind, has_image=False, ref_mode=""):
    """主界面状态 → 场景 key（唯一判据，UI 轮直接转发，不许另写一份）。

    kind = cfg["model_kind"]（chat / image / video）；has_image = 挂了图片附件；
    ref_mode = capability 那两档（edit / subject，取 _effective_ref_mode 的结果）。
    生视频带图当前只有「首帧」语义（图定画面基础）→ 归底图重绘；i2v_subject 为
    将来的参考图生视频链路（云端 r2v / H3 reference）预留，届时由调用方显式传。
    """
    kind = str(kind or "chat")
    if kind == "image":
        if not has_image:
            return "t2i"
        return "i2i_subject" if str(ref_mode or "") == "subject" else "i2i_edit"
    if kind == "video":
        return "i2v_edit" if has_image else "t2v"
    return "text"


# ---------------------------------------------------------------------------
# 三条策略（保守 / 增强 / 精炼）。temperature 与 max_tokens 上限 = 需求第 4 条；
# 下限 256 = W 2026-10-08 拍板（极短输入 ×2 必截断："画一只猫"≈5 token → 10 额度）。
# ---------------------------------------------------------------------------
CONSERVATIVE = "conservative"
ENHANCED = "enhanced"
CONDENSED = "condensed"
STRATEGIES = (CONSERVATIVE, ENHANCED, CONDENSED)

STRATEGY_LABEL = {CONSERVATIVE: "保守版", ENHANCED: "增强版", CONDENSED: "精炼版"}

_STRATEGY_TEXT = {
    CONSERVATIVE: {
        STATIC: "策略：保留原文全部意图，按逻辑顺序重新组织语句，补充必要的格式或结构。只输出重写结果。",
        DYNAMIC: "策略：保留原文全部意图，按逻辑顺序重新组织语句，补充必要的格式或结构。只输出重写结果。",
    },
    ENHANCED: {
        STATIC: "策略：细化缺失的约束——范围、程度、风格、光线或格式，不扩大原文意图。只输出重写结果。",
        DYNAMIC: "策略：细化动作的幅度与速度、节奏与时长，运镜仅在原提示词有需求时补充，不新增动作或镜头。只输出重写结果。",
    },
    CONDENSED: {
        STATIC: "策略：压缩为要点清晰的最短版本，保留全部关键信息。只输出重写结果。",
        DYNAMIC: "策略：压缩为要点清晰的最短版本，保留全部关键信息。只输出重写结果。",
    },
}

TEMPERATURE = {CONSERVATIVE: 0.3, ENHANCED: 0.7, CONDENSED: 0.3}
MAX_TOKENS_CAP = {CONSERVATIVE: 1024, ENHANCED: 2048, CONDENSED: 1024}
MAX_TOKENS_FLOOR = 256

USER_HEADER = "待优化内容："


def system_prompt(scenario):
    return (SCENARIOS.get(scenario) or SCENARIOS["text"])["system"]


def strategy_text(scenario, strategy):
    sc = SCENARIOS.get(scenario) or SCENARIOS["text"]
    return _STRATEGY_TEXT[strategy][sc["group"]]


def build_messages(scenario, user_text, strategy):
    """[system, user]；user = "待优化内容：\\n{输入}\\n\\n{策略指令}"（需求第 2 条）。

    策略指令**必须**留在 user 消息末尾：三路请求因此共享逐字节一致的前缀
    （system + 头部 + 用户输入），这是 prompt cache 命中的前提。谁把可变内容
    挪到前面，谁就把缓存打没了——自检 test_prompt_opt [4] 钉住这条性质。
    """
    return [
        {"role": "system", "content": system_prompt(scenario)},
        {"role": "user",
         "content": "%s\n%s\n\n%s" % (USER_HEADER, user_text,
                                       strategy_text(scenario, strategy))},
    ]


def plan_max_tokens(user_text, strategy):
    """需求第 4 条：输入 token 数 ×2，保守/精炼上限 1024、增强上限 2048；下限 256（W 定）。"""
    est = textfile.est_tokens(user_text) * 2
    return max(MAX_TOKENS_FLOOR, min(est, MAX_TOKENS_CAP[strategy]))


# ---------------------------------------------------------------------------
# 保真度校验（需求第 5 条注：轻量前端校验，关键实体丢失 → 标「疑似偏离原意」）
#
# 只用标准库（本项目没有分词器，也不为此引第三方依赖），所以是启发式：
#   · ASCII 词（≥2 字符）逐个 verbatim 查（大小写不敏感）——LoRA / 4K / 专名这类
#     丢了就是丢了，任何一个缺失即可疑；
#   · 中文段（按虚词/量词切；整段切完只剩单字时单字也收）：≤2 字 verbatim；
#     ≥3 字放宽到"字符 bigram 重合 ≥0.5"也算保留（容忍"橘色小猫"→"橘色的小猫咪"
#     这类合法改写）。
# 「疑似」二字是给用户的提示而不是判决：宁可多标让人看一眼，不可漏标（需求原文：
# 保真度是首要要求）。阈值与切词表要调，先跑 _selftest/test_prompt_opt.py [6]。
# ---------------------------------------------------------------------------
_ASCII_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-\.+%#]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 多字虚词/泛请求词/含"一"的常见复合词：先从中文串里切掉
_SPLIT_WORDS = ("一个", "一种", "一些", "一起", "一直", "一定", "一般", "一致",
                "一体", "一律", "统一", "唯一", "第一", "万一",
                "什么", "怎么", "如何", "非常", "十分", "帮我")
# 单字虚词/量词/方位：切分点。刻意**不含** 画/做/生成 这类内容动词——它们是画面
# 动作本身（"做饭""油画"），切掉会把真实体打碎；泛化动词的过滤在 _GENERIC_VERBS。
_SPLIT_CHARS = set("的了着过在和与及或把被让使向对从到跟比为个条张幅些这那你我它他她"
                   "很更最太也就都还又再才只上里是有一")
# 抽出来也不算实体的泛指令词：改写它们本来就该换说法，丢了不算偏离
_GENERIC_VERBS = {"生成", "绘制", "创作", "制作", "拍摄", "输出", "设计", "呈现",
                  "请", "希望", "需要", "可以", "进行", "保留", "添加", "修改",
                  "画", "做", "拍", "写", "想", "要", "需", "可", "应", "将"}
ENTITIES_MAX = 60
_BIGRAM_KEEP = 0.5


def _split_cjk(run):
    """一段连续中文 → 实体候选。整段切完只剩单字时（"画一只猫"→画/猫）单字也收
    ——超短提示词里单字就是全部实体；能切出多字实体的段（"统一风格"→风格）则把
    单字碎渣丢掉，避免"统"这类残片混进来。"""
    for w in _SPLIT_WORDS:
        run = run.replace(w, "\x00")
    frags = []
    for seg in run.split("\x00"):
        cur = []
        for ch in seg:
            if ch in _SPLIT_CHARS:
                if cur:
                    frags.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            frags.append("".join(cur))
    if any(len(f) >= 2 for f in frags):
        frags = [f for f in frags if len(f) >= 2]
    return [f for f in frags if f not in _GENERIC_VERBS]


def key_entities(text):
    """原始提示词 → 关键实体词清单（去重保序，上限 ENTITIES_MAX）。

    返回 [{"text": 原文片段, "ascii": bool}, ...]。
    """
    text = str(text or "")
    ents, seen = [], set()

    def add(s, ascii_):
        k = s.lower() if ascii_ else s
        if k in seen:
            return
        if len(ents) >= ENTITIES_MAX:
            return
        seen.add(k)
        ents.append({"text": s, "ascii": ascii_})

    for m in _ASCII_TOKEN.finditer(text):
        add(m.group(0), True)
    for run in _CJK_RUN.findall(text):
        for seg in _split_cjk(run):
            if seg in _GENERIC_VERBS:
                continue
            add(seg, False)
    return ents


def _bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _preserved(ent, out_text, out_bigrams):
    s = ent["text"]
    if ent["ascii"]:
        return s.lower() in out_text.lower()
    if s in out_text:
        return True
    if len(s) >= 3:
        bg = _bigrams(s)
        return bool(bg) and len(bg & out_bigrams) / len(bg) >= _BIGRAM_KEEP
    return False


def fidelity_check(original, optimized):
    """关键实体保留校验 → {"total", "missing": [原文片段…], "suspect": bool}。

    suspect 判据：任一 ASCII 词缺失，或缺失占比 ≥1/3。optimized 为空时全缺 → 可疑。
    """
    ents = key_entities(original)
    if not ents:
        return {"total": 0, "missing": [], "suspect": False}
    out_text = str(optimized or "")
    out_bigrams = set()
    for run in _CJK_RUN.findall(out_text):
        out_bigrams |= _bigrams(run)
    missing, ascii_lost = [], False
    for e in ents:
        if not _preserved(e, out_text, out_bigrams):
            missing.append(e["text"])
            if e["ascii"]:
                ascii_lost = True
    suspect = bool(missing) and (ascii_lost or len(missing) * 3 >= len(ents))
    return {"total": len(ents), "missing": missing, "suspect": suspect}


# ---------------------------------------------------------------------------
# usage 里的 prompt cache 命中数（需求第 4 条「缓存验证」，内部项，不给用户看）
# ---------------------------------------------------------------------------

def extract_cache_hit(usage):
    """usage → 命中缓存的 prompt token 数；没有该字段返回 None（不猜、不编数字）。

    两家形状（官方页核实 2026-10-08）：
      · DeepSeek：usage.prompt_cache_hit_tokens（api-docs.deepseek.com/api/create-chat-completion）
      · OpenAI / 阿里云兼容模式：usage.prompt_tokens_details.cached_tokens
        （help.aliyun.com/zh/model-studio/context-cache）
    本地 llama-server 的 usage 没有这类字段 → None（本地靠引擎自己的 prompt cache，
    命中与否体现在后两路 prefill 变快，不出数字）。
    """
    if not isinstance(usage, dict):
        return None
    v = usage.get("prompt_cache_hit_tokens")
    if v is None:
        d = usage.get("prompt_tokens_details")
        if isinstance(d, dict):
            v = d.get("cached_tokens")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 用户最终选择的本地留存（需求第 6 条：只存不析，偏好分析是后续轮的事）
# 文件写法沿用 core/cloudjobs.py：RLock、读失败回空表、原子写、读-改-写全程持锁
# （铁律 8，坑 107）。路径是模块级变量，自检先 stub 再调用（同 SECRETS_PATH 套路）。
# ---------------------------------------------------------------------------
CHOICES_PATH = os.path.join(APP_DIR, "prompt_opt_choices.json")
CHOICES_VERSION = 1
CHOICE_ORIGINAL = "original"          # 需求第 3 条的「回退至原始输入」选项
CHOICE_VALUES = STRATEGIES + (CHOICE_ORIGINAL,)

_LOCK = threading.RLock()


def load_choices():
    """读取选择记录 → {"version": int, "records": [...]}；异常一律回空表。"""
    with _LOCK:
        try:
            with open(CHOICES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {"version": CHOICES_VERSION, "records": []}
    if not isinstance(data, dict):
        return {"version": CHOICES_VERSION, "records": []}
    records = data.get("records")
    if not isinstance(records, list):
        records = []
    return {"version": int(data.get("version") or CHOICES_VERSION), "records": records}


def record_choice(scenario, strategy, model="", suspect=False):
    """登记一次"用户最终选了哪版"；非法场景/取值拒记（返回 False）。

    每条：{"ts", "time", "scenario", "strategy", "model", "suspect"} ——
    strategy ∈ 保守/增强/精炼/original（回退原始输入）；suspect = 被选中的那版
    当时是否带着「疑似偏离原意」标记（将来分析偏好时用得上"用户是否无视标记"）。
    """
    if strategy not in CHOICE_VALUES or scenario not in SCENARIOS:
        return False
    with _LOCK:
        data = load_choices()
        data["records"].append({
            "ts": int(time.time()),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scenario": scenario,
            "strategy": strategy,
            "model": str(model or ""),
            "suspect": bool(suspect),
        })
        return atomic_write_json(CHOICES_PATH, data)


def list_choices():
    return load_choices()["records"]
