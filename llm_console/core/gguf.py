# -*- coding: utf-8 -*-
"""llm_console.core.gguf — GGUF 解析：元数据读取（带缓存）与文件头结构判据（kv 条数 / 视频组件标记）"""

import os
import struct


# 判定用标记：直接在 GGUF 头部的原始字节里找张量名子串。
# 视频组件的导出没有按 GGUF 规范组织张量表（dims 宽度与规范不符），任何"逐条跳张量信息"
# 的解析都会读出一个天量长度并疯狂分配内存（实测能把进程顶到 20GB+ 并假死），
# 所以这里只做固定长度的读取 + 子串匹配，不猜布局。
VIDEO_DIFFUSION_MARKERS = (b"video_patch_proj", b"adaln_t_table",
                           b"token_refiner", b"audio_patch_proj")

VIDEO_ENCODER_MARKERS = (b"visual.blocks", b"model.embed_tokens")

_GGUF_STRUCT_CACHE = {}            # path -> (mtime, size, (kv 条数, 头部原始字节))

def gguf_structure(path, head_bytes=262144):
    """读 GGUF 固定头部，返回 (元数据条数, 其后 head_bytes 字节的原始头部)；失败返回 None。

    只读前 28 字节 + 其后最多 256KB，不做任何布局假设，因此对格式异常的导出也安全。
    缓存键含 mtime/size，文件被替换后自动失效（与 read_gguf_info 同一套规则）。
    """
    try:
        st = os.stat(path)
    except Exception:
        return None
    hit = _GGUF_STRUCT_CACHE.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    out = None
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            (ver,) = struct.unpack("<I", f.read(4))
            (n_tensors,) = struct.unpack("<Q", f.read(8))
            (kv_count,) = struct.unpack("<Q", f.read(8))
            if ver < 2:                       # v1 没有元数据段
                kv_count = 0
            blob = b""
            if kv_count == 0:                 # 无元数据才需要看张量名；有元数据直接判为普通 GGUF
                blob = f.read(head_bytes)
            out = (int(kv_count), blob)
    except Exception:
        out = None
    _GGUF_STRUCT_CACHE[path] = (st.st_mtime, st.st_size, out)
    return out

def gguf_is_chat_capable(path):
    """该 GGUF 是否可能作为语言模型加载：必须有元数据键（kv > 0）。

    llama-server 依赖 general.architecture 等元数据键，而视频链路组件的导出是
    "裸权重容器"（kv=0），加载必然失败 —— 据此把它们从聊天模型列表摘出去。
    判据本身失效（读不动、非 GGUF）时一律按普通模型处理，绝不因判定失败误删聊天模型。
    """
    s = gguf_structure(path)
    if s is None:
        return True
    return s[0] > 0

_GGUF_VT_FMT = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
                6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}

_GGUF_VT_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                 10: 8, 11: 8, 12: 8}

def _gguf_read_string(f):
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", "replace")

def _gguf_skip_array(f):
    """跳过一个 array value（支持嵌套 array）；大缓冲下逐元素快速前进。"""
    (et,) = struct.unpack("<I", f.read(4))
    (n,) = struct.unpack("<Q", f.read(8))
    if et == 9:                                   # array of array：递归消费
        for _ in range(n):
            _gguf_skip_array(f)
    elif et == 8:                                 # string array：逐个跳
        rd, sk = f.read, f.seek
        for _ in range(n):
            (ln,) = struct.unpack("<Q", rd(8))
            sk(ln, 1)
    else:
        esz = _GGUF_VT_SIZE.get(et)
        if esz is None:
            raise ValueError("unknown gguf elem type %d" % et)
        f.seek(esz * n, 1)

def _gguf_read_value(f, vtype):
    if vtype == 8:
        return _gguf_read_string(f)
    if vtype == 9:                                # array：内容不需要，快速跳过
        _gguf_skip_array(f)
        return None
    fmt = _GGUF_VT_FMT.get(vtype)
    if fmt is None:
        raise ValueError("unknown gguf value type %d" % vtype)
    return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]

_GGUF_CACHE = {}          # path -> (mtime, size, info)：同一文件只解析一次

def read_gguf_info(path):
    """读取 GGUF 元数据（带进程内缓存）。

    同一文件被多次调用（ngl 计算、ctx 匹配、KV 预估）时只解析一次；
    缓存键含 mtime/size，文件被替换后自动失效。
    """
    try:
        st = os.stat(path)
    except Exception:
        return None
    hit = _GGUF_CACHE.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return dict(hit[2]) if hit[2] else hit[2]
    info = _read_gguf_info(path)
    _GGUF_CACHE[path] = (st.st_mtime, st.st_size, info)
    return dict(info) if info else info

def _read_gguf_info(path):
    """（原始实现）读取 GGUF 元数据里估算所需字段；找不到所需键时返回 None。"""
    wanted = ("block_count", "attention.head_count_kv", "attention.key_length",
              "attention.head_count", "embedding_length", "context_length",
              "expert_count")
    try:
        with open(path, "rb", buffering=1 << 20) as f:
            if f.read(4) != b"GGUF":
                return None
            struct.unpack("<I", f.read(4))            # version
            struct.unpack("<Q", f.read(8))            # tensor_count
            (kv_count,) = struct.unpack("<Q", f.read(8))
            arch = None
            out = {}
            for _ in range(kv_count):
                key = _gguf_read_string(f)
                (vtype,) = struct.unpack("<I", f.read(4))
                val = _gguf_read_value(f, vtype)
                if key == "general.architecture":
                    arch = str(val)
                elif arch and key.startswith(arch + "."):
                    suffix = key[len(arch) + 1:]
                    if suffix in wanted and val is not None:
                        out[suffix] = val
                elif key.startswith("tokenizer."):
                    # tokenizer 大数组区：架构参数到此为止，可以收工
                    # （expert_count 若存在必然排在 tokenizer 之前）
                    if "block_count" in out and "attention.head_count_kv" in out:
                        break
                if arch and all(s in out for s in
                                ("block_count", "attention.head_count_kv",
                                 "expert_count")):
                    break                              # 需要的都拿到了，提前收工
    except Exception:
        return None
    if "block_count" not in out:
        return None
    return out
