# -*- coding: utf-8 -*-
"""llm_console.core.engine_install — 引擎管理：找资产 → 选档 → 下载校验 → 解压安装 + 定向查找

**只依赖标准库 + `core.updater`**（GitHub 资产清单那一层已经写过 ETag/304 与令牌），
不 import tkinter（铁律 1）。界面在 `ui/settings.py:_t10`，本模块只管事实与文件系统。

为什么不用 `connection.cloud_media.download`：那份是给 24 小时过期的 OSS 签名产物用的，
单次命中即走；引擎包几百 MB、要能断点续传（开发机每日 23:30 断电，`07` §5），
还要按 `digest` 校验，所以这里另写一份带 `Range` 续传 + 重试的。

## 三条与上游打交道的实测事实（2026-10-04，改这里前先重测）

1. **llama.cpp 的正式版 tag 里没有 Windows 资产**：`releases/latest` = `v0.5.0`，
   资产只有 `nightly-tag.txt`；预编译包在 `b11382` 这类 tag 上且 `prerelease=True`。
   ⇒ 挑版本**不能**用 `updater.pick_latest`（它按正式版通道挑，会挑中那个空 tag），
   只能"挑最新且**确实带本平台资产**的那一条"（见 `pick_build`）。
2. **两个仓库的 tag 口径完全不同**：llama 是 `b11382`，sd.cpp 是 `master-929-3f8527a`，
   `updater.parse_version` 对两者都返回 `None`。⇒ `build_key` 另写一套。
3. **CUDA 档是"引擎 + 运行库"两个包**（llama `cudart-llama-…`、sd `cudart-sd-…`），
   漏一个就是启动时报缺 dll。两个包的档位写法还不一样（sd 主包 `cuda12`、运行库 `cu12`），
   ⇒ 配对靠"档位段数字集相等"（`cudart_for`）。

平台：档位与 exe 名都按 `os.name` 取，Linux 下同一套代码走 `linux-*` 资产与无后缀 exe
（`07` §4 记了跨平台是既定方向，`tools/deploy_llamacpp.sh` 也是这么写的）。
"""

import hashlib
import os
import re
import shutil
import sys
import urllib.request
import zipfile

from . import updater
from .config import APP_DIR, USER_AGENT

# ---------------------------------------------------------------------------
# 引擎规格（只有"两个仓库口径不同"的部分才写死；档位清单是运行时从资产里推出来的）
# ---------------------------------------------------------------------------

ENGINES = {
    "llama": {
        "label": "llama.cpp（对话引擎）",
        "repo": "ggml-org/llama.cpp",
        "page": "https://github.com/ggml-org/llama.cpp/releases",
        # 发行包名前缀：`llama-b11382-bin-…` / sd 的是 `sd-master-3f8527a-bin-…`
        "bin_tag": "bin",
        # 要装成功后确实存在、才能算"就位"的那个可执行文件（按 os.name 挑后缀）
        "exe": ("llama-server.exe", "llama-server"),
        "dir": "llama",
    },
    "sd": {
        "label": "stable-diffusion.cpp（生图 / 生视频引擎）",
        "repo": "leejet/stable-diffusion.cpp",
        "page": "https://github.com/leejet/stable-diffusion.cpp/releases",
        "bin_tag": "bin",
        "exe": ("sd-cli.exe", "sd-cli"),
        "dir": "sd",
    },
}

# 资产名里的平台段。sd 的资产带 `win` / `Linux-Ubuntu-24.04` / `Darwin-macOS-…`，
# 这里只取**能唯一认出本平台**的那一段，认不出就返回空串（该引擎本平台暂无资产）。
_PLATFORM_TOKENS = ("win", "linux", "darwin", "macos")

# arch 段 → 本机架构的同义写法。**必须按本机架构过滤**：一次发布里通常同时有
# x86_64 与 arm64 两套，不过滤的话两者档位段同名，会被合成一条然后装错架构。
_ARCH_TOKENS = {"x64": ("x64", "x86_64", "amd64"),
                "arm64": ("arm64", "aarch64")}


