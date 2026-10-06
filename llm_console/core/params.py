# -*- coding: utf-8 -*-
"""llm_console.core.params — 参数推算：GPU 层数（MoE 感知）、context 自动匹配、KV 显存预估"""

import os

from .gguf import read_gguf_info

# ctx 取不到时的兜底：与 core/config.DEFAULT_CONFIG["ctx"] 同步（params 不能反向 import config）。
# 只可能在配置被手改 / 损坏成 0 或 null 时现形，必须往小兜 —— 兜大了就是坑 130 那一族（下次启动 OOM）。
_CTX_FALLBACK = 10240


def current_ngl(cfg):
    """当前模型应使用的 GPU 层数：按模型记忆优先，否则回落全局 ngl。

    没有记录且显存为 0（没探到独显）= **纯 CPU**：全局兜底的 24 层对核显机器是毒药
    （llama-server 起不来，用户只看到"服务进程已退出"），2026-10-06 起直接给 0。
    """
    b = os.path.basename(cfg["model"])
    rec = (cfg.get("model_ngl") or {}).get(b)
    if rec is not None:
        return int(rec)
    if not (cfg.get("vram_gb") or 0):
        return 0
    return int(cfg.get("ngl", 24))

def compute_ngl(cfg, path, vram_gb):
    """按显存与模型大小估算该模型的最优 GPU 层数。

    依据（自上而下）：
      0) **vram_gb ≤ 0（没探测到独立显卡）= 纯 CPU**：直接给 ngl=0，不再靠调用方兜
         一个假显存 —— 纯核显机器按 8GB 假显存算出十几层，启动必炸（2026-10-06 随
         modelreq 校准轮一并修的口径）。
      1) 显存预算 = 显存总量 × 92%（留系统/桌面余量）
                   − 非层权重（≈ 文件大小 × 7%，嵌入表 + 输出头）
                   − 运行时开销 0.7GB（CUDA 上下文 / cuBLAS 工作区 / 计算缓冲）
      2) 每层显存 = 文件大小 × 93% ÷ 层数          （层权重，均摊）
                  + KV cache：2(K/V) × KV头数 × head_dim × 2字节(f16) × ctx
                 （层数 / KV头数 / head_dim 直接从 GGUF 文件头读取）
      3) 显存上限层数 ngl_max = 预算 ÷ 每层显存
      4) 最终应用 = ngl_max × 性能折扣
         —— dense 模型折扣 0.85：笔记本 GPU PCIe 带宽有限，GPU 层堆到
            接近显存上限时跨设备搬运开销反而拖慢速度；由开发机 27B 实测
            校准（算出上限约 28 层，×0.85 ≈ 23，与实测甜点 24 吻合）
         —— MoE 模型折扣 1.0 且预算更宽松：每 token 只激活约 3B/35B
            参数，CPU offload 部分本来就快，堆层收益温和但为正；专家
            权重占绝对大头，非层权重占比远低于 dense 的 7%。实测校准
            （35B-A3B，8GB 显存）：旧公式给 11 层，实测 20 层仍可用且
            略快；新公式给约 16 层，落在实测可用区间、显存留 ~1GB 余量。
    结果按模型记入 model_ngl；手动改过的值不会被覆盖。
    """
    info = read_gguf_info(path)
    if not info:
        return None
    try:
        n_layers = int(info["block_count"])
        is_moe = int(info.get("expert_count") or 0) > 0
        if not vram_gb or float(vram_gb) <= 0:
            return {"ngl": 0, "ngl_max": 0, "n_layers": n_layers,
                    "vram": 0.0, "moe": is_moe}
        fsize = os.path.getsize(path)
        kv_heads = int(info.get("attention.head_count_kv") or 0)
        head_dim = int(info.get("attention.key_length") or 0)
        if not head_dim:
            heads = int(info.get("attention.head_count") or 0)
            emb = int(info.get("embedding_length") or 0)
            head_dim = emb // heads if heads else 0

        ctx = max(512, int(cfg.get("ctx", 8192)))   # 故意不等于 _CTX_FALLBACK：它参与 ngl 反推，系数按此值校准
        if is_moe:
            layer_ratio = 0.97          # 专家权重占绝对大头，非层占比极小
            overhead = int(0.5 * (1 << 30))
            vram_factor = 0.97
            discount = 1.0              # 无 PCIe 折扣（MoE 的 CPU 部分不慢）
        else:
            layer_ratio = 0.93
            overhead = int(0.7 * (1 << 30))
            vram_factor = 0.92
            discount = 0.85
        layer_bytes = fsize * layer_ratio / max(1, n_layers)
        kv_per_layer = (2 * kv_heads * head_dim * 2 * ctx) if (kv_heads and head_dim) else 0
        budget = float(vram_gb) * (1 << 30) * vram_factor - fsize * (1 - layer_ratio) - overhead
        if budget <= 0:
            return {"ngl": 0, "ngl_max": 0, "n_layers": n_layers,
                    "vram": vram_gb, "moe": is_moe}
        ngl_max = int(budget / max(1, layer_bytes + kv_per_layer))
        ngl_max = max(0, min(ngl_max, n_layers))
        ngl = max(0, min(n_layers, int(ngl_max * discount)))
        return {"ngl": ngl, "ngl_max": ngl_max, "n_layers": n_layers,
                "vram": vram_gb, "moe": is_moe}
    except Exception:
        return None

