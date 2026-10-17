# -*- coding: utf-8 -*-
"""llm_console.connection.cloud_media — 云端原生媒体接口（生图 / 生视频）适配器。

一期文本走 OpenAI 兼容端（connection/cloud.py）。**生图与生视频没有 OpenAI 兼容格式**
（阿里官方明确写过），只有厂商原生协议，所以单独一层。铁律沿用：厂商字段名只允许出现在
connection 层，UI 只消费归一化后的事件与结构。

已实现的协议（v40 起按 providers.media_api() 分派，一套形状发错家就是满屏 404）：
  · **阿里云原生**（DashScope 形状）：Token Plan 专属域名与百炼按量的工作空间域名同族，
    只差 base_url 与 key，共用一个适配器。生图同步、生视频异步。
  · **MiniMax**：生图 `POST /v1/image_generation`（同步，URL 在 `data.image_urls[]`）；
    生视频分两代 —— Hailuo/T2V 走 v1 **三步**（建单 → 轮询拿 `file_id` → 再查
    `/v1/files/retrieve` 才拿得到下载地址，且那个地址只活 1 小时），
    H3 走 v2（请求体是 `content[]`、状态小写、直接给 url）。
  · **智谱 GLM**：生图 `POST {base}/images/generations`（同步，`data[0].url`）；
    生视频 `POST {base}/videos/generations` → 轮询 `GET {base}/async-result/{id}`。
  · **华为云 MaaS**：生图 `POST /v1/images/generations`（同步，**只给 b64**）；
    生视频 `POST /v1/video/generations`（`input` + `parameters` 两段嵌套）→ 同路径轮询。
  · **火山方舟（字节火山引擎 Ark）**：生图 `POST /api/v3/images/generations`（同步，
    `data[].url`，`response_format` 也可要 b64_json）；生视频 `POST /api/v3/contents/
    generations/tasks` → 顶层 `id`（`cgt-…`）→ `GET …/tasks/{id}`（状态**小写**
    `queued/running/succeeded/failed/cancelled/expired`，产物 `content.video_url`）；
    取消 `DELETE …/tasks/{id}`。请求体与 MiniMax v2 同形（`content[]` + 顶层档位），
    但**档位字段名与取值是另一套**：`resolution` 只收小写 `480p/720p/1080p/4k`、
    `ratio` ∈ `16:9/4:3/1:1/3:4/9:16/21:9/adaptive`、`duration` 按模型 [4,30]/[4,15]/[2,12]。
    出处（2026-10-17 逐页核过）：docs.volcengine.com/docs/ark 的
    image-generation-api / create-video-generation-task-api /
    get-video-generation-task-api / cancel-or-delete-video-generation-tasks-api。
    ⚠ **取消这条要特别小心**：官方页写了 DELETE 按当前状态分两种动作 —— `queued` 才是
    取消，而 `succeeded/failed/expired` 上是**删除任务记录、之后再也查不到**；
    running 不支持。所以 `cancel()` 先查一次状态、只在排着队的时候发 DELETE。
    参考图官方页写了（`image`，URL 或 base64），但语义没实测 → 仍不放行（见
    `providers.REF_IMAGE_APIS` 上方那段，坑 101）。
  · 官方**没有**生图/生视频接口的（Kimi；腾讯云混元的生图生视频在 TokenHub/TC3 签名那套
    上、不是这个 API Key 直连的端点）→ 不猜、不接，`providers.validate_for_send` 会拦下并说明。

坑 51/52 的规矩仍然成立：认不出来就明确拒绝，别拿一家形状去发另一家。

范围边界：**云端生图可以吃参考图**，但只放行官方页写明入参格式的两家 ——
阿里云把图放进 `content[]`、MiniMax 放进 `subject_reference[]`，两家的图都支持
"公网 URL 或 base64 data URL"，所以本地图不用先上图床（见下方 REF_LIMITS）。
2026-10-01 三家真机各跑一张：阿里云（Token Plan / 百炼按量）保住了底图的构图，
是真正的图生图；MiniMax 的 `subject_reference` 实测是**主体/角色参考**（喂条纹图
它生成人像），所以界面措辞按 `providers.ref_image_mode` 分开，别一律叫"图生图"。
**云端生视频首帧**：只放行官方页明说收 base64 的万相 i2v；happyhorse、百炼托管 H3、MiniMax H3、方舟等按 `providers.first_frame_mode` 分档（没有首帧 / 官方页写了未实测）拦下，话术分开（C2-4）。

三条来自文档 §12.1 的硬约束（阿里云这条链路，开发机实测过），都在代码里落实：
  · 异步任务端点**必须**带 X-DashScope-Async: enable，缺了直接报"不支持同步调用"；
  · 产物 URL 只活 24 小时（MiniMax 的检索地址更短，1 小时）→ 终态后立即下载落地；
  · 下载用的是临时签名地址，**绝不带 Authorization**（把云密钥发给第三方主机）。
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import texts
from ..core import capability, config, media, providers
from . import cloud

PROTOCOL_ALIYUN = "aliyun"
PROTOCOL_MINIMAX = "minimax"
PROTOCOL_ZHIPU = "zhipu"
PROTOCOL_HUAWEI = "huawei"
PROTOCOL_VOLC = "volcengine"
PROTOCOLS = (PROTOCOL_ALIYUN, PROTOCOL_MINIMAX, PROTOCOL_ZHIPU, PROTOCOL_HUAWEI,
             PROTOCOL_VOLC)
# 任务号放**顶层 `id`** 的那两家（其余家的顶层 id 是请求号，误当任务号写台账 = 造出
# 永远取不回的假任务，坑 202③）
_TOP_ID_PROTOCOLS = (PROTOCOL_ZHIPU, PROTOCOL_VOLC)

# 各家状态字面量不统一（百炼/Token Plan 大写、MiniMax v1 是 PascalCase、v2 小写），
# 一律归一化成这六个，界面只认归一化结果。
_STATUS = {
    "PENDING": "pending", "QUEUED": "pending", "PREPARING": "pending",
    "SUBMITTED": "pending", "QUEUEING": "pending", "WAITING": "pending",
    "RUNNING": "running", "PROCESSING": "running",
    "SUCCEEDED": "succeeded", "SUCCESS": "succeeded",
    "COMPLETED": "succeeded", "DONE": "succeeded", "FINISHED": "succeeded",
    "FAILED": "failed", "ERROR": "failed", "INVALID": "failed", "UNKNOWN_ERROR": "failed",
    "FAIL": "failed",
    # 方舟那一套还有 `expired`（超过 execution_expires_after 没跑成）：既不会出片也回不去，
    # 按失败处理；认不出的字面量一律走 normalize_status 的 "unknown"，绝不落"其余即成功"（坑 192）
    "EXPIRED": "failed",
    "CANCELED": "canceled", "CANCELLED": "canceled",
    "UNKNOWN": "unknown",
}

# 会计费的提交类 POST 超时/断连时的追加提示（排查报告 R-04）：请求可能已到达服务端，
# "没拿到回包"不等于"没建单没扣钱"（与坑 53"提交 200 不等于没扣钱"是同一条纪律的两面）。
_BILL_NOTE = ("注意：请求可能已到达服务端——这一单可能已经创建并计费。"
              "重发之前，请先在服务商控制台核对账单/任务列表。")

_IMG_KEYS = ("image", "image_url", "url", "image_urls", "download_url", "file_url",
             "result_url", "video_url")
_VID_KEYS = ("video_url", "video", "url", "video_urls", "download_url", "file_url",
             "result_url", "video_result")
_B64_KEYS = ("b64_json", "image_base64", "b64")

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
_VID_EXT = (".mp4", ".webm", ".mov", ".avi", ".mkv")


def normalize_status(v):
    return _STATUS.get(str(v or "").strip().upper(), "unknown")


# ---------------------------------------------------------------- 地址与请求

def _root(provider):
    """原生接口的根地址（各协议的版本段位置不同，派生规则见 providers.media_api_root）。"""
    return providers.media_api_root(provider)


def protocol_of(provider):
    """这一单走哪套原生协议。认不出来时**保持阿里云**：老配置全是阿里云系，
    而真正会发出去的路径都先经 check()/validate_for_send() 拦一道。"""
    return providers.media_api(provider) or PROTOCOL_ALIYUN


def _u(provider, tail):
    return _root(provider) + "/api/v1/" + tail.lstrip("/")


def _at(provider, tail):
    """根地址 + 路径（路径自带版本段的那些家用它）。"""
    return _root(provider) + tail


def _mm_v2(model):
    """MiniMax 的两代视频接口：H3 系走 v2（content[] 请求体、状态小写、直接给 url），
    Hailuo / T2V-01 等走 v1（建单 → file_id → 检索下载地址）。"""
    s = str(model or "").lower()
    return "h3" in s


def image_endpoint(provider):
    proto = protocol_of(provider)
    if proto == PROTOCOL_MINIMAX:
        return _at(provider, "/v1/image_generation")
    if proto == PROTOCOL_ZHIPU:
        return _at(provider, "/images/generations")
    if proto == PROTOCOL_HUAWEI:
        return _at(provider, "/v1/images/generations")
    if proto == PROTOCOL_VOLC:
        return _at(provider, "/images/generations")
    return _u(provider, "services/aigc/multimodal-generation/generation")


def video_endpoint(provider, model=""):
    proto = protocol_of(provider)
    if proto == PROTOCOL_MINIMAX:
        return _at(provider, "/v2/video_generation" if _mm_v2(model)
                   else "/v1/video_generation")
    if proto == PROTOCOL_ZHIPU:
        return _at(provider, "/videos/generations")
    if proto == PROTOCOL_HUAWEI:
        return _at(provider, "/v1/video/generations")
    if proto == PROTOCOL_VOLC:
        return _at(provider, "/contents/generations/tasks")
    return _u(provider, "services/aigc/video-generation/video-synthesis")


def task_endpoint(provider, task_id=None, model=""):
    """轮询地址。`model` 只 MiniMax 用得上（v1 是 query 参数、v2 是路径段）。"""
    proto = protocol_of(provider)
    qid = urllib.parse.quote(str(task_id or ""), safe="-_.")
    if proto == PROTOCOL_MINIMAX:
        if _mm_v2(model):
            # v2：GET /v2/query/video_generation/{task_id}（路径参数，不是 query）
            return _at(provider, "/v2/query/video_generation/%s" % qid)
        if not task_id:
            return ""          # v1 没有批量查询端点
        return _at(provider, "/v1/query/video_generation?task_id=%s" % qid)
    if proto == PROTOCOL_ZHIPU:
        return _at(provider, "/async-result/%s" % qid)
    if proto == PROTOCOL_HUAWEI:
        return _at(provider, "/v1/video/generations/%s" % qid)
    if proto == PROTOCOL_VOLC:
        # 带 id 是单查，不带就是官方那条列表端点（分页参数我们不拼，没人批量查）
        return _at(provider, "/contents/generations/tasks%s"
                   % (("/%s" % qid) if task_id else ""))
    if not task_id:
        return _u(provider, "tasks")
    return _u(provider, "tasks/%s" % urllib.parse.quote(str(task_id), safe="-_."))


def file_endpoint(provider, file_id):
    """MiniMax v1 的第二跳：轮询只给 file_id，要再查一次才拿得到真正的下载地址。"""
    return _at(provider, "/v1/files/retrieve?file_id=%s"
               % urllib.parse.quote(str(file_id or ""), safe="-_."))



def _headers(provider, key, extra=None, signed=False):
    """signed=True 用于 OSS 产物地址：那种 URL 的凭证在 query 里，再带 Authorization
    既没必要、又把云密钥发给了非 API 主机。"""
    h = {"User-Agent": config.USER_AGENT}
    if not signed:
        h["Content-Type"] = "application/json"
        h["Authorization"] = "Bearer %s" % key
    h.update(extra or {})
    return h


class _Log(object):
    """逐任务日志（与产物同名 + .log）：把请求体、原始回包、每一步状态都留下。

    沿用生视频链路的做法（ui/video_gen.py 的 _vid_reader）：界面只回显末几行，
    排查"服务端说成功了但文件在哪"这类问题要看的是这份文件。行缓冲，强杀也留得住。
    """

    def __init__(self, path, title=""):
        self.path = path or ""
        self.f = None
        if not self.path:
            return
        try:
            self.f = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            self.f = None
            return
        if title:
            self.w(texts.CME_CAT % (title, time.strftime("%Y-%m-%d %H:%M:%S")))

    def w(self, text):
        if self.f is None:
            return
        try:
            self.f.write(text if text.endswith("\n") else text + "\n")
        except Exception:
            pass

    def json(self, tag, obj):
        try:
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(obj)
        self.w("%s:\n%s" % (tag, pretty))

    def close(self):
        if self.f is not None:
            try:
                self.f.close()
            except Exception:
                pass
            self.f = None


def _request(provider, method, url, body=None, extra=None, timeout=60, signed=False):
    """一次 HTTP：返回 (status, text)。网络层异常也折成负数码 + 人话，而不是抛。

    status：-1 = 根本没连上；-2 = 本地就没通过（缺密钥）；0 保留给"被停止生成打断"。
    """
    key = ""
    if not signed:
        key = providers.api_key_of(provider)
        if not key:
            return -2, (texts.CLD_CAT_18
                        % (provider or {}).get("name", ""))
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers=_headers(provider, key, extra, signed=signed),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return -1, cloud.humanize_net(e, provider)


def _says(text):
    """从错误回包里抠服务端原话（阿里云用 code/message，OpenAI 风格用 error.message）。"""
    try:
        j = json.loads(text)
    except Exception:
        return str(text or "").strip()[:260]
    if not isinstance(j, dict):
        return str(text or "")[:260]
    code = j.get("code")
    msg = j.get("message") or j.get("msg") or ""
    e = j.get("error")
    if isinstance(e, dict):
        msg = msg or e.get("message") or ""
    if code and msg:
        return "%s：%s" % (code, msg)
    return str(code or msg or text)[:260]


_HINTS = (
    ("unpurchased", texts.CME_L1),
    ("accessdenied", texts.CME_L1_2),
    ("arrearage", texts.CME_L1_3),
    ("invalidapikey", texts.CME_L1_4),
    ("url error", texts.CME_L1_5),
    ("datainspection", texts.CME_L1_6),
    ("inappropriate", texts.CME_L1_6),
    ("duration", texts.CME_L1_7),
    ("resolution", texts.CME_L1_8),
    ("size", texts.CME_L1_9),
    ("throttling", texts.CME_L1_10),
    # v40 新增那几家的说法（MiniMax 的 base_resp 码写在错误消息里，这里按关键词兜底）
    ("sensitive", "内容审核没通过（MiniMax 分「输入敏感 1026」和「输出敏感 1027」两种）："
                  "提示词换个说法再试。"),
    ("balance", texts.CME_L1_12),
    ("insufficient", texts.CME_L1_13),
    ("rate limit", texts.CME_L1_10),
    ("not activated", texts.CME_L1_14),
)


def explain(status, text, provider, endpoint=""):
    """HTTP 状态 + 回包 → 一句能行动的话（复用一期的 humanize_http，再补原生接口的暗号）。

    `endpoint` = **本次实际请求的媒体端点**（image/video/task/file/cancel 之一）。humanize_http
    里的 URL 取自 provider 的 `base_url`，那是**文本对话端点**，云端生图/生视频失败时报它会把人
    带去查错方向（Y4，2026-10-06 实测：阿里云生图超时，报错却指向 `/chat/completions`）。把真实
    端点补一行，日志里本就有它（`log.json("请求 POST …")`），这里让失败文案也带上。
    """
    tail = (texts.CME_CAT_3 % endpoint) if endpoint else ""
    if status in (-1, -2, 0) or status is None:
        return str(text or texts.CME_N) + tail
    msg = cloud.humanize_http(status, text, provider)
    # 阿里云的错误码（AccessDenied.Unpurchased / UnsupportedOperation …）比 message
    # 更好定位，但 humanize_http 只取 message——这里补一行，别把它丢掉
    code = ""
    payload = _safe_json(text)
    if isinstance(payload, dict):
        code = str(payload.get("code") or "")
    if code and code not in msg:
        msg += texts.CME_CAT_4 % code
    if endpoint and endpoint not in msg:
        msg += tail
    low = (text or "").lower()
    # 限流 / 网关类状态**先按状态给话**（R-52）：正文里撞巧出现 "duration=" 之类的词
    # 会被子串表抢走、说成"时长档位不对"（429 回包里带 duration 实测过）
    if status == 429 or (isinstance(status, int) and 500 <= status < 600):
        for w, hint in _HINTS:
            if w in ("throttling", "rate limit"):
                return msg + "\n  " + hint
    for word, hint in _HINTS:
        if word in low:
            return msg + "\n  " + hint
    return msg


# ---------------------------------------------------------------- 回包解析

def _collect_http(obj, depth=0):
    """把某个键**下面**所有 http 开头的字符串都收走（不再要求 URL 自带扩展名）。
    MiniMax 的 `data.image_urls: [...]` 就是这个形状：键名已经说明是产物，
    而签名下载地址常常没有 .png/.mp4 后缀。"""
    out = []
    if depth > 8:
        return out
    if isinstance(obj, dict):
        for v in obj.values():
            out += _collect_http(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            out += _collect_http(v, depth + 1)
    elif isinstance(obj, str) and obj.startswith("http"):
        out.append(obj)
    return out


def _walk_urls(obj, keys, exts, depth=0):
    """在任意结构的回包里找出产物 URL。

    为什么不按固定路径取值：qwen-image 走 `output.choices[].message.content[].image`，
    万相系走 `output.task_id` + 轮询回包里的 `output.video_url` / `results[].url`，
    智谱走 `data[0].url` / `video_result[0].url`，MiniMax 走 `data.image_urls[]` 或
    `file.download_url`，华为只有 `content.result_url`；官方页还写明同家不同模型档位
    回包也不一致。取不到时宁可报"回包结构不认识 + 日志已落盘"，
    也不要静悄悄返回一张空图（坑 40：失败原因被界面吞掉是最难查的一种）。
    """
    found = []
    if depth > 8:
        return found
    if isinstance(obj, dict):
        for k, val in obj.items():
            hit = str(k).lower() in keys
            if hit and isinstance(val, str) and val.startswith("http"):
                found.append(val)
            elif hit and isinstance(val, (list, dict)):
                found += _collect_http(val)
            else:
                found += _walk_urls(val, keys, exts, depth + 1)
    elif isinstance(obj, list):
        for it in obj:
            found += _walk_urls(it, keys, exts, depth + 1)
    elif isinstance(obj, str) and obj.startswith("http"):
        low = obj.lower().split("?")[0]
        if any(low.endswith(e) for e in exts):
            found.append(obj)
    out, seen = [], set()
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_urls(payload, kind):
    keys, exts = (_IMG_KEYS, _IMG_EXT) if kind == "image" else (_VID_KEYS, _VID_EXT)
    got = _walk_urls(payload, keys, exts)
    # 带正确扩展名的优先（避免把 request_id 页面上的别的链接当成产物）
    good = [u for u in got if u.lower().split("?")[0].endswith(exts)]
    return good or got


def _task_id_of(payload, protocol=PROTOCOL_ALIYUN):
    """任务号各家放的位置不同：阿里云 `output.task_id`、MiniMax/华为顶层 `task_id`、
    智谱顶层 `id`、火山方舟也是顶层 `id`（`cgt-…`，官方创建端点的响应参数只有这一个）。
    只认**字符串**，且不去碰 request_id（那是排查用的请求号，不能当任务号轮询）。顶层 `id`
    **只对智谱与方舟**当任务号（R-40 / 坑 202③）：MiniMax/华为回包里的顶层 id 是请求号，
    写进台账会造出永远取不回的假任务。"""
    try:
        if not isinstance(payload, dict):
            return ""
        if protocol == PROTOCOL_ALIYUN:
            out = payload.get("output") or {}
            return str(out.get("task_id") or "")
        keys = ("task_id", "id") if protocol in _TOP_ID_PROTOCOLS else ("task_id",)
        for k in keys:
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        out = payload.get("output") or {}
        return str(out.get("task_id") or "") if isinstance(out, dict) else ""
    except Exception:
        return ""


def _proto_error(payload, protocol):
    """**HTTP 200 也可能带错误**：MiniMax 把结果包在 `base_resp` 里，status_code≠0 就是失败；
    方舟的生图回包另有顶层 `error`（`{code, message}`，成功时是 null）。不掀这一层，用户
    只会看到"回包里没有图片地址"这种猜不出所以然的说法。"""
    if not isinstance(payload, dict):
        return ""
    if protocol == PROTOCOL_VOLC:
        err = payload.get("error")
        if isinstance(err, dict) and (err.get("code") or err.get("message")):
            return texts.CME_CAT_5 % (err.get("code") or "-",
                                          err.get("message") or texts.CME_N_2)
        if isinstance(err, str) and err.strip():
            return "服务端错误：%s" % err.strip()
        return ""
    if protocol != PROTOCOL_MINIMAX:
        return ""
    br = payload.get("base_resp")
    if not isinstance(br, dict):
        return ""
    try:
        code = int(br.get("status_code") or 0)
    except Exception:
        return ""
    if code == 0:
        return ""
    return texts.CME_CAT_5 % (code, br.get("status_msg") or texts.CME_N_2)


def _extract_b64(payload):
    """华为 MaaS 的生图接口**只给 base64**（`data[0].b64_json`，可能是带
    `data:image/jpg;base64,` 前缀的 data URL）：没有 URL 可下，就直接把字节落盘。"""
    out = []

    def walk(o, d=0):
        if d > 8:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() in _B64_KEYS and isinstance(v, str) and len(v) > 64:
                    out.append(v)
                else:
                    walk(v, d + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, d + 1)
    walk(payload)
    return out


def write_b64(items, dest, log=None):
    """base64 产物 → 本地文件；返回 (paths, errors)。"""
    paths, errs = [], []
    base, ext = os.path.splitext(dest)
    ext = ext or ".png"
    for i, s in enumerate(items or []):
        head, _, tail = str(s).partition(",")
        try:
            data = base64.b64decode(tail or str(s))
        except Exception as e:
            errs.append(texts.CME_CAT_6 % e)
            continue
        real = ext
        for cand in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            if cand.lstrip(".") in head.lower():
                real = cand if cand != ".jpeg" else ".jpg"
                break
        p = "%s%s%s" % (base, "" if i == 0 else "-%d" % (i + 1), real)
        try:
            if os.path.dirname(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)
            if log is not None:
                log.w(texts.CME_CAT_7 % (p, len(data)))
        except Exception as e:
            errs.append(texts.CHL_CAT % e)
    return paths, errs


# ---------------------------------------------------------------- 产物下载

def download(url, dest, emit=None, timeout=None, log=None, stop_flag=None):
    """把 24 小时过期的产物 URL 立刻落到本地；写 .part 再原子改名。

    没有 Authorization（signed=True）：签名参数在 query 里。
    """
    t0 = time.time()
    timeout = int(timeout or 180)
    part = dest + ".part"
    # 目录可能在任务进行中途被人删掉/换掉（改过 设置 → 云端模型 → 生图/生视频 的存放目录就是这样）：
    # 下载前补一次，否则用户只会看到一个 [Errno 2]，跟"云端失败了"完全无关（开发机实测踩过）
    try:
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT},
                                     method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as f:
                while True:
                    if stop_flag is not None and stop_flag.is_set():
                        break
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if emit and (done < 65536 or done % 262144 < 65536):
                        emit(("progress", texts.CME_CAT_9
                              % (done / 1048576.0,
                                 (" / %.1f MB" % (total / 1048576.0)) if total else "",
                                 int(time.time() - t0))))
        if stop_flag is not None and stop_flag.is_set():
            _discard(part)
            return False, texts.CME_L1_15
        if done < 1024:
            _discard(part)
            return False, texts.CME_CAT_10 % done
        # 落盘前嗅一眼魔数/内容类型（R-55）：≥1KB 的 HTML/XML/JSON 错误页（签名过期、
        # 额度不足都会回这些）会被当图片/视频存下来并 mark_done，用户拿到一个打不开的"产物"
        try:
            ct = str(r.headers.get("Content-Type") or "").lower()
        except Exception:
            ct = ""
        try:
            with open(part, "rb") as _f:
                head = _f.read(512)
        except Exception:
            head = b""
        low_head = head.lstrip()[:64].lower()
        # 魔数是硬证据，Content-Type 是软提示（坑 276）：网关 / CDN 给真图错配
        # text/* 的事见过，单看类型会把真产物误杀。头部明明白白是网页 / 错误页
        # 才拒；只有在魔数也不认识图片/视频时，Content-Type 的 text/xml 指控才算数。
        looks_like_page = low_head.startswith(
            (b"<!doctype", b"<html", b"<?xml", b"<error", b"{"))
        looks_like_media = (
            head.startswith(b"\x89PNG\r\n\x1a\n")
            or head[:3] == b"\xff\xd8\xff"
            or (head[:6] in (b"GIF87a", b"GIF89a"))
            or head[:4] == b"RIFF" and head[8:12] == b"WEBP"
            or head[4:8] == b"ftyp"           # mp4/mov 系列 ISO-BMFF
        )
        if looks_like_page or (
                (ct.startswith("text/") or ct.startswith("application/xml"))
                and not looks_like_media):
            _discard(part)
            return False, (texts.CME_CAT_11
                           % (ct or low_head[:24].decode("latin-1", "replace")))
        os.replace(part, dest)
        if log is not None:
            log.w(texts.CME_CAT_12
                  % (url[:160], dest, done / 1048576.0, time.time() - t0))
        return True, ""
    except Exception as e:
        _discard(part)
        return False, texts.CME_CAT_13 % e


def _discard(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def _url_ext(url):
    """从产物 URL 取真实扩展名：文件名与实际容器一致，系统播放器才认（服务端给 MP4
    就别存成 .webm）。取不到返回空串，由调用方沿用配置里的扩展名。"""
    try:
        path = urllib.parse.urlparse(str(url or "")).path
    except Exception:
        return ""
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in _IMG_EXT + _VID_EXT else ""


def _dest_paths(dest, urls):
    """多张产物时给第 2 张起加 -2/-3 后缀，别互相覆盖。"""
    base, ext = os.path.splitext(dest)
    out = []
    for i, u in enumerate(urls):
        suffix = "" if i == 0 else "-%d" % (i + 1)
        out.append("%s%s%s" % (base, suffix, _url_ext(u) or ext))
    return out


# ---------------------------------------------------------------- 生图（二期）

def _size_for(protocol, size):
    """尺寸写法各家不同，界面统一填 "宽x高"（或留空），这里翻成各家要的形态：
      阿里云 1024*1024 · MiniMax 收 aspect_ratio（16:9）或 width/height 两个整数 ·
      智谱/华为 1024x1024 · 方舟 1024x1024 或官方档位（1K / 1.5K / 2K）。
    返回要塞进 body 的 (键, 值) 列表，可能是 0 项或 2 项。
    分隔符写法与本地链路同一份判据（media.parse_size，坑 183：* ，× 全角都认，
    先归一成 x 再按家换算），比例写法（16:9，含全角冒号）原样交各家判定。"""
    raw = str(size or "").strip()
    if not raw:
        return []
    parsed = media.parse_size(raw)
    if parsed:
        s = "%dx%d" % parsed
    else:
        s = raw.lower().replace("：", ":")
    if protocol == PROTOCOL_ALIYUN:
        return [("size", s.replace("x", "*"))]
    if protocol in (PROTOCOL_ZHIPU, PROTOCOL_HUAWEI):
        return [("size", s)]
    if protocol == PROTOCOL_VOLC:
        # 认得出宽高就送归一后的 宽x高；认不出就送**原样**（官方档位是 "2K" 这种大写 K，
        # 上面那行 lower() 是给比例写法准备的，套到档位上就变成服务端不认的 "2k"）
        return [("size", s if parsed else raw)]
    if protocol == PROTOCOL_MINIMAX:
        if ":" in s:                       # 界面里直接写比例
            return [("aspect_ratio", s)]
        parts = s.split("x")
        if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
            # 写死宽高只有 image-01 支持（且必须 8 的倍数）；比例不对服务端会点名说
            return [("width", int(parts[0])), ("height", int(parts[1]))]
        return [("aspect_ratio", s)]
    return [("size", s)]


# ---------------------------------------------------------------- 参考图（图生图）
# 两家官方 API 页都写明参考图可以是 **URL 或 base64 data URL**，所以本地图不用先上图床：
#   阿里云  POST …/multimodal-generation/generation
#           input.messages[0].content = [{"image": …}, … , {"text": 提示词}]（一次最多 3 张，
#           JPG/JPEG/PNG/BMP/TIFF/WEBP/GIF，单张 ≤10MB；qwen-image-edit 那一支不吃 size/prompt_extend）
#   MiniMax POST /v1/image_generation
#           subject_reference = [{"type": "character", "image_file": …}]（官方只写了 character
#           这一种，图要 JPG/PNG、<10MB）
# 智谱的 /images/generations 请求体里没有参考图入口（官方页只有 prompt/model/size 一类字段），
# 华为那页没查过 —— 两家都不放行，判据在 providers.REF_IMAGE_APIS，别在这儿再列一遍。
# 方舟官方页**写了**参考图（`image`：URL 或 data:image/<fmt>;base64,，按模型 10/14 张、
# 单张 ≤30MB，格式含 heic/heif/gif），但"单图生图/多图生图"到底是底图重绘还是主体参考
# 没实测过 → 这里**故意不登记**（坑 101：接口页写了支持参考图 ≠ 它拿这张图干同一件事；
# 登记错了就是一次白花的计费 + 一句说错的回显）。
REF_MAX_BYTES = 10 * 1024 * 1024
REF_LIMITS = {
    PROTOCOL_ALIYUN: {"max": 3,
                      "exts": {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                               ".png": "image/png", ".bmp": "image/bmp",
                               ".tif": "image/tiff", ".tiff": "image/tiff",
                               ".webp": "image/webp", ".gif": "image/gif"}},
    PROTOCOL_MINIMAX: {"max": 1,
                       "exts": {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                ".png": "image/png"}},
}


def ref_limits(protocol):
    return REF_LIMITS.get(protocol)


# 官方页**写了**参考图入参、但我们还没实测过它拿这张图干的是哪一档语义的家（坑 101）。
# 拦下的动作和 REF_LIMITS 缺席一样，但说法不能一样 —— 对这几家说"只收文字提示词"是假的。
# 判据只有一份，在 providers.REF_UNVERIFIED_APIS。
REF_UNVERIFIED = providers.REF_UNVERIFIED_APIS


def ref_data_url(path, protocol):
    """本地图片 → data URL。返回 `(data_url, 错误说明)`，错误一律带下一步动作。

    格式与体积都在**发出去之前**判：这两家对坏图的回包各不相同（有的给 400，有的给
    200 + 空结果），拿真金白银去试服务端脸色不划算（同坑 53 的教训：提交返回 200
    不等于没扣钱）。
    """
    lim = ref_limits(protocol)
    if not lim:
        return "", texts.CME_L1_16
    p = str(path or "")
    if not os.path.isfile(p):
        return "", texts.CME_CAT_14 % (p or texts.CME_N_3)
    ext = os.path.splitext(p)[1].lower()
    mime = lim["exts"].get(ext)
    if not mime:
        ok_fmt = " / ".join(sorted({e.lstrip(".").upper() for e in lim["exts"]}))
        return "", (texts.CME_CAT_15
                    % (ok_fmt, ext or texts.CME_N_4))
    try:
        size = os.path.getsize(p)
    except Exception:
        size = 0
    if size > REF_MAX_BYTES:
        return "", texts.CME_CAT_16 % (size / 1048576.0)
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except Exception as e:
        return "", texts.CME_CAT_17 % e
    if not raw:
        return "", texts.CME_L1_17
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii")), ""


def ref_images_error(provider, paths, mode=""):
    """发送前的预检（模式 / 数量 / 格式 / 体积）。返回人话说明，空串代表可以发。

    **顺序有意义**：模式校验排在最前 —— 用户选的那一档这家不支持时就地拦下，
    一次请求都不发（放行错了就是一次白花的计费，坑 102）。
    """
    paths = [p for p in (paths or []) if str(p or "").strip()]
    if not paths:
        return ""
    proto = protocol_of(provider)
    lim = ref_limits(proto)
    if not lim:
        if proto in REF_UNVERIFIED:
            return ("这一家的参考图官方页写了，但我们还没实测它拿这张图是改图还是另画一张，"
                    "所以先不发（图片已保留，不会丢）；要改图请先用本地的〔生图〕模型")
        return ("这一家的生图接口只收文字提示词，参考图请改用本地的〔生图〕模型"
                "（图片已保留，不会丢）")
    want = str(mode or "").strip()
    if want and not providers.supports_ref_mode(provider, want):
        return (texts.CME_CAT_18
                % (capability.MODE_LABEL.get(want, want),
                   capability.MODE_LABEL.get(providers.ref_image_mode(provider), texts.CME_A1)))
    if len(paths) > lim["max"]:
        return (texts.CME_CAT_19
                % (lim["max"], len(paths)))
    for p in paths:
        _url, err = ref_data_url(p, proto)
        if err:
            return err
    return ""


# ---------------------------------------------------------------- 视频首帧（C2-4）
# 只放行官方页**明说收 base64 data URL** 的万相 i2v；其余家由 providers.first_frame_mode
# 分三档（没有 / 未实测 / base64）。格式与体积官方页没列全，按参考图同一套 10MB 保守判，
# 真机首验后再校准。请求体上限也要在真机那一轮量（plan C2-4）。
FIRST_FRAME_MAX_BYTES = 10 * 1024 * 1024
_FRAME_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}


def first_frame_data_url(path):
    """本地首帧 → data URL，返回 `(data_url, 错误说明)`；错误一律带下一步动作。"""
    p = str(path or "")
    if not os.path.isfile(p):
        return "", texts.CME_FRAME_MISSING % (p or texts.CME_N_3)
    ext = os.path.splitext(p)[1].lower()
    mime = _FRAME_EXTS.get(ext)
    if not mime:
        return "", texts.CME_FRAME_EXT % (ext or texts.CME_N_4)
    try:
        size = os.path.getsize(p)
    except Exception:
        size = 0
    if size > FIRST_FRAME_MAX_BYTES:
        return "", texts.CME_FRAME_BIG % (size / 1048576.0)
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except Exception as e:
        return "", texts.CME_FRAME_READ % e
    if not raw:
        return "", texts.CME_FRAME_EMPTY
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii")), ""


def first_frame_error(provider, model, path):
    """发送前预检：这家能不能带首帧、这张图能不能编码。空串 = 可以发。"""
    p = str(path or "").strip()
    if not p:
        return ""
    mode = providers.first_frame_mode(provider, model)
    if mode == "unverified":
        return texts.CME_FRAME_UNVERIFIED
    if mode != "base64":
        return texts.CME_FRAME_NONE
    _u, err = first_frame_data_url(p)
    return err


def _shrink_for_log(body):
    """日志里把 base64 那种超长字符串折成一行摘要。

    逐任务 `.log` 存在的意义是"一眼看清发了什么"（H3 那次结案就靠它），把 8MB 的
    base64 整段写进去就把日志本身变成了查不动的砖头。
    """
    if not isinstance(body, dict):
        return body
    out = {}
    for k, v in body.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = "<%d 字符，已折行：%s…>" % (len(v), v[:60])
        elif isinstance(v, dict):
            out[k] = _shrink_for_log(v)
        elif isinstance(v, list):
            out[k] = [_shrink_for_log(i) if isinstance(i, (dict, list))
                      else (i if not (isinstance(i, str) and len(i) > 200)
                            else "<%d 字符，已折行：%s…>" % (len(i), i[:60])) for i in v]
        else:
            out[k] = v
    return out


def image_body(model, prompt, size="", negative="", seed=-1, extra=None,
               protocol=PROTOCOL_ALIYUN, ref_images=None, ref_mode="", n=1):
    r"""拼一次云端生图的请求体。`ref_images` 是**已经编成 data URL 的参考图**列表。

    `n` = 一次要出几张；只有能力档案 `img_n` 上限 > 1 的家（当前是 MiniMax 1~9）
    才把 `n` 写进请求体，其余家**不发明字段**（阿里 plus/max 固定 1，传别的会报错）。

    编码放在 `generate_image` 里做（那里能出"这张图不合格"的人话），这里只负责各家**字段
    形状不同**这一件事：阿里云把图塞进 `content[]`，MiniMax 塞进 `subject_reference[]`。

    两档在协议层**同形**（各家官方页各只写了一个带图入口），真正把两种语义分开的是**提示词** ——
    2026-10-06 真机测出来的（`D:\tmp\ref_verify\`，同一张四格色块图、同一接口）：
      · `edit`    + "把色块改成夜晚蓝紫、位置不变" → **四个色块位置大小全保留**，配色按要求改
      · `subject` + "参考配色画一只橘猫" → **全新构图**的猫，只借了配色（地毯条纹、墙上四格画）
    同一接口、同一个入参，差别全在提示词上。所以 `ref_mode` 在这里的作用是**给提示词加一句语义
    约束** —— 是**我们侧的提示词构造**，不是接口原生开关，界面别写成"官方开关"。
    MiniMax 只有 subject 一档（实测改不了图），对它加这句只是加固，不改变行为。
    """
    refs = [u for u in list(ref_images or []) if str(u).strip()]
    if refs:
        _hint = {"edit": texts.CME_DEDIT, "subject": texts.CME_DSUBJECT}.get(str(ref_mode or "").strip())
        if _hint:
            prompt = ("%s%s" % (_hint, prompt)) if str(prompt).strip() else _hint
    if protocol == PROTOCOL_MINIMAX:
        body = {"model": model, "prompt": prompt, "response_format": "url"}
        try:
            _n = int(n or 1)
        except Exception:
            _n = 1
        _lo, _hi = providers.profile_for(protocol, model).get("img_n") or (1, 1)
        _n = max(int(_lo), min(int(_hi), _n))
        if _n > 1:
            body["n"] = _n          # 官方 image-01 支持 1~9；1 张时不传（默认就是 1）
        for k, v in _size_for(protocol, size):
            body[k] = v
        try:
            if int(seed) >= 0:
                body["seed"] = int(seed)
        except Exception:
            pass
        # 官方只写了 model/prompt/aspect_ratio 或 width+height/n/response_format/seed；
        # negative_prompt 这一家没有 → 不发明字段，交给 extra 显式带
        for u in refs[:REF_LIMITS[PROTOCOL_MINIMAX]["max"]]:
            # 图生图的入参就这一个字段；type 官方只列了 character 一种，不猜别的值
            body.setdefault("subject_reference", []).append(
                {"type": "character", "image_file": u})
        body.update(extra or {})
        return body
    if protocol == PROTOCOL_ZHIPU:
        body = {"model": model, "prompt": prompt}
        for k, v in _size_for(protocol, size):
            body[k] = v
        body.update(extra or {})
        return body
    if protocol == PROTOCOL_HUAWEI:
        body = {"model": model, "prompt": prompt, "response_format": "b64_json"}
        for k, v in _size_for(protocol, size):
            body[k] = v
        try:
            if int(seed) >= 0:
                body["seed"] = int(seed)
        except Exception:
            pass
        body.update(extra or {})
        return body
    if protocol == PROTOCOL_VOLC:
        body = {"model": model, "prompt": prompt, "response_format": "url"}
        for k, v in _size_for(protocol, size):
            body[k] = v
        try:
            if int(seed) >= 0:
                body["seed"] = int(seed)
        except Exception:
            pass
        # 官方默认 true = 右下角印一枚"AI 生成"水印；产物是用户自己存下来的图片，默认关掉
        body["watermark"] = False
        # 参考图官方页有字段（`image`）但语义没实测 → 这家不进 REF_IMAGE_APIS/REF_LIMITS，
        # 上面 generate_image 的预检就把它拦下了，走到这里时 refs 必为空（坑 101/102）
        # negative_prompt 这一家没有 → 不发明字段，交给 extra 显式带（同 MiniMax 那条）
        body.update(extra or {})
        return body
    content = [{"image": u} for u in refs[:REF_LIMITS[PROTOCOL_ALIYUN]["max"]]]
    content.append({"text": prompt})
    body = {"model": model, "input": {"messages": [{"role": "user", "content": content}]}}
    p = {}
    for k, v in _size_for(protocol, size):
        p[k] = v
    neg = str(negative or "").strip()
    if neg:
        p["negative_prompt"] = neg
    try:
        if int(seed) >= 0:
            p["seed"] = int(seed)
    except Exception:
        pass
    p.update(extra or {})
    if p:
        body["parameters"] = p
    return body



def generate_image(cfg, provider, model, prompt, dest, emit=None, stop_flag=None,
                   negative="", size="", seed=-1, log=None, log_path="", ref_images=None,
                   on_task_id=None, persist=None, ref_mode="", n=1):
    """云端生图：同步端点出 URL；万一服务端给了 task_id，就地转成轮询。

    `ref_images` 是本地参考图路径列表，`ref_mode` 是要用哪一档（空 = 这一家的默认档）。
    发送前先 `ref_images_error` 预检（模式 / 张数 / 格式 / 体积），不合格就地报错，
    **一次请求都不发**（预检在 UI 侧也做一遍，这里是最后一道）。

    `on_task_id`：拿到 task_id 的那一刻回调一次（**在轮询之前**）—— 调用方靠它把
    task_id 立刻落进台账，断电 / 强杀之后还能「取回」（产物地址只活 24 小时）。
    `persist`：透传给 `wait_task`，让轮询过程中的状态也同步进台账。

    返回 dict(ok, paths, urls, error, seconds, log_path, raw)。产物 URL 只活 24 小时，
    所以成功判定 = **文件已经在本地**，而不是"服务端回了 200"。
    """
    emit = emit or (lambda e: None)
    t0 = time.time()
    own = log is None
    if own:
        # 日志落到 media_logs（与产物分开，W 2026-10-11 定）；_Log 的标题沿用原样
        log = _Log(log_path or config.media_log_path(cfg, dest, "cloud"),
                   texts.CME_CAT_20 % model)
    timeout = int(cfg.get("cloud_image_wait_seconds", 180) or 180)
    proto = protocol_of(provider)
    bad = ref_images_error(provider, ref_images, mode=ref_mode)
    if bad:
        return _img_fail(bad, t0, log)
    urls_in = []
    for p in [x for x in (ref_images or []) if str(x or "").strip()]:
        data_url, err = ref_data_url(p, proto)
        if err:
            return _img_fail(err, t0, log)
        urls_in.append(data_url)
    _pf = providers.profile_for(provider, model)
    try:
        _n = int(n or 1)
    except Exception:
        _n = 1
    _lo, _hi = _pf.get("img_n") or (1, 1)
    _n = max(int(_lo), min(int(_hi), _n))
    body = image_body(model, prompt, size=size, negative=negative, seed=seed,
                      extra=providers.media_extra_params(provider, model),
                      protocol=proto, ref_images=urls_in, ref_mode=ref_mode, n=_n)
    log.json("请求 POST %s" % image_endpoint(provider), _shrink_for_log(body))
    emit(("line", texts.CME_CAT_21 % (providers.short_of(model),
                                      texts.CME_CAT_22 % (capability.MODE_LABEL.get(ref_mode,
                                                                             texts.CME_A1_2),
                                                        len(urls_in))
                                      if urls_in else "")))
    emit(("progress", texts.CME_CAT_23 % int(time.time() - t0)))

    st, text = _request(provider, "POST", image_endpoint(provider), body, timeout=timeout)
    log.w("回包 HTTP %s" % st)
    log.json("回包原文", _safe_json(text))
    if st != 200:
        err = explain(st, text, provider, image_endpoint(provider))
        if st == -1:
            # 超时/断连不等于"没发出去"：这一单可能已在服务端创建并计费，
            # 必须明说，否则用户重发就是重复扣钱（排查报告 R-04）
            err += "\n  " + _BILL_NOTE
        return _img_fail(err, t0, log)
    payload = _safe_json(text)
    if not isinstance(payload, dict):
        return _img_fail(texts.CME_A0, t0, log)
    err = _proto_error(payload, proto)
    if err:
        # MiniMax 这类"200 里藏着错误码"的家，直接按错误收
        return _img_fail(err, t0, log)

    cont = (payload.get("output") or payload) if proto == PROTOCOL_ALIYUN else payload
    urls = _extract_urls(cont, "image")
    tid = _task_id_of(payload, proto)
    if not urls and tid:
        # 少数模型即使请求同步也回 task_id：转轮询，别当成失败
        emit(("progress", texts.CME_CAT_24 % tid))
        if on_task_id is not None:
            # 先把 task_id 交出去（调用方落台账）再开始轮询：这一步晚做，中间断电
            # 就等于这条结果再也取不回来（坑 136）
            try:
                on_task_id(tid)
            except Exception:
                pass
        res = wait_task(cfg, provider, tid, dest, kind="image", emit=emit,
                        stop_flag=stop_flag, log=log, persist=persist, model=model)
        res["seconds"] = round(time.time() - t0, 1)
        if own:
            log.close()
        return res

    if not urls:
        b64 = _extract_b64(payload)
        if b64:
            # 华为 MaaS 的生图只给 b64_json：没有 URL 可下，就地写字节，
            # 成功判定仍然是"文件已经在本地"
            paths, errs = write_b64(b64, dest, log=log)
            out = {"ok": bool(paths), "paths": paths, "urls": [],
                   "error": "" if paths else (errs[0] if errs else texts.CME_ELSE),
                   "seconds": round(time.time() - t0, 1), "log_path": log.path,
                   "usage": payload.get("usage") or {}, "task_id": "", "raw": payload}
            if own:
                log.w(texts.CME_CAT_25 % (len(paths), out["error"] or texts.CME_N_5))
                log.close()
            return out
        return _img_fail(texts.CME_A0_2, t0, log)

    paths = _dest_paths(dest, urls)
    got, errs = [], []
    for url, p in zip(urls, paths):
        if stop_flag is not None and stop_flag.is_set():
            break
        emit(("progress", texts.CME_CAT_26
              % (len(got) + 1, len(urls), int(time.time() - t0))))
        ok, why = download(url, p, emit=emit, log=log, stop_flag=stop_flag)
        if ok:
            got.append(p)
        else:
            errs.append(why)
    out = {"ok": bool(got), "paths": got, "urls": urls,
           "error": "" if got else (errs[0] if errs else texts.CME_ELSE_2),
           "seconds": round(time.time() - t0, 1), "log_path": log.path,
           "usage": payload.get("usage") or {}, "task_id": tid, "raw": payload}
    if own:
        log.w(texts.CME_CAT_25 % (len(got), out["error"] or texts.CME_N_5))
        log.close()
    return out


def _img_fail(msg, t0, log):
    if log is not None:
        log.w(texts.CME_CAT_27 % msg)
    return {"ok": False, "paths": [], "urls": [], "error": msg,
            "seconds": round(time.time() - t0, 1),
            "log_path": log.path if log else "", "usage": {}, "task_id": "", "raw": None}


# ---------------------------------------------------------------- 生视频（三期）

def video_body(model, prompt, resolution="", duration=0, ratio="", seed=-1,
               negative="", extra=None, protocol=PROTOCOL_ALIYUN, first_frame=""):
    """各家的档位字段名不一样，这里只送**这一家有的**：
    阿里云 parameters.{resolution,ratio,duration}、MiniMax v1 只有 resolution/duration、
    MiniMax v2 是 content[] + resolution/duration/ratio、智谱用 size、华为嵌套 parameters.size、
    方舟是 content[] + 顶层 resolution/ratio/duration。
    界面填的"分辨率"可能是 720P 也可能是 1280x720 —— 认不出来的形式就**不送**，
    让服务端用它自己的默认档，别把猜的值发上去（发了也只换来一个看不懂的 400）。
    宽高写法与本地链路同一份判据（media.parse_size，坑 183）：智谱 / 华为送 size 前
    先把 * ，× 全角等分隔符归一成 x；档位标签（720P）各家原样送。"""
    res = str(resolution or "").strip()
    wh = media.parse_size(res)
    sized = wh is not None or "x" in res.lower()
    # 认得出宽高的（含 * ，× 全角写法）一律送归一后的 1280x720；档位标签（720P）原样送。
    res_out = ("%dx%d" % wh) if wh else res
    try:
        dur = int(duration or 0)
    except Exception:
        dur = 0
    if protocol == PROTOCOL_MINIMAX and _mm_v2(model):
        body = {"model": model, "content": [{"type": "text", "text": prompt}]}
        if res:
            body["resolution"] = res_out
        if dur:
            body["duration"] = dur
        if ratio:
            body["ratio"] = ratio
        body.update(extra or {})
        return body
    if protocol == PROTOCOL_MINIMAX:
        body = {"model": model, "prompt": prompt}
        if res:
            body["resolution"] = res_out
        if dur:
            body["duration"] = dur
        body.update(extra or {})
        return body
    if protocol == PROTOCOL_ZHIPU:
        body = {"model": model, "prompt": prompt}
        if sized:
            body["size"] = ("%dx%d" % wh) if wh else res.lower()
        if dur:
            body["duration"] = dur
        body.update(extra or {})
        return body
    if protocol == PROTOCOL_HUAWEI:
        p = {}
        if sized:
            p["size"] = ("%dx%d" % wh) if wh else res.lower()
        if dur:
            p["duration"] = dur
        try:
            if int(seed) >= 0:
                p["seed"] = int(seed)
        except Exception:
            pass
        p.update(extra or {})
        body = {"model": model, "input": {"prompt": prompt}}
        if p:
            body["parameters"] = p
        return body
    if protocol == PROTOCOL_VOLC:
        # 请求体形状与 MiniMax v2 同族（content[] + 顶层档位），**取值是另一套**：
        # resolution 官方只收小写 480p/720p/1080p/4k，宽高比走 ratio，duration 按模型 [2,30]；
        # 界面里写成宽x高的那一档这家没有对应字段 → 不送，交服务端的默认档
        body = {"model": model, "content": [{"type": "text", "text": prompt}]}
        if res and not sized:
            body["resolution"] = res_out.lower()
        if ratio:
            body["ratio"] = ratio
        if dur:
            body["duration"] = dur
        try:
            if int(seed) >= 0:
                body["seed"] = int(seed)
        except Exception:
            pass
        body.update(extra or {})
        return body
    inp = {"prompt": prompt}
    if first_frame:
        # 万相 i2v：input.media[] 里放 {"type":"first_frame","url": data URL}
        # （11 §5③；只在这条阿里分支里出现，其余家由 first_frame_error 拦住）
        inp["media"] = [{"type": "first_frame", "url": first_frame}]
    body = {"model": model, "input": inp}
    neg = str(negative or "").strip()
    if neg:
        body["input"]["negative_prompt"] = neg
    p = {}
    if res:
        p["resolution"] = res_out
    if ratio:
        p["ratio"] = ratio
    if dur:
        p["duration"] = dur
    try:
        if int(seed) >= 0:
            p["seed"] = int(seed)
    except Exception:
        pass
    p.update(extra or {})
    if p:
        body["parameters"] = p
    return body



def submit_video(cfg, provider, model, prompt, resolution="", duration=0, ratio="",
                 negative="", seed=-1, log_path="", title="", first_frame=""):
    """创建异步视频任务。缺 X-DashScope-Async 头会被服务端直接拒（文档 §12.1）。

    返回 dict(ok, task_id, error, raw, log_path)。日志落在 log_path（一般是产物同名
    .log）：这样"建单成功但轮询断了"和"根本没建单"是两回事，翻日志一眼能分开——
    而生视频链路那次"引擎说完成却没落盘"就是靠同名日志结的案。
    """
    log = _Log(log_path, title or (texts.CME_CAT_28 % model))
    proto = protocol_of(provider)
    body = video_body(model, prompt, resolution=resolution, duration=duration,
                      ratio=ratio, negative=negative, seed=seed,
                      extra=providers.media_extra_params(provider, model),
                      protocol=proto, first_frame=first_frame)
    url = video_endpoint(provider, model)
    # 首帧是 data URL 时 body 里会有几十 KB~几 MB 的 base64 —— 与生图日志同一条纪律：
    # 超长字符串折成"长度 + 头一段 + 已折行"，否则逐任务日志变成查不动的砖头（坑 102）。
    log.json("请求 POST %s" % url, _shrink_for_log(body))
    # X-DashScope-Async 是阿里云要求的异步开关；各家不认的请求头不该乱发（有的网关
    # 会把未知头当可疑请求拦下来）
    extra = {"X-DashScope-Async": "enable"} if proto == PROTOCOL_ALIYUN else None
    st, text = _request(provider, "POST", url, body, extra=extra,
                        timeout=int(cfg.get("cloud_submit_timeout", 90) or 90))
    log.w("回包 HTTP %s" % st)
    log.json("回包原文", _safe_json(text))
    payload = _safe_json(text)
    err = _proto_error(payload, proto) if st == 200 else ""
    tid = _task_id_of(payload, proto) if (st == 200 and isinstance(payload, dict)) else ""
    if err:
        tid = ""
    if tid:
        err = ""
        log.w(texts.CME_CAT_29 % tid)
    elif err:
        log.w(texts.CME_CAT_30 % err)
    elif st == 200:
        # 200 但没有 task_id：不能报"请求失败（200）"，那是另一种失败——结构不认识
        err = texts.CME_ERR
        log.w(err)
    else:
        err = explain(st, text, provider, url)
        if st == -1:
            # 同 generate_image：提交超时不等于没建单，必须把"可能已计费"说出来（R-04）
            err += "\n  " + _BILL_NOTE
        log.w(texts.CME_CAT_30 % err)
    out = {"ok": bool(tid), "task_id": tid, "raw": payload,
           "error": err, "log_path": log.path}
    log.close()
    return out


def query_task(provider, task_id, model=""):
    """查一次任务 → dict(status, urls, error, raw, usage, submit_time, end_time)。

    坑 53 的教训：**"提交返回 200"不等于"没扣钱"**，取回旧任务时也要按同一套解析。

    MiniMax v1 是**两跳**：轮询只给 `file_id`，要再查一次 `/v1/files/retrieve` 才拿到
    真正的下载地址（而且那个地址只活 1 小时）。第二跳在这一层里做掉，交给上层的
    永远是"能直接下载的地址"，UI 不需要知道有几家要跳两回。
    """
    proto = protocol_of(provider)
    url = task_endpoint(provider, task_id, model)
    if not url:
        return {"status": "unknown", "urls": [], "image_urls": [],
                "error": texts.CME_DERROR,
                "code": "", "raw": None, "usage": {}, "task_id": task_id,
                "submit_time": "", "end_time": ""}
    st, text = _request(provider, "GET", url, timeout=45)
    payload = _safe_json(text)
    if st != 200:
        return {"status": "unknown", "urls": [], "image_urls": [],
                "error": explain(st, text, provider, url), "code": "",
                "raw": payload, "usage": {}, "task_id": task_id,
                "submit_time": "", "end_time": ""}
    if not isinstance(payload, dict):
        # 200 但回包不是 JSON 对象（网关的 HTML 错误页、空体、裸数组）：
        # 后面全程按字典取值，放过去就是 AttributeError 穿出轮询循环 →
        # worker 线程静默死掉、不投 exit 事件、界面永久卡在"生成中"。
        # 按"结构不认识"如实上报，由 wait_task 计入失败次数并重试。
        return {"status": "unknown", "urls": [], "image_urls": [],
                "error": texts.CME_CAT_31 % str(text or "")[:120],
                "code": "", "raw": None, "usage": {}, "task_id": task_id,
                "submit_time": "", "end_time": ""}
    err = _proto_error(payload, proto)
    if proto == PROTOCOL_ALIYUN:
        cont = payload.get("output") or {}
    elif proto == PROTOCOL_MINIMAX and _mm_v2(model) and isinstance(payload.get("task"), dict):
        cont = payload["task"]                 # v2 把状态与产物都放在 task 里
    else:
        cont = payload
    if not isinstance(cont, dict):
        cont = {}                            # 回包顶层是数组/字符串时别在取值上炸掉
    status = normalize_status(cont.get("task_status") or cont.get("status"))
    urls = _extract_urls(cont, "video")
    iurls = _extract_urls(cont, "image")
    if status == "failed":
        err = err or _says(json.dumps(cont, ensure_ascii=False))
    if status == "unknown" and not err:
        # 认不出的状态字面量（MiniMax v1 的 "Fail"、阿里云对已清理 task_id 回的
        # UNKNOWN 一类）绝不能含糊带过：如实报"状态不认识"，由 wait_task 计入查询
        # 失败并重试 —— 原来它会落到"其余即成功"分支，任务还在跑就被判死，真正的
        # 服务端 code/message 被吞（排查报告 R-03）。
        err = ("服务端返回了不认识的状态（%s）：不能判定成功或失败，"
               "已按查询失败处理（原文已存日志）。"
               % str(cont.get("task_status") or cont.get("status") or texts.CME_N_6))
    if proto == PROTOCOL_MINIMAX and not _mm_v2(model) and status == "succeeded" \
            and not urls and not iurls and payload.get("file_id"):
        st2, t2 = _request(provider, "GET", file_endpoint(provider, payload.get("file_id")),
                           timeout=45)
        p2 = _safe_json(t2)
        if st2 != 200:
            err = explain(st2, t2, provider, file_endpoint(provider, payload.get("file_id")))
            status = "unknown"
        elif not isinstance(p2, dict):
            err = texts.CME_ERR_2
            status = "unknown"
        else:
            err = _proto_error(p2, proto)
            urls = _extract_urls(p2, "video")
            iurls = _extract_urls(p2, "image")
            if err and not urls and not iurls:
                status = "failed"
    return {"status": status,
            "urls": urls,
            "image_urls": iurls,
            "error": err or ("" if status != "failed" else _says(json.dumps(
                cont, ensure_ascii=False))),
            "code": (cont.get("code") or payload.get("code") or "") if isinstance(cont, dict) else "",
            "raw": payload, "usage": (payload or {}).get("usage") or {},
            "task_id": task_id,
            "submit_time": cont.get("submit_time") or "",
            "end_time": cont.get("end_time") or ""}



_LABEL = {"pending": texts.CME_DPENDING, "running": texts.CME_DRUNNING, "succeeded": texts.CME_DSUCCEEDED,
          "failed": texts.CME_DFAILED, "canceled": texts.CME_DCANCELED, "unknown": texts.CME_DUNKNOWN}


def list_tasks(provider, model="", start="", end="", status="", page=1, size=20):
    """按模型名 + 时间窗查"我刚提交的任务" → `(ok, items, error, raw)`。

    干什么用（16 §24.1 / 排查报告 R-04 的后半）：提交请求超时 / 断连时，**"没拿到回包"
    不等于"没建单"**（`_BILL_NOTE` 那句话的另一半）。重发就会重复扣钱，所以给一条
    "只查不提交"的核对出口：查到就把它当台账任务取回，一个字节都不用再提交。

    只有**阿里云系**有这条批量端点（`GET /api/v1/tasks?model_name=&start_time=&end_time=`，
    时间格式 `YYYYMMDDhhmmss`，见 `11` §3.5）。别家要么没给（MiniMax 只能按 id 查）、
    要么没实测（方舟的 list 有、语义没验），一律如实回"这一家没有"。

    items 里每条只留界面要用的字段：`task_id / status / model / submit_time /
    end_time / urls / error`，原始回包整份放 `raw` 供"另存日志"。
    """
    proto = protocol_of(provider)
    if proto != PROTOCOL_ALIYUN:
        return False, [], ("这一家没有可用的「批量查任务」端点（或未实测）："
                           "只能按 task_id 单个查，任务号得从台账或控制台拿。"), None
    q = {"page_no": str(max(1, int(page or 1))),
         "page_size": str(max(1, min(100, int(size or 20))))}
    if model:
        q["model_name"] = str(model)
    if start:
        q["start_time"] = str(start)
    if end:
        q["end_time"] = str(end)
    if status:
        q["status"] = str(status)
    url = task_endpoint(provider) + "?" + urllib.parse.urlencode(q)
    st, text = _request(provider, "GET", url, timeout=45)
    payload = _safe_json(text)
    if st != 200:
        return False, [], explain(st, text, provider, url), payload
    if not isinstance(payload, dict):
        return False, [], (texts.CME_CAT_31 % str(text or "")[:120]), None
    err = _proto_error(payload, proto)
    # 列表可能藏在 output.tasks / tasks / data / output.items 任一处：官方页只写了
    # "批量列表"，没给回包示例（`11` §3.5），几种形状都认，认不出就是空表 + 原话。
    cont = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    rows = (cont.get("tasks") or cont.get("items") or payload.get("tasks")
            or payload.get("data") or [])
    if isinstance(rows, dict):
        rows = rows.get("tasks") or rows.get("items") or []
    items = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("task_id") or r.get("id") or "")
        if not tid:
            continue
        urls = _extract_urls(r, "video") + _extract_urls(r, "image")
        items.append({"task_id": tid,
                      "status": normalize_status(r.get("task_status") or r.get("status")),
                      "model": str(r.get("model") or r.get("model_name") or ""),
                      "submit_time": str(r.get("submit_time") or ""),
                      "end_time": str(r.get("end_time") or ""),
                      "urls": urls,
                      "error": _says(json.dumps(r, ensure_ascii=False)) if
                      normalize_status(r.get("task_status") or r.get("status")) == "failed" else ""})
    return (not err), items, err, payload


def download_urls(urls, dest, emit=None, stop_flag=None, timeout=None):
    """按台账里存的产物 URL 直接重下，**不查服务端**（16 §24.2 / R-40 后半）。

    什么时候用：同步生图是"服务端当场回 URL"，下载那一步失败时，URL 就是唯一还活着
    的线索（24 小时内有效）。原来它只活在 `.log` 里，重启后没有任何入口能拿回来。
    多个 URL（×n）时主图用 `dest`，其余 `dest` 换后缀 `-2`、`-3`…

    返回 dict(ok, paths, error, urls)——与 generate_image 的收尾形状对齐，
    上层两条链路（取回按钮 / worker）可以用同一套事件。
    """
    urls = [str(u) for u in (urls or []) if str(u or "").strip()]
    if not urls:
        return {"ok": False, "paths": [], "error": "台账里没有可重下的产物地址。", "urls": []}
    paths, errs = [], []
    stem, ext = os.path.splitext(dest)
    for i, u in enumerate(urls):
        p = dest if i == 0 else "%s-%d%s" % (stem, i + 1, ext)
        ok, why = download(u, p, emit=emit, timeout=timeout, stop_flag=stop_flag)
        (paths if ok else errs).append(p if ok else why)
    return {"ok": bool(paths), "paths": paths,
            "error": "" if paths else (errs[0] if errs else texts.CME_ELSE_4),
            "urls": urls}


def wait_task(cfg, provider, task_id, dest, kind="video", emit=None, stop_flag=None,
              log=None, log_path="", deadline=None, persist=None, model=""):
    """轮询到终态并下载产物。进度来自轮询，**不是** stdout，但仍推进同一个队列。

    emit 的事件与本地引擎一致：("progress", 文本) 刷进度行、("line", 文本) 进尾部缓冲。
    stop_flag 置位 = "停止等待"：只停我们这一侧，服务端任务继续跑、继续计费，
    所以文案必须如实（文档 §12.3 ⑥：不能写成"已取消"）。
    `model` 只有 MiniMax 用得上（同一家的 v1/v2 两套轮询地址要按模型名选）。
    """
    emit = emit or (lambda e: None)
    own = log is None
    if own:
        log = _Log(log_path or config.media_log_path(cfg, dest, "cloud"),
                   texts.CME_CAT_33 % (texts.CME_IF if kind == "image" else texts.CME_ELSE_3, task_id))
    interval = max(5, int(cfg.get("cloud_poll_seconds", 15) or 15))
    if deadline is None:
        deadline = time.time() + max(60, int(cfg.get("cloud_wait_minutes", 20) or 20) * 60)
    t0 = time.time()
    last = ""
    bad = 0
    res = {"ok": False, "status": "unknown", "paths": [], "urls": [],
           "error": texts.CME_DERROR_2, "seconds": 0.0, "task_id": task_id,
           "log_path": log.path, "raw": None, "usage": {}}
    while True:
        if stop_flag is not None and stop_flag.is_set():
            res["status"] = "waiting"
            res["error"] = texts.CME_ASGN
            res["seconds"] = round(time.time() - t0, 1)
            log.w(res["error"])
            if own:
                log.close()
            return res
        try:
            q = query_task(provider, task_id, model)
        except Exception as e:
            # 兜底：轮询里任何未预料的异常都不能穿出这个循环。一旦穿出，worker 线程
            # 会静默死掉、不投 exit 事件，界面就永久卡在"生成中…"（busy 标志不复位）。
            # 折成一次"查询失败"，交给下面的 bad 计数与重试逻辑。
            q = {"status": "unknown", "urls": [], "image_urls": [],
                 "error": texts.CME_CAT_34 % (type(e).__name__, e),
                 "code": "", "raw": None, "usage": {}, "task_id": task_id,
                 "submit_time": "", "end_time": ""}
        if q["status"] == "unknown":
            # 一切"不认识"（查询异常 / 非 200 / 状态字面量认不出）一律计入查询失败
            # 并重试；**绝不落到下面的成功分支**（排查报告 R-03：原来要求 error 非空
            # 才算失败，服务端回 200+UNKNOWN 时会被当"任务已完成"）。
            q_err = q["error"] or texts.CME_N_7
            try:
                log.json(texts.CME_CAT_35 % (bad + 1), q.get("raw"))
            except Exception:
                pass
            bad += 1
            # 单次查询失败（限流 / 网络抖）不该丢掉已经排队的任务：容忍几轮再说
            if bad <= 3:
                emit(("line", texts.CME_CAT_36 % (bad, q_err)))
                emit(("progress", texts.CME_CAT_37 % int(time.time() - t0)))
                if _sleep(stop_flag, interval):
                    continue
                continue
            res["error"] = texts.CME_CAT_38 % (bad, q_err)
            res["seconds"] = round(time.time() - t0, 1)
            log.w(res["error"])
            if own:
                log.close()
            return res
        bad = 0
        status = q["status"]
        if status != last:
            # 状态没变就不落盘（D3）：persist 每拍都全量重写 cloud_jobs.json 并刷新
            # updated_at，PENDING/RUNNING 一路每拍重写同一个状态纯属写放大。updated_at
            # 只有 TTL 清理会读，而 TTL 只清已完结任务（OPEN_STATUS 永不清理），心跳
            # 断掉不影响任何判据；首次进循环 last="" 必触发一次，终态也一定触发。
            if persist is not None:
                try:
                    persist(status, q.get("raw"))
                except Exception:
                    pass
            last = status
            log.json(texts.CME_CAT_39 % (status, task_id), q.get("raw"))
            emit(("line", texts.CME_CAT_40 % (status, _LABEL.get(status, status))))
        if status in ("pending", "running"):
            emit(("progress", texts.CME_CAT_41
                  % (texts.CME_IF if kind == "image" else texts.CME_ELSE_3, _LABEL.get(status, status),
                     int(time.time() - t0))))
            if time.time() > deadline:
                res["status"] = "timeout"
                res["error"] = ("等满了 %d 分钟还没出结果。任务记录已存进 cloud_jobs.json，"
                                "之后可以在对话里点「取回」接着拿。"
                                % int(cfg.get("cloud_wait_minutes", 20) or 20))
                res["seconds"] = round(time.time() - t0, 1)
                log.w(res["error"])
                if own:
                    log.close()
                return res
            if _sleep(stop_flag, interval):
                continue
            continue
        if status in ("failed", "canceled"):
            res["status"] = status
            res["error"] = q["error"] or (texts.CME_CAT_43 % _LABEL.get(status, status))
            res["seconds"] = round(time.time() - t0, 1)
            res["raw"] = q.get("raw")
            log.w(texts.CME_CAT_27 % res["error"])
            if own:
                log.close()
            return res
        # succeeded：产物 URL 24 小时过期，立刻落地
        if kind == "image":
            urls = q.get("image_urls") or q.get("urls") or []
        else:
            urls = q.get("urls") or q.get("image_urls") or []
        if not urls:
            b64 = _extract_b64(q.get("raw")) if kind == "image" else []
            got, errs = write_b64(b64, dest, log=log) if b64 else ([], [])
            if got:
                # 华为 MaaS 这类只给 base64 的家：异步路径也照样能落地
                res.update({"ok": True, "paths": got, "urls": [], "status": "succeeded",
                            "error": "", "seconds": round(time.time() - t0, 1),
                            "raw": q.get("raw"), "usage": q.get("usage") or {}})
                log.w(texts.CME_CAT_44 % "，".join(got))
                if own:
                    log.close()
                return res
            res["status"] = "succeeded"
            res["error"] = texts.CME_ASGN_2
            res["seconds"] = round(time.time() - t0, 1)
            res["raw"] = q.get("raw")
            log.json(texts.CME_A0_3, q.get("raw"))
            if own:
                log.close()
            return res
        emit(("progress", texts.CME_CAT_45 % int(time.time() - t0)))
        paths = _dest_paths(dest, urls)
        got, errs = [], []
        for url, p in zip(urls, paths):
            ok, why = download(url, p, emit=emit, log=log, stop_flag=stop_flag,
                               timeout=int(cfg.get("cloud_download_seconds", 180) or 180))
            (got if ok else errs).append(p if ok else why)
        res.update({"ok": bool(got), "paths": got, "urls": urls, "status": "succeeded",
                    "error": "" if got else (errs[0] if errs else texts.CME_ELSE_4),
                    "seconds": round(time.time() - t0, 1), "raw": q.get("raw"),
                    "usage": q.get("usage") or {}})
        log.w(texts.CME_CAT_46 % (texts.CME_IF_2 if got else texts.CME_DFAILED,
                                  res["error"] or "，".join(got)))
        if own:
            log.close()
        return res


def _cancel_volc(provider, task_id, model=""):
    """方舟的取消要先查状态再决定发不发。

    官方页写了同一个 `DELETE …/contents/generations/tasks/{id}` **按当前状态干两件不同的事**：
    `queued` = 取消排队（变更为 cancelled）；`succeeded` / `failed` / `expired` = **删除任务
    记录，之后不支持再查询**；`running` 不支持。闭着眼睛发就会把刚跑完的片子连同一个
    「取回」入口一起删掉 —— 那正是坑 202② 那条纪律的反面（台账还在、对象已经没了）。
    查询不产生费用、官方也写明"查询频率不影响任务执行进度"，所以这一次预检是白给的。
    """
    url = task_endpoint(provider, task_id, model)
    st, text = _request(provider, "GET", url, timeout=30)
    payload = _safe_json(text)
    tail = ("任务记录已存进 cloud_jobs.json，之后可以点「取回」拿结果。")
    if st != 200 or not isinstance(payload, dict):
        return False, ("取消之前先查了一次任务状态，没查到（HTTP %s）。"
                       "这一家乱发取消会把已经完成的记录一起删掉，所以这次不发。"
                       "%s" % (st, tail))
    state = normalize_status(payload.get("status"))
    if state != "pending":
        if state == "running":
            return False, ("任务已经在跑了，这一家只在排队中取消得了（官方页写明 running 不支持）"
                           "——这边只能不再等它，照常计费。%s" % tail)
        return False, ("任务已经是「%s」状态，取消不了；这一家的取消接口在终态上是删除任务记录"
                       "（删了就再也取不回来），所以没有发它。%s"
                       % (payload.get("status") or state, tail))
    st2, text2 = _request(provider, "DELETE", url, timeout=30)
    p2 = _safe_json(text2)
    if st2 == 200 and not (isinstance(p2, dict) and p2.get("error")):
        return True, texts.CME_L1_18
    why = _says(text2) or explain(st2, text2, provider, url)
    return False, ("取消没成功：%s\n  云端任务一旦开始运行就取消不了，只能不再等它——%s"
                   % (why, tail))


def _cancel_minimax(provider, task_id, model=""):
    """MiniMax v2（H3 系）的取消：`DELETE /v2/video_generation/{id}`。

    同坑 294 的纪律（先问"这个动词在终态上干的还是同一件事吗"）：官方把这个端点写成
    「取消 / 删除」两个意思，终态上调它到底是取消还是**删记录**，官方页没写死。
    所以照方舟那条办 —— 先 GET 一次状态（`/v2/query/video_generation/{id}`，不花钱），
    只在 `queued` 时才发 DELETE；`running` 与终态都不发，如实说清楚。
    v2 的状态是**小写**（queued/running/succeeded/failed/cancelled），normalize_status 统一收。
    """
    url = task_endpoint(provider, task_id, model)
    st, text = _request(provider, "GET", url, timeout=30)
    payload = _safe_json(text)
    tail = "任务记录已存进 cloud_jobs.json，之后可以点「取回」拿结果。"
    if st != 200 or not isinstance(payload, dict):
        return False, ("取消之前先查了一次任务状态，没查到（HTTP %s）。"
                       "这一家的取消端点兼作删除，状态不明就不发。%s" % (st, tail))
    # v2 把状态放在 task.status；v1 在 task_status。两处都认，认不出就走 unknown 分支
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    state = normalize_status(task.get("status") or payload.get("task_status"))
    if state != "pending":
        if state == "running":
            return False, ("任务已经在跑了，这一家只在排队中取消得掉"
                           "——这边只能不再等它，照常计费。%s" % tail)
        return False, ("任务已经是「%s」状态，取消不了；这一家的取消端点兼作删除任务记录，"
                       "所以没有发它。%s"
                       % (task.get("status") or payload.get("task_status") or state, tail))
    del_url = _at(provider, "/v2/video_generation/%s"
                  % urllib.parse.quote(str(task_id or ""), safe="-_."))
    st2, text2 = _request(provider, "DELETE", del_url, timeout=30)
    p2 = _safe_json(text2)
    if st2 == 200 and not (isinstance(p2, dict) and p2.get("error")):
        return True, texts.CME_L1_18
    why = _says(text2) or explain(st2, text2, provider, del_url)
    return False, ("取消没成功：%s\n  云端任务一旦开始运行就取消不了，只能不再等它——%s"
                   % (why, tail))


def cancel(provider, task_id, model=""):
    """取消任务。**阿里云只有 PENDING 能取消**；RUNNING 会被 400 + UnsupportedOperation 拒
    （文档 §12.3 ④）。方舟那条见 `_cancel_volc`（同一个 DELETE 在终态上是删记录），
    MiniMax v2 那条见 `_cancel_minimax`（同一层意思，官方页没写死）。
    其它家有的压根没给取消端点 → 如实说"取消不了"，不假装成功。
    返回 (ok, 给用户看的说明)。
    """
    proto = protocol_of(provider)
    if proto == PROTOCOL_VOLC:
        return _cancel_volc(provider, task_id, model)
    if proto == PROTOCOL_MINIMAX and _mm_v2(model):
        # 只有 H3 系走 v2；v1（Hailuo 老一代）官方没给取消端点，落到下面那句如实说
        return _cancel_minimax(provider, task_id, model)
    if proto != PROTOCOL_ALIYUN:
        return False, ("「%s」这一家没有可用的取消任务接口（官方文档里没给，或在另一套鉴权上）。"
                       "任务已经在云端排队/运行，这边只能停止等待——"
                       "任务记录已存进 cloud_jobs.json，之后可以点「取回」拿结果。"
                       % (provider or {}).get("name", ""))
    st, text = _request(provider, "POST", task_endpoint(provider, task_id, model) + "/cancel",
                        timeout=30)
    payload = _safe_json(text)
    ok = st == 200 and not (isinstance(payload, dict) and payload.get("code"))
    if ok:
        return True, texts.CME_L1_18
    why = _says(text) or explain(st, text, provider, task_endpoint(provider, task_id, model) + "/cancel")
    return False, ("取消没成功：%s\n  云端任务一旦开始运行就取消不了，只能不再等它——"
                   "任务记录已存进 cloud_jobs.json，之后可以点「取回」拿结果。" % why)



def _sleep(stop_flag, seconds):
    """可被打断的等待；返回是否被置位（与 connection/cloud.py 同名函数一致）。"""
    end = time.time() + seconds
    while time.time() < end:
        if stop_flag is not None and stop_flag.is_set():
            return True
        time.sleep(min(0.2, max(0.0, end - time.time())))
    return False


def _safe_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


# 供 UI 与自检使用：这个 provider 的原生媒体接口能不能用
def check(cfg, provider, model, kind):
    """返回一句"还不能发"的原因，或空串表示可以发。UI 在提交前调它。"""
    if provider is None:
        return texts.CME_RET_2
    if not providers.media_api(provider):
        return ("provider「%s」没接上云端生图/生视频：认不出它的原生接口协议。"
                "\n  解决：在 设置 → 云端模型 → 服务商与密钥 里选「原生接口协议」"
                "（阿里云系 / MiniMax / 智谱 / 华为系 / 火山方舟），或填「原生接口地址」。"
                "\n  官方没有这类接口的（例如 Kimi），请在本地那一组里选模型。"
                % provider.get("name", ""))
    if not providers.media_api_root(provider):
        return ("provider「%s」推不出原生接口地址：在 设置 → 云端模型 → 服务商与密钥 里"
                "填「原生接口地址」。" % provider.get("name", ""))
    if not providers.api_key_of(provider):
        return texts.CLD_CAT_18 % provider.get("name", "")
    if kind == "video" and int(cfg.get("cloud_wait_minutes", 20) or 20) <= 0:
        return texts.CME_RET_3
    return ""
