# -*- coding: utf-8 -*-
"""llm_console.core.capability — "这个模型能不能收图"的统一判定层（v35）。

三类模型的判据完全不同，所以集中在这一层，界面上只消费一个结论 + 一句依据：

  聊天模型（本地）  决定性判据是**有没有视觉投影器（mmproj）**：没有就真的收不了图，
                   这条可靠；但投影器文件名不规范 / 放在别处时会漏判 → 允许人工覆盖。
  聊天模型（云端）  /models 没有统一的能力字段，只能按名字启发式**猜**；
                   所以"猜出来的支持"不算数，要么用户明确声明，要么发一次真请求验证。
  生图模型          本地：附图 = 参考图/底图（图生图），由 sd.cpp 的 -i 决定 → 支持。
                   云端：看这一家的原生生图接口有没有参考图入参（providers.supports_ref_image，
                   判据是各家官方页写明的字段）→ 有就支持，没有就是不支持，不猜。
  生视频模型        本地：能不能给首帧取决于权重变体：fl2va / flf2v / i2v / ref2va 都有
                   图像输入通路，纯 t2va / t2v 没有 → 按变体标记判，允许人工覆盖。
                   云端：目前各家只接了文生 → 不支持首帧。

结论三态：yes / no / unknown；外加 basis 说明是谁下的结论，UI 才好决定要不要拦住用户。

**模式（v1.0.8 起）**：生图带图有**两种语义**，配置键 `img_ref_mode` 贯穿全链路 ——

  · `edit`    底图重绘：拿原图当底，按提示词改（本地 `-i`，云端各家原生字段）
  · `subject` 主体参考：只借图里的人/物/风格，构图另说（本地 `-r`，云端各家原生字段）

两者是**能力维度而不是偏好**：`resolve_modes()` 判"这个模型支持哪些"，判据是
引擎 / 服务商**官方页写明的入参形状**（本地查 `sdprofile.edit_modes`、云端查
`providers.ref_image_modes`）。不支持的那一档由调用方**在发请求之前**拦下 ——
放行错了就是一次白花的计费（坑 102）。
"""

import os
import re
from .. import texts

AUTO = ""          # 存进配置里表示"没人工指定，走自动判据"
YES = "yes"
NO = "no"
UNKNOWN = "unknown"

# 生图带图的两种模式。**文案统一在这里取**，别处不许自己写（措辞纪律见 13 坑 101）。
MODE_EDIT = "edit"              # 底图重绘：原图当底，按提示词改
MODE_SUBJECT = "subject"        # 主体参考：只借图里的人/物/风格
MODES = (MODE_EDIT, MODE_SUBJECT)
# 默认档 = 底图重绘：与这两种模式落地之前的行为完全一致（`-i` 那条实测链路）
DEFAULT_MODE = MODE_EDIT
MODE_LABEL = {MODE_EDIT: texts.CAP_DICT, MODE_SUBJECT: texts.CAP_DICT_2}
# 每种模式在引擎/接口上落在哪个入参上 —— 用于日志与排查（"这一档到底怎么发的"）
MODE_NOTE = {MODE_EDIT: texts.CAP_DICT_3,
             MODE_SUBJECT: texts.CAP_DICT_4}
