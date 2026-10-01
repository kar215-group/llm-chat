# -*- coding: utf-8 -*-
"""llm_console.core.cloudjobs — 云端异步任务的本地台账（cloud_jobs.json）。

为什么需要它：云端生视频是"提交任务 → 轮询 → 下载"，而**产物 URL 只活 24 小时**，
本机每天 23:30 断电。没有台账的话，一次断电就把排到一半的任务连同唯一的产物地址
一起丢了——那份结果就再也拿不回来。

只记 task_id 与元信息，**绝不记密钥**（密钥在 secrets.json，另有不进备份的约定）。
文件写法沿用 core/secrets.py：RLock、读失败返回空表、写失败返回 False 不抛——
它是可选台账，不该让界面崩掉。
"""

import json
import os
import threading
import time

from .config import APP_DIR

JOBS_PATH = os.path.join(APP_DIR, "cloud_jobs.json")
JOBS_VERSION = 1

# 台账里"还没结束"的状态：这些会在启动时列出来，等用户点「取回」
OPEN_STATUS = ("pending", "running", "waiting", "unknown", "timeout", "submitted",
               "succeeded")          # succeeded 但没落地 = 下载失败，同样要能重试

_LOCK = threading.RLock()
# URL 有效期：文档 §12.1 明写产物 URL 24 小时失效（百炼托管的 H3 写的是 30 天，
# 但统一按最紧的 24 小时处理——过期了服务端不会通知，只会给一个看不懂的下载错误）
URL_TTL_HOURS = 24


def load_jobs():
    """读取台账 → {"version": int, "jobs": {task_id: {...}}}；异常一律回空表。"""
    with _LOCK:
        try:
            with open(JOBS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {"version": JOBS_VERSION, "jobs": {}}
    if not isinstance(data, dict):
        return {"version": JOBS_VERSION, "jobs": {}}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        jobs = {}
    return {"version": int(data.get("version") or JOBS_VERSION), "jobs": jobs}


def save_jobs(data):
    with _LOCK:
        try:
            with open(JOBS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False


def add_job(task_id, kind, provider_id, provider_name, model, prompt, dest,
            status="pending", resolution="", duration=0):
    """提交成功后立刻登记；断电前至少留得下 task_id。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    data = load_jobs()
    job = {"task_id": task_id, "kind": kind, "provider_id": provider_id,
           "provider_name": provider_name, "model": model,
           "prompt": str(prompt or "")[:500], "dest": dest,
           "status": status, "created_at": time.time(), "updated_at": time.time(),
           "resolution": resolution, "duration": duration,
           "url": "", "paths": [], "error": "", "seconds": 0}
    data["jobs"][task_id] = job
    save_jobs(data)
    return job


def get_job(task_id):
    return load_jobs()["jobs"].get(str(task_id or ""))


def update_job(task_id, **patch):
    task_id = str(task_id or "").strip()
    data = load_jobs()
    job = data["jobs"].get(task_id)
    if not isinstance(job, dict):
        return None
    job.update(patch)
    job["updated_at"] = time.time()
    data["jobs"][task_id] = job
    save_jobs(data)
    return job


def remove_job(task_id):
    task_id = str(task_id or "").strip()
    data = load_jobs()
    if task_id not in data["jobs"]:
        return False
    data["jobs"].pop(task_id, None)
    save_jobs(data)
    return True


def mark_done(task_id, paths=(), urls=(), seconds=0, usage=None):
    """落地成功：状态改 succeeded、记下本地路径，**保留 task_id 与 URL** 供追溯。"""
    return update_job(task_id, status="succeeded", paths=list(paths or []),
                      urls=list(urls or []), seconds=round(float(seconds or 0), 1),
                      usage=usage or {})


def mark_failed(task_id, error, status="failed"):
    return update_job(task_id, status=status, error=str(error or "")[:600])


def unfinished():
    """还没结束的任务（含"succeeded 但没落地"），按提交时间倒序。"""
    out = []
    for j in load_jobs()["jobs"].values():
        if not isinstance(j, dict):
            continue
        if str(j.get("status") or "") in OPEN_STATUS and not j.get("paths"):
            out.append(j)
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return out


def expired(job, hours=None):
    """超过 URL 有效期就再也取不回来（服务端不会通知，只会给个下载错误）。"""
    hours = hours or URL_TTL_HOURS
    try:
        return (time.time() - float(job.get("created_at") or 0)) > hours * 3600
    except Exception:
        return True


def prune(keep_days=7):
    """清掉**已结束**且超过 keep_days 的记录；未完成的留着，由 expired() 判断能不能取。"""
    data = load_jobs()
    cut = time.time() - max(1, int(keep_days or 7)) * 86400
    drop = [tid for tid, j in data["jobs"].items()
            if isinstance(j, dict) and str(j.get("status")) not in OPEN_STATUS
            and float(j.get("updated_at") or 0) < cut]
    for tid in drop:
        data["jobs"].pop(tid, None)
    if drop:
        save_jobs(data)
    return len(drop)


def describe(job):
    """界面用的一行：什么能力、哪个模型、什么时候提交、还取不取得回。"""
    kind = "生图" if str(job.get("kind")) == "image" else "生视频"
    t = time.strftime("%m-%d %H:%M", time.localtime(float(job.get("created_at") or 0)))
    tail = "（URL 已过 24 小时，取不回来了）" if expired(job) else ""
    return "☁ 云端%s · %s · 提交于 %s · task_id=%s%s" % (
        kind, job.get("model") or "?", t, str(job.get("task_id") or "")[:16], tail)
