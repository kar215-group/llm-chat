# -*- coding: utf-8 -*-
"""llm_console.core.providers — 云端 provider 注册表与云模型清单。

三种能力各走各的协议：text = OpenAI 兼容（connection/cloud.py）；
image / video = 服务商**原生**协议（connection/cloud_media.py），本模块只负责
"这条能力这个服务商能不能走"的结构判定（media_api / media_api_root / supports_media），
不下沉到厂商字段名。

云模型的身份是 "<provider_id>::<model>"：本地模型用 GGUF 路径标识，云模型没有文件，
用复合 id 存进 cfg["model"]——它不含路径分隔符，所以 os.path.basename() 之类的既有
代码不会炸；而"当前是不是云端"一律以 cfg["model_provider"] 为准，不靠字符串猜。
"""

import re

CLOUD_SEP = "::"
LOCAL = "local"
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KINDS = (KIND_TEXT, KIND_IMAGE, KIND_VIDEO)
KIND_LABEL = {KIND_TEXT: "文本模型", KIND_IMAGE: "生图", KIND_VIDEO: "生视频"}
KIND_ORDER = (KIND_TEXT, KIND_IMAGE, KIND_VIDEO)

# 候选模型超过这个数，设置页就多给一行"手动输入模型名"（清单太长时下拉里翻不动）
MANUAL_INPUT_AT = 12

# ---- 内置服务商：**只有名称与 base_url 是内置的** ----
# 模型清单一律不预置：用户填好信息、测试连通通过后，在"模型选择"界面里自己勾选
# 哪些进主页面菜单（v33 的约定，也免得给别人发这个程序时菜单里蹦出一堆用不上的模型）。
BUILTIN_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
    },
    # Token Plan 的 key 必须走这个专属域名，与按量计费的公共 DashScope 域名不通用
    "aliyun-token-plan": {
        "name": "阿里云 Token Plan",
        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    },
    # 按量计费的百炼：公共域名是**所有账号通用**的那一个，所以敢当默认值预置。
    # 但业务空间（WorkspaceId）专属域名是按账号来的，写死了别人就用不了（坑 55）
    # → 这一条放开 base_url 让用户自己改，名称仍然固定。
    "aliyun-bailian": {
        "name": "阿里云百炼（按量）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "editable_base_url": True,
    },
    # ---- v40 新增的五家（W 2026-10-01 直接给的地址）----
    # 全部是 OpenAI 兼容的 chat 端；生图/生视频各家走的是**自己那套原生协议**，
    # 见 connection/cloud_media.py 的 protocol 分派。文档里没有生图/生视频的，
    # 就不写 media_api（Kimi、腾讯混元：官方接口清单只有 chat/embeddings）。
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1",
    },
    "zhipu": {
        "name": "智谱GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "kimi": {
        "name": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
    },
    "tencent-hunyuan": {
        "name": "腾讯云混元（按量）",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
    },
    # 华为 MaaS：聊天走 /openai/v1，生图与生视频走**同主机的 /v1**（官方文档如此），
    # 所以这里预置「原生接口地址」为不带 /openai 的那个前缀
    "huawei-maas": {
        "name": "华为云 MaaS（按量）",
        "base_url": "https://api.modelarts-maas.com/openai/v1",
        "media_base_url": "https://api.modelarts-maas.com",
    },
}


def is_builtin(pid):
    return str(pid or "") in BUILTIN_PROVIDERS


def builtin_base_editable(pid):
    """内置服务商里，base_url 允许用户改的那一类（只有按量百炼：工作空间域名因账号而异）。"""
    return bool(BUILTIN_PROVIDERS.get(str(pid or ""), {}).get("editable_base_url"))


def builtin(pid):
    return dict(BUILTIN_PROVIDERS.get(str(pid or "")) or {})


def guess_kind(model):
    """按名字猜能力，只作为选择界面里的**默认勾选**，用户可以改。"""
    s = str(model or "").lower()
    if any(k in s for k in ("video", "t2v", "i2v", "r2v", "s2v", "happyhorse",
                           "hailuo", "cogvideo", "vidu", "pixverse")):
        return KIND_VIDEO
    if any(k in s for k in ("image", "img2", "text2img", "wan2", "cogview")) \
            and "video" not in s:
        return KIND_IMAGE
    # 百炼托管的 MiniMax-H3 名字里既没有 video 也没有 v 后缀，只有 "minimax/" 命名空间
    # 与 H+数字；这一条只影响界面**默认勾选**，勾选窗口里可以改（不当结论用）
    if "minimax" in s and re.search(r"h\s*[1-9]", s):
        return KIND_VIDEO
    if any(k in s for k in ("audio", "tts", "asr", "realtime")):
        return KIND_TEXT          # 语音类暂不单列，归到文本以免菜单出现没接的分组
    return KIND_TEXT


def catalog(provider):
    """该服务商"已知的模型"= 已加进主页面的 + model_kinds 里记录过的（拉取/手填验证过的）。"""
    provider = provider or {}
    out = list(provider.get("models") or [])
    for m in (provider.get("model_kinds") or {}):
        if m not in out:
            out.append(m)
    return out


def models_in_menu(provider):
    """真正出现在主页面模型菜单里的那些。"""
    return list((provider or {}).get("models") or [])


def set_menu_models(provider, models, kinds=None):
    """写入主页面清单与能力表；能力表只增不删，保留"已知但没加进菜单"的模型记录。"""
    k = dict(provider.get("model_kinds") or {})
    k.update(dict(kinds or {}))
    provider["models"] = [str(m).strip() for m in models if str(m).strip()]
    for m in provider["models"]:
        k.setdefault(m, guess_kind(m))
    provider["model_kinds"] = k
    return provider


def model_kind_of(provider, model):
    """某个模型属于哪一类能力；没记的一律当文本（一期的老配置都是文本）。"""
    kinds = (provider or {}).get("model_kinds") or {}
    k = kinds.get(str(model or ""))
    return k if k in KINDS else KIND_TEXT


