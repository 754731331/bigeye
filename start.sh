#!/usr/bin/env bash
# 大眼X — macOS 启动脚本（对应 Windows 的 start.bat）
#
# 用法：
#   ./start.sh                    # 默认 127.0.0.1:9890（仅本机访问）
#   ./start.sh --host 0.0.0.0     # 允许局域网访问（手机等设备）
#   PORT=8765 ./start.sh          # 自定义端口
set -euo pipefail

# 定位脚本所在目录（仓库根），支持从任意目录调用
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKDIR"

PORT="${PORT:-9890}"
HOST="${HOST:-127.0.0.1}"
LOGFILE="$WORKDIR/data/server_output.log"

# 探测 Python 解释器：优先项目虚拟环境，其次系统 python3
PY=""
for cand in "$WORKDIR/.venv/bin/python" "$WORKDIR/../.venv/bin/python" "$(command -v python3 2>/dev/null)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "错误：找不到 Python 解释器。请先安装 Python 3.11+ 并配置依赖。" >&2
    exit 1
fi

mkdir -p "$WORKDIR/data"

# model_config.json 含 API 密钥、不入库（见 .gitignore）。首次运行时从示例创建。
if [ ! -f "$WORKDIR/model_config.json" ] && [ -f "$WORKDIR/model_config.example.json" ]; then
    cp "$WORKDIR/model_config.example.json" "$WORKDIR/model_config.json"
    echo "已从 model_config.example.json 创建 model_config.json（请填入你的 API 配置）"
fi

echo "================================"
echo "  大眼X (macOS)"
echo "================================"
echo "  Python   : $PY"
echo "  监听地址 : $HOST"
echo "  端口     : $PORT"
echo "  工作目录 : $WORKDIR"
echo "  日志文件 : $LOGFILE"
echo "================================"
echo ""
echo "正在启动大眼X..."

# 前台运行，日志同时落盘；Ctrl+C 停止
exec "$PY" -u "$WORKDIR/server.py" --host "$HOST" --port "$PORT" 2>&1 | tee -a "$LOGFILE"