# 两种模式各自的引擎参数（槽位纪律：参数名必须能在 `sd_cli_help.txt` 里查到）
MODE_FLAG = {MODE_EDIT: "-i", MODE_SUBJECT: "-r"}
CHOICES = (AUTO, YES, NO)
CHOICE_LABEL = {AUTO: texts.CAP_DICT_5, YES: texts.CAP_DICT_6, NO: texts.CAP_DICT_7}
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
    cloud = providers.is_cloud(cfg)
    if kind == "image":
        if cloud:
            # 云端生图能不能带参考图 = 这一家的原生接口有没有那个字段（判据在
            # providers.REF_IMAGE_APIS，来源是各家官方页）。不靠"看起来像不像"放行：
            # 放行错了就是一次白花花的计费。
            p = providers.current_provider(cfg) or {}
            if providers.supports_ref_image(p):
                # 同样是"收参考图"，阿里云拿它当底图重绘，MiniMax 拿它当主体/角色参考
                # （2026-10-01 真机各跑一张实测出来的差别），所以话分两句说
                if providers.ref_image_mode(p) == "subject":
                    return YES, "cloud", texts.CAP_L2
                return YES, "cloud", texts.CAP_L2_2
            if providers.ref_image_unverified(p):
                # 官方页写了这个入参、只是我们还没实测语义（坑 101）：同样拦下，
                # 但不能对人说"这一家只收文字" —— 那是假的
                return NO, "cloud", texts.CAP_L2_3_2
            return NO, "cloud", (texts.CAP_L2_3)
        return YES, "engine", texts.CAP_L2_4
    if kind == "video":
        if cloud:
            p = providers.current_provider(cfg) or {}
            sp = providers.split_cloud_id(cfg.get("model", ""))
            model = sp[1] if sp else ""
            mode = providers.first_frame_mode(p, model)
            if mode == "base64":
                return YES, "cloud", texts.CAP_L2_13
            if mode == "unverified":
                return NO, "cloud", texts.CAP_L2_5_2
            return NO, "cloud", texts.CAP_L2_5
        if _VIDEO_IMG_IN.search(key):
            return YES, "variant", texts.CAP_L2_6
        if _VIDEO_TEXT_ONLY.search(key):
            return NO, "variant", texts.CAP_L2_7
        return UNKNOWN, "variant", texts.CAP_L2_8
    if providers.is_cloud(cfg):
        if _NAME_HINT.search(key):
            return UNKNOWN, "name-hint", (texts.CAP_L2_9)
        return UNKNOWN, "none", (texts.CAP_L2_10)
    if is_vl_model(cfg, cfg.get("model", "")):
        return YES, "mmproj", texts.CAP_L2_11
    return NO, "mmproj", texts.CAP_L2_12



def resolve(cfg):
    """最终结论 → dict(verdict, basis, note, explicit)。人工声明永远优先。"""
    choice = get_choice(cfg)
    if choice == YES:
        return {"verdict": YES, "basis": "user", "explicit": True,
                "note": texts.CAP_DNOTE}
    if choice == NO:
        return {"verdict": NO, "basis": "user", "explicit": True,
                "note": texts.CAP_DNOTE_2}
    v, basis, note = auto_detect(cfg)
    return {"verdict": v, "basis": basis, "explicit": False, "note": note}


def resolve_key(cfg, key, kind=None):
    """按指定模型（文件名或 "pid::model"）判，供模型菜单逐行标注用。

    **探针的"云 / 本地"与"类型"一律由 `key` 自身决定**，不跟着当前选中的模型走
    （W 2026-10-08 报的 bug：选着云端 / 生图 / 生视频模型时，本地文本模型会被套上
    "当前选中那一类"的判据 —— 云端取不到能力字段、视频按变体标记猜 —— 菜单里一排
    「（看图未确认）」，那是"当前模型的上下文"漏进了"逐行标注"）。

    · 云 id（含 `::`）：带上它**自己的** provider 与注册表里的能力类型（调用方给了
      `kind` 就用调用方的，模型菜单的分组自己知道是哪一类）；
    · 本地文件名 / 路径：一律按 `model_provider="local"` 判，类型默认 `chat` ——
      本地这条"能不能看图"的标注只对聊天模型有意义（生图 / 生视频带图走发送链路的
      `resolve_modes`，是另一套判据）。
    """
    from . import providers
    probe = dict(cfg)
    k = str(key or "")
    pid, model = providers.split_cloud_id(k) or ("", "")
    if pid:
        probe["model_provider"] = pid
        probe["model"] = k
        probe["model_kind"] = kind or {"text": "chat", "image": "image",
                                       "video": "video"}.get(
            providers.model_kind_of(providers.get_provider(cfg, pid), model), "chat")
    else:
        probe["model_provider"] = providers.LOCAL
        probe["model"] = k
        probe["model_kind"] = kind or "chat"
    return resolve(probe)


# ---------------------------------------------------------------- 生图模式（两种语义）

def modes_note(mode):
    """这一档是干什么的（界面提示与发送回显统一问这里，别处自己写就会写出两种说法）。"""
    return "%s（%s）" % (MODE_LABEL.get(mode, mode), MODE_NOTE.get(mode, ""))