def models_by_kind(provider):
    """{kind: [模型名, ...]}，用于设置页的分类显示与模型菜单分组。"""
    out = {k: [] for k in KIND_ORDER}
    for m in (provider or {}).get("models") or []:
        out[model_kind_of(provider, m)].append(m)
    return out

# 设置页「新增 provider」时的初始值
PROVIDER_TEMPLATE = {
    "id": "",
    "name": "",
    "base_url": "",
    "kind": KIND_TEXT,
    "models": [],
    "model_kinds": {},
    "ctx": 0,                        # 该服务商模型的上下文窗口（token）；0 = 未声明
    "enabled": True,
    "timeout": 600,
    "extra_headers": {},
    # ---- 云端生图 / 生视频（二三期）----
    # 原生媒体接口的根地址：留空 = 从 base_url 推（把 /compatible-mode/v1 换成 /api/v1，
    # 同族域名本来就只差这一段）。跨方案时（例如按量计费的公共端）才需要显式填。
    "media_base_url": "",
    # 用哪套原生协议："auto" = 按域名识别；none = 明确不接；aliyun = 阿里云 DashScope 形状
    "media_api": "auto",
    # 个别模型要额外 parameters（各家档位不一致，留个不用改代码的口子）
    "media_extra_params": {},
    # 单价（元）：**按模型记，不按服务商**，且**按链路分三张表**（2026-10-07 W 定：
    # 成本估算按 文本 / 生图 / 生视频 拆开，计费单位也各自一套）。同一家下
    # happyhorse-1.0 与 1.1 就不同价，MiniMax 的 H3 按秒、Hailuo 按条 ——
    # 挂在服务商上必然算错。形状：{"模型名": {"price": 0.5, "unit": …}}；
    # 空 = 没填，界面明说"以账单为准"，绝不编一个数字出来。
    # 旧的单张 media_prices 由 normalize_provider 按 model_kinds / 单位自动拆进两张
    # 媒体表（幂等，拆完即弃）。
    "text_prices": {},             # 云端文本：元/千token
    "image_prices": {},            # 云端生图：元/张
    "video_prices": {},            # 云端生视频：元/秒、元/条
}

# 阿里云原生媒体接口可用的域名（文档 §12.1/§12.3：Token Plan 专属域名与百炼按量的
# 工作空间域名同族，所以共用一个适配器）
_ALI_HOST_SUFFIXES = ("maas.aliyuncs.com", "dashscope.aliyuncs.com",
                      "dashscope-intl.aliyuncs.com")
# v40 新增的三家：主域名一认就够，路径与字段名都留在 connection 层里分派
_MINIMAX_HOSTS = ("minimaxi.com", "minimax.chat", "minimax.cn", "minimax.io")
_ZHIPU_HOSTS = ("bigmodel.cn", "zhipuai.cn", "z.ai")
_HUAWEI_HOSTS = ("modelarts-maas.com",)
MEDIA_ALIYUN = "aliyun"
MEDIA_MINIMAX = "minimax"
MEDIA_ZHIPU = "zhipu"
MEDIA_HUAWEI = "huawei"
MEDIA_APIS = ("auto", "none", MEDIA_ALIYUN, MEDIA_MINIMAX, MEDIA_ZHIPU, MEDIA_HUAWEI)
MEDIA_LABEL = {MEDIA_ALIYUN: "阿里云原生（DashScope）", MEDIA_MINIMAX: "MiniMax 原生",
               MEDIA_ZHIPU: "智谱GLM 原生", MEDIA_HUAWEI: "华为云MaaS 原生"}
# 原生路径**自带版本段**（/v1/...）的协议：根地址只取到"协议+主机"，
# 否则从 base_url 推根时会把 /openai 这种中间段留在根里，拼出 404
_HOST_ONLY_MEDIA = (MEDIA_MINIMAX, MEDIA_HUAWEI)


def _host_of(url):
    s = str(url or "").strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    return s.split("/", 1)[0].split(":", 1)[0]


def _ends_with(host, suffixes):
    return any(host == s or host.endswith("." + s) for s in suffixes)


def media_api(p):
    """这个 provider 的云端生图/生视频走哪套协议；"" 表示认不出来（不该放行）。"""
    p = p or {}
    want = str(p.get("media_api") or "auto").strip().lower()
    if want == "none":
        return ""
    if want in MEDIA_APIS and want != "auto":
        return want
    for src in (p.get("media_base_url"), p.get("base_url")):
        host = _host_of(src)
        if not host:
            continue
        if _ends_with(host, _ALI_HOST_SUFFIXES):
            return MEDIA_ALIYUN
        if _ends_with(host, _MINIMAX_HOSTS):
            return MEDIA_MINIMAX
        if _ends_with(host, _ZHIPU_HOSTS):
            return MEDIA_ZHIPU
        if _ends_with(host, _HUAWEI_HOSTS):
            return MEDIA_HUAWEI
    return ""


def media_api_root(p):
    """原生接口的根地址（不含各协议自己的路径）。

    优先用显式填的 media_base_url，否则从 base_url 剥掉兼容模式那一段：
      https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
        → https://token-plan.cn-beijing.maas.aliyuncs.com
      https://xxx.cn-beijing.maas.aliyuncs.com/api/v1 → https://xxx.cn-beijing.maas.aliyuncs.com
    key 与域名仍不可混用（sk-sp- 打公共域名必 401），所以这里只换路径、绝不换主机。

    MiniMax / 华为这两套的原生路径自己带版本段（/v1/image_generation、
    /v1/images/generations），根地址一律收到主机为止；智谱的原生路径就挂在
    base_url 那一段后面（/api/paas/v4/images/generations），所以保持不剥。
    """
    p = p or {}
    src = str(p.get("media_base_url") or "").strip() or str(p.get("base_url") or "").strip()
    if not src:
        return ""
    if "://" not in src:
        return ""
    if media_api(p) in _HOST_ONLY_MEDIA:
        scheme, rest = src.split("://", 1)
        return "%s://%s" % (scheme, rest.split("/", 1)[0].rstrip("/"))
    low = src.lower()
    # 只剥**一个**尾巴，剥完就停：写成"逐个 endswith 就 cut"会把 /compatible-mode/v1
    # 只砍掉 /v1，剩下 .../compatible-mode 拼出个不存在的地址（实测踩过，接口全 404）
    for seg in ("/compatible-mode/v1", "/api/v1", "/v1"):
        if low.endswith(seg):
            src = src[:len(src) - len(seg)]
            break
    return src.rstrip("/")


