# -*- coding: utf-8 -*-
"""llm_console.core.updater — 检查更新：从 GitHub Releases 取版本、与本机版本比新旧

版本比较是纯逻辑（不碰 tkinter、不联网），`fetch` 可注入，自检喂假响应不打真网络。

**正式版 / 测试版怎么分**：本项目 tag 与 `config.APP_VERSION` 一致，形如 `v1.0.3` /
`v1.0.3beta1` / `v1.0.1repair`。带 beta/rc/alpha/dev 的算测试版，`repair` 这类修补后缀
排在正式版之下；判据取"tag 与 GitHub 的 prerelease 勾选**都**说正式才算正式"—— 宁可少报
一次"有新版"，也不把作者还没敢标正式的构建推给正式版用户。

⚠ **404 必须报人话**：仓库不可见 / 该版本已撤下 / 出口被网关拦时，匿名查 releases 都会 404；
不能静默，更不能假装"已是最新"。
"""

import json
import re
import urllib.error
import urllib.request

from .config import USER_AGENT

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

TIMEOUT = 12                  # 秒。GitHub 匿名接口正常 <1s


class UpdaterError(Exception):
    """可预期失败（网络 / 404 / 返回体不对）—— 都已翻成人话。"""


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


# ---------------------------------------------------------------- 取 Release 列表

def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_releases(fetch=None, timeout=TIMEOUT, repo=REPO):
    """GET 该仓库的 releases 列表 → 解析成数组；`fetch` 可注入（自检喂假响应）。"""
    f = fetch or _http_get
    try:
        body = f(API_RELEASES % repo, timeout)
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
        403: ("GitHub 拒绝了此次访问",
              "请几分钟后重试；若仍失败，可能是当前出口 IP 被 GitHub 限流"),
        404: ("GitHub 上找不到该仓库的 Release",
              "请稍后重试；若一直如此，可能是仓库地址变了或该版本已撤下"),
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
    """网络层错误（DNS / 连不上 / 超时）→ 同一句式。"""
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
                 repo=REPO):
    """查该通道有没有比 `current` 新的版本 → `(状态, 详情)`。

    STATE_UPDATE（有新版本，详情带 tag / 名字 / 日期 / 说明摘录 / 下载页地址）/
    STATE_LATEST（已是最新）/ STATE_AHEAD（本机比线上还新）/ STATE_ERROR（查不到，
    详情里的 `msg` 是可直接显示的人话）。**没有"出错就当最新"那一档。**
    """
    try:
        items = fetch_releases(fetch=fetch, timeout=timeout, repo=repo)
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
