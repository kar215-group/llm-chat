# -*- coding: utf-8 -*-
"""llm_console.core.updater — 检查更新：从 GitHub Releases 取版本、与本机版本比新旧

版本比较是纯逻辑（不碰 tkinter、不联网），`fetch` 可注入，自检喂假响应不打真网络。

**正式版 / 测试版怎么分**：本项目 tag 与 `config.APP_VERSION` 一致，形如 `v1.0.3` /
`v1.0.3beta1` / `v1.0.1repair`。带 beta/rc/alpha/dev 的算测试版，`repair` 这类修补后缀
排在正式版之下；判据取"tag 与 GitHub 的 prerelease 勾选**都**说正式才算正式"—— 宁可少报
一次"有新版"，也不把作者还没敢标正式的构建推给正式版用户。

⚠ **404 必须报人话**：仓库不可见 / 该版本已撤下 / 出口被网关拦时，匿名查 releases 都会 404；
不能静默，更不能假装"已是最新"。

**省额度**（W 2026-10-04）：匿名接口每 IP 每小时只有 60 次，而"进关于页就查一次"会白白吃它。
两手一起上：① `cache` 里留着上次响应的 ETag 与结果，下次请求带上 `If-None-Match` —— 内容没变
时 GitHub 回 304，且**不计入限流额度**，这时直接复用上次那份、连 JSON 都不重新解析；
② 界面侧给"进页自动查"加冷却（`COOLDOWN`），手动点「检查更新」不受限。
两者只管"什么时候问"，不管"结论是什么"：304 也**不许**被当成"已是最新"（那是 200 + 比对的结果）。

**带令牌那条路**（藏在设置 → 开发者选项里，普通用户看不到）：`token` 非空时请求带
`Authorization: Bearer <token>`，额度从 60 次/小时升到 5000 次/小时，于是界面敢把进页冷却
压到几秒、还敢在后台定期查（见 ui/app.py 的 `_dev_upd_*`）。令牌只走 `secrets.json`，
不落配置、不进日志 —— 这个模块只负责把它放进请求头，不负责它在哪儿存。
"""

import json
import os
import re
import urllib.error
import urllib.request

from . import config
from .config import APP_DIR, USER_AGENT
# 进页冷却的判据统一在 core/throttle（模型补全那边共用同一处），这里转出同名符号
from .throttle import cooldown_left

# 对外仓库坐标（公开仓库，匿名 API 可读）
REPO = "kar215-group/llm-chat"
API_RELEASES = "https://api.github.com/repos/%s/releases?per_page=30"
PAGE_RELEASES = "https://github.com/%s/releases"
PAGE_TAG = "https://github.com/%s/releases/tag/%s"

CHANNEL_STABLE = "stable"
CHANNEL_BETA = "beta"
CHANNELS = ((CHANNEL_STABLE, "正式版"), (CHANNEL_BETA, "测试版"))
CHANNEL_LABEL = dict(CHANNELS)

# 四种结果，没有"出错就当最新"这一档
STATE_UPDATE = "update"      # 有新版本
STATE_LATEST = "latest"      # 已是最新
STATE_AHEAD = "ahead"        # 本机比线上还新（自己的构建没打 tag / 线上还没发）
STATE_ERROR = "error"        # 查不到

# 彩蛋（W 2026-10-05）：本机版本**高于**本次查到的线上最新版时，状态行末尾追这一句。
# 只按**本次返回的结果**判 —— 不为了凑这个彩蛋再去查另一个通道（选正式版就只看正式版，
# 选测试版就只看测试版）。判据就是 `check_update` 给出 STATE_AHEAD，没有额外逻辑。
AHEAD_EGG = "莫非你是测试用户！"

TIMEOUT = 12                  # 秒。GitHub 匿名接口正常 <1s

# 「进关于页自动查」两次之间的最小间隔（秒）；手动点「检查更新」不受它限制。
# 由来：匿名接口每 IP 每小时只给 60 次，而"来回翻设置页"这种不花钱的操作不该把额度吃光。
# W 2026-10-04 定 600 秒。它只是个**节流**，不改变任何结论：冷却内不发请求。
COOLDOWN = 600