def platform_token():
    """本机在资产名里长什么样的那一段；认不出返回空串（不猜）。"""
    if os.name == "nt":
        return "win"
    if sys.platform == "darwin":
        return "darwin"
    if os.name == "posix":
        return "linux"
    return ""


def arch_token():
    """架构段的标准写法（`x64` / `arm64`）；只用于**展示**与挑选同义集合。"""
    m = (os.environ.get("PROCESSOR_ARCHITECTURE", "") or "").lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def _arch_tokens():
    """本机架构在资产名里的**全部**写法。同一种架构上游有几种拼法都得认
    （win 侧 `x64`、Linux 侧 `x86_64`），少认一种 = 那一类包整批消失。"""
    return _ARCH_TOKENS[arch_token()]


def exe_name(engine):
    """本平台那个可执行文件的名字（Windows 带 `.exe`）。"""
    spec = ENGINES.get(engine) or {}
    names = spec.get("exe") or ("", "")
    return names[0] if os.name == "nt" else (names[1] if len(names) > 1 else names[0])


def default_dir(engine):
    """自动安装的落点：`<程序目录>/engines/<llama|sd>`（二期只装到这里，不动用户已有的目录）。"""
    spec = ENGINES.get(engine) or {}
    return os.path.join(APP_DIR, "engines", spec.get("dir") or str(engine))


def page_url(engine):
    spec = ENGINES.get(engine) or {}
    return spec.get("page") or ""


# ---------------------------------------------------------------------------
# 版本序：两个仓库各一套（`build_key` 的存在理由见模块开头事实 2）
# ---------------------------------------------------------------------------

_RE_LLAMA = re.compile(r"^b(\d+)", re.I)
_RE_SD = re.compile(r"^master-(\d+)-([0-9a-f]+)", re.I)
_RE_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)", re.I)


def build_key(tag):
    """把 tag 变成可比元组；**认不出来返回 None（不猜）**。

    三种形态各占一个 kind 位，所以"semver 那种"永远排在 `bNNNN` / `master-N-hash` 之前 ——
    这正是我们要的：`v0.5.0` 没有平台资产，本来就该被 `pick_build` 过滤掉。
    """
    s = str(tag or "").strip()
    m = _RE_LLAMA.match(s)
    if m:
        return (1, int(m.group(1)), 0)
    m = _RE_SD.match(s)
    if m:
        return (2, int(m.group(1)), int(m.group(2), 16))
    m = _RE_SEMVER.match(s)
    if m:
        return (0, int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3)), 0)
    return None


# ---------------------------------------------------------------------------
# 资产 → 档位
# ---------------------------------------------------------------------------

# 档位段的中间那截（平台段与 arch 段之间）译成中文。认不出的原样透出，
# 这样上游新增档位时这一行不必跟着改（`07` §5 规则 8：最小表达）。
_FLAVOR_LABEL = {
    "base": "CPU（无后端限定词，纯 CPU）",
    "cpu": "CPU（不占显卡，最慢）",
    "vulkan": "Vulkan（含 AMD / Intel 核显）",
    "sycl": "SYCL（Intel 核显）",
    "blas": "BLAS（纯 CPU）",
    "opencl-adreno": "OpenCL（Adreno）",
    "opencl-adreno-arm64": "OpenCL（Adreno）",
}


def _flavor_label(flavor):
    """`cuda-12.4` → `CUDA 12.4`；`vulkan` → 查表；不认识就原样。"""
    f = str(flavor or "").strip().lower()
    if f in _FLAVOR_LABEL:
        return _FLAVOR_LABEL[f]
    m = re.match(r"^cuda-?([\d.]+)$", f)
    if m:
        return "CUDA %s" % m.group(1)
    m = re.match(r"^rocm-?([\d.]+)$", f)
    if m:
        return "ROCm %s" % m.group(1)
    m = re.match(r"^openvino-?([\d.]+)$", f)
    if m:
        return "OpenVINO %s" % m.group(1)
    return f.upper() if f else "（未识别）"


