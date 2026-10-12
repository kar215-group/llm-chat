# -*- coding: utf-8 -*-
"""llm_console.connection.remote_jobs — 远程生图任务管理器（2026-10-12 模块化）。

单槽：**同一时间全机只跑一份 sd-cli**。与 GUI 的互斥是**双向**的（现查结论，
见 `15` §1 2026-10-11 行）：提交前问 `local_busy()`（`App._any_busy`），
`App._any_busy` 反向查 `busy()` —— 两份 sd-cli 抢 8GB 显存比排队糟。

预检与命令组装**逐字复用 GUI 链路的判据**（`resolve_modes → plan_ref_images →
edit_strength → build_img_cmd`），回显行与 argv 同源；"必闪退"档远程**直接拒**
（没人可按「仍要继续」）。

**给手机端的每一句话都不带本机路径**（坑 209⑥）：引擎原始输出只进本机日志与审计。
"""

import datetime as _datetime
import os
import secrets as _pysecrets
import subprocess
import threading
import time

from ..core import config
from .remote_audit import audit
from .remote_ledger import ledger_add, ledger_load, ledger_update
from .remote_uploads import upload_paths

# ---- 给手机端的失败原话（W 2026-10-12 定口径：**只给人话，引擎原始行不留**）----
# sd-cli 的输出里带着模型与产物的**绝对路径**（盘符、用户名、目录结构），而坑 209⑥
# 早就定了"不把本机路径回给手机端"——`submit` 里"引擎没就位"那句就是照这个写的。
# 原始行并没有丢：整份都进本机日志（media_log_path）与审计 detail，本机想看随时能看。
FATAL_MSG = "电脑端的生图引擎没出图（退出码 %s）。详细输出留在电脑端的生图日志里。"
READ_FAIL_MSG = "电脑端读不到生图引擎的输出了。详细输出留在电脑端的生图日志里。"
SPAWN_FAIL_MSG = "电脑端的生图引擎起不来（sd-cli 没能启动）。请在电脑端确认引擎目录还在。"