# 开发者选项里填了 GitHub 令牌时的冷却（W 2026-10-04 定 **60 秒**）：带认证的额度是
# 5000 次/小时，进页那点频率远够不着上限，没必要再让用户等 10 分钟。
# 从 5 秒调到 60 秒是 W 2026-10-04 的第二次口径：5 秒那档实测太频繁，而 ETag 机制
# （`Cache` 落盘）已经把"每次都真打一次"这个问题解决了大半 —— 冷却只需兜住翻页节奏。
COOLDOWN_TOKEN = 60


# 后台**定时**查更新的间隔（秒）。只有开发者选项里填了 GitHub 令牌才起这个定时器：
# 同一条额度账 —— 匿名 60 次/小时经不起后台常驻，认证 5000 次/小时则连零头都不算。
# 它和 `COOLDOWN` 不冲突：那个管"进页自动查"的节流，这个管"不在关于页时也盯着"。
# W 2026-10-04 定 1800 秒（30 分钟，原 10 分钟）：本程序发版不频繁，盯着太勤没有意义。
POLL_EVERY = 1800


def cooldown_seconds(token=""):
    """按有没有令牌给出该用哪个冷却；界面只问这一处，别自己写 if。"""
    return COOLDOWN_TOKEN if str(token or "").strip() else COOLDOWN


class UpdaterError(Exception):
    """可预期失败（网络 / 404 / 返回体不对）—— 都已翻成人话。"""


class NotModified(Exception):
    """服务端回了 304：内容没变，直接复用上次那份（不计入 GitHub 限流额度）。

    只在**确实留着上次结果**时才允许走到这里（见 `fetch_releases`）；没得复用就翻成人话报错。
    """


class Cache:
    """上一次 releases 响应的 ETag 与解析结果（**只存内存**，由调用方持有，进程退出即清）。

    为什么要带 `If-None-Match`：内容没变时 GitHub 回 304 并且不计入匿名限流额度
    （60 次/小时/IP），于是"进关于页就查一次"可以放心带着它发。
    304 时复用 `items`，不重新解析 JSON —— 也就不存在"304 没东西可比"这种岔路。
    """

    def __init__(self):
        self.etag = None      # 上次响应头里的 ETag（服务端没给就留 None：下次走普通 GET）
        self.items = None     # 上次解析好的数组（None = 没缓存过，304 时不敢复用）


