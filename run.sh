#!/bin/bash
# 一键启动 Cluerich Leads 持续监听（CDP 远程调试模式）
# 用法: ./run.sh
# 终止: Ctrl+C

cd "$(dirname "$0")"

CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT=9222
ORIG_DATA_DIR="/Users/liang/Library/Application Support/Google/Chrome"
# 调试专用数据目录（复制 Default 保留登录态）
CDP_DATA_DIR="/Users/liang/.chrome-cdp-debug"

# ==================== 检测/启动 Chrome CDP 浏览器 ====================

echo "[1] 检测 Chrome CDP 端口..."

detect_cdp_port() {
    curl -s --max-time 2 "http://localhost:$CDP_PORT/json/version" > /dev/null 2>&1
    return $?
}

if detect_cdp_port; then
    echo "    ✅ CDP端口 $CDP_PORT 可用（使用已有浏览器）"
else
    echo "    ❌ 未检测到 CDP 端口"
    echo ""
    echo "    → 即将自动关闭现有 Chrome，重新以调试模式启动..."
    echo ""

    osascript -e 'tell application "Chrome" to quit' 2>/dev/null
    sleep 2

    while pgrep -x "Google Chrome" > /dev/null 2>&1; do
        echo "    等待 Chrome 完全退出..."
        sleep 1
    done
    echo "    ✅ Chrome 已完全退出"

    # 准备调试数据目录（复制 Default 保留登录态）
    echo "    → 准备调试数据目录..."
    rm -rf "$CDP_DATA_DIR"
    mkdir -p "$CDP_DATA_DIR"
    cp -R "$ORIG_DATA_DIR/Default" "$CDP_DATA_DIR/Default"
    cp "$ORIG_DATA_DIR/First Run" "$CDP_DATA_DIR/" 2>/dev/null
    cp "$ORIG_DATA_DIR/Local State" "$CDP_DATA_DIR/" 2>/dev/null
    echo "    ✅ 数据目录已创建（已复制 Default 登录态）"

    echo "    → 启动带调试端口的 Chrome..."
    "$CHROME_PATH" \
        --remote-debugging-port=$CDP_PORT \
        --user-data-dir="$CDP_DATA_DIR" \
        "https://leads.cluerich.com/pc/cs/chat/session" &
    CHROME_PID=$!
    echo "    Chrome 主进程 PID: $CHROME_PID"

    echo "    → 等待浏览器就绪..."
    MAX_WAIT=30
    COUNT=0
    while [ $COUNT -lt $MAX_WAIT ]; do
        if detect_cdp_port; then
            echo "    ✅ CDP端口 $CDP_PORT 可用"
            break
        fi
        sleep 1
        COUNT=$((COUNT + 1))
    done

    if ! detect_cdp_port; then
        echo "    ❌ 等待超时（${MAX_WAIT}秒），浏览器未就绪"
        exit 1
    fi
fi

echo ""
echo "[2] 等待页面加载..."
sleep 8

echo "[3] 运行持续监听脚本（Ctrl+C 终止）..."
echo "--------------------------------------------------------------------------------"
python3 extract_conversation.py
