#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arena Hero 自动游玩 Agent v2（黑暗森林战略版）
==============================================

基于算法设计文档的完整实现：
  * 地图记忆系统（障碍永久、资源迷雾过期、扇区探索）
  * A* 寻路（障碍感知、路径校验与断裂重规划）
  * Worker 一一对应资源分配 + 采集→交付→探索循环
  * 敌方记忆（位置预测、停止/移动状态、追踪与重新获取）
  * 战斗策略（Core 优先打击、爆发攻击点、攻击/撤退平衡、包抄战术）
  * Core 防御（近防区域 + Ranger 环形巡逻）
  * 手动覆盖检测（尊重 Manual 来源命令）
  * 持久化与热更新（跨重启保持地图/敌方/任务记忆）

文档：https://doc.arenahero.io/zh-Hans/
"""

from __future__ import annotations

import heapq
import json
import math
import os
import sys
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from getpass import getpass
from pathlib import Path
from typing import Optional
from uuid import UUID

from arena_hero import (
    ArenaHeroClient,
    BeaconStatus,
    CommandSource,
    Direction,
    UnitType,
    unit_cost,
)

# =========================================================================== #
# 可调参数（环境变量覆盖）
# =========================================================================== #
TARGET_WORKERS = int(os.environ.get("AH_TARGET_WORKERS", "20"))
SPAWN_BUFFER = int(os.environ.get("AH_SPAWN_BUFFER", "2"))      # 生产前预留（早期压低以快速破局）
BEACON_ENABLED = os.environ.get("AH_BEACON", "1") != "0"
BEACON_MIN_POP = int(os.environ.get("AH_BEACON_MIN_POP", "12"))  # 人口达到此数才去捡 Beacon（避免早期浪费工人长途跋涉）
DEFEND = os.environ.get("AH_DEFEND", "1") != "0"
MAX_RANGERS = int(os.environ.get("AH_MAX_RANGERS", "2"))
MAX_VANGUARDS = int(os.environ.get("AH_MAX_VANGUARDS", "4"))
STAT_EVERY = int(os.environ.get("AH_STAT_EVERY", "10"))
PERSISTENCE_FILE = os.environ.get(
    "AH_PERSISTENCE",
    str(Path(__file__).parent / ".arena_state.json"),
)
# 防御参数
CORE_DEFENSE_RADIUS = int(os.environ.get("AH_CORE_DEFENSE_RADIUS", "3"))  # Core 近防范围（曼哈顿）
PATROL_RADIUS = int(os.environ.get("AH_PATROL_RADIUS", "6"))           # 巡逻半径（兼容旧分支）
# 三层巡逻：以 Core 为中心的外/中/内圈巡逻半径（欧氏圆周半径，单位=格）
# 外圈=36×36（半径18）、中圈=20×20（半径10）、内圈=10×10（半径5）
PATROL_RADIUS_OUTER = int(os.environ.get("AH_PATROL_RADIUS_OUTER", "18"))  # 外圈巡逻半径（36×36 范围）
PATROL_RADIUS_MID   = int(os.environ.get("AH_PATROL_RADIUS_MID", "10"))   # 中圈巡逻半径（20×20 范围）
PATROL_RADIUS_INNER = int(os.environ.get("AH_PATROL_RADIUS_INNER", "5"))  # 内圈巡逻半径（10×10 范围）
# 各圈巡逻组数量上限（外3/中2/内1）；战斗单位不足时按上限分配，溢出归入最外圈
PATROL_RING_GROUPS = (int(os.environ.get("AH_PATROL_OUTER_N", "3")),
                      int(os.environ.get("AH_PATROL_MID_N", "2")),
                      int(os.environ.get("AH_PATROL_INNER_N", "1")))
MIN_WORKERS_FOR_COMBAT = int(os.environ.get("AH_MIN_WORKERS_COMBAT", "5"))  # 工人到此数即开始出战斗单位/可主动进攻
ECONOMY_FREEZE_CAPACITY = int(os.environ.get("AH_ECONOMY_FREEZE_CAPACITY", "95"))  # 资源容量达此时冻结生产：记录峰值、只补损失
PATROL_COMBAT_RATIO = float(os.environ.get("AH_PATROL_COMBAT_RATIO", "0.6"))      # 进攻单位>2时，此比例围Core巡逻保护
MARCH_MIN_COMBAT = int(os.environ.get("AH_MARCH_MIN_COMBAT", "5"))               # 进军(信标迁移)门槛：信标开启且先锋/游侠合计>=此数才向信标推进
EXPLORE_MIN_COMBAT = int(os.environ.get("AH_EXPLORE_MIN_COMBAT", "3"))           # 派出进攻单位门槛：战斗单位>2(即>=此数)才去迷雾探索/猎杀；否则全部围Core保护
# 探索组交战：兵力劣势时跟踪保持距离、求援；占优或只有工人/核心时直接进攻
TRACK_STANDOFF = int(os.environ.get("AH_TRACK_STANDOFF", "3"))   # 跟踪时与敌方单位的最小曼哈顿距离（格）
# 死守/封堵/生产触发半径
CORE_BLOCK_RADIUS = int(os.environ.get("AH_CORE_BLOCK_RADIUS", "20"))   # 以Core为中心此曼哈顿范围内出现敌人→所有战斗单位死守core：进攻+封堵，绝不后撤
CORE_RANGER_RADIUS = int(os.environ.get("AH_CORE_RANGER_RADIUS", "10")) # 敌方进入Core此范围内且仍有资源→立即造游侠
CORE_BLOCK_WORKERS = int(os.environ.get("AH_CORE_BLOCK_WORKERS", "4"))  # 威胁出现时抽调来封堵敌方路线的工人数上限
# 仅这些敌方单位类型才算"进攻单位"，会触发 Core 死守逻辑（全员回防+封堵+造游侠）；
# 工人/WORKER、敌方 Core 属于非进攻单位，出现在 20 格内不触发死守（避免无谓调动）。
ENEMY_COMBAT_TYPES = (UnitType.VANGUARD, UnitType.RANGER)
SPIRAL_START = int(os.environ.get("AH_SPIRAL_START", "400"))  # 螺旋起始 idx（仅作兜底，正常走扇区前沿）
# 探索小队参数
EXPLORER_RATIO = float(os.environ.get("AH_EXPLORER_RATIO", "0.25"))              # 专职探索工人占空闲工人的比例
MAX_EXPLORERS = int(os.environ.get("AH_MAX_EXPLORERS", "6"))                     # 专职探索工人上限
MIN_WORKERS_FOR_EXPLORERS = int(os.environ.get("AH_MIN_WORKERS_EXPLORERS", "2"))  # 工人>1 即抽探索工
FRONTIER_RADIUS_SECTORS = int(os.environ.get("AH_FRONTIER_RADIUS_SECTORS", "12"))  # 前沿搜索半径(区块数，旧逻辑保留)
EXPLORER_DETOUR = int(os.environ.get("AH_EXPLORER_DETOUR", "3"))                # 探索工人顺路采的最大半径
EXPLORER_COLLECT_RANGE = int(os.environ.get("AH_EXPLORER_COLLECT_RANGE", "150"))  # 安全时探索工主动去采"记得"的资源的最大距离
SAFE_RADIUS = int(os.environ.get("AH_SAFE_RADIUS", "8"))                        # 附近有敌人在此半径内→不安全，暂停外出采集
COLLECT_ASSIGN_MAX = int(os.environ.get("AH_COLLECT_ASSIGN_MAX", "400"))  # 全局就近派单：采集工被远派去采记忆资源的最大距离(超此不硬派)
# 定向探索：朝固定航向直线外扩，到达半径或受阻时顺时针换航向（替代围绕 Core 的螺旋）
EXPLORER_BASE_RADIUS = int(os.environ.get("AH_EXPLORER_BASE_RADIUS", "20"))      # 基础探索半径
EXPLORER_RADIUS_PER_UNIT = int(os.environ.get("AH_EXPLORER_RADIUS_PER_UNIT", "2"))  # 每多一个单位半径+多少
EXPLORER_MAX_RADIUS = int(os.environ.get("AH_EXPLORER_MAX_RADIUS", "60"))       # 半径上限
EXPLORER_HOME_RADIUS = int(os.environ.get("AH_EXPLORER_HOME_RADIUS", "40"))    # 离家硬围栏：工人离 Core 超过此值改回撤(默认40)
SCOUT_HANDOFF_MAX_DIST = int(os.environ.get("AH_SCOUT_HANDOFF_MAX_DIST", "60"))  # 角色交接：派遣最近的 B 接手探索的最大距离
SCOUT_COMPASS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]  # 8 罗盘方向(顺时针)
# 事件日志（供可视化 UI 回放；设为空字符串可关闭）
EVENT_LOG_PATH = os.environ.get("AH_EVENT_LOG", "event_log.json")
# 单文件实时流（可选；默认关，改用下面的分片流）
EVENT_STREAM_PATH = os.environ.get("AH_EVENT_STREAM", "")
# 实时分片流：每 SHARD_SIZE 帧一个分片文件，仅保留最近 MAX_SHARDS 个（环形缓冲+分片）
STREAM_SHARD_DIR = os.environ.get("AH_STREAM_SHARD_DIR", "stream")
SHARD_SIZE = int(os.environ.get("AH_SHARD_SIZE", "200"))
MAX_SHARDS = int(os.environ.get("AH_MAX_SHARDS", "12"))
# 事件日志内存环形边界（供 event_log.json 全量回放，防止长局无限增长）
MAX_TICKS_MEMORY = int(os.environ.get("AH_MAX_TICKS_MEMORY", "2000"))
STREAM_COMPACT_EVERY = int(os.environ.get("AH_STREAM_COMPACT_EVERY", "500"))
# 地图层持久快照：完整障碍/资源记忆/已探索分片写入 stream/map.json，永不随分片淘汰
MAP_SNAPSHOT_EVERY = int(os.environ.get("AH_MAP_SNAPSHOT_EVERY", "50"))
SECTOR_SIZE = 32

# 敌方"消失点"与三角定位（战争迷雾上的敌情标注）
LOST_CLEAR_RADIUS = int(os.environ.get("AH_LOST_CLEAR_RADIUS", "2"))   # 我方单位到达该格内即视为已 revisit
LOST_CLEAR_ENEMY_R = int(os.environ.get("AH_LOST_CLEAR_ENEMY_R", "6")) # 该格周围无敌人才取消感叹号
CORE_VOTE_RMAX = int(os.environ.get("AH_CORE_VOTE_RMAX", "120"))      # 沿撤退方向投票的最大距离
CORE_ZONE_R = int(os.environ.get("AH_CORE_ZONE_R", "12"))             # 估算敌方核心区域半径
CORE_MIN_RAYS = int(os.environ.get("AH_CORE_MIN_RAYS", "2"))          # 至少多少条射线收敛才标核心区
# 换局检测：重启后当前 Core 与存档中"上次 Core 位置"距离超过此值 → 判定为新对局，
# 旧对局的地图坐标系已失效，必须清空障碍/资源记忆，否则脏数据会污染小地图与寻路
MATCH_RESET_DIST = int(os.environ.get("AH_MATCH_RESET_DIST", "64"))
# 动态 Worker 目标：按已知资源量封顶，贫瘠出生点不再堆 20 个空转工人
DYNAMIC_WORKERS = os.environ.get("AH_DYNAMIC_WORKERS", "1") != "0"

MAX_HP = {UnitType.WORKER: 2, UnitType.VANGUARD: 4, UnitType.RANGER: 2}
BEACON_CELL = (0, 0)
# Core 迁移暂停：带货工人在 Core 周围 ≤ 此曼哈顿距离时，Core 停止迁移等其交付；
# 再配合"追击破局"距离(下见 _plan_core_march)，避免工人永远追不上正在迁移的 Core。
CORE_MARCH_PAUSE_R = int(os.environ.get("AH_CORE_MARCH_PAUSE_R", "5"))  # 带货工人在 Core 周围此格数内才暂停迁移(默认5)
# 视野半径（曼哈顿）：用于把"探索到的区域"精确铺成视野圆盘，而非只有地形格。
# Core 5 / Worker 3 / Vanguard 4 / Ranger 5（与官方文档一致）。Core 是 CoreView，
# 不是 UnitType，单独用 CORE_VISION 常量处理。
VISION_RADIUS = {
    UnitType.WORKER: 3, UnitType.VANGUARD: 4, UnitType.RANGER: 5,
}
CORE_VISION = 5
# 逐扇区探索：探索工先把一个 32×32 扇区填满，才继续向外扩；但不过度外扩。
SECTOR_DONE_RATIO = float(os.environ.get("AH_SECTOR_DONE_RATIO", "0.95"))  # 扇区覆盖率达此即视为"已填满"
EXPLORER_MAX_RING = int(os.environ.get("AH_EXPLORER_MAX_RING", "4"))      # 探索上限环(从 Core 扇区算)，避免一下扩太远
# 信标进军时最小探索环数：确保探索范围至少覆盖到信标方向（否则 Core 远离原点时
# 信标方向的扇区全被 MAX_RING 裁掉，导致探索永远背向信标）
EXPLORER_MIN_BEACON_RING = int(os.environ.get("AH_EXPLORER_MIN_BEACON_RING", "8"))
EXPLORER_SECTOR_MAX_TICKS = int(os.environ.get("AH_EXPLORER_SECTOR_MAX_TICKS", "45"))  # 探索工在同一扇区最长驻留 tick：超时强制释放重选，避免某方向扇区始终达不到"完成"比例→工人永久卡单向
# 进攻敌Core相关：
CORE_ATTACK_SAFE_RADIUS = int(os.environ.get("AH_CORE_ATTACK_SAFE_RADIUS", "20"))  # 敌Core周围此半径内无敌方进攻单位→判定为"可进攻"
GROUP_TRAIL = int(os.environ.get("AH_GROUP_TRAIL", "0"))   # 游侠与先锋的编队间距：=0 时游侠直接叠到先锋所在格(重叠编队，先锋在前当盾，被攻击优先承伤)；>=1 时游侠落后先锋 GROUP_TRAIL 格跟随
# 死亡触发的禁止/危险区域机制（用户策略）：战斗单位阵亡且敌方战斗单位数 > 我方探索战斗组数时，
# 以敌方"推进最远"的战斗单位为圆心、圆心到我方最后死亡单位为半径建立禁止区；禁止区内探索单位绕行；
# 当我方探索战斗组数 > 当时记录的敌方数量 → 禁止区转为危险区，派遣全部探索战斗组压入清场；圆内无敌方进攻单位即解除。
DANGER_ENABLED = os.environ.get("AH_DANGER", "1") != "0"
FORBID_TRIGGER_RATIO = float(os.environ.get("AH_FORBID_RATIO", "1.0"))  # 敌战斗单位数 > 我方探索组数*此值 → 建禁止区（默认严格大于）
DANGER_DISK_MAX_CELLS = int(os.environ.get("AH_DANGER_DISK_MAX", "2500"))  # 禁止区曼哈顿圆盘栅格数上限（防极端半径拖慢寻路）
ATTACK_TARGET_TTL = int(os.environ.get("AH_ATTACK_TARGET_TTL", "60"))  # 可进攻敌Core记忆过期tick

# =========================================================================== #
# 几何工具
# =========================================================================== #

Position = tuple[int, int]


def manhattan(a: Position, b: Position) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def chebyshev(a: Position, b: Position) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def sign(x: int) -> int:
    """返回 -1 / 0 / 1。用于把敌方撤退向量归一成单位方向（三角定位用）。"""
    return 0 if x == 0 else (1 if x > 0 else -1)


DIRECTIONS = list(Direction)
DIR_DELTA = {d: d.delta for d in DIRECTIONS}
DIR_FROM_DELTA = {v: k for k, v in DIR_DELTA.items()}


def neighbors(pos: Position) -> list[tuple[Direction, Position]]:
    return [(d, (pos[0] + dx, pos[1] + dy)) for d, (dx, dy) in DIR_DELTA.items()]


def dir_between(a: Position, b: Position) -> Optional[Direction]:
    """a 到相邻格 b 的方向。"""
    return DIR_FROM_DELTA.get((b[0] - a[0], b[1] - a[1]))


# =========================================================================== #
# A* 寻路（障碍感知）
# =========================================================================== #

def astar(start: Position, goal: Position, obstacles: set[Position],
          max_iter: int = 5000) -> list[Position] | None:
    """A* 寻路，返回从 start 到 goal 的路径（不含起点），None 表示不可达。"""
    if start == goal:
        return []
    if goal in obstacles:
        return None

    open_set: list[tuple[int, int, Position]] = [(manhattan(start, goal), 0, start)]
    came_from: dict[Position, Position] = {}
    g_score: dict[Position, int] = {start: 0}
    open_hash: set[Position] = {start}
    iterations = 0

    while open_set and iterations < max_iter:
        iterations += 1
        _, g, current = heapq.heappop(open_set)
        open_hash.discard(current)

        if current == goal:
            path = []
            while current in came_from:
                path.append(current)
                current = came_from[current]
            path.reverse()
            return path

        for _, nxt in neighbors(current):
            if nxt in obstacles:
                continue
            tentative_g = g + 1
            if tentative_g < g_score.get(nxt, float('inf')):
                came_from[nxt] = current
                g_score[nxt] = tentative_g
                f = tentative_g + manhattan(nxt, goal)
                if nxt not in open_hash:
                    heapq.heappush(open_set, (f, tentative_g, nxt))
                    open_hash.add(nxt)
    return None  # 不可达或超限


def first_step(start: Position, goal: Position,
               obstacles: set[Position]) -> Optional[Direction]:
    """返回从 start 向 goal 走的第一步方向；不可达则返回 None。"""
    path = astar(start, goal, obstacles)
    if not path:
        return None
    return dir_between(start, path[0])


def step_toward_fallback(pos: Position, target: Position,
                          obstacles: set[Position]) -> Optional[Direction]:
    """A* 失败时的贪心回退：尝试任意不踩障碍的方向朝目标走一步。"""
    dx = target[0] - pos[0]
    dy = target[1] - pos[1]
    order = []
    if abs(dx) >= abs(dy):
        if dx > 0: order.append(Direction.RIGHT)
        elif dx < 0: order.append(Direction.LEFT)
        if dy > 0: order.append(Direction.DOWN)
        elif dy < 0: order.append(Direction.UP)
    else:
        if dy > 0: order.append(Direction.DOWN)
        elif dy < 0: order.append(Direction.UP)
        if dx > 0: order.append(Direction.RIGHT)
        elif dx < 0: order.append(Direction.LEFT)
    for d in order:
        nxt = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        if nxt not in obstacles:
            return d
    for d in DIRECTIONS:
        nxt = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        if nxt not in obstacles:
            return d
    return None


def step_toward(pos: Position, target: Position,
                obstacles: set[Position]) -> Optional[Direction]:
    """向目标走一步：优先 A*，失败时贪心回退。"""
    d = first_step(pos, target, obstacles)
    if d:
        return d
    return step_toward_fallback(pos, target, obstacles)


# ---- 工人目标格冲突协调（每格最多 2 个可占位实体；进攻单位不参与，能攻击）---- #
# 每 tick 由 plan_turn_v2 调 reset_move_coord 重置；worker_step 在认领落点时累加 claimed。
_MOVE_COORD: dict = {"occupied": {}, "claimed": {}}
CELL_UNIT_LIMIT = 2


def reset_move_coord(turn) -> None:
    """每 tick 开头重置移动协调表：occupied=我方单位(含 Core)当前各格计数。"""
    occ: dict[Position, int] = {}
    if turn.core is not None:
        cp = turn.core.position
        occ[cp] = occ.get(cp, 0) + 1
    for u in getattr(turn, "units", []):
        occ[u.position] = occ.get(u.position, 0) + 1
    _MOVE_COORD["occupied"] = occ
    _MOVE_COORD["claimed"] = {}


def _coord_can_enter(cell: Position) -> bool:
    """该格本 tick 结束时是否还能进一个我方单位（容量 < 2）。"""
    occ = _MOVE_COORD["occupied"].get(cell, 0)
    clm = _MOVE_COORD["claimed"].get(cell, 0)
    return occ + clm < CELL_UNIT_LIMIT


def _coord_claim(cell: Position) -> None:
    _MOVE_COORD["claimed"][cell] = _MOVE_COORD["claimed"].get(cell, 0) + 1


# 每 Tick 记录我方单位「本 tick 的移动方向」，供 monitor.html 画方向箭头。
# 键=单位 UUID，值=方向 delta (dx,dy)。plan_turn_v2 每 tick 开头清空。
_MOVE_DIRS: dict = {}


def _mv(u, d) -> None:
    """执行一格移动并记录方向（供地图方向箭头）。d 为 None 时跳过。

    注意：内部用 type(u).move(u, d) 调用，避免与下方 replace_all 的 '_mv(u, d)'
    字面量冲突导致递归。
    """
    if d is None:
        return
    try:
        type(u).move(u, d)
    except Exception:
        return
    _MOVE_DIRS[u.id] = d.delta


def worker_step(pos: Position, target: Position,
                 blocked: set[Position]) -> Optional[Direction]:
    """工人走一步：A* 优先；若落点本 tick 容量超限(≥2，即别的人工也认领了)→ 绕路或原地等。

    依据官方"移动与叠加"规则：每格最多 2 个可占位实体；同玩家对象争同格时按 UUID 小者进、
    其余失败。为避免这种不可控失败（下 tick 又重试→卡住），工人提前协调：落点冲突就换条
    路，实在没路就原地等。进攻单位（先锋/游侠）不走这里——它们能攻击，不绕路。
    """
    d = step_toward(pos, target, blocked)
    if d is not None:
        c = (pos[0] + d.delta[0], pos[1] + d.delta[1])
        if _coord_can_enter(c):
            _coord_claim(c)
            return d
    # 落点冲突/被堵 → 试别的方向：朝 target 推进、不踩障碍/敌、不冲突
    best = None
    best_score = None
    for alt in DIRECTIONS:
        c = (pos[0] + alt.delta[0], pos[1] + alt.delta[1])
        if c in blocked or not _coord_can_enter(c):
            continue
        score = manhattan(c, target) - manhattan(pos, target)  # 越负越靠近目标
        if best_score is None or score < best_score:
            best_score = score
            best = alt
    if best is not None:
        _coord_claim((pos[0] + best.delta[0], pos[1] + best.delta[1]))
        return best
    return None  # 没有不冲突的路 → 原地等，避免失败卡死


# =========================================================================== #
# 地图记忆
# =========================================================================== #

class MapMemory:
    """持久化地图记忆：障碍永久有效，资源在迷雾中可能过期。"""

    def __init__(self):
        self.obstacles: set[Position] = set()       # 已确认的障碍格（永久）
        self.resource_memory: dict[Position, int] = {}  # 资源位置 → 最后看到它的 tick
        self.explored_cells: set[Position] = set()  # 已探索过的"逐格"坐标（1×1，精准迷雾）
        self.explored_sectors: set[tuple[int, int]] = set()  # 已探索区块(32×32)，仅供 AI 规划
        self.sector_counts: dict[tuple[int, int], int] = {}  # 每扇区已探索格数（O(1) 算覆盖率）
        self.unreachable_sectors: set[tuple[int, int]] = set()  # 被完全封锁的区块
        self.last_tick: int = 0
        self.last_core: Optional[Position] = None   # 上次记录的 Core 位置（换局检测用）

    def reset_terrain(self) -> None:
        """换局时清空与坐标系绑定的记忆（障碍/资源/已探索），保留结构本身。"""
        self.obstacles.clear()
        self.resource_memory.clear()
        self.explored_cells.clear()
        self.explored_sectors.clear()
        self.sector_counts.clear()
        self.unreachable_sectors.clear()

    def _add_explored(self, p: Position) -> None:
        """登记一个已探索格（幂等）：同步维护逐格集合、扇区集合与扇区计数。"""
        if p in self.explored_cells:
            return
        self.explored_cells.add(p)
        sec = (p[0] // SECTOR_SIZE, p[1] // SECTOR_SIZE)
        self.explored_sectors.add(sec)
        self.sector_counts[sec] = self.sector_counts.get(sec, 0) + 1

    def sector_explored_ratio(self, cx: int, cy: int) -> float:
        """某 32×32 扇区的已探索覆盖率（0~1）。用于"填满扇区才外扩"。"""
        total = SECTOR_SIZE * SECTOR_SIZE
        return min(1.0, self.sector_counts.get((cx, cy), 0) / total)

    def update(self, turn) -> None:
        """每 Tick 用当前视野更新地图记忆。"""
        self.last_tick = turn.tick
        if turn.core is not None:
            self.last_core = tuple(turn.core.position)
        # 障碍永远累加（不会消失）
        self.obstacles.update(turn.obstacle_cells)

        # ---- 先收集本 tick 所有"被照亮"的格子（用于资源过期检测）----
        _this_vision: set[Position] = set()
        def _mark_vision(center, radius):
            cx, cy = center
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if abs(dx) + abs(dy) <= radius:
                        cell = (cx + dx, cy + dy)
                        _this_vision.add(cell)
                        self._add_explored(cell)

        for u in getattr(turn, "units", ()):  # 每个我方单位照亮周围一圈
            p = tuple(u.position)
            _this_vision.add(p)
            self._add_explored(p)             # 单位脚下格（地形 batch 可能不含空格）
            _mark_vision(p, VISION_RADIUS.get(u.unit_type, 3))
        if turn.core is not None:
            p = tuple(turn.core.position)
            _this_vision.add(p)
            self._add_explored(p)
            _mark_vision(p, CORE_VISION)
        # 地形 batch：补充登记（含障碍/资源所在格，部分可能落在视野边缘外）
        for obj in turn.terrain:
            for p in obj.positions:
                self._add_explored(tuple(p))

        # ---- 资源记忆更新 ----
        visible_resources = turn.resource_cells
        visible_res_set = set(visible_resources)
        # 当前可见的资源 → 刷新时间戳
        for p in visible_resources:
            self.resource_memory[p] = turn.tick
        # 清除"记得有资源但本 Tick 视野内已确认无资源"的脏数据。
        # 这修复了资源被采走/耗尽后地图上仍显示绿色菱形的问题——
        # 不需要等工人亲自走到那格才清除，视野覆盖即足够验证。
        if _this_vision:
            stale = [p for p in self.resource_memory
                     if p in _this_vision and p not in visible_res_set]
            for p in stale:
                self.resource_memory.pop(p, None)

    def get_known_resources(self, current_tick: int,
                            max_age: int = 24) -> set[Position]:
        """返回仍可信的资源点（最近 N 个 Tick 内见过）。仅用于统计等次要用途。"""
        return {p for p, t in self.resource_memory.items()
                if current_tick - t <= max_age}

    def get_remembered_resources(self) -> set[Position]:
        """返回全部"记得有资源"的坐标 —— 一旦看到就永久记住，直到被采空/被别人采。

        设计意图：资源点离开视野后不靠"重新探索"才能再利用，空闲工人可直接锁定
        这些记忆坐标去采集。过期（max_age）只用于辅助判断是否"新鲜"，不参与派遣，
        否则会出现用户反馈的现象：离开视野 24 tick 后资源被遗忘、工人又得重探一遍。
        """
        return set(self.resource_memory.keys())

    def mark_resource_depleted(self, pos: Position) -> None:
        """标记某个资源点已被消耗（从记忆中移除）。"""
        self.resource_memory.pop(pos, None)

    def sector_for(self, pos: Position) -> tuple[int, int]:
        return pos[0] // 32, pos[1] // 32

    def to_dict(self) -> dict:
        return {
            "obstacles": [list(p) for p in self.obstacles],
            "resource_memory": {f"{p[0]},{p[1]}": t for p, t in self.resource_memory.items()},
            "explored_cells": [list(p) for p in self.explored_cells],
            "explored_sectors": [list(s) for s in self.explored_sectors],
            "unreachable_sectors": [list(s) for s in self.unreachable_sectors],
            "last_tick": self.last_tick,
            "last_core": list(self.last_core) if self.last_core else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MapMemory:
        m = cls()
        m.obstacles = {tuple(p) for p in data.get("obstacles", [])}
        m.resource_memory = {
            tuple(int(x) for x in k.split(",")): v
            for k, v in data.get("resource_memory", {}).items()
        }
        m.explored_cells = {tuple(p) for p in data.get("explored_cells", [])}
        m.explored_sectors = {tuple(s) for s in data.get("explored_sectors", [])}
        for p in m.explored_cells:
            sec = (p[0] // SECTOR_SIZE, p[1] // SECTOR_SIZE)
            m.sector_counts[sec] = m.sector_counts.get(sec, 0) + 1
        m.unreachable_sectors = {tuple(s) for s in data.get("unreachable_sectors", [])}
        m.last_tick = data.get("last_tick", 0)
        lc = data.get("last_core")
        m.last_core = tuple(lc) if lc else None
        return m


# =========================================================================== #
# 敌方记忆
# =========================================================================== #

class EnemyStatus(Enum):
    UNKNOWN = "unknown"
    STOPPED = "stopped"      # 停止（可能在防守/埋伏）
    MOVING = "moving"        # 移动中（潜在威胁）


@dataclass
class EnemyRecord:
    id: UUID
    unit_type: UnitType
    last_pos: Position
    last_seen_tick: int
    hp: int
    status: EnemyStatus = EnemyStatus.UNKNOWN
    prev_pos: Optional[Position] = None   # 上一次位置（用于预测移动方向）
    predicted_pos: Optional[Position] = None  # 预测的当前位置
    threat_level: float = 1.0             # 威胁等级


class EnemyMemory:
    """敌方单位记忆：位置、状态预测、威胁评估。

    额外维护两类"敌情标注"，画在战争迷雾上：
      - lost_contacts：敌方单位"可见→不可见"的消失点（橙色 !）。一旦某敌方单位
        从视野里消失，就把它最后出现的位置记下来；直到我方单位到达该格、且周围
        视野里没有敌人时，才取消这个感叹号。
      - core_zones：由多个同阵营消失点的"撤退方向"投票反推的敌方核心大致区域
        （橙色半透明块）。需要 >=CORE_MIN_RAYS 条射线才输出，单点只画 !。
    """

    def __init__(self):
        self.records: dict[UUID, EnemyRecord] = {}
        # 消失点：{"pos":[x,y], "faction":str, "unit_type":str,
        #         "dir":[dx,dy]|None, "tick":int}
        self.lost_contacts: list[dict] = []
        self._lost_eids: set[UUID] = set()   # 已登记过消失点的敌方 id，防止重复添加
        self._last_tick: int = 0

    def update(self, turn) -> None:
        """用当前可见敌人更新记忆，并检测"消失点"。"""
        self._last_tick = turn.tick
        current_ids: set[UUID] = set()
        prev_ids = set(self.records.keys())
        our_core = tuple(turn.core.position) if turn.core is not None else None
        for enemy in turn.visible_enemies:
            eid = enemy.id
            current_ids.add(eid)
            self._lost_eids.discard(eid)     # 重新出现 → 不再是"丢失"
            ut = enemy.unit_type if hasattr(enemy, 'unit_type') else UnitType.WORKER

            if eid in self.records:
                rec = self.records[eid]
                rec.prev_pos = rec.last_pos
                rec.last_pos = enemy.position
                rec.last_seen_tick = turn.tick
                rec.hp = enemy.hp
                # 更新移动状态
                if rec.prev_pos and rec.prev_pos != enemy.position:
                    rec.status = EnemyStatus.MOVING
                    dx = enemy.position[0] - rec.prev_pos[0]
                    dy = enemy.position[1] - rec.prev_pos[1]
                    rec.predicted_pos = (enemy.position[0] + dx,
                                         enemy.position[1] + dy)
                else:
                    rec.status = EnemyStatus.STOPPED
                    rec.predicted_pos = enemy.position
            else:
                self.records[eid] = EnemyRecord(
                    id=eid, unit_type=ut, last_pos=enemy.position,
                    last_seen_tick=turn.tick, hp=enemy.hp,
                )

        # 检测"消失"：上一 tick 可见、本 tick 不可见 → 登记消失点（橙色 !）
        for eid in prev_ids - current_ids:
            if eid in self._lost_eids:
                continue     # 已经登记过，避免每 tick 重复添加
            rec = self.records.get(eid)
            if rec is None:
                continue
            p = tuple(rec.last_pos)
            # 撤退方向：优先用上一帧→本帧的移动向量；拿不到则用"远离我方核心"的方向
            d = None
            if rec.prev_pos and rec.prev_pos != p:
                d = (sign(p[0] - rec.prev_pos[0]), sign(p[1] - rec.prev_pos[1]))
            elif our_core is not None:
                d = (sign(p[0] - our_core[0]), sign(p[1] - our_core[1]))
            self._lost_eids.add(eid)
            self._add_lost_contact(p, rec.unit_type, d)

        # 过期清理：超过 60 Tick 未见的记录降级（消失点已独立保留，不受影响）
        expired = [eid for eid, r in self.records.items()
                   if eid not in current_ids and turn.tick - r.last_seen_tick > 60]
        for eid in expired:
            self.records.pop(eid, None)
            self._lost_eids.discard(eid)

    def _add_lost_contact(self, pos: Position, unit_type, direction) -> None:
        """登记一个消失点。同阵营、同格(容差1)不重复登记。"""
        faction = "enemy"
        for lc in self.lost_contacts:   # type: ignore[union-attr]
            if lc["faction"] == faction and manhattan(lc["pos"], pos) <= 1:
                lc["tick"] = self._last_tick
                return
        self.lost_contacts.append({
            "pos": [pos[0], pos[1]],
            "faction": faction,
            "unit_type": unit_type.value if hasattr(unit_type, "value") else str(unit_type),
            "dir": list(direction) if direction else None,
            "tick": self._last_tick,
        })

    def clear_resolved_contacts(self, turn) -> None:
        """我方单位到达消失点且周围视野无敌人时，取消对应的感叹号。"""
        our_units = [tuple(u.position) for u in getattr(turn, "units", ())]
        enemies_pos = [tuple(e.position) for e in getattr(turn, "visible_enemies", ())]
        if not our_units:
            return
        kept = []
        for lc in self.lost_contacts:
            pos = tuple(lc["pos"])
            visited = any(manhattan(pos, up) <= LOST_CLEAR_RADIUS for up in our_units)
            if not visited:
                kept.append(lc)
                continue
            # 到达该格后，若周围仍有敌人 → 保留感叹号；否则取消
            enemy_near = any(manhattan(pos, ep) <= LOST_CLEAR_ENEMY_R for ep in enemies_pos)
            if enemy_near:
                kept.append(lc)
        self.lost_contacts = kept

    def estimate_core_zones(self, our_core: Optional[Position]) -> list[dict]:
        """由多个同阵营消失点的撤退方向投票，反推敌方核心大致区域。

        算法：每个消失点 P 沿其撤退方向 d 投射一条射线，沿线按距离衰减累加投票；
        得票最高的格即"最可能的敌方核心"。仅当 >=CORE_MIN_RAYS 条射线都穿过
        该区域（收敛）时才输出橙色区域，避免单点误报。
        """
        rays = []   # (pos, dir)
        for lc in self.lost_contacts:
            p = tuple(lc["pos"])
            d = tuple(lc["dir"]) if lc.get("dir") else None
            if d is None and our_core is not None:
                d = (sign(p[0] - our_core[0]), sign(p[1] - our_core[1]))
            if d is None or (d[0] == 0 and d[1] == 0):
                continue
            rays.append((p, d))
        if len(rays) < CORE_MIN_RAYS:
            return []     # 单点不估算核心区（只画 ! 即可）

        # 计数投票：每格被多少条射线穿过。汇聚点计数值最高 ——
        # 用"条数"而非"距离衰减权重"，避免单条射线起点附近(权重≈1)盖过三方汇聚点。
        counts: dict[tuple[int, int], int] = {}
        weights: dict[tuple[int, int], float] = {}
        for p, d in rays:
            for k in range(1, CORE_VOTE_RMAX + 1):
                cx = p[0] + d[0] * k
                cy = p[1] + d[1] * k
                key = (cx, cy)
                counts[key] = counts.get(key, 0) + 1
                weights[key] = weights.get(key, 0.0) + 1.0 / (1 + k * 0.05)
        if not counts:
            return []

        # 主排序=穿过条数(汇聚优先)，次排序=衰减权重和(同条数时往中段靠)
        best = max(counts, key=lambda c: (counts[c], weights[c]))
        hit = counts[best]
        if hit < CORE_MIN_RAYS:
            return []     # 射线未收敛，不画区域
        return [{
            "pos": [best[0], best[1]],
            "radius": CORE_ZONE_R,
            "confidence": hit,
        }]

    def get_nearby_enemies(self, pos: Position, radius: int,
                           tick: int) -> list[EnemyRecord]:
        """返回 pos 半径内（含预测位置）的所有已知敌人。"""
        result = []
        for rec in self.records.values():
            effective_pos = rec.predicted_pos or rec.last_pos
            if manhattan(pos, effective_pos) <= radius:
                # 根据时间衰减置信度
                age = tick - rec.last_seen_tick
                if age <= 30:  # 30 Tick 内的记忆视为有效
                    result.append(rec)
        return result

    def get_enemy_cores(self) -> list[EnemyRecord]:
        """返回已知的敌方 Core 记录。"""
        return [r for r in self.records.values()
                if r.unit_type == UnitType.WORKER and False]  # TODO: 区分 Core

    def to_dict(self) -> dict:
        return {
            "records": {
                str(r.id): {
                    "unit_type": r.unit_type.value,
                    "last_pos": list(r.last_pos),
                    "last_seen_tick": r.last_seen_tick,
                    "hp": r.hp,
                    "status": r.status.value,
                    "prev_pos": list(r.prev_pos) if r.prev_pos else None,
                    "predicted_pos": list(r.predicted_pos) if r.predicted_pos else None,
                }
                for r in self.records.values()
            },
            "lost_contacts": [
                {
                    "pos": lc["pos"],
                    "faction": lc["faction"],
                    "unit_type": lc["unit_type"],
                    "dir": lc["dir"],
                    "tick": lc["tick"],
                }
                for lc in self.lost_contacts
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> EnemyMemory:
        em = cls()
        for eid_str, ed in data.get("records", {}).items():
            ut = UnitType(ed["unit_type"])
            em.records[UUID(eid_str)] = EnemyRecord(
                id=UUID(eid_str), unit_type=ut,
                last_pos=tuple(ed["last_pos"]),
                last_seen_tick=ed["last_seen_tick"],
                hp=ed["hp"],
                status=EnemyStatus(ed["status"]),
                prev_pos=tuple(ed["prev_pos"]) if ed.get("prev_pos") else None,
                predicted_pos=tuple(ed["predicted_pos"]) if ed.get("predicted_pos") else None,
            )
        for lc in data.get("lost_contacts", []):
            em.lost_contacts.append({
                "pos": list(lc["pos"]),
                "faction": lc.get("faction", "enemy"),
                "unit_type": lc.get("unit_type", "WORKER"),
                "dir": list(lc["dir"]) if lc.get("dir") else None,
                "tick": lc.get("tick", 0),
            })
        return em


# =========================================================================== #
# Worker 任务状态
# =========================================================================== #

class WorkerTask(Enum):
    IDLE = "idle"
    HARVESTING = "harvesting"     # 前往 / 正在采集资源点
    DELIVERING = "delivering"     # 带着 Cargo 回 Core
    EXPLORING = "exploring"       # 探索新区域
    FLEEING = "fleeing"           # 受伤撤退
    BEACON_FETCH = "beacon_fetch" # 去(0,0)捡 Beacon


@dataclass
class WorkerState:
    task: WorkerTask = WorkerTask.IDLE
    target: Optional[Position] = None  # 当前目标坐标
    assigned_resource: Optional[Position] = None  # 一一对应的资源点
    ticks_on_task: int = 0  # 在当前任务上花费的 Tick 数
    # 探索用：方螺旋覆盖（空间填充），保证扫过核心周边区域而非一条细线
    explore_waypoint: Optional[Position] = None
    spiral_idx: int = 0
    explore_sector: Optional[tuple[int, int]] = None  # 专职探索工人被分配的未探索扇区
    explore_sector_since: int = 0  # 当前探索扇区被分配时的 tick（配合 EXPLORER_SECTOR_MAX_TICKS 超时释放）
    # 定向探索：固定航向直线外扩，到达半径或受阻时顺时针换航向
    scout_heading: Optional[tuple[int, int]] = None   # 罗盘单位向量
    scout_origin: Optional[Position] = None          # 本条探索腿起点
    scout_radius: int = 0                              # 本条腿半径
    scout_target: Optional[Position] = None          # 本条腿目标点
    # 采集工兜底游走目标（在已探索扇区里巡游，独立于采集目标）
    loiter_target: Optional[Position] = None
    # 角色交接：探索工 A 发现资源去送货时，派 B 接手 A 的探索、A 送完接手 B 原职责
    is_scout: bool = False              # 角色标记（持久）：本工人当前是探索工。
                                        # explorer_ids 只挑 cargo==0 的空手工人，
                                        # 探索工一背货就会掉出集合，所以角色必须自己记住，
                                        # 否则"带货返程时发起交接"这条规则永远触发不了。
    takeover_active: bool = False       # 本工人正接手别人的探索职责（按探索工处理）
    leg_swapped: bool = False           # 本工人本轮已发起交接（避免重复，提交完重置）
    pending_target: Optional[Position] = None  # 提交完要去接手的目标(B 的原职责)
    # 卡死检测：连续 N tick 位置没变化（移动被服务端拒绝/无路）→ 强制换探索航向，
    # 避免死胡同里"出来一格又进去"的来回横跳。
    last_pos: Optional[Position] = None
    stuck_ticks: int = 0
    # 封堵记忆：工人占据敌方朝 Core 前格后原地卡死，避免每 tick 被敌方位置牵着往前挪。
    # 仅当被封堵敌方改变推进方向（朝 Core 的 step_toward delta 变化）时才重新占位拦截。
    blockade_cell: Optional[Position] = None          # 当前封堵占据的格
    blockade_enemy_dir: Optional[tuple] = None        # 封堵时敌方朝 Core 的推进方向(delta)


# =========================================================================== #
# 扇区探索
# =========================================================================== #

SECTOR_DIRECTIONS = [
    ("NE", (1, -1)),   # 右上
    ("SE", (1, 1)),    # 右下
    ("SW", (-1, 1)),   # 左下
    ("NW", (-1, -1)),  # 左上
]


def spiral_offset(idx: int) -> tuple[int, int]:
    """返回以核心为原点的方螺旋第 idx 格的坐标偏移（R,U,L,D 交替，边长 1,1,2,2,3,3...）。

    方螺旋是空间填充曲线，能让 Worker 沿其行进时以视野半径扫过核心周边每一格，
    从而抓住稀疏分布的资源点（直线射线会漏掉）。
    """
    if idx <= 0:
        return (0, 0)
    x = y = 0
    consumed = 0
    di = 0
    dirs = [(1, 0), (0, 1), (-1, 0), (0, -1)]  # R, U, L, D
    k = 1
    while consumed < idx:
        length = k
        dx, dy = dirs[di % 4]
        take = min(length, idx - consumed)
        x += dx * take
        y += dy * take
        consumed += take
        di += 1
        if di % 2 == 0:
            k += 1
    return (x, y)


def frontier_sectors(map_mem: MapMemory, core_pos: Position) -> list[tuple[int, int]]:
    """返回从已探索区域向外扩展的"前沿扇区"列表。

    候选 = 已探索扇区(32x32)的相邻扇区中、尚未探索、且在搜索半径内、且非已知不可达者。
    排序：离核心近优先，叠加"朝原点(0,0)方向"余弦相似度加权——让探索主动向
    beacon / 疑似敌方核心方向推进（黑暗森林主动侦察）。
    """
    if core_pos is None:
        return []
    ccx, ccy = map_mem.sector_for(core_pos)
    explored = map_mem.explored_sectors
    if not explored:
        explored = {(ccx, ccy)}
    frontier: set[tuple[int, int]] = set()
    for (cx, cy) in explored:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                ns = (cx + dx, cy + dy)
                if ns in explored or ns in map_mem.unreachable_sectors:
                    continue
                if max(abs(ns[0] - ccx), abs(ns[1] - ccy)) <= FRONTIER_RADIUS_SECTORS:
                    frontier.add(ns)

    def _centroid(s):
        return (s[0] * 32 + 16, s[1] * 32 + 16)

    # 核心→原点(0,0) 的单位方向（核心在原点附近时无偏置，退化为纯距离）
    ox, oy = -ccx, -ccy
    on = math.hypot(ox, oy) or 1.0
    ux, uy = ox / on, oy / on

    def _key(s):
        sx, sy = s[0] - ccx, s[1] - ccy
        sn = math.hypot(sx, sy) or 1.0
        align = (sx / sn) * ux + (sy / sn) * uy  # 1=正朝原点, -1=背离
        return manhattan(core_pos, _centroid(s)) - 4 * align

    return sorted(frontier, key=_key)


def _scout_target_from(origin: Position, heading: tuple[int, int],
                       radius: int) -> Position:
    """从起点沿航向走 radius 格的目标点（对角航向按切比雪夫归一）。"""
    m = max(abs(heading[0]), abs(heading[1])) or 1
    ux, uy = heading[0] / m, heading[1] / m
    return (origin[0] + round(ux * radius), origin[1] + round(uy * radius))


def _worker_index(w, workers) -> int:
    for i, ww in enumerate(workers):
        if ww.id == w.id:
            return i
    return 0


def _find_handoff_partner(w, anchor: Position, workers, worker_states,
                          max_dist: int):
    """找最近的"空闲"探索/采集工人 B（不带货、无已分配资源、未在交接），用于接手 A 的探索。
    anchor = A 的探索目标(即 B 要去接手的区域)；返回 B 或 None。"""
    best = None
    best_d = max_dist + 1
    for ww in workers:
        if ww.id == w.id:
            continue
        if getattr(ww, "cargo", 0) > 0:
            continue  # 正在送货
        wws = worker_states.get(ww.id)
        if not wws:
            continue
        if wws.assigned_resource is not None:
            continue  # 正在采
        if wws.leg_swapped:
            continue  # 别人本轮也在交接
        d = manhattan(anchor, ww.position)
        if d < best_d:
            best_d = d
            best = ww
    return best


def _nearby_lower_used_headings(w, pos: Position, workers, worker_states,
                                 avoid_radius: int) -> set:
    """更低索引、且在身边的工人当前已选的航向集合（高索引者应避让，防并行同向）。"""
    used: set = set()
    my_idx = _worker_index(w, workers)
    for ww in workers:
        if ww.id == w.id:
            continue
        if _worker_index(ww, workers) >= my_idx:
            continue  # 只让给更低索引，避免双方互让死锁
        if manhattan(pos, ww.position) > avoid_radius:
            continue
        ows = worker_states.get(ww.id)
        if ows and ows.scout_heading:
            used.add(ows.scout_heading)
    return used


def _avoid_radius(map_mem: MapMemory, num_units: int) -> int:
    """同向避让范围：按"已探索面积 / 单位数"的平均间距算，探索越广、单位越多越宽松。
    返回 [6, 30] 的曼哈顿半径。"""
    explored_area = len(map_mem.explored_sectors) * 1024  # 每扇区 32x32 格
    spacing = math.sqrt(max(explored_area, 1) / max(num_units, 1))
    return int(min(30, max(6, 0.5 * spacing)))


def _harvest_radius(map_mem: MapMemory, core_pos: Optional[Position]) -> int:
    """采集工巡逻半径：取已探索扇区相对 Core 扇区的最大环号推算，带合理上下限，
    让采集工停在已探索区里巡逻（不外扩去暗区），又不挤在 Core 一格。"""
    secs = map_mem.explored_sectors
    if not secs or core_pos is None:
        return SECTOR_SIZE
    ccx, ccy = core_pos[0] // SECTOR_SIZE, core_pos[1] // SECTOR_SIZE
    maxr = 0
    for (sx, sy) in secs:
        maxr = max(maxr, max(abs(sx - ccx), abs(sy - ccy)))
    return max(SECTOR_SIZE, min((maxr + 1) * SECTOR_SIZE // 2, 3 * SECTOR_SIZE))


def _bearing_unexplored_fraction(pos: Position, heading: tuple[int, int],
                                  map_mem: MapMemory, max_dist: int) -> float:
    """沿航向采样若干距离，返回落在未探索扇区的比例（探索工优先去暗区）。"""
    if not map_mem.explored_sectors:
        return 1.0
    unexp = 0
    total = 0
    for dist in (max(1, max_dist // 3), max(1, max_dist // 2), max(1, max_dist)):
        cell = (pos[0] + heading[0] * dist, pos[1] + heading[1] * dist)
        sec = (cell[0] // 32, cell[1] // 32)
        total += 1
        if sec not in map_mem.explored_sectors:
            unexp += 1
    return unexp / max(total, 1)


def _choose_scout_heading(pos: Position, w, workers, map_mem: MapMemory,
                          base_idx: int, avoid_radius: int,
                          worker_states, blocked: Optional[set] = None,
                          exclude: Optional[tuple] = None,
                          prev_heading: Optional[tuple] = None,
                          toward_core: Optional[Position] = None) -> tuple[int, int]:
    """选一条"别跟别的工人撞、尽量朝暗区、且不朝死胡同(相邻格是障碍)"的航向。

    评分 = 拥堵(身边前向半圆内别人)×3 + (1-未探索占比)×1 + 离基础航向角差×0.15
           + 身边低索引已用同向×6（强制避让）
           + 相邻格是障碍(死胡同)×5（朝墙走必卡，重罚）
           + 与 exclude 同向×8（卡住换航向时绝不重选刚卡住的那个方向）
           + 航向动量偏差×2（rechoose 时偏好与上一航向相近的方向，防跨地图跳目标）
           + 远离Core惩罚×4（围栏超限时传入 toward_core，惩罚远离Core的航向）。
    """
    others = [ww.position for ww in workers if ww.id != w.id]
    used = _nearby_lower_used_headings(w, pos, workers, worker_states, avoid_radius)
    n = max(len(workers), 1)
    base_angle = 2 * math.pi * base_idx / n
    base_ci = round(base_angle / (2 * math.pi / 8)) % 8
    best_h = SCOUT_COMPASS[base_ci]
    best_score = None
    for ci, h in enumerate(SCOUT_COMPASS):
        cong = 0.0
        for op in others:
            dx = op[0] - pos[0]
            dy = op[1] - pos[1]
            d = abs(dx) + abs(dy)
            if d == 0 or d > avoid_radius:
                continue
            dot = dx * h[0] + dy * h[1]
            if dot > 0:  # 前向半圆 → 会撞/并行走
                cong += 1.0 - d / (avoid_radius + 1)
        unexp = _bearing_unexplored_fraction(pos, h, map_mem, avoid_radius)
        ang_diff = min((ci - base_ci) % 8, (base_ci - ci) % 8)  # 0..4
        used_pen = 6.0 if h in used else 0.0
        # 死胡同：朝该航向走一步就撞障碍 → 必卡，重罚
        block_pen = 0.0
        if blocked:
            nc = (pos[0] + h[0], pos[1] + h[1])
            if nc in blocked:
                block_pen = 5.0
        exc_pen = 8.0 if (exclude is not None and h == exclude) else 0.0
        # 航向动量：rechoose 时偏好与上一航向相近的方向（±90°内无惩罚，
        # ±135°轻惩罚，反向重惩罚），防止跨地图跳目标
        mom_pen = 0.0
        if prev_heading is not None:
            pm = max(abs(prev_heading[0]), abs(prev_heading[1])) or 1
            pux, puy = prev_heading[0] / pm, prev_heading[1] / pm
            # 点积：1=同向, 0=垂直, -1=反向
            dot_p = pux * h[0]/max(abs(h[0]),1) + puy * h[1]/max(abs(h[1]),1)
            if dot_p < -0.5:   # 反向（≥135°）：重惩罚
                mom_pen = 4.0
            elif dot_p < 0.3:   # 大角度偏转（90°~135°）：中惩罚
                mom_pen = 1.5
            # dot_p >= 0.3：相近方向（≤~70°），无惩罚，保持动量
        # 围栏切向约束：超限时惩罚远离 Core 的航向（鼓励沿围栏探索或微回撤）
        core_pen = 0.0
        if toward_core is not None:
            dx_c = pos[0] - toward_core[0]
            dy_c = pos[1] - toward_core[1]
            dc = max(abs(dx_c), abs(dy_c)) or 1
            # Core 方向单位向量（从当前位置指向 Core）
            cx, cy = -dx_c / dc, -dy_c / dc
            # 航向与"指向Core"方向的点积：负值=远离Core
            dot_c = cx * (h[0]/max(abs(h[0]),1)) + cy * (h[1]/max(abs(h[1]),1))
            if dot_c < -0.3:   # 明显远离 Core → 重惩罚
                core_pen = 4.0
            elif dot_c < 0.1:  # 切向/微远离 → 轻惩罚
                core_pen = 1.0
            # dot_c >= 0.1：朝向或切向Core → 无惩罚
        score = (cong * 3.0 + (1 - unexp) * 1.0 + ang_diff * 0.15
                 + used_pen + block_pen + exc_pen + mom_pen + core_pen)
        if best_score is None or score < best_score - 1e-9:
            best_score = score
            best_h = h
    return best_h


def _scout_init_leg(ws: WorkerState, w, pos: Position, radius: int,
                    workers, map_mem: MapMemory, avoid_radius: int,
                    worker_states, blocked: Optional[set] = None) -> None:
    idx = _worker_index(w, workers)
    ws.scout_heading = _choose_scout_heading(pos, w, workers, map_mem, idx,
                                              avoid_radius, worker_states, blocked)
    ws.scout_origin = pos
    ws.scout_radius = radius
    ws.scout_target = _scout_target_from(pos, ws.scout_heading, radius)


def _scout_rechoose(ws: WorkerState, w, pos: Position, radius: int,
                    workers, map_mem: MapMemory, avoid_radius: int,
                    worker_states, blocked: Optional[set] = None,
                    exclude: Optional[tuple] = None,
                    prev_heading: Optional[tuple] = None,
                    toward_core: Optional[Position] = None) -> None:
    """到达/受阻时重选航向（避开别的人工已占方向、优先朝未探索区、不朝死胡同）。
    exclude：卡死换航向时传入"刚卡住的航向"，强制选一个不同的方向。
    prev_heading：上一条航向，用于动量偏好（避免跨地图跳目标）。
    toward_core：围栏超限时传入 Core 坐标，惩罚远离Core的航向（沿围栏切向探索）。"""
    idx = _worker_index(w, workers)
    ws.scout_heading = _choose_scout_heading(pos, w, workers, map_mem, idx,
                                              avoid_radius, worker_states, blocked,
                                              exclude, prev_heading, toward_core)
    ws.scout_origin = pos
    ws.scout_radius = radius
    ws.scout_target = _scout_target_from(pos, ws.scout_heading, radius)


def _should_yield_heading(w, pos: Position, heading: tuple[int, int],
                          workers, worker_states, avoid_radius: int) -> bool:
    """身边是否有更低索引工人同航向 → 高索引者应让路重选（防并行同向漂移）。"""
    my_idx = _worker_index(w, workers)
    for ww in workers:
        if ww.id == w.id:
            continue
        if _worker_index(ww, workers) >= my_idx:
            continue
        if manhattan(pos, ww.position) > avoid_radius:
            continue
        ows = worker_states.get(ww.id)
        if ows and ows.scout_heading == heading:
            return True
    return False


def _plan_directional_explore(ws: WorkerState, w, pos: Position,
                               core_pos: Position, blocked: set[Position],
                               workers, map_mem: MapMemory,
                               worker_states) -> None:
    """探索工：朝固定航向直线外扩；到达半径/受阻/身边同向时重选航向
    （避开别的人工已占方向、优先朝未探索区、不朝死胡同）。单位越多探索半径越大。

    卡死保护：连续 N tick 位置没变化（移动被服务端拒绝/无路可走）→ 强制换一个
    *不同* 的航向（exclude 当前航向），避免死胡同里"出来一格又进去"的来回横跳。
    """
    ws.task = WorkerTask.EXPLORING
    num_units = len(workers)
    radius = min(EXPLORER_MAX_RADIUS,
                 EXPLORER_BASE_RADIUS + EXPLORER_RADIUS_PER_UNIT * num_units)
    avoid_radius = _avoid_radius(map_mem, num_units)
    # 离家硬围栏：工人离 Core 超过阈值 → 不回家，改为沿围栏切向继续探索。
    # 旧决策探索是"从当前位置接力外扩"，本无离家上限；此围栏把最远离家
    # 距离锁死在 EXPLORER_HOME_RADIUS 内。超限时清掉外扩腿、重新选一条不远离
    # Core 的航向（优先切向/微回撤方向），让工人沿围栏边缘持续铺图而非空跑回家。
    if core_pos and manhattan(pos, core_pos) > EXPLORER_HOME_RADIUS:
        ws.scout_target = None
        ws.scout_origin = None
        _scout_rechoose(ws, w, pos, radius, workers, map_mem, avoid_radius,
                        worker_states, blocked,
                        prev_heading=ws.scout_heading,
                        toward_core=core_pos)
        ws.last_pos = tuple(pos)
        return
    # 卡死检测：用上一 tick 记录的位置判断本 tick 是否真的移动了
    if ws.last_pos is not None and ws.last_pos == tuple(pos):
        ws.stuck_ticks += 1
    else:
        ws.stuck_ticks = 0
    if ws.scout_target is None or ws.scout_heading is None:
        _scout_init_leg(ws, w, pos, radius, workers, map_mem, avoid_radius, worker_states, blocked)
    # 让路：身边同向、更低索引工人优先 → 高索引者重选，避免两两并行走
    if ws.scout_heading and _should_yield_heading(w, pos, ws.scout_heading,
                                                   workers, worker_states, avoid_radius):
        _scout_rechoose(ws, w, pos, radius, workers, map_mem, avoid_radius, worker_states, blocked,
                        prev_heading=ws.scout_heading)
    # 卡死 → 换一个不同的航向（绝不重选刚卡住的那个方向）
    if ws.stuck_ticks >= 2:
        ws.stuck_ticks = 0
        ws.takeover_active = False
        _scout_rechoose(ws, w, pos, radius, workers, map_mem, avoid_radius,
                        worker_states, blocked, exclude=ws.scout_heading,
                        prev_heading=ws.scout_heading)
    reached = ws.scout_target is not None and manhattan(pos, ws.scout_target) <= 1
    overshot = (ws.scout_origin is not None
                and manhattan(pos, ws.scout_origin) >= max(ws.scout_radius, 1))
    if reached or overshot:
        # 接手的这条腿走完了 → 释放"接手"标记，重新回到正常的探索/采集分配池，
        # 否则被派遣过的工人会永久锁定成探索工，探采比例逐渐失衡。
        ws.takeover_active = False
        _scout_rechoose(ws, w, pos, radius, workers, map_mem, avoid_radius, worker_states, blocked,
                        prev_heading=ws.scout_heading)
    tgt = ws.scout_target
    d = worker_step(pos, tgt, blocked)
    if d:
        _mv(w, d)
        ws.target = tgt
    else:
        # 朝当前航向无路（死胡同/被障碍封死）→ 换一个不同的开放航向，破局
        _scout_rechoose(ws, w, pos, radius, workers, map_mem, avoid_radius,
                        worker_states, blocked, exclude=ws.scout_heading,
                        prev_heading=ws.scout_heading)
        ws.target = ws.scout_target
    ws.last_pos = tuple(pos)  # 记录本 tick 位置供下 tick 卡死检测


def _plan_harvester_loiter(ws: WorkerState, w, pos: Position,
                            core_pos: Position, blocked: set[Position],
                            map_mem: MapMemory, workers,
                            worker_states,
                            used_resources: Optional[set[Position]] = None) -> None:
    """采集工：在已探索区里按"绕 Core 的扇形"分派巡逻点，各自负责一块
    （32×32 区域平分给每个采集单位），避免所有采集工挤向同一个大方向（如都往左）。

    做法：把已探索扇区按"相对 Core 的极角"排序，第 i 个采集工取第 i 个角度的扇区中心
    （优先其中记忆资源）作为巡逻点；若该方向近处已有别的采集工，顺延到下一个方向
    （朝其他方向移动）。完全没已探索扇区时才退化为定向探索（极早期）。"""
    if not map_mem.explored_sectors:
        _plan_directional_explore(ws, w, pos, core_pos, blocked, workers, map_mem, worker_states)
        return
    ws.task = WorkerTask.EXPLORING  # 复用标签；EventLog 会显示"已探索区游走采集"
    # 区分采集工（非探索工）：只有"采集单位"参与 32×32 区域平分与互相避让
    harvesters = [ww for ww in workers
                  if not (worker_states.get(ww.id) and worker_states[ww.id].is_scout)]
    if not harvesters:
        harvesters = list(workers)
    n = max(len(harvesters), 1)
    i = harvesters.index(w) if w in harvesters else 0
    # 每个采集工平分 32×32 区域；避让距离取该区域边长量级（同区/邻域都算"撞区"）
    avoid = max(5, int(math.sqrt(max(1, (SECTOR_SIZE * SECTOR_SIZE) // n))))
    # 已探索扇区中心按"相对 Core 的极角"排序 → 不同采集工取不同角度扇区（上下左右散开）
    csec = (core_pos[0] // SECTOR_SIZE, core_pos[1] // SECTOR_SIZE)
    secs = sorted(map_mem.explored_sectors,
                  key=lambda s: (math.atan2((s[1] - csec[1]), (s[0] - csec[0])) + math.pi))
    res_mem = map_mem.resource_memory

    def pick(k: int) -> Position:
        """第 k 个方向的巡逻目标：该方向已探索扇区中心（优先其中记忆资源）；
        多人共一扇区时按索引错开落点，避免完全重叠。
        排除已被其他工人占用的资源（used_resources），避免游走点撞上别人正在采的目标。"""
        sec = secs[k % len(secs)]
        cx = sec[0] * SECTOR_SIZE + 16
        cy = sec[1] * SECTOR_SIZE + 16
        in_sec = [r for r in res_mem
                  if r not in (used_resources or set())
                  and r[0] // SECTOR_SIZE == sec[0] and r[1] // SECTOR_SIZE == sec[1]]
        if in_sec:
            return min(in_sec, key=lambda r: manhattan(r, (cx, cy)))
        off = ((k // len(secs)) * 10) % (SECTOR_SIZE - 4)  # 共扇区时错开落点
        return (cx + off - 6, cy + off - 6)

    # 优先用我自己的方向 i；该方向近处有别的采集工 → 顺延到其他方向（朝其他方向移动）
    tgt = None
    for off in range(n):
        cand = pick(i + off)
        crowded = any(manhattan(ww.position, cand) <= avoid
                      for ww in harvesters if ww.id != w.id)
        if not crowded:
            tgt = cand
            break
    if tgt is None:
        tgt = pick(i)
    # 平滑：到达、或当前目标被别的采集工占了才重选，平时保持巡逻点（不每 tick 抖）
    if (ws.loiter_target is None or manhattan(pos, ws.loiter_target) <= 1
            or any(manhattan(ww.position, ws.loiter_target) <= avoid
                   for ww in harvesters if ww.id != w.id)):
        ws.loiter_target = tgt
    ws.target = ws.loiter_target  # 同步目标坐标，供地图虚线展示（避免残留旧资源目标）
    d = worker_step(pos, ws.loiter_target, blocked)
    if d:
        _mv(w, d)


def _axis_of(c: int) -> int:
    """环号轴向坐标：>=0 不变，<0 取 -c-1（让原点两侧对称，符合官方环定义）。"""
    return c if c >= 0 else -c - 1

def _ring_of(cx: int, cy: int, core_cx: int, core_cy: int) -> int:
    """扇区 (cx,cy) 相对 Core 扇区的环号（官方：axis 之和）。"""
    return _axis_of(cx - core_cx) + _axis_of(cy - core_cy)


def _dir_bucket_of(cx: int, cy: int, core_cx: int, core_cy: int) -> tuple[int, int]:
    """以 Core 扇区为原点的 8 向分桶（含中心），用于探索方向均衡：
    (0,0)=中心, (±1,0)=东西, (0,±1)=南北, (±1,±1)=四对角。"""
    dx = cx - core_cx
    dy = cy - core_cy
    if dx == 0 and dy == 0:
        return (0, 0)
    return (1 if dx > 0 else (-1 if dx < 0 else 0),
            1 if dy > 0 else (-1 if dy < 0 else 0))

def _choose_frontier_sector(map_mem: MapMemory, pos: Position,
                            core_pos: Optional[Position],
                            claimed_sectors: Optional[set] = None,
                            beacon_dirs: Optional[set] = None,
                            bias_beacon: bool = False,
                            forbidden_zones: Optional[list] = None
                            ) -> Optional[tuple[int, int]]:
    """选下一个要填满的 32×32 扇区。

    策略：优先最近的、尚未填满的扇区；候选来自「已探索扇区的 ±1 邻域」（逐环外扩，
    不跳格），并允许 EXPLORER_MAX_RING 环内的所有扇区作为兜底（防止邻域被障碍封死后
    无路可走）。排序按 (环号升序, 距当前Core近者优先, 该方向已认领数升序, 迷雾浓者优先,
    距探索者近者优先) → 由内到外、逐环填满，且**绕 Core 四周均衡散开**（方向均衡优先于
    工人当前位置），不会一下把探索工甩到地图最远端、也不会单向偏置。
    claimed_sectors：本 tick 已被其他探索工认领的扇区，直接跳过 → 探索工各自认领不同
    扇区/不同方向，扇形散开而不是全挤向同一个最近前沿扇区。
    """
    explored = map_mem.explored_sectors
    if not explored:
        if core_pos is None:
            return (0, 0)
        return (core_pos[0] // SECTOR_SIZE, core_pos[1] // SECTOR_SIZE)
    core_cx = core_pos[0] // SECTOR_SIZE if core_pos else 0
    core_cy = core_pos[1] // SECTOR_SIZE if core_pos else 0
    # 各 8 向已探索扇区数（方向均衡依据）：某方向已探得越多 → 越不优先，
    # 让探索自然绕 Core 四周补齐欠探索方向，而不是被工人当前位置或历史探索拖向单一方向。
    _dir_count: dict[tuple[int, int], int] = {}
    for (ecx, ecy) in explored:
        b = _dir_bucket_of(ecx, ecy, core_cx, core_cy)
        _dir_count[b] = _dir_count.get(b, 0) + 1
    cands: set[tuple[int, int]] = set()
    for (ecx, ecy) in explored:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cands.add((ecx + dx, ecy + dy))
    # 基础范围：Core 周边 EXPLORER_MAX_RING 环内
    for cx in range(core_cx - EXPLORER_MAX_RING, core_cx + EXPLORER_MAX_RING + 1):
        for cy in range(core_cy - EXPLORER_MAX_RING, core_cy + EXPLORER_MAX_RING + 1):
            if _ring_of(cx, cy, core_cx, core_cy) <= EXPLORER_MAX_RING:
                cands.add((cx, cy))
    # 信标进军扩展：当 beacon_dirs 激活时，额外加入信标方向上的远距离扇区
    # （修复 Core 远离原点时信标方向扇区被 MAX_RING 裁掉导致探索背向信标的 bug）
    if beacon_dirs and bias_beacon:
        # 计算需要扩展到的最小环数：至少覆盖到信标所在方向
        # 信标在 (0,0)，Core 在 core_pos → 扇区偏移
        beacon_sx = 1 if 0 > (core_pos[0] if core_pos else 0) else (-1 if 0 < (core_pos[0] if core_pos else 0) else 0)
        beacon_sy = 1 if 0 > (core_pos[1] if core_pos else 0) else (-1 if 0 < (core_pos[1] if core_pos else 0) else 0)
        # 扩展环数：取 MAX_RING 和 MIN_BEACON_RING 中较大的有效范围
        ext_ring = max(EXPLORER_MAX_RING, EXPLORER_MIN_BEACON_RING)
        for cx in range(core_cx - ext_ring, core_cx + ext_ring + 1):
            for cy in range(core_cy - ext_ring, core_cy + ext_ring + 1):
                if _ring_of(cx, cy, core_cx, core_cy) <= ext_ring:
                    # 只加入信标主方向桶内的扇区（避免全向扩太远）
                    b = _dir_bucket_of(cx, cy, core_cx, core_cy)
                    if b in beacon_dirs:
                        cands.add((cx, cy))
    valid = []
    for (cx, cy) in cands:
        if claimed_sectors and (cx, cy) in claimed_sectors:
            continue
        ratio = map_mem.sector_explored_ratio(cx, cy)
        if ratio >= SECTOR_DONE_RATIO:
            continue
        ring = _ring_of(cx, cy, core_cx, core_cy)
        # 扇区中心坐标
        sec_cx = cx * SECTOR_SIZE + SECTOR_SIZE // 2
        sec_cy = cy * SECTOR_SIZE + SECTOR_SIZE // 2
        # 禁止区规避：扇区中心落在任一"禁止"状态危险区内 → 跳过该扇区（探索不向禁止区铺开）
        if forbidden_zones:
            _sc = (sec_cx, sec_cy)
            if any(z.state == "forbidden" and manhattan(_sc, z.center) <= z.radius
                   for z in forbidden_zones):
                continue
        d_core = manhattan(core_pos, (sec_cx, sec_cy)) if core_pos else 0
        d_expl = abs(cx * SECTOR_SIZE - pos[0]) + abs(cy * SECTOR_SIZE - pos[1])
        # 方向分桶（以 Core 扇区为原点，8 向 + 中心）：用于环内方向均衡
        bucket = _dir_bucket_of(cx, cy, core_cx, core_cy)
        # 该方向本 tick 已被其他探索工认领的扇区数（无 claimed_sectors 时为 0）
        claimed_in_dir = 0
        if claimed_sectors:
            claimed_in_dir = sum(1 for (ccx, ccy) in claimed_sectors
                                 if _dir_bucket_of(ccx, ccy, core_cx, core_cy) == bucket)
        # 该方向已探索扇区数（地图级方向均衡）：越大说明该方向已探得越多 → 越不优先，
        # 让探索绕 Core 四周补齐欠探索方向，根治"单向偏上"。
        dir_count = _dir_count.get(bucket, 0)
        # 信标进军偏置：被标记 bias_beacon 的单位，优先选朝向信标的主方向桶扇区；
        # in_primary=0 在前、1 在后，从而 80% 探索产能压向信标方向（仍限 EXPLORER_MAX_RING），
        # 非主方向扇区作为兜底（主方向扇区填满后才退回去补），不浪费单位。
        in_primary = 0 if (beacon_dirs and bias_beacon and bucket in beacon_dirs) else 1
        if beacon_dirs and bias_beacon:
            # 排序：① 主方向桶优先(in_primary)；② 由内到外逐环(ring)；③ 同环靠近当前
            #   Core(d_core)；④ 主方向内各方向已探越少越优先(dir_count)→在右/下/右下三向
            #   间自均衡散开、不挤单方向；⑤ 同 tick 该方向认领越少越优先(claimed_in_dir)；
            #   ⑥ 迷雾浓(ratio)；⑦ 最后 d_expl 弱就近。
            valid.append((in_primary, ring, d_core, dir_count, claimed_in_dir, ratio, d_expl, (cx, cy)))
        else:
            # 排序：① 由内到外逐环(ring)；② 同环优先靠近当前Core(d_core)；
            # ③ 该方向已探索越少越优先(dir_count) → 绕 Core 四周自均衡散开；
            # ④ 同 tick 该方向本被认领越少越优先(claimed_in_dir) → 多探索工各自错开方向；
            # ⑤ 优先未探索比例高(迷雾浓)的扇区(ratio)；⑥ 最后才是 d_expl 弱就近
            #    （仅在所有方向都均衡时才起作用，不再造成方向偏置）。
            valid.append((ring, d_core, dir_count, claimed_in_dir, ratio, d_expl, (cx, cy)))
    if not valid:
        return None  # 范围内都已填满
    valid.sort()
    return valid[0][-1]   # 末位即扇区坐标(cx, cy)，兼容偏置分支多出的排序字段

def _pick_unexplored_in_sector(map_mem: MapMemory, sec: tuple[int, int],
                               pos: Position, blocked: set[Position]) -> Optional[Position]:
    """在目标扇区内挑最近的、尚未探索且当前可通行的格作为下一格目标。"""
    x0, y0 = sec[0] * SECTOR_SIZE, sec[1] * SECTOR_SIZE
    best = None
    best_d = 1e9
    for x in range(x0, x0 + SECTOR_SIZE):
        for y in range(y0, y0 + SECTOR_SIZE):
            if (x, y) in map_mem.explored_cells:
                continue
            if (x, y) in blocked:
                continue
            d = abs(x - pos[0]) + abs(y - pos[1])
            if d < best_d:
                best_d = d
                best = (x, y)
    return best

def _plan_sector_explore(ws: WorkerState, w, pos: Position, core_pos: Position,
                         blocked: set[Position], map_mem: MapMemory,
                         claimed_sectors: Optional[set] = None,
                         tick: int = 0,
                         beacon_dirs: Optional[set] = None,
                         bias_beacon: bool = False,
                         forbidden_zones: Optional[list] = None) -> None:
    """逐扇区探索：先把当前承诺的 32×32 扇区填满，才换下一个扇区；不放过暗区、
    也不过度外扩（限 EXPLORER_MAX_RING 环）。claimed_sectors 让多个探索工各自认领
    不同前沿扇区（扇形散开），而不是全挤向同一个最近扇区。

    EXPLORER_SECTOR_MAX_TICKS 超时：探索工在同一扇区驻留过久（该扇区始终达不到
    "完成"比例，例如工人被反复调走/卡在细路径）→ 强制释放重选。重选时
    _choose_frontier_sector 会优先欠探索方向，从而绕 Core 四周散开，不再单向卡死。"""
    ws.task = WorkerTask.EXPLORING
    sec = ws.explore_sector
    # 已承诺扇区填满、被本 tick 别的探索工认领、或驻留超时 → 释放，下一 tick 重选
    if sec is not None:
        if map_mem.sector_explored_ratio(sec[0], sec[1]) >= SECTOR_DONE_RATIO:
            sec = None
        elif claimed_sectors is not None and sec in claimed_sectors:
            sec = None
        elif tick >= EXPLORER_SECTOR_MAX_TICKS and \
                (tick - ws.explore_sector_since) >= EXPLORER_SECTOR_MAX_TICKS:
            sec = None
    if sec is None:
        ws.explore_sector_since = tick
        sec = _choose_frontier_sector(map_mem, pos, core_pos, claimed_sectors,
                                      beacon_dirs=beacon_dirs, bias_beacon=bias_beacon,
                                      forbidden_zones=forbidden_zones)
        if sec is None:
            # 范围内全满/被认领完：退化为朝 Core 方向移动（不再硬外扩，避免乱跑）
            ws.target = core_pos
            d = worker_step(pos, core_pos, blocked)
            if d:
                _mv(w, d)
            return
        ws.explore_sector = sec
    # 认领本扇区，避免同 tick 其他探索工重复选它
    if claimed_sectors is not None:
        claimed_sectors.add(sec)
    # 锁定既有侦察目标：只要它仍未被探索、未到达、未被障碍/敌人封死，就持续朝它走，
    # 避免每 tick 重选"最近未探索格"在已探索区来回打转（优先朝战争迷雾方向推进）。
    cell = None
    st = ws.scout_target
    if st is not None and st != pos and st not in map_mem.explored_cells and st not in blocked:
        cell = st
    if cell is None:
        cell = _pick_unexplored_in_sector(map_mem, sec, pos, blocked)
        if cell is None:
            # 该扇区已无可探格（全障碍/已探索）→ 视为填满，释放
            ws.explore_sector = None
            ws.scout_target = None
            return
        ws.scout_target = cell
    ws.target = cell
    d = worker_step(pos, cell, blocked)
    if d:
        _mv(w, d)
    else:
        ws.scout_target = None  # 被障碍/敌人封死 → 重选扇区

def plan_explore(ws: WorkerState, w, pos: Position, core_pos: Position,
                obstacles: set[Position], blocked: set[Position], workers,
                map_mem: MapMemory, is_explorer: bool,
                worker_states, claimed_sectors: Optional[set] = None,
                tick: int = 0,
                beacon_dirs: Optional[set] = None,
                bias_beacon: bool = False,
                forbidden_zones: Optional[list] = None) -> None:
    """探索工：先填满一个 32×32 扇区，再逐环向外扩（不过度外扩）。claimed_sectors
    让多个探索工各自认领不同前沿扇区，扇形散开。tick 用于探索扇区驻留超时释放。
    beacon_dirs/bias_beacon：信标进军期 80% 探索工偏向信标方向展开（范围仍受约束）。
    forbidden_zones：死亡建立的禁止区，探索扇区选择会跳过落入其中的扇区。"""
    _plan_sector_explore(ws, w, pos, core_pos, blocked, map_mem, claimed_sectors, tick,
                         beacon_dirs=beacon_dirs, bias_beacon=bias_beacon,
                         forbidden_zones=forbidden_zones)

# =========================================================================== #
# 战斗管理
# =========================================================================== #

@dataclass
class CombatState:
    engaging: bool = False              # 是否正在交战
    primary_target_core: Optional[EnemyRecord] = None  # 主攻敌方 Core
    retreat_mode: bool = False           # 是否处于撤退模式
    assault_force_size: int = 0          # 当前投入的进攻兵力
    last_combat_tick: int = 0


def assess_threat(turn, combat: CombatState, enemy_mem: EnemyMemory) -> float:
    """评估当前面临的综合威胁等级（0-10）。"""
    if not turn.visible_enemies and not enemy_mem.records:
        return 0.0
    threat = 0.0
    for e in turn.visible_enemies:
        dist_to_core = manhattan(e.position, turn.core.position) if turn.core else 99
        if dist_to_core <= CORE_DEFENSE_RADIUS:
            threat += 3.0
        elif dist_to_core <= PATROL_RADIUS:
            threat += 1.0
        # Vanguard/Ranger 威胁更高
        ut = e.unit_type if hasattr(e, 'unit_type') else UnitType.WORKER
        if ut == UnitType.VANGUARD:
            threat += 1.5
        elif ut == UnitType.RANGER:
            threat += 2.0
    return min(threat, 10.0)


def should_retreat(turn, combat: CombatState, enemy_mem: EnemyMemory) -> bool:
    """判断是否应进入撤退模式。"""
    if turn.core is None:
        return True
    # Core 血量过低 → 撤退
    if turn.core.hp <= 2:
        return True
    # 我方战斗单位过少且敌人较多
    combat_units = len(turn.vanguards) + len(turn.rangers)
    enemy_count = len(turn.visible_enemies)
    if combat_units < 2 and enemy_count >= 3:
        return True
    # 进攻中损失惨重
    if combat.engaging and combat.assault_force_size > 0:
        alive_combat = sum(1 for u in turn.units
                           if u.unit_type != UnitType.WORKER and u.hp > 0)
        if alive_combat < combat.assault_force_size * 0.3:
            return True
    return False


# =========================================================================== #
# Core 防御巡逻
# =========================================================================== #

@dataclass
class PatrolState:
    patrol_units: set[UUID] = field(default_factory=set)  # 正在巡逻的 Ranger ID
    patrol_targets: dict[UUID, Position] = field(default_factory=dict)  # 每个巡逻者的目标点


def assign_patrol_targets(turn, patrol: PatrolState, core_pos: Position) -> None:
    """为 Ranger 分配绕 Core 的环形巡逻目标，逐 tick 缓慢旋转。"""
    if core_pos is None:
        return
    # 纳入所有未超上限的 Ranger（已加入的也每 tick 重算目标，保证真正旋转）
    rangers = list(turn.rangers)[:MAX_RANGERS]
    n = max(len(rangers), 1)
    for i, ranger in enumerate(rangers):
        angle = 2 * math.pi * i / n + turn.tick * 0.05
        tx = core_pos[0] + round(PATROL_RADIUS * math.cos(angle))
        ty = core_pos[1] + round(PATROL_RADIUS * math.sin(angle))
        patrol.patrol_units.add(ranger.id)
        patrol.patrol_targets[ranger.id] = (tx, ty)


# =========================================================================== #
# 手动覆盖检测
# =========================================================================== #

@dataclass
class ManualOverride:
    """跟踪哪些 Unit 被 Manual 来源占用了（上一 Tick 的 received 信息）。"""
    overridden_units: set[UUID] = field(default_factory=set)
    overridden_core: bool = False

    def update_from_receipt(self, game) -> None:
        """从 latest_receipts 中读取 MANUAL 来源的计划，标记被手动控制的 Unit。"""
        self.overridden_units.clear()
        self.overridden_core = False
        manual_receipt = game.latest_receipts.get(CommandSource.MANUAL)
        if manual_receipt is None:
            return
        plan = getattr(manual_receipt, 'plan', None)
        if plan is None:
            return
        # Manual 计划里有动作的 Unit 视为被手动控制
        for uid in getattr(plan, 'unit_actions', {}):
            self.overridden_units.add(uid if isinstance(uid, UUID) else UUID(str(uid)))
        if getattr(plan, 'core_action', None) is not None:
            self.overridden_core = True


# =========================================================================== #
# 持久化
# =========================================================================== #

class Persistence:
    @staticmethod
    def save(map_mem: MapMemory, enemy_mem: EnemyMemory,
             filepath: str = PERSISTENCE_FILE) -> None:
        try:
            data = {
                "map": map_mem.to_dict(),
                "enemies": enemy_mem.to_dict(),
                "saved_at": time.time(),
            }
            tmp = filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, filepath)  # 原子替换
        except Exception:
            pass  # 持久化失败不应影响游戏

    @staticmethod
    def load(filepath: str = PERSISTENCE_FILE) -> tuple[MapMemory, EnemyMemory]:
        try:
            if not os.path.exists(filepath):
                return MapMemory(), EnemyMemory()
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return (
                MapMemory.from_dict(data.get("map", {})),
                EnemyMemory.from_dict(data.get("enemies", {})),
            )
        except Exception:
            return MapMemory(), EnemyMemory()


# =========================================================================== #
# Worker 决策：一一对应资源分配 + 采集→交付→探索循环
# =========================================================================== #

def plan_worker_collect(w, ws: WorkerState, turn, map_mem: MapMemory,
                        core_pos: Position, obstacles: set[Position],
                        used_resources: set[Position],
                        enemy_cells: set[Position],
                        is_explorer: bool = False,
                        worker_states=None,
                        claimed_sectors: Optional[set] = None,
                        beacon_dirs: Optional[set] = None,
                        bias_explorer: bool = False,
                        forbidden_cells: Optional[set] = None) -> None:
    """单个 Worker 的采集/交付/探索决策（核心循环）。

    forbidden_cells：死亡建立的"禁止区"栅格集合；仅探索工人（含接手工）需绕行，
    资源采集工人逻辑不变（仍绕 Core 36×36 寻找采集），故只在此类单位上并入寻路障碍。
    """
    pos = w.position
    # 移动障碍 = 地形障碍 + 当前/近期敌方单位（让 A* 自动绕开敌人，避免被卡死）
    blocked = obstacles | enemy_cells
    # 探索工人（含接手工）需绕开禁止区（资源工人逻辑不变，不并入）
    if forbidden_cells and (is_explorer or ws.takeover_active):
        blocked = blocked | forbidden_cells
    has_cargo = w.cargo > 0
    is_hurt = w.hp < MAX_HP.get(UnitType.WORKER, 2)

    # ---- 优先级 1：受伤且不在 Core 格 → 撤回 Core ----
    if is_hurt and pos != core_pos:
        ws.task = WorkerTask.FLEEING
        ws.target = core_pos
        d = worker_step(pos, core_pos, blocked)
        if d:
            _mv(w, d)
        return

    # ---- 优先级 2：带着 Cargo 且 Core 有空间 → 交付 ----
    if has_cargo:
        # 角色交接：探索工/接手工 A 首次拿到货 → 派最近的 B 接手 A 的探索目标
        if ((is_explorer or ws.takeover_active) and not ws.leg_swapped
                and ws.scout_target is not None):
            partner = _find_handoff_partner(w, ws.scout_target, turn.workers,
                                            worker_states, SCOUT_HANDOFF_MAX_DIST)
            if partner is not None:
                pws = worker_states.get(partner.id)
                if pws is not None:
                    b_old = pws.scout_target
                    pws.scout_target = ws.scout_target
                    pws.scout_origin = partner.position
                    nb = _dir_toward(partner.position, ws.scout_target)
                    if nb:
                        pws.scout_heading = nb
                    pws.takeover_active = True        # B 接手探索职责
                    pws.is_scout = True               # 角色标记同步，避免下轮被当采集工
                    pws.loiter_target = None          # 清掉 B 原来的游走目标
                    ws.pending_target = b_old         # A 送完去接手 B 的原职责
                    ws.leg_swapped = True
                    ws.scout_target = None            # A 送货期间不占用该目标，避免与 B 撞
        if pos == core_pos:
            w.deposit()
            ws.task = WorkerTask.IDLE
            ws.target = None
            ws.assigned_resource = None
            # 提交完 → 接手 B 的原职责
            if ws.leg_swapped:
                ws.leg_swapped = False
                if ws.pending_target is not None:
                    ws.scout_target = ws.pending_target
                    ws.scout_origin = pos
                    nb = _dir_toward(pos, ws.pending_target)
                    if nb:
                        ws.scout_heading = nb
                    ws.takeover_active = True
                    ws.pending_target = None
        else:
            ws.task = WorkerTask.DELIVERING
            ws.target = core_pos
            d = worker_step(pos, core_pos, blocked)
            if d:
                _mv(w, d)
        return

    # ---- 优先级 3：站在资源点上 → 采集 ----
    if pos in turn.resource_cells:
        w.harvest()
        ws.task = WorkerTask.HARVESTING
        ws.assigned_resource = pos
        used_resources.add(pos)
        return

    # ---- 专职探索工人(含接手工)：安全情况下采集资源优先级最高 ----
    if is_explorer or ws.takeover_active:
        # 总规则：安全时采集资源优先级最高。视野内或"记得"的资源（离开视野也有效）
        # 都优先去采，不再只顺路采极近的；只有确实没有可采资源（或附近有敌不安全）
        # 才继续外扩探索。这样"已探索并标记的资源"会被最近的空背包工人优先采掉，
        # 而不是让工人一直往前走把资源晾着。
        safe = _worker_safe(pos, enemy_cells)
        cand = [r for r in turn.resource_cells if r not in used_resources]
        if not cand and safe:
            known = map_mem.get_remembered_resources()
            cand = [r for r in known
                    if r not in used_resources
                    and manhattan(pos, r) <= EXPLORER_COLLECT_RANGE]
        if cand:
            best = min(cand, key=lambda r: manhattan(pos, r))
            ws.assigned_resource = best
            used_resources.add(best)
            ws.task = WorkerTask.HARVESTING
            ws.target = best
            d = worker_step(pos, best, blocked)
            if d:
                _mv(w, d)
            return
        plan_explore(ws, w, pos, core_pos, obstacles, blocked,
                     turn.workers, map_mem, True, worker_states, claimed_sectors, turn.tick,
                     beacon_dirs=beacon_dirs, bias_beacon=bias_explorer)
        return

    # ---- 优先级 4：有已分配的目标资源点 → 前往 ----
    if ws.assigned_resource and ws.assigned_resource not in used_resources:
        target = ws.assigned_resource
        # 资源坐标一旦看到就永久记忆（不过期）。只要还"记得这格有资源"就值得去；
        # 走到发现没了（被采空/被别人采）→ 清除记忆并重新分配。
        if target in map_mem.resource_memory:
            if pos == target and target not in turn.resource_cells:
                # 到了却没资源（已采空/被别人采走）→ 清记忆，落入下方重新选点
                map_mem.mark_resource_depleted(target)
                ws.assigned_resource = None
                ws.target = None  # 清除旧目标，避免虚线指向已采空资源
            else:
                used_resources.add(target)
                ws.task = WorkerTask.HARVESTING
                ws.target = target
                d = worker_step(pos, target, blocked)
                if d:
                    _mv(w, d)
                return
        else:
            # 目标已失效（记忆里也没了，等于已被采空），清除分配
            ws.assigned_resource = None
            ws.target = None  # 清除旧目标，避免虚线指向失效资源

    # ---- 优先级 5：寻找新的未占用资源点（一一对应分配） ----
    # 视野内优先（在视野内通常安全）；视野内没有时，用记忆坐标直接锁定去采，
    # 但不安全（附近有敌人）时不长途跋涉去采记忆资源，交给战斗/防御逻辑处理。
    candidates = [r for r in turn.resource_cells if r not in used_resources]
    if not candidates and _worker_safe(pos, enemy_cells):
        remembered = map_mem.get_remembered_resources()
        candidates = [r for r in remembered if r not in used_resources]

    if candidates:
        # 选最近的
        best = min(candidates, key=lambda r: manhattan(pos, r))
        ws.assigned_resource = best
        used_resources.add(best)
        ws.task = WorkerTask.HARVESTING
        ws.target = best
        d = worker_step(pos, best, blocked)
        if d:
            _mv(w, d)
        return

    # ---- 优先级 6：没有可用资源点 ----
    # 探索工(含接手工)：定向外扩点亮暗区；采集工：在已探索区游走（不外扩），路过资源转采集
    if is_explorer or ws.takeover_active:
        plan_explore(ws, w, pos, core_pos, obstacles, blocked, turn.workers,
                     map_mem, True, worker_states, claimed_sectors, turn.tick,
                     beacon_dirs=beacon_dirs, bias_beacon=bias_explorer)
    else:
        _plan_harvester_loiter(ws, w, pos, core_pos, blocked, map_mem,
                               turn.workers, worker_states, used_resources)


def plan_global_resource_assignment(turn, state, carrier_id, enemy_cells,
                                   used_resources: set[Position]) -> None:
    """全局就近派单：把地图上的资源点(视野内 + 已记忆)指派给最近的空背包工人去采。

    解决的问题：之前采集工只在"已探索扇区游走巡逻"，遇到记忆资源并不主动前往，
    导致小地图上明明标着绿色资源图标、工人却空手路过/原地乱走、资源一直晾着。
    现在每 tick 做一次全局最优匹配——每个资源点配最近的空背包工人(贪心)，
    保证"地图上有资源图标时，最近的空闲工人优先去采"。

    两条规则：
    - 视野内资源必然安全、优先派(站在资源上的工人会优先就近接单)；
    - 记忆资源仅当目标周围无可见敌人时才远派，避免送货上门送人头。
    探索工不在此全局派单内(保留其探索职责，它们自己会在安全时顺路采)，
    这样既清空地图资源、又不至于完全停掉探索。已带 Cargo / 已派单的工人不会被覆盖。
    """
    mm = state.map_memory
    # 候选工人：空背包、非搬运工、非手动控制、尚未被分配资源(不覆盖已有目标)
    workers = [w for w in turn.workers
               if w.cargo == 0
               and w.id != carrier_id
               and w.id not in state.manual.overridden_units
               and state.worker_states.get(w.id, WorkerState()).assigned_resource is None]
    if not workers:
        return
    # 目标点：视野内资源(安全) + 周围无可见敌人的记忆资源，排除已被占用
    targets = list(turn.resource_cells)
    for r in mm.get_remembered_resources():
        if r in turn.resource_cells or r in used_resources:
            continue
        if any(manhattan(r, e) <= SAFE_RADIUS for e in enemy_cells):
            continue
        targets.append(r)
    if not targets:
        return
    # 已被其他工人"持久占用"的资源（来自之前 tick 的 assigned_resource）→ 不再派给
    # 空闲工人。这样"一个单位已在前往某资源时，其余单位继续做自己原本的事"，不会
    # 出现截图里所有空闲工人都涌向同一资源的重复占用。注意：保护的是"别的工人"的
    # 目标，不拦工人自身（自身目标在 plan_worker_collect 优先级4 正常继续前往）。
    already_claimed = {ws.assigned_resource
                       for ws in state.worker_states.values()
                       if ws.assigned_resource is not None}
    # 贪心最近匹配：每个目标配最近的、尚未派单的工人；超距离不硬派(留给顺路/探索)
    # 同时跳过已被占用(本 tick 已认领/其他工人持久占用)的目标
    for t in targets:
        if t in used_resources or t in already_claimed:
            continue
        cand = [w for w in workers
                if state.worker_states.get(w.id, WorkerState()).assigned_resource is None
                and manhattan(w.position, t) <= COLLECT_ASSIGN_MAX]
        if not cand:
            continue
        best = min(cand, key=lambda w: manhattan(w.position, t))
        ws = state.worker_states.setdefault(best.id, WorkerState())
        ws.assigned_resource = t
        ws.target = t
        used_resources.add(t)  # 立即标记占用，防止后续目标复用同一工人或资源


# =========================================================================== #
# Beacon 管理
# =========================================================================== #

def plan_beacon_worker(turn, carrier_state: dict, obstacles: set[Position],
                       enemy_cells: set[Position]) -> Optional[UUID]:
    """管理 Champion Beacon 搬运工。返回被派去捡 Beacon 的 Worker ID 或 None。"""
    beacon = turn.beacon

    # Beacon 掉回地面 → 重置
    if beacon.status == BeaconStatus.GROUND:
        carrier_state["carrying"] = False

    if not BEACON_ENABLED:
        return None

    # 早期人口不足时不长途去捡 Beacon（715 格外跋涉会拖垮经济）
    if BEACON_MIN_POP and turn.state.population < BEACON_MIN_POP:
        return None

    cid = carrier_state.get("id")
    if beacon.status == BeaconStatus.GROUND and cid is None and turn.workers:
        # 派最近的空闲 Worker
        carrier = min(turn.workers, key=lambda w: manhattan(w.position, BEACON_CELL))
        carrier_state["id"] = carrier.id
        cid = carrier.id

    if cid is None:
        return None

    carrier = next((w for w in turn.workers if w.id == cid), None)
    if carrier is None:
        carrier_state["id"] = None
        carrier_state["carrying"] = False
        return None

    pos = carrier.position
    if pos == BEACON_CELL and beacon.status == BeaconStatus.GROUND:
        carrier.pickup_beacon()
        carrier_state["carrying"] = True
        carrier_state["id"] = None
        return cid

    d = worker_step(pos, BEACON_CELL, obstacles | enemy_cells)
    if d:
        _mv(carrier, d)
    return cid


# =========================================================================== #
# 战斗决策
# =========================================================================== #

def plan_combat(turn, combat: CombatState, enemy_mem: EnemyMemory,
                obstacles: set[Position], manual: ManualOverride,
                enemy_cells: set[Position]) -> None:
    """战斗策略：Core 优先打击、爆发攻击、撤退判断。"""
    if not DEFEND or not turn.visible_enemies:
        combat.engaging = False
        return

    enemies = list(turn.visible_enemies)
    core_pos = turn.core.position if turn.core else None

    # 评估是否应撤退
    if should_retreat(turn, combat, enemy_mem):
        combat.retreat_mode = True
        combat.engaging = False
        # 所有战斗单位撤向 Core
        for u in turn.units:
            if u.unit_type == UnitType.WORKER or u.id in manual.overridden_units:
                continue
            if u.position != core_pos:
                d = step_toward(u.position, core_pos, obstacles | enemy_cells)
                if d:
                    _mv(u, d)
        return

    combat.retreat_mode = False

    # 人口太少时不主动进攻
    if turn.state.population < MIN_WORKERS_FOR_COMBAT:
        combat.engaging = False
        # 仅自卫
        _plan_self_defense(turn, enemies, obstacles, manual, enemy_cells)
        return

    # 寻找敌方 Core（高价值目标）
    enemy_cores = [e for e in enemies
                   if getattr(e, "kind", None) == "CORE"]

    if enemy_cores and not combat.retreat_mode:
        # 仅当「进攻单位」自身也看到了敌Core，才全体突击（集中火力）。
        # 否则（只有工人/视野偶然照到）不拉走防御单位——交给 plan_attack_dispatch
        # 抽调最近的探索组去进攻。避免工人偶然发现敌Core就全员撤离 Core 防御。
        seen_by_combat = any(
            u.unit_type != UnitType.WORKER
            and u.id not in manual.overridden_units
            and manhattan(u.position, ec.position)
            <= VISION_RADIUS.get(u.unit_type, 3)
            for ec in enemy_cores
            for u in turn.units
        )
        if seen_by_combat:
            # 优先打击敌方 Core（"二向箔"式精确打击）
            combat.engaging = True
            combat.primary_target_core = enemy_cores[0]
            # 记录本次投入兵力，供 should_retreat 判断"进攻损失惨重撤退"
            combat.assault_force_size = sum(
                1 for u in turn.units
                if u.unit_type != UnitType.WORKER
                and u.id not in manual.overridden_units)
            _plan_assault_core(turn, enemy_cores[0], enemies, obstacles, manual, enemy_cells)
            return
        # 仅工人发现：不突击，仍按自卫逻辑；可进攻目标由 _update_attack_targets 登记

    # 无敌方 Core → 自卫 + 追击普通敌人
    combat.engaging = False
    _plan_self_defense(turn, enemies, obstacles, manual, enemy_cells)


def _plan_self_defense(turn, enemies, obstacles, manual, enemy_cells):
    """最低限度自卫：Ranger 射程内射击、射程外靠近；Vanguard 清扫相邻敌。"""
    blocked = obstacles | enemy_cells
    for r in turn.rangers:
        if r.id in manual.overridden_units:
            continue
        tgt = min(enemies, key=lambda e: manhattan(r.position, e.position),
                  default=None)
        if not tgt:
            continue
        dist = manhattan(r.position, tgt.position)
        if dist <= 3:  # Ranger 射程 1-3
            try:
                r.shoot(tgt)
            except Exception:
                pass
        else:
            # 射程外先靠近，避免原地空转（下一 tick 进入射程再射击）
            d = step_toward(r.position, tgt.position, blocked)
            if d:
                _mv(r, d)

    for v in turn.vanguards:
        if v.id in manual.overridden_units:
            continue
        adj = [e for e in enemies if manhattan(v.position, e.position) == 1]
        if adj:
            d = dir_between(v.position, adj[0].position)
            if d:
                v.sweep(d)


def _plan_assault_core(turn, target_core, all_enemies, obstacles, manual, enemy_cells):
    """集中火力打击敌方 Core：各单位从不同方向逼近，进入射程/相邻再直接攻击。"""
    core_pos = turn.core.position if turn.core else None
    target_pos = target_core.position
    blocked = obstacles | enemy_cells

    # 所有非 Worker、未被手动控制的单位向敌方 Core 集结
    combat_units = [u for u in turn.units
                    if u.unit_type != UnitType.WORKER
                    and u.id not in manual.overridden_units]
    n = max(len(combat_units), 1)

    for i, u in enumerate(combat_units):
        dist = manhattan(u.position, target_pos)
        ut = u.unit_type
        # 多方向逼近：每个单位朝目标周围不同方位的接近点前进，避免聚成一团走同一路径
        ang = 2 * math.pi * i / n
        ap = (target_pos[0] + round(2 * math.cos(ang)),
              target_pos[1] + round(2 * math.sin(ang)))
        if ut == UnitType.RANGER:
            if dist <= 3:  # Ranger 射程 1-3
                try:
                    u.shoot(target_core)
                except Exception:
                    pass
            else:
                d = step_toward(u.position, ap, blocked)
                if d:
                    _mv(u, d)
        elif ut == UnitType.VANGUARD:
            if dist == 1:
                d = dir_between(u.position, target_pos)
                if d:
                    u.sweep(d)
            else:
                d = step_toward(u.position, ap, blocked)
                if d:
                    _mv(u, d)
        else:
            d = step_toward(u.position, target_pos, blocked)
            if d:
                _mv(u, d)


# =========================================================================== #
# Core 决策
# =========================================================================== #

def _dir_toward(a: Position, b: Position) -> Optional[Direction]:
    """a→b 的单步主方向（取 |Δ| 较大的轴；按 SDK 的 Direction delta 约定：RIGHT=+x, DOWN=+y）。"""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    if abs(dx) >= abs(dy):
        if dx > 0:
            return Direction.RIGHT
        if dx < 0:
            return Direction.LEFT
    if dy > 0:
        return Direction.DOWN
    if dy < 0:
        return Direction.UP
    return None


def _worker_safe(pos: Position, enemy_cells: set[Position],
                 radius: int = SAFE_RADIUS) -> bool:
    """工人附近有无可见敌人。安全（附近无敌人）→ 可放心外出采集；否则暂停外出。"""
    for e in enemy_cells:
        if manhattan(pos, e) <= radius:
            return False
    return True


def _core_is_moving(core) -> bool:
    """Core 是否正在迁移。兼容 SDK 把 state 表示为字符串 'MOVING' 或枚举
    CoreState.MOVING（其 .value=='MOVING'）两种情况——直接用 `== "MOVING"` 会在
    枚举形态下恒为 False，导致 cancel_move 永不触发、Core 永远停不下来。"""
    s = getattr(core.view, "state", None)
    if s is None:
        return False
    if s == "MOVING":
        return True
    try:
        if getattr(s, "value", None) == "MOVING":
            return True
    except Exception:
        pass
    return False


def beacon_march_info(turn, core_pos) -> tuple[bool, set]:
    """返回 (是否正朝信标进军, 朝信标的主方向桶集合)。

    进军条件与 _plan_core_march 完全一致：信标开启 + 先锋/游侠合计>=MARCH_MIN_COMBAT
    且 Core 尚未抵达信标(曼哈顿>1)。命中时，探索工 / 探索战斗组应有 80% 偏向信标方向
    展开探索（仅 20% 维持全向覆盖，防止地图一侧彻底盲区），但探索范围仍受
    EXPLORER_MAX_RING 约束、绝不超出计算出的探索范围。

    主方向桶以 Core 扇区为原点：信标在 Core 右下方 → 取 {右(+x), 下(+y), 右下(+x,+y)}
    三个桶（即用户说的"朝、右、右下"）。
    """
    if not (BEACON_ENABLED and core_pos is not None):
        return (False, set())
    total_combat = len(turn.vanguards) + len(turn.rangers)
    if total_combat < MARCH_MIN_COMBAT:
        return (False, set())
    if manhattan(core_pos, BEACON_CELL) <= 1:
        return (False, set())
    sx = 1 if BEACON_CELL[0] > core_pos[0] else (-1 if BEACON_CELL[0] < core_pos[0] else 0)
    sy = 1 if BEACON_CELL[1] > core_pos[1] else (-1 if BEACON_CELL[1] < core_pos[1] else 0)
    buckets: set = set()
    if sx != 0:
        buckets.add((sx, 0))            # 纯轴向（东/西）
    if sy != 0:
        buckets.add((0, sy))            # 纯轴向（南/北）
    if sx != 0 and sy != 0:
        buckets.add((sx, sy))           # 对角（右下/右上/左下/左上）
    return (True, buckets)


def _core_cargo_pause(core, cargo_workers) -> bool:
    """取消/暂停 Core 迁移的必要条件：带货工人在 Core 周围 CORE_MARCH_PAUSE_R 格(默认5)
    之内才暂停，等其交付。严格按此半径判定（不额外放宽），自动进军与手动迁移共用，
    避免"无条件下取消迁移"掐掉用户手动迁移。"""
    r = CORE_MARCH_PAUSE_R
    d = lambda w: manhattan(w.position, core.position)
    return any(d(w) <= r for w in cargo_workers)


def _core_march_direction(core_pos: Position, beacon: Position,
                          obstacles: set[Position]) -> Optional[Direction]:
    """Core 朝信标迁移的单步方向：优先主方向(_dir_toward)；主方向相邻格是障碍 → 改选
    任意能减少到信标曼哈顿距离的自由相邻格；四面被堵 → 返回 None（原地不迁移，绝不撞石头）。"""
    desired = _dir_toward(core_pos, beacon)
    if desired is None:
        return None
    cand = (core_pos[0] + desired.delta[0], core_pos[1] + desired.delta[1])
    if cand not in obstacles:
        return desired
    # 主方向被堵：挑一个能朝信标推进的自由方向（按距离升序）
    best = None
    best_d = manhattan(core_pos, beacon)
    for alt in DIRECTIONS:
        nc = (core_pos[0] + alt.delta[0], core_pos[1] + alt.delta[1])
        if nc in obstacles:
            continue
        nd = manhattan(nc, beacon)
        if nd < best_d:
            best_d = nd
            best = alt
    return best  # None = 四面被堵


def _plan_core_march(turn, core) -> None:
    """冠军信标进军：Core 朝 (0,0) 迁移（带局部避障，不撞石头）；带货工人在周围时停下等交付。"""
    moving = _core_is_moving(core)
    cargo_workers = [w for w in turn.workers if getattr(w, "cargo", 0) > 0]
    if _core_cargo_pause(core, cargo_workers):
        if moving:
            try:
                core.cancel_move()
            except Exception:
                pass
        return  # 本 tick 不迁移、不生产，等工人 deposit（deposit 是工人动作，不占 Core 槽）
    # 没有带货工人在身边 → 朝信标迁移（带局部避障，不撞石头）
    obstacles = set(getattr(turn, "obstacle_cells", []) or [])
    desired = _core_march_direction(core.position, BEACON_CELL, obstacles)
    if desired is None:
        return  # 四面被堵或已到信标：本 tick 不迁移
    if moving:
        # 方向不对（如已到达某一轴需转向）→ 取消，下一 tick 以正确方向重启
        if getattr(core.view, "move_direction", None) != desired:
            try:
                core.cancel_move()
            except Exception:
                pass
        # 方向正确 → 让它继续迁移，本 tick 不再排队动作
    else:
        try:
            core.start_move(desired)
        except Exception:
            pass


def plan_core_actions(turn, combat: CombatState, map_mem=None, state=None) -> None:
    """Core 动作：恢复优先 → （有自保能力时朝信标进军）→ 生产 → 经济冻结。"""
    core = turn.core
    if core is None:
        return
    pop = turn.state.population

    # 撤退模式下只回血不生产
    if combat.retreat_mode:
        if core.hp < 5:
            core.heal()
        return

    # 恢复优先（单动作槽，恢复期间不生产）
    if core.hp < 5:
        core.heal()
        return
    if core.shield < 5 and turn.resources > 0:
        core.repair_shield()
        return

    # ---- 威胁即造游侠（仅次于回血/护盾）：敌方【进攻单位】进入 Core 周围 CORE_RANGER_RADIUS(默认10)
    #      且仍有资源可造 → 立即造游侠，优先于经济冻结/进军/补工人。把战斗单位全数拉回守 Core 时，
    #      这是补齐远程火力的直接手段。仅敌方进攻单位(VANGUARD/RANGER)触发，非进攻单位(工人/Core)不触发。----
    if DEFEND and CORE_RANGER_RADIUS and core is not None:
        cpos = core.position
        if any(manhattan(e.position, cpos) <= CORE_RANGER_RADIUS
               and (e.unit_type if hasattr(e, "unit_type") else UnitType.WORKER) in ENEMY_COMBAT_TYPES
               for e in getattr(turn, "visible_enemies", [])):
            r_count = len(turn.rangers)
            if r_count < MAX_RANGERS:
                cost = unit_cost(UnitType.RANGER, pop)
                if turn.resources >= cost:
                    core.spawn(UnitType.RANGER)
                    return

    # ---- 经济冻结模式（最高优先级，仅次于回血/护盾/威胁造游侠）：资源容量达上限 → 记录峰值，只补损失 ----
    if state is not None and turn.resource_capacity >= ECONOMY_FREEZE_CAPACITY:
        if not state.economy_frozen:
            state.economy_frozen = True
            state.economy_peak_workers = len(turn.workers)
            state.economy_peak_vanguards = len(turn.vanguards)
            state.economy_peak_rangers = len(turn.rangers)
            print(f"[经济冻结] 容量{turn.resource_capacity}>={ECONOMY_FREEZE_CAPACITY}，"
                  f"记录峰值 W={state.economy_peak_workers}/"
                  f"V={state.economy_peak_vanguards}/R={state.economy_peak_rangers}",
                  flush=True)
        w_now = len(turn.workers)
        v_now = len(turn.vanguards)
        r_now = len(turn.rangers)
        w_deficit = state.economy_peak_workers - w_now
        v_deficit = state.economy_peak_vanguards - v_now
        r_deficit = state.economy_peak_rangers - r_now
        if w_deficit > 0:
            cost = unit_cost(UnitType.WORKER, pop)
            if turn.resources >= cost:
                core.spawn(UnitType.WORKER)
            return
        if v_deficit > 0:
            cost = unit_cost(UnitType.VANGUARD, pop)
            if turn.resources >= cost:
                core.spawn(UnitType.VANGUARD)
            return
        if r_deficit > 0:
            cost = unit_cost(UnitType.RANGER, pop)
            if turn.resources >= cost:
                core.spawn(UnitType.RANGER)
            return
        return  # 冻结且全满：不生产、不进军、不迁移，只攒资源

    # 冠军信标进军：信标开启 + 已有战斗单位(先锋/游侠)>=MARCH_MIN_COMBAT(默认5) 才向信标推进；
    # 战斗单位不足时不进军（满足"进军条件=信标开启+战斗单位>=5"的设定）。
    total_combat = len(turn.vanguards) + len(turn.rangers)
    has_combat = total_combat >= MARCH_MIN_COMBAT
    at_beacon = manhattan(core.position, BEACON_CELL) <= 1
    if BEACON_ENABLED and has_combat and not at_beacon:
        _plan_core_march(turn, core)
        return

    # 迁移中：仅当"带货工人在周围"才暂停（用户手动迁移 / 自动进军通用），否则不打断，
    # 让 Core 自然走完本次迁移。修复：原先无条件 cancel_move 会掐掉用户的手动迁移，导致卡死。
    if _core_is_moving(core):
        cargo_workers = [w for w in turn.workers if getattr(w, "cargo", 0) > 0]
        if _core_cargo_pause(core, cargo_workers):
            try:
                core.cancel_move()
            except Exception:
                pass
        return

    # 生产（用户指定比例逻辑）：
    #   ① 工人未满 MIN_WORKERS_FOR_COMBAT(默认5) → 只补工人，不出战斗单位；
    #   ② 满 5 工人后开始造战斗单位：先锋满 3 → 游侠满 3 → 之后先锋/游侠交替；
    #   ③ 每造 2 个战斗单位，补充 1 个工人（目标工人 = 5 + combat_produced//2，封顶 TARGET_WORKERS）；
    #   ④ 一切受上方 95 资源经济冻结规则约束（冻结后只补损失、不新造）。
    workers = len(turn.workers)
    v_count = len(turn.vanguards)
    r_count = len(turn.rangers)
    if workers < MIN_WORKERS_FOR_COMBAT:
        # 早期铺工人：不留预留，快速破局
        cost = unit_cost(UnitType.WORKER, pop)
        if turn.resources >= cost:
            core.spawn(UnitType.WORKER)
        return

    # 已到 5 工人 → 按指定顺序决定下一个战斗单位
    nu = next_combat_unit(v_count, r_count)
    worker_target = min(TARGET_WORKERS, MIN_WORKERS_FOR_COMBAT + state.combat_produced // 2)
    # 每造 2 个战斗单位插入一次工人补产（仅当工人尚未达标）
    if state.combat_since_worker >= 2 and workers < worker_target:
        cost = unit_cost(UnitType.WORKER, pop)
        if turn.resources >= cost + SPAWN_BUFFER:
            core.spawn(UnitType.WORKER)
            state.combat_since_worker = 0
        return
    # 否则造下一个战斗单位（即便总人口未到 TARGET_WORKERS 也按需转产）
    cost = unit_cost(nu, pop)
    if turn.resources >= cost + SPAWN_BUFFER:
        core.spawn(nu)
        state.combat_produced += 1
        state.combat_since_worker += 1


# =========================================================================== #
# 战斗单位分流：巡逻保护 vs 迷雾猎杀
# =========================================================================== #

def _split_combat_units(turn, manual: ManualOverride, defend_all: bool = False):
    """将进攻单位分流为「巡逻池(围Core保护)」与「探索池(迷雾猎杀)」。

    规则（与用户设定一致）：
      - defend_all=True（Core 周围 CORE_BLOCK_RADIUS 内出现威胁）：全员死守 Core，
        不派出任何进攻/探索单位（把战斗单位全数调回保护 Core）；
      - 战斗单位 <= 2：不派出进攻单位，全部围 Core 巡逻保护（优先保核）；
      - 战斗单位 >= 3(>2) 且无威胁：按 PATROL_COMBAT_RATIO 比例留部分围Core，
        其余组成 V+R 对去迷雾探索/猎杀。

    返回 (patrol_units, explore_units) 两个列表，元素均为可控单位(V/R)。
    """
    all_combat = [u for u in turn.units
                  if u.unit_type != UnitType.WORKER
                  and u.id not in manual.overridden_units]
    all_combat.sort(key=lambda u: str(u.id))  # 确定性分配
    n = len(all_combat)
    if defend_all or n < EXPLORE_MIN_COMBAT:        # 全员死守 或 <=2：全部保护 Core
        return all_combat, []
    patrol_n = max(1, round(n * PATROL_COMBAT_RATIO))
    return all_combat[:patrol_n], all_combat[patrol_n:]


def _form_vr_pairs(explore_units):
    """把探索池的进攻单位编成「一先锋+一游侠」组。
    返回 list[(vanguard|None, ranger|None)]，未配对的单独成组。
    """
    vangs = [u for u in explore_units if u.unit_type == UnitType.VANGUARD]
    rangs = [u for u in explore_units if u.unit_type == UnitType.RANGER]
    pairs = []
    for i in range(max(len(vangs), len(rangs))):
        v = vangs[i] if i < len(vangs) else None
        r = rangs[i] if i < len(rangs) else None
        pairs.append((v, r))
    return pairs


def _vr_needs_stack_wait(v, r) -> bool:
    """先锋是否需等待游侠叠加（用户策略）。

    规则：游侠与先锋应「叠加成为一组」（同格站位，先锋在前当盾）。
    - 本组有游侠(r 非 None)但二者尚未同格 → 返回 True：先锋原地等待，
      由游侠主动靠拢叠加上来后，下一 tick 再一起推进/交战。
    - r 为 None（场上无多余游侠可配该先锋）→ 返回 False：先锋自由行动，不等待。
    - v 为 None（仅游侠单兵组）→ 返回 False：无先锋可等，游侠自行行动。
    """
    if v is None or r is None:
        return False
    return v.position != r.position


def _ranger_engage(r, v, target_pos: Position, target_obj=None,
                  blocked: set[Position] | None = None) -> bool:
    """游侠交战：射程内主攻射击，射程外主动逼近，无目标时回退编队。

    解决的核心问题：旧逻辑中游侠在射程外只被动跟随先锋编队（_group_trail），
    永远不会主动靠近敌人进入射程；且射程内射击后仍被 _group_trail 拉离射程。

    返回 True 表示游侠本 tick 已执行了攻击/逼近动作（调用方不应再额外移动它）。
    """
    if r is None:
        return False
    if blocked is None:
        blocked = set()
    RANGER_RANGE = 3  # 游侠曼哈顿射程

    dist = manhattan(r.position, target_pos)
    # ---- 射程内：射击为主攻，且不移开（不调用 _group_trail）----
    if dist <= RANGER_RANGE:
        try:
            if target_obj is not None:
                r.shoot(target_obj)
            else:
                # 无对象引用时用坐标射击（shoot_cell）
                r.shoot_cell(target_pos)
        except Exception:
            pass
        return True  # 已射击，不再移动

    # ---- 射程外：直接朝目标逼近（step_toward），而非被动跟随先锋编队 ----
    d = step_toward(r.position, target_pos, blocked)
    if d:
        nr = (r.position[0] + d.delta[0], r.position[1] + d.delta[1])
        # 不踩到先锋头上（如果有的话）
        if v is None or nr != v.position:
            _mv(r, d)
            return True

    # ---- 逼近失败（被挡）：回退到编队跟随 ----
    _group_trail(r, v, target_pos, blocked)
    return True


def _group_trail(r, v, goal: Position, blocked: set[Position]) -> None:
    """游侠与先锋的编队。

    - GROUP_TRAIL<=0（重叠编队）：游侠直接叠到先锋所在格（同格站位，先锋在前当盾，
      被攻击时优先承伤）。先锋朝 goal 推进、游侠每 tick 追到先锋头上 → 始终重叠。
    - GROUP_TRAIL>=1：游侠落后先锋 GROUP_TRAIL 格跟随（沿推进反方向），不踩到先锋头上。
    """
    if r is None:
        return
    # 仅游侠无先锋（单兵组）→ 直接朝 goal 推进
    if v is None:
        d = step_toward(r.position, goal, blocked)
        if d:
            _mv(r, d)
        return
    if GROUP_TRAIL <= 0:
        # 重叠编队：游侠直接移动到先锋所在格（允许同格，不同于工人坐标协调）
        if r.position == v.position:
            return
        d = step_toward(r.position, v.position, blocked)
        if d:
            _mv(r, d)   # 允许与先锋同格（重叠）
        return
    # GROUP_TRAIL>=1：游侠落后先锋 GROUP_TRAIL 格跟随
    heading = _dir_toward(v.position, goal) if v.position != goal else None
    if heading:
        hdx, hdy = heading.delta
        behind = (v.position[0] - hdx * GROUP_TRAIL,
                  v.position[1] - hdy * GROUP_TRAIL)
    else:
        behind = v.position
    dist = manhattan(r.position, v.position)
    if dist <= GROUP_TRAIL:
        d = step_toward(r.position, goal, blocked)        # 跟先锋一起前进
    else:
        d = step_toward(r.position, behind, blocked)      # 回到落后位置
    if d:
        nr = (r.position[0] + d.delta[0], r.position[1] + d.delta[1])
        if nr != v.position:                              # 不踩到先锋头上
            _mv(r, d)


def _plan_group_assault(v, r, target_pos: Position,
                        core_obj, blocked: set[Position],
                        targets: dict | None = None) -> None:
    """一组 V+R 进攻敌Core：先锋推进至相邻扫荡；游侠射程内射击，否则跟随编队。

    - 先锋：距敌Core==1 → sweep；>1 → 朝目标推进（领先）。
    - 游侠：可见敌Core且距<=3 → shoot(core_obj)；否则落后先锋 GROUP_TRAIL 格跟随。
    - core_obj 为实际可见敌Core对象（射程内射击用）；远途未看见时为 None，游侠只跟随。
    """
    if v is not None:
        dv = manhattan(v.position, target_pos)
        if dv == 1:
            d = dir_between(v.position, target_pos)
            if d:
                try:
                    v.sweep(d)
                except Exception:
                    pass
        elif dv > 1:
            d = step_toward(v.position, target_pos, blocked)
            if d:
                _mv(v, d)
    if r is not None:
        _ranger_engage(r, v, target_pos, target_obj=core_obj, blocked=blocked)
    if targets is not None:
        if v is not None:
            targets[v.id] = target_pos
        if r is not None:
            targets[r.id] = target_pos


def _is_core_attackable(turn, enemy_mem: EnemyMemory) -> list[Position]:
    """返回本 tick 可见、且周围 CORE_ATTACK_SAFE_RADIUS 内无敌方进攻单位的敌Core位置。

    即「只有敌Core自己，或敌Core+敌方工人」才算"可进攻"；若附近有敌方 V/R 进攻单位
    （可见或记忆中）则视为有防守，不标记。
    """
    safe: list[Position] = []
    for e in turn.visible_enemies:
        if getattr(e, "kind", None) != "CORE":
            continue
        if getattr(e, "controlled", False):
            continue  # 我方Core不算
        ecp = tuple(e.position)
        defended = False
        for o in turn.visible_enemies:
            if o is e:
                continue
            ut = o.unit_type if hasattr(o, "unit_type") else None
            if ut in ENEMY_COMBAT_TYPES and manhattan(o.position, ecp) <= CORE_ATTACK_SAFE_RADIUS:
                defended = True
                break
        if not defended:
            for rec in enemy_mem.records.values():
                if rec.unit_type in ENEMY_COMBAT_TYPES and rec.last_pos is not None \
                        and manhattan(rec.last_pos, ecp) <= CORE_ATTACK_SAFE_RADIUS:
                    defended = True
                    break
        if not defended:
            safe.append(ecp)
    return safe


def _update_attack_targets(turn, state, tick: int) -> None:
    """维护 state.attack_targets：登记本 tick 新发现的"可进攻"敌Core，清理过期目标。"""
    for p in _is_core_attackable(turn, state.enemy_memory):
        state.attack_targets[p] = {"pos": p, "discovered_tick": tick}
    # 过期清理：超过 ATTACK_TARGET_TTL tick 未再确认的目标丢弃
    expired = [p for p, info in state.attack_targets.items()
               if tick - info.get("discovered_tick", tick) > ATTACK_TARGET_TTL]
    for p in expired:
        del state.attack_targets[p]


def plan_attack_dispatch(turn, state, explore_pool: list,
                         obstacles: set[Position], enemy_cells: set[Position],
                         targets: dict | None = None) -> list:
    """从探索池中抽调最近的 V+R 组去进攻已确认的"可进攻"敌Core。

    返回被抽调的单位 id 集合（调用方据此从 explore_pool 移除，避免探索逻辑覆盖）。
    每个可进攻目标贪心选距离最近的未使用探索组；先锋领先推进+扫荡，游侠射程内
    射击或跟随编队。
    """
    if not state.attack_targets or not explore_pool:
        return []
    # 可见敌Core对象（射程内射击用）：pos → 对象
    vis_core = {tuple(e.position): e for e in turn.visible_enemies
                if getattr(e, "kind", None) == "CORE"
                and not getattr(e, "controlled", False)}
    blocked = obstacles | enemy_cells
    pairs = _form_vr_pairs(list(explore_pool))
    dispatched_ids: set = set()
    used_pairs: list = []
    for tpos in state.attack_targets:
        best = None
        best_d = 1e9
        for v, r in pairs:
            if (v and v.id in dispatched_ids) or (r and r.id in dispatched_ids):
                continue
            anchor = v or r
            if anchor is None:
                continue
            d = manhattan(anchor.position, tpos)
            if d < best_d:
                best_d = d
                best = (v, r)
        if best is None:
            continue
        v, r = best
        if v:
            dispatched_ids.add(v.id)
        if r:
            dispatched_ids.add(r.id)
        _plan_group_assault(v, r, tpos, vis_core.get(tpos), blocked, targets)
        used_pairs.append((v, r))
    return dispatched_ids


def _step_away(pos: Position, enemy_pos: Position,
               obstacles: set[Position]) -> Optional[Direction]:
    """朝远离 enemy_pos 的方向走一步（不踩障碍），用于跟踪时保持最小间距。"""
    best = None
    best_d = manhattan(pos, enemy_pos)
    for d, nxt in neighbors(pos):
        if nxt in obstacles:
            continue
        nd = manhattan(nxt, enemy_pos)
        if nd > best_d:
            best = d
            best_d = nd
    if best is not None:
        return best
    # 没有更远的自由格 → 退而求其次走任意非障碍方向，避免原地卡死
    for d, nxt in neighbors(pos):
        if nxt not in obstacles:
            return d
    return None


@dataclass
class Engagement:
    """探索组交战评估：决定本 tick 进攻 / 跟踪 / 继续探索。"""
    mode: str = "EXPLORE"            # ATTACK / TRACK / EXPLORE
    enemy_pos: Optional[Position] = None   # 交战/跟踪目标（最近敌方单位位置）
    enemy_combat_count: int = 0       # 我方进攻单位视野内的敌方进攻单位数
    our_count: int = 0               # 我方参与交战的进攻单位数（探索池）
    reinforced_this_tick: bool = False


def assess_explore_engagement(turn, explore_units: list) -> Engagement:
    """评估探索组遇敌时应「进攻」还是「跟踪」。

    兵力计数（严格按用户定义）：
      - 我方数量 = 探索池中的进攻单位数(len(explore_units))；
      - 敌方进攻单位数 = 落入「我方任一进攻单位视野半径」内的敌方进攻单位(先锋/游侠)
        的去重个数（即我方两个单位视野都看到的同一个敌算 1，都看到 3 个共同敌算 3）。

    决策：
      - 视野内无任何敌方单位 → EXPLORE（继续探索）；
      - 敌方进攻单位 == 0（只有敌方工人/核心）或 敌方进攻单位 < 我方 → ATTACK（直接追赶攻击）；
      - 敌方进攻单位 >= 我方 → TRACK（不攻击，保持距离跟踪 + 求援）。
    """
    our = len(explore_units)
    ec: list = []          # 我方视野内的敌方进攻单位
    noncombat: list = []   # 我方视野内的敌方工人/核心
    for e in turn.visible_enemies:
        ut = e.unit_type if hasattr(e, "unit_type") else UnitType.WORKER
        seen = any(manhattan(u.position, e.position)
                   <= VISION_RADIUS.get(u.unit_type, 3)
                   for u in explore_units)
        if not seen:
            continue
        if ut in (UnitType.VANGUARD, UnitType.RANGER):
            ec.append(e)
        else:
            noncombat.append(e)

    if not (ec or noncombat):
        return Engagement(mode="EXPLORE", our_count=our)

    # 目标：优先最近的敌方进攻单位，否则最近的敌方工人/核心
    def _nd(e):
        return min(manhattan(u.position, e.position) for u in explore_units)
    target = min(ec + noncombat, key=_nd) if (ec or noncombat) else None
    ecount = len(ec)
    if ecount == 0 or ecount < our:
        mode = "ATTACK"
    else:
        mode = "TRACK"
    return Engagement(mode=mode,
                      enemy_pos=target.position if target else None,
                      enemy_combat_count=ecount,
                      our_count=our)


def _plan_explore_combat_groups(turn, pairs, obstacles: set[Position],
                                 enemy_cells: set[Position], map_mem,
                                 core_pos: Position,
                                 engage: Optional[Engagement] = None,
                                 targets: dict | None = None,
                                 beacon_dirs: Optional[set] = None,
                                 forbidden_cells: Optional[set] = None,
                                 danger_zones: Optional[list] = None) -> None:
    """每对 V+R 朝迷雾方向推进：依交战评估决定进攻 / 跟踪 / 继续探索。

    交战规则（用户定义）：
      - ATTACK：可见范围内只有敌方工人/核心，或我方进攻单位 > 敌方进攻单位 →
        直接追赶并攻击敌方单位（不论敌方单位总数）。
      - TRACK：敌方进攻单位 >= 我方 → 不攻击，但持续跟踪敌方单位并保持
        最小间距(TRACK_STANDOFF 格)，由 plan_turn_v2 调遣最近巡逻单位来支援，
        直到我方数量 > 对方才转为 ATTACK。
    """
    blocked = obstacles | enemy_cells
    if forbidden_cells:
        blocked = blocked | forbidden_cells   # 仅并入"禁止"状态区域（危险区不避开，要压入清场）
    # 探索方向均衡：同一 tick 内多组 V+R 各自认领不同方向的扇区，绕 Core 四周散开
    _claimed: set = set()
    # 交战模式：未传入 engage 时按旧逻辑（有敌即进攻）兜底
    mode = "ATTACK"
    enemy_pos = None
    if engage is not None:
        mode = engage.mode
        enemy_pos = engage.enemy_pos

    for i, (v, r) in enumerate(pairs):
        # 确定本组的"锚点"（优先先锋，无先锋则用游侠）
        anchor = v or r
        if anchor is None:
            continue
        # 信标进军偏置：每 5 组中 4 组(80%)偏向信标主方向，1 组(20%)维持全向覆盖。
        # 仅 march 主方向存在时生效；范围仍受 EXPLORER_MAX_RING 约束。
        bias_group = bool(beacon_dirs) and (i % 5 < 4)

        # ---- TRACK：兵力劣势，保持距离跟踪、不攻击、等待支援 ----
        if mode == "TRACK" and enemy_pos is not None:
            for u in (x for x in (v, r) if x):
                dist = manhattan(u.position, enemy_pos)
                if dist < TRACK_STANDOFF:
                    # 太近 → 后撤拉开到最小间距
                    d = _step_away(u.position, enemy_pos, blocked)
                elif dist > TRACK_STANDOFF:
                    # 太远 → 贴近到最小间距（持续跟随）
                    d = step_toward(u.position, enemy_pos, blocked)
                else:
                    # 恰好保持最小间距 → 原地盯防，不攻击
                    d = None
                if d:
                    try:
                        _mv(u, d)
                    except Exception:
                        pass
            if targets is not None and enemy_pos is not None:
                for u in (v, r):
                    if u is not None:
                        targets[u.id] = enemy_pos
            continue

        # ---- ATTACK：追赶并攻击最近敌方单位（不论敌方单位总数）----
        # 编队原则：先锋推进+承伤当盾；游侠跟随先锋同路线、射程内优先射击
        if turn.visible_enemies and mode == "ATTACK":
            tgt = min(turn.visible_enemies,
                      key=lambda e: manhattan(anchor.position, e.position),
                      default=None)
            if tgt:
                tgt_pos = tgt.position
                if targets is not None:
                    for u in (v, r):
                        if u is not None:
                            targets[u.id] = tgt_pos
                # 先锋：朝目标推进（距离=1 时不扫荡，仅站位承伤当盾）
                if v:
                    dist_v = manhattan(v.position, tgt_pos)
                    if dist_v > 1:
                        d = step_toward(v.position, tgt_pos, blocked)
                        if d:
                            _mv(v, d)
                # 游侠：射程内主攻射击；射程外主动逼近敌人（不再被动跟随编队）
                if r:
                    _ranger_engage(r, v, tgt_pos, target_obj=tgt, blocked=blocked)
                continue

        # ---- 先锋等待游侠叠加（用户规则，仅非交战阶段）----
        # 本组有游侠 R 且尚未叠到先锋同格 → 先锋原地等待，游侠靠过来叠加；
        # 本组无游侠(R=None，场上无多余游侠可配该先锋) → 先锋自由行动，不等待。
        # 交战(ATTACK/TRACK)阶段不等待，直接接敌。
        if _vr_needs_stack_wait(v, r):
            _group_trail(r, v, v.position, blocked)   # 游侠朝先锋靠拢直到同格
            if targets is not None:
                targets[v.id] = v.position
                targets[r.id] = v.position
            continue

        # ---- EXPLORE / 危险区清场 / 无可见敌人 → 朝迷雾最浓方向（未探索扇区）推进 ----
        goal_cell = None
        if danger_zones:
            # 危险区存在 → 全部探索战斗组压向最近危险区圆心清场（不避开、不外扩探索）
            dz = min(danger_zones, key=lambda z: manhattan(anchor.position, z.center))
            goal_cell = dz.center
        elif map_mem is not None:
            sec = _choose_frontier_sector(map_mem, anchor.position, core_pos, _claimed,
                                          beacon_dirs=beacon_dirs, bias_beacon=bias_group,
                                          forbidden_zones=danger_zones)
            if sec is not None:
                cell = _pick_unexplored_in_sector(map_mem, sec, anchor.position, blocked)
                if cell is not None:
                    goal_cell = cell
        if goal_cell is not None:
            # 编队推进：先锋领先朝目标推进；游侠重叠(GROUP_TRAIL=0)或跟随(GROUP_TRAIL>=1)
            if v and v.position != goal_cell:
                d = step_toward(v.position, goal_cell, blocked)
                if d:
                    _mv(v, d)
            if r is not None:
                _group_trail(r, v, goal_cell, blocked)
            if targets is not None:
                for u in (v, r):
                    if u is not None:
                        targets[u.id] = goal_cell
            continue

        # 兜底：朝远离 Core 方向移动（外扩），游侠跟随先锋编队
        if core_pos and anchor.position != core_pos:
            outward = _dir_toward(core_pos, anchor.position)
            # 反向：从 core 向外
            rev = {Direction.UP: Direction.DOWN, Direction.DOWN: Direction.UP,
                   Direction.LEFT: Direction.RIGHT, Direction.RIGHT: Direction.LEFT}
            outward = rev.get(outward, outward)
            if v and v.position != anchor.position:
                try:
                    _mv(v, outward)
                except Exception:
                    pass
            if r is not None:
                # 游侠通过编队跟随先锋（同路线）
                _group_trail(r, v, anchor.position, blocked)


# =========================================================================== #
# 防御巡逻
# =========================================================================== #

def _front_cell(enemy_pos: Position, core_pos: Position,
                obstacles: set[Position]) -> Optional[Position]:
    """敌方朝 Core 一侧的「前格」：敌人若要直扑 Core，下一步必然踏入的格子。
    我方单位占据它即可封堵敌方推进路线。返回 None 表示该格不可站（障碍/即Core）。
    """
    if core_pos is None:
        return None
    d = step_toward(enemy_pos, core_pos, obstacles)
    if d is None:
        return None
    cand = (enemy_pos[0] + d.delta[0], enemy_pos[1] + d.delta[1])
    if cand in obstacles or cand == core_pos:
        return None
    return cand


def _assign_patrol_rings(n: int) -> list[tuple[int, int, float]]:
    """把 n 个巡逻单位分配到三层巡逻圈（外/中/内），返回 [(radius, count, phase), ...]。

    组数上限按 PATROL_RING_GROUPS=(3,2,1)；战斗单位不足时按上限分配（如 5 个→外3+中2），
    超出 6 个的部分归入最外圈（溢出）。各圈相位错开，避免圆周轨迹重叠。
    """
    caps = PATROL_RING_GROUPS
    radii = (PATROL_RADIUS_OUTER, PATROL_RADIUS_MID, PATROL_RADIUS_INNER)
    counts = []
    rem = n
    for cap in caps:
        c = min(cap, rem)
        counts.append(c)
        rem -= c
    if rem > 0:
        counts[0] += rem  # 溢出单位归入最外圈
    out = []
    for idx, (radius, cnt) in enumerate(zip(radii, counts)):
        if cnt > 0:
            phase = idx * (math.pi / 3)  # 各圈错开相位，避免重叠
            out.append((radius, cnt, phase))
    return out


def _core_defense_dispatch(patrol_pool: list, explore_pool: list,
                           enemy_combat_near: list[Position], core_pos: Position) -> int:
    """按【比例】调配 Core 防御兵力，直接修改 patrol_pool / explore_pool 列表。

    规则（用户定义）：
      - enemy_combat_near 为空（无非进攻单位威胁）→ 不召回，返回 0；
      - 敌方进攻单位数 = len(enemy_combat_near)；要求投入总战斗单位 严格 > 敌方
        (need_total = n + 1)；
      - 巡逻组数量 >= need_total → 不召回探索组（巡逻组直接全部迎击）；
      - 巡逻组数量 <  need_total → 按缺口召回【最近的】探索组（到 Core 距离升序），
        直到总战斗单位 > 敌方；探索组不足则全部召回（仍可能劣势，但已是最大可投入）。
    返回实际召回的探索组数量（供日志）。
    """
    if not enemy_combat_near:
        return 0
    enemy_n = len(enemy_combat_near)
    need_total = enemy_n + 1
    recalled = 0
    if len(patrol_pool) < need_total:
        shortfall = need_total - len(patrol_pool)
        explorers_sorted = sorted(explore_pool,
                                  key=lambda u: manhattan(u.position, core_pos))
        for u in explorers_sorted[:shortfall]:
            explore_pool.remove(u)
            patrol_pool.append(u)
            recalled += 1
    return recalled


def plan_patrol(turn, patrol: PatrolState, core_pos: Position,
                obstacles: set[Position], enemy_mem: EnemyMemory,
                manual: ManualOverride, enemy_cells: set[Position],
                patrol_units=None, targets: dict | None = None,
                aggressive: bool = False) -> None:
    """Core 防御巡逻：安全时绕 Core 巡逻，遇敌时追击。

    aggressive=True（死守模式）：以 Core 为中心 CORE_BLOCK_RADIUS 曼哈顿范围内
    出现敌人即全员进攻+封堵——先锋相邻扫荡、游侠射程内射击，并抢占敌人朝 Core
    一侧前格挡住其推进路线；绝不保持距离、绝不后撤。
    """
    # 如果没有显式指定巡逻单位 → 兼容旧逻辑：全部分配 Ranger
    if patrol_units is None:
        assign_patrol_targets(turn, patrol, core_pos)
        patrol_units = [r for r in turn.rangers
                        if r.id in patrol.patrol_units
                        and r.id not in manual.overridden_units]
    else:
        # 显式指定 → 为这些单位分配巡逻目标（含 V+R）
        for u in patrol_units:
            patrol.patrol_units.add(u.id)
        n = max(len(patrol_units), 1)
        # 已在 plan_combat 中领取交战动作(SHOOT/SWEEP)的单位，本 tick 不再改派巡逻/追击，
        # 避免覆盖其进攻指令（如可见敌方 Core 时 Ranger 应射击而非被改去巡逻环）。
        # 兼容测试桩：turn 可能无 plan/unit_actions 属性。
        _plan_actions = getattr(getattr(turn, "plan", None), "unit_actions", None)
        committed = set()
        if _plan_actions is not None:
            for u in patrol_units:
                _a = _plan_actions.get(u.id)
                if _a is not None and getattr(_a, "type", None) in ("SHOOT", "SWEEP"):
                    committed.add(u.id)
        # ---- 三层巡逻：外圈(36×36)/中圈(20×20)/内圈(10×10) 各分配固定组数，绕 Core 圆周分布 ----
        # 组数上限 外3/中2/内1（PATROL_RING_GROUPS），战斗单位不足时按上限分配，溢出归外圈。
        # V+R 编队模式：每对(V,R)共享同一巡逻目标位置，游侠跟随先锋到同格/紧邻格。
        # 环分配仍按原始单位数（保持外/中/内比例），每对占一个目标槽位。
        patrol_pairs = _form_vr_pairs(patrol_units)  # 按 V+R 配对
        rings = _assign_patrol_rings(n)  # n=原始单位数（保持环分配比例不变）
        idx = 0
        # 展开所有环槽位为扁平列表 [(radius, phase), ...]，方便按序分配给配对
        flat_slots = []
        for radius, cnt, phase in rings:
            for k in range(cnt):
                flat_slots.append((radius, phase, k, cnt))
        # 按轮询方式把配对分配到各环（而非顺序填满外圈再中圈），使 V+R 对均匀分布在各层
        for pair_idx, (v, r) in enumerate(patrol_pairs):
            if pair_idx >= len(flat_slots):
                break  # 配对多于槽位时溢出归最后可用槽位所在环
            radius, phase, k, cnt = flat_slots[pair_idx]
            angle = 2 * math.pi * k / max(cnt, 1) + turn.tick * 0.05 + phase
            tx = core_pos[0] + round(radius * math.cos(angle))
            ty = core_pos[1] + round(radius * math.sin(angle))
            pair_target = (tx, ty)
            if v is not None:
                patrol.patrol_targets[v.id] = pair_target
            if r is not None:
                patrol.patrol_targets[r.id] = pair_target  # 同目标

    # ---- 巡逻推进：每对 V+R 朝各自巡逻目标推进（先锋领路、游侠跟随编队）----
    # 先处理非攻击状态的普通巡逻移动（编队模式）
    _patrol_moved: set = set()  # 记录已在本函数中移动过的单位 id
    if not (aggressive and core_pos is not None):
        # 非死守或死守但无威胁时：按巡逻环目标推进（V+R 编队）
        for v, r in (_form_vr_pairs(patrol_units) if patrol_units else []):
            anchor = v or r  # 有先锋用先锋锚点，否则用游侠
            if anchor is None or anchor.id in manual.overridden_units or anchor.id in committed:
                continue
            tgt = patrol.patrol_targets.get(anchor.id)
            if tgt is None:
                continue
            blocked_patrol = obstacles | enemy_cells
            # 先锋等待游侠叠加（非交战）：有游侠未同格→先锋不动、游侠靠拢；无游侠→自由行动
            if _vr_needs_stack_wait(v, r):
                _group_trail(r, v, v.position, blocked_patrol)
                _patrol_moved.add(v.id)
                _patrol_moved.add(r.id)
                if targets is not None:
                    targets[v.id] = v.position
                    targets[r.id] = v.position
                continue
            # 先锋朝巡逻目标推进
            if v is not None and v.id not in committed:
                d = step_toward(v.position, tgt, blocked_patrol)
                if d:
                    _mv(v, d)
                _patrol_moved.add(v.id)
            # 游侠跟随先锋编队（同路线）
            if r is not None and r.id not in committed:
                _group_trail(r, v, tgt, blocked_patrol)
                _patrol_moved.add(r.id)
            if targets is not None:
                if v is not None:
                    targets[v.id] = tgt
                if r is not None:
                    targets[r.id] = tgt

    # ---- 死守模式：Core 周围 CORE_BLOCK_RADIUS 内出现敌人 → 全员进攻+封堵，绝不后撤 ----
    if aggressive and core_pos is not None:
        blocked = obstacles | enemy_cells
        threat_cells: set[Position] = set()
        for e in turn.visible_enemies:
            if manhattan(e.position, core_pos) <= CORE_BLOCK_RADIUS:
                threat_cells.add(e.position)
        for rec in enemy_mem.records.values():
            lp = getattr(rec, "last_pos", None)
            if lp and manhattan(lp, core_pos) <= CORE_BLOCK_RADIUS:
                threat_cells.add(lp)
        if threat_cells:
            # 死守也按 V+R 编队进攻：先锋推进承盾、游侠射击主攻
            for v, r in (_form_vr_pairs(patrol_units) if patrol_units else []):
                anchor = v or r
                if anchor is None or anchor.id in manual.overridden_units:
                    continue
                # 跳过已交战的单位
                if v is not None and v.id in committed:
                    continue
                if r is not None and r.id in committed:
                    continue
                tgt_pos = min(threat_cells, key=lambda c: manhattan(anchor.position, c))
                front = _front_cell(tgt_pos, core_pos, obstacles)
                goal = front if front else tgt_pos
                # 先锋：朝目标推进（距离=1 时相邻扫荡承伤输出；>1 时逼近）
                if v is not None:
                    dist_v = manhattan(v.position, goal)
                    if dist_v == 1:
                        # 相邻敌方前格 → 扫荡（承盾+伤害）
                        d2 = dir_between(v.position, goal)
                        if d2:
                            try:
                                v.sweep(d2)
                            except Exception:
                                pass
                    elif dist_v > 1:
                        d = step_toward(v.position, goal, blocked)
                        if d:
                            _mv(v, d)
                    _patrol_moved.add(v.id)
                # 游侠：射程内主攻射击；射程外主动逼近（不再被动跟随编队）
                if r is not None:
                    _ranger_engage(r, v, goal, blocked=blocked)
                    _patrol_moved.add(r.id)
                if targets is not None:
                    if v is not None:
                        targets[v.id] = tgt_pos
                    if r is not None:
                        targets[r.id] = tgt_pos
            return  # 死守模式不再走普通追击

    # ---- 普通巡逻追击（非死守、巡逻范围内有敌人）----
    # 近敌判定扩到外圈半径(36×36)：巡逻范围内出现【任何敌方单位】即追击——
    # 含敌方进攻单位(主动迎击)，也含敌方非进攻单位(工人/Core，巡逻组直接攻击，
    # 不召回探索组)。具体"全员死守+封堵"只在 CORE_BLOCK_RADIUS 内出现【敌方进攻单位】
    # (threat_near) 时由上方 aggressive 分支触发。
    nearby_enemies = [e for e in enemy_mem.get_nearby_enemies(core_pos, PATROL_RADIUS_OUTER, turn.tick)]

    # 普通追击/巡逻：按 V+R 编队（先锋领路、游侠跟随+主攻）
    for v, r in (_form_vr_pairs(patrol_units) if patrol_units else []):
        anchor = v or r
        if anchor is None or anchor.id in manual.overridden_units:
            continue
        if (v is not None and v.id in committed) or (r is not None and r.id in committed):
            continue
        # 跳过已在上方编队移动中处理过的单位
        if (v is not None and v.id in _patrol_moved) or (r is not None and r.id in _patrol_moved):
            continue

        blocked_patrol = obstacles | enemy_cells

        # 有近敌 → 追击（V+R 编队：V推进承盾、R射击/跟随）
        if nearby_enemies:
            tgt_enemy = min(nearby_enemies, key=lambda e: manhattan(anchor.position, e.last_pos))
            tgt_pos = tgt_enemy.last_pos
            # 先锋朝敌人推进
            if v is not None and v.id not in _patrol_moved:
                d = step_toward(v.position, tgt_pos, blocked_patrol)
                if d:
                    _mv(v, d)
                _patrol_moved.add(v.id)
            # 游侠：射程内主攻射击；射程外主动逼近（不再被动跟随编队）
            if r is not None and r.id not in _patrol_moved:
                _ranger_engage(r, v, tgt_pos, blocked=blocked_patrol)
                _patrol_moved.add(r.id)
            if targets is not None:
                if v is not None:
                    targets[v.id] = tgt_pos
                if r is not None:
                    targets[r.id] = tgt_pos
            continue

        # 无近敌 → 巡逻环目标（已在上方编队分配中处理，这里兜底未处理的单位）
        tgt = patrol.patrol_targets.get(anchor.id) if anchor else None
        if tgt and anchor.position != tgt:
            # 先锋等待游侠叠加（无近敌）：有游侠未同格→先锋不动、游侠靠拢
            if _vr_needs_stack_wait(v, r):
                _group_trail(r, v, v.position, blocked_patrol)
                _patrol_moved.add(v.id)
                _patrol_moved.add(r.id)
                if targets is not None:
                    targets[v.id] = v.position
                    targets[r.id] = v.position
                continue
            if v is not None and v.id not in _patrol_moved:
                d = step_toward(v.position, tgt, blocked_patrol)
                if d:
                    _mv(v, d)
                _patrol_moved.add(v.id)
            if r is not None and r.id not in _patrol_moved:
                _group_trail(r, v, tgt, blocked_patrol)
                _patrol_moved.add(r.id)
            if targets is not None:
                if v is not None:
                    targets[v.id] = tgt
                if r is not None:
                    targets[r.id] = tgt


def plan_worker_blockade(turn, threat_cells: set[Position], core_pos: Position,
                          obstacles: set[Position], enemy_cells: set[Position],
                          state: "AgentState", carrier_id=None) -> None:
    """威胁出现时抽调工人回防，占据「敌方朝 Core 一侧前格」以封堵敌方单位的推进路线。

    只抽最多 CORE_BLOCK_WORKERS 个工人；优先非载货、且离 Core 较近者（更快到位），
    搬运工(carrier)与载货工不参与（他们本就在向 Core 交付）。

    封堵到位后工人**原地卡死**，不再每 tick 跟随敌方位置往前挪；只有当被封堵的敌方
    改变推进方向（朝 Core 的 step_toward delta 变化，即敌方绕路/改向）时，才重新计算
    封堵点并移动拦截。这样「明明堵住了却跟着敌人往前走」的抖动不再发生：敌方被前格
    挡住无法推进→位置不变→工人保持原地封锁；敌方改向绕路→工人换到新前格封堵。
    """
    # 威胁消失：清空所有工人封堵记忆，让其回归正常采集/探索任务
    if not threat_cells or core_pos is None:
        for w in turn.workers:
            ws = state.worker_states.get(w.id)
            if ws is not None:
                ws.blockade_cell = None
                ws.blockade_enemy_dir = None
        return
    blocked = obstacles | enemy_cells
    # 可见敌方进攻单位：用于推进方向判断与封堵前格（记忆敌人方向未知，到位后保持封锁）
    vis_enemies = [e for e in turn.visible_enemies
                   if (e.unit_type if hasattr(e, "unit_type") else UnitType.WORKER) in ENEMY_COMBAT_TYPES]
    cands = [w for w in turn.workers
             if w.id not in state.manual.overridden_units and w.id != carrier_id]
    if not cands:
        return
    # 载货工排后面（优先抽空手工），再按离 Core 距离升序（更快到位封堵）
    cands.sort(key=lambda w: (w.cargo > 0, manhattan(w.position, core_pos)))
    chosen = cands[:CORE_BLOCK_WORKERS]
    for w in chosen:
        ws = state.worker_states.setdefault(w.id, WorkerState())
        # 选封堵目标：优先最近可见敌（可判断方向），否则用最近威胁格兜底
        if vis_enemies:
            tgt_e = min(vis_enemies, key=lambda e: manhattan(w.position, e.position))
            tgt = tgt_e.position
            sd = step_toward(tgt, core_pos, obstacles)
            enemy_dir = sd.delta if sd else None
        else:
            # 无可见敌（仅记忆威胁）→ 维持当前封堵点不动，不盲目移动
            if ws.blockade_cell is not None and w.position == ws.blockade_cell:
                continue
            tgt = min(threat_cells, key=lambda c: manhattan(w.position, c))
            enemy_dir = None
        front = _front_cell(tgt, core_pos, obstacles)
        goal = front if (front and front not in blocked and front != core_pos) else tgt
        # 已就位：仅当敌方改向（或封锁点失效）才移动，否则原地卡死继续封锁
        if w.position == ws.blockade_cell and ws.blockade_cell is not None:
            changed = (enemy_dir is not None and ws.blockade_enemy_dir is not None
                       and enemy_dir != ws.blockade_enemy_dir)
            if not changed:
                continue  # 堵住且敌方未改向 → 原地不动
            ws.blockade_cell = None  # 敌方改向 → 重新占位拦截
        # 重新占位 / 移动
        ws.blockade_cell = goal
        ws.blockade_enemy_dir = enemy_dir
        if w.position == goal:
            continue  # 已到位，原地卡死
        d = step_toward(w.position, goal, blocked)
        if d:
            _mv(w, d)
            if state.unit_targets is not None:
                state.unit_targets[w.id] = goal


# =========================================================================== #
# Unit 回血
# =========================================================================== #

def plan_heal_units(turn) -> None:
    """战后给与静止 Core 同格且不满血的 Unit 回血。"""
    core = turn.core
    if core is None:
        return
    for u in turn.units:
        if u.position != core.position:
            continue
        max_hp_val = MAX_HP.get(u.unit_type, 2)
        if u.hp < max_hp_val and turn.resources > 0:
            try:
                u.heal()
            except Exception:
                pass


# =========================================================================== #
# 主决策入口
# =========================================================================== #

class EventLog:
    """记录每 Tick 的态势与语义事件，供可视化 UI 回放完整行动路线与事件流。"""

    def __init__(self):
        self.prev_hp: dict[str, int] = {}
        self.prev_enemy_hp: dict[str, int] = {}
        self.obs_hash = None
        self.ticks: list[dict] = []
        self._since_compact = 0
        # 分片流状态
        self.shard_seq = 0
        self.cur_shard: list[dict] = []
        self._map_since = MAP_SNAPSHOT_EVERY  # 保证第一帧就写一次地图快照

    def record(self, turn, state: AgentState) -> None:
        core = turn.core
        core_data = None
        if core is not None:
            core_data = {"pos": list(core.position),
                         "hp": getattr(core, "hp", 0),
                         "shield": getattr(core, "shield", 0)}
        units = []
        for u in getattr(turn, "units", []):
            ut = u.unit_type.value if hasattr(u.unit_type, "value") else str(u.unit_type)
            tgt = state.unit_targets.get(u.id)
            md = _MOVE_DIRS.get(u.id)
            units.append({
                "id": str(u.id), "type": ut,
                "pos": list(u.position), "hp": getattr(u, "hp", 0),
                "cargo": getattr(u, "cargo", 0),
                "target": list(tgt) if tgt else None,
                "dir": list(md) if md is not None else None,
            })
        enemies = []
        for e in getattr(turn, "visible_enemies", []):
            # 敌方核心是 CoreView、无 unit_type；单位才有。统一安全取值
            ut = getattr(e, "unit_type", None)
            if ut is not None:
                et = ut.value if hasattr(ut, "value") else str(ut)
            else:
                et = getattr(e, "kind", "ENEMY")
            enemies.append({"id": str(getattr(e, "id", "")), "type": et,
                            "pos": list(e.position), "hp": getattr(e, "hp", 0)})

        # 障碍只在变化时落盘，减小体积；
        # 但每个新分片的第一帧强制写完整障碍 → 保证每个分片自包含，
        # 旧分片被环形缓冲淘汰后不会把"唯一那份障碍数据"一起带走。
        obs = state.map_memory.obstacles
        oh = hash(frozenset(obs)) if obs else 0
        obs_changed = (oh != self.obs_hash)
        force_full = (STREAM_SHARD_DIR and not self.cur_shard)
        obstacles = [list(p) for p in obs] if (obs_changed or force_full) else None
        self.obs_hash = oh

        evs: list[dict] = []
        for ev in getattr(turn, "events", []):
            et = getattr(ev, "event_type", None)
            if et:
                evs.append({"type": et, "unit": None,
                            "pos": list(getattr(ev, "position", [0, 0]) or [0, 0]),
                            "detail": str(getattr(ev, "resource_amount", ""))})
        # 被攻击：hp 下降
        for u in units:
            pid = u["id"]
            prev = self.prev_hp.get(pid)
            if prev is not None and prev > u["hp"]:
                evs.append({"type": "attacked", "unit": pid, "pos": u["pos"],
                            "detail": f"HP {prev}→{u['hp']}"})
            self.prev_hp[pid] = u["hp"]
        # 攻击敌人：敌人 hp 下降 → 我方对其造成伤害（覆盖自卫/进攻）
        for e in enemies:
            eid = e["id"]
            prev = self.prev_enemy_hp.get(eid)
            if prev is not None and prev > e["hp"]:
                evs.append({"type": "attack", "unit": eid, "pos": e["pos"],
                            "detail": f"敌方 HP {prev}→{e['hp']}"})
            self.prev_enemy_hp[eid] = e["hp"]
        # 工人任务语义
        for w in getattr(turn, "workers", []):
            ws = state.worker_states.get(w.id)
            if not ws:
                continue
            wid = str(w.id)
            wp = list(w.position)
            near = state.enemy_memory.get_nearby_enemies(w.position, 5, turn.tick)
            if near:
                evs.append({"type": "encounter_enemy", "unit": wid, "pos": wp,
                            "detail": f"{len(near)} 个敌方单位在附近"})
            t = ws.task.value
            if t == "exploring":
                evs.append({"type": "explore", "unit": wid, "pos": wp,
                            "detail": (f"定向探索 航向{ws.scout_heading} 半径{ws.scout_radius}"
                                       if ws.scout_heading else "已探索区游走采集")})
            elif t == "fleeing":
                evs.append({"type": "flee", "unit": wid, "pos": wp,
                            "detail": "受伤撤退"})
            elif t == "delivering":
                evs.append({"type": "deposit_move", "unit": wid, "pos": wp,
                            "detail": "带回核心交付"})
            elif t == "harvesting":
                evs.append({"type": "harvest", "unit": wid, "pos": wp,
                            "detail": "采集资源"})
        # 进攻事件
        if state.combat.engaging:
            tgt = state.combat.primary_target_core
            if tgt is not None:
                # primary_target_core 可能是 CoreView(.position) 或 EnemyRecord(.last_pos)
                tp = list(getattr(tgt, "position", None)
                          or getattr(tgt, "last_pos", None)
                          or ([0, 0]))
            else:
                tp = list(core_data["pos"]) if core_data else [0, 0]
            evs.append({"type": "attack", "unit": None, "pos": tp,
                        "detail": "集中火力攻击敌方核心" if tgt else "发起进攻"})

        # 死亡触发的禁止/危险区域（供 monitor 画圆）：圆心/半径/状态/记录敌数
        dz = [{"center": list(z.center), "radius": z.radius, "state": z.state,
               "recorded": z.recorded_enemy_count} for z in state.danger_zones]
        # 死亡事件（供前端画 ❌ + 事件流显示）
        de = list(state.death_events)  # 本 tick 新增的阵亡
        # 把死亡事件也写入 events 流（供事件过滤面板显示）
        for dv in de:
            ut_cn = {"VANGUARD": "先锋", "RANGER": "游侠"}.get(dv["unit_type"], dv["unit_type"])
            evs.append({"type": "death", "unit": None, "pos": dv["pos"],
                        "detail": f"{ut_cn}阵亡"})
        # 单位计数（供状态面板）
        w_count = len(getattr(turn, "workers", []))
        v_count = len(getattr(turn, "vanguards", []))
        r_count = len(getattr(turn, "rangers", []))
        # 下一步生产单位
        pop = len(getattr(turn, "units", []))
        next_prod = next_combat_unit(v_count, r_count).name if (
            not state.economy_frozen and turn.resources >= unit_cost(next_combat_unit(v_count, r_count), pop) + SPAWN_BUFFER
        ) else None
        # 清空本 tick 死亡事件（避免下 tick 重复记录）
        state.death_events.clear()
        self.ticks.append({
            "tick": turn.tick, "core": core_data, "resources": turn.resources,
            "units": units, "enemies": enemies, "obstacles": obstacles,
            "resources_cells": [list(p) for p in getattr(turn, "resource_cells", set())],
            "events": evs, "danger_zones": dz,
            "death_events": de,
            "unit_counts": {"worker": w_count, "vanguard": v_count, "ranger": r_count},
            "next_production": next_prod,
        })
        # 内存限长，防止长局 ticks 列表无限膨胀
        if len(self.ticks) > MAX_TICKS_MEMORY:
            del self.ticks[:-MAX_TICKS_MEMORY]
        # 可选：单文件流（追加+定期重写为最近 N 条）
        if EVENT_STREAM_PATH:
            self.append_stream(EVENT_STREAM_PATH, self.ticks[-1])
            self._since_compact += 1
            if self._since_compact >= STREAM_COMPACT_EVERY:
                self._rewrite_stream(EVENT_STREAM_PATH)
                self._since_compact = 0
        # 默认：分片流（每 SHARD_SIZE 帧一个文件，仅保留最近 MAX_SHARDS 个）
        if STREAM_SHARD_DIR:
            self._write_shard(self.ticks[-1])
            # 地图层永久快照：障碍/资源记忆/已探索分片。与分片流解耦，
            # 永不淘汰 → 重开地图一定能画出全部已知石头，而不是只剩最近窗口。
            self._map_since += 1
            if obs_changed or self._map_since >= MAP_SNAPSHOT_EVERY:
                self._map_since = 0
                self._write_map_snapshot(turn, state)
            # 敌方敌情标注（消失点 ! + 三角定位核心区）每 tick 更新，体量小、独立文件
            self._write_enemy_marks(turn, state)

    def append_stream(self, path: str, tick_obj: dict) -> None:
        """把单 tick 快照追加到单文件实时流（一行一个 JSON，向后兼容）。"""
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(tick_obj, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _rewrite_stream(self, path: str) -> None:
        """把单文件流重写为内存中最近 MAX_TICKS_MEMORY 条（环形边界）。"""
        try:
            with open(path, "w", encoding="utf-8") as f:
                for t in self.ticks:
                    f.write(json.dumps(t, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def to_dict(self, meta: dict | None = None) -> dict:
        return {"meta": meta or {}, "ticks": self.ticks}

    def save(self, path: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 分片流：每 SHARD_SIZE 帧一个文件，仅保留最近 MAX_SHARDS 个（环形缓冲）
    # ------------------------------------------------------------------ #
    def _shard_path(self, seq: int) -> str:
        return os.path.join(STREAM_SHARD_DIR, f"shard_{seq:05d}.jsonl")

    def _write_map_snapshot(self, turn, state: AgentState) -> None:
        """写 stream/map.json：完整地图记忆快照，独立于分片流、永不淘汰。

        分片流是"最近 N 帧"的环形缓冲，只适合承载动态态势；
        障碍这类"一旦看到就永久有效"的静态信息必须单独持久化，
        否则携带它的那一帧被淘汰后前端就再也拿不到石头了。
        """
        try:
            mm = state.map_memory
            snap = {
                "tick": turn.tick,
                "sector_size": SECTOR_SIZE,
                "core": list(turn.core.position) if turn.core is not None else None,
                "obstacles": [list(p) for p in mm.obstacles],
                "resource_memory": [[p[0], p[1], t]
                                    for p, t in mm.resource_memory.items()],
                "explored_cells": [list(p) for p in mm.explored_cells],
                "explored_sectors": [list(s) for s in mm.explored_sectors],
            }
            os.makedirs(STREAM_SHARD_DIR, exist_ok=True)
            path = os.path.join(STREAM_SHARD_DIR, "map.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, path)  # 原子替换，前端不会读到半个文件
        except Exception:
            pass

    def _write_enemy_marks(self, turn, state: AgentState) -> None:
        """写 stream/enemy_marks.json：敌方消失点(橙色 !) + 三角定位核心区(橙色块)。

        与 map.json 解耦：这部分每 tick 变化且体量小，单独成文件可做到近实时刷新，
        不必等 MAP_SNAPSHOT_EVERY(50 tick) 才更新感叹号。
        """
        try:
            em = state.enemy_memory
            our_core = tuple(turn.core.position) if turn.core is not None else None
            snap = {
                "tick": turn.tick,
                "enemy_lost": em.lost_contacts,
                "enemy_core_zones": em.estimate_core_zones(our_core),
            }
            os.makedirs(STREAM_SHARD_DIR, exist_ok=True)
            path = os.path.join(STREAM_SHARD_DIR, "enemy_marks.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, path)  # 原子替换
        except Exception:
            pass

    def _write_shard(self, frame: dict) -> None:
        """把当前帧写入分片；满 SHARD_SIZE 则滚动到下一片，并丢弃超出窗口的旧片。"""
        try:
            os.makedirs(STREAM_SHARD_DIR, exist_ok=True)
            self.cur_shard.append(frame)
            # 当前分片（含未满部分）每帧覆盖写，保证监控端能读到最新
            with open(self._shard_path(self.shard_seq), "w", encoding="utf-8") as f:
                for t in self.cur_shard:
                    f.write(json.dumps(t, ensure_ascii=False) + "\n")
            # 满了 → 滚到下一片（上一片文件保留为静态历史分片）
            if len(self.cur_shard) >= SHARD_SIZE:
                self.shard_seq += 1
                self.cur_shard = []
            # 丢弃掉出环形窗口的旧分片
            drop = self.shard_seq - MAX_SHARDS
            if drop >= 0:
                p = self._shard_path(drop)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            # 写索引（监控端先读它再决定取哪些分片）
            idx = {
                "latest_tick": frame["tick"],
                "latest_shard": self.shard_seq,
                "kept_min": max(0, self.shard_seq - MAX_SHARDS + 1),
                "kept_max": self.shard_seq,
                "shard_size": SHARD_SIZE,
                "max_shards": MAX_SHARDS,
                "latest_shard_frames": len(self.cur_shard),
            }
            with open(os.path.join(STREAM_SHARD_DIR, "index.json"),
                      "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
        except Exception:
            pass


# =========================================================================== #
# 禁止 / 危险区域（战斗单位阵亡触发的战术规避区）
# =========================================================================== #
@dataclass
class DangerZone:
    """战斗单位阵亡时建立的规避圆。

    - center：圆心 = 触发时刻敌方"推进最远"（距我方 Core 曼哈顿最远）的战斗单位坐标。
    - radius：曼哈顿半径 = 圆心 → 我方最后死亡战斗单位坐标（覆盖阵亡走廊）。
    - recorded_enemy_count：建区时敌方战斗单位数量。
    - state："forbidden" 禁止区（探索工人/探索战斗组绕行）；
             "danger" 危险区（我方探索战斗组数 > recorded_enemy_count 后转入，
             派遣全部探索战斗组压入清场）。
    - cleared：本 tick 圆内已无敌方进攻单位 → 标记解除。
    """
    center: Position
    radius: int
    recorded_enemy_count: int
    state: str = "forbidden"
    created_tick: int = 0
    deaths: list[Position] = field(default_factory=list)
    cleared: bool = False


def _enemy_combat_units(turn) -> list:
    """当前可见敌方进攻单位（VANGUARD/RANGER）列表。"""
    out = []
    for e in getattr(turn, "visible_enemies", []):
        ut = getattr(e, "unit_type", None)
        if ut in ENEMY_COMBAT_TYPES:
            out.append(e)
    return out


def _manhattan_disk(center: Position, radius: int, max_cells: int = DANGER_DISK_MAX_CELLS) -> set[Position]:
    """返回以 center 为心、曼哈顿半径 radius 内的所有格（限制上限防极端半径拖慢寻路）。"""
    cells: set[Position] = set()
    if radius <= 0:
        return {center}
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if abs(dx) + abs(dy) <= radius:
                cells.add((center[0] + dx, center[1] + dy))
                if len(cells) >= max_cells:
                    return cells
    return cells


def _enemies_in_zone(turn, z: DangerZone) -> list:
    """圆内（曼哈顿<=radius）的敌方进攻单位。"""
    return [e for e in _enemy_combat_units(turn)
            if manhattan(tuple(e.position), z.center) <= z.radius]


def _danger_update(turn, state: "AgentState", core_pos: Optional[Position],
                   explore_pool: list) -> None:
    """每 tick 维护危险区域状态机：死亡检测→建禁止区；禁止→危险转换；无威胁解除。

    触发建区：存在我方战斗单位阵亡 且 敌方战斗单位数 > 我方探索战斗组数*FORBID_TRIGGER_RATIO。
    """
    # 1) 更新已知战斗单位位置+兵种，并检测本 tick 阵亡的我方战斗单位（V/R）
    cur = [u for u in getattr(turn, "units", [])
           if u.unit_type != UnitType.WORKER and u.id not in state.manual.overridden_units]
    cur_ids = {u.id for u in cur}
    died_ids = set(state.known_combat_pos) - cur_ids
    died = []
    for i in died_ids:
        if i in state.known_combat_pos:
            entry = state.known_combat_pos[i]
            # 兼容新旧格式：新格式=(position, unit_type)，旧格式=position
            if isinstance(entry, tuple) and len(entry) == 2:
                pos, ut = entry
            else:
                pos, ut = entry, "UNKNOWN"
            died.append((pos, ut))
            # 记录死亡事件（供前端画 ❌ + 事件流）
            state.death_events.append({
                "pos": list(pos) if hasattr(pos, '__iter__') else [pos, pos],
                "unit_type": ut,
                "tick": turn.tick,
            })
    # 清除已阵亡单位的位置记录，避免后续 tick 重复判定"死亡"而反复建区
    for i in died_ids:
        state.known_combat_pos.pop(i, None)
    for u in cur:
        state.known_combat_pos[u.id] = (u.position, u.unit_type.name if hasattr(u.unit_type, 'name') else str(u.unit_type))

    # 2) 解除已无威胁的区域：圆内无敌方进攻单位即清除（禁止区与危险区通用）
    for z in state.danger_zones:
        if not _enemies_in_zone(turn, z):
            z.cleared = True
    state.danger_zones = [z for z in state.danger_zones if not z.cleared]

    if not DANGER_ENABLED:
        return

    # 3) 阵亡且满足条件 → 新建禁止区
    if died:
        enemy_combat = _enemy_combat_units(turn)
        ec = len(enemy_combat)
        explore_groups = len(_form_vr_pairs(explore_pool))
        if ec > explore_groups * FORBID_TRIGGER_RATIO:
            # 圆心 = 敌方推进最远（距我方 Core 曼哈顿最远）的战斗单位
            if core_pos is not None and enemy_combat:
                center = max(enemy_combat, key=lambda e: manhattan(tuple(e.position), core_pos)).position
            else:
                center = died[0][0]
            center = tuple(center)
            # 我方最后死亡单位：取离 Core 最远（最前）的死亡点作半径锚（覆盖阵亡走廊）
            last_death = max(died, key=lambda p: manhattan(p[0], core_pos)) if core_pos else died[0]
            radius = manhattan(center, last_death[0])
            z = DangerZone(center=center, radius=radius, recorded_enemy_count=ec,
                           state="forbidden", created_tick=turn.tick, deaths=[p[0] for p in died])
            state.danger_zones.append(z)
            print(f"[危险区] 战斗单位阵亡{len(died)}个@{[p[0] for p in died]}；敌战斗单位{ec} > 探索组{explore_groups}"
                  f" → 建禁止区 圆心{center} 半径{radius} 记录敌数{ec}", flush=True)

    # 4) 禁止→危险 转换：我方探索战斗组数 > 记录敌数时，派遣全部探索战斗组清场
    for z in state.danger_zones:
        if z.state == "forbidden":
            explore_groups = len(_form_vr_pairs(explore_pool))
            if explore_groups > z.recorded_enemy_count:
                z.state = "danger"
                print(f"[危险区] 探索组{explore_groups} > 记录敌数{z.recorded_enemy_count}"
                      f" → 禁止区转危险区(圆心{z.center})，全部探索战斗组压入清场", flush=True)


@dataclass
class AgentState:
    """跨 Tick 持有的完整 Agent 状态。"""
    map_memory: MapMemory = field(default_factory=MapMemory)
    enemy_memory: EnemyMemory = field(default_factory=EnemyMemory)
    worker_states: dict[UUID, WorkerState] = field(default_factory=dict)
    combat: CombatState = field(default_factory=CombatState)
    patrol: PatrolState = field(default_factory=PatrolState)
    manual: ManualOverride = field(default_factory=ManualOverride)
    carrier_state: dict = field(default_factory=lambda: {"id": None, "carrying": False})
    event_log: EventLog = field(default_factory=EventLog)
    explorer_targets: dict[UUID, Optional[tuple[int, int]]] = field(default_factory=dict)
    # 每 Tick 规划时重填：我方每个单位"当前终点目标坐标"，供 monitor.html 画虚线连接。
    # 键=单位 UUID，值=目标格 (x,y)。每 tick 在 plan_turn_v2 开头清空，规划各阶段写入。
    unit_targets: dict[UUID, tuple[int, int]] = field(default_factory=dict)
    match_checked: bool = False   # 本进程是否已做过"换局"判定
    # 经济冻结模式：资源容量 >= ECONOMY_FREEZE_CAPACITY 时记录当前单位峰值，
    # 之后只补损失不新造，全力攒资源。
    economy_frozen: bool = False
    economy_peak_workers: int = 0
    economy_peak_vanguards: int = 0
    economy_peak_rangers: int = 0
    # 生产调度计数（用户指定比例逻辑用）：
    #   combat_produced  = 累计造出的战斗单位数（工人不计入）。
    #   combat_since_worker = 距上次"补工人"已造出的战斗单位数，到 2 时插入一次工人补产。
    combat_produced: int = 0
    combat_since_worker: int = 0
    # 已确认的"可进攻"敌Core：{pos_tuple: {"pos":(x,y), "discovered_tick":int}}。
    # 由 _update_attack_targets 每 tick 维护：工人或任意单位发现敌Core且周围
    # CORE_ATTACK_SAFE_RADIUS 内无敌方进攻单位→登记；超过 ATTACK_TARGET_TTL tick
    # 未再确认→清理。供 plan_attack_dispatch 抽调最近探索组去进攻。
    attack_targets: dict = field(default_factory=dict)
    # 死亡触发的禁止/危险区域（见 DangerZone / _danger_update）。
    danger_zones: list = field(default_factory=list)
    # 我方战斗单位(V/R)上 tick 已知位置+兵种（用于阵亡检测：id 从本集合消失即判定阵亡）
    known_combat_pos: dict = field(default_factory=dict)
    # 本 tick 新增的死亡事件列表（每 tick 在 _danger_update 中追加，record 后清空）
    # 每项：{"pos": (x,y), "unit_type": str, "tick": int}
    death_events: list = field(default_factory=list)


def next_combat_unit(v: int, r: int):
    """按用户指定顺序决定下一个该造的战斗单位：

    1) 先锋不足 3 个 → 造先锋（V<3）
    2) 游侠不足 3 个 → 造游侠（R<3）
    3) 均已满 3 个 → 先锋/游侠交替（每多造一个，奇偶切换一次）

    返回 UnitType.VANGUARD / UnitType.RANGER。战斗单位无硬上限（由 95 资源
    经济冻结规则最终约束整体规模），故该函数不会返回 None。
    """
    if v < 3:
        return UnitType.VANGUARD
    if r < 3:
        return UnitType.RANGER
    # 3V+3R 之后进入交替：extras = 超出"3+3"的额外战斗单位数；
    # 偶→先锋、奇→游侠，保证两者均衡增长。
    extras = (v - 3) + (r - 3)
    return UnitType.VANGUARD if extras % 2 == 0 else UnitType.RANGER


def plan_turn_v2(turn, state: AgentState, game=None) -> None:
    """每个 Tick 的主决策入口（v2 黑暗森林版）。"""
    # 0) 换局检测（仅进程启动后第一个 tick 判一次）：
    #    存档里的 last_core 是上一次运行时 Core 的位置。Core 现在会向信标迁移，
    #    但存档每次保存都会刷新 last_core，所以同一局重启时两者必然很近；
    #    差得很远只能说明换了对局 → 旧坐标系的障碍/资源全部作废。
    if not state.match_checked:
        state.match_checked = True
        lc = state.map_memory.last_core
        cp = tuple(turn.core.position) if turn.core is not None else None
        if lc and cp and manhattan(lc, cp) > MATCH_RESET_DIST:
            stale = len(state.map_memory.obstacles)
            state.map_memory.reset_terrain()
            state.enemy_memory.records.clear()
            print(f"[换局] 存档 Core@{lc} → 当前 Core@{cp} 距离 "
                  f"{manhattan(lc, cp)} > {MATCH_RESET_DIST}，"
                  f"丢弃上局脏数据（障碍 {stale} 个）", flush=True)

    # 1) 更新地图记忆 & 敌方记忆
    state.map_memory.update(turn)
    state.enemy_memory.update(turn)
    state.enemy_memory.clear_resolved_contacts(turn)   # 到达消失点且周围无敌人→取消 !

    # 2) 手动覆盖检测
    if game:
        state.manual.update_from_receipt(game)

    obstacles = state.map_memory.obstacles
    core_pos = turn.core.position if turn.core else None

    # 信标进军检测：Core 开始朝冠军信标迁移时，探索工/探索战斗组应有 80% 偏向信标方向。
    marching, beacon_dirs = beacon_march_info(turn, core_pos)
    if marching:
        print(f"[信标进军] 探索偏置→主方向桶{beacon_dirs}，80% 探索单位朝信标展开"
              f"（范围仍限 EXPLORER_MAX_RING）", flush=True)

    # 4.4) 战斗单位分流（提前：死亡触发的危险区判定需依赖"探索战斗组数量"）。
    #      注意始终按原比例探索/巡逻（威胁时再按比例调配，不无脑召回）。
    patrol_pool, explore_pool = _split_combat_units(turn, state.manual, defend_all=False)
    # 4.5) 死亡危险区状态机：阵亡检测→建禁止区；禁止→危险转换；圆内无威胁→解除。
    _danger_update(turn, state, core_pos, explore_pool)
    # 禁止区栅格（探索工人/探索战斗组绕行用）与危险区列表（探索战斗组清场用）。
    forbidden_cells: set[Position] = set()
    danger_zones_active: list = []
    for z in state.danger_zones:
        if z.state == "forbidden":
            forbidden_cells |= _manhattan_disk(z.center, z.radius)
        elif z.state == "danger":
            danger_zones_active.append(z)

    # 每 tick 的"已占用资源"表：本 tick 内被工人认领的资源（防同 tick 重复派单）
    used_resources: set[Position] = set()

    # 每 tick 重置"单位终点目标"映射（避免上一 tick 的过期目标残留）
    state.unit_targets.clear()
    # 每 tick 重置"单位移动方向"映射（供地图方向箭头）
    _MOVE_DIRS.clear()

    # 敌对单位位置 → 动态障碍（仅供绕行当前可见敌；预测位置仅用于
    # should_retreat / get_nearby_enemies 的警戒判断，不当硬障碍以免过期预测绕远路）
    enemy_cells: set[Position] = set()
    for e in turn.visible_enemies:
        p = getattr(e, "position", None)
        if p:
            enemy_cells.add(p)

    # ---- 调试：记录可见资源首次出现 & 前若干 tick 的态势 ----
    if os.environ.get("AH_DEBUG"):
        try:
            with open("arena_debug.log", "a") as _f:
                if turn.resource_cells:
                    _f.write(f"[t{turn.tick}] RESOURCE_CELLS={sorted(turn.resource_cells)}\n")
                if turn.tick - getattr(state, "_dbg_t0", turn.tick) < 25:
                    _wp = {str(w.id)[:6]: w.position for w in turn.workers}
                    _f.write(f"[t{turn.tick}] core={core_pos} workers={_wp} obstacles={len(turn.obstacle_cells)}\n")
        except Exception:
            pass

    # 2.5) 重置工人目标格冲突协调表（每 tick 重建我方占位）
    reset_move_coord(turn)

    # 3) Beacon 搬运工
    carrier_id = plan_beacon_worker(turn, state.carrier_state, obstacles, enemy_cells)
    if carrier_id is not None and BEACON_CELL is not None:
        state.unit_targets[carrier_id] = BEACON_CELL

    # 3.2) 全局就近派单：把地图资源图标指派给最近的空背包工人(探索工除外，保探索)。
    # 必须在挑选探索工之前执行——被派单的工人 assigned_resource 已非 None，
    # 自然不会被抽去做探索工，从而既清空资源图标又不挤占探索产能。
    plan_global_resource_assignment(turn, state, carrier_id, enemy_cells, used_resources)

    # 3.5) 专职探索小队：工人>1 时抽部分工人做定向探索（不同初始航向错开）
    # 不再围绕 Core 螺旋；航向、半径、换向都由 _plan_directional_explore 自理。
    explorer_ids: set[UUID] = set()
    num_workers = len(turn.workers)
    if num_workers >= MIN_WORKERS_FOR_EXPLORERS:
        desired = min(MAX_EXPLORERS, max(1, round(num_workers * EXPLORER_RATIO)))
        free = [w for w in turn.workers
                if w.cargo == 0
                and w.id != carrier_id
                and w.id not in state.manual.overridden_units
                and state.worker_states.get(w.id, WorkerState()).assigned_resource is None]
        for w in free[:desired]:
            explorer_ids.add(w.id)
    # 角色标记持久化：被选中→探索工；空手且未被选中且没在接手→回落为采集工。
    # 带货的不动标记，保证"探索工带货返程"仍被识别为探索工（交接规则依赖它）。
    for w in turn.workers:
        ws0 = state.worker_states.setdefault(w.id, WorkerState())
        if w.id in explorer_ids:
            ws0.is_scout = True
        elif w.cargo == 0 and not ws0.takeover_active:
            ws0.is_scout = False

    # 信标进军期：探索工中 80% 偏向信标方向（其余 20% 维持全向覆盖，防地图盲区）。
    # 按稳定排序的 ID 切前 80%，保证"哪 80%"每 tick 一致、不抖动。
    if marching and explorer_ids:
        _ex_sorted = sorted(explorer_ids, key=lambda u: str(u))
        _n_biased = max(1, round(len(_ex_sorted) * 0.8))
        beacon_bias_explorers: set = set(_ex_sorted[:_n_biased])
    else:
        beacon_bias_explorers = set()

    # 4) Worker 一一对应资源分配 + 采集→交付→探索循环
    # 探索工"本 tick 认领的前沿扇区"集合：保证多个探索工各自认领不同扇区（扇形散开）。
    claimed_sectors: set = set()
    for w in turn.workers:
        if carrier_id and w.id == carrier_id:
            continue  # 搬运工单独处理
        if w.id in state.manual.overridden_units:
            continue  # 手动控制的不干预
        ws = state.worker_states.setdefault(w.id, WorkerState())
        plan_worker_collect(w, ws, turn, state.map_memory,
                            core_pos, obstacles, used_resources, enemy_cells,
                            is_explorer=(w.id in explorer_ids or ws.is_scout),
                            worker_states=state.worker_states,
                            claimed_sectors=claimed_sectors,
                            beacon_dirs=beacon_dirs,
                            bias_explorer=(w.id in beacon_bias_explorers),
                            forbidden_cells=forbidden_cells)
        # 记录工人终点目标（资源/Core/探索航点），供地图虚线展示
        if ws.target is not None:
            state.unit_targets[w.id] = ws.target

    # 5) 战斗决策（反应式：遇敌交战/撤退）
    plan_combat(turn, state.combat, state.enemy_memory, obstacles, state.manual, enemy_cells)

    # 5b) 威胁判定：Core 周围 CORE_BLOCK_RADIUS(默认20) 曼哈顿内出现【敌方进攻单位】→ 触发 Core 防御调配。
    #     注意：只认敌方进攻单位(VANGUARD/RANGER)。20 格内只有敌方工人/Core 等非进攻单位时，
    #     不触发死守（不把探索组调回），由巡逻组在巡逻范围内直接追击攻击（见 plan_patrol nearby_enemies）。
    #     其余单位继续各司其职（巡逻/探索/采集）。
    enemy_combat_near: list[Position] = []   # 敌方进攻单位在 CORE_BLOCK_RADIUS 内的位置（每单位一条）
    if core_pos is not None:
        for e in turn.visible_enemies:
            ut = e.unit_type if hasattr(e, "unit_type") else UnitType.WORKER
            if ut in ENEMY_COMBAT_TYPES and manhattan(e.position, core_pos) <= CORE_BLOCK_RADIUS:
                enemy_combat_near.append(tuple(e.position))
        for rec in state.enemy_memory.records.values():
            if rec.unit_type not in ENEMY_COMBAT_TYPES:
                continue
            lp = getattr(rec, "last_pos", None)
            if lp and manhattan(lp, core_pos) <= CORE_BLOCK_RADIUS:
                enemy_combat_near.append(tuple(lp))
    threat_cells: set[Position] = set(enemy_combat_near)
    threat_near = bool(threat_cells)

    # 5b-1.5) 维护"可进攻"敌Core目标表：工人或任意单位发现敌Core且周围无敌方进攻单位
    #          → 登记；过期清理。供下方进攻派遣(6b-0)抽调最近探索组去进攻。
    _update_attack_targets(turn, state, turn.tick)

    # 5b-1) 战斗单位分流已在 4.4 提前完成（死亡危险区判定依赖探索组数量）；
    #       此处 patrol_pool / explore_pool 复用该结果。是否需召回探索组由下方"按比例调配"决定。

    # 5b-2) Core 防御调配（按比例，不无脑召回）：见 _core_defense_dispatch 注释。
    if threat_near:
        state.combat.retreat_mode = False
        state.combat.engaging = True
        recalled = _core_defense_dispatch(patrol_pool, explore_pool,
                                          enemy_combat_near, core_pos)
        if recalled:
            print(f"[调配] 巡逻组{len(patrol_pool) - recalled} < 敌{len(enemy_combat_near)} → "
                  f"召回 {recalled} 个最近探索组支援，总战斗单位={len(patrol_pool)}",
                  flush=True)

    # 5c) 探索组交战评估 + 求援再分配（仅在【无 Core 威胁】时生效，避免核心危机时外抽调巡逻单位）：
    #     - 评估探索组遇敌是「进攻」还是「跟踪」（兵力计数见 assess_explore_engagement）；
    #     - 跟踪(兵力劣势)时，调遣最近的巡逻单位来支援（至少保留 1 个围 Core），直至兵力反超转进攻。
    engage = assess_explore_engagement(turn, explore_pool)
    if (not threat_near) and explore_pool and engage.mode == "TRACK" and engage.enemy_pos is not None \
            and patrol_pool and len(patrol_pool) >= 2:
        supporter = min(patrol_pool,
                        key=lambda u: manhattan(u.position, engage.enemy_pos))
        patrol_pool.remove(supporter)
        explore_pool.append(supporter)
        engage.reinforced_this_tick = True
        engage.our_count = len(explore_pool)
        # 求援后若兵力反超 → 本 tick 即转为进攻
        if engage.our_count > engage.enemy_combat_count:
            engage.mode = "ATTACK"
        print(f"[求援] 探索组兵力劣势({engage.our_count} vs 敌{engage.enemy_combat_count})"
              f" → 调遣巡逻单位 {str(supporter.id)[:6]} 支援", flush=True)

    # 6) Core 防御巡逻（有威胁→aggressive 死守：进攻+封堵，绝不后撤；无威胁→普通巡逻，
    #    普通巡逻下巡逻范围内出现任何敌方单位(含非进攻)即直接追击攻击）
    if core_pos and patrol_pool:
        plan_patrol(turn, state.patrol, core_pos, obstacles,
                     state.enemy_memory, state.manual, enemy_cells,
                     patrol_units=patrol_pool, targets=state.unit_targets,
                     aggressive=threat_near)

    # 6b-0) 进攻派遣：从探索池抽调最近的 V+R 组去进攻已确认的"可进攻"敌Core。
    #       被抽调的单位移出 explore_pool，避免下方探索逻辑覆盖其进攻指令。
    #       （进攻单位自己探索时直接看到敌Core → assess_explore_engagement 已判 ATTACK
    #        并由下方 _plan_explore_combat_groups 直接进攻，不依赖此派遣。）
    dispatched_ids: set = set()
    if explore_pool and state.attack_targets:
        dispatched_ids = plan_attack_dispatch(
            turn, state, explore_pool, obstacles, enemy_cells,
            targets=state.unit_targets)
        if dispatched_ids:
            explore_pool = [u for u in explore_pool if u.id not in dispatched_ids]

    # 6b) 迷雾猎杀编组（仅无威胁时：进攻单位以 V+R 组探索战争迷雾、寻敌攻击）
    if explore_pool:
        pairs = _form_vr_pairs(explore_pool)
        _plan_explore_combat_groups(turn, pairs, obstacles, enemy_cells,
                                     state.map_memory, core_pos, engage=engage,
                                     targets=state.unit_targets,
                                     beacon_dirs=beacon_dirs,
                                     forbidden_cells=forbidden_cells,
                                     danger_zones=danger_zones_active)

    # 6c) 工人回防封堵：有威胁时抽调工人占据敌方朝 Core 前格，阻挡敌方单位推进路线
    if threat_near:
        plan_worker_blockade(turn, threat_cells, core_pos, obstacles, enemy_cells,
                              state, carrier_id=carrier_id)

    # 7) Core 动作（恢复 + 生产）
    plan_core_actions(turn, state.combat, state.map_memory, state)

    # 8) Unit 回血
    plan_heal_units(turn)

    # 9) 从事件中更新资源记忆（采集成功/资源耗尽）
    for ev in turn.events:
        if ev.event_type == "HARVEST_SUCCEEDED":
            pos = getattr(ev, 'position', None)
            if pos:
                state.map_memory.mark_resource_depleted(pos)
        elif ev.event_type == "RESOURCE_DEPLETED":
            pos = getattr(ev, 'position', None)
            if pos:
                state.map_memory.mark_resource_depleted(pos)

    # 10) 记录事件日志（供可视化 UI 回放完整行动路线与事件流）
    state.event_log.record(turn, state)


# =========================================================================== #
# 统计
# =========================================================================== #

class Stats:
    def __init__(self):
        self.ticks = 0
        self.peak_resources = 0
        self.total_harvested = 0
        self.harvest_events = 0
        self.deposit_events = 0
        self.combat_ticks = 0

    def update(self, turn, combat: CombatState) -> None:
        self.ticks += 1
        self.peak_resources = max(self.peak_resources, turn.resources)
        if combat.engaging:
            self.combat_ticks += 1
        for ev in turn.events:
            if ev.event_type in ("HARVEST_SUCCEEDED", "CORE_RESOURCES_CAPTURED",
                                  "DEPOSIT_SUCCEEDED"):
                amt = getattr(ev, "resource_amount", None)
                if isinstance(amt, int) and amt > 0:
                    self.total_harvested += amt
                    if ev.event_type == "HARVEST_SUCCEEDED":
                        self.harvest_events += 1
                    elif ev.event_type == "DEPOSIT_SUCCEEDED":
                        self.deposit_events += 1

    def report(self, turn, combat: CombatState, worker_states: dict = None) -> None:
        # 调试：Worker 任务分布 + 位置
        task_info = ""
        pos_info = ""
        if worker_states:
            tasks = {}
            positions = []
            for w in turn.workers:
                ws = worker_states.get(w.id)
                if ws:
                    tasks[ws.task.value] = tasks.get(ws.task.value, 0) + 1
                    tgt = ws.target
                    positions.append(f"{w.position}→{tgt}")
            task_info = " " + ",".join(f"{k}={v}" for k, v in tasks.items())
            pos_info = " W@[" + "|".join(positions) + "]"

        print(
            f"[tick {turn.tick}] "
            f"资源={turn.resources}/{turn.resource_capacity} "
            f"人口={turn.state.population} "
            f"W/V/R={len(turn.workers)}/{len(turn.vanguards)}/{len(turn.rangers)} "
            f"CoreHP={turn.core.hp if turn.core else '?'}/{turn.core.shield if turn.core else '?'} "
            f"峰值={self.peak_resources} "
            f"累计≈{self.total_harvested} "
            f"采{self.harvest_events}/交{self.deposit_events} "
            f"可见资源={len(turn.resource_cells)}"
            f"障碍={len(turn.obstacle_cells)}"
            f"Core@{turn.core.position if turn.core else '?'} "
            f"战斗={'Y' if combat.engaging else 'N'}"
            f"撤退={'Y' if combat.retreat_mode else 'N'}"
            f"{task_info}"
            f"{pos_info}",
            flush=True,
        )


# =========================================================================== #
# 启动
# =========================================================================== #

def load_api_key() -> str:
    key = os.environ.get("ARENA_HERO_API_KEY")
    if key:
        return key.strip()
    for path in ("arena_key.txt",
                 os.path.join(os.path.dirname(__file__), "arena_key.txt")):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                txt = f.read().strip()
                if txt:
                    return txt
    return getpass("Arena Hero API key: ").strip()


def main():
    api_key = load_api_key()
    if not api_key:
        print("未提供 API Key，退出。", file=sys.stderr)
        sys.exit(1)

    # 加载持久化状态
    map_mem, enemy_mem = Persistence.load(PERSISTENCE_FILE)
    print(f"加载持久化: 障碍={len(map_mem.obstacles)} "
          f"资源记忆={len(map_mem.resource_memory)} "
          f"敌方记忆={len(enemy_mem.records)}", flush=True)

    state = AgentState(
        map_memory=map_mem,
        enemy_memory=enemy_mem,
    )
    stats = Stats()

    print("=== Arena Hero Agent v2 启动（黑暗森林战略版）===", flush=True)
    print(f"参数: 生产=5工后造兵→先锋满3→游侠满3→交替V/R，每2兵补1工 "
          f"(W目标={TARGET_WORKERS}，受95资源冻结约束)；"
          f"紧急造游侠上限R={MAX_RANGERS} Beacon={'开' if BEACON_ENABLED else '关'} "
          f"防御={'开' if DEFEND else '关'}",
          flush=True)

    # 实时流：启动时清空旧数据，保证小地图只反映本次运行
    if EVENT_STREAM_PATH:
        try:
            open(EVENT_STREAM_PATH, "w", encoding="utf-8").close()
        except Exception:
            pass
    if STREAM_SHARD_DIR:
        import shutil
        try:
            shutil.rmtree(STREAM_SHARD_DIR, ignore_errors=True)
            os.makedirs(STREAM_SHARD_DIR, exist_ok=True)
        except Exception:
            pass

    save_counter = 0
    reconnect_delay = 3
    # 无 tick 看门狗：有时连接"假活"（WS 没报错但服务器不再推 tick，单位全静止），
    # SDK 内部重连不会触发（因为没抛异常），导致必须人工重启。看门狗在超过
    # STALE_TIMEOUT 秒没收到 tick 时，从另一线程关闭当前客户端 → 主循环 for 结束
    # → 外层 while 新建 ArenaHeroClient 重新连接，自动恢复对局。
    STALE_TIMEOUT = float(os.environ.get("AH_STALE_TIMEOUT", "150"))
    game_ref = {"cur": None}
    last_turn_ts = {"t": time.time()}
    stop_wd = threading.Event()

    def _watchdog():
        while not stop_wd.is_set():
            time.sleep(10)
            g = game_ref["cur"]
            if g is not None and (time.time() - last_turn_ts["t"]) > STALE_TIMEOUT:
                print(f"[看门狗] {STALE_TIMEOUT:.0f}s 无 tick，强制重连…", flush=True)
                try:
                    g.close()
                except Exception:
                    pass

    threading.Thread(target=_watchdog, daemon=True).start()

    # 断线自动重连：WebSocket 被服务器断开时，异常从 game.turns() 迭代器抛出，
    # 会绕过每 tick 的 try/except 直接杀进程。这里外层包一层，断线后保留状态重连，
    # 新对局由 match_checked 首 tick 自动清掉上局脏数据。
    while True:
        try:
            with ArenaHeroClient(api_key=api_key) as game:
                game_ref["cur"] = game
                last_turn_ts["t"] = time.time()   # 新连接：重置无 tick 计时
                state.match_checked = False   # 允许重连后首 tick 重新判定是否换局
                for turn in game.turns():
                    last_turn_ts["t"] = time.time()   # 收到 tick → 续命
                    # 实时调参：读滑块覆盖（每 tick 热更新全局常量）
                    try:
                        import live_params as _lp
                        _lp.apply_to_globals(globals(), "old")
                    except Exception:
                        pass

                    try:
                        plan_turn_v2(turn, state, game)
                    except Exception as exc:
                        print(f"[tick {turn.tick}] 决策异常：{exc!r}", flush=True)
                        try:
                            turn.clear()
                        except Exception:
                            pass
                    try:
                        turn.submit()
                    except Exception as exc:
                        print(f"[tick {turn.tick}] 提交异常：{exc!r}", flush=True)

                    stats.update(turn, state.combat)
                    save_counter += 1

                    if stats.ticks % STAT_EVERY == 0:
                        stats.report(turn, state.combat, state.worker_states)

                    # 定期持久化（每 60 Tick 或 STAT_EVERY 取大者）
                    if save_counter % max(STAT_EVERY * 6, 60) == 0:
                        Persistence.save(state.map_memory, state.enemy_memory,
                                         PERSISTENCE_FILE)
                        if EVENT_LOG_PATH:
                            state.event_log.save(EVENT_LOG_PATH)
                # game.turns() 正常结束（对局结束）→ 直接进入下一轮重连新对局
                print("=== 连接正常结束，重连下一局 ===", flush=True)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            print(f"[连接断开] {exc!r}，{reconnect_delay}s 后重连…", flush=True)
            try:
                Persistence.save(state.map_memory, state.enemy_memory,
                                 PERSISTENCE_FILE)
            except Exception:
                pass
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)   # 指数退避，封顶 30s
            continue


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n=== Agent 已停止 ===", flush=True)
