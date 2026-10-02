# -*- coding: utf-8 -*-
"""llm_console.core.diagnose — 新设备排障：把"起不来 / 起得来 / 用得对"三层各查一遍。

面向的是**下载打包 exe 双击运行**的用户（不是从源码跑的人），所以：
  · 每条 `fix` 都写"他自己能做的动作"，不出现 `python -m ...`、不出现任何盘符假设（坑 55）；
  · 源码模式才相关的项（Python / Tkinter）在 exe 模式标成 `na`（不适用），**不藏代码路径**；
  · 全部只读，唯一的副作用是往 APP_DIR 试写一个 `.probe` 再删（"目录可写"这件事只能真试）。

不做什么：不联网、不 Popen 推理引擎、不跑 GPU。`nvidia-smi` 只在配置里没有 GPU 信息时
才调一次（坑 4：外部命令只在用户动作点上跑一次，这里就是那个点）。

Check 结构：
    {id, group, level, title, fact, fix, action}
    level  ∈ ok | warn | fail | na
    action ∈ ("none",) | ("open_dir", 路径) | ("settings", 指路文字) | ("log",)
"""

import os
import sys
import time

from . import config, hardware, models, params, secrets
from .config import write_error          # 只读：本模块不写状态文件（判据见 run_checks 里那条注释）


GROUP_ENV = "运行环境"
GROUP_ENGINE = "推理引擎"
GROUP_MODEL = "模型"
GROUP_MEDIA = "本地生图 / 生视频"
GROUP_PORT = "端口与代理"
GROUP_CLOUD = "云端"
GROUP_STATE = "配置文件"

OK, WARN, FAIL, NA = "ok", "warn", "fail", "na"


def _c(cid, group, level, title, fact, fix="", action=("none",)):
    return {"id": cid, "group": group, "level": level, "title": title,
            "fact": fact, "fix": fix, "action": action}


