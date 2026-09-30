# -*- coding: utf-8 -*-
"""llm_console.core.providers — 云端 provider 注册表与云模型清单。

一期只有 OpenAI 兼容的文本能力（kind="text"）；生图 / 生视频走厂商原生协议，
在二三期接入时按 kind 分派到 connection/cloud.py 里对应的实现，**厂商字段名不允许
扩散到 UI 层**（UI 只见这里给出的统一结构）。

云模型的身份是 "<provider_id>::<model>"：本地模型用 GGUF 路径标识，云模型没有文件，
用复合 id 存进 cfg["model"]——它不含路径分隔符，所以 os.path.basename() 之类的既有
代码不会炸；而"当前是不是云端"一律以 cfg["model_provider"] 为准，不靠字符串猜。
"""

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
}


def is_builtin(pid):
    return str(pid or "") in BUILTIN_PROVIDERS


def builtin(pid):
    return dict(BUILTIN_PROVIDERS.get(str(pid or "")) or {})


def guess_kind(model):
    """按名字猜能力，只作为选择界面里的**默认勾选**，用户可以改。"""
    s = str(model or "").lower()
    if any(k in s for k in ("image", "img2", "text2img", "wan2")) and "video" not in s:
        return KIND_IMAGE
    if any(k in s for k in ("video", "t2v", "i2v", "r2v", "s2v", "happyhorse")):
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
}


def normalize_base_url(url):
    """规范化 base_url：去尾部斜杠，缺 /v1 就补上。

    OpenAI 兼容端的 chat 路径是 {base}/chat/completions，而用户粘贴的地址
    有时带 /v1 有时不带（OpenRouter、DashScope compatible-mode 都带）。
    设置页会把最终请求地址回显出来，拼错能一眼看到。
    """
    u = str(url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.endswith("/v1"):
        u += "/v1"
    return u


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
    """OpenAI 兼容的模型清单地址：{base}/models（base 已含 /v1）。"""
    b = normalize_base_url((provider or {}).get("base_url"))
    return (b[:-len("/v1")] if b.endswith("/v1") else b) + "/v1/models"


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


def display_of_cloud(cfg, model_id):
    """云模型在界面上的显示名：<模型名>（云）。"""
    sp = split_cloud_id(model_id)
    if not sp:
        return str(model_id or "")
    return "%s（云）" % sp[1]


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
        return "云端 provider「%s」不存在或已被删除——请在 设置 → 云端 API 里检查。" % pid
    if not p["enabled"]:
        return "云端 provider「%s」已停用——请在 设置 → 云端 API 里勾选启用。" % p["name"]
    if not p["base_url"]:
        return "云端 provider「%s」没填 base_url。" % p["name"]
    sp = split_cloud_id(cfg.get("model", ""))
    if not sp:
        return "当前云模型标识无效：%r" % cfg.get("model")
    if sp[1] not in p["models"]:
        return "模型「%s」不在 provider「%s」的清单里（可能被改过），请重新在菜单里选一次。" % (
            sp[1], p["name"])
    k = model_kind_of(p, sp[1])
    if k != KIND_TEXT:
        # 能力已经归类，但云端生图/生视频还没实现：拦在这里，别让它去调本地引擎
        return ("「%s」是%s模型，而云端这一路还没实现（按规划在%s期）。"
                "文本对话不受影响；要出图/出片请切回本地模型。"
                % (sp[1], KIND_LABEL[k], "二" if k == KIND_IMAGE else "三"))
    from . import secrets
    if not secrets.has_api_key(p["id"]):
        return ("还没给「%s」填 API Key——设置 → 云端 API → 密钥（存在 secrets.json，"
                "不进备份）。" % p["name"])
    return None