def _split_asset(name, plat, archs):
    """资产名 → `(档位段, True)`；不是本平台 / 本架构返回 `(None, False)`。

    按 `-` 切段看四件事：arch 段在不在、**是不是本机那个架构**、平台段在不在、
    档位段在 arch 之前还是之后。后两件是实测踩出来的：

    - sd 的 Linux 包是 `…-Linux-Ubuntu-24.04-x86_64-vulkan.zip` —— arch 夹在**中间**，
      当"收尾"找会把它整批丢掉（win 侧一律收尾，所以这个坑只在 Linux 上炸）。
    - 架构必须过滤：一次发布里通常同时有 `x86_64` 与 `arm64`，不过滤的话两者的档位段
      同名，会被合成一条然后**装错架构**。

    认不出的档位段一律给 `base`（无后端限定词 = 纯 CPU 版），**不猜也不丢**：
    上游 `…-bin-linux-x64.zip`、`…-bin-win-arm64.zip` 都是这个形状，丢掉就少一档。
    """
    n = str(name or "").lower()
    if not n.endswith(".zip"):
        return None, False
    segs = n[:-len(".zip")].split("-")
    arch_idx = -1
    for i, s in enumerate(segs):
        if s in archs:
            arch_idx = i
            break
    if arch_idx < 0:
        return None, False
    plat_idx = -1
    for i, s in enumerate(segs):
        # 平台段必须**等于本平台**：`_PLATFORM_TOKENS` 是所有平台的全集，
        # 只判"含某个平台段"会把 win 包当成 linux 的列出来。
        if s == plat:
            plat_idx = i
            break
    if plat_idx < 0:
        return None, False
    # 档位段 = arch 之后（`…-x86_64-vulkan`）优先，否则 = 平台段与 arch 之间（`…-win-cuda-12.4-x64`）
    tail = segs[arch_idx + 1:]
    flavor = "-".join(tail) if tail else "-".join(segs[plat_idx + 1:arch_idx])
    return (flavor.strip("-") or "base"), True


def _nums(text):
    """抽数字段：`cuda-12.4` → `{12, 4}`；`cu12` → `{12}`。"""
    return set(re.findall(r"\d+", str(text or "")))


def cudart_for(assets, main_name, plat, archs):
    """给主包配它那个 CUDA 运行库包；配不到返回 `None`（**不是错误**：CPU / Vulkan 本来就没有）。

    配对判据 = **档位段数字集相等**：主包 `…-cuda-12.4-x64` 只配 `…-cuda-12.4-x64`，
    不会错配到同一次发布里的 `cuda-13.4`；sd 那种主包 `cuda12` / 运行库 `cu12`
    数字集都是 `{12}`，也能配上（见模块开头事实 3）。
    """
    flavor, ok = _split_asset(main_name, plat, archs)
    if not ok:
        return None
    want = _nums(flavor)
    if not want:
        return None
    for a in assets or []:
        n = str(a.get("name") or "").lower()
        if "cudart" not in n or not n.endswith(".zip"):
            continue
        f2, ok2 = _split_asset(n, plat, archs)
        if ok2 and _nums(f2) == want:
            return a
    return None


def flavors_from(build, plat=None, archs=None):
    """从一条 release 的资产里推出本平台可装的档位（按推荐顺序）。

    **刻意不写死档位表**：上游随时会加 / 改名档位，写死的表一漂就静默装错。
    这里每次从真实资产推，代价只是"没联网就没有档位可列"。
    顺序 = CUDA → ROCm → Vulkan → SYCL/OpenVINO → CPU。**这只是显示顺序**；界面里的默认值
    另由 `recommend_flavor` 按本机显卡挑（W 2026-10-04：没有 N 卡就不该默认 CUDA）。
    这里刻意不掺硬件判断：本模块只依赖标准库 + `core.updater`，而探硬件会 spawn
    `nvidia-smi`（最坏 10 秒），那一份判据留在 `core/hardware.py`。
    """
    plat = plat if plat is not None else platform_token()
    archs = archs if archs is not None else _arch_tokens()
    out, seen = [], {}
    for a in build.get("assets") or []:
        n = str(a.get("name") or "")
        if "cudart" in n.lower():
            continue
        flavor, ok = _split_asset(n, plat, archs)
        if not ok:
            continue
        cur = seen.get(flavor)
        if cur is None:
            cur = {"id": flavor, "label": _flavor_label(flavor),
                   "size": int(a.get("size") or 0), "main": a, "extra": None}
            seen[flavor] = cur
            out.append(cur)
        else:
            cur["size"] += int(a.get("size") or 0)
    for f in out:
        rt = cudart_for(build.get("assets"), (f["main"] or {}).get("name"), plat, archs)
        f["extra"] = rt
        f["size"] += int((rt or {}).get("size") or 0)
    out.sort(key=lambda f: _flavor_rank(f["id"]))
    return out


