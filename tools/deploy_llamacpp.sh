#!/usr/bin/env bash
# =============================================================================
# tools/deploy_llamacpp.sh —— 取 llama.cpp 推理引擎（本工作台的必需外部依赖）
#
# 工作台本身不含推理引擎：它调用 llama.cpp 的 llama-server 可执行文件。
# 这个脚本负责"下载 + 解压 + 把 CUDA 运行库铺到同一个目录"，跑完把目录路径
# 填进工作台的 设置 → 服务与引擎 → llama-server 路径 即可。
#
# 用法：
#   bash tools/deploy_llamacpp.sh                      # 装到 ./llama-engine
#   bash tools/deploy_llamacpp.sh /d/tools/llama       # 装到指定目录
#
# 可选环境变量：
#   BUILD=11224                  llama.cpp 的 release 号（对应 v11224 那个 tag 的产物名）
#   FLAVOR=win-cuda-12.4-x64     发行包后缀；纯 CPU 改成 win-x64，
#                                Linux 用 linux-x64 / linux-cuda-12-x64，
#                                macOS 直接去 brew 或自己编译（见 README）
#   MIRROR=                      GitHub 加速镜像前缀，例如 https://ghfast.top；
#                                留空表示直连 github.com
#   PYTHON=python3               用来解 zip 的 Python（找不到会退到 python）
#
# 说明：下载用 curl 的 -C - 断点续传 + 多轮重试，网络差的机器上"卡住"是
# 正常的，脚本会自己接着传；全程只写目标目录，不动仓库里其他东西。
# =============================================================================
set -u

TARGET="${1:-./llama-engine}"
BUILD="${BUILD:-11224}"
FLAVOR="${FLAVOR:-win-cuda-12.4-x64}"
MIRROR="${MIRROR:-}"
PYTHON="${PYTHON:-python3}"

case "$FLAVOR" in
  *cuda*) CUDART_ZIP="cudart-llama-bin-${FLAVOR}.zip" ;;
  *)      CUDART_ZIP="" ;;                       # CPU 包不需要额外运行库
esac
MAIN_ZIP="llama-b${BUILD}-bin-${FLAVOR}.zip"

command -v curl >/dev/null 2>&1 || { echo "需要 curl"; exit 1; }
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi
command -v "$PYTHON" >/dev/null 2>&1 || { echo "需要 Python（用来解 zip）"; exit 1; }

mkdir -p "$TARGET"
cd "$TARGET" || exit 1
echo "== 目标目录：$(pwd)"
echo "== 主包：$MAIN_ZIP    CUDA 运行库：${CUDART_ZIP:-（不需要）}"

dl() {
  local f="$1"
  local url="$MIRROR/https://github.com/ggml-org/llama.cpp/releases/download/b${BUILD}/$f"
  echo "== 下载 $f"
  for i in $(seq 1 60); do
    if curl -L -C - --retry 12 --retry-delay 3 --connect-timeout 30 \
            --max-time 1800 -o "$f" "$url"; then
      echo "   OK（第 $i 轮完成）"
      return 0
    fi
    echo "   第 $i 轮没传完，断点续传重试…"
    sleep 3
  done
  echo "   FAILED: $f"
  return 1
}

dl "$MAIN_ZIP" || exit 1
[ -n "$CUDART_ZIP" ] && { dl "$CUDART_ZIP" || exit 1; }

echo "== 解压（两个包都铺平到同一目录，CUDA dll 必须和 llama-server 挨着）"
TARGET="$TARGET" MAIN_ZIP="$MAIN_ZIP" CUDART_ZIP="$CUDART_ZIP" "$PYTHON" - <<'PYEOF'
import os, glob, shutil, zipfile

target = os.path.abspath(os.environ["TARGET"])
zips = [z for z in (os.environ["MAIN_ZIP"], os.environ.get("CUDART_ZIP") or "") if z]

for z in zips:
    with zipfile.ZipFile(os.path.join(target, z)) as zf:
        zf.extractall(target)
    print("extracted", z)

# 有的发行包在 zip 里带一层目录：把含 llama-server 的那层内容铺平上来，
# 免得 CUDA dll 在根目录、exe 却藏在子目录里互相找不到
need = "llama-server.exe" if os.name == "nt" else "llama-server"
for root, dirs, files in os.walk(target):
    if root == target or need not in files:
        continue
    for f in files:
        dst = os.path.join(target, f)
        if not os.path.exists(dst):
            shutil.move(os.path.join(root, f), dst)
    print("flattened", os.path.relpath(root, target))
    break

hit = [p for p in glob.glob(os.path.join(target, "**", need), recursive=True)]
print("引擎位置：", hit[0] if hit else "!! 没找到 llama-server，请检查 FLAVOR/BUILD")
if hit:
    print("把它所在目录填进工作台的 设置 → llama-server 路径。")
PYEOF

echo "== 结果"
ls -la "$TARGET" | head -30
echo "ALL DONE"
