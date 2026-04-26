#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cluerich Leads 自动化程序守护进程 (Watchdog)
功能：监控主程序运行状态，一旦停止自动重启
用法: python3 watchdog.py
终止: Ctrl+C
"""

import subprocess
import time
import signal
import sys
import os
from datetime import datetime

# ==================== 配置 ====================
MAIN_SCRIPT = "extract_conversation.py"      # 主程序文件名
CHECK_INTERVAL = 30                          # 检查间隔（秒）
RESTART_DELAY = 5                            # 重启前等待（秒）
INIT_WAIT = 15                               # 启动后等待时间（秒），让主程序有足够时间初始化
LOG_FILE = "watchdog.log"                    # 日志文件
PAUSE_FLAG = ".paused"                       # 暂停标记文件（存在时不会自动重启）

# 全局停止标志
stop_flag = False


def is_paused():
    """检查是否处于手动暂停状态"""
    return os.path.exists(PAUSE_FLAG)


def set_paused():
    """设置暂停标记"""
    with open(PAUSE_FLAG, 'w') as f:
        f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


def clear_paused():
    """清除暂停标记"""
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)


def log(msg):
    """打印并记录日志"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def is_main_running():
    """检查主程序是否在运行（包括暂停状态）"""
    try:
        # 使用 ps aux 替代 pgrep，可以找到所有状态的进程（包括 Stopped）
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True
        )
        pids = []
        # 匹配 python/Python 启动的 extract_conversation 进程
        for line in result.stdout.split('\n'):
            if 'python' in line.lower() and 'extract_conversation' in line and 'grep' not in line.lower():
                # 提取 PID（ps aux 第二列）
                parts = line.split()
                if len(parts) >= 2:
                    pids.append(parts[1])
        
        # 额外诊断：如果进程列表为空，检查一下详细原因
        if not pids:
            # 检查是否有 Chrome 浏览器在运行（主程序依赖浏览器）
            chrome_check = subprocess.run(
                ["pgrep", "-f", "Google Chrome"],
                capture_output=True, text=True
            )
            if not chrome_check.stdout.strip():
                log("⚠️ 注意：Chrome 浏览器未运行，主程序可能无法正常工作")
        
        return len(pids) > 0
    except Exception:
        return False


def start_main():
    """启动主程序"""
    log(f"🚀 正在启动 {MAIN_SCRIPT}...")
    try:
        # 使用 nohup 方式启动，确保持续运行
        process = subprocess.Popen(
            ["python3", MAIN_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True  # 创建新会话，避免被终端影响
        )
        log(f"   ✅ 主程序已启动 (PID: {process.pid})")
        return True
    except Exception as e:
        log(f"   ❌ 启动失败: {e}")
        return False


def stop_main():
    """停止主程序"""
    log(f"🛑 正在停止 {MAIN_SCRIPT}...")
    try:
        # 使用 ps + kill 方式，可以确保停止包括暂停状态的进程
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True
        )
        pids = []
        for line in result.stdout.split('\n'):
            if 'python' in line.lower() and 'extract_conversation' in line and 'grep' not in line.lower():
                parts = line.split()
                if len(parts) >= 2:
                    pids.append(parts[1])
        
        for pid in pids:
            subprocess.run(["kill", "-9", pid], capture_output=True)
        
        time.sleep(2)  # 等待进程终止
        log("   ✅ 主程序已停止")
    except Exception as e:
        log(f"   ⚠️ 停止时出错: {e}")


def signal_handler(signum, frame):
    """信号处理：优雅退出"""
    global stop_flag
    log("\n[信号] 收到终止信号，正在停止守护进程...")
    stop_flag = True


def main():
    global stop_flag
    
    # 设置信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    log("=" * 60)
    log("Cluerich Leads 自动化程序守护进程")
    log(f"监控目标: {MAIN_SCRIPT}")
    log(f"检查间隔: {CHECK_INTERVAL} 秒")
    log("按 Ctrl+C 终止守护进程")
    log("=" * 60)
    
    # 启动时检查：如果主程序已在运行，先停止它（避免重复）
    if is_main_running():
        log("⚠️ 启动时发现主程序已在运行，先停止它...")
        stop_main()
    
    # 启动主程序
    start_main()
    
    # 启动后等待一段时间，让主程序有足够时间初始化和连接浏览器
    log(f"⏳ 等待 {INIT_WAIT} 秒，让主程序初始化...")
    time.sleep(INIT_WAIT)
    
    # 进入监控循环
    check_count = 0
    restart_count = 0
    first_check_done = False  # 标记第一次检查是否完成
    paused_notified = False  # 是否已经提示过暂停状态
    
    while not stop_flag:
        check_count += 1
        
        # 检查主程序状态
        if not is_main_running():
            # 检查是否处于手动暂停状态
            if is_paused():
                if not paused_notified:
                    log("⏸️ 检测到暂停标记，处于手动暂停模式，不会自动重启")
                    paused_notified = True
            else:
                # 首次检测不立即重启，可能是主程序还在启动中
                if first_check_done:
                    restart_count += 1
                    paused_notified = False
                    log(f"⚠️ 检测到主程序已停止 (第 {restart_count} 次重启)")
                    time.sleep(RESTART_DELAY)
                    start_main()
                else:
                    # 第一次检测到停止，给主程序更多时间
                    log(f"⏳ 首次检测到主程序未运行，继续等待...")
                    first_check_done = True
        else:
            first_check_done = True
            paused_notified = False
            # 每10次检查打印一次心跳
            if check_count % 10 == 0:
                log(f"💚 心跳检查 #{check_count} | 主程序运行正常 | 已重启 {restart_count} 次")
        
        # 等待下次检查
        for _ in range(CHECK_INTERVAL):
            if stop_flag:
                break
            time.sleep(1)
    
    # 退出前清理
    log("=" * 60)
    log("守护进程正在退出...")
    log(f"总检查次数: {check_count}")
    log(f"重启次数: {restart_count}")
    
    # 停止主程序
    log("🛑 正在停止主程序...")
    stop_main()
    
    # 清除暂停标记
    if is_paused():
        clear_paused()
        log("🧹 已清除暂停标记")
    
    log("✅ 守护进程已退出")


if __name__ == '__main__':
    main()
