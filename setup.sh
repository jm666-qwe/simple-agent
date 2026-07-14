#!/bin/bash
# Simple Agent 一键安装脚本
# 用法: bash setup.sh

set -e

echo "===== Simple Agent 安装 ====="
echo ""

# 检测 Python
PYTHON=""
for cmd in python3 python; do
    $cmd -c "import sys; assert sys.version_info >= (3, 10)" 2>/dev/null && PYTHON=$cmd && break
done
if [ -z "$PYTHON" ]; then
    echo "[错误] 需要 Python 3.10+"
    exit 1
fi
echo "Python: $($PYTHON --version)"

# 创建 venv
if [ ! -d "venv" ]; then
    $PYTHON -m venv venv
    echo "虚拟环境: 已创建"
else
    echo "虚拟环境: 已存在"
fi

# 安装依赖
./venv/bin/pip install -r requirements.txt -q
echo "依赖: 已安装"

# 配置 API Key
if [ ! -f ".env" ]; then
    echo ""
    echo "API Key 注册地址: https://platform.deepseek.com/api-keys"
    echo ""
    read -p "输入你的 DeepSeek API Key: " key
    echo "DEEPSEEK_API_KEY=$key" > .env
    echo -e "\nKey 已保存到 .env"
else
    echo "API Key: 已配置"
fi

echo ""
echo "===== 安装完成 ====="
echo ""
echo "运行: python3 agent.py"
echo "帮助: 进入后输入 /help"
echo "====== ======== ====="
