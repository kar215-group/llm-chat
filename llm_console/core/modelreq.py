# -*- coding: utf-8 -*-
"""llm_console.core.modelreq — 模型运行需求评估：推荐 / 最低两档 × 显存 / 内存两维 → 0~3 级

2026-10-06 W 点名：加入 / 启动模型时检查"当前配置带不带得动"。判据口径（W 定 + 当日实测校准）：

  · **推荐档** = 权重（文本）/ 主体 + VAE（生图生视频）全进显存的舒服线；
    文本内存另需 ≥ 权重 + 3GB（系统与运行时保留）。
  · **闪退只由内存判**：显存装不下会**溢出到内存慢跑，不会闪退** —— 实测依据（W 2026-10-06）：
    MiniMax-H3 的 q4 量化在 8GB 显存机上慢跑可运行；纯核显（显存 0/共享）只要内存充足
    也是"速度较慢"而非"难以胜任"。所以显存维**只贡献"慢"**，"可能闪退 / 必闪退"
    全看内存：min_ram（编码器 + 系统保留）+ 显存装不下的溢出部分。
  · **MoE 文本模型显存推荐按 30% 折算**（专家权重常驻内存，与 `params.auto_ctx` 同系数）
    —— 实测依据：Qwen3.6-35B MoE 在 8GB 显存机上生成很快，应视为可胜任；非 MoE 的
    Qwen3.8-27B 判"慢"是对的。
  · 分级（W 2026-10-06 定，阈值较首版调高）：
      低于推荐 ×0.85       → 1 级「可能速度较慢」
      内存低于最低 ×0.9    → 2 级「可能无法正常运行，导致闪退」
      内存低于最低 ×0.7    → 3 级「以目前探测结果来看，此设备几乎难以胜任该模型，必闪退」

本机配置一律用调用方传入的 `vram_gb` / `ram_gb`（设置 → 服务参数 → 高级参数里可手动改，
0 = 未探测 / 无独立显卡）—— 本模块不自己探硬件。系数是保守估计、以开发机实测校准过两端
（Qwen-Image Q5_0 = 0 级、H3 q4 = 慢、27B dense = 慢、35B MoE = 可胜任）；要精化只动
这个模块，界面与调用点不感知。组件大小按文件字节数算。
"""

import os

from .gguf import read_gguf_info

LEVEL_OK = 0          # 够用
LEVEL_SLOW = 1        # 低于推荐 ×0.85：可能速度较慢
LEVEL_RISKY = 2       # 内存低于最低 ×0.9：可能无法正常运行，导致闪退
LEVEL_FATAL = 3       # 内存低于最低 ×0.7：几乎难以胜任，必闪退

# 简洁文案（W 定的口径，界面直接引用；别在各调用点重写）
LEVEL_TEXT = {
    LEVEL_SLOW: "可能速度较慢",
    LEVEL_RISKY: "可能无法正常运行，导致闪退",
    LEVEL_FATAL: "以目前探测结果来看，此设备几乎难以胜任该模型，必闪退",
}

KIND_LABEL = {"text": "文本", "image": "生图", "video": "生视频"}


def _need(rec_vram, rec_ram, min_ram, gpu_total, detail):
    """统一的需求形状。

    `rec_vram` / `rec_ram` = 舒服线；`min_ram` = 内存闪退线**基数**（编码器 + 系统保留）；
    `gpu_total` = 想放进显存的权重总量 —— 显存装不下的部分在 `grade` 里按本机显存折算成
    内存溢出（文本为 0：权重本来就在内存 / 磁盘，不随显存变）。
    """
    return {"rec_vram": rec_vram, "rec_ram": rec_ram, "min_ram": min_ram,
            "gpu_total": gpu_total, "detail": detail}


def _size_gb(path):
    try:
        return os.path.getsize(path) / (1 << 30) if path and os.path.isfile(path) else 0.0
    except Exception:
        return 0.0


def assess_text(cfg, path, files=None):
    """文本模型：需求 = 权重本身；MoE 的显存推荐按 30% 折算，闪退线在内存维。"""
    del cfg, files
    f = _size_gb(path)
    if f <= 0:
        return None
    try:
        info = read_gguf_info(path) or {}
    except Exception:
        info = {}
    moe = bool(info.get("expert_count"))
    # 推荐显存：dense 全进显存 + 计算缓冲；MoE 只需激活部分的量级（专家权重待在内存，
    # CPU offload 不拖慢生成 —— 开发机 35B-A3B 实测）。KV 由自动 ctx 那套另行钳住。
    rec_vram = f * (0.3 if moe else 1.05) + 1.0
    return _need(rec_vram, f * 1.2 + 5.0, f + 3.0, 0.0,
                 "权重 %.1fGB%s" % (f, "（MoE）" if moe else ""))


