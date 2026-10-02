# -*- coding: utf-8 -*-
"""llama_gui.py — 启动入口。

v30 起代码按 core / connection / ui 三层拆进 llm_console 包，本文件只保留入口，
所以命令行、双击 .py、快捷方式、pythonw 无窗启动这几种习惯都不用改。

    python  llama_gui.py     # 带控制台，报错直接看得见，第一次跑推荐用这个
    pythonw llama_gui.py     # 无控制台窗口

**这个文件是最后一道能看见错误的地方**：打成 `--windowed` 的 exe 后没有控制台，
而新设备上最容易出的问题（Tcl/Tk 运行库不完整、目录不可写）偏偏发生在界面画出来之前，
不接住就是"双击没反应、什么痕迹都不留"。所以这里：
  · 把 `import llm_console` 本身也包进 try —— 它正是最可能炸的那一步；
  · 炸了就先写 `llm-chat-error.log`（与 exe 同目录），再用 Win32 消息框说一次话；
  · 弹窗走 ctypes 而不是 tkinter，因为 tkinter 可能就是坏掉的那个东西。
落盘与弹窗的正常实现都在 `core/crashlog.py`；本文件只用标准库写一份**兜底副本**，
因为 import 那个模块失败时正好需要它。两边的目录判据要一致（见 crashlog._app_dir）。
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _fallback_note(text):
    """core.crashlog 都没能 import 时的兜底：只写文件，不依赖包内任何东西。"""
    import datetime
    app_dir = (os.path.dirname(os.path.abspath(sys.executable))
               if getattr(sys, "frozen", False)
               else os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(app_dir, "llm-chat-error.log"), "a", encoding="utf-8") as f:
            f.write("LLM Chat 入口异常 · %s\n  Python：%s\n%s\n\n"
                    % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       sys.version.split()[0], text.rstrip()))
    except Exception:
        pass


def _fail(text):
    try:
        from llm_console.core import crashlog
        crashlog.note(text)
        crashlog.popup("程序没能正常启动。\n\n详情已写到：\n%s\n\n"
                       "常见原因：杀毒软件或 Windows 拦下了刚下载的文件；运行库不完整；"
                       "或这个目录不允许写入 —— 把 exe 换到一个你自己建的、可写的文件夹里再试。"
                       % crashlog.log_path())
    except Exception:
        _fallback_note(text)


def _run():
    from llm_console.ui.app import main
    main()


def _app_dir():
    """与 `core/config.APP_DIR`、`core/crashlog._app_dir` 同一条判据（三处必须一致）。"""
    return (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))


def _dpi_pref():
    """从 gui_config.json 里读 `dpi_aware`（读不到就按默认"开"）。

    这一步**不 import 包、也不写文件**：DPI 感知必须在创建任何窗口之前定下来，
    而它偏偏赶在最前面 —— 配置被写坏、目录读不动这些情况都得让程序继续起得来。
    """
    try:
        import json
        with open(os.path.join(_app_dir(), "gui_config.json"), "r", encoding="utf-8") as f:
            return int(json.load(f).get("dpi_aware", 1))
    except Exception:
        return 1


def _apply_dpi(on):
    """把进程标成 DPI 感知，返回一句状态（诊断那一页会把它原样打出来）。

    为什么默认是"开"：不开的话 Windows 会把整个窗口按系统缩放**位图拉伸**，
    150% 屏上界面大 1.5 倍且发虚；而三条界面自检全部在 `SetProcessDpiAwareness(2)`
    之后量（断言 scaling ≥ 1.5），也就是说被验证过的那一版正是"开"的这一版。
    为什么做成可关：少数机器上显卡驱动 / 远程桌面的缩放会跟 Per-Monitor 打架，
    留一个开关比让用户改注册表友好 —— 它是**冷切换**，只在下次启动生效。
    """
    if not on:
        return "关（界面按系统缩放拉伸，高分屏会发虚）"
    try:
        import ctypes
    except Exception as e:                       # 连 ctypes 都没有就别再往下试了
        return "开不了：%s: %s" % (type(e).__name__, e)
    # 已经定过了就别说"开不了"：清单里带了 dpiAware 的 exe、或同一进程里第二次调用，
    # 都会让下面每个 setter 返回 E_ACCESSDENIED —— 那正是"已经是感知档"的意思
    if _dpi_state() != 0:
        return "开（进程起时就已定档）"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4（Win10 1703+），返回非 0 = 成功。
        # 注意它排在最前**不是**为了"更高档"：老的 GetProcessDpiAwareness 只会报 2，
        # 分不清 V1/V2 —— 排前面是因为多显示器不同缩放时 V2 的行为才是对的
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "开（逐窗感知 V2）"
    except Exception:
        pass                                     # 老系统没这个导出，退下一档
    try:
        # shcore 返回 HRESULT，**0 才是成功**（这里最容易写反）
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:      # Win8.1+
            return "开（逐窗感知）"
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():                # 非 0 = 成功
            return "开（系统级感知，老接口）"
    except Exception:
        pass
    return "开不了（Windows 没接受任何一档）"


def _dpi_state():
    """当前进程的 DPI 感知档：0=不感知 1=System 2=PerMonitor 3=PerMonitorV2。"""
    try:
        import ctypes
        v = ctypes.c_int()
        if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(v)) == 0:
            return int(v.value)
    except Exception:
        pass
    try:
        import ctypes
        return 1 if ctypes.windll.user32.IsProcessDPIAware() else 0
    except Exception:
        return 0


if __name__ == "__main__":
    _apply_dpi(_dpi_pref())
    try:
        _run()
    except SystemExit:
        raise                       # --selfcheck / --version 靠它给退出码，别当成崩溃
    except BaseException:
        _fail(traceback.format_exc())
        sys.exit(1)
