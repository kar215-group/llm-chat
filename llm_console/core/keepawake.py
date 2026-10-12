"""阻止系统自动睡眠（「允许锁屏运行」的核心，2026-10-11 远程连接功能一期）。

**锁屏 ≠ 睡眠**：TCP 监听与后台线程在锁屏下照常工作，真正断掉远程连接的是
系统睡眠（S3 / Modern Standby）。所以本模块做的事不是"保持亮屏"，而是
阻止系统睡眠 —— 用 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`，
**不加** `ES_DISPLAY_REQUIRED`（屏幕照常熄灭，省电、护屏）。

三个要点（实现与测试都围着它们转）：
  1. 该调用**只对调用线程生效** → 本模块持一个常驻线程周期性"续约"，
     同时天然具备"配置改了自动跟随"的能力（每个 tick 重新问一次 state_fn）；
  2. **不需要管理员权限**，资源消耗 = 一个 15 秒醒一次的定时线程；
  3. 程序退出必须**显式清掉**（`ES_CONTINUOUS` 单独调用 = 清除），否则
     睡眠抑制在本进程死后残留到系统重新评估为止。

「仅接电源时保持唤醒」由 `on_ac()` 支撑：调用方（App）把
`keepawake_enabled` / `keepawake_ac_only` 两个配置键合成一个 state_fn 传进来，
本模块不读配置、不碰界面（铁律 1：core 不 import tkinter）。

判不了的情况一律放行（AC 判不了 = 当接了电源；执行调用失败 = 记下来下个
tick 重试）—— 宁可多保持一会儿唤醒，不做静默的反向伤害。
"""

import os
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# 续约周期：足够短（配置改动很快生效），又足够省（一个 15s 的定时等待）。
TICK_SECONDS = 15.0


def _real_exec(flags):
    """SetThreadExecutionState 的真实实现（测试经 `_exec` 参数打桩替换，坑 34 纪律）。

    返回值：成功 = 上一次的执行状态（非零），失败 = 0。这里归一成 bool。
    """
    if os.name != "nt":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(
            ctypes.c_uint32(flags)))
    except Exception:
        return False


def on_ac(_ps=None):
    """现在接着电源吗？`ACLineStatus`：0 = 用电池，1 = 接电源，255 = 判不了。

    判不了（非 Windows / 调用失败 / 255）一律返回 True —— 本开关的语义是
    "仅接电源时**才**保持唤醒"，判错成"没接电源"会让桌面机（往往也报 255）
    永远不保持唤醒，那是有害方向。测试经 `_ps` 注入假结构。
    """
    if os.name != "nt":
        return True
    import ctypes

    try:
        if _ps is None:
            class _PS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_uint32),
                    ("BatteryFullLifeTime", ctypes.c_uint32),
                ]

            _ps = _PS()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(
                    ctypes.byref(_ps)):
                return True
        return int(_ps.ACLineStatus) != 0
    except Exception:
        return True


class KeepAwake:
    """睡眠抑制持有者：常驻线程按 `state_fn()` 的当前意愿周期性续约。

    `state_fn()` 每个.tick 调一次（在后台线程，**绝不许读 Tk 变量**——
    App 侧传进来的是纯 cfg 判据），返回 True = 此刻要保持唤醒。
    """

    def __init__(self, state_fn, interval=TICK_SECONDS, _exec=None):
        self._state_fn = state_fn
        self._interval = max(0.05, float(interval))
        self._exec = _exec or _real_exec
        self._thread = None
        self._stop_evt = threading.Event()
        self._wake_evt = threading.Event()
        self._active = False          # 最近一次实际生效（want 且调用成功）
        self._last_error = ""         # 最近一次调用失败的说明（诊断用，人话）

    # ---- 生命周期 ----

    def start(self):
        """启动常驻线程（幂等：已在跑就只触发一次立即重估）。"""
        if self._thread is not None and self._thread.is_alive():
            self.refresh()
            return
        self._stop_evt.clear()
        self._wake_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="keepawake")
        self._thread.start()

    def stop(self):
        """停线程并**当场清一次**睡眠抑制（清动作在调用线程做，保证执行）。

        join 有超时：线程卡在等 tick 时唤醒事件会先把它放出来；超时也不再等
        （daemon 线程随进程退出），清除调用本身无论如何都要做。
        """
        self._stop_evt.set()
        self._wake_evt.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        self._active = False
        self._exec(ES_CONTINUOUS)

    def refresh(self):
        """配置刚改过，叫线程别等下个 tick、立刻重估一次。"""
        self._wake_evt.set()

    # ---- 状态回显 ----

    def active(self):
        """此刻真的在抑制睡眠吗（want 且最近一次调用成功）。"""
        return bool(self._active)

    def last_error(self):
        """最近一次执行失败的说明（"" = 没失败过；诊断页用）。"""
        return self._last_error

    # ---- 内部 ----

    def _run(self):
        last_flags = None
        while not self._stop_evt.is_set():
            self._wake_evt.wait(self._interval)
            self._wake_evt.clear()
            if self._stop_evt.is_set():
                break
            try:
                want = bool(self._state_fn())
            except Exception:
                want = False
            if want:
                # 续约：每个 tick 重发一次（幂等，且能顶掉系统偶发的状态复位）
                ok = self._exec(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self._active = ok
                last_flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                self._last_error = "" if ok else "系统拒绝了睡眠抑制请求"
            elif last_flags is not None:
                # 从"要"变"不要"：清一次（单独的 ES_CONTINUOUS = 清除）
                self._exec(ES_CONTINUOUS)
                self._active = False
                last_flags = None
