#!/bin/bash
# BP-Lighter 项目安装脚本

echo "=================================="
echo "  BP-Lighter 安装脚本"
echo "=================================="

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo ""
echo "步骤 1: 安装基本依赖..."
pip install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "步骤 2: 安装 Lighter SDK..."
if [ -d "$PROJECT_ROOT/lighter-python-main" ]; then
    echo "发现 lighter-python-main 目录，正在安装..."
    pip install -e "$PROJECT_ROOT/lighter-python-main"
    echo "✅ Lighter SDK 安装完成"
else
    echo "❌ 未找到 lighter-python-main 目录"
    echo "请确保项目结构正确"
    exit 1
fi

echo ""
echo "步骤 3: 检查 .env 文件..."
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "✅ .env 文件存在"
else
    echo "⚠️ .env 文件不存在"
    echo "请复制 env_example.txt 到 .env 并配置正确的密钥:"
    echo "  cp $SCRIPT_DIR/env_example.txt $SCRIPT_DIR/.env"
fi

echo ""
echo "=================================="
echo "  安装完成！"
echo "=================================="
echo ""
echo "运行测试脚本检查连接:"
echo "  python3 $SCRIPT_DIR/test_connection.py"
echo ""
echo "运行套利脚本:"
echo "  python3 $SCRIPT_DIR/arbitrage.py --ticker BTC --size 0.001 --max-position 0.008"