def api_key_of(p):
    from . import secrets
    return secrets.get_api_key((p or {}).get("id"))


def media_extra_params(p, model):
    """按模型取额外的 parameters（没有就是空 dict，交给服务端默认）。"""
    ep = (p or {}).get("media_extra_params")
    if not isinstance(ep, dict):
        return {}
    val = ep.get(str(model or ""))
    if val is None:
        val = ep.get("*")
    return dict(val) if isinstance(val, dict) else {}


# ---- 单价与计费单位（2026-10-07 W 定：成本估算按 文本 / 生图 / 生视频 三条链路拆开，
# 计费单位与价格表也各自一套，不再共用一张 media_prices）----
# 各家计费口径不同这件事是实测出来的：H3 ≈0.50 元/秒、Hailuo 768P/6s ≈2 元/条、
# 图像按张 ≈0.025~0.5 元、文本按千 token —— 只给"元/秒 + 元/张"两档就会把按条的模型算成 0。
TEXT_PRICE_UNIT = "千token"
UNITS_BY_KIND = {KIND_TEXT: (TEXT_PRICE_UNIT,), KIND_IMAGE: ("张",),
                 KIND_VIDEO: ("秒", "条")}
# 旧单表时代的媒体单位集合（秒/张/条）：现在只用于 media_prices 的迁移判据
PRICE_UNITS = UNITS_BY_KIND[KIND_IMAGE] + UNITS_BY_KIND[KIND_VIDEO]
_DEFAULT_UNIT = {KIND_TEXT: TEXT_PRICE_UNIT, KIND_IMAGE: "张", KIND_VIDEO: "秒"}
_PRICE_KEY = {KIND_TEXT: "text_prices", KIND_IMAGE: "image_prices",
              KIND_VIDEO: "video_prices"}


def _norm_prices(raw, kind=None):
    """把 provider 记录里的单价表洗成 {模型: {"price": float>0, "unit": 该链路认识的单位}}。

    `kind` 给链路时按 `UNITS_BY_KIND[kind]` 认单位（跨链路的单位进不了表）；
    不给 kind（None）= 迁移旧 media_prices 用的宽口径（秒/张/条都认）。
    """
    units = UNITS_BY_KIND.get(kind, PRICE_UNITS) if kind is not None else PRICE_UNITS
    out = {}
    if not isinstance(raw, dict):
        return out
    for m, e in raw.items():
        m = str(m or "").strip()
        if not m or not isinstance(e, dict):
            continue
        try:
            per = float(e.get("price") or 0)
        except Exception:
            continue
        u = str(e.get("unit") or "").strip()
        if per > 0 and u in units:
            out[m] = {"price": per, "unit": u}
    return out


def _split_legacy_media_prices(out):
    """旧单张 media_prices → 生图 / 生视频两张表（幂等：旧表不在就什么都不做）。

    归属判据先用 model_kinds（勾选窗口记下的能力），记不了就按单位退（张=生图、
    秒/条=生视频）—— 媒体表里不该有文本模型，认不出的一律进生视频（宁可显式可见，
    不静默丢价）。拆完把旧表整个弃掉。
    """
    legacy = out.get("media_prices")
    if isinstance(legacy, dict) and legacy:
        kinds = out.get("model_kinds") or {}
        for m, e in _norm_prices(legacy).items():
            k = kinds.get(m)
            if k not in (KIND_IMAGE, KIND_VIDEO):
                k = KIND_IMAGE if e["unit"] == "张" else KIND_VIDEO
            key = _PRICE_KEY[k]
            tbl = _norm_prices(out.get(key), k)
            tbl.setdefault(m, e)
            out[key] = tbl
    out.pop("media_prices", None)


def price_of(p, model, kind):
    """某个模型在**某条链路**上的单价与计费单位 → (元, 单位)；没填或形状不对 → (0.0, "")。"""
    key = _PRICE_KEY.get(kind)
    if not key:
        return (0.0, "")
    e = _norm_prices((p or {}).get(key), kind).get(str(model or "").strip())
    return (e["price"], e["unit"]) if e else (0.0, "")


def set_price(cfg, pid, model, price, unit, kind):
    """写某个模型在某条链路上的单价（元）。**price<=0 = 删掉这条记录**；
    单位不属于这条链路 = 直接拒写。

    单位不认时不能顺手删：那会让一个手滑的配置值把已经填好的单价抹掉，
    而"抹掉"在界面上长得像"我没填过" —— 拒写返回 False，让调用方去解释。
    只改内存里的 cfg，**落盘由调用方负责**（与 update_provider 同一条约定）。
    """
    model = str(model or "").strip()
    key = _PRICE_KEY.get(kind)
    if not model or not key:
        return False
    p = get_provider(cfg, pid)
    if not p:
        return False
    try:
        per = float(price)
    except Exception:
        return False
    unit = str(unit or "").strip()
    if per > 0 and unit not in UNITS_BY_KIND[kind]:
        return False
    prices = _norm_prices(p.get(key), kind)
    if per <= 0:
        prices.pop(model, None)
    else:
        prices[model] = {"price": per, "unit": unit}
    return bool(update_provider(cfg, pid, {key: prices}))


