#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cluerich Leads 会话持续监听工具 v3
持续监听用户列表，新消息实时追加到报告，支持断点续传。

依赖: pip install playwright && python -m playwright install chromium
"""

import asyncio
import json
import re
import subprocess
import sys
import os
import signal
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ==================== 配置 ====================
TARGET_URL = "https://leads.cluerich.com/pc/cs/chat/session"
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversation_report.md")
PHONE_REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone_list.md")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
CHECK_INTERVAL = 10  # 秒

# 全局停止标志
stop_flag = False


# ==================== 信号处理 ====================

def setup_signal():
    """设置 Ctrl+C 优雅退出"""
    def handler(signum, frame):
        global stop_flag
        print("\n\n[信号] 收到终止信号，正在保存状态...")
        stop_flag = True
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


# ==================== CDP 端口检测 ====================

def detect_cdp_port():
    """自动检测 Chrome CDP 端口"""
    try:
        result = subprocess.run(
            ["bash", "-c",
             "ps aux | grep chrome | grep 'remote-debugging-port' | grep -v grep | "
             "tail -1 | grep -oE 'remote-debugging-port=[0-9]+' | grep -oE '[0-9]+'"],
            capture_output=True, text=True, timeout=5
        )
        port = result.stdout.strip()
        if port:
            return int(port)
    except Exception:
        pass
    return None


# ==================== JavaScript 提取脚本 ====================

JS_GET_USERS = r"""
(function() {
    // 新页面结构：虚拟滚动列表
    var inner = document.querySelector('.JmY-ToSeUjj7i .rc-virtual-list-holder-inner');
    if (!inner) return JSON.stringify({error: '用户列表容器未找到'});

    var userItems = inner.querySelectorAll(':scope > div.JmY-GLUCXScV4');
    var users = [];
    for (var i = 0; i < userItems.length; i++) {
        var item = userItems[i];
        // 用户名
        var nameEl = item.querySelector('.JmY-uYmlfTr51');
        var name = nameEl ? nameEl.innerText.trim() : '';

        // 状态
        var statusEl = item.querySelector('.JmY-Yatr9Qfc1');
        var status = statusEl ? statusEl.innerText.trim() : '';

        // 时间
        var timeEl = item.querySelector('.ml-1.text-xs.text-gray-3');
        var time = timeEl ? timeEl.innerText.trim() : '';

        // 最新消息预览
        var previewEl = item.querySelector('.JmY-SO0CLcnAS');
        var preview = previewEl ? previewEl.innerText.trim() : '';

        // 数字角标（未读消息数）
        var badgeEl = item.querySelector('.leads-badge-sup .leads-animated-number');
        var badge = badgeEl ? parseInt(badgeEl.innerText.trim()) || 0 : 0;

        users.push({
            index: i + 1,
            name: name,
            status: status,
            time: time,
            preview: preview,
            badge: badge,
            raw: item.innerText.substring(0, 300)
        });
    }
    return JSON.stringify(users);
})()
"""

JS_CLICK_USER_BY_INDEX = r"""
(function(index) {
    var inner = document.querySelector('.JmY-ToSeUjj7i .rc-virtual-list-holder-inner');
    if (!inner) return 'inner not found';
    var items = inner.querySelectorAll(':scope > div.JmY-GLUCXScV4');
    if (items.length >= index) {
        items[index - 1].click();
        return 'clicked';
    }
    return 'not found';
})({index})
"""

JS_EXTRACT_MESSAGES = r"""
(function() {
    var msgs = document.querySelectorAll('.leadsCsUI-MessageItem');
    if (!msgs || msgs.length === 0) return JSON.stringify({error: '消息列表未找到'});

    var messages = [];

    for (var i = 0; i < msgs.length; i++) {
        var item = msgs[i];
        var innerHTML = item.innerHTML || '';
        var innerText = item.innerText || '';

        // 时间分隔
        if (item.classList.contains('leadsCsUI-MsgTimeGap')) continue;

        // 客服名
        var nameEl = item.querySelector('.leadsCsUI-MessageNickname_name');
        var hasName = nameEl && nameEl.innerText.trim().length > 0;

        // 已读/未读标记
        var isReadEl = item.querySelector('.leadsCsUI-MessageAffix_isRead');
        var isAgent = hasName || (isReadEl !== null);

        // 机器人检测
        var isRobot = innerHTML.indexOf('对话剧本') !== -1;

        // 消息正文
        var textEl = item.querySelector('.leadsCsUI-Text');
        var text = textEl ? textEl.innerText.trim() : innerText.trim();

        if (isAgent) {
            var name = nameEl ? nameEl.innerText.trim() : (isRobot ? '[机器人]' : '[客服]');
            messages.push({ type: 'agent', name: name, text: text, is_robot: isRobot });
        } else {
            messages.push({ type: 'user', name: '[用户]', text: text, is_robot: false });
        }
    }

    return JSON.stringify(messages);
})()
"""


# ==================== 解析函数 ====================

def parse_user_info(user_raw):
    """从用户JSON对象中提取结构化信息（新页面结构）"""
    result = {
        'name': user_raw.get('name', ''),
        'time': user_raw.get('time', ''),
        'status': user_raw.get('status', ''),
        'preview': user_raw.get('preview', '')[:50],
        'source': ''
    }

    # 从预览文本中猜测来源
    preview = result['preview']
    source_kw = {'抖音': '抖音广告', '快手': '快手广告', '百度': '百度广告', 
                 '微信': '微信广告', '头条': '头条广告', '搜索': '搜索广告'}
    for kw, source in source_kw.items():
        if kw in preview:
            result['source'] = source
            break

    return result


def make_user_key(user_info, user_raw):
    """生成用户的唯一标识键"""
    name = user_info.get('name', '') or f"用户{user_raw.get('index','?')}"
    status = user_info.get('status', '')
    return f"{name}|{status}"


def get_user_signature(messages):
    """获取用户的最新一条消息签名（用于检测是否有新消息）"""
    if not messages:
        return ""
    last = messages[-1]
    return f"{last['type']}|{last.get('text','')[:80]}"


def analyze_conversation(messages):
    """分析对话，生成摘要，提取电话号码"""
    user_msgs = [m for m in messages if m['type'] == 'user']
    agent_msgs = [m for m in messages if m['type'] == 'agent']
    robot_msgs = [m for m in agent_msgs if m.get('is_robot')]
    human_msgs = [m for m in agent_msgs if not m.get('is_robot')]

    summary = {
        'total': len(messages),
        'user_count': len(user_msgs),
        'agent_count': len(agent_msgs),
        'human_agent_count': len(human_msgs),
        'robot_count': len(robot_msgs),
        'user_msgs': [m['text'][:100] for m in user_msgs],
        'last_user_msg': user_msgs[-1]['text'][:100] if user_msgs else '',
    }
    return summary


def extract_phone_from_messages(messages):
    """从用户消息文本中提取电话号码
    
    思路：遍历每条用户消息，扫描文本中的数字序列，
    如果找到11位数字且以1开头，即为手机号。
    """
    for m in messages:
        if m['type'] != 'user':
            continue
        text = m['text']
        # 从文本中提取所有连续数字序列
        import re
        # 找到所有数字序列（连续数字）
        for match in re.finditer(r'\d+', text):
            num = match.group()
            # 如果是11位且以1开头，很可能是手机号
            if len(num) == 11 and num.startswith('1'):
                # 进一步验证：第二位是3-9
                if num[1] in '3456789':
                    return num
    return ''


# ==================== 状态管理 ====================

def load_state():
    """加载状态文件（断点续传）"""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'users': {},       # {user_key: {user_info, messages, last_signature, last_update}}
        'order': [],       # [user_key, ...] 保持顺序
        'last_full_scan': None
    }


def save_state(state):
    """保存状态文件"""
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==================== 报告生成 ====================

def generate_report(sessions_ordered, state_users):
    """生成 Markdown 报告（增量追加模式）"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    total_users = len(sessions_ordered)
    留资_count = sum(
        1 for k in sessions_ordered
        if '已留资' in state_users[k]['user_info'].get('status', '')
    )

    lines = [
        f"# 会话分析报告",
        f"",
        f"**生成时间**: {now}",
        f"**监听模式**: 持续运行中",
        f"**提取会话数**: {total_users} 个",
        f"**已留资**: {留资_count} 个",
        f"",
        f"---",
        f""
    ]

    for idx, user_key in enumerate(sessions_ordered, 1):
        data = state_users[user_key]
        user_info = data['user_info']
        messages = data.get('messages', [])
        summary = analyze_conversation(messages)

        lines.append(f"## {idx}. {user_info['name']}")
        lines.append(f"")
        lines.append(f"| 项目 | 内容 |")
        lines.append(f"|------|------|")
        lines.append(f"| 状态 | {user_info['status']} |")
        lines.append(f"| 时间 | {user_info['time']} |")
        if user_info.get('phone'):
            lines.append(f"| 电话 | {user_info['phone']} |")
        if user_info.get('source'):
            lines.append(f"| 来源 | {user_info['source']} |")
        lines.append(f"| 消息总数 | {summary['total']} |")
        lines.append(f"| 用户消息 | {summary['user_count']} |")
        lines.append(f"| 人工客服 | {summary['human_agent_count']} |")
        lines.append(f"| 机器人 | {summary['robot_count']} |")
        if user_info.get('preview'):
            lines.append(f"| 最新消息 | {user_info['preview']} |")
        lines.append(f"")

        if summary['last_user_msg']:
            lines.append(f"**用户最后消息**: {summary['last_user_msg']}")
            lines.append(f"")

        if summary['user_msgs']:
            lines.append(f"**用户消息 ({len(summary['user_msgs'])}条)**:")
            for msg in summary['user_msgs']:
                lines.append(f"> {msg}")
            lines.append(f"")

        # 客服消息摘要（取前3条有意义的）
        human = [m for m in messages if m['type'] == 'agent' and not m.get('is_robot')]
        if human[:3]:
            lines.append(f"**客服回复 (前3条)**:")
            for m in human[:3]:
                lines.append(f"> *{m['name']}*: {m['text'][:100]}")
            lines.append(f"")

        lines.append(f"---\n")

    return '\n'.join(lines)


