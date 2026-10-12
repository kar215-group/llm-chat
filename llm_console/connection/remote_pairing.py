# -*- coding: utf-8 -*-
"""llm_console.connection.remote_pairing — 配对码状态机（2026-10-12 模块化）。

一整套自己的不变量（一个码、一个期限、一个试错计数、一个"已作废"标记），
全部由**同一把锁**保护 —— 放在监听面里太容易被后来的改动漏掉锁：
"匹配即作废"必须是**一次持锁**的 `take()`（原来"先查再作废"分两次拿锁，
同一个码的两个并发请求可以都判匹配、都拿到 token，一次性码就名存实亡）。

限次口径 = **按码不按 IP**（W 2026-10-12 拍板，坑 231）：同一个码被试错满
`PAIR_TRIES_MAX` 次即当场作废，要电脑端刷新重出；按来源 IP 连坐会让校园网 NAT
同出口的人互相牵连（一人撞码、整层楼填对了也进不来）。loopback 两条都豁免
（本机浏览器本来就不需要配对；自检的错码也不该烧掉用户手里真在用的码）。
"""

import secrets as _pysecrets
import threading
import time

from .remote_net import is_loopback

PAIR_TTL_S = 300          # 配对码有效期（W 拍板：5 分钟）
# 同一个配对码被试错这么多次 → **该码作废**（W 2026-10-12 定"按码限次"）
PAIR_TRIES_MAX = 5
# W 2026-10-12 逐字批准的两句原话（拟稿：手机端 B / 设置页 A）
PAIR_BURNED_MSG = "配对码已被试错作废——在电脑端刷新配对码再试。"
PAIR_BURNED_PAGE = "上一码被连续试错，已作废——点「刷新配对码」重出"
PAIR_BAD_MSG = "配对码不对或已过期，请在电脑端刷新后重试。"

class PairGate:
    """一次性配对码 + 按码限次的状态机（全部不变量由同一把锁保护）。

    `RemoteServer` 只做委派（`new_pair_code` / `pair_allowed` / …），监听面里
    不碰码的内部字段；`loopback` 豁免与两句 W 批准的原话都在这里。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.code = ""
        self.deadline = 0.0
        self.tries = 0            # 当前码已被试错几次（满 PAIR_TRIES_MAX 作废）
        self.burned_flag = False  # 上一码被试错作废（新码一出即清）

    def new_code(self):
        """生成新的 6 位一次性配对码（覆盖旧的；设置页"刷新"按钮调它）。

        同时清零按码试错计数与"上一码已作废"标记 —— 刷新出一个新码，
        手机端与设置页的两句作废话术都该收掉。
        """
        code = "%06d" % _pysecrets.randbelow(1000000)
        with self._lock:
            self.code = code
            self.deadline = time.time() + PAIR_TTL_S
            self.tries = 0
            self.burned_flag = False
        return code

    def code_left(self):
        """当前码还剩多少秒（0 = 没有 / 已过期）。"""
        with self._lock:
            return max(0, int(self.deadline - time.time())) \
                if self.code else 0

    def matches(self, code):
        with self._lock:
            return (bool(self.code) and self.code == str(code).strip()
                    and time.time() < self.deadline)

    def consume(self):
        with self._lock:
            self.code = ""
            self.deadline = 0.0

    def take(self, code):
        """配对码**匹配即作废**，一次持锁里做完（原子 CAS）→ 是否匹配成功。

        `_do_pair` 原来是先 `pair_code_matches()` 再 `consume_pair_code()`，两次
        分别拿锁：同一个码的两个并发请求可以都判匹配、都拿到 token，"6 位一次性码"
        就名存实亡。作废仍在"电脑端确认"之前（拒绝也要重拿新码，防重放），语义不变。
        """
        code = str(code or "").strip()
        with self._lock:
            if not (self.code and self.code == code
                    and time.time() < self.deadline):
                return False
            self.code = ""
            self.deadline = 0.0
            return True

    def allowed(self, ip):
        """此刻能不能试配对 → (能不能, 被拒时的原话)。

        按码限次（16 §26 第 7 条，W 2026-10-12 拍板）：判据不看来源 IP ——
        旧的"按 IP 连坐"在校园网 NAT 下会让同出口的人互相牵连（一个人撞码，
        整层楼的人 5 分钟里填对了也进不来）。现在只看**当前码**还有没有试错
        额度：码被试错作废后（`_pair_burned`）要等电脑端刷新新码。
        loopback 永放行：本机浏览器本来就不需要配对。
        """
        if is_loopback(ip):
            return True, ""
        with self._lock:
            if self.burned_flag and not self.code:
                return False, PAIR_BURNED_MSG
            return True, ""

    def missed(self, ip):
        """记一次"没对上"→ 这一码是否**刚被作废**（手机端与设置页各有一句原话）。

        只对"当前有码"的失败计数：没码 / 码已过期的失败没什么可烧的，不计数
        （也顺带让"码过期后才来试"的人不受任何影响）。loopback 不计数 ——
        本机自检的错码用例不该把用户手里真在用的码烧掉。
        """
        if is_loopback(ip):
            return False
        now = time.time()
        with self._lock:
            if not (self.code and now < self.deadline):
                return False
            self.tries += 1
            if self.tries >= PAIR_TRIES_MAX:
                self.code = ""
                self.deadline = 0.0
                self.burned_flag = True
                return True
            return False

    def burned(self):
        """上一码是不是被试错作废了（设置页配对码行据此显示原话）。"""
        with self._lock:
            return bool(self.burned_flag and not self.code)