def resolve_modes(cfg, family=None):
    """生图带图时，这个模型**支持哪些模式** → `{mode: {"ok", "why", "flag"}}`。

    判据只认"官方页 / `--help` 写明的入参形状"：

    · 本地：`sdprofile.edit_modes(family)` —— `-i`（底图重绘）与 `-r`（主体参考）是
      sd-cli 的两个通用入口，`edit_modes` 记的是"这一族的权重走哪条路有依据"。
      `family` 由调用方传（它手上已经有 `resolve_img_files` 的结果），不给就只认
      配置里人工指定的 `img_family`，认不出按"两种都能试"给，**不由这一层瞎猜**。
    · 云端：`providers.ref_image_modes(provider)` —— 官方页没写参考图入参的一律空集合。

    `flag` 是给界面用的简短依据（"按 --help" / "官方页" / "未实测"），
    **界面必须照实显示**，别把没实测的写成实测（坑 101 / 10 §5.1）。
    """
    from . import providers, sdprofile
    if providers.is_cloud(cfg):
        p = providers.current_provider(cfg) or {}
        modes = providers.ref_image_modes(p)
        unverified = providers.ref_image_unverified(p)
        flag = texts.CAP_IF if (modes or unverified) else "none"
        if not modes:
            return {m: {"ok": False, "flag": flag,
                        "why": texts.CAP_DWHY_2 if unverified else texts.CAP_DWHY}
                    for m in MODES}
        return {m: {"ok": m in modes, "flag": flag,
                    "why": "" if m in modes
                    else texts.CAP_CAT % (MODE_LABEL[m], MODE_LABEL[DEFAULT_MODE])}
                for m in MODES}
    fid = str(family or cfg.get("img_family") or "").strip()
    modes, flag = sdprofile.edit_modes(fid)
    return {m: {"ok": m in modes, "flag": flag,
                "why": "" if m in modes
                else texts.CAP_CAT_2 % (
                    sdprofile.label_of(fid) or texts.CAP_N,
                    texts.CAP_IF_2 if m == MODE_EDIT else texts.CAP_ELSE, MODE_LABEL[m])}
            for m in MODES}


def default_ref_mode(cfg, family=None):
    """本次用哪一档：配置里选了它、且这一档确实支持 → 用它；否则回该模型的默认档。

    **配置里的值只在模型支持时才生效** —— 换个不支持那一档的模型就自动回默认，
    不会出现"设置页选了这一档、发出去却按另一种语义跑"。

    云端还有一个额外事实：各家的默认档不同（阿里云 `edit`、MiniMax `subject`），
    配置里那个全局默认值（出厂 `edit`）在 MiniMax 上根本不支持。回退时不能直接
    落 `DEFAULT_MODE`，否则界面会显示一个置灰的"底图重绘"、发送侧却按"主体参考"
    发（2026-10-19 云端对齐轮修）。所以云端先问 `providers.ref_image_mode()`。
    本地回到 `DEFAULT_MODE` 与改造前逐字一致（生图各族两档都支持，`edit` 恒可用）。
    """
    from . import providers
    modes = resolve_modes(cfg, family)
    want = str((cfg or {}).get("img_ref_mode", "") or "").strip()
    if want in MODES and modes.get(want, {}).get("ok"):
        return want
    if providers.is_cloud(cfg):
        p = providers.current_provider(cfg) or {}
        d = providers.ref_image_mode(p)
        if d in MODES and modes.get(d, {}).get("ok"):
            return d
    return DEFAULT_MODE


def supports_ref_mode(cfg, mode, family=None):
    return bool(resolve_modes(cfg, family).get(mode, {}).get("ok"))


def can_take_image(cfg):
    """能不能真的把图发出去：unknown 一律按不行处理（但界面要说明是"没确认"而不是"不支持"）。"""
    return resolve(cfg)["verdict"] == YES


def label_of(res):
    """给界面用的一行结论。"""
    v = res["verdict"]
    who = texts.CAP_IF_3 if res.get("explicit") else ""
    if v == YES:
        return texts.CAP_CAT_3 + who
    if v == NO:
        return texts.CAP_CAT_4 + who
    return texts.CAP_CAT_5 + who