def generate_phone_report(sessions_ordered, state_users):
    """生成电话客户清单"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    # 筛选有电话的用户
    phone_users = []
    for user_key in sessions_ordered:
        info = state_users[user_key].get('user_info', {})
        phone = info.get('phone', '')
        if phone:
            phone_users.append({
                'name': info.get('name', ''),
                'phone': phone,
                'status': info.get('status', ''),
                'time': info.get('time', ''),
                'update': state_users[user_key].get('last_update', '')
            })

    total = len(sessions_ordered)
    phone_count = len(phone_users)

    lines = [
        f"# 已获取电话客户清单",
        f"",
        f"**更新时间**: {now}",
        f"**总客户数**: {total}",
        f"**有电话数**: {phone_count}",
        f"**电话覆盖率**: {'{:.1f}%'.format(phone_count/total*100) if total > 0 else '0%'}",
        f"",
        f"| # | 客户名 | 状态 | 电话 | 获取时间 |",
        f"|---|--------|------|------|---------|",
    ]

    for idx, pu in enumerate(phone_users, 1):
        lines.append(f"| {idx} | {pu['name']} | {pu['status']} | {pu['phone']} | {pu['update']} |")

    if not phone_users:
        lines.append(f"| | （暂无电话客户） | | | |")

    lines.append("")
    return '\n'.join(lines)


# ==================== 提取单个用户消息 ====================

async def extract_user_messages(page, user_raw):
    """点击用户并提取其消息"""
    click_js = JS_CLICK_USER_BY_INDEX.replace('{index}', str(user_raw['index']))
    result = await page.evaluate(click_js)

    if result != 'clicked':
        print(f"    ⚠️ 点击用户失败: {user_raw.get('name','?')}")
        return []

    # 等待消息加载（虚拟列表需要时间）
    await page.wait_for_timeout(2000)

    # 检查是否有消息出现，最多等 5 秒
    for attempt in range(5):
        raw_msgs = await page.evaluate(JS_EXTRACT_MESSAGES)
        try:
            messages = json.loads(raw_msgs)
            if isinstance(messages, dict) and 'error' in messages:
                pass  # 还没加载好，继续等
            elif len(messages) > 0:
                return messages  # 有消息了
        except Exception:
            pass
        await page.wait_for_timeout(1000)

    # 最后再试一次
    raw_msgs = await page.evaluate(JS_EXTRACT_MESSAGES)
    try:
        messages = json.loads(raw_msgs)
        if isinstance(messages, list):
            return messages
    except Exception:
        pass
    return []


# ==================== 用户索引 ====================

def get_user_list_position(page, user_key, all_visible_names):
    """根据用户的 name 在可见列表中找位置"""
    for i, name in enumerate(all_visible_names):
        if name == user_key.split('|')[0]:
            return i
    return -1


async def scroll_user_list_down(page):
    """下滑用户列表（触发虚拟列表加载更多）"""
    await page.evaluate('''() => {
        var scrollDiv = document.querySelector('.JmY-ToSeUjj7i .rc-virtual-list-holder');
        if (scrollDiv) {
            scrollDiv.scrollTop += 200;
        }
    }''')
    await page.wait_for_timeout(500)


async def scroll_user_list_up(page):
    """上滑用户列表"""
    await page.evaluate('''() => {
        var scrollDiv = document.querySelector('.JmY-ToSeUjj7i .rc-virtual-list-holder');
        if (scrollDiv) {
            scrollDiv.scrollTop -= 200;
        }
    }''')
    await page.wait_for_timeout(500)


# ==================== 主监听流程 ====================

async def main():
    global stop_flag

    setup_signal()

    print("=" * 60)
    print("Cluerich Leads 持续监听工具 v4 - 全量用户扫描")
    print(f"检测间隔: {CHECK_INTERVAL} 秒")
    print("按 Ctrl+C 终止并保存状态")
    print("=" * 60)

    # 1. 检测 CDP 端口
    port = detect_cdp_port()
    if not port:
        print("[1] ❌ 未检测到 Chrome CDP 端口")
        print("    请先运行: uvx browser-use --profile --headed open \"https://leads.cluerich.com/pc/cs/chat/session\"")
        return
    print(f"\n[1] CDP端口: {port}")

    # 2. 连接浏览器
    async with async_playwright() as p:
        connected = False
        for attempt in range(3):
            try:
                browser = await p.chromium.connect_over_cdp(f'http://localhost:{port}', timeout=8000)
                connected = True
                print(f"[2] ✅ 已连接到 Chrome (port {port})")
                break
            except Exception as e:
                if attempt == 0:
                    print(f"[2] 连接失败，尝试重新检测端口...")
                    port = detect_cdp_port()
                    if not port:
                        print(f"    仍未检测到端口，请确认浏览器已打开")
                        return
                    print(f"    新端口: {port}")
                else:
                    print(f"[2] ❌ 最终连接失败: {e}")
                    return

        if not connected:
            return

        ctx = browser.contexts[0]
        page = ctx.pages[0]

        if 'cluerich' not in (page.url or ''):
            print(f"[2b] 正在打开目标页面...")
            await page.goto(TARGET_URL, wait_until='networkidle', timeout=30000)

        print(f"[2b] 等待页面加载稳定...")
        try:
            await page.wait_for_load_state('networkidle', timeout=20000)
            print(f"     ✅ 页面已稳定")
        except Exception as e:
            print(f"     ⚠️ 等待networkidle超时: {e}，继续...")
        await page.wait_for_timeout(3000)

        # 3. 加载状态（断点续传）
        state = load_state()
        if state['order']:
            print(f"\n[3] 加载已有状态: {len(state['order'])} 个用户（断点续传）")
            # 记录已有电话的用户数量
            phone_count = sum(1 for v in state['users'].values() if v.get('user_info', {}).get('phone'))
            print(f"    已有电话: {phone_count} 个用户")
        else:
            print(f"\n[3] 首次运行，无历史状态")

        # 扫描队列：需要补电话的老用户 key 列表
        # 每次启动时生成一次，后续新用户添加时实时更新
        scan_queue = []
        scan_queue_idx = 0  # 当前扫描到的位置
        scanning_active = True  # 是否正在进行补电话扫描

        # 4. 进入主监听循环
        cycle = 0
        print(f"\n[4] 开始监听循环...")
        print("-" * 60)

        # 首轮生成扫描队列
        for user_key, v in state['users'].items():
            if not v.get('user_info', {}).get('phone'):
                scan_queue.append(user_key)
        print(f"   待补电话: {len(scan_queue)} 个老用户")

        while not stop_flag:
            cycle += 1
            loop_start = datetime.now()
            new_events = []

            try:
                # ====== 步骤1: 提取当前可见用户列表 ======
                raw = await page.evaluate(JS_GET_USERS)
                try:
                    user_list = json.loads(raw)
                except Exception:
                    user_list = []

                if isinstance(user_list, dict) and 'error' in user_list:
                    print(f"  [{cycle}] ⚠️ 用户列表提取失败: {user_list['error']}")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                visible_names = [u['name'] for u in user_list]

                # ====== 步骤2: 处理新用户（不在 state['users'] 中的） ======
                any_new_user = False
                for user_raw in user_list:
                    user_info = parse_user_info(user_raw)
                    if not user_info['name']:
                        user_info['name'] = f"用户{user_raw.get('index','?')}"
                    user_key = make_user_key(user_info, user_raw)

                    if user_key not in state['users']:
                        any_new_user = True
                        messages = await extract_user_messages(page, user_raw)
                        phone = extract_phone_from_messages(messages)
                        current_sig = get_user_signature(messages)
                        user_info['phone'] = phone

                        state['users'][user_key] = {
                            'user_info': user_info,
                            'messages': messages,
                            'last_signature': current_sig,
                            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        }
                        state['order'].append(user_key)
                        new_events.append(f"🆕 新用户: {user_info['name']}")
                        if phone:
                            new_events.append(f"    📞 电话: {phone}")
                        else:
                            # 新用户也没电话，加入待补队列
                            if user_key not in scan_queue:
                                scan_queue.append(user_key)

                # ====== 步骤3: 需要补电话时，处理扫描队列 ======
                # 每轮最多处理 3 个老用户（避免太慢）
                if scanning_active and scan_queue_idx < len(scan_queue):
                    batch_processed = 0
                    max_per_cycle = 3

                    while scan_queue_idx < len(scan_queue) and batch_processed < max_per_cycle:
                        target_key = scan_queue[scan_queue_idx]

                        # 检查这个用户是否已经从 state 中被删掉
                        if target_key not in state['users']:
                            scan_queue_idx += 1
                            continue

                        # 检查是否已经有电话了（可能被新消息补上了）
                        if state['users'][target_key].get('user_info', {}).get('phone'):
                            scan_queue_idx += 1
                            continue

                        target_name = target_key.split('|')[0]

                        # 在当前可见列表中找这个用户
                        pos = -1
                        for i, name in enumerate(visible_names):
                            if name == target_name:
                                pos = i
                                break

                        if pos >= 0:
                            # 用户当前可见，直接提取
                            target_raw = user_list[pos]
                            messages = await extract_user_messages(page, target_raw)
                            phone = extract_phone_from_messages(messages)

                            if phone:
                                state['users'][target_key]['user_info']['phone'] = phone
                                state['users'][target_key]['messages'] = messages
                                state['users'][target_key]['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                new_events.append(f"📞 补电话: {target_name}")
                                new_events.append(f"    ✅ 电话: {phone}")
                            else:
                                # 没有电话，但是拿到了消息，至少更新下
                                if messages:
                                    state['users'][target_key]['messages'] = messages
                                    state['users'][target_key]['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            # 用户不在当前可见区，下滑加载
                            await scroll_user_list_down(page)
                            # 重新获取可见列表
                            raw2 = await page.evaluate(JS_GET_USERS)
                            try:
                                user_list2 = json.loads(raw2)
                                user_list = user_list2
                                visible_names = [u['name'] for u in user_list]
                            except Exception:
                                pass
                            # 再找一次
                            pos = -1
                            for i, name in enumerate(visible_names):
                                if name == target_name:
                                    pos = i
                                    break
                            if pos >= 0:
                                target_raw = user_list[pos]
                                messages = await extract_user_messages(page, target_raw)
                                phone = extract_phone_from_messages(messages)
                                if phone:
                                    state['users'][target_key]['user_info']['phone'] = phone
                                    state['users'][target_key]['messages'] = messages
                                    state['users'][target_key]['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                    new_events.append(f"📞 补电话(下滑): {target_name}")
                                    new_events.append(f"    ✅ 电话: {phone}")

                        scan_queue_idx += 1
                        batch_processed += 1

                        # 每处理一个后重新获取可见列表
                        raw2 = await page.evaluate(JS_GET_USERS)
                        try:
                            user_list = json.loads(raw2)
                            visible_names = [u['name'] for u in user_list]
                        except Exception:
                            pass

                    if batch_processed > 0:
                        eta = len(scan_queue) - scan_queue_idx
                        new_events.append(f"    📊 扫描进度: {scan_queue_idx}/{len(scan_queue)} (剩余约{eta}个)")

                # ====== 步骤4: 检查预览变化（新消息通知） ======
                for user_raw in user_list:
                    user_info = parse_user_info(user_raw)
                    if not user_info['name']:
                        continue
                    user_key = make_user_key(user_info, user_raw)

                    if user_key in state['users']:
                        old_preview = state['users'][user_key]['user_info'].get('preview', '')
                        new_preview = user_info.get('preview', '')
                        if new_preview and new_preview != old_preview:
                            messages = await extract_user_messages(page, user_raw)
                            phone = extract_phone_from_messages(messages)
                            current_sig = get_user_signature(messages)

                            if messages:
                                old_msgs = state['users'][user_key]['messages']
                                old_count = len(old_msgs)
                                old_sig = state['users'][user_key]['last_signature']

                                if current_sig != old_sig:
                                    new_msgs = messages[old_count:]
                                    state['users'][user_key]['messages'] = messages
                                    state['users'][user_key]['last_signature'] = current_sig
                                    state['users'][user_key]['user_info']['preview'] = new_preview
                                    state['users'][user_key]['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                    if phone:
                                        state['users'][user_key]['user_info']['phone'] = phone
                                    new_events.append(f"💬 {user_info['name']}: +{len(new_msgs)} 条新消息")
                                    if phone:
                                        new_events.append(f"    📞 电话: {phone}")

                # ====== 步骤5: 如果扫描队列全部完成，休息一下 ======
                if scanning_active and scan_queue_idx >= len(scan_queue):
                    scanning_active = False
                    new_events.append(f"✅ 所有老用户电话扫描完成！总共 {len(scan_queue)} 个")

                # ====== 步骤6: 所有还在扫描队列中的用户也更新预览 ======
                for user_raw in user_list:
                    user_info = parse_user_info(user_raw)
                    if not user_info['name']:
                        continue
                    user_key = make_user_key(user_info, user_raw)
                    if user_key in state['users']:
                        state['users'][user_key]['user_info']['preview'] = user_info.get('preview', '')

                # 重新生成报告
                report = generate_report(state['order'], state['users'])
                with open(REPORT_PATH, 'w', encoding='utf-8') as f:
                    f.write(report)

                # 生成电话客户清单
                phone_report = generate_phone_report(state['order'], state['users'])
                with open(PHONE_REPORT_PATH, 'w', encoding='utf-8') as f:
                    f.write(phone_report)

                # 保存状态
                save_state(state)

                # 打印本轮结果
                elapsed = (datetime.now() - loop_start).total_seconds()
                if new_events:
                    phone_total = sum(1 for v in state['users'].values() if v.get('user_info', {}).get('phone'))
                    print(f"\n  [{cycle}] {datetime.now().strftime('%H:%M:%S')} ({elapsed:.1f}秒)")
                    for ev in new_events:
                        print(f"    {ev}")
                    print(f"    📊 共 {len(state['order'])} 个用户 | {phone_total} 个有电话")
                else:
                    print(f"\r  [{cycle}] {datetime.now().strftime('%H:%M:%S')} ({elapsed:.1f}秒) ✓ {len(state['order'])} 个用户 {phone_total}个电话", end='', flush=True)

            except Exception as e:
                import traceback
                print(f"\n  [{cycle}] ❌ 循环异常: {e}")
                traceback.print_exc()

            # 等待下次检测
            try:
                await asyncio.sleep(CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

        # 退出前保存
        print(f"\n\n[退出] 保存最终状态...")
        save_state(state)
        print(f"       报告已保存: {REPORT_PATH}")
        print(f"       状态已保存: {STATE_PATH}")
        print(f"       共监听 {cycle} 个周期")


if __name__ == '__main__':
    asyncio.run(main())