_RANK = {"cuda": 0, "rocm": 1, "vulkan": 2, "sycl": 3, "openvino": 3,
         "cpu": 4, "base": 4, "blas": 5}


def _flavor_rank(flavor):
    """推荐序：CUDA → ROCm → Vulkan → SYCL/OpenVINO → CPU → 其余。只决定**显示顺序**。"""
    f = str(flavor or "").lower()
    for k, r in _RANK.items():
        if f.startswith(k):
            return r
    return 9


# ---------------------------------------------------------------------------
# 默认该选哪一档（按本机显卡挑；W 2026-10-04）
# ---------------------------------------------------------------------------

# 厂商标记 → 档位前缀的偏好序。键来自 `hardware.gpu_kinds`，值按 `flavors_from` 推出来的
# 那套档位 id 做前缀匹配（`cuda-12.4` / `cuda12` / `rocm-7.14.0` / `vulkan` / `sycl` /
# `openvino-2026.4.1` / `cpu` / `base` / `blas` / `opencl-adreno`）：
#   · 有 N 卡 → CUDA（与改动前一致，显示序里 CUDA 本来也排第一）；
#   · A 卡 → 先 Vulkan 再 ROCm：ROCm 只认特定 gfx 型号，Vulkan 各家 GPU 都跑得起来；
#   · Intel（含核显）→ 先 Vulkan，再 SYCL / OpenVINO（后两者还要额外的 Intel 运行库）；
#   · 高通（`opencl-adreno` 档，arm64 Windows）→ OpenCL。
_VENDOR_PREF = {
    "nvidia": ("cuda", "vulkan", "cpu", "base", "blas"),
    "amd": ("vulkan", "rocm", "cpu", "base", "blas"),
    "intel": ("vulkan", "sycl", "openvino", "cpu", "base", "blas"),
    "adreno": ("opencl", "cpu", "base", "blas"),
}
# 多个厂商同时在时按这个序取偏好（实测 W 的机器：Intel 核显 + NVIDIA 独显 ⇒ 独显优先）
_VENDOR_ORDER = ("nvidia", "amd", "intel", "adreno")
# 认不出显卡（远程桌面 / 无头 / 纯虚拟机）时：**保证跑得起来**优先于快 —— Vulkan 档在
# 没有 GPU 的机器上启动即失败，CPU 档在任何机器上都能跑（慢，但用户自己换得回来）。
_UNKNOWN_PREF = ("cpu", "base", "blas", "vulkan")


def recommend_flavor(build, kinds=None, plat=None, archs=None):
    """按本机显卡挑"默认该选哪一档"，返回档位 id；这一版没有可装档位时返回空串。

    `kinds` = `hardware.gpu_kinds()` 的结果（厂商标记集合；`None` / 空集 = 认不出）。
    **本函数不自己去探硬件**：探测会 spawn `nvidia-smi`（最坏 10 秒，坑 4），而本模块
    只依赖标准库 + `core.updater`；判硬件那一份在 `core/hardware.py`，由界面在子线程里
    问一次再传进来（判据仍然只写在一处：厂商 → 档位的映射就在下面这张表）。

    为什么要单独一个"默认值"判据（W 2026-10-04）：界面原来直接拿 `flavors_from` 的第一项
    当默认，而那个显示序把 CUDA 排在最前 ⇒ **没有 N 卡的用户一进页面就被选中一个装上必然
    启动失败的档位**。显示序不动（它只是清单顺序），默认值改为按本机硬件挑。
    """
    fl = flavors_from(build, plat, archs)
    if not fl:
        return ""
    ids = [str(f["id"]) for f in fl]
    pref = _UNKNOWN_PREF
    for v in _VENDOR_ORDER:
        if kinds and v in kinds:
            pref = _VENDOR_PREF[v]
            break
    for p in pref:
        for i in ids:
            if i.lower().startswith(p):
                return i
    # 偏好表一个都没命中（上游改了档位名、或这一版只有 GPU 档）：退回显示序第一项，
    # 别让用户拿不到默认值 —— 与 `pick_build` 的"挑不出就退回最新"同一条口径。
    return ids[0]


