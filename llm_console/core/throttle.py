# -*- coding: utf-8 -*-
"""llm_console.core.throttle — 进页自动任务的节流闸（纯标准库，不 import tkinter）。

「进关于页自动查更新」与「进模型文件与引擎页自动补全缺失项」是同一类事：**切进某个
设置页时想自动跑一次，但同一进程内不许反复跑**。这条判据与它的读数只写在本模块一处，
两边共用：

  · `cooldown_left(last_at, now, cooldown)` —— 纯函数：还剩多少秒才算冷却结束（≤0 = 到点）；
  · `AutoThrottle(seconds)` —— 把"上次跑的时刻"与冷却时长包在一起，`due()` 问、`mark()` 记。

为什么单独一层（W 2026-10-05）：原来 `cooldown_left` 长在 `core.updater` 里 —— 那是"查
GitHub 版本"的账本，模型补全要复用它就得 import updater，语义上不对（两者只共享"节流"
这一件事）。`updater` 现在从本模块转出同名符号，既有调用点与自检一个字不用改。
"""

import time

# 进页自动补全模型参数的冷却：10 分钟（与「检查更新」匿名档同一数量级）。
AUTO_SCAN_COOLDOWN = 600

# 「第一个可用的模型配好了就切过去」那条**兜底**轮询的冷却（秒）。
# 真正让它"立即响应"的是事件钩子（设置页保存 / 引擎装完 / 手动定向模型），这里只是安全网：
# 用户在程序外面放了模型、或者密钥是别的进程写进去的。判据要扫盘（读 GGUF 头），
# 所以给它一个冷却 —— 不然每 3 秒一次状态轮询就白扫一遍（坑 4 的通用纪律）。
AUTO_PICK_COOLDOWN = 10


def cooldown_left(last_at, now, cooldown=600):
    """距"允许下一次自动跑"还差几秒；`<= 0` = 现在就可以跑。

    `last_at` / `now` 都是**墙钟秒**（`time.time()`），不是单调时钟 —— 界面还要用它把结果
    写成"上次 14:32"，两种语义不能混用（所以这里不做时间源抽象）。
    `last_at` 为 0 / None（从没跑过）时返回 0：第一次进页总能跑。
    """
    if not last_at:
        return 0
    elapsed = float(now) - float(last_at)
    if elapsed < 0:        # 系统时间往回拨过：当成"刚刚跑过"，别把冷却拖成无穷（手动仍可跑）
        elapsed = 0.0
    return max(0.0, float(cooldown) - elapsed)


class AutoThrottle(object):
    """"进页自动跑一次 + 冷却"那套状态的模块化封装：一个时刻 + 一个时长。

    `App` 持有一个实例即可（会话内有效、重启即清，与「检查更新」的进页冷却同一条口径）；
    手动触发的入口**不看它**（用户亲手点不受冷却限制）。
    """

    def __init__(self, seconds, at=0.0):
        self.seconds = float(seconds)
        self.at = float(at or 0.0)      # 上次真跑的时刻（墙钟秒；0 = 从没跑过）

    def left(self, now=None):
        return cooldown_left(self.at, time.time() if now is None else now, self.seconds)

    def due(self, now=None):
        """到点了没有（没跑过 / 冷却已过 = True）。"""
        return self.left(now) <= 0

    def mark(self, now=None):
        """记一次"刚刚跑过"。"""
        self.at = time.time() if now is None else float(now)
        return self.at