def price_table(p, kind):
    """这家某条链路填过的单价表 → {模型名: {"price": float, "unit": str}}（形状已洗净）。"""
    key = _PRICE_KEY.get(kind)
    if not key:
        return {}
    return _norm_prices((p or {}).get(key), kind)


def default_unit(model=None, kind=None):
    """没填过时给个起始计费单位：文本按千token、图片按张、视频按秒（认不出能力就按秒）。"""
    k = kind or guess_kind(model)
    return _DEFAULT_UNIT.get(k, "秒")


def priced_models(p, kind=None):
    """这家填过单价的模型名（kind 给链路就只列那张表；不给 = 三张表合并）。"""
    p = p or {}
    keys = ([_PRICE_KEY[kind]] if kind in _PRICE_KEY else list(_PRICE_KEY.values()))
    out = set()
    for key in keys:
        out.update(_norm_prices(p.get(key)))
    return sorted(out)


def media_price_note(p, kind, seconds=0, model=""):
    """费用预估一行；单价没填就明说不知道，别拍一个数字出来（坑 55 的延伸：不编数）。

    单价**按模型**查 —— 同一家不同模型不同价、不同单位，按服务商查是这次改掉的那个错。
    """
    per, unit = price_of(p, model, kind)
    if per <= 0:
        return ("模型「%s」的单价未填（设置 → 云端模型 → 成本估算），"
                "费用以服务商账单为准。" % model if model else
                "单价未填（设置 → 云端模型 → 成本估算），费用以服务商账单为准。")
    if unit == "秒":
        n = int(seconds or 0)
        return "预估 %.2f 元/秒 × %d 秒 ≈ %.2f 元。" % (per, n, per * n)
    if unit == "张":
        return "预估 %.2f 元/张。" % per
    return "预估 %.2f 元/条。" % per


def text_cost_note(p, model, usage):
    """云端文本每轮「用量」行尾的费用预估；单价没填返回空串（不编数字，同 media_price_note）。

    口径：(输入 + 输出) 合计 token ÷ 1000 × 单价（元/千token）。各家 input/output
    实际不同价，这里只有一个均价档 —— 是预估不是账单，行尾措辞也写"约"。
    """
    per, _unit = price_of(p, model, KIND_TEXT)
    if per <= 0 or not isinstance(usage, dict):
        return ""

    def pick(*names):
        for n in names:
            v = usage.get(n)
            if isinstance(v, (int, float)):
                return int(v)
        return None

    tot = pick("total_tokens")
    if tot is None:
        tot = (pick("prompt_tokens", "input_tokens") or 0) \
            + (pick("completion_tokens", "output_tokens") or 0)
    if tot <= 0:
        return ""
    return "、费用约 %.2f 元" % (per * tot / 1000.0)


def supports_media(p, kind):
    """云端生图/生视频这条能不能走：协议认得 + 有根地址（密钥在 validate_for_send 里查）。"""
    p = p or {}
    if kind not in (KIND_IMAGE, KIND_VIDEO):
        return True
    return bool(media_api(p) and media_api_root(p))


# 收参考图（图生图）的原生协议。判据只认**各家官方 API 页写明的字段**：
#   阿里云 `multimodal-generation/generation` → content[] 里放 {"image": URL 或 data:image/...;base64,...}
#   MiniMax `/v1/image_generation` → subject_reference[].image_file（同样收 URL 或 base64 data URL）
# 智谱的 `/images/generations` 请求体里只有 prompt/model/size 一类字段，官方页没有参考图入口；
# 华为那页没查过（开发机没密钥），所以两家都不放行 —— 放行错了就是白扣一次费。
#
# 2026-10-01 真机各跑一张（图=红黄斜条纹，提示词"改成夜晚蓝紫色调、其余不变"）：
#   阿里云（Token Plan 与百炼按量）出图**保住了条纹的几何结构**，Token Plan 的 usage 里
#   还回显 input_image_count=1 / rewrite_status=success → 这条是真正的"底图重绘"。
#   MiniMax 的 subject_reference 官方定位就是**主体/角色参考**（type 只有 character 一种），
#   喂条纹图它直接生成一个蓝紫夜景的人像 → 图被"用"了，但用的不是底图那条语义。
#
# 2026-10-06 W 给的新事实：**两家两种模式都支持**（此前这里只登记了各自那一种默认档）。
# 上面那段真机结论仍然成立 —— 它说的是"各自**默认**那一档发出去是什么语义"，
# 不等于另一档不支持。所以拆成两张表：
#   REF_DEFAULT_MODE  这一家**默认**走哪一档（回显与不选时的行为，= 2026-10-01 实测那档）
#   REF_MODES         这一家**支持**哪几档（用户可选的范围）
# ⚠ 两档各自对应哪个请求字段、以及 MiniMax 那一档有没有与默认档不同的入参形状，
#   **尚未真机确认** —— 已登记 `99-待确认规则清单`，确认前不把任何一档说成实测。
REF_IMAGE_APIS = (MEDIA_ALIYUN, MEDIA_MINIMAX)
REF_EDIT_APIS = (MEDIA_ALIYUN,)          # 默认档 = 底图重绘
REF_SUBJECT_APIS = (MEDIA_MINIMAX,)      # 默认档 = 主体 / 角色一致性
# 模式标识与 `capability.MODE_*` 同一套字面量（那边是权威，这里不反向 import 免得成环；
# 一致性由 `_selftest/test_cloud_ref_image.py` 断言钉住）。
REF_MODE_EDIT = "edit"
REF_MODE_SUBJECT = "subject"
REF_MODES = {
    MEDIA_ALIYUN: (REF_MODE_EDIT, REF_MODE_SUBJECT),
    # ⚠ **只有主体参考一档**（2026-10-06 真机实测，`D:\tmp\ref_verify\`）：官方页的
    # `ImageGenerationReq` 只有 `subject_reference` 一个输入图字段，`type` 仅 `character`＝人像，
    # 没有任何底图重绘入参。实测喂四格色块图 + "把色块改成蓝紫、位置不变"，出来的是
    # **一张人像**（把"四个色块"理解成左右色调分区），布局没保住 → 改不了图。
    # 所以别把 subject_reference 当成两档通用的入口 —— 选了 edit 会被 preflight 拦下。
    MEDIA_MINIMAX: (REF_MODE_SUBJECT,),
}


