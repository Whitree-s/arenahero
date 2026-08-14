#!/bin/bash
# Arena Hero 决策层切换脚本：在原决策(legacy) 与 新决策(evolve) 之间互斥切换。
#
# 用法：
#   bash "/Users/mima1234/WorkBuddy/Arena Hero/switch_agent.sh" legacy   # 切回原决策 (arena_agent.py)
#   bash "/Users/mima1234/WorkBuddy/Arena Hero/switch_agent.sh" evolve   # 切到新决策 (arena-evolve 进化策略)
#   bash "/Users/mima1234/WorkBuddy/Arena Hero/switch_agent.sh"          # 默认 legacy
#
# 行为：
#   - 同一时间只跑一个决策 agent（两边都写 stream/，故互斥）。
#   - 切换会清空 stream/*，避免两套决策的帧混在一起（monitor 重新渲染新一局）。
#   - 地图服务 com.arenahero.map 两决策共用、独立常驻；本脚本会确保它在跑。
#   - 切换结果写入 .active_agent 状态文件（内容为 legacy / evolve）。
#
# 注意（live 上线）：两套决策连正式世界都需要 ARENA_HERO_API_KEY。
#   launchd 托管的进程不继承交互 shell 环境，须在 plist 的 <EnvironmentVariables>
#   里写入 key，或先执行 `launchctl setenv ARENA_HERO_API_KEY xxx`。
#   缺 key 时 agent 每 30s 重试（KeepAlive 自动拉起），配好即自动连上。

set -u
DIR="/Users/mima1234/WorkBuddy/Arena Hero"
MYUID=$(id -u)
MAP_PLIST=~/Library/LaunchAgents/com.arenahero.map.plist
AGENT_LEGACY=~/Library/LaunchAgents/com.arenahero.agent.plist
AGENT_EVOLVE=~/Library/LaunchAgents/com.arenahero.agent.evolve.plist
MODE="${1:-legacy}"

cd "$DIR" || { echo "无法进入 $DIR"; exit 1; }

if [ "$MODE" != "legacy" ] && [ "$MODE" != "evolve" ]; then
  echo "用法: switch_agent.sh [legacy|evolve]"
  exit 2
fi

echo "[switch] 目标模式: $MODE"

# 1) 停掉两个决策 agent（互斥，确保只跑一个）
echo "[switch] 停止现有 agent..."
pkill -f "arena_agent" 2>/dev/null || true
launchctl bootout "gui/$MYUID/com.arenahero.agent" 2>/dev/null || true
launchctl bootout "gui/$MYUID/com.arenahero.agent.evolve" 2>/dev/null || true
sleep 1

# 2) 清除帧流临时文件（保留共享地图记忆，切换决策不丢失探索数据）
#    保留：map.json（障碍/已探索/资源记忆）、enemy_marks.json（敌情标注）
#    删除：shard_*.jsonl（分片帧流）、latest.json（实时帧）、event_log.json（事件流）、index.json（分片索引）
echo "[switch] 清除帧流临时文件（保留地图记忆）..."
rm -f stream/shard_*.jsonl stream/latest.json stream/event_log.json stream/index.json 2>/dev/null || true
# 确保 stream 目录存在
mkdir -p stream

# 3) 确保地图服务在跑（地图层两决策共用，不随切换变）
if ! lsof -iTCP:8742 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "[switch] 地图服务未运行，启动 com.arenahero.map..."
  launchctl bootstrap "gui/$MYUID" "$MAP_PLIST" 2>/dev/null || true
  launchctl kickstart "gui/$MYUID/com.arenahero.map" 2>/dev/null || true
  sleep 2
fi

# 4) 启动选中的决策 agent
if [ "$MODE" = "evolve" ]; then
  echo "[switch] 启动新决策 (arena-evolve)..."
  launchctl bootstrap "gui/$MYUID" "$AGENT_EVOLVE" 2>/dev/null || true
  launchctl kickstart "gui/$MYUID/com.arenahero.agent.evolve" 2>/dev/null || true
  echo "evolve" > .active_agent
else
  echo "[switch] 启动原决策 (arena_agent.py)..."
  launchctl bootstrap "gui/$MYUID" "$AGENT_LEGACY" 2>/dev/null || true
  launchctl kickstart "gui/$MYUID/com.arenahero.agent" 2>/dev/null || true
  echo "legacy" > .active_agent
fi

sleep 4
echo "[switch] 当前激活: $(cat .active_agent 2>/dev/null)"
echo "[switch] 地图(8742): $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8742/monitor.html --max-time 3)"
echo "[switch] agent 进程: $(pgrep -f 'arena_agent' >/dev/null && echo 在跑 || echo 未启动)"
echo "[switch] launchd 状态:"
launchctl list | grep arenahero || true
echo "--- 完成。浏览器硬刷新 Cmd+Shift+R 查看 monitor ---"
