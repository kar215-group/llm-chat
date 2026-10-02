# -*- coding: utf-8 -*-
"""llm_console.core.crashlog — 把"静默失败"变成有名字、有文件、看得见的一次报错。

为什么要有它：exe 是 `--windowed` 打的，控制台被拿掉了，而新设备上最容易出的两类问题
（Tcl/Tk 运行库不完整、放 exe 的目录不允许写入）恰好都发生在**界面还没画出来之前**，
现象就是"双击没反应"，任务管理器里连进程都不留 —— 用户无从下手，我们也无从判断
（坑 65 那次 CI 挂住、坑 93 那次子线程静默死掉，都是同一类"异常看不见"）。

三条约定：
  · 落盘 = `APP_DIR/llm-chat-error.log`（追加，带时间头），与配置同目录，用户按 README 找得到；
  · 弹窗走 Win32 `MessageBoxW`：tkinter 本身可能就是坏掉的那个东西，不能指望它；
  · 本模块**只依赖标准库**（os/sys/datetime/traceback），不 import tkinter、也不在模块顶层
    import config —— 它要在"import 阶段就炸"的时候还能工作。APP_DIR 的算法与
    `core/config._resolve_app_dir` 同源，**改那边要同步这里**（两边各留了指着对方的注释）。

界面用户看得见的入口在 设置 → 关于：「上次异常退出」摘要 + 「打开错误日志」。
"""

import datetime
import os
import sys
import threading
import traceback

LOG_NAME = "llm-chat-error.log"
MAX_LOG_BYTES = 512 * 1024      # 崩溃日志涨到 512KB 就重开一份，别把用户磁盘吃掉


def _app_dir():
    """与 core/config._resolve_app_dir 同一套判据（冻结模式取 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def log_path():
    return os.path.join(_app_dir(), LOG_NAME)


def _version():
    try:
        from .config import APP_VERSION
        return APP_VERSION
    except Exception:
        return "未知（config 没读出来）"


def _header():
    return ("LLM Chat %s 异常 · %s\n  运行方式：%s\n  位置：%s\n  Python：%s\n"
            % (_version(), datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "打包 exe" if getattr(sys, "frozen", False) else "源码",
               _app_dir(), sys.version.split()[0]))


def note(text):
    """往崩溃日志追加一段。返回日志路径；**写不进去也不抛**（这就是最后一次报错）。"""
    path = log_path()
    try:
        if os.path.isfile(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")       # 只留一份旧的，不堆时间戳快照
    except Exception:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_header() + str(text).rstrip() + "\n\n")
    except Exception:
        return ""
    return path


def tail(lines=12):
    """最近几条异常的尾部（给「关于」页显示摘要用）。读不到就回空串。"""
    path = log_path()
    try:
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            rows = f.read().splitlines()
    except Exception:
        return ""
    keep = []
    for ln in reversed(rows):
        keep.append(ln)
        if len(keep) >= lines:
            break
    return "\n".join(reversed(keep))


def popup(text, title="LLM Chat 启动失败"):
    """Win32 消息框：不依赖 tkinter。弹不出来就算了，日志已经先落地。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x10)
        return True
    except Exception:
        return False


def install():
    """把未捕获异常接住写进日志（Tk 回调里的那类由 App.report_callback_exception 负责）。

    两个钩子都要挂：`sys.excepthook` 只管主线程，而本项目最容易出事的是**工作线程**
    （生图 / 生视频 / 云端 worker）—— 那些线程里抛异常走的是 `threading.excepthook`，
    不挂就等于"点什么都没反应、日志里零痕迹"（坑 93 那一类）。
    """
    def _hook(kind, val, tb):
        try:
            note("".join(traceback.format_exception(kind, val, tb)))
        except Exception:
            pass
        try:
            sys.__excepthook__(kind, val, tb)
        except Exception:
            pass

    def _thread_hook(args):
        _hook(type(args.exc_value), args.exc_value, args.exc_traceback)

    sys.excepthook = _hook
    try:
        threading.excepthook = _thread_hook        # Py3.8+
    except Exception:
        pass
    return True