def _writable(dirpath):
    """真试写一个临时文件再删 —— 目录权限这件事，猜不出来（Program Files 会骗人）。"""
    probe = os.path.join(dirpath, ".llm-chat-write.probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
        return True, ""
    except Exception as e:
        try:
            os.remove(probe)
        except Exception:
            pass
        return False, "%s: %s" % (type(e).__name__, e)


def _drive_type(path):
    try:
        import ctypes
        root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except Exception:
        return 0


def _dpi_aware():
    """本进程的 DPI 感知档：0=不感知 1=System 2=PerMonitor 3=PerMonitorV2。

    为什么值得单列一条：三条界面自检都是先 `SetProcessDpiAwareness(2)` 再量的
    （断言 scaling ≥ 1.5），所以"被验证过的那一版"就是感知版；打包 exe 的清单里
    没写 `dpiAware`，感知与否**完全由入口 `llama_gui.py` 在开窗之前那一下决定** ——
    用户那边"界面比预期大一圈还发虚"就是这么来的，报告里要能直接看到答案。
    """
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


def dpi_label(tier):
    """把感知档翻成人话。老的 Win32 查询分不清 V1/V2（都报 2），所以不写"第几档"。"""
    if tier >= 2:
        return "已开（逐窗感知）"
    if tier == 1:
        return "已开（系统级感知）"
    return "没开"


def _free_gb(path):
    try:
        import ctypes
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        if not ctypes.windll.kernel32.GetDiskFreeSpaceExW(root, None,
                                                          ctypes.byref(total),
                                                          ctypes.byref(free)):
            return 0.0
        return round(free.value / (1 << 30), 1)
    except Exception:
        return 0.0


def _zone_marked(path):
    """文件带"来自网络"标记（NTFS 备用数据流 Zone.Identifier）—— SmartScreen 拦的就是它。"""
    try:
        with open(path + ":Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            return "ZoneId=" in f.read()
    except Exception:
        return False


def _port_busy(port):
    """能不能占住这个端口。127.0.0.1 上的 bind 是本地的，不发任何包出去。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        return False
    except OSError:
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _config_state():
    """区分"从没配过"与"配置文件读坏了"—— 这两件事在界面上以前长得一模一样（坑 33 / 107）。

    路径**每次调用时现读** `config.CONFIG_PATH`：模块级绑定的话，自检把 CONFIG_PATH
    指到临时目录就永远测不到这条（真踩过）。
    """
    path = config.CONFIG_PATH
    if not os.path.isfile(path):
        return "none", "还没有配置文件（首次运行会创建）"
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return "ok", path
    except Exception as e:
        return "broken", "%s: %s" % (type(e).__name__, e)


# 只有本地链路才需要的条目：云端已经能用时，它们不该继续报"故障"
LOCAL_ONLY = ("server_exe", "cudart", "gpu", "ram", "models_dir", "chat_models",
              "selected", "budget", "vision", "sd_cli", "img_models", "vid_models")


def _cloud_ready(cfg):
    """云端这条通不通 = 有启用的服务商 **且** 那家填过密钥。只数有没有，不外泄内容。

    ⚠ 密钥表是**嵌套**的：`load_secrets()` 返回 `{"version":…, "api_keys": {pid: key}}`，
    按顶层取 pid 会永远取不到 —— 那会让每一个真在用云端的用户都被判成"云端未就绪"
    （写这条时踩过，判据要走 `api_keys` 那一层，或用 secrets.get_api_key）。
    """
    try:
        keys = (secrets.load_secrets() or {}).get("api_keys") or {}
    except Exception:
        return False
    for p in (cfg.get("cloud_providers") or []):
        pid = str((p or {}).get("id", ""))
        if pid and (p or {}).get("enabled", True) and keys.get(pid):
            return True
    return False


def guide_missing(cfg):
    """引导第一屏那份缺件清单（Check 形状，但**只走廉价判据**）。

    为什么不用 `run_checks`：那套要真往目录里试写 `.probe`、bind 两个端口、逐模型算
    ngl —— 而引导这份清单是在**主线程**上算的（首跑 after(400) 与每次点「新手引导」），
    挂上去就是"一点就冻"。要看全的出口是「诊断」页里的「一键诊断」（那里放线程里跑）。

    口径与 §5.6 一致：**本地与云端任一能跑就不算缺件**（只想用云端的人不缺东西）。
    """
    q = quick_paths(cfg)
    if q["local"] or q["cloud"]:
        return []
    out = [_c("guide_miss_%d" % i, GROUP_ENGINE, FAIL, t, "", "",
              ("settings", "本地模型 → 模型文件与引擎"))
           for i, t in enumerate(q["missing"])]
    out.append(_c("guide_cloud", GROUP_CLOUD, FAIL, "云端一家 API Key 都没填", "", "",
                  ("settings", "云端模型 → 服务商与密钥")))
    return out


def quick_paths(cfg):
    """两条路通不通的**廉价**判定：不试写目录、不查端口、不探显卡。

    给"每次开程序都要跑一次"的地方用（输出栏那句缺引擎的提示）。`run_checks` 不能
    替代它 —— 那条会往目录里试写 `.probe`、还要量端口，那是用户点「一键诊断」时
    才付得起的一次性开销。
    返回 {"local": bool, "cloud": bool, "missing": [给人看的一句话]}。
    """
    cfg = cfg or {}
    exe = str(cfg.get("exe", "") or "")
    engine = bool(exe.strip()) and os.path.isfile(exe)
    try:
        _d, chat, _i = models.scan_models(cfg)
    except Exception:
        chat = []
    missing = []
    if not engine:
        missing.append("推理引擎（llama-server）还没下载或没指路")
    elif not chat:
        missing.append("模型目录里没有能对话的模型")
    return {"local": bool(engine and chat), "cloud": _cloud_ready(cfg),
            "missing": missing}


def _apply_paths(out, cloud_ok):
    """按「本地与云端任一跑通即算成功使用」重判级别（W 定的口径，2026-10-01）。

    原来把"没有本地可聊天模型"报成 fail 是**判错了目标**：一个只想接云端的人
    根本不需要引擎和权重。规则改成三条：
      · 本地能跑 → 照原样；
      · 本地不能跑但云端能跑 → 本地那 12 项一律降成"不适用"，并说清为什么，
        报告里它们挤在一起、不再占"需要处理"的名额；
      · 两条都不通 → 才在**最前面**加一条 fail 汇总，明细保持各自的原级。
    """
    by = {c["id"]: c for c in out}
    local_ok = by.get("server_exe", {}).get("level") == OK \
        and by.get("chat_models", {}).get("level") == OK
    if not local_ok and cloud_ok:
        for cid in LOCAL_ONLY:
            c = by.get(cid)
            if c and c["level"] in (WARN, FAIL):
                c["level"] = NA
                c["fact"] = "只用云端不需要这个 —— " + c["fact"]
    out.insert(0, _c("paths", GROUP_ENV, OK if (local_ok or cloud_ok) else FAIL,
                     "能不能跑通",
                     "本地：%s ｜ 云端：%s ｜ 只要通一条就能用"
                     % ("就绪" if local_ok else "未就绪",
                        "就绪" if cloud_ok else "未就绪"),
                     "" if (local_ok or cloud_ok) else
                     "想走本地：在 设置 → 本地模型 → 模型文件与引擎 打开下载页，模型放进模型目录；"
                     "想走云端：在 设置 → 云端模型 → 服务商与密钥 填密钥，再到「选择模型」里勾上要用那几个。"
                     "两条只要挑通一条。", ("settings", "关于与诊断 → 诊断")))
    return out


def run_checks(cfg, proxy_running=False, server_running=False, probe_gpu=True):
    """跑一遍。`proxy_running` / `server_running` 由界面传进来：
    8081 被**我们自己**占着是正常，不是故障，这条判断只能由调用方给。"""
    from .crashlog import log_path, tail
    out = []
    frozen = bool(getattr(sys, "frozen", False))
    app_dir = config.APP_DIR          # 现读，理由同 _config_state

    # ---------------- 运行环境 ----------------
    mode = "打包 exe" if frozen else "源码运行"
    ok, err = _writable(app_dir)
    out.append(_c("app_dir", GROUP_ENV, OK if ok else FAIL,
                  "程序目录能不能写（%s）" % mode,
                  "%s%s" % (app_dir, "" if ok else " → " + err),
                  "" if ok else "这个目录不允许写入，设置保存不下来。"
                                "把本程序整个文件夹移到一个可写的普通文件夹"
                                "（不要放在系统程序目录里）再启动。",
                  ("open_dir", app_dir)))

    if write_error()[0]:
        out.append(_c("last_write", GROUP_ENV, FAIL, "最近一次保存状态文件失败",
                      "%s（%s）" % (os.path.basename(write_error()[0]), write_error()[1]),
                      "同上：目录不可写或磁盘已满。修好后重启本程序。"))

    dt = _drive_type(app_dir)
    oned = "onedrive" in app_dir.lower()
    # GetDriveTypeW：2=可移动 3=**本地硬盘** 4=网络 5=光驱 6=内存盘（3 是正常，别当故障）
    if dt == 4 or oned:
        out.append(_c("location", GROUP_ENV, WARN, "程序放在同步盘或网络盘上",
                      ("网络驱动器" if dt == 4 else "") + ("OneDrive 目录" if oned else ""),
                      "这类目录会后台同步、偶尔锁文件，首次加载明显变慢还可能写不进配置。"
                      "建议移到本地磁盘的普通文件夹。"))
    if dt in (2, 5, 6):
        out.append(_c("media_type", GROUP_ENV, WARN, "程序所在介质不是本机硬盘",
                      {2: "可移动磁盘", 5: "光驱", 6: "内存盘"}.get(dt, ""),
                      "换设备或弹出后就找不到了；模型文件也会读得很慢。"))

    exe_path = (sys.executable if frozen else os.path.abspath(__file__))
    if _zone_marked(exe_path):
        out.append(_c("zone", GROUP_ENV, WARN, "文件带「来自网络」标记",
                      os.path.basename(exe_path),
                      "Windows 可能因此拦一次（SmartScreen /「已保护你的电脑」）。"
                      "右键本程序 → 属性 → 勾选「解除锁定」，或在 SmartScreen 点「更多信息 → 仍要运行」。"))

    try:
        wv = sys.getwindowsversion()
        if wv.major < 10:
            out.append(_c("winver", GROUP_ENV, WARN, "Windows 版本偏旧",
                          "%d.%d" % (wv.major, wv.minor),
                          "本程序按 Windows 10/11 设计与验证，旧系统上的界面与进程控制可能不正常。"))
        else:
            out.append(_c("winver", GROUP_ENV, OK, "Windows 版本", "%d.%d" % (wv.major, wv.minor)))
    except Exception:
        pass

    free = _free_gb(app_dir)
    out.append(_c("disk", GROUP_ENV, OK if free >= 20 else WARN, "本机磁盘剩余",
                  "%.1f GB" % free,
                  "" if free >= 20 else "模型文件动辄 10GB 以上，剩余空间偏少；"
                                        "下载大模型前先腾出空间。"))

    lp = log_path()
    if os.path.isfile(lp):
        out.append(_c("crashlog", GROUP_ENV, WARN, "留有过异常退出记录",
                      (tail(3) or "").strip().replace("\n", " / ")[:160],
                      "如果现在能正常用，这条可以忽略；「打开错误日志」能看全文。",
                      ("log",)))

    tier = _dpi_aware()
    out.append(_c("dpi", GROUP_ENV, OK if tier else WARN,
                  "高分屏清晰度（DPI 感知）",
                  ("%s —— 界面按显示器的真实像素画，文字清晰" % dpi_label(tier)) if tier else
                  "没开 —— Windows 会把整个窗口按系统缩放位图拉伸，"
                  "界面大一圈同时有点糊",
                  "" if tier else "在 设置 → 关于与诊断 勾上「高分屏清晰度」，"
                                  "然后重开程序（这项是冷切换，当前窗口不会变）。",
                  ("settings", "关于与诊断")))

    if not frozen:
        v = "%d.%d.%d" % sys.version_info[:3]
        out.append(_c("python", GROUP_ENV, OK if sys.version_info[:2] >= (3, 9) else FAIL,
                      "Python 版本（源码运行）", v,
                      "" if sys.version_info[:2] >= (3, 9) else "需要 Python 3.9 及以上。"))
        # core 层**不许 import tkinter**（分层规矩 + 零依赖打包的口径），所以这里只查模块在不在位，
        # 不去 import 它。真判"能不能建窗口"不需要这条：这份报告本身就画在 Tk 里，
        # 用户能看到它 = Tk 起得来；Tk 版本在 设置 → 关于与诊断 →「诊断」那页显示。
        try:
            import importlib.util
            has_tk = importlib.util.find_spec("tkinter") is not None
        except Exception:
            has_tk = False
        out.append(_c("tkinter", GROUP_ENV, OK if has_tk else FAIL, "Tkinter 模块",
                      "在位" if has_tk else "找不到 tkinter 模块",
                      "" if has_tk else "换 python.org 的完整安装包，或装上系统发行版提供的 tkinter 包。"))
    else:
        out.append(_c("python", GROUP_ENV, NA, "Python / Tkinter", "打包 exe 用自带运行库，不适用"))

    # ---------------- 推理引擎 ----------------
    exe = str(cfg.get("exe", "") or "")
    if not exe:
        out.append(_c("server_exe", GROUP_ENGINE, FAIL, "没有指定推理引擎",
                      "设置里的「引擎程序」是空的",
                      "去 设置 → 本地模型 → 服务参数 里指向 llama-server.exe。",
                      ("settings", "本地模型 → 服务参数")))
    elif not os.path.isfile(exe):
        out.append(_c("server_exe", GROUP_ENGINE, FAIL, "找不到推理引擎 llama-server.exe",
                      exe,
                      "引擎不随本程序分发（许可与体积原因）。在 设置 → 本地模型 → 模型文件与引擎 "
                      "打开 llama.cpp 的下载页，取 Windows 的 CUDA 包，整包解压到本程序同目录。",
                      ("open_dir", app_dir)))
    else:
        out.append(_c("server_exe", GROUP_ENGINE, OK, "推理引擎已就位", exe))
        try:
            has_cudart = any(f.lower().startswith("cudart64")
                             for f in os.listdir(os.path.dirname(exe) or "."))
        except Exception:
            has_cudart = True
        if not has_cudart:
            out.append(_c("cudart", GROUP_ENGINE, WARN, "引擎目录里缺 CUDA 运行库",
                          "没找到 cudart64_*.dll",
                          "服务会启动即退出。用 llama.cpp 官方的 CUDA 12 发行包"
                          "（它自带 cudart），或把 cudart 的 dll 复制进引擎目录。"))

    name = str(cfg.get("gpu_name", "") or "")
    vram = float(cfg.get("vram_gb", 0) or 0)
    if not name or not vram:
        # nvidia-smi 最坏会等满 10 秒：首跑引导那条路上 probe_gpu=False，
        # 显卡信息缺失就让下面那条检查报"没检测到"，别把界面冻在那儿（坑 4）
        name2, vram2 = hardware.detect_gpu() if probe_gpu else ("", 0)
        if name2:
            # **只填进传进来的这份 cfg，不落盘**：诊断承诺过"只读"，而调用方给的往往是
            # 点击那一刻的快照 —— 在这里 save_config(快照) 会把这 10 秒里用户自己保存的
            # 设置整份盖回去（P1 数据丢失）。要记下来由主线程合并回活的 cfg。
            name, vram = name2, vram2
            cfg["gpu_name"], cfg["vram_gb"] = name, vram
    if name:
        out.append(_c("gpu", GROUP_ENGINE, OK if vram >= 6 else WARN, "显卡",
                      "%s · %.1f GB" % (name, vram),
                      "" if vram >= 6 else "显存偏小：大模型会主要落在内存上，速度明显变慢。"
                                           "建议选 8GB 以内的量化模型。"))
    else:
        out.append(_c("gpu", GROUP_ENGINE, WARN, "没有检测到 NVIDIA 显卡",
                      "nvidia-smi 没给出结果",
                      "没有 N 卡也能跑（全在 CPU/内存上，很慢）。有 N 卡却读不到，通常是驱动没装好"
                      "或装的是精简驱动 —— 更新一次驱动再重启本程序。"))
    out.append(_c("ram", GROUP_ENGINE, OK, "物理内存",
                  "%.1f GB" % (cfg.get("ram_gb") or hardware.detect_ram_gb())))

    # ---------------- 模型 ----------------
    mdir = str(cfg.get("models_dir", "") or "")
    if not mdir or not os.path.isdir(mdir):
        out.append(_c("models_dir", GROUP_MODEL, FAIL, "模型目录不存在", mdir or "（未填）",
                      "把你下载的 .gguf 模型放进这个文件夹，或在 设置 → 本地模型 → 模型文件与引擎 里指到"
                      "它们实际所在的位置。", ("settings", "本地模型 → 模型文件与引擎")))
        _dir, chat, img = "", [], []
    else:
        _dir, chat, img = models.scan_models(cfg)
        out.append(_c("models_dir", GROUP_MODEL, OK, "模型目录", mdir))

    if not mdir or not os.path.isdir(mdir):
        pass
    elif chat:
        out.append(_c("chat_models", GROUP_MODEL, OK, "可以聊天的模型",
                     "%d 个（当前：%s）" % (len(chat),
                                          os.path.basename(str(cfg.get("model", ""))) or "未选")))
    else:
        # 这句"目录里有 N 个文件"要列目录，而列目录本身可能炸（权限、网络盘断了、
        # 路径里有非法字符）—— 不能因为一句补充说明把整份诊断带走（坑 117 同族）
        try:
            _n_gguf = len([f for f in os.listdir(mdir) if f.lower().endswith(".gguf")])
            _fact = "目录里有 %d 个文件，但都不是能对话的语言模型" % _n_gguf
        except Exception:
            _fact = "模型目录读不动，也判不出里面有什么"
        out.append(_c("chat_models", GROUP_MODEL, FAIL, "没找到可以聊天的模型",
                      _fact,
                      "需要 GGUF 格式、且带模型元数据的语言模型（llama.cpp 的下载页会标 "
                      "\"universal / chat\"）。生图模型与视频组件不算，它们不会出现在聊天菜单里。",
                      ("open_dir", mdir)))

    cur = str(cfg.get("model", "") or "")
    if cur and not os.path.isfile(cur):
        out.append(_c("selected", GROUP_MODEL, WARN, "选中的模型文件不在原位", cur,
                      "被移动或删掉了。在顶部模型菜单里重选一个，或把它放回原处。"))

    # 档位与看图：按**模型库整体**报，不看"菜单当前选中哪个"。
    # 诊断回答的是"这台机器带不带得动"，跟着选中项变来变去就看不出真问题；
    # 而且 compute_ngl / estimate_kv_gb 返回的是**字典**（{ngl,ngl_max,n_layers,...}），
    # 当数字用会直接 TypeError —— W 的真实配置炸过一次，这里按契约取键、取不到就跳过。
    if chat:
        try:
            pairs = models.find_vl_pairs(cfg) or {}
        except Exception:
            pairs = {}
        in_gpu, cpu_only, with_vision = 0, [], 0
        for p in chat:
            try:
                r = params.compute_ngl(cfg, p, vram or 8.0)
            except Exception:
                r = None
            n = r.get("ngl") if isinstance(r, dict) else None
            if isinstance(n, int) and n > 0:
                in_gpu += 1
            elif isinstance(n, int):
                cpu_only.append(os.path.basename(p))
            if p in pairs:
                with_vision += 1
        lv = OK if in_gpu else (WARN if cpu_only else NA)
        out.append(_c("budget", GROUP_MODEL, lv, "这些模型带得动吗（%.0f GB 显存）" % (vram or 0),
                      "%d / %d 个能进显卡%s" % (in_gpu, len(chat),
                        ("；只能跑 CPU 的：" + "、".join(cpu_only[:3])) if cpu_only else ""),
                      "" if in_gpu else "这些模型在这块显卡上一层都放不进去，会全部走 CPU / 内存，"
                                        "速度通常是个位数每秒。换更小的量化（文件更小）会快很多。"))
        out.append(_c("vision", GROUP_MODEL, OK if with_vision else NA, "有多少模型能看图",
                      "%d 个配了视觉投影器 mmproj" % with_vision
                      if with_vision else "一个都没配 mmproj（只能文字对话）",
                      ""))

    # ---------------- 本地生图 / 生视频 ----------------
    sd = str(cfg.get("sd_dir", "") or "")
    cli = os.path.join(sd, "sd-cli.exe") if sd else ""
    if not sd or not os.path.isfile(cli):
        out.append(_c("sd_cli", GROUP_MEDIA, NA, "本地生图 / 生视频引擎",
                      "没装 stable-diffusion.cpp（只有想用本地生图或生视频时才需要）",
                      "要用的话在 设置 → 本地模型 → 模型文件与引擎 打开 sd.cpp 的下载页，"
                      "整包解压后到 设置 → 本地模型 → 生图 里指路。",
                      ("settings", "本地模型 → 模型文件与引擎")))
    else:
        out.append(_c("sd_cli", GROUP_MEDIA, OK, "本地生图 / 生视频引擎", cli))
    _d2, _c2, img2 = ("", [], [])
    try:
        _d2, _c2, img2 = models.scan_models(cfg)
    except Exception:
        pass
    if img2:
        out.append(_c("img_models", GROUP_MEDIA, OK, "本地生图模型", "%d 个" % len(img2)))
    vid = []
    try:
        vid, _enc = models.scan_video_models(cfg)     # 返回 (主体, 编码器) 两段
    except Exception:
        pass
    if vid:
        out.append(_c("vid_models", GROUP_MEDIA, OK, "本地生视频主体", "%d 个" % len(vid)))

    # ---------------- 端口 ----------------
    p1 = int(cfg.get("port", 8080) or 8080)
    busy1 = _port_busy(p1)
    if not busy1:
        out.append(_c("port_server", GROUP_PORT, OK, "服务端口 %d" % p1, "空闲"))
    elif server_running:
        out.append(_c("port_server", GROUP_PORT, OK, "服务端口 %d" % p1,
                      "被正在运行的 llama-server 占着（这是本程序自己起的，正常）"))
    else:
        out.append(_c("port_server", GROUP_PORT, WARN, "服务端口 %d 已被别的程序占用" % p1,
                      "占用者不是本程序启动的服务",
                      "关掉那个程序，或在 设置 → 本地模型 → 服务参数 里换一个端口。",
                      ("settings", "本地模型 → 服务参数")))

    p2 = int(cfg.get("proxy_port", 8081) or 8081)
    busy2 = _port_busy(p2)
    if not busy2:
        out.append(_c("port_proxy", GROUP_PORT, OK, "代理端口 %d" % p2, "空闲"))
    elif proxy_running:
        out.append(_c("port_proxy", GROUP_PORT, OK, "代理端口 %d" % p2,
                      "本程序自己的 agent 接口在用（正常）"))
    else:
        out.append(_c("port_proxy", GROUP_PORT, WARN, "代理端口 %d 已被别的程序占用" % p2,
                      "本程序没在监听这个端口",
                      "agent 接入会失败。在 设置 → API 连接 里换一个端口并重启代理。",
                      ("settings", "API 连接")))

    # ---------------- 配置与云端 ----------------
    st, detail = _config_state()
    out.append(_c("config", GROUP_STATE, {"ok": OK, "none": OK, "broken": FAIL}[st],
                  "配置文件" + ("读坏了（会退回默认值）" if st == "broken" else "正常"),
                  detail,
                  "" if st != "broken" else "这份文件解析不了，程序现在用的是默认值，"
                                            "所以你会看到设置「全没了」。同目录若留有 .1 或备份可找回；"
                                            "否则删掉它让程序重建一份。"))
    try:
        sec = (secrets.load_secrets() or {}).get("api_keys") or {}
        n = len([k for k, v in sec.items() if v])
        out.append(_c("secrets", GROUP_CLOUD, OK, "云端密钥",
                      "%d 家服务商填过密钥（密钥只存在这台机器上，不会被上传）" % n if n else
                      "一把都没填 —— 只想用本地模型的话不用管"))
    except Exception as e:
        out.append(_c("secrets", GROUP_CLOUD, WARN, "云端密钥文件读不出来", str(e),
                      "删掉 secrets.json 会丢全部云端密钥，先确认它是不是被安全软件拦了。"))
    return _apply_paths(out, _cloud_ready(cfg))


def summary(checks):
    f = len([c for c in checks if c["level"] == FAIL])
    w = len([c for c in checks if c["level"] == WARN])
    return f, w


def render_text(checks, meta=None):
    """给人看的报告（贴给别人求助 / 存档）。纯文本，不夹任何颜色标记。

    **只展开需要处理的**：十几条"正常"堆在一起，真正那条提醒就看不见了
    （W 的反馈：要精简整齐）。正常项压成最后一行计数；不适用项照列 ——
    "为什么不适用"本身就是信息（本地没装但走云端 = 正常状态，不是故障）。
    """
    lines = []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines.append("LLM Chat 环境诊断 · %s" % stamp)
    for k, v in (meta or {}).items():
        lines.append("  %s：%s" % (k, v))
    paths = next((c for c in checks if c["id"] == "paths"), None)
    if paths:
        lines.append("  %s：%s" % (paths["title"], paths["fact"]))
    f, w = summary(checks)
    ok = len([c for c in checks if c["level"] == OK])
    lines.append("  结论：%s" % (
        "两条路都还没通，先按下面补一样" if f
        else ("能跑通。%s" % ("%d 项提醒" % w if w else "没有提醒"))))
    lines.append("")
    show = [c for c in checks if c["level"] != OK and c["id"] != "paths"]
    if not show:
        lines.append("  全部 %d 项检查正常。" % (ok - 1))
        return "\n".join(lines) + "\n"
    mark = {OK: "[正常]", WARN: "[提醒]", FAIL: "[需处理]", NA: "[不适用]"}
    group = ""
    for c in show:
        if c["group"] != group:
            group = c["group"]
            lines.append("— %s —" % group)
        lines.append("  %s %s：%s" % (mark.get(c["level"], "[?]"), c["title"], c["fact"]))
        if c["fix"] and c["level"] != NA:
            lines.append("      怎么办：%s" % c["fix"])
    if ok:
        lines.append("")
        lines.append("  其余 %d 项检查正常，未列出。" % (ok - 1))
    return "\n".join(lines) + "\n"