class FileCache(Cache):
    """`Cache` 的**落盘**版：ETag 与上次那份数组一起写进 `<APP_DIR>/upd_cache.json`。

    为什么内存那份不够（W 2026-10-04）：匿名额度 60 次/小时是**按 IP 跨进程**算的，
    而内存缓存进程退出即清 —— 于是"重启程序 → 点一次检查更新"必然又吃 1 次额度，
    一天几十次就见底。落盘之后重启也能带 `If-None-Match`，拿到 304（不计额度）。

    写盘一律走 `config.atomic_write_json`（先写 .tmp 再 `os.replace`，不抛）。
    **缓存是纯优化，不是权威**：读不出来 / 坏了 / 写不下去，一律当没有缓存（走普通 200），
    绝不让"缓存坏了"变成"查不到版本"。`items` 为空时**不落盘**（304 时没东西可复用）。
    """

    def __init__(self, path=None):
        Cache.__init__(self)
        self.path = path or os.path.join(APP_DIR, "upd_cache.json")

    def load(self):
        """把上次落盘的 ETag 与数组读回来；读不到 / 不成形就当没有（返回是否可用）。"""
        try:
            with open(self.path, "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
        except Exception:
            return False
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return False
        self.items = items
        self.etag = str(data.get("etag") or "") or None
        return True

    def save(self):
        """把当前这份写下去。**只在有东西可复用时写**（见类注释）。"""
        if not self.items:
            return False
        try:
            return bool(config.atomic_write_json(
                self.path, {"etag": self.etag or "", "items": self.items}))
        except Exception:
            return False


# ---------------------------------------------------------------- 版本号解析与比较

_VER_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?([A-Za-z][\w.\-]*)?\s*$")
_PRE_WORDS = ("alpha", "beta", "rc", "dev", "pre", "preview", "snapshot", "nightly")

_RANK_HOTFIX = 0     # 修补构建：1.0.1repair < 1.0.1
_RANK_PRE = 1        # 测试版：1.0.3beta1 < 1.0.3beta2 < 1.0.3
_RANK_FINAL = 2      # 正式版


def _suffix_key(suffix):
    """后缀 → 排序键，整个模块的版本序都定在这里：`repair` → `(0, …)` 垫底、
    `beta1` → `(1, 序号)`、纯数字 → `(2, …)` 最高，故 `beta1 < beta2 < 正式版`。"""
    s = str(suffix or "").strip().lower().lstrip("-_.")
    if not s:
        return (_RANK_FINAL, 1)
    m = re.match(r"^([a-z]+)\.?(\d*)$", s)
    if not m:
        return (_RANK_HOTFIX, 1, s)     # 认不出的后缀按修补处理，同类之间仍按字典序
    word, num = m.group(1), int(m.group(2) or 0)
    if word in _PRE_WORDS:
        return (_RANK_PRE, num + 1)
    return (_RANK_HOTFIX, num + 1, word)


def parse_version(text):
    """`v1.0.3beta1` → `(major, minor, patch, 档位, 档内序号)`；认不出来返回 None（不猜）。

    小版本按数字比，所以 `1.0.10 > 1.0.9`（字符串比会把这条判反）。
    """
    m = _VER_RE.match(str(text or ""))
    if not m:
        return None
    g = m.groups()
    return (int(g[0]), int(g[1] or 0), int(g[2] or 0)) + _suffix_key(g[3])


def is_prerelease(text):
    """是不是非正式版（tag 口径）；认不出来的版本名一律当正式版 —— 宁可少报一次。"""
    k = parse_version(text)
    return bool(k) and k[3] < _RANK_FINAL


def compare_versions(a, b):
    """a 比 b 新返回 1、旧 -1、一样 0；任一端认不出来返回 None（"比不了"≠"一样新"）。"""
    ka, kb = parse_version(a), parse_version(b)
    if ka is None or kb is None:
        return None
    return (ka > kb) - (ka < kb)


def display_version(text):
    """补界面用的 `v` 前缀，已经有不补 —— tag 自带 `v` 而 `APP_VERSION` 不带，
    两处都硬拼会出 `vv1.0.0`。"""
    s = str(text or "").strip()
    return s if (not s or s[0] in "vV") else "v" + s


def channel_of_label(label):
    """界面文案（"正式版" / "测试版"）→ 通道代码；认不出来按正式版（保守）。"""
    for code, text in CHANNELS:
        if str(label or "").strip() == text:
            return code
    return CHANNEL_STABLE


def channel_label(channel):
    return CHANNEL_LABEL.get(channel, "正式版")


# ---------------------------------------------------------------- 冷却（进页自动查的节流）
#
# `cooldown_left` 实体已搬到 `core/throttle`（模型补全要用同一套判据），这里只转出符号：
# 别在别处再写一份 —— 冷却口径只有一处（W 2026-10-05）。


# ---------------------------------------------------------------- 取 Release 列表

def _http_get(url, timeout, etag=None, token=None):
    """GET → `(body, etag)`；带了 `etag` 就发 `If-None-Match`、带了 `token` 就发 `Authorization`。

    服务端回 304 时 urlopen 抛 HTTPError（没有响应体可读），这里翻成 `NotModified`：
    对调用方来说"没变化"不是失败。
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if etag:
        headers["If-None-Match"] = str(etag)
    if token:
        headers["Authorization"] = "Bearer " + str(token).strip()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if int(getattr(e, "code", 0) or 0) == 304:
            raise NotModified(str(etag or ""))
        raise


def fetch_releases(fetch=None, timeout=TIMEOUT, repo=REPO, cache=None, token=None):
    """GET 该仓库的 releases 列表 → 解析成数组；`fetch` 可注入（自检喂假响应）。

    `cache`（`Cache`，可不传）里留着上次的 ETag 与结果：有得带就带上 `If-None-Match`，
    内容没变时 GitHub 回 304、**不计入限流额度**，这时直接复用上次那份（不重新解析）。
    结果照旧写回 `cache`，供下一次用。

    `token` 非空时请求带 `Authorization`（额度 60 → 5000 次/小时）。它**不往 `fetch` 上递**：
    自检注入的假 fetch 只认 `(url, timeout, etag)`，多一个位置参数就把它们全打翻；
    令牌只在与真实 `_http_get` 打交道的那个闭包里出现。
    """
    f = fetch or (lambda url, timeout, etag=None: _http_get(url, timeout, etag, token))
    # 只有"上次真留下结果"时才带条件头：否则万一服务端回 304，我们没东西可复用，
    # 只能当场报错 —— 与其走到那一步，不如老老实实发一次普通 GET（200）。
    etag = cache.etag if (cache is not None and cache.items is not None) else None
    try:
        body, new_etag = f(API_RELEASES % repo, timeout, etag)
    except NotModified:
        if cache is not None and cache.items is not None:
            return cache.items
        # 没得复用：翻成人话报错，**不**装作"已是最新"（见模块开头那段）
        raise UpdaterError(humanize_http(304, "", repo))
    except UpdaterError:
        raise
    except urllib.error.HTTPError as e:
        raise UpdaterError(humanize_http(e.code, _safe_read(e), repo))
    except Exception as e:
        raise UpdaterError(humanize_net(e, repo))
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception:
        raise UpdaterError("检查更新失败：GitHub 返回的不是 JSON（可能被公司网关拦截），"
                           "请换个网络环境后重试。%s" % _MANUAL)
    if not isinstance(data, list):
        raise UpdaterError("检查更新失败：GitHub 返回的数据格式不对，"
                           "请稍后重试。%s" % _MANUAL)
    if cache is not None:
        cache.items = data
        # 服务端这次没回 ETag（或换了弱校验）就别把旧值丢了：留着它下次照样能问出 304
        cache.etag = new_etag or cache.etag
    return data


def pick_latest(items, channel=CHANNEL_STABLE):
    """挑该通道最新的一个；挑不出返回 None。

    正式版 = tag 与 prerelease 勾选都说正式（任一说是就算测试版）；测试版 = 全部。
    版本号认不出来的条目跳过 —— 不拿看不懂的 tag 去催更。
    """
    best, best_key = None, None
    for it in items or []:
        it = it or {}
        tag = str(it.get("tag_name") or "")
        key = parse_version(tag)
        if key is None:
            continue
        if channel == CHANNEL_STABLE and (is_prerelease(tag) or it.get("prerelease")):
            continue
        if best_key is None or key > best_key:
            best, best_key = it, key
    return best


# ---------------------------------------------------------------- 错误翻人话

_MANUAL = "请手动前往仓库下载更新。"


def _reason(code):
    """HTTP 状态码 →（原因短语，下一步短语）。

    统一句式（W 2026-10-03 定的口径）：**一句原因 + 一句下一步**，不把服务端原话、
    接口地址、字段名往外倒 —— 用户要看到的是"我该做什么"。每条都以"手动去仓库下载"
    收尾：客户端这条路失败时，手动那条永远走得通。
    """
    return {
        # 401 只可能出现在"带了令牌"那条路上（不带的请求根本没有 Authorization 头，
        # GitHub 对匿名只会回 403）。所以这里可以直接把话说死：是令牌的问题。
        401: ("GitHub 不认这个令牌",
              "请在「开发者选项」里重新填写 GitHub 令牌"),
        403: ("GitHub 拒绝了此次访问",
              "请几分钟后重试；若仍失败，可能是当前出口 IP 被 GitHub 限流"),
        404: ("GitHub 上找不到该仓库的 Release",
              "请稍后重试；若一直如此，可能是仓库地址变了或该版本已撤下"),
        304: ("GitHub 说 Release 没变化，但本机没留下上次的结果可复用",
              "请再点一次「检查更新」"),
        422: ("GitHub 不认这个请求", "请稍后重试"),
        429: ("请求太密，被 GitHub 限流了", "请过几分钟再点一次"),
        500: ("GitHub 服务端出错", "请稍后重试"),
        502: ("GitHub 网关无响应", "请稍后重试"),
        503: ("GitHub 服务暂时不可用", "请稍后重试"),
    }.get(int(code), ("GitHub 返回异常状态（HTTP %s）" % code, "请稍后重试"))


def _safe_read(err, limit=400):
    try:
        return err.read().decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def humanize_http(code, body="", repo=REPO):
    """HTTP 错误 → 可直接显示的一句话（原因 + 下一步 + 手动兜底）。"""
    why, how = _reason(code)
    return "检查更新失败：%s，%s。%s" % (why, how, _MANUAL)


def humanize_net(err, repo=REPO):
    """网络层错误（DNS / 连不上 / 超时）→ 同一句式。

    `UpdaterError` 进来**原样返回**：它已经是 `humanize_http` 翻好的人话了，再套一层
    会变成"检查更新失败：检查更新失败：…。。。请手动前往仓库下载更新。。请手动前往…" ——
    前缀重复、句号叠成"。。"。2026-10-04 真联网才暴露（403 限流那条路径）。
    """
    if isinstance(err, UpdaterError):
        return str(err)
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", err)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return ("检查更新失败：连接 GitHub 超时（%d 秒内无响应），"
                    "请检查网络或代理设置后重试。%s" % (TIMEOUT, _MANUAL))
        return ("检查更新失败：连不上 GitHub（%s），"
                "请检查网络 / 代理 / 防火墙（公司网络常拦 api.github.com）。%s"
                % (reason, _MANUAL))
    return "检查更新失败：%s。%s" % (err, _MANUAL)


# ---------------------------------------------------------------- 主入口

def _notes(item):
    """Release 说明摘前几行（整篇贴进对话框会让人读不下去）。"""
    s = re.sub(r"^#{1,6}\s*", "", str((item or {}).get("body") or "").strip(), flags=re.M)
    out, n = [], 0
    for line in s.splitlines():
        out.append(line)
        n += len(line) + 1
        if n >= 240:
            break
    return "\n".join(out).strip()


def check_update(current, channel=CHANNEL_STABLE, fetch=None, timeout=TIMEOUT,
                 repo=REPO, cache=None, token=None):
    """查该通道有没有比 `current` 新的版本 → `(状态, 详情)`。

    STATE_UPDATE（有新版本，详情带 tag / 名字 / 日期 / 说明摘录 / 下载页地址）/
    STATE_LATEST（已是最新）/ STATE_AHEAD（本机比线上还新）/ STATE_ERROR（查不到，
    详情里的 `msg` 是可直接显示的人话）。**没有"出错就当最新"那一档。**

    `cache`（`Cache`，可不传）用来省匿名限流额度：留着上次的 ETag 与结果，下次带
    `If-None-Match`，没变化时 GitHub 回 304 不计额度（见 `fetch_releases`）。

    `token`（可不传）非空就走认证请求，额度 60 → 5000 次/小时；只有开发者选项里
    填了 GitHub 令牌才会有值。它**不改变任何判据**，只改变"我们能问多频繁"。
    """
    try:
        items = fetch_releases(fetch=fetch, timeout=timeout, repo=repo, cache=cache,
                               token=token)
    except UpdaterError as e:
        return STATE_ERROR, {"channel": channel, "current": current, "msg": str(e)}
    except Exception as e:                      # 兜底：绝不让异常穿到 UI 线程
        return STATE_ERROR, {"channel": channel, "current": current,
                             "msg": humanize_net(e, repo)}

    latest = pick_latest(items, channel)
    if latest is None:
        return STATE_ERROR, {
            "channel": channel, "current": current,
            "msg": "检查更新失败：%s 上没找到可比的%s Release，"
                   "请稍后重试。%s" % (repo, channel_label(channel), _MANUAL)}

    tag = str(latest.get("tag_name") or "")
    info = {
        "channel": channel, "current": current, "tag": tag,
        "name": str(latest.get("name") or tag),
        "published": str(latest.get("published_at") or "")[:10],
        "notes": _notes(latest),
        "url": str(latest.get("html_url") or "") or (PAGE_TAG % (repo, tag)),
    }
    cmp = compare_versions(tag, current)
    if cmp is None:
        info["msg"] = ("线上最新是 %s、本机是 %s：版本号写法认不出来，没法比大小，"
                       "请手动去 %s 看一眼。" % (tag, current, PAGE_RELEASES % repo))
        return STATE_ERROR, info
    if cmp > 0:
        return STATE_UPDATE, info
    return (STATE_LATEST if cmp == 0 else STATE_AHEAD), info
