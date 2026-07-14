#!/bin/bash
# 同步代码到 Linux 原生文件系统并启动 agent
set -e

SRC=/mnt/c/Users/123/simple-agent
DST=$HOME/simple-agent

mkdir -p "$DST"

# 复制所有 Python 文件
cp "$SRC"/*.py "$DST/" 2>/dev/null || true
cp "$SRC"/.env "$DST/" 2>/dev/null || true

cd "$DST"

# 激活虚拟环境
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
else
    echo "[!] 未找到 venv，请先运行: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

python3 agent.py "$@"