class RemoteJobs:
    """远程生图的单槽任务管理器：提交 / 进度 / 取消 / 产物取回。

    **与 GUI 的互斥是双向的**（现查结论，见 15 §1 2026-10-11 行）：
      · 提交前问 `local_busy()`（App._any_busy：GUI 的生成 / 服务操作进行中就拒）；
      · App._any_busy 反向纳入 `self.busy()`（远程任务在跑，GUI 也被拦住）——
        两份 sd-cli 并发抢 8GB 显存 + 单槽共享状态互相覆盖，都比"排队"糟。
    预检与命令组装**逐字复用 GUI 链路的判据**（resolve_img_model_path /
    resolve_img_files / missing_slots / build_img_cmd），产物名加微秒与任务短号
    防同秒碰撞。"必闪退"档（modelreq 3 级）远程**直接拒绝**——本地 GUI 有人弹窗
    确认，远程没人可问。
    """

    def __init__(self, cfg, local_busy=None, note_fn=None):
        self.cfg = cfg
        self._local_busy = local_busy or (lambda: False)
        self.note = note_fn or (lambda msg: None)
        self._lock = threading.RLock()
        self._proc = None
        self._stop_flag = None
        self._task = None            # 运行中任务的内存态（进度 / tail）

    # ---- 状态 ----

    def busy(self):
        # **不抢锁**：`submit` 全程持 `_lock`（预检 + 拼命令 + Popen），而电脑端主线程
        # 经 `App._any_busy()` 问的正是这个函数 —— 抢同一把锁就变成"手机一提交、
        # 电脑界面卡到 Popen 返回"（磁盘慢时是秒级）。读一个引用是原子的，这里要的
        # 只是个提示位；真正串行化的占槽判定仍在 submit 的锁内。
        return self._proc is not None

    def running_task(self):
        with self._lock:
            return dict(self._task) if self._task else None

    # ---- 提交 ----

    def submit(self, prompt, device_id, refs=None, ref_mode="",
               strength_tier="", mask=""):
        """提交一个生图任务 → (task_id, 错误文案)。错误非空 = 没提交。

        二期入参（2026-10-11，W 拍板）：`refs` = 已上传附件的登记名列表、
        `ref_mode` = 带图两档（edit / subject，空 = 该模型默认档）、
        `strength_tier` = 重绘强度档（空 = 交给提示词自动分档）、
        `mask` = 遮罩的登记名（只跟底图重绘档走）。预检顺序与判据**逐字复用
        GUI 链路**（ui/image_gen._start_chat_image：resolve_modes → 手选档
        校验 → default_ref_mode → plan_ref_images → edit_strength），回显行
        与命令行同源（同一条"说出去的强度就是引擎收到的强度"纪律）。
        """
        prompt = str(prompt or "").strip()
        if not prompt:
            return None, "提示词不能为空。"
        if len(prompt) > 4000:
            return None, "提示词太长（上限 4000 字）。"
        with self._lock:
            if self._proc is not None:
                return None, "已有一个远程生图在进行（本机同一时间只跑一个）。"
            if self._local_busy():
                return None, "本机正在忙（生成 / 生视频 / 服务操作），稍后再试。"
            from ..core.config import cfg_lock
            from ..core import capability, modelreq, providers, sdprofile
            from ..core.media import (build_img_cmd, edit_strength, img_out_dir,
                                      mask_hint, plan_ref_images,
                                      resolve_img_files, resolve_img_model_path)
            with cfg_lock():
                snap = dict(self.cfg)          # 提交点做一次快照（坑 194 口径）
            # 远程只走本地链路：GUI 停在云端模型上时 resolve_modes 会拐进云端
            # 分支（判成服务商的档）——快照里钉回 local，判据才是本机 sd-cli 的。
            snap["model_provider"] = providers.LOCAL
            sd = str(snap.get("sd_dir", "") or "")
            cli = os.path.join(sd, "sd-cli.exe")
            if not os.path.isfile(cli):
                # 不把引擎的绝对路径回给手机端（坑 209⑥：那是本机用户名与目录结构）；
                # 路径进审计，手机那头只留"去哪儿修"。
                audit("image.submit", device_id, False, "no sd-cli: %s" % (cli or "(空)"))
                return None, ("电脑端还没配好生图引擎：在 设置 → 本地模型 → "
                              "生图（sd.cpp） 里指向 sd.cpp 目录。")
            diffusion = resolve_img_model_path(snap)
            if not diffusion:
                return None, ("未找到生图模型：电脑端还没配好扩散权重。")
            files = resolve_img_files(snap, diffusion)
            missing = sdprofile.missing_slots(files["family"], files)
            if missing:
                return None, ("这个生图模型（识别为 %s）缺配套文件：%s——在电脑端"
                              "设置里补齐。" % (sdprofile.label_of(files["family"]),
                                               "、".join(missing)))
            try:
                need = modelreq.assess(snap, diffusion, "image", files=files)
                lvl = modelreq.grade(need, snap.get("vram_gb") or 0,
                                     snap.get("ram_gb") or 0) if need else 0
            except Exception:
                lvl = 0
            if lvl >= modelreq.LEVEL_FATAL:
                return None, ("这台机器带不动这个模型（%s）——远程没人能按"
                              "「仍要继续」，请在电脑端确认后再来。"
                              % modelreq.LEVEL_TEXT[lvl])
            # ---- 二期：附件 → 带图两档 → 强度 → 遮罩（判据与 GUI 逐字同源）----
            ref_paths, err = upload_paths(device_id, refs or [], "ref")
            if err:
                return None, err
            mask_path = ""
            if str(mask or ""):
                mp, err = upload_paths(device_id, [mask], "mask")
                if err:
                    return None, err
                mask_path = mp[0] if mp else ""
            modes = capability.resolve_modes(snap, files["family"])
            mode = str(ref_mode or "").strip()
            if ref_paths and mode and not modes.get(mode, {}).get("ok"):
                return None, str(modes[mode]["why"])   # 不支持的档就地拦，一个字都不丢
            mode = mode or capability.default_ref_mode(snap, files["family"])
            used, dropped, drop_note = plan_ref_images(ref_paths, mode)
            if mode and mode != capability.MODE_EDIT:
                mask_path = ""           # 主体参考档（-r）不吃遮罩（10 §5.0.4）
            if not used:
                mode = ""                # 纯文生图不带模式（GUI 同口径）
                mask_path = ""
            st_value = None              # 底图重绘档才算强度；None = build_img_cmd 自己判
            echo = []
            if used:
                echo.append("[%s] 参考图 %d 张%s"
                            % (capability.MODE_LABEL.get(mode, "带图"), len(used),
                               "" if not drop_note else "（%s）" % drop_note))
                if mode == capability.MODE_EDIT:
                    # 回显与命令行同源：这里算一次，数值显式交给 build_img_cmd
                    _t, st_value, st_why = edit_strength(
                        snap, prompt, tier=str(strength_tier or ""))
                    # 小改档 + 没带遮罩 → 半句指路（判据在 media.mask_hint，与 GUI 同源）
                    _hint = mask_hint(_t, auto=not str(strength_tier or "").strip(),
                                      has_mask=bool(mask_path))
                    echo.append("重绘强度 %s（%s）%s%s"
                                % (st_value, st_why,
                                   "｜遮罩：只重绘涂抹区" if mask_path else "",
                                   _hint))
            try:
                steps = max(1, int(snap.get("img_steps", 12) or 12))
            except Exception:
                steps = 12
            size = str(snap.get("img_size", "1024x1024"))
            try:
                cfg_scale = float(snap.get("img_cfg", 2.5) or 2.5)
            except Exception:
                cfg_scale = 2.5
            try:
                seed = int(snap.get("img_seed", -1))
            except Exception:
                seed = -1
            outdir = img_out_dir(snap)
            try:
                os.makedirs(outdir, exist_ok=True)
            except Exception as e:
                audit("image.submit", device_id, False, "outdir failed: %s (%s: %s)"
                      % (outdir, type(e).__name__, e))
                return None, ("电脑端的产物目录建不出来：检查那个目录的磁盘与权限"
                              "（位置在 设置 → 云端模型 → 生图 / 生视频 的「修改产物位置」）。")
            tid = _pysecrets.token_hex(4)
            # 微秒防同秒碰撞：**不能**走 time.strftime("%f") —— Windows 的 C 运行时
            # strftime 不认 %f（glibc 扩展），当场 ValueError；datetime 是 Python
            # 自己实现的，各平台都行。
            out = os.path.join(outdir, "img_r%s_%s_%s.png" % (
                tid, time.strftime("%Y%m%d_%H%M%S"),
                _datetime.datetime.now().strftime("%f")))
            log_path = config.media_log_path(snap, out)
            cmd = build_img_cmd(snap, prompt, out, steps, size, diffusion,
                                cfg_scale, seed, files=files,
                                ref_mode=mode, ref_images=used,
                                strength=st_value, mask=mask_path)
            try:
                with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
                    lf.write("=== argv（远程 %s）===\n" % device_id)
                    for i, a in enumerate(cmd):
                        lf.write("[%2d] %s\n" % (i, a))
                    lf.write("=== cwd=%s ｜ out=%s ｜ 提交于 %s ===\n\n"
                             % (sd, out, time.strftime("%Y-%m-%d %H:%M:%S")))
            except Exception:
                log_path = ""
            flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace",
                                        creationflags=flags if os.name == "nt" else 0,
                                        cwd=sd)
            except Exception as e:
                # 异常文本里带着 sd-cli 的绝对路径：原话进审计，手机端留人话
                audit("image.submit", device_id, False,
                      "spawn failed: %s: %s" % (type(e).__name__, e))
                return None, SPAWN_FAIL_MSG
            self._proc = proc
            self._stop_flag = threading.Event()
            self._task = {"id": tid, "device_id": device_id,
                          "prompt": prompt[:200], "state": "running",
                          "progress": None, "tail": [], "files": [],
                          "error": "", "out": out, "t0": time.time(),
                          "echo": echo}
            ledger_add({"id": tid, "device_id": device_id,
                         "name": os.path.basename(out), "path": out,
                         "done": False, "ok": False,
                         "submitted": time.strftime("%Y-%m-%d %H:%M:%S")})
            threading.Thread(target=self._reader, daemon=True,
                             name="remote-img").start()
            audit("image.submit", device_id, True, tid)
            return tid, ""

    # ---- 执行（无 GUI 版 reader，判据与 ui/image_gen 同源）----

    def _reader(self):
        import re
        proc = self._proc
        task = self._task
        stop = self._stop_flag
        prog_rx = re.compile(r"(\d+)/(\d+) - ")
        tail = []
        log = config.media_log_path(self.cfg, task["out"])
        try:
            for line in proc.stdout:
                line = (line or "").rstrip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-40]                       # 保尾 40 行（失败原因在这）
                m = prog_rx.search(line)
                if m:
                    task["progress"] = [int(m.group(1)), int(m.group(2))]
                if log:
                    try:
                        with open(log, "a", encoding="utf-8",
                                  errors="replace") as lf:
                            lf.write(line + "\n")
                    except Exception:
                        pass
                if stop.is_set():
                    proc.kill()
                    break
            rc = proc.wait()
        except Exception as e:
            rc = -1
            # 原话只进审计（异常文本可能带路径），手机端留人话
            audit("image.read", task.get("device_id", "?"), False,
                  "%s: %s" % (type(e).__name__, e))
            task["error"] = READ_FAIL_MSG
        with self._lock:
            ok = (rc == 0 and os.path.isfile(task["out"])
                  and not stop.is_set())
            task["state"] = "done" if ok else (
                "cancelled" if stop.is_set() else "failed")
            if not ok and rc != 0 and not task["error"]:
                task["error"] = FATAL_MSG % rc
            task["tail"] = tail[-6:]      # 只给本机看（status() 不外发，见 P0-2）
            task["files"] = ([os.path.basename(task["out"])]
                             if os.path.isfile(task["out"]) else [])
            # state 一起进台账（P1-7）：原先只能从 done/ok 反推，于是"已停止"
            # 在手机端被报成"失败"。台账加键不动形状，旧条目照读。
            ledger_update(task["id"], done=True, ok=ok, state=task["state"],
                          files=task["files"])
            # 收尾写完**再**释放单槽：反过来的话会有一瞬"槽已空、台账还是
            # done=False"（busy() 不抢锁，手机端就在这一瞬提交下一条）
            self._proc = None
        # 引擎末尾行进审计（本机的痕迹），手机端只留上面那句人话
        audit("image.exit", task.get("device_id", "?"), ok,
              "rc=%s%s" % (rc, "" if ok else "｜" + " ｜ ".join(tail[-3:])))

    def cancel(self, task_id=None, device_id=None):
        with self._lock:
            if self._proc is None:
                return False
            if (task_id and self._task and self._task["id"] != task_id):
                return False
            if (device_id and device_id != "local" and self._task
                    and self._task.get("device_id") != device_id):
                return False
            proc = self._proc
            if self._stop_flag:
                self._stop_flag.set()
        # **当场 kill**，不能只置 stop_flag 等 reader 去杀：reader 的 `proc.kill()`
        # 写在 `for line in proc.stdout` 循环里，引擎不吐字（加载十几 GB 权重那几十秒、
        # 或卡住）就一直等不到下一行 —— 「停止」按了没反应，单槽永久 busy，
        # 电脑端也被 `_any_busy` 一起拦死。GUI 那条链路（_cancel_chat_image）就是直接杀的。
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        audit("image.cancel", device_id or "?", True, task_id or "")
        return True

    # ---- 查询 / 取回 ----

    def _history_ok(self):
        """历史产物开关（cfg["remote_history"]，默认关）：开启后取回判据从
        "本设备自己的任务"放宽到台账全量（16 §4 第 3 条预留的口径——只放宽
        判据，台账形状与"白名单落点"都不动）。"""
        try:
            return bool(self.cfg.get("remote_history", False))
        except Exception:
            return False

    def _may_see(self, owner, device_id):
        """归属判据**唯一一处**：谁的任务能被谁看见。

        loopback（`device_id == "local"`）全可见；其余默认只看自己提交的，
        `remote_history` 开着才放宽到台账全量（16 §4 第 3 条预留的口径 ——
        只放宽判据，台账形状与"白名单落点"都不动）。
        status / artifact / history 三条路径都问它，不在入口里各写一遍（坑 128 家族）。
        """
        if device_id == "local" or self._history_ok():
            return True
        return owner == device_id

    @staticmethod
    def _from_ledger(t2):
        """台账条目 → 任务形状（P1-7：状态优先读 `state`，旧条目按 done/ok 反推）。"""
        return {"id": t2.get("id"),
                "state": t2.get("state") or ("done" if t2.get("ok") else "failed"),
                "progress": None, "files": t2.get("files") or [],
                "error": "" if t2.get("ok") else "已结束（详见电脑端 media_logs）",
                "echo": [], "device_id": t2.get("device_id")}

    def status(self, task_id, device_id):
        """手机端 `/api/task` 的回包。

        **不含 tail**（P0-2）：引擎原始行带着本机绝对路径，只进本机日志与审计；
        页面上那行"末尾输出"因此自然空掉，前端零改动。

        当前 / 上一个任务走**无锁快路径**（B3）：`submit` 全程持 `_lock` 做磁盘
        扫描 + Popen，而运行中任务的轮询最频繁——抢同一把锁会让手机端的进度
        查询堵到 submit 让锁。读引用 + `dict()` 拷贝在 GIL 下原子，各字段都是
        整体替换不是原地改（与 `busy()` 同款理由）。台账路径仍持锁。
        """
        t = self._task
        if t is not None and t["id"] == task_id:
            if not self._may_see(t["device_id"], device_id):
                return None
            cur = dict(t)
        else:
            with self._lock:
                cur = None
                for t2 in ledger_load():
                    if t2.get("id") == task_id:
                        cur = self._from_ledger(t2)
                        break
                if cur is None or not self._may_see(cur.get("device_id"),
                                                    device_id):
                    return None
        return {"id": cur["id"], "state": cur.get("state", "failed"),
                "progress": cur.get("progress"), "files": cur.get("files") or [],
                "error": cur.get("error", ""), "echo": cur.get("echo") or []}

    def history(self, device_id, limit=20):
        """「之前的成果」：台账里**成功产物**的清单（新→旧）。

        只回"能不能取 + 取回要的参数"，**绝不回 path**（那是本机盘符与目录结构，
        坑 209⑥）。开关的用处到这里才真正落地：`remote_history` 关 = 这台设备
        自己的历史，开 = 台账全量。
        """
        out = []
        for t2 in reversed(ledger_load()):
            if not (t2.get("done") and t2.get("ok")) or not (t2.get("files") or []):
                continue
            if not self._may_see(t2.get("device_id"), device_id):
                continue
            out.append({"task": t2.get("id"), "name": t2["files"][0],
                        "submitted": t2.get("submitted", "")})
            if len(out) >= limit:
                break
        return out

    def artifact_path(self, task_id, device_id, name):
        """产物取回的白名单判据：任务属于该设备（loopback 全可见）+ 文件名是
        纯 basename + 路径就在台账登记的那份（台账是我们写的，路径逃逸在源头
        就不存在；这里再做一层双层校验，坑 209 纪律）。"""
        if not name or os.path.basename(name) != name or name in (".", ".."):
            return None
        with self._lock:
            if self._task and self._task["id"] == task_id:
                if not self._may_see(self._task["device_id"], device_id):
                    return None
                for f in self._task["files"]:
                    if f == name:
                        p = os.path.join(os.path.dirname(self._task["out"]), name)
                        return p if os.path.isfile(p) else None
                return None
        for t in ledger_load():
            if t.get("id") == task_id and t.get("done") and t.get("ok"):
                if not self._may_see(t.get("device_id"), device_id):
                    return None
                if name in (t.get("files") or []):
                    p = os.path.join(os.path.dirname(str(t.get("path", ""))), name)
                    base = os.path.realpath(os.path.dirname(str(t.get("path", ""))))
                    real = os.path.realpath(p)
                    if real == p and os.path.dirname(real) == base \
                            and os.path.isfile(p):
                        return p
                    return None
        return None