def supports_ref_image(p):
    """这一家的云端生图能不能带参考图（能力判定层与 📎 入口都问它，别在界面里各写一遍）。

    走 `media_api()` 而不是直接读字段：它同时处理"手动指定的协议"与"按域名自动认"两种来源。
    """
    return media_api(p) in REF_IMAGE_APIS


def ref_image_modes(p):
    """这一家**支持**哪几档模式（用户可选的范围）。没登记原生协议的 → 空元组。

    问它判"能不能选"，`ref_image_mode()` 判"默认是哪一档"，两件事别混。
    """
    return REF_MODES.get(media_api(p), ())


def supports_ref_mode(p, mode):
    return str(mode or "") in ref_image_modes(p)


def ref_image_mode(p):
    """默认档：这一家不选模式时按哪一档发（2026-10-01 真机实测的那一档）。

    回显与提示都问它。写成两种而不是"都叫图生图"：把 MiniMax 的角色参考说成底图重绘，
    用户会以为出来的是同一张图换了个色调。
    """
    return REF_MODE_SUBJECT if media_api(p) in REF_SUBJECT_APIS else REF_MODE_EDIT



_VER_TAIL = re.compile(r"/v\d+(\.\d+)?$", re.I)


def normalize_base_url(url):
    """规范化 base_url：去尾部斜杠，末尾没有版本号才补 /v1。

    OpenAI 兼容端的 chat 路径是 {base}/chat/completions，而用户粘贴的地址
    有时带 /v1 有时不带（OpenRouter、DashScope compatible-mode 都带）。
    各家自带的版本段不一样（智谱 /api/paas/v4、华为 /openai/v1），一律"补 /v1"
    会拼出 /api/paas/v4/v1 这种根本不存在的路径 → 认 /v数字 的尾巴。
    """
    u = str(url or "").strip().rstrip("/")
    if not u:
        return ""
    if _VER_TAIL.search(u):
        return u
    return u + "/v1"


def chat_completions_url(provider):
    return normalize_base_url((provider or {}).get("base_url")) + "/chat/completions"


def normalize_provider(p):
    """补齐缺字段，保证下游拿到的 provider 结构完整。

    内置服务商（BUILTIN_PROVIDERS）的 name / base_url **一律以代码里的为准**：
    设置页对这两项不开放输入，跨计费方案的域名写错只会换来一个 401，不如不让改。
    """
    out = dict(PROVIDER_TEMPLATE)
    out.update({k: v for k, v in dict(p or {}).items() if v is not None})
    out["id"] = str(out.get("id") or "").strip()
    b = BUILTIN_PROVIDERS.get(out["id"])
    if b:
        out["name"] = b["name"]
        if builtin_base_editable(out["id"]):
            # 按量百炼的工作空间域名因账号而异，只能用内置值当"没填时的默认"
            out["base_url"] = str(out.get("base_url") or "").strip() or b["base_url"]
        else:
            out["base_url"] = b["base_url"]
    out["name"] = str(out.get("name") or "").strip() or out["id"]
    out["base_url"] = str(out.get("base_url") or "").strip()
    out["kind"] = out.get("kind") if out.get("kind") in KINDS else KIND_TEXT
    models = out.get("models") or []
    if isinstance(models, str):                 # 允许用逗号/换行分隔的字符串填
        models = [m.strip() for m in models.replace("\n", ",").split(",")]
    out["models"] = [str(m).strip() for m in models if str(m).strip()]
    kinds = out.get("model_kinds")
    if not isinstance(kinds, dict):
        kinds = {}
    out["model_kinds"] = {str(m): (k if k in KINDS else KIND_TEXT)
                          for m, k in kinds.items() if str(m)}
    try:
        out["ctx"] = max(0, int(out.get("ctx") or 0))
    except Exception:
        out["ctx"] = 0
    try:
        out["timeout"] = max(15, int(out.get("timeout") or 600))
    except Exception:
        out["timeout"] = 600
    out["enabled"] = bool(out.get("enabled", True))
    if not isinstance(out.get("extra_headers"), dict):
        out["extra_headers"] = {}
    out["media_base_url"] = str(out.get("media_base_url") or "").strip()
    if b and not out["media_base_url"]:
        # 内置条目可以预置"原生接口地址"（华为的聊天与生图挂在同主机的不同前缀下），
        # 用户自己填过就以用户的为准
        out["media_base_url"] = str(b.get("media_base_url") or "").strip()
    ma = str(out.get("media_api") or "auto").strip().lower()
    out["media_api"] = ma if ma in MEDIA_APIS else "auto"
    if not isinstance(out.get("media_extra_params"), dict):
        out["media_extra_params"] = {}
    # 单价三张表（文本 / 生图 / 生视频各一张）：先把旧单张 media_prices 拆掉（幂等），
    # 再逐张洗净 —— 跨链路的单位进不了各自的表（2026-10-07 拆分，见 _PRICE_KEY）
    _split_legacy_media_prices(out)
    for _k, _key in _PRICE_KEY.items():
        out[_key] = _norm_prices(out.get(_key), _k)
    # 旧的"按服务商单价"两个字段直接作废、不迁移也不回退：一家多价、多种计费单位，
    # 挂在服务商上给出的预估就是错价（W 定的口径，2026-10-01）
    out.pop("price_per_image", None)
    out.pop("price_per_second", None)
    return out