def ctx_for(cfg, path=None, agent=False):
    """本次启动应使用的 context：按模型 + 场景（agent=API/代理）查记录，兜底全局默认。"""
    b = os.path.basename(path or cfg.get("model", ""))
    table = cfg.get("model_ctx_api" if agent else "model_ctx") or {}
    try:
        v = int(table.get(b) or 0)
    except Exception:
        v = 0
    if v <= 0:
        # 场景未记录时互相兜底：agent 场景回退主页面值，反之亦然
        other = cfg.get("model_ctx" if agent else "model_ctx_api") or {}
        try:
            v = int(other.get(b) or 0)
        except Exception:
            v = 0
    return v if v > 0 else int(cfg.get("ctx", _CTX_FALLBACK) or _CTX_FALLBACK)

def auto_ctx_for_model(cfg, path):
    """按 GGUF 元数据 + 显存/内存预算，自动推算该模型的安全 context。

    规则（系数用开发机 27B / 35B-MoE 实测手配值校准）：
      显存预算 = 显存 − 权重常驻(ngl 部分；MoE 专家多数自动回退 CPU，按 0.3 折算) − 0.6GB 缓冲
      内存预算 = 内存 − 权重(CPU 部分) − 8GB 系统保留
      cap = min(两预算推导值, 131072, GGUF 声明的原生上下文)，再圆整到常用档位。
      main 档另夹 98304：主页面比 agent 保守一档（大 ctx 首 token 明显变慢），与显存够不够无关。
    返回 {"main": …, "agent": …}；元数据不足返回 None。
    显存缺省按 0（没探测到独显）而不是假 8GB —— 纯核显机器的 ctx 全部由内存预算推导
    （2026-10-06 随 modelreq 校准轮统一口径）。
    """
    info = read_gguf_info(path)
    if not info:
        return None
    try:
        layers = int(info["block_count"])
        kvh = int(info.get("attention.head_count_kv") or 0)
        hd = int(info.get("attention.key_length") or 0)
        if not hd:
            heads = int(info.get("attention.head_count") or 0)
            emb = int(info.get("embedding_length") or 0)
            hd = emb // heads if heads else 0
        if not kvh or not hd or layers <= 0:
            return None
        fsize = os.path.getsize(path)
        per_tok = 2 * kvh * hd * 2 * layers          # 每 token KV（f16，K+V）
        ngl_rec = (cfg.get("model_ngl") or {}).get(os.path.basename(path))
        ngl = int(ngl_rec) if ngl_rec else current_ngl(cfg)
        ngl = max(0, min(ngl, layers))
        is_moe = int(info.get("expert_count") or 0) > 0
        vram = float(cfg.get("vram_gb") or 0.0) * (1 << 30)
        ram = float(cfg.get("ram_gb") or 32.0) * (1 << 30)
        w_vram = fsize * (ngl / layers) * (0.3 if is_moe else 1.0)
        kv_vram = max(0.5 * (1 << 30), vram - w_vram - 0.6 * (1 << 30))
        kv_ram = max(1.0 * (1 << 30),
                     ram - fsize * (layers - ngl) / layers - 8.0 * (1 << 30))
        cap_v = kv_vram * layers / (per_tok * ngl) if ngl else 9e9
        cap_r = kv_ram * layers / (per_tok * (layers - ngl)) if layers > ngl else 9e9
        cap = min(cap_v, cap_r, 131072)
        cl = int(info.get("context_length") or 0)
        if cl > 0:
            cap = min(cap, max(8192, cl))
        steps = [8192, 16384, 24576, 30720, 32768, 49152, 65536, 98304, 131072]

        def snap(v):
            below = [s for s in steps if s <= v]
            return below[-1] if below else 8192

        return {"main": snap(min(cap, 98304)), "agent": snap(cap)}
    except Exception:
        return None

def estimate_kv_gb(cfg, path=None, ctx=None):
    """估算 KV cache 占用（GB）：total / 显存部分 / 内存部分；元数据不足返回 None。"""
    path = path or cfg.get("model", "")
    try:
        ctx = int(ctx or cfg.get("ctx", _CTX_FALLBACK) or _CTX_FALLBACK)
        info = read_gguf_info(path)
        if not info:
            return None
        layers = int(info["block_count"])
        kvh = int(info.get("attention.head_count_kv") or 0)
        hd = int(info.get("attention.key_length") or 0)
        if not hd:
            heads = int(info.get("attention.head_count") or 0)
            emb = int(info.get("embedding_length") or 0)
            hd = emb // heads if heads else 0
        if not kvh or not hd:
            return None
        per_tok = 2 * kvh * hd * 2 * layers      # K+V，f16
        total = per_tok * ctx
        ngl = max(0, min(current_ngl(cfg), layers))
        vram = total * ngl / layers
        return {"total": total / 2 ** 30, "vram": vram / 2 ** 30,
                "ram": (total - vram) / 2 ** 30}
    except Exception:
        return None
