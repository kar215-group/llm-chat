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
        "name": "智谱 GLM",
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
    # 单价（元）：只在提交前给用户看费用预估用；0 = 未填，界面就明说"以账单为准"，不编数字
    "price_per_image": 0.0,
    "price_per_second": 0.0,
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
               MEDIA_ZHIPU: "智谱 GLM 原生", MEDIA_HUAWEI: "华为云 MaaS 原生"}
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


def media_price_note(p, kind, seconds=0):
    """费用预估一行；单价没填就明说不知道，别拍一个数字出来（坑 55 的延伸：不编数）。"""
    p = p or {}
    try:
        per = float(p.get("price_per_image") or 0) if kind == KIND_IMAGE \
            else float(p.get("price_per_second") or 0)
    except Exception:
        per = 0.0
    if per <= 0:
        return "单价未在设置里填写，费用以服务商账单为准。"
    if kind == KIND_IMAGE:
        return "预估 %.2f 元/张。" % per
    return "预估 %.2f 元/秒 × %d 秒 ≈ %.2f 元。" % (per, int(seconds or 0),
                                                   per * int(seconds or 0))


def supports_media(p, kind):
    """云端生图/生视频这条能不能走：协议认得 + 有根地址（密钥在 validate_for_send 里查）。"""
    p = p or {}
    if kind not in (KIND_IMAGE, KIND_VIDEO):
        return True
    return bool(media_api(p) and media_api_root(p))



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
    for k in ("price_per_image", "price_per_second"):
        try:
            out[k] = max(0.0, float(out.get(k) or 0))
        except Exception:
            out[k] = 0.0
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
            return ("还没给「%s」填 API Key——设置 → 云端模型 → 服务商与密钥 → 密钥（存在 secrets.json，"
                    "不进备份）。" % p["name"])
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
        return ("还没给「%s」填 API Key——设置 → 云端模型 → 服务商与密钥 → 密钥（存在 secrets.json，不进备份）。"
                % p["name"])
    if not media_api_root(p):
        return ("provider「%s」推不出原生接口地址：请在 设置 → 云端模型 → 服务商与密钥 里填「原生接口地址」。"
                % p["name"])
    return None