# ---------------------------------------------------------------------------
# 拉清单（复用 updater 的 ETag/304 与令牌，不另写 HTTP 层）
# ---------------------------------------------------------------------------

_CACHES = {}


def _cache(repo):
    """按仓库各留一份 `updater.FileCache`：**落盘** ETag 与上次那份资产清单。

    为什么落盘（见 `updater.FileCache` 类注释）：匿名额度 60 次/小时按 IP 跨进程算，
    内存缓存进程退出即清 ⇒ "重启程序再点一次刷新"必然又吃 1 次，一天几十次就见底。
    落盘后重启也能带 `If-None-Match` 拿 304（**不计入额度**）。

    **这一页没有任何自动检查**（W 2026-10-04：不显示未主动触发的版本提示），
    所以 ETag 省钱的地方就在"用户自己反复点检查更新"与"重启后再点"这两处。
    """
    if repo not in _CACHES:
        c = updater.FileCache(os.path.join(
            APP_DIR, "upd_cache_%s.json" % str(repo).replace("/", "_")))
        c.load()          # 读不到就当没有（走普通 GET）；读盘失败不许影响查版本
        _CACHES[repo] = c
    return _CACHES[repo]


def list_builds(engine, token="", fetch=None, timeout=updater.TIMEOUT):
    """列该引擎"确实带本平台资产"的版本，新→旧。`(engine_key, tag, page, assets)`。

    过滤掉没资产的版本是有意的（见模块开头事实 1）——llama.cpp 的 `v0.5.0` 就该被滤掉。
    联网失败按 `updater` 的口径翻人话，调用方直接把 `""` 那一段显示出来即可。
    """
    spec = ENGINES.get(engine) or {}
    repo = spec.get("repo") or ""
    plat = platform_token()
    if not plat:
        return [], "认不出本机在发布包名里长什么样（%s），这一屏不列档位。" % os.name
    if not repo:
        return [], "这个引擎没登记发布地址。"
    try:
        items = updater.fetch_releases(fetch=fetch, timeout=timeout, repo=repo,
                                       cache=_cache(repo), token=token)
        # 拿到响应才落盘（`fetch_releases` 已经把 ETag 与数组写回 cache 对象）。
        # 落盘失败不报错 —— 缓存是纯优化，读写不了就退回"每次都真查一次"。
        _cache(repo).save()
    except Exception as e:
        return [], updater.humanize_net(e, repo)
    builds = []
    for it in items or []:
        assets = list(it.get("assets") or [])
        tag = str(it.get("tag_name") or "")
        if not assets or build_key(tag) is None:
            continue
        b = {"tag": tag, "page": str(it.get("html_url") or "") or spec.get("page"),
             "assets": assets}
        if flavors_from(b, plat, _arch_tokens()):
            builds.append(b)
    builds.sort(key=lambda b: build_key(b["tag"]), reverse=True)
    return builds, ""


def pick_build(builds, flavor_id):
    """按档位 id 在候选版本里挑最新的那一条（挑不到就退回最新那条，别让用户卡住）。"""
    for b in builds or []:
        for f in flavors_from(b):
            if f["id"] == flavor_id:
                return b
    return (builds or [None])[0]


def plan(engine, build, flavor_id, plat=None, archs=None):
    """把"版本 + 档位"落成一张下载清单：`{tag, page, files:[…], dir, exe, total}`。

    `files` 的顺序 = 主包在前、运行库在后（解压无先后要求，但日志好读）。
    """
    plat = plat if plat is not None else platform_token()
    files, total = [], 0
    for f in flavors_from(build, plat, archs):
        if f["id"] != flavor_id:
            continue
        for a in (f["main"], f["extra"]):
            if not a:
                continue
            files.append({"name": str(a.get("name") or ""),
                          "url": str(a.get("browser_download_url") or ""),
                          "size": int(a.get("size") or 0),
                          "digest": str(a.get("digest") or "")})
            total += int(a.get("size") or 0)
        break
    return {"tag": str((build or {}).get("tag") or ""),
            "page": str((build or {}).get("page") or "") or page_url(engine),
            "files": files,
            "dir": default_dir(engine),
            "exe": exe_name(engine),
            "total": total}