def assess_image(cfg, path, files=None):
    """生图模型：显存维 = 扩散主体 + VAE（默认后端放显卡），内存维 = 编码器类。

    `files` 可传 `media.resolve_img_files` 的现成结果（发送链路手里就有，免二次解析）；
    缺件的槽位按 0 计 —— "缺不缺件"由发送前的缺件预检管，这里只管装不装得下。
    """
    del cfg
    if not path or not os.path.isfile(path):
        return None
    if files is None:
        from .media import resolve_img_files       # 延迟导入：media 依赖重，别拖累纯逻辑调用方
        files = resolve_img_files({}, path)
    gpu = _size_gb(path) + _size_gb(files.get("vae"))
    cpu = sum(_size_gb(files.get(k)) for k in ("llm", "clip_l", "clip_g", "t5xxl", "tokenizer"))
    return _need(gpu * 1.15 + 1.0, cpu * 1.2 + gpu + 4.0, cpu + 3.0, gpu,
                 "主体 %.1f + VAE %.1f（显存）/ 编码器 %.1f（内存）"
                 % (_size_gb(path), _size_gb(files.get("vae")), cpu))


def assess_video(cfg, path, files=None):
    """生视频模型：与生图同一套，音频 VAE（要出声才有）也进显存维。"""
    del cfg
    if not path or not os.path.isfile(path):
        return None
    if files is None:
        from .media import resolve_video_files
        files = resolve_video_files({}, path) or {}
    gpu = (_size_gb(path) + _size_gb(files.get("vae")) + _size_gb(files.get("audio_vae")))
    cpu = sum(_size_gb(files.get(k)) for k in ("llm", "t5xxl", "tokenizer"))
    return _need(gpu * 1.15 + 1.0, cpu * 1.2 + gpu + 4.0, cpu + 3.0, gpu,
                 "主体 %.1f + VAE %.1f（显存）/ 编码器 %.1f（内存）"
                 % (_size_gb(path), _size_gb(files.get("vae")), cpu))


def assess(cfg, path, kind, files=None):
    """按 kind 分派；kind ∈ text / image / video。评估不出（文件不在 / 大小为 0）返回 None。"""
    if kind == "text":
        return assess_text(cfg, path)
    if kind == "image":
        return assess_image(cfg, path, files)
    if kind == "video":
        return assess_video(cfg, path, files)
    return None


def grade(need, vram_gb, ram_gb):
    """设备 (vram_gb, ram_gb) 对一份需求的等级 → 0~3。

    显存维只判"慢"（不够就溢出到内存，实测不崩）；内存维判"险 / 必闪退"，
    且基数上要加**显存装不下的溢出部分**（纯核显机器全部权重都落内存）。
    两维独立比、取最严。`need` 为 None 返 0。
    """
    if not need:
        return LEVEL_OK
    vram = max(0.0, float(vram_gb or 0.0))
    ram = max(0.0, float(ram_gb or 0.0))
    spill = max(0.0, need.get("gpu_total", 0.0) - vram)
    min_ram_eff = need["min_ram"] + spill
    lvl = LEVEL_OK
    if vram < need["rec_vram"] * 0.85 or ram < need["rec_ram"] * 0.9:
        lvl = LEVEL_SLOW
    if ram < min_ram_eff * 0.9:
        lvl = LEVEL_RISKY
    if ram < min_ram_eff * 0.7:
        lvl = LEVEL_FATAL
    return lvl


def evaluate_new(cfg, chat=(), images=(), vids=(), vram_gb=0.0, ram_gb=0.0, seen=None):
    """对一批模型里**没评估过**的各评一次 → `(reports, new_seen)`。

    `seen` 是持久登记表（cfg["perf_reported"]，键 = 模型路径，值 = 上次的级别）：
    评过的不再评；文件删掉后的剪枝由调用方做（它知道本轮完整清单）。
    **评估结果目前只在启动前的 3 级确认里现场重算，登记表不产生输出**
    （W 2026-10-06：性能检查结果不进输出栏）—— 留着它是为将来的查看界面备好数据。
    """
    seen = dict(seen or {})
    out = []
    for kind, paths in (("text", chat), ("image", images), ("video", vids)):
        for p in paths or []:
            if not p or p in seen:
                continue
            try:
                need = assess(cfg, p, kind)
                lvl = grade(need, vram_gb, ram_gb) if need else LEVEL_OK
            except Exception:
                need, lvl = None, LEVEL_OK
            seen[p] = lvl
            if lvl >= LEVEL_SLOW and need:
                out.append((p, kind, lvl, need))
    return out, seen