def ensure_builtin_providers(cfg):
    """把内置服务商种进配置里（幂等）；**模型清单留空**，由用户在模型选择界面勾选。

    只做"没有就补上"，不覆盖已有清单——用户在这里删掉的模型不该下次启动又回来。
    """
    added = []
    lst = cfg.setdefault("cloud_providers", [])
    have = {str((p or {}).get("id", "")) for p in lst}
    for pid in BUILTIN_PROVIDERS:
        if pid in have:
            continue
        ok, _err = add_provider(cfg, {"id": pid, "models": [], "model_kinds": {}})
        if ok:
            added.append(pid)
    return added


def models_list_url(provider):
    """OpenAI 兼容的模型清单地址：与 chat 同一个前缀（{base}/models）。

    以前是硬剥掉 /v1 再补回 /v1/models —— 各家自带的版本段不同（智谱 /api/paas/v4），
    那样会拼出 /api/paas/v4/v1/models 这种不存在的地址。
    """
    return normalize_base_url((provider or {}).get("base_url")) + "/models"



def list_providers(cfg, enabled_only=True):
    out = []
    for p in cfg.get("cloud_providers") or []:
        np = normalize_provider(p)
        if np["id"] and (np["enabled"] or not enabled_only):
            out.append(np)
    return out


def get_provider(cfg, pid):
    for p in list_providers(cfg, enabled_only=False):
        if p["id"] == str(pid or ""):
            return p
    return None


def add_provider(cfg, provider):
    """新增；返回 (ok, 错误说明)。id 唯一且非空，base_url 必填。"""
    p = normalize_provider(provider)
    if not p["id"]:
        return False, "provider id 不能为空（用作密钥的索引，建议英文短名）"
    if any(c in p["id"] for c in (" ", CLOUD_SEP[0], CLOUD_SEP[1])):
        return False, "provider id 不能含空格或冒号（模型 id 用 :: 拼接）"
    if not p["base_url"]:
        return False, "base_url 不能为空"
    if get_provider(cfg, p["id"]):
        return False, "id 已存在：%s" % p["id"]
    cfg.setdefault("cloud_providers", []).append(p)
    return True, ""


def update_provider(cfg, pid, patch):
    """按 id 更新字段；不允许改 id 本身（改了会丢密钥对应关系）。"""
    lst = cfg.setdefault("cloud_providers", [])
    for i, p in enumerate(lst):
        if str((p or {}).get("id", "")) == str(pid):
            merged = dict(p)
            merged.update({k: v for k, v in dict(patch or {}).items() if k != "id"})
            lst[i] = normalize_provider(merged)
            return True
    return False


def remove_provider(cfg, pid):
    lst = cfg.get("cloud_providers") or []
    keep = [p for p in lst if str((p or {}).get("id", "")) != str(pid)]
    if len(keep) == len(lst):
        return False
    cfg["cloud_providers"] = keep
    return True


def make_cloud_id(pid, model):
    return "%s%s%s" % (pid, CLOUD_SEP, model)


def split_cloud_id(s):
    """拆 "<pid>::<model>"；不是云 id 返回 None。"""
    s = str(s or "")
    if CLOUD_SEP not in s:
        return None
    pid, model = s.split(CLOUD_SEP, 1)
    return (pid, model) if pid and model else None


def is_cloud(cfg):
    """当前选中的模型是不是云端模型（以 model_provider 为准）。"""
    return str(cfg.get("model_provider", LOCAL) or LOCAL) != LOCAL


def current_provider(cfg):
    """当前选中模型所属的 provider（本地返回 None）。"""
    if not is_cloud(cfg):
        return None
    return get_provider(cfg, cfg.get("model_provider"))


def cloud_models(cfg, kind=KIND_TEXT):
    """可用云模型清单：[(provider_id, provider_name, model), ...]，按能力筛。"""
    out = []
    for p in list_providers(cfg):
        for m in p["models"]:
            if model_kind_of(p, m) == kind:
                out.append((p["id"], p["name"], m))
    return out


# ---------------------------------------------------------------------------
# 云模型显示名缩写（v40）
#
# 各家名字动辄带完整发布日期，菜单一行放不下。本地那套 models.make_alias 走的是
# GGUF 文件名的规则（量化后缀 q3_k_xl→q3、噪声词 ud-/ggml-），云名字里没有这些东西，
# 能复用的只有"按连字符截断"这一招。
#
# 关键约束：**只压缩结构、不删词**。删 -latest / -128k / -preview 这类尾巴看着更短，
# 但 `qwen-plus` 与 `qwen-plus-latest` 会缩写成同一个名字，而用户在菜单里看不出差别
# —— 所以缩写程度交给 short_labels() 逐级退回：整批唯一就用最简形式，撞车才变长。
# ---------------------------------------------------------------------------
SHORT_MAX_LEN = 30      # 与本地 make_alias 的截断长度同一个数：两处菜单行宽看着一致

_DATE_YMD = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_VER_DOTTED = re.compile(r"\d+(\.\d+)+[a-z]?")     # 2.0 / 4.6v / 3.14


def _cut(s, n):
    """按连字符边界截到 n 字符内（沿用 make_alias 的截法：不拦腰切断一个词）。"""
    if len(s) <= n:
        return s
    head = s[:n]
    return head[:head.rfind("-")] if "-" in head[4:] else head


def _md(mo, dd):
    """月/日取值范围要卡：八位数字的型号 id（`12345678`）不该被当成日期截掉前四位。"""
    if not (1 <= int(mo) <= 12 and 1 <= int(dd) <= 31):
        return None
    return "%s%s" % (mo, dd)


