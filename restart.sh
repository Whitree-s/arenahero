#!/bin/bash
# Arena Hero 地图服务 + Agent 常驻重启脚本
# 用 macOS launchd 托管，进程脱离对话/沙箱，harness 无法回收，崩溃自动拉起。
# 用法： bash "/Users/mima1234/WorkBuddy/Arena Hero/restart.sh"

set -e
DIR="/Users/mima1234/WorkBuddy/Arena Hero"
MYUID=$(id -u)
MAP_PLIST=~/Library/LaunchAgents/com.arenahero.map.plist
AGENT_PLIST=~/Library/LaunchAgents/com.arenahero.agent.plist

cd "$DIR"

# 1) 停掉旧的（先回收可能残留的 Bash 后台进程，再 bootout launchd）
pkill -f "arena_agent.py" 2>/dev/null || true
OLD_PID=$(lsof -iTCP:8742 -sTCP:LISTEN -n -P 2>/dev/null | awk 'NR>1{print $2}')
[ -n "$OLD_PID" ] && kill "$OLD_PID" 2>/dev/null || true
launchctl bootout "gui/$MYUID/com.arenahero.map" 2>/dev/null || true
launchctl bootout "gui/$MYUID/com.arenahero.agent" 2>/dev/null || true
sleep 1

# 2) 清空旧 stream（让地图反映新一局）
rm -rf stream/* 2>/dev/null || true
mkdir -p stream

# 3) 注册并启动（launchd 常驻，RunAtLoad + KeepAlive）
launchctl bootstrap "gui/$MYUID" "$MAP_PLIST"
launchctl bootstrap "gui/$MYUID" "$AGENT_PLIST"
launchctl kickstart "gui/$MYUID/com.arenahero.map"
launchctl kickstart "gui/$MYUID/com.arenahero.agent"

sleep 5
echo "地图(8742): $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8742/monitor.html --max-time 3)"
echo "Agent: $(pgrep -f 'arena_agent.py' >/dev/null && echo 在跑 || echo 未启动)"
echo "launchd 状态:"
launchctl list | grep arenahero || true
echo "--- 完成。浏览器硬刷新 Cmd+Shift+R 查看 ---"
