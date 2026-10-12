# -*- coding: utf-8 -*-
"""llm_console.connection.remote_ledger — 远程任务台账（2026-10-12 模块化）。

`remote_jobs.json` 是产物取回的唯一白名单落点（台账是我们自己写的，路径逃逸在源头
就不存在；取回时再按 basename + 落点做双层校验，坑 209 纪律）。
**条目形状是接口**：一期定的 `{id, device_id, name, path, done, ok, submitted, files}`
被取回判据与历史开关按名引用，改形状先想清楚手机端与设置页两头（`16` §4 第 3 条
预留的"放开历史产物" = 只放宽判据，不动形状）。
"""

import json
import os
import threading

from ..core.config import APP_DIR, atomic_write_json

# ---------------------------------------------------------------------------
# 远程生图任务管理器（一期：文生图 prompt-only；二期 2026-10-11：参考图 /
# 带图两档 / 重绘强度 / 遮罩，判据逐字复用 GUI 链路，W 拍板并入）
# ---------------------------------------------------------------------------

LEDGER_LOCK = threading.RLock()   # 可重入：ledger_add/_update 持锁调 ledger_save（坑 12 教训）
LEDGER_PATH = os.path.join(APP_DIR, "remote_jobs.json")
LEDGER_KEEP = 200          # 台账保尾条数（防止无限涨）


def ledger_load():
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        tasks = data.get("tasks") if isinstance(data, dict) else None
        return tasks if isinstance(tasks, list) else []
    except Exception:
        return []


def ledger_save(tasks):
    with LEDGER_LOCK:
        atomic_write_json(LEDGER_PATH, {"tasks": tasks[-LEDGER_KEEP:]})


def ledger_add(entry):
    with LEDGER_LOCK:
        tasks = ledger_load()
        tasks.append(entry)
        ledger_save(tasks)


def ledger_update(task_id, **kw):
    with LEDGER_LOCK:
        tasks = ledger_load()
        for t in tasks:
            if t.get("id") == task_id:
                t.update(kw)
        ledger_save(tasks)