def _date_seg(seg):
    """发布日期 → MMDD；None 表示不是日期。认四种写法：
    `2026-03-03`、`20260303`、`250304`(YYMMDD，智谱爱用)、`0303`(已经压过的)。"""
    if not seg.isdigit():
        return None
    if len(seg) == 8:
        return _md(seg[4:6], seg[6:])
    if len(seg) == 6:
        return _md(seg[2:4], seg[4:])
    if len(seg) == 4:
        return seg if _md(seg[:2], seg[2:]) else None
    return None


def _short_seg(s, merge_ver=True):
    """日期压成 MMDD、点分版本号贴到核心标识后面、命名空间重复的厂商名只留一次。"""
    if "/" in s:
        head, tail = s.split("/", 1)
        # 百炼托管第三方写成 `MiniMax/MiniMax-H3`：厂商名重复两次，留后面那次
        if tail.lower().startswith(head.lower()):
            s = tail
    # 日期**先**整段替换再按连字符切：`2026-03-03` 自己就带两个连字符，
    # 先切后认会把 "2026"、"03"、"03" 当三个普通片段（第一版就是这么错的）
    s = _DATE_YMD.sub(lambda m: _date_seg(m.group(0).replace("-", ""))
                      or m.group(0), s)
    out, prev_ver = [], True
    for seg in s.split("-"):
        d = _date_seg(seg)
        if d is not None:
            out.append(d)
            prev_ver = False
            continue
        ver = bool(_VER_DOTTED.fullmatch(seg))
        if ver and merge_ver and out and not prev_ver:
            # 只粘**点分**版本号：`qwen-image-2.0`→`qwen-image2.0` 是 W 给的样例，
            # 而 `MiniMax-Hailuo-02`、`cogview-4` 这类纯数字尾巴粘上去反而读不出边界
            out[-1] = out[-1] + seg
            prev_ver = True
            continue
        out.append(seg)
        prev_ver = ver
    return "-".join(out)


def _short_at(model, level):
    s = str(model or "")
    if level >= 3:
        return s
    s = _short_seg(s, merge_ver=(level < 2))
    if level == 0:
        s = _cut(s, SHORT_MAX_LEN)
    return s


def short_of(model):
    """单个模型的缩写显示名（列表里要用 short_labels()：那里才判得出会不会撞车）。"""
    return _short_at(model, 0)


def short_labels(models):
    """一批模型 → {原名: 缩写}，并保证**批内不重名**。

    撞车时不是"直接退回原名"，而是**逐级放宽一档**（先少截断，再连字符回原样，
    最后才是原名）：`…-dates-0102` 与 `…-dates-0103` 这种只差尾数的，
    多给几个字符就能分开，没必要整条原名摆回菜单里。
    """
    names = [str(m or "") for m in (models or []) if str(m or "")]
    lvl = {m: 0 for m in names}
    out = {m: _short_at(m, 0) for m in names}
    for _round in range(4):
        seen = {}
        for lab in out.values():
            seen[lab] = seen.get(lab, 0) + 1
        bad = [m for m, lab in out.items() if seen[lab] > 1]
        if not bad:
            break
        for m in bad:
            lvl[m] = min(lvl[m] + 1, 3)
            out[m] = _short_at(m, lvl[m])
    return out


# ---------------------------------------------------------------------------
# 模型过多时的前缀分组（v40）：一家服务商拉回几十个模型，菜单里翻不动。
# 名字前缀一致的收进一个可展开/折叠的组，默认收起（W 的原话："含 qwen-image 的
# 统一归入一个可展开/折叠的选项组"）。
#
# 分组键取"最深的那个还能装下 ≥2 个成员的前缀"：`qwen-image-2.0-*` 与
# `qwen-image-edit-*` 先分成两个 qwen-image 子族，而不是被"qwen"一口吞掉；
# 只有前两段都唯一的，才退回一段前缀（qwen-plus / qwen-max 这种）；仍然唯一的
# 保持平铺 —— 一个只装得下自己的折叠组纯属碍事。
# ---------------------------------------------------------------------------
FOLD_AT = 15          # 服务商模型数超过这个值才分组（W 定的阈值）
FOLD_MAX_DEPTH = 2    # 前缀最多取两段
REST_KEY = "其他"     # 堆叠生效后，凑不成同族组的零散条目统一收进这一组（菜单与勾选窗口共用）


def _fam_key(model, depth):
    toks = [t for t in str(model or "").split("/")[-1].split("-") if t]
    if not toks:
        return ""
    if len(toks) <= depth:            # 名字本身就短：整名当键，别造出"qwen"这种半截组
        return "-".join(toks)
    return "-".join(toks[:depth])


def fold_groups(models, at=FOLD_AT, max_depth=FOLD_MAX_DEPTH):
    """→ (groups, flat)：groups=[{"key":前缀, "models":[原名]}]，flat=没进组的原名。"""
    names = [str(m or "") for m in (models or []) if str(m or "")]
    if len(names) <= at:
        return [], names
    key_of = {}
    pool = list(names)
    for depth in range(max_depth, 0, -1):
        buckets = {}
        for m in pool:
            buckets.setdefault(_fam_key(m, depth), []).append(m)
        for k, ms in buckets.items():
            # 两段以上才算"家族"；一段前缀留到最后一轮，免得 qwen 把 image 子族吞掉
            if len(ms) >= 2 and (depth == 1 or "-" in k):
                for m in ms:
                    key_of[m] = k
        pool = [m for m in names if m not in key_of]
    groups = []
    index = {}
    for m in names:
        k = key_of.get(m)
        if not k:
            continue
        if k not in index:
            index[k] = len(groups)
            groups.append({"key": k, "models": []})
        groups[index[k]]["models"].append(m)
    flat = [m for m in names if m not in key_of]
    return groups, flat


def display_of_cloud(cfg, model_id):
    """云模型在界面上的显示名：缩写后的模型名 +（云）。"""
    sp = split_cloud_id(model_id)
    if not sp:
        return str(model_id or "")
    return "%s（云）" % short_of(sp[1])