# ---------------------------------------------------------------------------
# 下载（Range 续传 + 重试 + sha256）
# ---------------------------------------------------------------------------

CHUNK = 262144
TIMEOUT = 60
RETRIES = 4


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1048576)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _discard(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def staging_dir():
    """装一次用的临时目录（下载的包先落这儿）。删除交给 `cleanup`——
    界面层不 import `shutil`（结构检查 [4] 段会判未绑定名字，坑 93 家族）。"""
    import tempfile
    return tempfile.mkdtemp(prefix="llm-engine-")


def cleanup(path):
    """删掉临时目录；删不掉不抛（临时文件残留不该让界面报错）。"""
    shutil.rmtree(path, ignore_errors=True)


def download(url, dest, emit=None, stop_flag=None, digest="", retries=RETRIES):
    """把一个资产落到 `dest`；写 `.part` 再原子改名。`(ok, why)`

    断点续传：`.part` 已存在且服务器认 `Range` 就接着传（几百 MB 的包遇上断电 / 网断是常态）。
    校验：`digest` 形如 `sha256:…` 时强校验，不过就删掉重下——**半个文件装上去比没装更糟**。
    取消：`stop_flag` 置位即停并删 `.part`（不留半个目录）。
    """
    part = dest + ".part"
    want = str(digest or "").strip().lower()
    if want.startswith("sha256:"):
        want = want[7:]
    else:
        want = ""
    d = os.path.dirname(dest)
    if d:
        os.makedirs(d, exist_ok=True)

    for attempt in range(max(1, int(retries))):
        if stop_flag is not None and stop_flag.is_set():
            _discard(part)
            return False, "已取消"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                # 服务器忽略了 Range（206 才是真续传）：从头写，别把两段拼起来
                if have and int(getattr(r, "status", 200) or 200) != 206:
                    have = 0
                    _discard(part)
                total = int(r.headers.get("Content-Length") or 0) + have
                mode = "ab" if have else "wb"
                if emit:
                    emit("正在下载 %s（%.0f MB）…" % (os.path.basename(dest),
                                                    (total or 0) / 1048576.0))
                with open(part, mode) as f:
                    while True:
                        if stop_flag is not None and stop_flag.is_set():
                            _discard(part)
                            return False, "已取消"
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if emit:
                            emit("正在下载 %s（%.1f / %.1f MB）…"
                                 % (os.path.basename(dest), have / 1048576.0,
                                    (total or 0) / 1048576.0))
            if have < 1024:
                _discard(part)
                return False, "下载到的文件过小（%d 字节），判定为失败" % have
            if want:
                got = _sha256(part)
                if got.lower() != want:
                    _discard(part)
                    if attempt + 1 < retries:
                        if emit:
                            emit("校验不通过，正在重下…")
                        continue
                    return False, "校验不通过（sha256 与发布页写的不一致），已丢弃"
            os.replace(part, dest)
            return True, ""
        except Exception as e:
            # **网络异常不删 `.part`**：留着它下一轮才能靠 `Range` 接着传（几百 MB 的包
            # 遇断电 / 网断是常态，见 `07` §5）。只有校验不过 / 文件过小 / 取消才删。
            if attempt + 1 >= retries:
                _discard(part)
                return False, "下载失败：%s" % e
            if emit:
                emit("下载中断（%s），正在续传（%d/%d）…" % (e, attempt + 2, retries))
    return False, "下载失败：已重试 %d 次仍没成" % retries


# ---------------------------------------------------------------------------
# 解压安装（先解到临时目录，验完再换目录 —— 绝不原地覆盖）
# ---------------------------------------------------------------------------

def _flatten_for_exe(root, exe):
    """发行包常带一层目录：把含 exe 的那层内容铺平到 `root`（CUDA dll 与 exe 必须挨着）。

    与 `tools/deploy_llamacpp.sh` 里那段 Python 同口径：只挪不删，且只做一层。
    """
    if os.path.isfile(os.path.join(root, exe)):
        return True
    for cur, _dirs, files in os.walk(root):
        if cur == root or exe not in files:
            continue
        for name in files:
            dst = os.path.join(root, name)
            if not os.path.exists(dst):
                try:
                    shutil.move(os.path.join(cur, name), dst)
                except Exception:
                    pass
        break
    return os.path.isfile(os.path.join(root, exe))


def install(engine, files, dest_dir, emit=None, stop_flag=None, guard=None):
    """把 `files` 装到 `dest_dir`。`(ok, why, exe_path)`

    落地顺序是"解到旁边 → 验 exe 在不在 → 换目录"，不是就地覆盖：
    换目录用两次 `os.rename`（先把旧的挪成 `.old`，再把新的换上），任何一步失败都回退，
    所以 `dest_dir` **要么是旧的、要么是新的，不存在半个目录**。
    `guard` 是界面给的回调（引擎在跑就返回一句人话拦住），返回非空即中止。
    """
    if guard is not None:
        why = guard()
        if why:
            return False, why, ""
    parent = os.path.dirname(os.path.abspath(dest_dir)) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as e:
        return False, "建不了上级目录：%s" % e, ""
    stage = dest_dir + ".new"
    old = dest_dir + ".old"
    for p in (stage, old):
        shutil.rmtree(p, ignore_errors=True)
    exe = exe_name(engine)
    try:
        os.makedirs(stage, exist_ok=True)
        for f in files or []:
            if stop_flag is not None and stop_flag.is_set():
                raise RuntimeError("已取消")
            if emit:
                emit("正在解压 %s …" % f.get("name"))
            with zipfile.ZipFile(f["path"]) as zf:
                zf.extractall(stage)
            if stop_flag is not None and stop_flag.is_set():
                raise RuntimeError("已取消")
        if not _flatten_for_exe(stage, exe):
            raise RuntimeError("解压完了但没找到 %s（档位可能选错了）" % exe)
    except Exception as e:
        shutil.rmtree(stage, ignore_errors=True)
        return False, "解压失败：%s" % e, ""

    moved_old = False
    try:
        if os.path.isdir(dest_dir):
            os.rename(dest_dir, old)
            moved_old = True
        os.rename(stage, dest_dir)
    except Exception as e:
        if moved_old and not os.path.isdir(dest_dir):
            try:
                os.rename(old, dest_dir)      # 回退：把旧的放回去
            except Exception:
                pass
        shutil.rmtree(stage, ignore_errors=True)
        return False, "换目录失败：%s" % e, ""
    shutil.rmtree(old, ignore_errors=True)
    return True, "", os.path.join(dest_dir, exe)


def installed(engine, dest_dir=None):
    """那份引擎现在是不是真的就位（`dest_dir` 缺省 = 自动安装的默认落点）。返回 exe 路径或空串。"""
    d = dest_dir or default_dir(engine)
    exe = os.path.join(d, exe_name(engine))
    return exe if os.path.isfile(exe) else ""


def find_exe(engine, roots=None, max_depth=3):
    """在 `roots`（默认 = 程序目录 `APP_DIR`）里递归找**本引擎**的可执行文件；找不到返回空串。

    界面「自动定向」用它：把软件所在文件夹扫一遍，找出用户自己放/自己解压的引擎。

    为什么按 **exe 名**找而不是按目录名：两个引擎（`llama-server.exe` / `sd-cli.exe`）可能与
    模型文件同处一个目录，也可能各自在自己的子目录里 —— 只有"这个引擎的可执行文件叫什么"
    是可靠判据（`ENGINES[engine]["exe"]` 一处）。两个引擎各调一次本函数，所以"同处一个目录"
    时也各认各的、不会张冠李戴。

    深度受限（默认 3）：程序目录下常有 `models/` 这类大目录，全树遍历没必要；引擎要么在根，
    要么在 `engines/<engine>` 这种浅层。隐藏目录与 `*.old` / `*.new`（安装换目录的中间态）
    一律跳过 —— 那里可能有半份引擎，指过去反而启动不了。
    """
    exe = exe_name(engine)
    if not exe:
        return ""
    want = exe.lower()
    for root in (list(roots) if roots is not None else [APP_DIR]):
        if not root or not os.path.isdir(root):
            continue
        base = os.path.abspath(root)
        for cur, dirs, files in os.walk(base):
            rel = os.path.relpath(cur, base)
            depth = 0 if rel == os.curdir else rel.count(os.sep) + 1
            if depth >= max_depth:
                dirs[:] = []          # 不再往下走，但这一层仍要查
            dirs[:] = sorted(d for d in dirs
                             if not d.startswith(".") and not d.endswith((".old", ".new")))
            for n in files:
                if n.lower() == want:
                    return os.path.join(cur, n)
    return ""


def path_key(engine):
    """该引擎在配置里存哪一项：llama 存 `exe`（**完整路径**）、sd 存 `sd_dir`（**目录**）。"""
    return "exe" if engine == "llama" else "sd_dir"


def configured_exe(engine, cfg):
    """配置里指向的那个可执行文件**确实存在**时返回它的路径，否则返回空串。

    只管"就位了没有"这一件事：界面的四态状态行还要分"没指路 / 指了路但找不到"，
    那两种措辞留在界面（`_state_of`）；而"首次打开的自动扫描要不要去找它"问这里。
    """
    if engine == "llama":
        p = str((cfg or {}).get("exe") or "").strip()
        return p if p and os.path.isfile(p) else ""
    d = str((cfg or {}).get("sd_dir") or "").strip()
    if not d:
        return ""
    p = os.path.join(d, exe_name("sd"))
    return p if os.path.isfile(p) else ""


def set_dir(cfg, engine, path):
    """把引擎落点写进该引擎的指路项，返回写回的值。

    `path` 可以是那个可执行文件、也可以是它所在的目录（两种都由这里归一）：
    llama 那一项存 **exe 完整路径**、sd 那一项存 **目录** —— 形状不同是既有的，
    本函数只按形状写、不擅自改形状。**只改传入的 cfg，不落盘**（写盘由调用方决定）。
    """
    p = str(path or "").strip()
    if not p:
        return ""
    # 是"本引擎的 exe"就取它所在目录，否则当目录用。判据看**名字**、不看目存在与否：
    # 落点目录可能还没建出来（自检里就是拿一个不存在的 dest 调的），按 isdir 判会多剥一层。
    if os.path.basename(p).lower() == exe_name(engine).lower():
        folder = os.path.dirname(p)
    else:
        folder = p
    if not folder:
        return ""
    if engine == "llama":
        cfg["exe"] = os.path.join(folder, exe_name("llama"))
    else:
        cfg["sd_dir"] = folder
    return cfg[path_key(engine)]


def auto_locate(engine, cfg=None, app_dir=None, max_depth=3):
    """「自动定向」的判据：先在**程序目录**找，扫不到再退**当前模型目录**
    （有些用户把引擎与模型放在一起）。返回本引擎可执行文件的路径，找不到返回空串。

    **只读、不写配置** —— 写回指路由调用方决定：界面「自动定向」按钮与"首次打开的
    自动扫描"共用这一处判据（W 2026-10-05），别各写一遍（坑 128）。
    `app_dir` 可显式传（自检把它指到临时目录，免得去扫真仓库）。
    """
    root = os.path.abspath(app_dir or APP_DIR)
    roots = [root]
    md = str((cfg or {}).get("models_dir") or "").strip()
    if md and os.path.isdir(md) and os.path.normpath(md) != os.path.normpath(root):
        roots.append(md)
    return find_exe(engine, roots=roots, max_depth=max_depth)


def mirror_url(url, mirror=""):
    """套 GitHub 加速镜像前缀（`07` §4：`github.com` 在本环境会被限流/不通，
    部署脚本一直走 `ghfast.top`）。`mirror` 为空表示直连。"""
    m = str(mirror or "").strip()
    if not m:
        return url
    return m.rstrip("/") + "/" + str(url or "").lstrip("/")
