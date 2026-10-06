# -*- coding: utf-8 -*-
"""llm_console.core.selfupdate — 自替换更新：把 Release 里的 zip 下载、验完、换掉正在跑的 exe

这是 2026-10-05 W 点名要的功能，同时**终结**了 `15-ADR` 里"只查不下载、不自替换"的搁置项
（触发条件"用户抱怨要手动下载"已经由点名本身满足）。

整个链条里最险的一步是"替换正在运行的 exe"。Windows 的规矩：**运行中的 exe 不可覆盖、
不可删除，但可以改名** —— 所以流程是：

    旧 exe  改名 → `llm-chat.exe.updating`（旧进程继续跑，用的是改过名的同一文件）
    新 exe  复制 → 原路径（腾出来的空位）
    拉起新 exe → 旧进程退出（onefile 的父子两进程都锁着旧文件？改了名就不碍事了）

留一代旧版（W 2026-10-05 定）：`.updating` 不马上删 —— 新进程启动时发现"上一次更新
成功启动过"（哨兵文件）才清它。这样万一新版在你机器上起不来，把 `.updating` 改回原名
就回到旧版；同时每次换新版前会先删掉**上一代** `.updating`，目录里最多只有一份。
哨兵与"发现没装好就自动回滚"的关系：新 exe 根本起不来时没有任何代码能替它跑回滚，
所以"自动"只能做到"装好了才清理"；真起不来的人工兜底就是上面那次改名（写进 04）。

纪律与 `engine_install` 一致：**零界面依赖**（无界面环境要能用，自检盯这条纪律的判据
就是"源码里不出现界面库的名字"）；下载 / 校验 / 解压全部复用现成件，不写第二套
（版本判据用 `updater` 的，资产校验用 `engine_install` 的下载器 —— 那套有续传、重试、
sha256 强校验、取消）。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from . import updater
from . import engine_install

# 程序自己的打包资产与可执行文件名（打包口径见 .github/workflows/build.yml 与 03 §5）
EXE_NAME = "llm-chat.exe"
_ASSET_SHAPE = re.compile(r"^llm-chat-.+-windows-x64\.zip$", re.IGNORECASE)

# 旧版文件名后缀与"上次更新成功启动过"的哨兵（都贴在 exe 旁边，跟程序目录走）
OLD_SUFFIX = ".updating"
SENTINEL_SUFFIX = ".update-ok"
# 跨进程更新闸的锁文件（同贴 exe 旁边）。锁的是文件里的一个字节，锁随进程死自动释放，
# 所以"程序更新到一半崩了"不会留下一把永远打不开的死锁 —— 残留的只是个空文件。
LOCK_SUFFIX = ".update-lock"

SMOKE_TIMEOUT = 20          # 新版 --version 烟测的秒数上限（onefile 解包要一两秒，留足）

# 启动清理在**内联**等旧版句柄释放的轮数（每轮 0.5 秒）。做成常量是给自检的：测试里
# 锁住文件走"删不掉 → 转后台"的分支时，把它改成 0，别真睡 5 秒。
_DRAIN_INLINE = 10


class SelfUpdateError(Exception):
    """可预期失败（没有资产 / 解压不像样 / 烟测不过）—— 都已翻成人话。"""


# ---------------------------------------------------------------- 挑资产

def pick_asset(item):
    """从 release JSON 里挑打包 zip → `(下载地址, digest)`；挑不出 `(None, None)` **不猜**。

    先按 tag 精确认（`llm-chat-<tag>-windows-x64.zip`，03 §5 的实表），认不出再按形状认
    （任何 `llm-chat-*-windows-x64.zip`）—— 形状兜底是给"tag 里带了 `/` 之类造不出合法
    文件名"的场合。`digest` 形如 `sha256:…`（GitHub 2025 起自带），直接交给下载器强校验。
    """
    tag = str((item or {}).get("tag_name") or "")
    want = "llm-chat-%s-windows-x64.zip" % tag
    exact, shaped = None, None
    for a in (item or {}).get("assets") or []:
        a = a or {}
        name = str(a.get("name") or "")
        if name == want:
            exact = a
        elif _ASSET_SHAPE.match(name) and shaped is None:
            shaped = a
    got = exact or shaped
    if got is None:
        return None, None
    return (str(got.get("browser_download_url") or ""),
            str(got.get("digest") or ""))


def find_download(channel, fetch=None, token=None, cache=None, repo=updater.REPO):
    """该通道最新一个**带 Windows 打包资产**的 release → 详情 dict；挑不出抛 `SelfUpdateError`。

    版本序与通道分档**全部复用** `updater`（fetch_releases / pick_latest）：这里不写第二套
    判据。`fetch` 可注入（自检喂假响应），`cache` / `token` 与检查更新共用同一套省额度的
    机制 —— 点「立即更新」时重新拉一次列表多半是 304，不吃额度。
    """
    items = updater.fetch_releases(fetch=fetch, cache=cache, token=token, repo=repo)
    latest = updater.pick_latest(items, channel)
    if latest is None:
        raise updater.UpdaterError(
            "更新失败：%s 上没找到可比的%s Release，请稍后重试。"
            % (repo, updater.channel_label(channel)))
    tag = str(latest.get("tag_name") or "")
    url, digest = pick_asset(latest)
    if not url:
        raise SelfUpdateError(
            "更新失败：线上版本 %s 没挂 Windows 安装包（%s），"
            "请到发布页手动下载。" % (updater.display_version(tag), repo))
    size = 0
    for a in latest.get("assets") or []:            # 资产大小给"磁盘还装不装得下"用
        a = a or {}
        if str(a.get("browser_download_url") or "") == url:
            size = int(a.get("size") or 0)
            break
    return {
        "tag": tag,
        "name": str(latest.get("name") or tag),
        "published": str(latest.get("published_at") or "")[:10],
        "notes": updater._notes(latest),
        "url": url,
        "digest": digest,
        "size": size,
    }


# ---------------------------------------------------------------- 下载 / 解压 / 烟测

def staging_dir():
    """装一次用的临时目录。与引擎下载的 `llm-engine-*` 分开前缀，互不误删。"""
    return tempfile.mkdtemp(prefix="llm-selfupdate-")


def cleanup(path):
    """删掉临时目录；删不掉不抛（temp 目录 OS 自己也会收）。直接用引擎那把扫帚。"""
    engine_install.cleanup(path)


def disk_ok(path, need_bytes):
    """`path` 所在盘的剩余空间够不够 `need_bytes` → `(够吗, 剩余字节数)`。

    zip 落 `%TEMP%`、解出的新 exe 进程序目录，**两处**都可能满 —— 满盘上更新
    必然半途而废。查不出来（盘符怪 / 权限）一律当够：空间探测是提醒，
    不许反过来把更新卡死在探测上。
    """
    try:
        free = shutil.disk_usage(path).free
    except Exception:
        return True, 0
    return free >= need_bytes, free


def download_zip(url, dest, emit=None, stop_flag=None, digest="", retries=None):
    """把 zip 落到 `dest` → `(ok, why)`。续传 / 重试 / sha256 强校验 / 取消全在
    `engine_install.download` 里，这里只是**转一手**：名字留在本模块，界面不用知道引擎。

    ⚠ digest 拿不到就**拒装**（2026-10-06）：引擎包下坏了还能重装，程序自己的包
    不带校验就换上去，"半个文件装上去比没装更糟"同样成立 —— 拿不到官方 sha256
    时宁可让用户去发布页手动下，也不做无校验的自替换。
    """
    if not str(digest or "").strip():
        return False, ("拿不到这个安装包的官方校验值（sha256），不敢自动更换程序。"
                       "请到发布页手动下载。")
    kw = {} if retries is None else {"retries": retries}
    return engine_install.download(url, dest, emit=emit, stop_flag=stop_flag,
                                   digest=digest, **kw)


def extract_exe(zip_path, root):
    """解 zip 到 `root`、铺平一层，返回 `llm-chat.exe` 的路径；`(None, why)` = 不像样。

    发行包常带一层目录（`llm-chat-<tag>-windows-x64/`），`engine_install._flatten_for_exe`
    正是干这个的 —— 复用，别抄。ZipSlip 防护：成员名是绝对路径或带 `..` 的直接整个拒收
    （解都不解，宁可失败也不把文件写到目录外面去）。
    """
    try:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                parts = str(n).replace("\\", "/").split("/")
                if str(n).startswith("/") or ".." in parts or (len(parts[0]) >= 2
                                                               and parts[0][1] == ":"):
                    return None, "压缩包里的文件路径不对（%s），已拒收。" % n
            z.extractall(root)
    except SelfUpdateError:
        raise
    except Exception as e:
        return None, "解压失败：%s" % e
    if not engine_install._flatten_for_exe(root, EXE_NAME):
        return None, "压缩包里没找到 %s —— 这个包不是本程序发布的安装包。" % EXE_NAME
    exe = os.path.join(root, EXE_NAME)
    if not os.path.isfile(exe):
        return None, "压缩包里没找到 %s。" % EXE_NAME
    return exe, ""


def child_env():
    """给「本程序起的子进程」备一份干净环境 —— 必须擦掉 PyInstaller onefile 的痕迹。

    **坑（2026-10-05 实测踩到，W 报的「能替换成功、手动双击也正常，就是自动打开起不来」）**：
    onefile 的 bootloader 会把解压目录写进 `_MEIPASS2`、把「我是第几层子进程」写进
    `_PYI_PARENT_PROCESS_LEVEL`。子进程看到这两个变量就**直接复用父进程那份解压目录、不再
    自己解压**；而父进程一退出就把那个目录删掉 —— 于是新进程恰好死在找不到
    `_tcl_data\\init.tcl` 上（日志里会看到 `_MEIxxxxx` 这种临时目录名）。手动双击没有父进程
    可继承，所以没事。

    擦掉这两个（连同 `TCL_LIBRARY` / `TK_LIBRARY`，让新版自己指到自己的目录），其余变量
    （PATH 等）原样带下去。返回的是**副本**，不动本进程的 `os.environ`。
    """
    env = dict(os.environ)
    for k in ("_MEIPASS2", "_PYI_PARENT_PROCESS_LEVEL", "TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(k, None)
    return env


def smoke_test(exe_path, expected, timeout=SMOKE_TIMEOUT, runner=None):
    """新版能不能起来：跑 `<新版> --version`，退出 0 就算过 → `(True, "")`。

    `expected` 是不带 v 的版本号（tag 去 v，与 `config.APP_VERSION` 同形）。**stdout 里的
    版本号对得上更好、对不上不拦**：`--windowed` 的 exe 没有控制台，某些环境下 stdout 是
    空的（`ui/app._say` 拿到 None 就静默）—— 退出码 0 已经证明 onefile 解包 + Python 起
    + 整条 import 链都是好的，这才是烟测要回答的问题。`runner` 可注入（自检不跑真进程）。

    同样要带 `child_env()`：烟测也是子进程，不清环境就是"复用本进程的解压目录"那种局面，
    测的不是新 exe **真能独立启动**这件事（`--version` 不建窗口，父目录还在时照样能过）。
    """
    run = runner or subprocess.run
    try:
        r = run([exe_path, "--version"], capture_output=True, timeout=timeout,
                env=child_env())
    except Exception as e:
        return False, ("新版启动自检没跑起来（%s），不敢替换正在用的程序。"
                       "请到发布页手动下载。" % e)
    if getattr(r, "returncode", 1) != 0:
        return False, ("新版启动自检失败（退出码 %s），不敢替换正在用的程序。"
                       "请到发布页手动下载。" % (
                           getattr(r, "returncode", "?"),))
    txt = ""
    for stream in (getattr(r, "stdout", None), getattr(r, "stderr", None)):
        txt += (stream or b"").decode("utf-8", "replace") if isinstance(
            stream, (bytes, bytearray)) else str(stream or "")
    m = re.search(r"LLM Chat\s+(\S+)", txt)
    if m and m.group(1).lstrip("vV") != str(expected).lstrip("vV"):
        return False, ("新版自检报的版本是 %s，不是要装的 %s —— "
                       "发布包可能与 tag 对不上，已停下。"
                       % (m.group(1), updater.display_version(expected)))
    return True, ""


# ---------------------------------------------------------------- 换 exe

def cur_exe_path():
    """正在运行的 exe 的路径；**源码运行返回 None**（没有 exe 可换，界面据此禁用）。"""
    if not getattr(sys, "frozen", False):
        return None
    return sys.executable


def pending_old(cur_exe):
    """旧版这一代落点的名字：`<exe>.updating`。"""
    return str(cur_exe) + OLD_SUFFIX


def _try_remove(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 跨进程闸与复核

def try_lock(cur_exe):
    """跨进程更新闸（2026-10-06）：同一时刻只许一个实例走「换 exe」这条链。

    为什么必须有：PyInstaller onefile 首启解包要 1~3 秒，"双击没反应再双击一次"是常态
    —— 两个实例都开着「发现新版本」窗口时，后点的会把先点那份**刚装好的新版**当旧版
    改名（`.updating`），用户以为能退回的旧版就被换成了新版。

    锁文件贴在 exe 旁边（`<exe>.update-lock`），锁住其中一个字节：拿到锁的实例握着它
    走完 换 exe → 拉新 → 写哨兵；**进程无论怎么退（含崩溃 / 断电），OS 都会放锁**，
    所以没有"陈锁卡死更新"这回事 —— 残留的至多是个空文件。

    → `(fd, "")` 拿到了（用完必须 `release_lock`）；
      `(None, "locked")` 已被别的实例握着；
      `(None, 其他原因)` 锁文件建不了（多半目录写不进去 —— 让 apply_update 的预检去说人话）。
    """
    try:
        fd = os.open(str(cur_exe) + LOCK_SUFFIX, os.O_CREAT | os.O_RDWR)
    except Exception as e:
        return None, str(e)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, ""
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        return None, "locked"


def release_lock(fd, cur_exe):
    """`try_lock` 的另一半：解锁、关掉、顺手删锁文件（删不掉就算了 —— 它只是个空文件）。"""
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    _try_remove(str(cur_exe) + LOCK_SUFFIX)


def exe_stamp(path):
    """当前 exe 的 (大小, 修改时间) 快照 —— 给 apply_update 做「装之前再核一遍」用。

    窗口打开时记一份，动手前现读现比：另一个实例可能在你确认的这几分钟里已经把
    程序换过了。读不出来返回 None（判不了就别拦更新）。
    """
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except Exception:
        return None


def stamp_changed(path, stamp):
    """exe 现在的样子还和记下的一样吗。`stamp` 为 None（当初没记上）= 当没变。"""
    return bool(stamp) and exe_stamp(path) != stamp


def apply_update(new_exe, cur_exe, expect=None):
    """换 exe（改名腾位 → 复制新 exe）→ `(ok, why)`；失败时**已**尽力回滚。

    每一步都留了退路：目录写不进去 / 程序已被别的实例换过 = 什么都不动就退出；
    改名失败 = 旧程序原封不动（只多不吓人）；复制失败 = 当场把改名改回去。
    走到返回 `(True, "")` 时，旧版在 `.updating`、新版已就位但**还没**启动 ——
    拉新是调用方的活（`launch`），因为它失败要走 `rollback` 而不是这里顺手一改。

    `expect`（可选）是动手前 `exe_stamp(cur_exe)` 记下的快照：装之前再核一遍，
    对不上 = 另一个实例已经换过了 —— 这一版就别再动（再换的话 `.updating` 里的
    回滚副本会被换成新版，用户以为能退回的旧版就没了）。
    """
    d = os.path.dirname(cur_exe) or None
    try:
        pfd, probe = tempfile.mkstemp(dir=d, prefix=".llmchat-upd-")
        os.close(pfd)
        os.remove(probe)
    except Exception as e:
        return False, ("程序所在文件夹写不进去（%s）。把它整个文件夹挪到可写的普通位置"
                       "（别放在 C:\\Program Files 这类系统目录）再更新，"
                       "或到发布页手动下载。" % e)
    if stamp_changed(cur_exe, expect):
        return False, ("本机程序文件已经被另一个窗口更新过了。请关闭本程序，"
                       "重新打开就是新版；这次更新到此为止。")
    old = pending_old(cur_exe)
    _try_remove(old)            # 每次只留一代：先清上一代（删不掉也无妨，改名那步会兜住）
    try:
        os.replace(cur_exe, old)
    except Exception as e:
        return False, ("改不动正在运行的程序（%s）。可能是杀毒软件在拦，也可能是程序文件"
                       "被别的窗口占用；请关掉其他本程序窗口后重试，或到发布页手动下载。" % e)
    # 新版先落 `cur_exe + ".new"` 再**同目录原子改名**：复制中途断电最多留下半个 .new，
    # 正位上要么没有、要么是完整的 —— 半份 exe 冒充程序那种最坏局面不再出现（2026-10-06）。
    new_tmp = cur_exe + ".new"
    try:
        shutil.copy2(new_exe, new_tmp)
        os.replace(new_tmp, cur_exe)
    except Exception as e:
        _try_remove(new_tmp)
        try:                                # 回滚：把旧版改回原名，一切照旧
            os.replace(old, cur_exe)
        except Exception:
            pass
        return False, "新版放不进程序目录（%s），已恢复原样，本机版本没有动。" % e
    return True, ""


def launch(cur_exe, spawner=None):
    """拉起新 exe → `(ok, why)`。`spawner` 可注入（自检不真开进程）。

    ⚠ **一定要带 `child_env()`**：新旧两份都是 onefile，不清环境的话新进程会复用**本进程**
    的解压目录，等本进程退出把它一删，新进程就起不来了 —— 这正是 2026-10-05 那次「更新能
    替换成功、手动双击也正常，就是自动打开报 `Can't find a usable init.tcl`」的根因。
    """
    p = spawner or subprocess.Popen
    try:
        p([cur_exe], cwd=os.path.dirname(cur_exe) or None, env=child_env())
        return True, ""
    except Exception as e:
        return False, str(e)


def rollback(cur_exe):
    """拉新失败时用：删掉放歪的新 exe，把 `.updating` 改回原名。**能保多少保多少**。"""
    old = pending_old(cur_exe)
    _try_remove(cur_exe)                    # 新 exe 没跑起来，没有句柄锁着它
    try:
        os.replace(old, cur_exe)
        return True
    except Exception:
        return False


def mark_updated(cur_exe):
    """换好、新 exe 也拉起了 → 写哨兵。新进程启动时凭它才敢清理 `.updating`。"""
    try:
        with open(str(cur_exe) + SENTINEL_SUFFIX, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        return True
    except Exception:
        return False


def finish_pending(cur_exe, bg=None):
    """新进程启动时调：上次更新**成功启动过**（哨兵在）→ 清哨兵、删旧版 `.updating`。

    幂等，随叫随到；没有旧账时是空操作。`.updating` 删不掉（旧进程的句柄偶尔晚几秒才放）
    就交给后台线程再试几轮，**绝不阻塞启动**——还删不掉就留给下次，反正它只是个旧文件。
    `bg` 可注入（自检传假线程工厂）；不注入时起一个 daemon 线程，界面不用等它。
    """
    if not cur_exe:             # 源码运行没有 exe，谈不上旧账
        return True
    old, sent = pending_old(cur_exe), str(cur_exe) + SENTINEL_SUFFIX
    if not os.path.isfile(old) and not os.path.isfile(sent):
        return True
    _try_remove(sent)
    if _try_remove(old):
        return True
    for _ in range(_DRAIN_INLINE):          # 前 5 秒原地轻试（多数句柄这会儿就放了）
        time.sleep(0.5)
        if _try_remove(old):
            return True
    if bg is None:                          # 还在锁着：转后台，别拖住启动
        import threading
        bg = threading.Thread
    bg(target=_drain_old, args=(old,), daemon=True).start()
    return False


def _drain_old(old, rounds=30):
    """后台慢慢删旧版（每秒一次，半分钟内放手）。删不掉就算了，下次启动再收。"""
    for _ in range(rounds):
        time.sleep(1.0)
        if _try_remove(old):
            return