def kind_of_current(cfg):
    """当前选中模型的能力；本地模型返回 None（本地靠 model_kind 自己判）。"""
    if not is_cloud(cfg):
        return None
    p = current_provider(cfg)
    sp = split_cloud_id(cfg.get("model", ""))
    return model_kind_of(p, sp[1]) if (p and sp) else KIND_TEXT


# ---------------------------------------------------------------------------
# 服务商 / 菜单清单的显示层（v41 从 ui/settings.py 搬进来）
#
# 这一层只把注册表翻成"给人看的字"：标签 ↔ pid、某家已进菜单的媒体模型名。
# 放在 core 而不是留在某一个窗口里，理由与 short_labels / fold_groups 相同：
# 显示与标识分离（坑 84 / 97），而且现在有**两个**窗口要用（「服务商与密钥」区
# 与「成本估算」窗口）—— 留在 UI 里就得让两个窗口互相 import。
# ---------------------------------------------------------------------------

def provider_labels(cfg):
    """服务商下拉的显示项 → `[(标签, pid)]`。

    界面上只摆人看得懂的名字（中文优先，DeepSeek / Kimi / MiniMax 这类品牌名保留英文），
    **不再把 `deepseek`、`aliyun-token-plan` 这种代码 id 摊到屏幕上** —— 原来两处下拉
    用的是 id 与「id — 名称」，用户看到的是英文代号，认不出哪家是哪家。
    只有两个服务商重名时（自定义条目同名很常见）才在标签里补 id 区分，否则标签→pid
    的反查会有歧义。
    """
    rows = list_providers(cfg, enabled_only=False)
    names = [str(p.get("name") or p["id"]) for p in rows]
    dup = {n for n in names if names.count(n) > 1}
    out = []
    for p, n in zip(rows, names):
        out.append(("%s（%s）" % (n, p["id"]) if n in dup else n, p["id"]))
    return out


def provider_label_for(cfg, pid, fallback=""):
    """某个服务商给用户看的名字（删除确认、状态回显这类单点场合用）。"""
    for label, one in provider_labels(cfg):
        if one == pid:
            return label
    return (builtin(pid).get("name") or fallback or pid or "")


def media_menu_models(provider, kind=None):
    """这家**已勾进主页面菜单**的媒体模型名（成本窗口只列这些，W 定的口径）。

    `kind` 给链路（image / video）就只列那一条的；不给 = 生图 + 生视频都列（旧口径）。
    媒体模型大多不在各家 `/models` 清单里（§12.1），只能靠「选择模型」窗口的「直接加入」
    手填进菜单 —— 所以这里读的是菜单清单而不是清单缓存：没进菜单的模型本来也用不到。
    """
    want = (kind,) if kind in (KIND_IMAGE, KIND_VIDEO) else (KIND_IMAGE, KIND_VIDEO)
    out = []
    for m in models_in_menu(provider):
        if model_kind_of(provider, m) in want:
            out.append(m)
    return out


def text_menu_models(provider):
    """这家**已勾进主页面菜单**的文本模型名（云端文本的成本窗口只列这些，口径同上）。"""
    return [m for m in models_in_menu(provider)
            if model_kind_of(provider, m) == KIND_TEXT]


def validate_for_send(cfg):
    """发送前的可操作校验；返回 None 表示可以发，否则返回给用户看的原因。"""
    pid = cfg.get("model_provider")
    p = get_provider(cfg, pid)
    if not p:
        return "云端 provider「%s」不存在或已被删除——请在 设置 → 云端模型 → 服务商与密钥 里检查。" % pid
    if not p["enabled"]:
        return "云端 provider「%s」已停用——请在 设置 → 云端模型 → 服务商与密钥 里勾选启用。" % p["name"]
    if not p["base_url"]:
        return "云端 provider「%s」没填 base_url。" % p["name"]
    sp = split_cloud_id(cfg.get("model", ""))
    if not sp:
        return "当前云模型标识无效：%r" % cfg.get("model")
    if sp[1] not in p["models"]:
        return "模型「%s」不在 provider「%s」的清单里（可能被改过），请重新在菜单里选一次。" % (
            sp[1], p["name"])
    k = model_kind_of(p, sp[1])
    from . import secrets
    if k == KIND_TEXT:
        if not secrets.has_api_key(p["id"]):
            return ("还没给「%s」填 API Key——设置 → 云端模型 → 服务商与密钥 → 密钥"
                    "（密钥单独存在 secrets.json，不会跟配置一起被复制走）。" % p["name"])
        return None
    # 生图 / 生视频走厂商**原生**协议（没有 OpenAI 兼容格式）：先确认这个 provider
    # 认得协议、有根地址、有密钥，否则拦在发送前，别掉进本地 sd-cli 分支
    if not media_api(p):
        return ("「%s」是%s模型，走的是服务商原生接口，而 provider「%s」没被认出支持这一套。"
                "\n  解决：在 设置 → 云端模型 → 服务商与密钥 里把该服务商的「原生接口协议」"
                "选成上面这些之一：%s；或者填「原生接口地址」。"
                "\n  （%s）"
                % (sp[1], KIND_LABEL[k], p["name"],
                   "、".join(MEDIA_LABEL[a] for a in (MEDIA_ALIYUN, MEDIA_MINIMAX,
                                                   MEDIA_ZHIPU, MEDIA_HUAWEI)),
                   "只有官方给了 API 的才接得进来；这家若官方没有生图/生视频接口，"
                   "就请在本地那一组里选模型"))
    if not secrets.has_api_key(p["id"]):
        return ("还没给「%s」填 API Key——设置 → 云端模型 → 服务商与密钥 → 密钥"
                "（密钥单独存在 secrets.json，不会跟配置一起被复制走）。"
                % p["name"])
    if not media_api_root(p):
        return ("provider「%s」推不出原生接口地址：请在 设置 → 云端模型 → 服务商与密钥 里填「原生接口地址」。"
                % p["name"])
    return None
