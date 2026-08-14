"""参数化启发式策略（v2，参考玩家社区防守文章思路）。

机制：
- 2 个 Ranger 在 Core 周围小半径环形巡逻（防偷家侦察）
- Core 遇险：储备资源立即生产单位，在外战斗单位回防
- 人口达到阈值且 Core 附近出现敌人：远处 Worker 自毁，转产 Ranger
- 远处敌人：除巡逻 Ranger 外全部攻击单位出击，调度 Worker 堵路
- 敌人消失：限定时间/范围追踪，超时标记遗忘（防反复横跳）
- Ranger 发现敌人后跟踪保持视线，掉血回 Core 治疗
- 区域记忆回访：记录各区域上次可见时间，定期回访（顺带找新资源）
- Core 资源满时 Worker 采完不回家，全体探路
- A* 确认不可达的目标点跳过（防卡死）

所有阈值均为基因（可被遗传算法进化），默认值取社区文章推荐。
"""

import math
from collections import deque

from ahsim.config import unit_cost, UNIT_STATS, VISION
from ahsim.vision import supercover_line

from .base import Strategy, Memory, Pathfinder, dir_name

# 同时外出探索的 Ranger 上限（2026-08-07）：idle 侦察把所有 Ranger 派到
# 54-70 格外 → Core 遇袭只剩 Vanguard 近战 + 巡逻 Ranger，回防来不及。
# 限最多 MAX_EXPLORERS 个 Ranger 探索，其余守家（idle 集结 Core 附近）。
MAX_EXPLORERS = 4

# 敌方 Core 位置遗忘倍率：敌方 Core 会迁移/死亡，位置记忆超过
# forget_ticks × CORE_FORGET_MULT 后不再追踪（普通敌人 = 1 倍）。
# 防止战斗单位永远钉在几小时前的旧位置（线上曾因此浪费 3/12 兵力）。
CORE_FORGET_MULT = 10

# Core 迁移门槛：资源记忆全空持续 MIGRATE_STARVE_TICKS tick 才考虑迁移
# （TTL 清空/侦察波动不算饥饿）；迁移冷却 MIGRATE_COOLDOWN tick
# （10 格迁移跨不出 chunk，连续迁移只会反复冻结经济）。
MIGRATE_STARVE_TICKS = 300
MIGRATE_COOLDOWN = 400

# Official reward redemption requires 150 stored Core resources.  Once the
# target is visible in an observation, preserve it until the reward is claimed.
RESOURCE_GOAL = 150

# Overnight vault mode keeps two units of population headroom above the exact
# redemption capacity.  At 32 population the Core holds 160 resources, so two
# casualties can reduce capacity to 150 without destroying the reward stock.
VAULT_POPULATION = 32
VAULT_WORKER_RADII = (6, 10)
VAULT_VANGUARD_RADII = (2, 4)
VAULT_RANGER_RADII = (3, 6)

# Keep one complete 2V1R squad permanently assigned to the Core.  These IDs
# stay stable while the units live; a casualty promotes the nearest surviving
# fighter instead of leaving home defense dependent on the current target.
HOME_VANGUARDS = 1
HOME_RANGERS = 1
HOME_VANGUARD_RADII = (2, 3)
HOME_RANGER_RADII = (3, 5)
BOOTSTRAP_WORKERS = 4

# A target must live long enough for a Unit to actually reach the outer search
# rings.  The previous 12-Tick window was shorter than a 20/30-cell trip and,
# combined with a candidate comparison bug, made Units reverse direction every
# few Ticks before revealing nearby resources.
WORKER_SCOUT_GOAL_TTL = 64
WORKER_RESOURCE_GOAL_TTL = 80
COMBAT_EXPLORE_GOAL_TTL = 80

# A visible attacker can disappear for a Tick as units move in and out of
# vision.  Keep the defensive posture long enough to avoid alternating between
# returning home and chasing the same force.
SIEGE_HOLD_TICKS = 30
SIEGE_WORKER_BLOCKERS = 3

# Workers only see three cells by themselves.  Without a short hold they flee
# once, lose sight of the attacker, and immediately walk back into range.
WORKER_DANGER_HOLD_TICKS = 8

# Population is storage, but Workers are not combat strength.  Above the early
# bootstrap phase keep at least three fighters per eight units; if the floor is
# missed, save for a fighter instead of buying another cheap Worker.
MIN_FIGHTER_SHARE = 3 / 8
MIN_FIGHTER_POPULATION = 10

# Once a badly outnumbered Core reaches this combined HP + shield, repairing a
# single point no longer changes the fight.  Start a free migration away from
# the nearest attacker while there is still time to move.
CORE_ESCAPE_DURABILITY = 4


# ----------------------------------------------------------------------
# 基因定义
# ----------------------------------------------------------------------
GENES = [
    # (name, default, lo, hi)
    ("worker_ratio",        0.55, 0.10, 0.90),  # 目标 Worker 占人口比例
    ("army_trigger",        0.70, 0.20, 3.00),  # 敌方/己方兵力比低于此才进攻（越高越敢打）
    ("flee_hp",             0.35, 0.00, 0.80),  # 单位低血（比例）回 Core
    ("heal_hp",             0.65, 0.20, 1.00),  # 单位血量低于此且在 Core 旁 → HEAL
    ("core_heal_hp",        0.40, 0.00, 1.00),  # Core HP 低于此 → HEAL
    ("shield_repair_hp",    0.35, 0.00, 1.00),  # Core 盾低于此且资源足 → REPAIR_SHIELD
    ("spawn_worker_until",  8.0,  2.0, 25.0),   # 资源低于此值时优先造 Worker
    ("vanguard_share",      2 / 3, 2 / 3, 2 / 3),  # v0.14 固定 2V1R；保留字段兼容旧结果
    ("rally_radius",        6.0,  2.0, 15.0),   # 集结半径（围绕 Core）
    ("beacon_go_range",     18.0, 5.0, 45.0),   # 距 Beacon 小于此就去抢
    ("max_population",      float(VAULT_POPULATION),
                            float(VAULT_POPULATION),
                            float(VAULT_POPULATION)),  # 32 人口容量 160：满仓守库
    #   可承受两次减员后仍保住 150；资源归零后自动恢复采集模式
    ("attack_core_first",   0.55, 0.00, 1.00),  # 进攻时优先 Core vs 单位
    ("kite_range",          2.0,  1.0, 3.0),    # Ranger 风筝距离
    ("worker_flee",         0.70, 0.00, 1.00),  # Worker 遇敌撤退倾向
    # ---- v2 新增 ----
    ("patrol_radius",       6.0,  3.0, 12.0),   # 巡逻 Ranger 的环形半径
    ("defense_radius",      10.0, 4.0, 20.0),   # Core 防御响应距离（敌人进入即回防）
    ("forget_ticks",        80.0, 20.0, 300.0), # 敌人消失多久后标记遗忘
    ("revisit_ticks",       150.0, 40.0, 400.0),# 区域回访周期
    ("selfdestruct_pop",    999.0, 999.0, 999.0),  # 自毁转 Ranger 阈值（禁用：
    #   v0.14 无维护费，自毁损采集产能 + 造 Ranger 花动态高价（30 人口 26 资
    #   源）与攒 150 兑换目标冲突；防御火力靠现有战斗单位）
    ("block_count",         0.0,  0.0, 6.0),    # 进攻时堵路的 Worker 数
    ("harass_workers",      0.0,  0.0, 1.0),    # 空闲 Worker 拦截敌方 Worker（压制经济）
    # ---- v3 借鉴 Drew-Z ----
    ("raid_stationary",     0.5,  0.0, 1.0),    # 突袭确认静止的敌方 Core（死玩家）倾向
    ("raid_min_obs",        3.0,  2.0, 8.0),    # 静止确认所需连续观察次数
    ("raid_max_dist",       40.0, 20.0, 60.0),  # 突袭最大距离（超此释放目标）
    # ---- v4 新增：idle 驻留（parking） ----
    ("park_radius",         3.0,  2.0, 8.0),   # idle 战斗单位驻留环带半径
    ("park_spread_w",       1.0,  0.0, 5.0),   # 驻留点偏离偏好半径的惩罚权重
    ("park_traffic_w",      2.0,  0.0, 10.0),  # Core 四邻（Worker 交通）惩罚权重
    # ---- v5 新增：idle 探索范围（网页实时可调，见 live_params.py）----
    ("idle_explore_radius", 15.0, 0.0,  60.0),  # 空闲战斗单位探索离 Core 上限（0=只巡逻不远征）
    ("scout_search_limit",  40.0, 10.0, 80.0),  # _nearest_unvisited 搜索半径（探索彻底度）
]


def make_default_genes():
    return {name: d for name, d, _lo, _hi in GENES}


def normalize_genes(genes=None):
    """Return a current-rules genome with unknown/legacy fields discarded.

    Result files outlive the rules they were trained against.  In particular,
    old v0.13 files contain a population cap of 19 and a live self-destruct
    threshold; applying those values to the v0.14 strategy silently changes the
    production policy.  The bounds here are the single source of truth used by
    deployment and by the strategy itself.
    """
    full = make_default_genes()
    if not genes:
        return full
    for name, _default, lo, hi in GENES:
        if name not in genes:
            continue
        try:
            value = float(genes[name])
        except (TypeError, ValueError):
            continue
        # Frozen genes are intentionally enforced, not merely clamped.  This
        # prevents stale v0.13 policy from re-enabling self-destruction.
        full[name] = lo if lo == hi else min(hi, max(lo, value))
    return full


class HeuristicStrategy(Strategy):
    name = "heuristic"

    def __init__(self, genes=None, bounds=None):
        self.genes = normalize_genes(genes)
        self.bounds = bounds
        self.reset()

    def reset(self):
        """完全重置（记忆 + 内部状态）。"""
        self.mem = Memory()
        self._reset_transient()

    def reset_transient(self):
        """仅清内部临时状态（目标/路径/回访），保留世界记忆。

        用于：Core 重生 / 断线重连。世界障碍永久不变，障碍/资源/区域
        记忆重生后依然有效；但指向旧位置的临时目标已失效，必须清空。
        """
        self._reset_transient()

    def _reset_transient(self):
        # Windowed reservation（window=1，2026-08-07）：A* 规划时避让
        # "其他单位下一步将占的格"（己方移动预演，官方引擎不保证成功，
        # 只做软避让减少无意义冲突尝试；目标格被 reserved 由 goal_ok
        # 豁免——排队语义保留）。window 1 tick 足够（冲突集中在门口）。
        self.pf = Pathfinder(self.bounds, self._is_obstacle,
                        goal_ok=lambda c: c in self._full_cells)
        self._beacon_task = None      # 被指派拾取 Beacon 的单位 uid
        self._worker_goal = {}        # uid -> (goal_pos, tick) 目标持久化
        self._goal_start = {}         # uid -> 开始走向目标的起始位置（hysteresis 进度计算）
        self._worker_dir = {}         # uid -> (direction, tick) 满载回家方向持久化
        self._worker_path = {}        # uid -> (path_list, tick) 完整路径持久化
        self._patrol_rangers = set()  # 本 Tick 巡逻 Ranger 的 uid
        self._home_vanguards = set()  # 跨 Tick 固定守家 2V
        self._home_rangers = set()    # 跨 Tick 固定守家 1R
        self._home_guards = set()
        self._block_workers = set()   # 本 Tick 负责堵路的 Worker uid
        self._attack_point = None     # 当前进攻目标点（敌人 core/单位位置）
        self._combat_clusters = []    # 本 Tick 全局进攻计划：敌方战斗单位簇
        self._combat_assigned = {}    # uid -> {ci, slot, enemy} 进攻分配
        self._engaged_clusters = set()  # 已聚齐、正式进攻的簇标识(敌方uid集合)
        # 友军受援：附近队友低血/被攻击时，健康单位优先掩护（不受威胁比例限制）
        self._support_targets = {}      # uid -> (ally_uid, enemy_pos) 掩护目标
        self._supporting = set()       # 本 Tick 被分配去支援的 uid 集合
        self._revisit_goal = None     # 当前回访目标
        self._revisit_since = 0
        self._last_prune = 0
        self._raid_point = None       # 突袭目标（确认静止的敌方 Core）
        self._raid_guards = set()     # 留守守卫的战斗单位 uid
        self._unit_hist = {}          # uid -> 最近 3 tick 位置（振荡检测）
        self._scout_stage = {}        # uid -> 侦察方向轮转 stage
        self._decisions = {}          # uid -> {tag, goal} 命中的策略分支
        self._scout_visited = {}      # 侦察点 -> 最近访问 tick
        self._scout_goal = {}         # uid -> (goal, tick) 侦察点粘性（防振荡）
        self._explore_goal = {}       # uid -> (goal, tick) 探索点粘性（防振荡）
        self._explore_path = {}       # uid -> [path] 探索完整路径（沿路径走防第一步摇摆）
        self._path_blocked = {}       # uid -> 连续被挡次数（blocked 先等 1 tick 再重算）
        self._starve_since = 0        # 资源记忆全空的起始 tick（迁移触发计时）
        self._last_migrate = 0        # 上次迁移起始 tick（迁移冷却）
        self._osc_tried = {}          # uid -> 已试的脱离方向（防反复撞同一方向）
        self._osc_wait = {}           # uid -> 冷却截止 tick（全方向被挡时等待）
        self._manual_goto = {}        # uid -> 人工指定目标（外部指令，最高优先）
        self._manual_done = []        # 本 tick 完成/取消的人工指令 uid（deploy 写回）
        self._spread_until = {}       # uid -> 散开冷却截止 tick（防 rally 拉回门口振荡）
        self._park_goal = {}          # uid -> (goal, tick) idle 驻留点粘性（防抖动）
        self._park_taken = set()      # 本 Tick 已认领的驻留点（同 tick 单位分散）
        self._spread_taken = set()    # 本 Tick 已认领的散开落点（防叠格单位同步）
        self._park_fail = {}          # uid -> (cell, tick) 不可达驻留点（20 tick 排除）
        self._pursuing = {}           # uid -> (mem_key, since) 追记忆目标状态（超时/到达 → 遗忘）
        self._combat_taken = {}       # 本 Tick 战斗目标认领（pos -> 认领数，防全员扑同一目标）
        self._resource_claims = {}    # 本 Tick 资源点 -> Worker uid（防近矿分支撞车）
        self._shot_claims = {}        # 本 Tick 射击格 -> 已分配伤害（防 Ranger 过量射击）
        self._shot_modes = {}         # uid -> shoot / shoot_lead / shoot_bracket
        self._fail_streak = {}        # uid -> 连续移动失败计数（>=3 触发退避）
        self._scout_stuck = {}        # uid -> 侦察位置停滞计数（>=10 换向）
        self._hunger_since = None     # 最近一次采集成功 tick（饥饿检测：长期无采集
                                      #  → 扩大侦察范围找新资源区）
        self._move_backoff = {}       # uid -> 冲突失败退避截止 tick（DEPENDENCY/
                                      #  CONTESTED 失败后短停，打破同步锁死循环）
        self._core_cell = None        # Core 格 A* 禁区（防路径穿过 Core 格）
        self._prev_core_cell = None   # 上一 tick 的 Core 格（位置变化 → bump A*）
        self._full_cells = set()      # 本 tick 我方单位占满的格（容量 2 已满，A* 绕行）
        self._core_pocket = set()     # Core 口袋：从 Core 四邻 BFS 的地形连通格（守家/驻留/巡逻用）
        self._core_pocket_dist = {}   # cell -> BFS 距离（门口/走廊判定，避免守卫堵交货通道）
        self._funnel_cells = set()    # 洞口必经格（口袋 d<=3 且四邻有 d>3 的格）
        self._funnel_shots = {}       # 口袋格 -> 能以射击规则打到的洞口必经格数
        self._funnel_exposed = {}     # 口袋格 -> 八邻中 d>3 的开放格数（暴露度）
        self._pf_ignore_full_pf = None   # 无视满格的 Pathfinder（满载回家兜底 / 守家可达性判定）
        self._vault_active = False    # 资源达到兑换线后的守库状态（资源下降即自动退出）
        self._vault_goal = {}         # uid -> 守库驻位（跨 tick 粘性，防队形抖动）
        self._patrol_goal = {}         # uid -> idle 巡逻当前航点（沿圈顺时针推进，代替待命）
        self._vault_taken = set()     # 本 Tick 已认领的守库驻位（每格仅驻一人，留通道）
        self._siege_active = False    # 近敌触发后持续一段时间，防视野闪烁导致追/撤抖动
        self._siege_until = 0
        self._defense_point = None    # 当前可见近敌，供 Worker 应急堵路/撤离定向
        self._defense_type = None     # 当前最近防御目标类型（Worker 不能挡远程火力）
        self._nearby_enemy_fighters = 0
        self._nearby_enemy_rangers = 0
        self._siege_vanguards = set() # 本 Tick 最多 2 个主动截击者，其余继续守环
        self._siege_rangers = set()   # 本 Tick 最多 3 个主动抢射击位
        self._worker_danger = {}      # uid -> (last dangerous fighter cells, expiry tick)
        self._planned_departures = {} # cell -> Units already ordered to leave this Tick
        self._planned_arrivals = {}   # cell -> Units already ordered to enter this Tick

    def load_memory(self, mem_dict):
        """崩溃恢复：注入持久化的视野记忆（障碍/资源/区域）。"""
        self.mem = Memory.from_dict(mem_dict)
        self.pf = Pathfinder(self.bounds, self._is_obstacle,
                        goal_ok=lambda c: c in self._full_cells)
        self._core_cell = None
        self._prev_core_cell = None
        self._full_cells = set()
        self._core_pocket = set()
        self._core_pocket_dist = {}
        self._funnel_cells = set()
        self._funnel_shots = {}
        self._funnel_exposed = {}
        self._pf_ignore_full_pf = None

    # ------------------------------------------------------------------
    def _is_obstacle(self, x, y):
        if (x, y) in self.mem.temp_blocked:
            return True
        # Core 格是 A* 禁区：任何路径都不得经过 Core 格（防止单位
        # "路过"踩上 Core 格后无人让出 → 满载 Worker 无法进格存放 →
        # 交货死锁；满载 Worker 进格/低血治疗走显式分支绕过 A*）
        if self._core_cell is not None and (x, y) == self._core_cell:
            return True
        # 满格（容量 2 已满）物理上进不去（官方规则：进入被占格要求
        # 占用者全部离开且最终容量允许）→ 路径中间格绕行；目标格满格
        # 由 _move_toward 等调用方单独处理（排队等待，不判不可达）
        if (x, y) in self._full_cells:
            return True
        if self.bounds is not None:
            x_lo, x_hi, y_lo, y_hi = self.bounds
            if not (x_lo <= x <= x_hi and y_lo <= y <= y_hi):
                return True
        return (x, y) in self.mem.obstacles

    def _is_terrain_obstacle(self, x, y):
        """仅地形障碍（不含满格/单位占用）——让出硬挤、环交换场景用。"""
        if (x, y) in self.mem.temp_blocked:
            return True
        if self.bounds is not None:
            x_lo, x_hi, y_lo, y_hi = self.bounds
            if not (x_lo <= x <= x_hi and y_lo <= y <= y_hi):
                return True
        return (x, y) in self.mem.obstacles

    def _is_persistent_obstacle(self, x, y):
        """仅**持久**地形障碍（不含临时避让 temp_blocked）。

        洞穴/口袋是地形几何，判定必须稳定：振荡检测把洞口必经格临时标记
        成障碍后，若口袋计算也把它当墙，整个漏斗会判空 → exit_cave 全被
        禁用 → 交货链断裂（线上 69119-69166 实测：exit_cave 每 6 tick 才
        出现 1 次，其余 tick 全员 wait，资源卡 36 达 30+ tick）。"""
        if self.bounds is not None:
            x_lo, x_hi, y_lo, y_hi = self.bounds
            if not (x_lo <= x <= x_hi and y_lo <= y <= y_hi):
                return True
        return (x, y) in self.mem.obstacles

    def _ignore_full_obstacle(self, x, y):
        """仅地形障碍 + Core 格（Core 格仍禁止穿越；但**不**把满格当障碍）。

        满载 Worker 回家兜底与守家驻位可达性判定用：单格走廊/口袋地形里
        必经格被占满时普通 A* 判"不可达"→ 满载 Worker 全体 WAIT（线上 14
        个满载全停、经济停摆）。引擎的 leaving/2 环交换会解开互锁，因此
        满格只是"暂时挤一下"，不是地形障碍。
        """
        if self._core_cell is not None and (x, y) == self._core_cell:
            return True
        return self._is_terrain_obstacle(x, y)

    def _pf_ignore_full(self):
        """懒创建无视满格的 Pathfinder（地形障碍不变时缓存路径有效）。"""
        if self._pf_ignore_full_pf is None:
            self._pf_ignore_full_pf = Pathfinder(
                self.bounds, self._ignore_full_obstacle, goal_ok=lambda c: True)
        return self._pf_ignore_full_pf

    def _compute_core_pocket(self, core_pos, radius=14):
        """Core 周围由地形连通的区域（BFS，半径限制）。

        只按记忆中的地形障碍约束；未知格当作可达（记忆可能不完整），
        满格/单位不视为障碍（口袋是地形概念）。返回 (格集合, cell->距离)。
        口袋不含 Core 格本身；Core 四面全被封死时返回空集合。
        """
        if core_pos is None:
            return set(), {}
        core_pos = tuple(core_pos)
        dist = {}
        dq = deque()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (core_pos[0] + dx, core_pos[1] + dy)
            if self._is_persistent_obstacle(*c):
                continue
            dist[c] = 1
            dq.append(c)
        while dq:
            cur = dq.popleft()
            if dist[cur] >= radius:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt == core_pos:   # 口袋不含 Core 格本身
                    continue
                if nxt in dist:
                    continue
                if self._is_persistent_obstacle(*nxt):
                    continue
                dist[nxt] = dist[cur] + 1
                dq.append(nxt)
        self._funnel_cells = set()
        self._funnel_shots = {}
        self._funnel_exposed = {}
        # 洞口判定：口袋内 BFS<=3 的格，若四邻存在 BFS>3 的可达格，说明它
        # 是"从洞外进入核心区"的必经格（Ranger 应优先覆盖它；洞内只有 8 格
        # 时该集合即两个入口格）。随后为每个口袋格计算射击覆盖（官方射击
        # 规则：八方向直线、射程 3、中间格阻挡）与暴露度（八邻开放格数）。
        for c, d in dist.items():
            if d > 3:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (c[0] + dx, c[1] + dy)
                if nxt == core_pos:
                    continue
                nd = dist.get(nxt)
                if nd is not None and nd > 3:
                    self._funnel_cells.add(c)
                    break
        # 真正的"洞穴"才启用出口撤离：开阔地形下 BFS<=3 的整圈格（约 24
        # 个）都满足"四邻有 BFS>3 格"，会把普通地图上的空载 Worker 全
        # 部误判成"洞内"往外赶（线上 69015 前哨；scout/采资源逻辑被
        # exit_cave 截断）。洞穴只有 1~3 个入口必经格，阈值取 6 足够。
        if len(self._funnel_cells) > 6:
            self._funnel_cells = set()
        if self._funnel_cells:
            for c in dist:
                if self._is_terrain_obstacle(*c):
                    continue
                shots = sum(1 for f in self._funnel_cells
                            if self._ranger_can_shoot(c, f))
                self._funnel_shots[c] = shots
                exposed = sum(
                    1 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                    if (dx or dy) and dist.get((c[0] + dx, c[1] + dy), 99) > 3)
                self._funnel_exposed[c] = exposed
        return set(dist), dist

    def _ranger_can_shoot(self, src, dst):
        """官方射击规则：八方向直线、射程 1..3、仅中间格阻挡。

        斜线旁的障碍不阻挡（斜缝穿射）；水平/垂直线中间有墙则全挡。
        与 ahsim.vision.is_shot_line / shot_intermediate_cells 一致。
        """
        dx = abs(src[0] - dst[0])
        dy = abs(src[1] - dst[1])
        if dx == 0 and dy == 0:
            return False
        if dx != 0 and dy != 0 and dx != dy:
            return False
        if not (1 <= max(dx, dy) <= 3):
            return False
        sx = 1 if dst[0] > src[0] else -1 if dst[0] < src[0] else 0
        sy = 1 if dst[1] > src[1] else -1 if dst[1] < src[1] else 0
        for t in range(1, max(dx, dy)):
            if self._is_terrain_obstacle(src[0] + sx * t, src[1] + sy * t):
                return False
        return True

    def _nearest_pocket_point(self, target):
        """在 Core 口袋内找距 target 最近的格；优先避开洞口走廊（BFS<=2）。"""
        if not self._core_pocket:
            return None
        best, best_d = None, 1 << 30
        fallback, fallback_d = None, 1 << 30
        for cand in self._core_pocket:
            d = abs(cand[0] - target[0]) + abs(cand[1] - target[1])
            if self._core_pocket_dist.get(cand, 99) <= 2:
                if d < fallback_d:
                    fallback, fallback_d = cand, d
                continue
            if d < best_d:
                best, best_d = cand, d
        return best if best is not None else fallback

    # ------------------------------------------------------------------
    def decide(self, obs):
        g = self.genes
        # 清理过期的临时障碍（撞墙避让过期）
        for c in list(self.mem.temp_blocked):
            if self.mem.temp_blocked[c][0] < obs.tick:
                del self.mem.temp_blocked[c]
                # 临时障碍移除改变可达性 → 使 A* 缓存失效
                self.mem.new_obstacles = True
        # 长跑内存淘汰（每 200 tick 一次，避免每 tick 全量扫描）
        if getattr(self, "_last_prune", 0) == 0 or obs.tick - self._last_prune >= 200:
            core_pos = tuple(obs.core["pos"]) if obs.core else None
            self.mem.prune(core_pos=core_pos)
            self._last_prune = obs.tick
        self.mem.update(obs)
        if self.pf is None:
            self.pf = Pathfinder(self.bounds, self._is_obstacle,
                        goal_ok=lambda c: c in self._full_cells)
        if self.mem.new_obstacles:
            self.pf.bump_version()
            if self._pf_ignore_full_pf is not None:
                self._pf_ignore_full_pf.bump_version()
            self.mem.new_obstacles = False
        self._taken_resources = set()
        self._resource_claims = {}
        self._shot_claims = {}
        self._shot_modes = {}
        self._planned_departures = {}
        self._planned_arrivals = {}
        self._intended_dest = {}       # uid -> 本 tick 计划目的地（合体同步移动用）
        # 资源任务必须足够长，才能覆盖策略允许的 60 格采集半径。仅在 Worker
        # 已载货、目标已被视野反证或超过任务 TTL 时释放，避免每 12 Tick
        # 重分配一次导致远途 Worker 改道和 A* 首步振荡。
        workers_by_id = {u["uid"]: u for u in obs.units
                         if u["utype"] == "WORKER"}
        for uid in list(self._worker_danger):
            if uid not in workers_by_id or self._worker_danger[uid][1] < obs.tick:
                del self._worker_danger[uid]
        for uid in list(self._worker_goal):
            goal, assigned_tick = self._worker_goal[uid]
            worker = workers_by_id.get(uid)
            if worker is None or worker["cargo"] > 0 \
                    or goal not in self.mem.resources \
                    or obs.tick - assigned_tick >= WORKER_RESOURCE_GOAL_TTL:
                del self._worker_goal[uid]
                self._goal_start.pop(uid, None)
        self._assignments = {}       # uid -> 本 Tick 集中分配的资源目标
        # 侦察点占用集合：各 Worker 当前粘性目标 + 本 Tick 已认领（防多 Worker 同点）
        self._scout_taken = {g[0] for g in self._scout_goal.values()
                             if g and g[0] and g[1]}
        self._explore_taken = {g[0] for g in self._explore_goal.values()
                               if g and g[0]}   # 跨 tick 探索目标认领（防多 Ranger 挤同一目标）
        self._park_taken = set()      # 本 Tick 已认领的 idle 驻留点
        self._spread_taken = set()    # 本 Tick 已认领的 idle 散开落点
        self._combat_taken = {}       # 本 Tick 战斗目标认领（pos -> 认领数）
        # 追击防横跳：追记忆目标超过 25 tick 未确认 → 遗忘（敌人已走远/
        # 已死亡，继续追旧位置 = 空转，且反复横跳）
        for uu in list(self._pursuing):
            key, since = self._pursuing[uu]
            if key not in self.mem.last_enemy_pos:
                del self._pursuing[uu]
            elif obs.tick - since > 25:
                self._forget_enemy(key)
                del self._pursuing[uu]
        # 饥饿检测：长期无采集成功 → 侦察扩大（出生区资源枯竭后靠远侦察
        # 发现新资源区；短局资源充足时不触发，保持近环高效侦察）
        if obs.prev_events:
            for ev in obs.prev_events:
                if ev.get("type") in ("HARVESTED", "HARVEST_SUCCEEDED"):
                    self._hunger_since = obs.tick
        # 冲突失败退避：**连续 3 次**移动失败才短停 2 tick——单位间同步互堵
        # （A 等 B、B 等 A）时每 tick 重试同一路径会永续失败（长局实测 76 万
        # 次失败 / 99% DEPENDENCY）；短停打破同步。单次/两次失败是正常排队
        # （链式下 tick 自然解开），退避会浪费吞吐（短局实测 -8~-30）。
        # MOVED 成功即清零连续计数。
        if obs.prev_events:
            moved = {ev.get("obj_id") for ev in obs.prev_events
                     if ev.get("type") == "MOVED"}
            for uid in moved:
                self._move_backoff.pop(uid, None)
                self._fail_streak[uid] = 0
            for ev in obs.prev_events:
                if ev.get("type") != "MOVE_BLOCKED":
                    continue
                n = self._fail_streak.get(ev["obj_id"], 0) + 1
                self._fail_streak[ev["obj_id"]] = n
                if n >= 3:
                    self._move_backoff[ev["obj_id"]] = obs.tick + 2
                    pass   # 侦察换向改用"位置停滞检测"（见 _decide_unit）
        self._revisit_assigned = False  # 本 Tick 回访点是否已被 Worker 认领

        core_pos = tuple(obs.core["pos"]) if obs.core else None
        self._sync_home_guards(obs, core_pos)
        vault_now = bool(obs.core and core_pos and
                         obs.core["resources"] >= RESOURCE_GOAL)
        if vault_now != self._vault_active:
            # 进出守库态都清除旧经济/远征任务。兑换后不能让单位继续执行
            # 150 资源时留下的回撤目标；进入时也不能沿旧目标继续跑远。
            self._worker_goal.clear()
            self._goal_start.clear()
            self._worker_path.clear()
            self._scout_goal.clear()
            self._explore_goal.clear()
            self._explore_path.clear()
            self._park_goal.clear()
            self._pursuing.clear()
            self._revisit_goal = None
            self._vault_goal.clear()
        self._vault_active = vault_now
        self._vault_taken = set()
        # Core 格 A* 禁区随位置更新；位置变化（迁移/重生）→ 使缓存失效
        self._core_cell = core_pos
        if self._core_cell != self._prev_core_cell:
            self._prev_core_cell = self._core_cell
            if self.pf is not None:
                self.pf.bump_version()
            if self._pf_ignore_full_pf is not None:
                self._pf_ignore_full_pf.bump_version()
        # 满格统计：容量 2 已满的格（A* 中间格绕行；占位变化时缓存路径
        # 撞上会由 MOVE_BLOCKED 反馈标记 temp_blocked 兜底，不每 tick bump）
        cells = {}
        for uu in obs.units:
            p = tuple(uu["pos"])
            cells[p] = cells.get(p, 0) + 1
        self._full_cells = {c for c, n in cells.items() if n >= 2}
        # 战斗组配对（VANGUARD + RANGER 同格）=「合体」：用于合体后同步移动，
        # 避免"先锋先动一步、游侠滞后一步"的串行现象（见 _apply_group_sync）。
        self._combat_pairs = {}   # cell -> (vanguard_uid, ranger_uid)
        self._pair_of = {}        # uid -> 共享 cell
        _occ = {}                 # cell -> (utype, uid)，同格异种即成一对
        for uu in obs.units:
            if uu["utype"] not in ("VANGUARD", "RANGER"):
                continue
            p = tuple(uu["pos"])
            prev = _occ.get(p)
            if prev is not None and prev[0] != uu["utype"]:
                v_uid = uu["uid"] if uu["utype"] == "VANGUARD" else prev[1]
                r_uid = prev[1] if uu["utype"] == "VANGUARD" else uu["uid"]
                self._combat_pairs[p] = (v_uid, r_uid)
                self._pair_of[v_uid] = p
                self._pair_of[r_uid] = p
            else:
                _occ[p] = (uu["utype"], uu["uid"])
        # Core 口袋：曼哈顿环跨墙选点会把守家单位丢到墙外/不可达格
        # （线上见过 Vanguard 站在 BFS 不可达格）。BFS 只按记忆地形障碍
        # 约束、无视满格（满格是单位占用，不是地形），供守家/驻留/巡逻
        # 目标选择与"洞口/走廊"判定。
        self._core_pocket, self._core_pocket_dist =             self._compute_core_pocket(core_pos)
        # 资源饥饿计时：记忆全空持续 tick 数（迁移条件之一；TTL 波动不触发）
        if core_pos:
            if len(self.mem.resources) == 0:
                if self._starve_since == 0:
                    self._starve_since = obs.tick
            else:
                self._starve_since = 0
        else:
            self._starve_since = 0
        bx, by = obs.beacon["position"]
        bpos = (bx, by)

        # ---- 全局评估 ----
        visible_enemies = [(tuple(e["pos"]), e["hp"], e["utype"], e["uid"])
                           for e in obs.enemies]
        visible_cores = [(tuple(ec["pos"]), ec["hp"] + ec["shield"], ec["owner"])
                         for ec in obs.enemy_cores]
        all_threats = [t[0] for t in visible_enemies] + [t[0] for t in visible_cores]

        threat = self._threat_ratio(obs, core_pos, visible_enemies,
                                    visible_cores)

        # 敌人追踪：可见敌人刷新记忆（遗忘只按超时触发，见 _nearest_*）
        if core_pos:
            d_enemy = self._nearest_enemy_pos(obs, core_pos)
        else:
            d_enemy = None

        # 防御状态：**可见敌方战斗单位**接近 Core 才触发（敌方 worker 无
        # 攻击力，只是来采资源/路过——不触发造兵防御，否则 3 个敌方 worker
        # 路过会把 Core 资源全拿去造 Ranger，经济停摆）。敌方 Core 也算
        # 威胁（会生产兵力）。记忆敌人不触发（类型未知 + 可能已过时）。
        danger_targets = []
        if core_pos:
            for e in obs.enemies:
                if e["utype"] == "WORKER":
                    continue
                if self._dist(e["pos"], core_pos) <= g["defense_radius"]:
                    danger_targets.append((tuple(e["pos"]), e["utype"]))
            for c in obs.enemy_cores:
                if self._dist(c["pos"], core_pos) <= g["defense_radius"]:
                    danger_targets.append((tuple(c["pos"]), "CORE"))
        danger_points = [target[0] for target in danger_targets]
        core_was_hit = any(ev.get("type") == "CORE_DAMAGED"
                           for ev in (obs.prev_events or ()))
        was_siege = self._siege_active
        if danger_points or core_was_hit:
            self._siege_until = max(self._siege_until,
                                    obs.tick + SIEGE_HOLD_TICKS)
        self._siege_active = bool(core_pos and obs.tick < self._siege_until)
        defense = self._siege_active
        self._nearby_enemy_fighters = len(danger_points)
        nearest_defense = (min(
            danger_targets, key=lambda target: self._dist(target[0], core_pos))
            if danger_targets and core_pos else None)
        self._defense_point = nearest_defense[0] if nearest_defense else None
        self._defense_type = nearest_defense[1] if nearest_defense else None
        self._nearby_enemy_rangers = sum(
            1 for _pos, utype in danger_targets if utype == "RANGER")
        self._siege_vanguards = set()
        self._siege_rangers = set()
        if self._defense_point is not None \
                and self._defense_type in ("VANGUARD", "RANGER"):
            vanguards = sorted(
                (u for u in obs.units if u["utype"] == "VANGUARD"),
                key=lambda u: (u["hp"] < UNIT_STATS["VANGUARD"]["hp"],
                               self._dist(u["pos"], self._defense_point),
                               self._uid_num(u["uid"])))
            rangers = sorted(
                (u for u in obs.units if u["utype"] == "RANGER"),
                key=lambda u: (u["hp"] < UNIT_STATS["RANGER"]["hp"],
                               self._dist(u["pos"], self._defense_point),
                               self._uid_num(u["uid"])))
            self._siege_vanguards = {u["uid"] for u in vanguards[:2]}
            self._siege_rangers = {u["uid"] for u in rangers[:3]}
        if defense and not was_siege:
            # Entering a siege invalidates every outward combat/exploration
            # task.  Otherwise a one-Tick visibility gap can revive a stale
            # pursuit before the unit has actually returned home.
            self._pursuing.clear()
            self._explore_goal.clear()
            self._explore_path.clear()
            self._raid_point = None
            self._revisit_goal = None

        # 进攻目标：最近的未遗忘敌人（core 优先）
        self._attack_point = None if vault_now or defense \
            else self._pick_attack_point(obs, core_pos, visible_enemies,
                                         visible_cores)
        # 全局进攻计划（按敌方人数2倍精确调配 + 包围式进攻）：
        # 仅在进攻模式(非躲藏/非防御)下构建；防御/劣势时清空并退回守家。
        if not vault_now and not defense:
            self._plan_combat(obs, core_pos, threat)

        # 友军受援：检测低血/被攻击的队友，指派附近健康单位去掩护
        # （无论威胁比例高低都生效——劣势时更需要支援）
        self._plan_support(obs, core_pos)

        # 巡逻 Ranger 分配：永久守家 Ranger 优先，再补到两个近家巡逻位。
        rangers = sorted([u["uid"] for u in obs.units if u["utype"] == "RANGER"])
        patrol = sorted(self._home_rangers)
        patrol.extend(uid for uid in rangers if uid not in self._home_rangers)
        self._patrol_rangers = set(patrol[:2])

        # 堵路 Worker 分配：仅当敌人靠近我方 Core（防御半径 2 倍内）才值得堵
        self._block_workers = set()
        block_target = self._defense_point if defense else self._attack_point
        # A Worker screen only delays melee Vanguard movement.  It does not
        # block Ranger line of fire, so assigning one while any nearby Ranger
        # is visible turns the Worker into a stationary 2-HP target.
        defense_screen_ok = (not defense or (
            self._defense_type == "VANGUARD"
            and self._nearby_enemy_rangers == 0))
        if block_target and core_pos and defense_screen_ok:
            d_ap = self._dist(block_target, core_pos)
            if d_ap <= g["defense_radius"] * 2:
                workers = sorted(
                    [uu for uu in obs.units if uu["utype"] == "WORKER"
                     and uu["cargo"] == 0
                     and uu["hp"] == UNIT_STATS["WORKER"]["hp"]],
                    key=lambda ww: self._dist(ww["pos"], block_target))
                if defense:
                    count = max(SIEGE_WORKER_BLOCKERS,
                                math.ceil(g["block_count"]))
                    # Keep one empty Worker available for economy/recovery.
                    count = min(count, max(0, len(workers) - 1))
                else:
                    count = math.ceil(g["block_count"])
                self._block_workers = {w["uid"] for w in workers[:count]}

        # 回访目标（周期性）
        if not vault_now and not defense:
            self._maybe_pick_revisit(obs, core_pos)

        # 突袭目标：确认静止的敌方 Core（死玩家）——重复观察 + 距离限制 + 留守卫
        self._raid_point = None
        self._raid_guards = set()
        if not vault_now and not defense \
                and g["raid_stationary"] > 0.5 and core_pos:
            raid = self._pick_raid_target(obs, core_pos)
            fighters = [u["uid"] for u in obs.units
                        if u["utype"] in ("VANGUARD", "RANGER")]
            if raid and len(fighters) >= 3:
                self._raid_point = raid
                self._raid_guards = set(self._home_guards)

        # Beacon 任务分配：坐标永远公开（status UNKNOWN 也可能在地面，派人去查看）
        self._beacon_task = None
        if not vault_now and not defense \
                and obs.beacon.get("status") in ("GROUND", "UNKNOWN"):
            best, bd = None, None
            for u in obs.units:
                if u["uid"] in self._home_guards:
                    continue
                d = self._dist(u["pos"], bpos)
                if d <= g["beacon_go_range"]:
                    if bd is None or d < bd:
                        bd, best = d, u["uid"]
            self._beacon_task = best

        # 资源目标集中分配：空闲 Worker 与候选资源点一对一（防多个 Worker
        # 在不同 tick 各自认领同一目标 → 全员挤向一个点。跨 tick 去重靠
        # _worker_goal 粘性 + 本分配排除已粘性占用的点）。
        # 分配顺序按"到最近候选点的距离"：离资源近的 Worker 优先拿近点
        # （否则按 uid 排序会把点分给远处的 Worker，近的反而摸鱼）。
        self._assignments = {}
        core_full = (obs.core is not None and core_pos is not None and
                     obs.core["resources"] >= obs.core["capacity"])
        if core_pos and not core_full and not vault_now:
            goal_points = {g[0] for g in self._worker_goal.values()}
            idle = [u for u in obs.units
                    if u["utype"] == "WORKER" and u["cargo"] == 0
                    and u["uid"] not in self._worker_goal
                    and u["uid"] not in self._block_workers]
            if idle:
                cands = self._resource_candidates(obs, core_pos)
                taken = set()
                # [对照] 旧算法：近 Core 矿优先（cands 顺序）+ 近候选 worker 先挑
                # 分配顺序：rollout 预测收获（未来 100 tick 采集循环，
                # 含补给节奏与共享竞争）——替代纯距离排序（"最近"≠"实际
                # 采得多"：近但被多人共享的点不如远但独占的点）
                def _score(w, c):
                    shared = sum(1 for ww in idle
                                 if ww["uid"] != w["uid"]
                                 and tuple(ww["pos"]) != tuple(w["pos"])
                                 and self._dist(ww["pos"], c)
                                 <= self._dist(w["pos"], c))
                    s = self._rollout_harvest(core_pos, tuple(w["pos"]),
                                              c, shared=shared)
                    # Opponent-aware 风险（实验 B，2026-08-07）：候选点
                    # 附近敌 Core/敌单位 → 分数扣减（会被抢/驻军防御）——
                    # 软性梯度（非硬排除），记忆越新风险越高
                    for _k, (_p, _tk) in self.mem.last_enemy_pos.items():
                        if _k in self.mem.enemy_forgotten:
                            continue
                        _d = self._dist(_p, c)
                        if str(_k).startswith("core_"):
                            if _d < 15:
                                s -= (15 - _d) * 0.15
                        else:
                            _age = obs.tick - _tk
                            if _age > 50:
                                continue   # 过时记忆无风险
                            if _d < 8:
                                s -= (8 - _d) * 0.1 * (1 - _age / 60)
                    # sticky_bonus（敌方 CoreFarmer 同款，2026-08-07）：
                    # 当前目标加分——分配器不因微小分数差换目标（决策层
                    # 稳定性；hysteresis 在 _decide_worker 的切换判定处
                    # 已有，这里是分配层的粘性兜底）
                    if self._worker_goal.get(w["uid"], (None, -99))[0] == c:
                        s += 0.5
                    return s
                scored = []
                for w in idle:
                    best_c, best_s = None, -1.0
                    for c in cands:
                        if c in taken or c in goal_points:
                            continue
                        if self.pf.find(w["pos"], c) is None:
                            continue
                        s = _score(w, c)
                        if s > best_s:
                            best_c, best_s = c, s
                    if best_c is not None:
                        scored.append((best_s, w, best_c))
                # 按预测收获降序分配（先满足高收益的 worker）
                for _s, w, c in sorted(scored, key=lambda x: -x[0]):
                    if c in taken:
                        continue
                    taken.add(c)
                    self._assignments[w["uid"]] = c

        plan = {"core": None, "units": {}}
        if obs.core:
            plan["core"] = self._decide_core(obs, core_pos, threat, d_enemy,
                                             defense, bpos)
        for u in sorted(obs.units, key=lambda u: u["uid"]):
            act = self._decide_unit(u, obs, core_pos, threat, d_enemy,
                                    all_threats, visible_enemies, visible_cores,
                                    bpos, defense)
            if act is not None:
                # 合体同步：先锋/游侠同格且本 tick 要去同一格(互堵)时，
                # 把后处理的一方改到伙伴目的地旁的空位，二者同 tick 各动一步。
                act = self._apply_group_sync(u, tuple(u["pos"]), act, obs)
                plan["units"][u["uid"]] = act
                if act[0] == "MOVE":
                    dx, dy = {
                        "UP": (0, -1), "DOWN": (0, 1),
                        "LEFT": (-1, 0), "RIGHT": (1, 0),
                    }[act[1]["direction"]]
                    origin = tuple(u["pos"])
                    destination = (origin[0] + dx, origin[1] + dy)
                    self._intended_dest[u["uid"]] = destination
                    self._planned_departures[origin] = (
                        self._planned_departures.get(origin, 0) + 1)
                    self._planned_arrivals[destination] = (
                        self._planned_arrivals.get(destination, 0) + 1)
        return plan

    # ------------------------------------------------------------------
    # Core 动作
    # ------------------------------------------------------------------
    def _sync_home_guards(self, obs, core_pos):
        """Persistently reserve the nearest available 2V1R home squad."""
        by_type = {
            utype: {u["uid"]: u for u in obs.units if u["utype"] == utype}
            for utype in ("VANGUARD", "RANGER")
        }

        def sync(current, utype, target):
            live = by_type[utype]
            kept = {uid for uid in current if uid in live}
            candidates = sorted(
                (u for uid, u in live.items() if uid not in kept),
                key=lambda u: (
                    self._dist(u["pos"], core_pos) if core_pos else 0,
                    self._uid_num(u["uid"]),
                ),
            )
            kept.update(u["uid"] for u in candidates[:max(0, target - len(kept))])
            return kept

        self._home_vanguards = sync(
            self._home_vanguards, "VANGUARD", HOME_VANGUARDS)
        self._home_rangers = sync(
            self._home_rangers, "RANGER", HOME_RANGERS)
        self._home_guards = self._home_vanguards | self._home_rangers

    def _missing_home_guard_type(self, obs):
        """Return the role needed to rebuild the minimum home squad."""
        v_count = sum(1 for u in obs.units if u["utype"] == "VANGUARD")
        r_count = sum(1 for u in obs.units if u["utype"] == "RANGER")
        if v_count < HOME_VANGUARDS:
            return "VANGUARD"
        if r_count < HOME_RANGERS:
            return "RANGER"
        return None

    def _threat_ratio(self, obs, core_pos, enemies, enemy_cores):
        """Return mobile enemy strength / available mobile defense strength.

        Core durability is deliberately excluded from our strength: it absorbs
        damage but cannot remove an attacker.  Nearby enemies receive a modest
        multiplier because distant fighters and a unit already at the Core do
        not have equal defensive value.
        """
        def value(utype, hp):
            # Vanguard can damage several adjacent targets with SWEEP.
            return max(0.0, float(hp)) * (1.25 if utype == "VANGUARD" else 1.0)

        ours = 0.0
        for unit in obs.units:
            if unit["utype"] == "WORKER":
                continue
            weight = 1.0
            if core_pos and self._dist(unit["pos"], core_pos) \
                    > self.genes["defense_radius"]:
                weight = 0.5
            ours += value(unit["utype"], unit["hp"]) * weight

        theirs = 0.0
        for pos, hp, utype, _uid in enemies:
            if utype == "WORKER":
                continue
            weight = 1.25 if core_pos and self._dist(pos, core_pos) \
                <= self.genes["defense_radius"] else 1.0
            theirs += value(utype, hp) * weight
        # A Core does not attack, but a nearby one can immediately spawn units.
        for pos, durability, _owner in enemy_cores:
            weight = 0.75 if core_pos and self._dist(pos, core_pos) \
                <= self.genes["defense_radius"] else 0.5
            theirs += max(0.0, float(durability)) * weight
        return theirs / max(1.0, ours)

    def _fighter_spawn_order(self, obs):
        """Produce Vanguard/Ranger in 1:1 alternating order (user-tuned)."""
        v_count = sum(1 for u in obs.units if u["utype"] == "VANGUARD")
        r_count = sum(1 for u in obs.units if u["utype"] == "RANGER")
        # 哪边少就先产哪边，长期维持 V:R = 1:1
        if v_count <= r_count:
            return ("VANGUARD", "RANGER")
        return ("RANGER", "VANGUARD")

    def _emergency_core_move(self, obs, core_pos, defense):
        """Start a free migration when the remaining defenders cannot hold."""
        if not defense or core_pos is None or self._defense_point is None:
            return None
        durability = obs.core["hp"] + obs.core["shield"]
        fighters = sum(1 for u in obs.units if u["utype"] != "WORKER")
        if durability > CORE_ESCAPE_DURABILITY \
                or self._nearby_enemy_fighters <= fighters:
            return None

        toward = self._dir_toward(core_pos, self._defense_point)
        away = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT",
                "RIGHT": "LEFT"}[toward]
        blocked = {d for d, t in self.mem.core_blocked.items()
                   if obs.tick - t < 20}
        directions = (away, "UP", "DOWN", "LEFT", "RIGHT")
        for direction in directions:
            if direction in blocked:
                continue
            dx, dy = {"UP": (0, -1), "DOWN": (0, 1),
                      "LEFT": (-1, 0), "RIGHT": (1, 0)}[direction]
            if not self._is_terrain_obstacle(core_pos[0] + dx,
                                             core_pos[1] + dy):
                self._last_migrate = obs.tick
                return ("START_MOVE", {"direction": direction})
        return None

    def _decide_core(self, obs, core_pos, threat, d_enemy, defense, bpos):
        g = self.genes
        core = obs.core
        if core["migration"] is not None:
            return None
        emergency_move = self._emergency_core_move(obs, core_pos, defense)
        if emergency_move is not None:
            return emergency_move
        # Reward mode: healing, shield repair and spawning all spend Core
        # resources.  Freeze those actions once the redemption target is met.
        if core["resources"] >= RESOURCE_GOAL:
            return None
        max_hp = 5
        cap = 10 if obs.beacon.get("status") == "CARRIED" and \
            self._beacon_carried_by_me(obs) else 5
        # 1) 治疗
        if core["hp"] < g["core_heal_hp"] * max_hp \
                and core["resources"] > 0:
            return ("HEAL", {})
        # 2) 修盾
        if core["shield"] < g["shield_repair_hp"] * cap \
                and core["resources"] > 0:
            return ("REPAIR_SHIELD", {})
        # 3) 防御：只买得起战斗单位时才生产。买不起就保留资源，
        #    不再回退到便宜 Worker 继续稀释守军比例。
        if defense:
            for utype in self._fighter_spawn_order(obs):
                price = unit_cost(UNIT_STATS[utype]["cost"], obs.population)
                if core["resources"] >= price:
                    return ("SPAWN", {"unit_type": utype})
            return None
        # 4) 正常生产。达到人口上限后自然停止，随后 Core 才开始积累库存。
        if core["resources"] >= 5 and obs.population < g["max_population"]:
            workers = sum(1 for u in obs.units if u["utype"] == "WORKER")
            ratio = workers / max(1, obs.population)
            fighters = obs.population - workers
            fighter_floor = (math.ceil(obs.population * MIN_FIGHTER_SHARE)
                             if obs.population >= MIN_FIGHTER_POPULATION else 0)
            fighter_deficit = fighters < fighter_floor
            missing_home_guard = (self._missing_home_guard_type(obs)
                                  if obs.population >= 4 else None)
            # Choose the composition target first, then price that exact unit.
            # Comparing against the Ranger price used to block cheap Worker or
            # Vanguard spawns whenever the Core had 5-11 resources.
            desired = "RANGER"
            # 30→32 是纯容量保险：两名便宜 Worker 比高价战斗单位更快把
            # 容量从150推到160，也补充最容易减员的经济人口。
            if obs.population >= 30:
                desired = "WORKER"
            elif workers < BOOTSTRAP_WORKERS:
                desired = "WORKER"
            elif missing_home_guard is not None:
                desired = missing_home_guard
            elif fighter_deficit:
                desired = self._fighter_spawn_order(obs)[0]
            elif ratio < g["worker_ratio"]:
                desired = "WORKER"
            else:
                desired = self._fighter_spawn_order(obs)[0]
            # Combat deficits must accumulate enough resources for a fighter.
            # Falling back to Worker here caused agent2 to reach 18W/4 fighters
            # and also raised the dynamic price of every future defender.
            # Outside a live siege, save for the exact missing squad role.
            # Spawning the cheaper alternate role recreates the same composition
            # drift that left agent2 with plenty of population but too little
            # coordinated combat power.
            candidates = [desired]
            for utype in candidates:
                price = unit_cost(UNIT_STATS[utype]["cost"], obs.population)
                if core["resources"] >= price:
                    return ("SPAWN", {"unit_type": utype})
        # 5) 迁移：**不主动迁向中央 [0,0]**。100+ 玩家的高竞争环境下，
        #    [0,0] 是 Beacon/资源争夺的死亡区，弱 Core 迁过去会被围攻。
        #    就地发展：资源回补（每 4 tick）保证本地持续供给。
        #    仅当"资源记忆全空持续很久（TTL 波动不算）"且经济盈余时才迁移；
        #    迁移有冷却（10 格迁移跨不出 chunk，连续迁移只会反复冻结经济）。
        starving = obs.tick - self._starve_since if self._starve_since else 0
        # 资源为 0 也必须能迁移：agent2 曾在出生区资源枯竭后无法生产，
        # 若迁移再要求库存就会永久死锁。迁移本身不消耗资源，cooldown 防抖。
        if core_pos and len(self.mem.resources) == 0 \
                and len(self.mem.area_seen) > 30 \
                and starving > MIGRATE_STARVE_TICKS \
                and obs.tick - self._last_migrate > MIGRATE_COOLDOWN:
            # 向最近未访问方向（随机一个远离中央的象限）小步迁移
            away = self._dir_toward(core_pos, (0, 0))
            away = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT",
                    "RIGHT": "LEFT"}[away]
            blocked = {d for d, t in self.mem.core_blocked.items()
                       if obs.tick - t < 20}
            for d in (away, "UP", "DOWN", "LEFT", "RIGHT"):
                if d not in blocked:
                    self._last_migrate = obs.tick
                    return ("START_MOVE", {"direction": d})
            return None  # 全方向近期被挡：放弃本次迁移（等 blocked 过期）
        return None

    # ------------------------------------------------------------------
    # 单位分发
    # ------------------------------------------------------------------
    def _decide_unit(self, u, obs, core_pos, threat, d_enemy, all_threats,
                     enemies, enemy_cores, bpos, defense):
        g = self.genes
        utype = u["utype"]
        pos = u["pos"]
        max_hp = 2 if utype == "WORKER" else 4 if utype == "VANGUARD" else 2
        self._decisions[u["uid"]] = {"tag": "wait", "goal": None}  # 默认：无动作

        # Survival overrides manual movement, conflict backoff and oscillation
        # recovery.  Online replays showed HP1 units taking an osc_break step
        # away from Core, or waiting because an earlier move had failed.
        retreating, retreat_action = self._low_hp_retreat(
            u, obs, core_pos, max_hp)
        if retreating:
            return retreat_action

        # 人工指令：强制移动（外部指定目标，最高优先级，先于振荡防护）
        mg = self._manual_goto.get(u["uid"])
        if mg is not None:
            if self._dist(pos, mg) <= 1:
                # 到达（或已在目标格）：清除指令，交给正常决策
                self._manual_goto.pop(u["uid"], None)
                self._manual_done.append(u["uid"])
            else:
                step = self.pf.next_step(pos, mg)
                if step:
                    self._decisions[u["uid"]] = {"tag": "manual", "goal": list(mg)}
                    return ("MOVE", {"direction": dir_name(
                        step[0] - pos[0], step[1] - pos[1])})
                # A* 无路：标记不可达 + 清除指令
                self.mem.mark_unreachable(mg)
                self._manual_goto.pop(u["uid"], None)
                self._manual_done.append(u["uid"])

        # 冲突失败退避：短停 2 tick 打破单位间同步锁死（见 decide 的 backoff）
        if obs.tick < self._move_backoff.get(u["uid"], 0):
            return None
        # 位置停滞检测：侦察目标持续 10+ tick 位置未变（到不了/被挡死）→
        # 换侦察方向（长局钉死场景）；短局单位一直在动，不触发（失败计数
        # 换向在短局拥挤时频繁误触 → 侦察路线跳变，-30 fitness）
        hst = self._unit_hist.setdefault(u["uid"], [])
        prev_pos = hst[-1] if hst else None
        if prev_pos == pos:
            self._scout_stuck[u["uid"]] = self._scout_stuck.get(u["uid"], 0) + 1
        else:
            self._scout_stuck[u["uid"]] = 0
        if self._scout_stuck.get(u["uid"], 0) >= 10 \
                and u["uid"] in self._scout_goal:
            self._scout_stuck[u["uid"]] = 0
            self._scout_goal.pop(u["uid"], None)
            self._scout_stage[u["uid"]] = (
                self._scout_stage.get(u["uid"], 0) + 1) % 8
        hst.append(pos)
        if len(hst) > 3:
            hst.pop(0)
        # Ranger 的探索分支已经维护完整路径。通用 A-B-A 脱离器若在这里
        # 插入随机一步，会让持久化路径失效；下一 Tick 重算后又走回原路，
        # 反而制造无限 osc_break。探索目标有效时交给路径层处理动态阻挡。
        ranger_on_path = utype == "RANGER" and u["uid"] in self._explore_goal
        if len(hst) == 3 and hst[0] == pos and hst[1] != pos \
                and not ranger_on_path:
            # 振荡冷却中：不强行脱离（占位单位可能没走），继续正常决策流程
            if obs.tick >= self._osc_wait.get(u["uid"], 0):
                # A↔B 横跳：把 B 格临时标记为障碍（4 tick）——A* 换路径的
                # 根因是两条等长路径第一步摇摆，只推一步下 tick 又走回；
                # 屏蔽 B 格后 A* 被迫走第三条路，真正脱离。
                bcell = tuple(hst[1])
                in_corridor = self._core_pocket_dist.get(bcell, 99) <= 3
                if not in_corridor and bcell not in self.mem.obstacles \
                        and bcell not in self.mem.resources:
                    # 资源点格/单位占位格不标记：振荡的另一格可能是资源点
                    # （排队采集时在资源格旁横跳），标记为障碍会让 A* 判定
                    # 目标不可达；交货走廊（口袋 BFS<=3）也不标记——它被
                    # temp_block 会让漏斗判空、exit_cave 全禁用（线上
                    # 69119-69166 卡死根因），走廊内"振荡"只是排队挤满格
                    occupied = {tuple(uu["pos"]) for uu in obs.units}
                    if obs.core is not None:
                        occupied.add(tuple(obs.core["pos"]))
                    if bcell not in occupied:
                        self.mem.temp_blocked[bcell] = [obs.tick + 4, 0]
                # 已试方向不重复：上次脱离被挡（位置没变又触发振荡）→ 换方向
                tried = self._osc_tried.get(u["uid"], set())
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    dn = dir_name(dx, dy)
                    if dn in tried:
                        continue
                    cand = (pos[0] + dx, pos[1] + dy)
                    if cand != bcell and not self._is_obstacle(*cand) \
                            and cand not in self.mem.temp_blocked:
                        self._osc_tried[u["uid"]] = tried | {dn}
                        self._dbg(u["uid"], "osc_break")
                        return ("MOVE", {"direction": dn})
                # 四方向全试过仍被挡：冷却 10 tick 再重试（占位单位可能已走开）
                self._osc_wait[u["uid"]] = obs.tick + 10
                self._osc_tried.pop(u["uid"], None)
                self._dbg(u["uid"], "osc_wait")
                return None
        else:
            self._osc_tried.pop(u["uid"], None)  # 不再振荡：清已试方向

        # Beacon 任务 / 携带 Beacon 回家
        if self._beacon_task == u["uid"] and bpos:
            if pos == bpos:
                return ("PICKUP_BEACON", {})
            step = self.pf.next_step(pos, bpos)
            if step:
                return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                       step[1] - pos[1])})
        if u["carries_beacon"] and core_pos:
            if self._dist(pos, core_pos) <= 1:
                return None
            goal = self._core_approach_goal(pos, core_pos)
            if goal:
                step = self.pf.next_step(pos, goal)
                if step:
                    return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                           step[1] - pos[1])})
        if utype == "WORKER":
            return self._decide_worker(u, obs, core_pos, threat, d_enemy,
                                       all_threats, bpos, defense)
        if utype == "VANGUARD":
            return self._decide_vanguard(u, obs, core_pos, threat, d_enemy,
                                         enemies, enemy_cores, bpos, defense)
        return self._decide_ranger(u, obs, core_pos, threat, d_enemy,
                                   enemies, enemy_cores, bpos, defense)

    # ------------------------------------------------------------------
    # 合体同步移动：先锋/游侠同格(战斗组)时，避免一先一后串行
    # ------------------------------------------------------------------
    def _apply_group_sync(self, u, pos, act, obs):
        """战斗组(V+R 同格)同步移动。

        同格两单位若本 tick 都算出了同一个目的地(先锋先动、游侠被引擎挡下
        再滞后一步)，则把后处理的一方改到伙伴目的地旁的空位，二者同 tick 各
        走一步、保持编队，消除"先锋动一步、游侠跟一步"的串行滞后。"""
        partner_uid = self._pair_of.get(u["uid"])
        if partner_uid is None or act is None or act[0] != "MOVE":
            return act
        dvec = {"UP": (0, -1), "DOWN": (0, 1),
                "LEFT": (-1, 0), "RIGHT": (1, 0)}[act[1]["direction"]]
        dest = (pos[0] + dvec[0], pos[1] + dvec[1])
        pdest = self._intended_dest.get(partner_uid)
        if pdest is None or pdest != dest:
            return act  # 伙伴未处理或目的地不同 → 不冲突，原样移动
        alt = self._group_alt_step(pos, dest, obs)
        if alt is None:
            return act  # 旁边无空位 → 兜底原样(罕见)
        adx, ady = alt[0] - pos[0], alt[1] - pos[1]
        return ("MOVE", {"direction": dir_name(adx, ady)})

    def _group_alt_step(self, pos, goal, obs):
        """在 pos 的 4 邻格里挑一个：空位、非伙伴目的地 goal、且离 goal 最近
        （即仍朝共同目标推进、仅错开一格保持编队）。无合适格返回 None。"""
        occ = {tuple(u["pos"]) for u in obs.units}
        occ.discard(tuple(pos))  # 自己当前格本 tick 离开，不算障碍
        best = None
        best_d = None
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (pos[0] + dx, pos[1] + dy)
            if self._is_obstacle(*c):
                continue
            if c in occ:
                continue
            if c == goal:
                continue  # 不与先锋抢同一格
            d = self._dist(c, goal)
            if best_d is None or d < best_d:
                best_d = d
                best = c
        return best

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _decide_worker(self, u, obs, core_pos, threat, d_enemy, all_threats,
                       bpos, defense):
        g = self.genes
        pos = u["pos"]
        uid = u["uid"]
        # 空载且站在 Core 格：让出——满载 Worker 需要进格 DEPOSIT，
        # 空载占着 Core 格会导致排队死锁（满载全在 Core 旁 WAIT）。
        # 抢占式：四邻全被自己人占时硬挤（方向按 tick 轮换），引擎的
        # leaving/2 环交换会解开"空载 Worker 想出去、满载 Worker 想进格"
        # 的互锁（否则 A* 缓存路径第一步永远是占位格 → 永久卡 Core 格）。
        # 低血治疗（HEAL 需要站 Core 格）已在 _decide_unit 通用部分优先处理。
        if u["cargo"] == 0 and core_pos and pos == core_pos:
            return self._yield_core(pos, obs, uid)
        # Siege screen: sacrifice a small number of empty Workers to occupy the
        # line between the nearest attacker and Core.  This branch precedes
        # normal flee/economy work; otherwise the selected screen immediately
        # runs away and 18 Workers still contribute zero defensive delay.
        if defense and u["cargo"] == 0 and uid in self._block_workers \
                and self._defense_point is not None:
            bp = self._block_point(self._defense_point, core_pos)
            if bp:
                self._dbg(uid, "siege_block", bp)
                if tuple(pos) == tuple(bp):
                    return None
                act = self._move_toward(pos, bp)
                if act:
                    return act
        # 防御：人口达阈值且 Core 危险 → 远处 Worker 自毁腾人口造 Ranger
        if defense and obs.population >= g["selfdestruct_pop"]:
            workers = [(uu["uid"], uu["pos"]) for uu in obs.units
                       if uu["utype"] == "WORKER"]
            if len(workers) > 2:
                farthest = max(workers, key=lambda w: self._dist(w[1], core_pos))
                if farthest[0] == uid:
                    self._dbg(uid, "self_destruct")
                    return ("SELF_DESTRUCT", {})
        # 危险：附近有敌人战斗单位 → 逃跑（Core 无攻击力，不触发）
        fighters = [e for e in obs.enemies if e["utype"] != "WORKER"]
        immediate = [tuple(e["pos"]) for e in fighters
                     if self._dist(pos, e["pos"])
                     <= (4 if e["utype"] == "RANGER" else 3)]
        if immediate:
            self._worker_danger[uid] = (
                tuple(immediate), obs.tick + WORKER_DANGER_HOLD_TICKS)
        remembered = self._worker_danger.get(uid)
        danger = immediate or (list(remembered[0]) if remembered
                               and remembered[1] >= obs.tick else [])
        if danger and core_pos:
            # Core HP/shield is not offensive strength.  The old global threat
            # ratio included it, so even an adjacent hostile rarely exceeded
            # worker_flee and Workers stood still to die.  Treat worker_flee as
            # the local evade tendency instead.  Keep evading briefly after the
            # fighter leaves vision so the Worker does not reverse next Tick.
            if g["worker_flee"] >= 0.5:
                step = self._escape_step(pos, danger, obs, core_pos)
                tag = "flee" if immediate else "flee_memory"
                self._dbg(uid, tag, step or core_pos)
                if step is not None:
                    return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                           step[1] - pos[1])})
                return None
        # 满载 → 回 Core 存放（Core 迁移中不能存，跟随 Core 走；资源满时探路）
        if u["cargo"] > 0:
            core_migrating = obs.core is not None and obs.core["migration"] is not None
            # 满仓判定：资源达到容量上限才 hold（capacity-2 会让资源 93
            # 就停——攒资到 95 永远差 2；DEPOSIT 失败不扣 cargo，无溢出
            # 风险，不需要预留）
            core_full = core_pos is not None and obs.core is not None and \
                obs.core["resources"] >= obs.core["capacity"]
            if core_full:
                if self._vault_active:
                    return self._vault_hold(
                        u, obs, core_pos, VAULT_WORKER_RADII, "vault_worker")
                # 资源满仓：带 cargo 待命（对齐真实玩家"核心资源已满，
                # 保留 Cargo 等待生产消耗"）——不白采、不空转侦察，
                # 等 Core 消耗资源（生产/修盾）后恢复交货
                self._dbg(uid, "hold_full", core_pos)
                return None
            if core_migrating:
                # 跟随迁移中的 Core（它最终会在富集区停下接受存放）
                if core_pos and self._dist(pos, core_pos) > 2:
                    goal = self._core_approach_goal(pos, core_pos)
                    if goal:
                        step = self.pf.next_step(pos, goal)
                        if step:
                            self._dbg(uid, "return_core", core_pos)
                            return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                                   step[1] - pos[1])})
                return None
            if not core_full:
                if core_pos and pos == core_pos:
                    self._dbg(uid, "deposit", core_pos)
                    return ("DEPOSIT", {})
                if core_pos:
                    d = self._dist(pos, core_pos)
                    core_cell_occupied = any(
                        uu["pos"] == core_pos and uu["uid"] != uid
                        for uu in obs.units)
                    if d == 1:
                        # 始终提交进格意图：Core 格被占也 MOVE（引擎的
                        # leaving/2 环交换机制会解开互锁；撞 Core 格的失败
                        # 反馈不污染 temp_blocked——Core 格在 occupied 保护内）。
                        # 不自己提前 wait：退出 dependency graph 会让链式断裂。
                        dx, dy = core_pos[0] - pos[0], core_pos[1] - pos[1]
                        self._dbg(uid, "return_core", core_pos)
                        return ("MOVE", {"direction": dir_name(dx, dy)})
                    if d <= 2:
                        # 满载推进不设"uid 最小才放行"的硬锁：非最小 uid 的
                        # 满载若 return None（静止），会卡死整条链式交换——
                        # 空载出洞需要满载让位、满载进 Core 需要空载让位，
                        # 任意一环不提交移动意图链条就断（线上洞穴死锁
                        # 实证：(-59,122)/(-58,123) 的满载全部 wait，洞内
                        # 空载和目标永远是满格 → 交货停摆）。改为全部提交
                        # 移动意图，由引擎 leaving/2 环交换串行化；同格
                        # 满载天然互斥（容量 2 + Core 占 1 槽），一次只
                        # 有一个能进 Core 格。
                        pass
                    # 沿持久化完整路径走（A* 一次算整条，防锯齿振荡）；
                    # 路径目标是 Core 四邻格（Core 格本身是 A* 禁区）
                    act = self._follow_path(uid, pos, obs)
                    if act:
                        self._dbg(uid, "return_core", core_pos)
                        return act
                    # 重算完整路径
                    goal = self._core_approach_goal(pos, core_pos)
                    if goal:
                        path = self.pf.find(pos, goal)
                        if path:
                            self._worker_path[uid] = (path, obs.tick)
                            nxt = path[0]
                            self._dbg(uid, "return_core", core_pos)
                            return ("MOVE", {"direction": dir_name(nxt[0] - pos[0],
                                                                   nxt[1] - pos[1])})
                    # 口袋/单格走廊死锁兜底：普通 A* 把满格当硬墙，必经格
                    # 被占满时判不可达 → 全队 WAIT。无视满格重寻路到 Core
                    # 四邻（引擎 leaving/2 环交换解开互锁）；仍失败则向 Core
                    # 方向贪心一格。
                    act = self._full_worker_push(pos, core_pos, obs, uid)
                    if act:
                        return act
                return None
        if self._vault_active:
            return self._vault_hold(
                u, obs, core_pos, VAULT_WORKER_RADII, "vault_worker")
        # 洞内空载撤离：cargo==0 且站在洞内（口袋 BFS<=3）→ 先出洞让出
        # 交货通道。满载分支已提前 return；普通 A* 把满格当硬墙，洞内被
        # 占满时空载 worker 连洞口都出不去（全员 wait，满载全堵在门口）。
        if u["cargo"] == 0:
            act = self._exit_cave(u, pos, obs, uid)
            if act:
                return act
        # 空货：站在资源上 → 采集（记录 chunk 供侦察回访——补给只发生在已消耗 chunk）
        if pos in obs.resources and u["cargo"] == 0:
            # 官方同点竞争只让 UUID 最小的 Worker 成功。其余 Worker 本 Tick
            # 等待资源结算，避免重复提交必败 HARVEST 和污染任务统计。
            contenders = [uu for uu in obs.units
                          if uu["utype"] == "WORKER" and uu["cargo"] == 0
                          and tuple(uu["pos"]) == tuple(pos)]
            winner = min(contenders, key=lambda uu: self._uid_num(uu["uid"]))
            if winner["uid"] == uid:
                self._resource_claims[tuple(pos)] = uid
                self.mem.harvest_chunks[(pos[0] // 32, pos[1] // 32)] = obs.tick
                self._dbg(uid, "harvest", pos)
                return ("HARVEST", {})
            self._dbg(uid, "wait_resource", pos)
            return None
        # 发现即采：视野内（≤3 格，worker 视野半径）有资源 → 直接去采。
        # 常理：侦察/空闲 worker 发现资源就采集交付，不等集中分配——
        # 解决"侦察 worker 路过资源不采"与"远处 worker 被分去采近点"。
        # 视野内资源是稳定条件（持续可见直到被采），不引入抖动。
        if u["cargo"] == 0 and obs.resources:
            claimed = set(self._resource_claims)
            claimed.update(goal[0] for other, goal in self._worker_goal.items()
                           if other != uid)
            claimed.update(target for other, target in self._assignments.items()
                           if other != uid)
            assigned = self._assignments.get(uid)
            nearby = [tuple(c) for c in obs.resources
                      if self._dist(pos, c) <= 3 and tuple(c) not in claimed]
            nearby.sort(key=lambda c: (c != assigned, self._dist(pos, c), c))
            best_c = nearby[0] if nearby else None
            if best_c is not None:
                if self._worker_goal.get(uid, (None, -99))[0] != best_c:
                    self._goal_start[uid] = pos
                self._worker_goal[uid] = (best_c, obs.tick)
                act = self._move_toward(pos, best_c)
                if act:
                    self._resource_claims[best_c] = uid
                    self._dbg(uid, "goto_near", best_c)
                    return act
        # 资源目标（持久化 + 收益驱动 hysteresis，2026-08-07 按专家建议）：
        # 固定 12 tick 粘性 → 切换条件 = 收益差 > SwitchCost(进度)。
        # 单位刚开始执行时切换成本低（能响应明显更优机会）；越接近完成
        # 切换成本越高（自动保持时间一致性，防决策层振荡）。
        goal = self._worker_goal.get(uid)
        if goal is not None:
            gpos = goal[0]
            if self._dist(pos, gpos) <= 1:
                del self._worker_goal[uid]   # 到达：完成
            elif self._dist(gpos, core_pos) > 60 if core_pos else False:
                del self._worker_goal[uid]   # 目标超出核心区：重新选
            else:
                # 新分配的目标（集中分配器给的）若收益显著更优 → 切换；
                # 收益差 < SwitchCost → 保持当前目标（时间一致性）
                new_t = self._assignments.get(uid)
                if new_t is not None and new_t != gpos:
                    keep_cost = self._dist(pos, gpos) + self._dist(gpos, core_pos)
                    new_cost = self._dist(pos, new_t) + self._dist(new_t, core_pos)
                    # 进度 = 已走 / (已走 + 剩余)——越接近完成越不愿换
                    travelled = self._dist(self._goal_start.get(uid, pos), pos)
                    remain = self._dist(pos, gpos)
                    prog = travelled / max(1, travelled + remain)
                    switch_cost = 2.0 * prog   # β=2：走一半时需省 1 格以上才换
                    if keep_cost - new_cost > switch_cost:
                        self._worker_goal[uid] = (new_t, obs.tick)
                        self._goal_start[uid] = pos
                        gpos = new_t
                act = self._move_toward(pos, gpos)
                if act:
                    self._dbg(uid, "goto_resource", gpos)
                    return act
                del self._worker_goal[uid]
        target = self._assignments.get(uid)
        if target:
            if self._worker_goal.get(uid, (None, -99))[0] != target:
                self._goal_start[uid] = pos
            self._worker_goal[uid] = (target, obs.tick)
            act = self._move_toward(pos, target)
            if act:
                self._dbg(uid, "goto_resource", target)
                return act
            del self._worker_goal[uid]
        # 进攻堵路任务：仅闲置 Worker（siege 屏障已在经济分支前处理）
        if self._attack_point and core_pos and uid in self._block_workers:
            bp = self._block_point(self._attack_point, core_pos)
            if bp:
                act = self._move_toward(pos, bp)
                if act:
                    self._dbg(uid, "block", bp)
                    return act
        # 拦截敌方 Worker（压制经济）：空闲 + 可见敌方 Worker + 无可见敌方战斗单位
        if g["harass_workers"] > 0.5 and core_pos:
            ew = [tuple(e["pos"]) for e in obs.enemies if e["utype"] == "WORKER"]
            ef = [e for e in obs.enemies if e["utype"] != "WORKER"]
            if ew and not ef:
                target = self._nearest_of(pos, ew)
                if target and self._dist(target, core_pos) <= 25:
                    act = self._move_toward(pos, target)
                    if act:
                        self._dbg(uid, "harass", target)
                        return act
        # 区域回访：只派一个 Worker 认领（目标已在 Core 12 格内，认领距离同步）
        if core_pos and self._revisit_goal and not self._revisit_assigned \
                and self._dist(self._revisit_goal, core_pos) <= 12:
            self._revisit_assigned = True
            act = self._move_toward(pos, self._revisit_goal)
            if act:
                self._dbg(uid, "revisit", self._revisit_goal)
                return act
            self.mem.mark_unreachable(self._revisit_goal)
            self._revisit_goal = None
        # 核心区侦察（参考 Drew-Z scout）：按 uid 固定方向槽扫 10/20/30 环带，
        # 到达即换下一环；目标格优先"久未看"（顺带发现补给资源，防经济死锁）。
        # 目标带 12 tick 粘性（_scout_goal）：重算点抖动时不换目标 → 防振荡
        patrol = self._patrol_point(obs, uid, core_pos)
        if patrol:
            sg = self._scout_goal.get(uid)
            if sg is not None and obs.tick - sg[1] < WORKER_SCOUT_GOAL_TTL \
                    and not self._is_obstacle(*sg[0]):
                patrol = sg[0]
            else:
                self._scout_goal[uid] = (patrol, obs.tick)
            patrol = self._scout_goal[uid][0]
            if self._dist(pos, patrol) <= 1:
                # 已到达：标记访问，推进到下一个侦察点
                self._scout_visited[patrol] = obs.tick
                self._scout_stage[uid] = (self._scout_stage.get(uid, 0) + 1) % 8
                patrol = self._patrol_point(obs, uid, core_pos)
                self._scout_goal[uid] = (patrol, obs.tick) if patrol else (patrol, 0)
            if patrol:
                act = self._move_toward(pos, patrol)
                if act:
                    self._dbg(uid, "scout", patrol)
                    return act
                # A* 无路：换下一个侦察点（不罚站；不标记 unreachable——
                # 侦察点只是"看一眼"，永久标记会耗尽全部候选 → 全员 WAIT）
                self._scout_visited[patrol] = obs.tick
                self._scout_stage[uid] = (self._scout_stage.get(uid, 0) + 1) % 8
                patrol = self._patrol_point(obs, uid, core_pos)
                self._scout_goal[uid] = (patrol, obs.tick) if patrol else (patrol, 0)
        return None

    def _explore_value(self, target, horizon=25):
        """探索覆盖潜力（rollout 扩展 1，2026-08-07）：从目标点出发未来
        horizon 格范围内的**未访问格数**（菱形扫描，障碍过滤）——衡量
        "去这个点能覆盖多少新区域"。替代"最近未访问"（可能指向死胡同/
        已访问包围的孤格——覆盖少）。"""
        visited = self.mem.visited
        seen = self.mem.area_seen
        n = 0
        tx, ty = target
        for dx in range(-horizon, horizon + 1):
            rem = horizon - abs(dx)
            for dy in range(-rem, rem + 1):
                p = (tx + dx, ty + dy)
                if p in visited or p in seen:
                    continue
                if self._is_obstacle(*p):
                    continue
                n += 1
        return n

    def _explore_score(self, target, horizon=25):
        """探索信息增益（实验 C，2026-08-07）：覆盖潜力 + 预期资源发现。
        局部资源密度先验（目标附近已知资源数——资源聚簇特性：已知富集
        区附近更可能有资源）→ 预期资源数 = 局部密度 × 覆盖格数。
        资源价值按 3 格覆盖等价计。"""
        covered = self._explore_value(target, horizon)
        local = sum(1 for r in self.mem.resources
                    if self._dist(r, target) <= horizon)
        expected_res = (local / max(1, covered)) * covered
        return covered + expected_res * 3.0

    def _rollout_harvest(self, core_pos, w_pos, cand, horizon=100,
                         shared=0):
        """轻量前瞻模拟（MVP，2026-08-07）：worker 采候选资源点未来
        horizon tick 的预测收获——逐 tick 模拟采集循环（去程→采→回程
        →交付→再来），考虑：
        - 往返路程（曼哈顿近似——rollout 是相对排序，绝对精度不重要）
        - 资源点补给节奏（官方：采后 4 tick 回补）
        - 共享竞争（shared=其他也在采此点的 worker 数 → 排队稀释）
        返回预测收获次数。"""
        d_go = self._dist(w_pos, cand)
        d_back = self._dist(cand, core_pos)
        # 一个循环：去程 d_go + 采集 1 + 回程 d_back + 交付 1
        cycle = d_go + 1 + d_back + 1
        if cycle <= 0:
            return 0.0
        replenish = 4.0 * (shared + 1)   # 竞争越多补给间隔越长
        # 可完成的循环数（受补给节奏限制：每次采完等 replenish）
        n_full = horizon / max(cycle, replenish)
        return n_full

    def _resource_candidates(self, obs, core_pos):
        """候选资源点：记忆点过滤 + 排序（22 格内优先，32 格内兜底）。

        不做认领/去重——集中分配器（decide 里的 _assignments）负责一对一。
        避开已知敌方 Core 附近 3 格——不去敌人地盘采资源（猥琐发育，
        且避免 Worker 被敌方单位卡住）。
        """
        cands = []
        for c in self.mem.resources:
            if self.mem.is_unreachable(c):
                continue
            empty = obs.tick - self.mem.empty_since.get(c, obs.tick)
            if empty > 12:
                continue  # 视野确认空且超过 12 tick 未再确认 → 放弃
            cands.append(c)
        # 采集距离：32 格内可用点充足（≥3）→ 限 32 格（防远征浪费，短局
        # 密集环境最优）；不足 → 放宽到 60 格（远征新点——侦察发现的点
        # 常在 32 格外，不远征就"有资源采不到"，经济枯竭）
        if core_pos is not None:
            maxd = 32 if sum(1 for c in cands
                             if self._dist(c, core_pos) <= 32) >= 3 else 60
            cands = [c for c in cands if self._dist(c, core_pos) <= maxd]
        enemy_cores = [p for k, (p, _t) in self.mem.last_enemy_pos.items()
                       if str(k).startswith("core_")]
        if enemy_cores:
            cands = [c for c in cands
                     if not any(self._dist(c, ec) < 3 for ec in enemy_cores)]
        near = [c for c in cands if self._dist(c, core_pos) <= 22]
        far = [c for c in cands if self._dist(c, core_pos) > 22]
        near.sort(key=lambda c: self._dist(c, core_pos))
        far.sort(key=lambda c: self._dist(c, core_pos))
        # 注：采过格（harvest_points）不做采集候选——多轮 A/B 实验证明回访
        # 旧格扑空后站桩等待，竞争不过侦察发现新点（-10 fitness）。补给
        # 发现由侦察承担（环带 + harvest_chunks 优先），看到后重新进入
        # resources 记忆。
        return near + far

    def _nearest_unvisited(self, pos, limit=40, exclude=None):
        """在记忆图上 BFS，找距离 pos 最近的未访问格（用于侦察/探索）。

        exclude: 可选集合——跳过其中目标（多 Ranger 探索去重用：
        已被认领的格跳过，视野不重合）。"""
        q = deque([(pos, 0)])
        seen = {pos}
        while q:
            (x, y), d = q.popleft()
            if d >= limit:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (x + dx, y + dy)
                if nxt in seen:
                    continue
                if self._is_obstacle(*nxt):
                    continue
                if nxt not in self.mem.visited:
                    if exclude is None or nxt not in exclude:
                        return nxt
                    seen.add(nxt)
                    q.append((nxt, d + 1))
                    continue
                seen.add(nxt)
                q.append((nxt, d + 1))
        return None

    @staticmethod
    def _uid_num(uid):
        """把 uid 转成稳定整数（模拟器 int 直接用；正式世界 UUID 用其 int 值）。"""
        from uuid import UUID
        return uid.int if isinstance(uid, UUID) else uid

    def _patrol_point(self, obs, uid, core_pos):
        """侦察点选择（参考 Drew-Z arena-hero-agent scout）：

        - 每 Worker 按 uid 固定方向槽（8 方向），stage 轮转换方向
        - 环带按 slot 分组：slot 0-7 扫 10/20/30 环，slot 8+ 扫 20/30/40 环
        - 同方向候选点排序：格越久未看越优先（顺带发现 4 tick 补给的新资源）
        - 资源配额高的 chunk 优先（官方规则：越靠近中央越富）
        """
        if core_pos is None:
            return None
        u = self._uid_num(uid) % 16    # 稳定小值 0-15（UUID 的 int 很大，必须取模）
        vectors = ((1, 0), (1, 1), (0, 1), (-1, 1),
                   (-1, 0), (-1, -1), (0, -1), (1, -1))
        stage = self._scout_stage.get(uid, 0)
        vx, vy = vectors[(u + stage) % 8]
        base = u // 8              # slot 0-7 → 环 1；slot 8+ → 环 2
        best, best_key = None, None
        now = obs.tick
        # 饥饿模式（连续 200 tick 无采集）：扩大侦察环带（8/16/24/32/40）
        # 找新资源区；正常模式近环（10/20/30）——远侦察会把 Worker 拖离
        # 采集，只在饥饿时启用（短局回归验证：不触发时行为不变）
        hunger_anchor = self._hunger_since \
            if self._hunger_since is not None else self._starve_since
        hungry = bool(hunger_anchor and obs.tick - hunger_anchor > 200)
        max_ring = 5 if hungry else 3
        step = 8 if hungry else 10
        for off in range(max_ring):
            radius = step * min(base + 1 + off, max_ring)
            scale = radius // (abs(vx) + abs(vy))
            cand = (core_pos[0] + vx * scale, core_pos[1] + vy * scale)
            if self.bounds is not None:
                x_lo, x_hi, y_lo, y_hi = self.bounds
                if not (x_lo <= cand[0] <= x_hi and y_lo <= cand[1] <= y_hi):
                    continue  # 世界边界外：跳过
            if cand in self.mem.obstacles or self.mem.is_unreachable(cand):
                continue
            # 已被其他 Worker 认领（粘性/本 Tick）→ 不撞车；但自己的粘性
            # 目标除外——否则每 tick 重算时自己的目标被跳过 → 目标永动 → 振荡
            my_goal = self._scout_goal.get(uid)
            my_point = my_goal[0] if my_goal else None
            if cand in self._scout_taken and cand != my_point:
                continue
            # 按 chunk 级扩散：优先"最近采过资源"的 chunk（补给回访）→
            # 再"整个 chunk 都没看过/最久没看"的（探索扩散）
            chunk = (cand[0] // 32, cand[1] // 32)
            h = self.mem.harvest_chunks.get(chunk, -1)
            recency = (now - h) if h >= 0 else 99999   # 刚采过 → 优先回访
            seen = self.mem.chunk_seen.get(chunk, 0)   # 0=从未看过 → 优先
            visited = self._scout_visited.get(cand, 0)  # 越久没来 → 优先
            key = (recency, seen, visited, -self._chunk_quota(cand),
                   self._dist(cand, core_pos), cand[0], cand[1])
            if best_key is None or key < best_key:
                best_key, best = key, cand
        if best is not None:
            self._scout_taken.add(best)   # 认领：本 Tick 其他 Worker 不再选它
        return best

    @staticmethod
    def _chunk_quota(pos):
        """官方资源配额公式（doc.arenahero.io rules/map-and-vision）：
        chunk 32×32，ring=axis(cx)+axis(cy)，quota=max(2, floor(128/(8+ring)))。"""
        def axis(c):
            return c if c >= 0 else -c - 1
        cx, cy = pos[0] // 32, pos[1] // 32
        return max(2, (16 * 8) // (8 + axis(cx) + axis(cy)))

    # ------------------------------------------------------------------
    # Vanguard
    # ------------------------------------------------------------------
    def _decide_vanguard(self, u, obs, core_pos, threat, d_enemy,
                         enemies, enemy_cores, bpos, defense):
        g = self.genes
        pos = u["pos"]
        uid = u["uid"]
        # 治疗残留/路径误入：站在 Core 格 → 无条件让出（低血治疗已由通用
        # 分支先拦截；满载 Worker 需要进格存放，Core 格被占 = 交货死锁）。
        # 抢占式：门口被自己人占着也提交移动意图——引擎 leaving/2 环交换
        # 会解开"Vanguard 等门口空、Worker 等 Core 格空"的互锁；
        # 方向按 tick 轮换，撞墙后换方向。
        if core_pos and pos == core_pos:
            return self._yield_core(pos, obs, uid)
        # 贴身敌人必须先打。旧顺序在 defense=True 时让远端 Vanguard
        # 直接回撤，即使敌人已在相邻格，也会白白放弃一次确定命中的 SWEEP。
        adjacent = [e for e in enemies if self._dist(pos, e[0]) == 1]
        if adjacent:
            target = min(adjacent, key=lambda e: (e[2] == "WORKER", e[1],
                                                   self._uid_num(e[3])))
            self._dbg(uid, "sweep", target[0])
            return ("SWEEP", {"direction": dir_name(target[0][0] - pos[0],
                                                       target[0][1] - pos[1])})
        # Limited counterattack: the nearest two healthy Vanguards close on a
        # visible fighter while the rest keep the inner ring.  The old posture
        # held every defender at Core, allowing a lone Ranger to shoot Workers
        # indefinitely from outside SWEEP range.
        if defense and core_pos:
            if uid in self._siege_vanguards \
                    and self._defense_point is not None:
                target = self._defense_point
                step = self.pf.next_step(pos, target)
                if step and self._projected_occupancy(step, obs) < 2:
                    self._dbg(uid, "siege_intercept_vanguard", target)
                    return ("MOVE", {"direction": dir_name(
                        step[0] - pos[0], step[1] - pos[1])})
            if self._dist(pos, core_pos) > 2:
                goal = self._core_approach_goal(pos, core_pos)
                if goal:
                    step = self.pf.next_step(pos, goal)
                    if step:
                        self._dbg(uid, "siege_vanguard", core_pos)
                        return ("MOVE", {"direction": dir_name(
                            step[0] - pos[0], step[1] - pos[1])})
            self._dbg(uid, "siege_vanguard", core_pos)
            return None
        if self._vault_active:
            return self._vault_hold(
                u, obs, core_pos, VAULT_VANGUARD_RADII, "vault_vanguard")
        if uid in self._home_vanguards:
            return self._vault_hold(
                u, obs, core_pos, HOME_VANGUARD_RADII, "home_vanguard")
        # 全局进攻计划（按敌方人数2倍调配 + 包围）：被分配到某簇则按聚齐
        # 状态机动包围或正式进攻；未分配单位在计划激活时不追击(避免多打一)。
        pa = self._combat_plan_action(u, obs, core_pos, enemies,
                                      enemy_cores, "VANGUARD")
        if pa is not None:
            return pa
        # 友军受援：被分配去支援受攻击队友（不受威胁比例限制）
        sa = self._support_action(u, obs, core_pos)
        if sa is not None:
            return sa
        # 无进攻计划（无可见敌方集群）时才走旧追击逻辑兜底
        if not self._combat_clusters:
            target, mem_key = self._select_combat_target(pos, enemies,
                                                         enemy_cores, obs)
            if target and mem_key is not None:
                if self._dist(pos, target) <= 1:
                    # 已到达记忆位置且无敌人 → 立即遗忘（防追丢后横跳）
                    self._forget_enemy(mem_key)
                    self._pursuing.pop(uid, None)
                    target = None
                else:
                    self._pursuing[uid] = (mem_key, obs.tick)
            elif uid in self._pursuing:
                self._pursuing.pop(uid, None)   # 可见目标：正常战斗，清除追击状态
            if target:
                d = self._dist(pos, target)
                if d == 1:
                    self._dbg(uid, "sweep", target)
                    return ("SWEEP", {"direction": dir_name(target[0] - pos[0],
                                                            target[1] - pos[1])})
            # 进攻：已认领的可见/记忆目标（兵力占优才出击）
            if target and threat <= g["army_trigger"] * 1.5:
                step = self.pf.next_step(pos, target)
                if step:
                    self._dbg(uid, "attack", target)
                    return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                           step[1] - pos[1])})
        # 突袭确认静止的敌方 Core（Drew-Z：strike group，守卫留守）
        if self._raid_point and u["uid"] not in self._raid_guards:
            d = self._dist(pos, self._raid_point)
            if d > 2:
                step = self.pf.next_step(pos, self._raid_point)
                if step:
                    self._dbg(uid, "raid", self._raid_point)
                    return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                           step[1] - pos[1])})
        # 两个 Vanguard 可以合法叠在同一格；若直接进入 parking，它们常因
        # A* 首步相同而整路同步，永远无法分开。叠格检查必须在 explore
        # 之前：空闲侦察对同格单位返回同一目标，会让叠格单位沿相同路径
        # 永久同步移动。仅对叠格情况先散开，单个 Vanguard 仍保持原有
        # explore/parking 行为。
        if sum(1 for uu in obs.units if tuple(uu["pos"]) == tuple(pos)) > 1:
            spread = self._idle_spread(u, pos, obs, core_pos, defense)
            if spread:
                return spread
        # 空闲侦察：向未访问区域探索（发现敌人/资源），限离家 15 格——
        # 战斗单位是 Core 的防线，跑太远=防御空虚+暴露送死。
        # 目标带 12 tick 粘性（_explore_goal）：visited 每 tick 增长导致
        # 目标抖动时不换向 → 防振荡
        eg = self._explore_goal.get(uid)
        if eg is not None and obs.tick - eg[1] < COMBAT_EXPLORE_GOAL_TTL \
                and not self._is_obstacle(*eg[0]):
            explore = eg[0]
        else:
            self._explore_goal.pop(uid, None)
            explore = self._nearest_unvisited(pos, limit=int(g["scout_search_limit"]))
            if explore and core_pos and self._dist(explore, core_pos) > g["idle_explore_radius"]:
                explore = None
            if explore:
                self._explore_goal[uid] = (explore, obs.tick)
        if explore and self._dist(pos, explore) > 0:
            step = self.pf.next_step(pos, explore)
            if step:
                self._dbg(uid, "explore", explore)
                return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                       step[1] - pos[1])})
            # A* 无路：标记不可达 + 清粘性（否则每 tick 重选同一目标死循环）
            self.mem.mark_unreachable(explore)
            self._explore_goal.pop(uid, None)
        else:
            self._explore_goal.pop(uid, None)  # 到达（站在目标格上）/无目标：清粘性
        # idle 巡逻：代替"原地待命"，空闲战斗单位分到三个巡逻圈之一绕圈
        # 巡防（半径递增，单位持续移动而非在 Core 门口聚集等待）。
        patrol = self._idle_patrol(u, obs, core_pos)
        if patrol:
            return patrol
        # idle 散开：叠格/堵 Core 门口时让位（防满格挡 Worker 交货路线）
        spread = self._idle_spread(u, pos, obs, core_pos, defense)
        if spread:
            return spread
        return None

    # ------------------------------------------------------------------
    # Ranger
    # ------------------------------------------------------------------
    def _decide_ranger(self, u, obs, core_pos, threat, d_enemy,
                       enemies, enemy_cores, bpos, defense):
        g = self.genes
        pos = u["pos"]
        uid = u["uid"]
        # 治疗残留/路径误入：站在 Core 格 → 无条件让出（同 Vanguard，
        # 抢占式 + tick 轮换，靠引擎 2 环交换解开互锁）
        if core_pos and pos == core_pos:
            return self._yield_core(pos, obs, uid)
        # Ranger 只有 2 HP。被 Vanguard 贴身时原地换一枪通常会在同 Tick
        # 吃到 SWEEP；先拉开一格，下一 Tick 才有机会持续输出。
        adjacent_vanguards = [
            e for e in enemies
            if e[2] == "VANGUARD" and self._dist(pos, e[0]) == 1
        ]
        if adjacent_vanguards:
            escape = self._escape_step(
                pos, [e[0] for e in adjacent_vanguards], obs, core_pos)
            if escape is not None:
                self._dbg(uid, "ranger_disengage", escape)
                return ("MOVE", {"direction": dir_name(
                    escape[0] - pos[0], escape[1] - pos[1])})
        # 全局进攻计划（按敌方人数2倍调配 + 包围）：被分配到某簇则按聚齐
        # 状态机动包围或正式进攻；未分配单位在计划激活时不射击/不追击。
        pa = self._combat_plan_action(u, obs, core_pos, enemies,
                                      enemy_cores, "RANGER")
        if pa is not None:
            return pa
        # 友军受援：被分配去支援受攻击队友（不受威胁比例限制）
        sa = self._support_action(u, obs, core_pos)
        if sa is not None:
            return sa
        # An unsupported 2 HP Ranger should not accept a stationary ranged
        # trade far from the squad.  Step out of the firing line, then re-engage
        # once support arrives or the enemy follows into a worse position.
        ranged_threats = [
            e for e in enemies
            if e[2] == "RANGER" and self._shot_valid(u, e[0], pos)
        ]
        nearby_support = [
            ally for ally in obs.units
            if ally["uid"] != uid and ally["utype"] != "WORKER"
            and self._dist(pos, ally["pos"]) <= 4
        ]
        if ranged_threats and not nearby_support and not defense:
            escape = self._escape_step(
                pos, [e[0] for e in ranged_threats], obs, core_pos)
            if escape is not None:
                self._dbg(uid, "ranger_disengage", escape)
                return ("MOVE", {"direction": dir_name(
                    escape[0] - pos[0], escape[1] - pos[1])})
        # 射击优先：视野内可射击目标（含预判移动目标）。进攻计划激活且本
        # 单位未被分配 → 不自由射击(交由计划统一调度，避免多打一/乱开火)。
        # （被分配到计划的 Ranger 已在上方 _combat_plan_action 提前返回）
        shot = self._best_shot(u, pos, enemies, enemy_cores) \
            if not self._combat_clusters else None
        if shot:
            # 角落堵死：目标被障碍困住（出口≤2）且一直在动（贴墙来回）
            # → 射必空（结算晚于移动，方向每 tick 变）；改为先走到目标
            # 出口占位——目标无路可走 WAIT → 下 tick 射中（用户反馈：
            # worker 被逼到角落但打不中）
            tid = None
            for _p, _hp, _ut, _uid in enemies:
                if tuple(_p) == shot:
                    tid = _uid
                    break
                # shot 可能是预测格（目标下一步位置——_best_shot 的预判）
                _prev = self.mem.enemy_prev.get(_uid)
                if _prev is not None and _prev != tuple(_p):
                    _dx = _p[0] - _prev[0]
                    _dy = _p[1] - _prev[1]
                    if abs(_dx) + abs(_dy) == 1 \
                            and (_p[0] + _dx, _p[1] + _dy) == shot:
                        tid = _uid
                        break
            if tid is not None:
                tpos = None
                for _p, _hp, _ut, _uid in enemies:
                    if _uid == tid:
                        tpos = tuple(_p)
                        break
                prev = self.mem.enemy_prev.get(tid)
                moving = prev is not None and prev != tpos
                if moving and tpos is not None:
                    exits = [n for n in ((tpos[0]+1, tpos[1]), (tpos[0]-1, tpos[1]),
                                         (tpos[0], tpos[1]+1), (tpos[0], tpos[1]-1))
                             if not self._is_obstacle(*n)]
                    if len(exits) <= 2 and self._dist(pos, tpos) <= 3:
                        # 目标被困：占最近可达出口（堵死），不射击
                        best_ex, best_d = None, None
                        for ex in exits:
                            if self._is_obstacle(*ex):
                                continue
                            occupied = any(tuple(u2["pos"]) == ex
                                           for u2 in obs.units)
                            if occupied:
                                continue
                            d = self._dist(pos, ex)
                            if best_ex is None or d < best_d:
                                best_ex, best_d = ex, d
                        if best_ex is not None and self._dist(pos, best_ex) > 0:
                            step = self.pf.next_step(pos, best_ex)
                            if step:
                                self._dbg(uid, "corner_block", best_ex)
                                return ("MOVE", {"direction": dir_name(
                                    step[0] - pos[0], step[1] - pos[1])})
            self._shot_claims[shot] = self._shot_claims.get(shot, 0) + 1
            self._dbg(uid, self._shot_modes.get(uid, "shoot"), shot)
            return ("SHOOT", {"expected_cell": list(shot)})
        # Limited counterattack: selected Rangers take a legal firing cell for
        # the visible attacker.  Everyone else keeps the five-cell home ring,
        # preventing one target from pulling the entire defense away.
        if defense and core_pos:
            if uid in self._siege_rangers \
                    and self._defense_point is not None:
                action = self._ranger_firing_position(
                    u, obs, self._defense_point, "siege_ranger")
                if action:
                    return action
            if self._dist(pos, core_pos) > 5:
                goal = self._core_approach_goal(pos, core_pos)
                if goal:
                    step = self.pf.next_step(pos, goal)
                    if step:
                        self._dbg(uid, "siege_ranger", core_pos)
                        return ("MOVE", {"direction": dir_name(
                            step[0] - pos[0], step[1] - pos[1])})
            self._dbg(uid, "siege_ranger", core_pos)
            return None
        if self._vault_active:
            return self._vault_hold(
                u, obs, core_pos, VAULT_RANGER_RADII, "vault_ranger")
        if uid in self._home_rangers:
            return self._vault_hold(
                u, obs, core_pos, HOME_RANGER_RADII, "home_ranger")
        # 记忆静止目标射击（v0.13 cell fire）：短暂视野缺失时射击确认静止的
        # 敌方 Core 的记忆格（Drew-Z：strike Ranger 打记忆静止格）
        if g["raid_stationary"] > 0.5 and core_pos:
            for key, st in self.mem.stationary.items():
                if not str(key).startswith("core_"):
                    continue
                if st["count"] < int(g["raid_min_obs"]):
                    continue
                if self._dist(st["pos"], core_pos) > g["raid_max_dist"]:
                    continue
                if self._shot_valid(u, pos, st["pos"]):
                    self._dbg(uid, "shoot_stationary", st["pos"])
                    return ("SHOOT", {"expected_cell": list(st["pos"])})
        # 突袭确认静止的敌方 Core（Drew-Z：strike group，守卫留守）
        if self._raid_point and u["uid"] not in self._raid_guards:
            d = self._dist(pos, self._raid_point)
            if d > 3:
                step = self.pf.next_step(pos, self._raid_point)
                if step:
                    self._dbg(uid, "raid", self._raid_point)
                    return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                           step[1] - pos[1])})
        # 巡逻 Ranger：Core 周围环形巡逻（防偷家）——有敌人时也留守
        # 巡逻（认领机制已限制出击兵力，巡逻单位不再"一股脑"扑目标）
        # 巡逻 Ranger：敌人很近（≤10 格）时参与追击（近单位追得上），
        # 远处敌人则留守巡逻（防偷家 + 不白跑）
        _patrol_join = False
        if uid in self._patrol_rangers and not defense:
            for e in obs.enemies:
                if e["utype"] == "WORKER":
                    continue
                if self._dist(pos, e["pos"]) <= 10:
                    _patrol_join = True
                    break
        if uid in self._patrol_rangers and not defense and not _patrol_join:
            r = int(g["patrol_radius"])
            idx = (obs.tick // 16 + self._uid_num(uid)) % 8
            angle = idx * math.pi / 4
            patrol = (core_pos[0] + int(round(r * math.cos(angle))),
                      core_pos[1] + int(round(r * math.sin(angle))))
            # 口袋感知：几何巡逻点可能跨墙（墙外/不可达），就近吸附到 Core
            # 口袋内可达格；口袋未知时保持原几何点（保守不变）。
            if self._core_pocket:
                if patrol not in self._core_pocket:
                    patrol = self._nearest_pocket_point(patrol)
                elif self._core_pocket_dist.get(patrol, 99) <= 1:
                    patrol = self._nearest_pocket_point(patrol)
            if patrol is None:
                return None
            if self._dist(pos, patrol) <= 1:
                return None
            step = self.pf.next_step(pos, patrol)
            if step:
                self._dbg(uid, "patrol", patrol)
                return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                       step[1] - pos[1])})
        # 跟踪敌人保持视线（带认领：可见上限 2 / 记忆上限 1，防全员扑同一目标）
        # 进攻计划激活且本单位未被分配 → 不追击(交由计划统一调度)。
        tracking, mem_key = (self._select_combat_target(pos, enemies,
                                                     enemy_cores, obs)
                              if not self._combat_clusters else (None, None))
        if tracking and mem_key is not None:
            if self._dist(pos, tracking) <= 1:
                # 已到达记忆位置且无敌人 → 立即遗忘（防追丢后横跳）
                self._forget_enemy(mem_key)
                self._pursuing.pop(uid, None)
                tracking = None
            else:
                self._pursuing[uid] = (mem_key, obs.tick)
        elif uid in self._pursuing:
            self._pursuing.pop(uid, None)
        if tracking:
            d = self._dist(pos, tracking)
            # Team vision can expose a target that this Ranger cannot see by
            # itself.  Use that spotter advantage to take a firing cell outside
            # every currently known enemy vision source, especially behind a
            # supercover corner that does not block the narrower shot ray.
            if d <= 6:
                action = self._ranger_firing_position(
                    u, obs, tracking, "ranger")
                if action:
                    return action
            if threat > g["army_trigger"] and d <= g["kite_range"]:
                away = self._away_from(pos, [tracking])
                step = self.pf.next_step(pos, away)
                if step:
                    self._dbg(uid, "kite", tracking)
                    return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                           step[1] - pos[1])})
            if d > g["kite_range"] and threat <= g["army_trigger"] * 1.5:
                step = self.pf.next_step(pos, tracking)
                if step:
                    self._dbg(uid, "track", tracking)
                    return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                           step[1] - pos[1])})
        # idle 侦察：Ranger 视野 5（比 worker 3 远）——非巡逻 Ranger 空闲
        # 时探索未访问区域（发现资源共享全队视野 → 缓解资源记忆萎缩）。
        # 限距自适应已探索范围：探索面积大（线上玩了几千 tick）→ 限距
        # 自动放宽（Core 周围早已探索完，固定限距会让探索永不触发）；
        # 模拟器短局面积小 → 近探索（保持模拟器高效行为）
        area_r = int((len(self.mem.area_seen) / 2) ** 0.5) if self.mem.area_seen else 10
        max_explore = min(60, max(int(g["idle_explore_radius"]), area_r + 10))
        # 方向扇区探索（视野不重合）：uid 模 8 定方向，从扇区锚点（12 格
        # 外）BFS 找最近未访问格——每个 Ranger 探索自己方向的区域，天然
        # 分散（去重竞争会让 Ranger 放弃探索，模拟器 -3~-5；扇区无竞争）
        sector_v = ((1, 0), (1, 1), (0, 1), (-1, 1),
                    (-1, 0), (-1, -1), (0, -1), (1, -1))
        # 扇区轮换（2026-08-07 修复探索停滞）：uid 固定方向扇区探索完
        # 后永不换方向 → 长局 t12000 探索死锁（area 停/mem_res 0/经济冻
        # 结）。自己方向无目标时偏移相邻扇区（±1..±3）继续探索。
        eg = self._explore_goal.get(uid)
        if eg is not None and obs.tick - eg[1] < COMBAT_EXPLORE_GOAL_TTL \
                and not self._is_obstacle(*eg[0]):
            # 粘性目标有效时直接沿用。旧流程每 Tick 先执行 8-32 次 BFS 与
            # 探索评分，随后才用 eg 覆盖结果；这些计算完全不参与决策。
            explore = eg[0]
        else:
            self._explore_goal.pop(uid, None)
            sector_idx = self._uid_num(uid) % 8
            best_t, best_v = None, -1
            # 已认领目标（其他单位粘性目标，防多 Ranger 挤同一探索点互堵）
            others_taken = {goal[0] for uu, goal in self._explore_goal.items()
                            if uu != uid and goal and goal[0]}
            for _off in (0, 1, -1, 2, -2, 3, -3, 4):
                sv = sector_v[(sector_idx + _off) % 8]
                for _k in (8, 12, 16, 20):
                    _anchor = (pos[0] + sv[0] * _k, pos[1] + sv[1] * _k)
                    _t = self._nearest_unvisited(_anchor, limit=max_explore)
                    if _t is None:
                        continue
                    if _t in others_taken:
                        continue
                    if core_pos and self._dist(_t, core_pos) > max_explore + 6:
                        continue
                    _v = self._explore_score(_t, 25)
                    if _v > best_v:
                        best_v, best_t = _v, _t
                if best_t is not None and _off != 0:
                    break   # 已在自己/相邻扇区找到目标
            explore = best_t
            # 探索名额限制（2026-08-07）：最多 MAX_EXPLORERS 个 Ranger 同时外出
            # 探索，其余守家。已有粘性目标的 Ranger 不在这里被打断。
            if explore is not None:
                exploring_now = sum(1 for uu in obs.units
                                    if uu["utype"] == "RANGER"
                                    and uu["uid"] in self._explore_goal)
                if exploring_now >= MAX_EXPLORERS:
                    explore = None
            if explore:
                self._explore_goal[uid] = (explore, obs.tick)
        if explore and self._dist(pos, explore) > 0:
            # 完整路径持久化：目标确定后 A* 算整条路径，沿路径走——
            # 消除每 tick next_step 的"两条等长路径第一步摇摆"（探索
            # Ranger 频繁 osc_break 的根源）
            path_data = self._explore_path.get(uid)
            if path_data is None or obs.tick - path_data[1] > 12 \
                    or (path_data[0] and path_data[0][-1] != explore) \
                    or (path_data[0] and self._dist(pos, path_data[0][0]) > 1):
                path = self.pf.find(pos, explore)
                self._explore_path[uid] = (path or [], obs.tick)
            path, _ = self._explore_path.get(uid, ([], obs.tick))
            # 只弹出已站上的格（==pos）——旧逻辑 _dist<=1 会把"下一步"
            # 也弹掉，导致每 tick 对 2 格外目标重算 A* 第一步 → 对称障碍
            # 区第一步摇摆 → 探索 Ranger 长时间振荡
            while path and tuple(path[0]) == tuple(pos):
                path.pop(0)
            if path:
                nxt = path[0]
                # 沿持久化路径走第一步（不重算）：只有被动态障碍/单位挡
                # 才清路径下 tick 重算——"稳定化"是振荡的解（振荡文档）
                if self._dist(pos, nxt) == 1 and not self._is_obstacle(*nxt) \
                        and not any(tuple(uu["pos"]) == nxt
                                    for uu in obs.units):
                    self._path_blocked.pop(uid, None)   # 推进成功：清零
                    self._dbg(uid, "explore", explore)
                    return ("MOVE", {"direction": dir_name(
                        nxt[0] - pos[0], nxt[1] - pos[1])})
                # 下一步被挡：清路径（动态挡不标不可达——下 tick 重算）。
                # 不能掉进 rally：目标还活跃时被 rally 反向拉走一步，下 tick
                # 重算又走回来 → 每 12 tick 一循环的拉锯（线上 13f41314/
                # 1aa17971 在 (-62,124) 口袋外围观察到的行为）。原地等 1
                # tick，下 tick 用新路径继续逼近。
                self._explore_path.pop(uid, None)
                return None
            else:
                self._explore_path.pop(uid, None)
                self._path_blocked.pop(uid, None)
                self.mem.mark_unreachable(explore)
                self._explore_goal.pop(uid, None)
        else:
            self._explore_goal.pop(uid, None)  # 到达（站在目标格上）/无目标：清粘性
            self._explore_path.pop(uid, None)
        # idle 巡逻：代替"原地待命"，空闲 Ranger 分到三个巡逻圈之一绕圈
        # 巡防（半径递增，敌人出现时由战斗分支接管）。单位持续移动而非在
        # Core 门口聚集等待。
        patrol = self._idle_patrol(u, obs, core_pos)
        if patrol:
            return patrol
        # idle 散开：叠格/堵 Core 门口时让位
        spread = self._idle_spread(u, pos, obs, core_pos, defense)
        if spread:
            return spread
        return None

    # ------------------------------------------------------------------
    # 敌人追踪 / 遗忘
    # ------------------------------------------------------------------
    def _nearest_enemy_pos(self, obs, core_pos):
        """最近敌人位置：可见优先，其次记忆（未遗忘）。

        注意：last_enemy_pos 的 key 含字符串，迭代必须排序保证确定性。
        敌方 Core 位置也会过期（对方会迁移/死亡）——遗忘周期取普通
        敌人的 CORE_FORGET_MULT 倍，避免兵力永远钉在旧位置。
        """
        best, bd = None, None
        for k in sorted(self.mem.last_enemy_pos, key=str):
            if k in self.mem.enemy_forgotten:
                continue
            p, tk = self.mem.last_enemy_pos[k]
            ttl = self.genes["forget_ticks"] * (
                1 if not str(k).startswith("core_") else CORE_FORGET_MULT)
            if obs.tick - tk > ttl:
                self.mem.enemy_forgotten.add(k)
                continue
            d = self._dist(p, core_pos)
            if bd is None or d < bd:
                bd, best = d, p
        return best

    def _nearest_enemy_memory_kv(self, pos, obs):
        """最近未遗忘敌人记忆 (pos, key)；无则 (None, None)。"""
        best, bd, bk = None, None, None
        for k in sorted(self.mem.last_enemy_pos, key=str):
            if k in self.mem.enemy_forgotten:
                continue
            p, tk = self.mem.last_enemy_pos[k]
            ttl = self.genes["forget_ticks"] * (
                1 if not str(k).startswith("core_") else CORE_FORGET_MULT)
            if obs.tick - tk > ttl:
                self.mem.enemy_forgotten.add(k)
                continue
            d = self._dist(pos, p)
            if bd is None or d < bd:
                bd, best, bk = d, p, k
        return (best, bk) if best is not None else (None, None)

    def _forget_enemy(self, key):
        """立即遗忘敌人记忆（追丢/到达确认无敌人，防反复横跳）。"""
        self.mem.enemy_forgotten.add(key)
        self.mem.last_enemy_pos.pop(key, None)
        self.mem.enemy_prev.pop(key, None)

    def _predict_pos(self, uid, pos, obs, k=2):
        """预测敌人 k 步后的位置（用 enemy_prev 推出速度方向；只在持续
        单步移动时预测，否则返回原位）。"""
        prev = self.mem.enemy_prev.get(uid)
        if prev is None or self.mem.enemy_dir_streak.get(uid, 0) < 2:
            return pos
        dx = pos[0] - prev[0]
        dy = pos[1] - prev[1]
        if abs(dx) + abs(dy) != 1:
            return pos
        px, py = pos[0] + dx * k, pos[1] + dy * k
        if self.bounds is not None:
            x_lo, x_hi, y_lo, y_hi = self.bounds
            if not (x_lo <= px <= x_hi and y_lo <= py <= y_hi):
                return pos
        if self._is_obstacle(px, py):
            return pos
        return (px, py)

    def _intercept_point(self, pos, enemy_pos, enemy_dir):
        """拦截点：找预测路径上'我比敌人更早或同时到'的格（抄近道截住
        直线逃跑者）；找不到就回退到敌方当前位置（同速追击）。"""
        ex, ey = enemy_pos
        for k in (1, 2, 3):
            ip = (ex + enemy_dir[0] * k, ey + enemy_dir[1] * k)
            if self.bounds is not None:
                x_lo, x_hi, y_lo, y_hi = self.bounds
                if not (x_lo <= ip[0] <= x_hi and y_lo <= ip[1] <= y_hi):
                    continue
            if self._is_obstacle(*ip):
                continue
            my_d = self._dist(pos, ip)      # 曼哈顿近似（同速场景下够用）
            en_d = k                          # 敌人到拦截点还需 k 步
            if my_d <= en_d + 1:
                return ip
        return enemy_pos

    def _select_combat_target(self, pos, enemies, enemy_cores, obs):
        """带认领的战斗目标选择：可见敌人（认领上限 2，近战+远程围攻合理）
        与记忆敌人（认领上限 1，单点确认防全员扑旧位置）混合；
        认领数少的优先，其次距离近（多目标分散）。返回 (target_pos, mem_key)。

        追击增强：
        - 拦截点预测：对持续直线移动的敌人，追其 2 步后的位置（抄近道截住）
        - 侧翼围堵：认领同一目标的第 2 个单位走敌人侧面（堵横向逃跑），
          第 1 个咬住尾巴——两个 Vanguard 形成夹击。"""
        cands = []
        for p, _hp, _ut, uid in enemies:
            if _ut != "WORKER" or self._combat_taken.get(tuple(p), 0) > 0:
                # 战斗单位追当前位置；worker 追预测拦截点（同速追不上直线跑）
                cands.append((tuple(p), None))
            else:
                pp = self._predict_pos(uid, tuple(p), obs, k=2)
                if pp != tuple(p):
                    cands.append((pp, None))
                cands.append((tuple(p), None))
        for p, _hp, _owner in enemy_cores:
            cands.append((tuple(p), None))
        mpos, mkey = self._nearest_enemy_memory_kv(pos, obs)
        if mpos is not None:
            cands.append((mpos, mkey))
        if not cands:
            return (None, None)
        best, bk, bd = None, None, None
        for p, k in cands:
            n = self._combat_taken.get(p, 0)
            d = self._dist(pos, p)
            # 距离门槛：>12 格不认领——同速追不上，远的单位跑过去敌人早
            # 跑出视野（实测：远处 Ranger 追 2 步敌人就没影了，白跑且
            # Core 空虚）。近的单位（≤12 格）才追。
            if d > 12:
                continue
            if best is None or (n, d) < (self._combat_taken.get(best, 0), bd):
                best, bk, bd = p, k, d
        is_worker = any(_ut == "WORKER" for _ut in (u[2] for u in enemies))
        if bk is not None:
            limit = 1                      # 记忆目标：单点确认
        else:
            limit = 2 if is_worker else 2  # 咬尾+侧翼最小夹击；上限 2 防 Core 空虚
        if self._combat_taken.get(best, 0) >= limit:
            return (None, None)
        order = self._combat_taken.get(best, 0) + 1
        self._combat_taken[best] = order
        # 侧翼围堵：第 2/3 个认领者走向目标垂直方向 ±2 格（堵横向逃跑，
        # 与咬尾者形成夹击——同速追击下唯一能缩短距离的途径）
        if order >= 2 and bk is None:
            uid2 = None
            for p, _hp, _ut, uid in enemies:
                if tuple(p) == best or self._predict_pos(uid, tuple(p), obs, 2) == best:
                    uid2 = uid
                    break
            if uid2 is not None:
                prev = self.mem.enemy_prev.get(uid2)
                if prev is not None:
                    dx = best[0] - prev[0]
                    dy = best[1] - prev[1]
                    if abs(dx) + abs(dy) == 1:
                        sgn = 1 if order == 2 else -1   # 2 号左翼、3 号右翼
                        flank = (best[0] - dy * sgn * 2,
                                 best[1] + dx * sgn * 2)
                        ok = True
                        if self._is_obstacle(*flank):
                            ok = False
                        if self.bounds is not None:
                            x_lo, x_hi, y_lo, y_hi = self.bounds
                            if not (x_lo <= flank[0] <= x_hi
                                    and y_lo <= flank[1] <= y_hi):
                                ok = False
                        if ok:
                            return (flank, bk)
        return (best, bk)

    def _nearest_enemy_memory(self, pos, obs):
        """最近未遗忘敌人记忆位置（用于追踪/追击）。"""
        best, bd = None, None
        for k in sorted(self.mem.last_enemy_pos, key=str):
            if k in self.mem.enemy_forgotten:
                continue
            p, tk = self.mem.last_enemy_pos[k]
            ttl = self.genes["forget_ticks"] * (
                1 if not str(k).startswith("core_") else CORE_FORGET_MULT)
            if obs.tick - tk > ttl:
                self.mem.enemy_forgotten.add(k)
                continue
            d = self._dist(pos, p)
            if bd is None or d < bd:
                bd, best = d, p
        return best

    def _pick_attack_point(self, obs, core_pos, visible_enemies, visible_cores):
        """进攻目标：敌人 Core 优先；可见敌人优先于记忆。遗忘的敌人不再追。"""
        g = self.genes
        if not core_pos:
            return None
        if visible_cores and g["attack_core_first"] > 0.5:
            return min((c[0] for c in visible_cores),
                       key=lambda p: self._dist(p, core_pos))
        # 最近敌人（可见或记忆）
        best, bd = None, None
        for p, _hp, _ut, _uid in visible_enemies:
            d = self._dist(p, core_pos)
            if bd is None or d < bd:
                bd, best = d, p
        for k in sorted(self.mem.last_enemy_pos, key=str):
            if k in self.mem.enemy_forgotten:
                continue
            p, tk = self.mem.last_enemy_pos[k]
            ttl = g["forget_ticks"] * (
                1 if not str(k).startswith("core_") else CORE_FORGET_MULT)
            if obs.tick - tk > ttl:
                self.mem.enemy_forgotten.add(k)
                continue
            d = self._dist(p, core_pos)
            if bd is None or d < bd:
                bd, best = d, p
        # 兵力不占优时不主动出击
        if best is None:
            return None
        return best

    # ------------------------------------------------------------------
    # 全局进攻计划：按敌方人数2倍精确调配 + 包围式进攻
    # ------------------------------------------------------------------
    def _plan_combat(self, obs, core_pos, threat):
        """每 Tick 构建一次全局进攻计划（仅在进攻模式调用）。

        规则（用户要求）：
        - 把视野内敌方战斗单位按距离聚类成簇；
        - 每簇 N 个敌人 → 派遣 2N 个我方战斗单位（优先 1先锋+1游侠配对，
          二打一），不出现多打一人挤人的情况；
        - 2N 个单位先机动到簇周围「上下左右」包围环上的槽位；
        - 只有聚齐(2N 全部到位)才正式发起进攻(SWEEP/SHOOT)，未聚齐只包围
          不接战；兵力不足 2N 倍 → 该簇不接战(不器械少打多)。
        """
        self._combat_clusters = []
        self._combat_assigned = {}
        pos_by_uid = {u["uid"]: tuple(u["pos"]) for u in obs.units}
        # 全局劣势/无敌人：清空已交战簇，退回守家
        enemies = [e for e in getattr(obs, "enemies", [])
                   if e["utype"] in ("VANGUARD", "RANGER")]
        if core_pos is None or threat > self.genes["army_trigger"] * 1.5 \
                or not enemies:
            self._engaged_clusters = set()
            return

        # ---- 聚类：距离阈值内归并 ----
        CLUSTER_DIST = 9
        pts = [(tuple(e["pos"]), e["uid"], e["utype"]) for e in enemies]
        parent = list(range(len(pts)))

        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                if self._dist(pts[i][0], pts[j][0]) <= CLUSTER_DIST:
                    ra, rb = _find(i), _find(j)
                    if ra != rb:
                        parent[ra] = rb
        groups = {}
        for i in range(len(pts)):
            groups.setdefault(_find(i), []).append(pts[i])

        clusters = []
        for members in groups.values():
            E = len(members)
            cx = sum(m[0][0] for m in members) / E
            cy = sum(m[0][1] for m in members) / E
            center = (round(cx), round(cy))
            maxd = max(self._dist(m[0], center) for m in members)
            sr = max(3, int(maxd) + 2)          # 包围外环半径
            clusters.append({
                "key": frozenset(m[1] for m in members),
                "center": center, "enemies": members, "E": E,
                "required": 2 * E, "slots": [], "engaged": False,
                "skip": False,
            })

        # ---- 候选我方战斗单位（排除守家 reserved）----
        cand = [u for u in obs.units
                if u["utype"] in ("VANGUARD", "RANGER")
                and u["uid"] not in self._home_guards]
        if not cand:
            self._engaged_clusters = set()
            return

        used = set()
        for c in clusters:
            E = c["E"]
            required = c["required"]
            avail = sorted(
                (u for u in cand if u["uid"] not in used),
                key=lambda u: self._dist(u["pos"], c["center"]))
            if len(avail) < required:
                c["skip"] = True          # 兵力不足 2 倍 → 不接战
                continue
            chosen = avail[:required]
            for u in chosen:
                used.add(u["uid"])
            # 包围槽位：required 个点在 center 周围半径 sr 的环上均匀分布
            # （含上下左右等方向 → 形成包围）
            slots = []
            for k in range(required):
                ang = k * 2 * math.pi / required
                ox = round(sr * math.cos(ang))
                oy = round(sr * math.sin(ang))
                slots.append((c["center"][0] + ox, c["center"][1] + oy))
            c["slots"] = slots
            # 配对：每敌方单位配 (1 先锋 + 1 游侠)；不足补其他兵种(仍2人)
            vangs = [u["uid"] for u in chosen if u["utype"] == "VANGUARD"]
            rangs = [u["uid"] for u in chosen if u["utype"] == "RANGER"]
            others = [u["uid"] for u in chosen
                      if u["utype"] not in ("VANGUARD", "RANGER")]
            vi = ri = oi = 0
            # 槽位按 uid 稳定分配(不按距离排名)，避免单位收拢时槽位被重排
            # 导致互相 chase 永远聚不齐。给定簇稳定时同一单位恒占同一槽位。
            chosen_stable = sorted(chosen, key=lambda u: self._uid_num(u["uid"]))
            slot_by_uid = {u["uid"]: slots[k]
                           for k, u in enumerate(chosen_stable)}
            for m in members:                      # 每个敌人配 2 人
                a1 = a2 = None
                if vi < len(vangs):
                    a1 = vangs[vi]; vi += 1
                elif oi < len(others):
                    a1 = others[oi]; oi += 1
                elif ri < len(rangs):
                    a1 = rangs[ri]; ri += 1
                if ri < len(rangs):
                    a2 = rangs[ri]; ri += 1
                elif oi < len(others):
                    a2 = others[oi]; oi += 1
                elif vi < len(vangs):
                    a2 = vangs[vi]; vi += 1
                for uid in (a1, a2):
                    if uid is None:
                        continue
                    self._combat_assigned[uid] = {
                        "ci": None, "slot": slot_by_uid[uid],
                        "enemy": tuple(m[0]),
                    }
            c["assigned_uids"] = [u["uid"] for u in chosen]

        # 记录 ci + 反查 slot 所属簇，便于到位判定
        slot_to_ci = {}
        for ci, c in enumerate(clusters):
            c["ci"] = ci
            for s in c["slots"]:
                slot_to_ci[s] = ci
        for uid, a in self._combat_assigned.items():
            a["ci"] = slot_to_ci.get(a["slot"])

        # ---- 聚齐判定：2N 全部到位(≤1) → 正式进攻（一旦聚齐即锁存）----
        live_assigned = set(pos_by_uid) & set(self._combat_assigned)
        for c in clusters:
            if c.get("skip"):
                self._engaged_clusters.discard(c["key"])
                continue
            arrived = sum(
                1 for uid in c.get("assigned_uids", [])
                if uid in live_assigned
                and self._dist(pos_by_uid[uid],
                               self._combat_assigned[uid]["slot"]) <= 1)
            if c["key"] in self._engaged_clusters:
                c["engaged"] = True            # 已锁存：继续进攻
            elif arrived >= c["required"]:
                c["engaged"] = True
                self._engaged_clusters.add(c["key"])
            else:
                c["engaged"] = False
                self._engaged_clusters.discard(c["key"])
            # 分配单位全灭 → 解除锁存
            if not any(uid in live_assigned
                       for uid in c.get("assigned_uids", [])):
                self._engaged_clusters.discard(c["key"])

        self._combat_clusters = clusters
        # 进攻目标点(兼容 block/raid 旧逻辑) = 最近未跳过簇中心
        active = [c["center"] for c in clusters if not c.get("skip")]
        if active:
            self._attack_point = min(
                active, key=lambda p: self._dist(p, core_pos))

    # ------------------------------------------------------------------
    # 友军受援：附近队友低血/被攻击时，健康单位优先掩护
    # ------------------------------------------------------------------
    SUPPORT_DIST = 12          # 支援搜索半径（距受援队友的距离）
    SUPPORT_ENEMY_DIST = 8     # 接敌距离（支援单位看到威胁敌人后直接攻击）

    def _plan_support(self, obs, core_pos):
        """检测需要支援的队友，指派附近健康单位去掩护。

        触发条件（任一满足即标记为需支援）：
        1. HP ≤ flee_hp × max_hp（低血撤退中）
        2. 上一 tick 受到攻击事件（CORE_DAMAGED / UNIT_DAMAGED）

        指派规则：
        - 守家预留单位不参与支援
        - 已被进攻计划分配的单位不重复指派
        - 每个受援队友最多分配 3 个支援者
        - 支援者优先走向威胁敌人位置（而非队友位置——去接敌不是去送死）
        """
        self._support_targets.clear()
        self._supporting.clear()

        if not obs.units or not core_pos:
            return

        g = self.genes
        flee_ratio = g["flee_hp"]  # 默认 0.35

        # ---- Step 1: 找出所有需要支援的队友 ----
        threatened = []  # (unit, reason, enemy_pos_hint)
        for u in obs.units:
            if u["utype"] not in ("VANGUARD", "RANGER"):
                continue
            uid = u["uid"]
            pos = tuple(u["pos"])
            hp = u["hp"]
            max_hp = 4 if u["utype"] == "VANGUARD" else 2
            retreat_at = max(1, math.ceil(flee_ratio * max_hp))

            ally_needs_help = False
            reason = ""
            enemy_hint = None

            # 条件1: 低血
            if hp <= retreat_at:
                ally_needs_help = True
                reason = "low_hp"
            # 条件2: 上一 tick 被攻击
            if not ally_needs_help and obs.prev_events:
                for ev in obs.prev_events:
                    if ev.get("type") in ("UNIT_DAMAGED", "CORE_DAMAGED") \
                            and ev.get("obj_id") == uid:
                        ally_needs_help = True
                        reason = "attacked"
                        # 从事件中提取攻击者位置（如果有）
                        ep = ev.get("source_pos")
                        if ep:
                            enemy_hint = tuple(ep)
                        break

            if ally_needs_help:
                # 如果没有明确的敌人位置提示，找视野内最近的敌方战斗单位
                if enemy_hint is None:
                    nearest_e = None
                    nearest_d = 999
                    for e in obs.enemies:
                        if e["utype"] in ("VANGUARD", "RANGER"):
                            d = self._dist(pos, tuple(e["pos"]))
                            if d < nearest_d:
                                nearest_d = d
                                nearest_e = tuple(e["pos"])
                    if nearest_e and nearest_d <= 10:
                        enemy_hint = nearest_e

                threatened.append((u, reason, enemy_hint))

        if not threatened:
            return

        # ---- Step 2: 为每个受援队友分配支援者 ----
        used = set()
        home = self._home_guards | set(self._combat_assigned.keys())
        # 已被进攻计划分配的也不重复用（避免一个单位同时执行两个任务）
        combat_assigned_uids = set(self._combat_assigned.keys())

        for ally, reason, enemy_hint in threatened:
            ally_pos = tuple(ally["pos"])
            ally_uid = ally["uid"]

            # 候选支援者：健康战斗单位，非守家、非已分配、非自己
            candidates = []
            for u in obs.units:
                uid = u["uid"]
                if uid in used or uid in home or uid in combat_assigned_uids:
                    continue
                if uid == ally_uid:
                    continue
                if u["utype"] not in ("VANGUARD", "RANGER"):
                    continue
                upos = tuple(u["pos"])
                uhp = u["hp"]
                umax = 4 if u["utype"] == "VANGUARD" else 2
                # 支援者也必须是健康的（>50% HP），否则自己也得撤退
                if uhp <= umax * 0.5:
                    continue
                d = self._dist(upos, ally_pos)
                if d <= self.SUPPORT_DIST:
                    candidates.append((u, d))

            # 按距离排序，最多分配 3 个
            candidates.sort(key=lambda x: x[1])
            support_count = min(3, len(candidates))
            for i in range(support_count):
                supporter, _d = candidates[i]
                sup_uid = supporter["uid"]
                used.add(sup_uid)
                self._supporting.add(sup_uid)
                # 支援目标：有明确敌人位置 → 去接敌；否则 → 去队友身边
                target = enemy_hint if enemy_hint else ally_pos
                self._support_targets[sup_uid] = (ally_uid, target)

    def _support_action(self, u, obs, core_pos):
        """返回支援行动；未分配支援则返回 None。"""
        a = self._support_targets.get(u["uid"])
        if a is None:
            return None
        ally_uid, target = a
        pos = tuple(u["pos"])

        # 已到达目标附近(≤2格) → 尝试攻击可见敌人或待命掩护
        if self._dist(pos, target) <= 2:
            # 目标是敌人位置：尝试 SWEEP / SHOOT
            enemies = [e for e in getattr(obs, "enemies", [])
                       if e["utype"] in ("VANGUARD", "RANGER")]
            enemy_cores = [(c["pos"], c.get("hp", 5), c.get("owner", "?"))
                           for c in getattr(obs, "enemy_cores", [])]

            if u["utype"] == "VANGUARD":
                # 找最近的可见敌人
                nearest_e = None
                nearest_d = 999
                for e in enemies:
                    d = self._dist(pos, tuple(e["pos"]))
                    if d < nearest_d:
                        nearest_d = d
                        nearest_e = e
                if nearest_e and nearest_d == 1:
                    ep = tuple(nearest_e["pos"])
                    self._dbg(u["uid"], "support_sweep", ep)
                    return ("SWEEP", {"direction": dir_name(
                        ep[0] - pos[0], ep[1] - pos[1])})
                if nearest_e and nearest_d > 1:
                    step = self.pf.next_step(pos, tuple(nearest_e["pos"]))
                    if step:
                        self._dbg(u["uid"], "support_approach", tuple(nearest_e["pos"]))
                        return ("MOVE", {"direction": dir_name(
                            step[0] - pos[0], step[1] - pos[1])})
                # 到位但无可见敌人 → 待命掩护
                self._dbg(u["uid"], "support_cover", target)
                return None

            # RANGER: 尝试射击
            for e in enemies + enemy_cores:
                ep = e["pos"] if isinstance(e, dict) else e[0]
                if self._shot_valid(u, pos, tuple(ep)):
                    self._dbg(u["uid"], "support_shoot", tuple(ep))
                    return ("SHOOT", {"expected_cell": list(ep)})
            # 无射击目标 → 移动向目标
            step = self.pf.next_step(pos, target)
            if step:
                self._dbg(u["uid"], "support_move_ranger", target)
                return ("MOVE", {"direction": dir_name(
                    step[0] - pos[0], step[1] - pos[1])})
            self._dbg(u["uid"], "support_cover", target)
            return None

        # 未到达 → 移向目标
        step = self.pf.next_step(pos, target)
        if step:
            self._dbg(u["uid"], "support_reinforce", target)
            return ("MOVE", {"direction": dir_name(
                step[0] - pos[0], step[1] - pos[1])})
        self._dbg(u["uid"], "support_stuck", target)
        return None

    def _combat_plan_action(self, u, obs, core_pos, enemies, enemy_cores, role):
        """被分配到进攻计划的单位：返回其本 Tick 行动；未分配返回 None。"""
        a = self._combat_assigned.get(u["uid"])
        if a is None:
            return None
        pos = tuple(u["pos"])
        # 防御/劣势下计划已清空，走兜底逻辑
        if not self._combat_clusters:
            return None
        cluster = self._combat_clusters[a["ci"]] if a["ci"] is not None \
            and a["ci"] < len(self._combat_clusters) else None
        if cluster is None:
            return None
        slot = a["slot"]
        ep = a["enemy"]
        if not cluster["engaged"]:
            # 机动包围：走向分配槽位；到位则待命(不提前进攻)
            if self._dist(pos, slot) <= 1:
                return None
            step = self.pf.next_step(pos, slot)
            if step:
                return ("MOVE", {"direction": dir_name(
                    step[0] - pos[0], step[1] - pos[1])})
            return None
        # 正式进攻：打分配到的敌方单位（二打一）
        if role == "VANGUARD":
            if self._dist(pos, ep) == 1:
                return ("SWEEP", {"direction": dir_name(
                    ep[0] - pos[0], ep[1] - pos[1])})
            step = self.pf.next_step(pos, ep)
            if step:
                return ("MOVE", {"direction": dir_name(
                    step[0] - pos[0], step[1] - pos[1])})
            return None
        # RANGER
        if self._shot_valid(u, pos, ep):
            return ("SHOOT", {"expected_cell": list(ep)})
        step = self.pf.next_step(pos, ep)
        if step:
            return ("MOVE", {"direction": dir_name(
                step[0] - pos[0], step[1] - pos[1])})
        return None

    def _block_point(self, enemy_pos, core_pos):
        """堵路点：敌人与 Core 连线上、距敌人 2 格的格。"""
        ex, ey = enemy_pos
        cx, cy = core_pos
        dx = cx - ex
        dy = cy - ey
        if dx == 0 and dy == 0:
            return None
        # 归一化到 2 格
        adx, ady = abs(dx), abs(dy)
        step_x = 1 if dx > 0 else -1
        step_y = 1 if dy > 0 else -1
        # 先走主方向 2 步（向 core）
        px = ex + step_x * min(2, adx)
        py = ey + step_y * min(2, ady)
        return (px, py)

    # ------------------------------------------------------------------
    # 区域回访
    # ------------------------------------------------------------------
    def _maybe_pick_revisit(self, obs, core_pos):
        g = self.genes
        if core_pos is None:
            self._revisit_goal = None
            return
        if self._revisit_goal is not None:
            return  # 目标未完成，沿用
        if obs.tick - self._revisit_since < g["revisit_ticks"]:
            return
        # 找"距 Core 12 格内、最久未看"的区域格（收紧：回访只是顺路看一眼
        # 旧区域，跑太远=横跨地图浪费移动；侦察/补给回访已覆盖更远环带）
        best, best_age = None, -1
        for (x, y), tk in self.mem.area_seen.items():
            if self._dist((x, y), core_pos) > 12:
                continue
            if (x, y) in self.mem.obstacles:
                continue
            if self.mem.is_unreachable((x, y)):
                continue
            age = obs.tick - tk
            if age > best_age:
                best_age = age
                best = (x, y)
        if best is not None and best_age > g["revisit_ticks"]:
            self._revisit_goal = best
            self._revisit_since = obs.tick
        else:
            self._revisit_since = obs.tick  # 没有可回访的，延后

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _follow_path(self, uid, pos, obs):
        """沿持久化完整路径走一步；返回 MOVE 动作，路径失效返回 None（需重算）。"""
        ent = self._worker_path.get(uid)
        if ent is None or obs.tick - ent[1] >= 40:
            return None
        plist = ent[0]
        try:
            idx = plist.index(pos)
        except ValueError:
            return None  # 被推离路径 → 重算
        if idx + 1 >= len(plist):
            del self._worker_path[uid]
            return None
        nxt = plist[idx + 1]
        if self._is_obstacle(*nxt) or nxt in self.mem.temp_blocked:
            return None  # 路径失效 → 重算
        return ("MOVE", {"direction": dir_name(nxt[0] - pos[0], nxt[1] - pos[1])})

    def _full_worker_push(self, pos, core_pos, obs, uid):
        """满载 Worker 回家兜底：穿越"满格假墙"向 Core 推进。

        口袋/单格走廊地形里（如当前洞口 (-59,121)/(-58,122) 被 2 个满载
        占满），普通 A* 把所有满格当硬障碍 → 唯一通路被截断 → 满载 Worker
        全体 wait。这里用无视满格的 Pathfinder 找一条仅按地形障碍的路到
        Core 四邻，引擎的 leaving/2 环交换会解开互锁；A* 仍失败时退化为
        向 Core 方向贪心一格（允许满格，但不进战斗单位驻守格/敌格/Core 格）。
        """
        goal, best_d = None, None
        cx, cy = core_pos
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (cx + dx, cy + dy)
            if self._is_terrain_obstacle(*c):
                continue
            dd = abs(pos[0] - c[0]) + abs(pos[1] - c[1])
            if best_d is None or dd < best_d:
                best_d, goal = dd, c
        if goal is not None:
            step = self._pf_ignore_full().next_step(pos, goal)
            if step:
                self._dbg(uid, "push_core", goal)
                return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                       step[1] - pos[1])})
        # 贪心向 Core 一格：优先空格、其次满格（按距离/方向打破平局）
        occ = {}
        for uu in obs.units:
            occ.setdefault(tuple(uu["pos"]), []).append(uu["utype"])
        enemy_cells = {tuple(e["pos"]) for e in obs.enemies}
        cur_d = abs(pos[0] - core_pos[0]) + abs(pos[1] - core_pos[1])
        best, best_key = None, None
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cand = (pos[0] + dx, pos[1] + dy)
            if cand == tuple(core_pos):
                continue
            if cand in enemy_cells:
                continue
            if self._is_terrain_obstacle(*cand):
                continue
            nd = abs(cand[0] - core_pos[0]) + abs(cand[1] - core_pos[1])
            if nd >= cur_d:
                continue
            types = occ.get(cand, ())
            if types and any(t != "WORKER" for t in types):
                continue
            key = (len(types) >= 2, nd, dx, dy)
            if best_key is None or key < best_key:
                best_key, best = key, cand
        if best is not None:
            self._dbg(uid, "push_greedy", best)
            return ("MOVE", {"direction": dir_name(best[0] - pos[0],
                                                   best[1] - pos[1])})
        return None

    def _move_toward(self, pos, goal):
        """返回向静态目标 goal 的 MOVE 动作；A* 确认不可达则标记并返回 None。
        目标格满格由 Pathfinder.goal_ok 豁免（可达，排队等待）；
        真不可达才标记 unreachable。"""
        goal = tuple(goal)
        step = self.pf.next_step(pos, goal)
        if step is None:
            self.mem.mark_unreachable(goal)
            return None
        return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                               step[1] - pos[1])})

    def _low_hp_retreat(self, u, obs, core_pos, max_hp):
        """Return ``(active, action)`` for the emergency retreat state."""
        retreat_at = max(1, math.ceil(self.genes["flee_hp"] * max_hp))
        if core_pos is None or u["hp"] >= max_hp or u["hp"] > retreat_at:
            return False, None

        uid = u["uid"]
        pos = tuple(u["pos"])
        self._move_backoff.pop(uid, None)
        self._osc_wait.pop(uid, None)
        self._osc_tried.pop(uid, None)
        self._dbg(uid, "retreat_low_hp", core_pos)

        can_heal = bool(obs.core and 0 < obs.core["resources"] < RESOURCE_GOAL)
        if pos == core_pos:
            if can_heal:
                return True, ("HEAL", {})
            return True, self._yield_core(pos, obs, uid)

        distance = self._dist(pos, core_pos)
        if distance == 1:
            if can_heal and self._projected_occupancy(core_pos, obs) < 2:
                dx, dy = core_pos[0] - pos[0], core_pos[1] - pos[1]
                return True, ("MOVE", {"direction": dir_name(dx, dy)})
            return True, None

        enemy_cells = {tuple(enemy["pos"]) for enemy in obs.enemies}
        choices = []
        for order, (dx, dy) in enumerate(((1, 0), (-1, 0), (0, 1), (0, -1))):
            candidate = (pos[0] + dx, pos[1] + dy)
            if self._is_terrain_obstacle(*candidate) \
                    or candidate in enemy_cells \
                    or self._projected_occupancy(candidate, obs) >= 2:
                continue
            choices.append((self._dist(candidate, core_pos),
                            self._projected_occupancy(candidate, obs),
                            order, candidate))

        improving = [choice for choice in choices if choice[0] < distance]
        if improving:
            step = min(improving)[-1]
            return True, ("MOVE", {"direction": dir_name(
                step[0] - pos[0], step[1] - pos[1])})

        goal = self._core_approach_goal(pos, core_pos)
        if goal:
            step = self.pf.next_step(pos, goal)
            if step:
                step = tuple(step)
                if self._projected_occupancy(step, obs) < 2:
                    return True, ("MOVE", {"direction": dir_name(
                        step[0] - pos[0], step[1] - pos[1])})

        # Pathfinder treats a currently full cell as a hard obstacle, even if
        # an earlier Unit in this Tick has already been ordered to leave it.
        # Near Core that made wounded Units reverse into the attack line.  Use
        # projected end-of-Tick occupancy for the immediate emergency step.
        if choices:
            step = min(choices)[-1]
            return True, ("MOVE", {"direction": dir_name(
                step[0] - pos[0], step[1] - pos[1])})
        return True, None

    def _projected_occupancy(self, cell, obs):
        """End-of-Tick friendly occupancy from decisions already made."""
        cell = tuple(cell)
        current = sum(1 for ally in obs.units
                      if tuple(ally["pos"]) == cell)
        if obs.core is not None and tuple(obs.core["pos"]) == cell:
            current += 1
        return (current - self._planned_departures.get(cell, 0)
                + self._planned_arrivals.get(cell, 0))

    def _dbg(self, uid, tag, goal=None):
        """记录单位本 Tick 命中的策略分支（供监控面板显示）。"""
        self._decisions[uid] = {
            "tag": tag,
            "goal": [int(goal[0]), int(goal[1])] if goal else None,
        }

    def _pick_raid_target(self, obs, core_pos):
        """选突袭目标：记忆里确认静止的敌方 Core（连续观察≥raid_min_obs、
        距我方 Core ≤raid_max_dist、距当前视野无活跃敌舰队）。
        返回 (pos, key) 或 None。"""
        g = self.genes
        best, bd = None, None
        for key, st in self.mem.stationary.items():
            if not str(key).startswith("core_"):
                continue
            if st["count"] < int(g["raid_min_obs"]):
                continue
            d = self._dist(st["pos"], core_pos)
            if d > g["raid_max_dist"]:
                continue
            # 目标已移走（位置与最近观察不符）→ 跳过
            rec = self.mem.last_enemy_pos.get(key)
            if rec is not None and rec[0] != st["pos"]:
                continue
            if bd is None or d < bd:
                bd, best = d, st["pos"]
        return best

    def _raid_target(self, obs):
        """当前突袭目标（策略内部状态）。"""
        return self._raid_point

    def _pick_target(self, u, pos, enemies, enemy_cores):
        g = self.genes
        cands = list(enemies) + list(enemy_cores)
        if not cands:
            return None
        if enemy_cores and g["attack_core_first"] > 0.5:
            return min((c[0] for c in enemy_cores),
                       key=lambda p: self._dist(pos, p))
        return min((c[0] for c in cands), key=lambda p: self._dist(pos, p))

    def _best_shot(self, u, pos, enemies, enemy_cores):
        """Assign current, lead, and escape cells across available Rangers.

        Movement resolves before combat, so firing every Ranger at the visible
        cell is trivially dodged.  Each target contributes ordered shot slots:
        enough current-cell fire for a stationary kill, then cardinal exits;
        a target that moved last Tick gets one lead and one current-cell shot
        before its other exits.  Additional damage is stacked only after this
        first coverage layer has been claimed.
        """
        g = self.genes
        cands = list(enemies)
        cores = [(c[0], c[1], "CORE", c[2]) for c in enemy_cores]
        cands = cores + cands
        slots = {}
        primary_cells = {}

        def add_slots(cell, priorities, target_rank, hp, uid, mode):
            cell = tuple(cell)
            if self._is_terrain_obstacle(*cell):
                return
            mode_rank = {"shoot_lead": 0, "shoot": 1,
                         "shoot_bracket": 2}[mode]
            uid_key = str(uid)
            if mode != "shoot_bracket":
                primary_cells.setdefault(uid_key, set()).add(cell)
            bucket = slots.setdefault(cell, [])
            for priority in priorities:
                bucket.append((priority, target_rank, hp, mode_rank,
                               uid_key, mode))

        for raw_pos, raw_hp, utype, uid in cands:
            target = tuple(raw_pos)
            hp = max(1, int(raw_hp))
            target_rank = (0 if utype == "CORE"
                           and g["attack_core_first"] > 0.5 else 1)
            if utype == "CORE":
                add_slots(target, [0] * hp, target_rank, hp, uid, "shoot")
                continue

            prev = self.mem.enemy_prev.get(uid)
            moving = False
            direction = None
            if prev is not None and tuple(prev) != target:
                direction = (target[0] - prev[0], target[1] - prev[1])
                moving = abs(direction[0]) + abs(direction[1]) == 1

            if moving:
                lead = (target[0] + direction[0],
                        target[1] + direction[1])
                add_slots(lead, [0] + [2] * (hp - 1),
                          target_rank, hp, uid, "shoot_lead")
                add_slots(target, [0] + [2] * (hp - 1),
                          target_rank, hp, uid, "shoot")
            else:
                # Two committed shots still kill a stationary 2-HP Ranger;
                # higher-HP targets get their remaining stack after exits.
                committed = min(hp, 2)
                add_slots(target, [0] * committed + [2] * (hp - committed),
                          target_rank, hp, uid, "shoot")

            exits = ((target[0] + 1, target[1]),
                     (target[0] - 1, target[1]),
                     (target[0], target[1] + 1),
                     (target[0], target[1] - 1))
            lead = ((target[0] + direction[0], target[1] + direction[1])
                    if moving else None)
            for exit_cell in exits:
                if exit_cell == lead:
                    continue
                add_slots(exit_cell, [1], target_rank, hp, uid,
                          "shoot_bracket")

        choices = []
        for cell, cell_slots in slots.items():
            if not self._shot_valid(u, pos, cell):
                continue
            ordered = sorted(cell_slots)
            claimed = self._shot_claims.get(cell, 0)
            if claimed >= len(ordered):
                continue
            priority, target_rank, hp, mode_rank, uid_key, mode = ordered[claimed]
            if mode == "shoot_bracket" and not any(
                    self._shot_claims.get(primary, 0) > 0
                    for primary in primary_cells.get(uid_key, ())):
                continue
            distance = max(abs(cell[0] - pos[0]), abs(cell[1] - pos[1]))
            choices.append((target_rank, priority, hp, mode_rank, distance,
                            cell[0], cell[1], uid_key, cell, mode))
        if not choices:
            self._shot_modes.pop(u["uid"], None)
            return None
        choice = min(choices)
        self._shot_modes[u["uid"]] = choice[-1]
        return choice[-2]

    def _shot_valid(self, u, pos, target):
        """target 是否在 Ranger 射程内且视线无阻挡（八方向 1-3）。

        目标格本身是障碍 → False（预测格是障碍 = 敌人下一步撞墙，射了
        必空枪——用户反馈：Ranger 往障碍上射空枪）。"""
        if self._is_obstacle(*target):
            return False
        dx = abs(target[0] - pos[0])
        dy = abs(target[1] - pos[1])
        if dx > 3 or dy > 3:
            return False
        if dx != 0 and dy != 0 and dx != dy:
            return False
        if dx == 0 and dy == 0:
            return False
        d = max(dx, dy)
        if not (1 <= d <= 3):
            return False
        for t in range(1, d):
            mx = pos[0] + (target[0] - pos[0]) // d * t
            my = pos[1] + (target[1] - pos[1]) // d * t
            if self._is_obstacle(mx, my):
                return False
        return True

    def _known_enemy_sees(self, cell, obs):
        """Best-effort visibility of ``cell`` from currently visible enemies.

        Hidden hostile objects are unknowable, so unknown space is treated as
        open and only permanent obstacles already learned by our fleet provide
        cover.  This makes the estimate conservative for known vision sources.
        """
        cell = tuple(cell)
        sources = [
            (tuple(enemy["pos"]), VISION.get(enemy["utype"], 5))
            for enemy in obs.enemies
        ]
        sources.extend((tuple(core["pos"]), VISION["CORE"])
                       for core in obs.enemy_cores)
        for source, radius in sources:
            if self._dist(source, cell) > radius:
                continue
            blocked = any(
                terrain_cell in self.mem.obstacles
                for terrain_cell in supercover_line(
                    source[0], source[1], cell[0], cell[1])[1:-1]
            )
            if not blocked:
                return True
        return False

    def _ranger_firing_position(self, u, obs, target, tag_prefix):
        """Move toward a shot cell, preferring cover from known enemy vision."""
        pos = tuple(u["pos"])
        target = tuple(target)
        enemy_cells = {tuple(enemy["pos"]) for enemy in obs.enemies}
        directions = ((1, 0), (-1, 0), (0, 1), (0, -1),
                      (1, 1), (1, -1), (-1, 1), (-1, -1))

        # Prefer an immediate step that creates a shot this Tick.  Cardinal
        # movement is the only legal Unit move even though shooting is 8-way.
        immediate = []
        for order, (dx, dy) in enumerate(directions[:4]):
            candidate = (pos[0] + dx, pos[1] + dy)
            if self._is_terrain_obstacle(*candidate) \
                    or candidate == self._core_cell \
                    or candidate in enemy_cells \
                    or self._projected_occupancy(candidate, obs) >= 2 \
                    or not self._shot_valid(u, candidate, target):
                continue
            firing_range = max(abs(target[0] - candidate[0]),
                               abs(target[1] - candidate[1]))
            exposed = self._known_enemy_sees(candidate, obs)
            immediate.append((exposed, -firing_range, order, candidate))
        if immediate:
            candidate = min(immediate)[-1]
            hidden = not self._known_enemy_sees(candidate, obs)
            if hidden:
                tag = tag_prefix + "_cover"
            elif tag_prefix == "ranger":
                tag = "angle"
            else:
                tag = tag_prefix + "_angle"
            self._dbg(u["uid"], tag, candidate)
            return ("MOVE", {"direction": dir_name(
                candidate[0] - pos[0], candidate[1] - pos[1])})

        # Otherwise route toward one of the 24 cells from which the target can
        # be shot.  Rank by Manhattan distance first so the common case needs
        # only one short A* lookup; verify reachability before committing.
        goals = []
        for dx, dy in directions:
            for firing_range in (3, 2, 1):
                candidate = (target[0] + dx * firing_range,
                             target[1] + dy * firing_range)
                if candidate == pos or candidate in enemy_cells \
                        or self._is_obstacle(*candidate) \
                        or self._projected_occupancy(candidate, obs) >= 2 \
                        or not self._shot_valid(u, candidate, target):
                    continue
                exposed = self._known_enemy_sees(candidate, obs)
                goals.append((exposed, self._dist(pos, candidate),
                              -firing_range, candidate[0], candidate[1],
                              candidate))
        for exposed, _distance, _range, _x, _y, goal in sorted(goals):
            path = self.pf.find(pos, goal, max_steps=40)
            if not path:
                continue
            step = tuple(path[0])
            if self._projected_occupancy(step, obs) >= 2:
                continue
            if not exposed:
                tag = tag_prefix + "_cover_advance"
            elif tag_prefix == "ranger":
                tag = "firing_advance"
            else:
                tag = tag_prefix + "_advance"
            self._dbg(u["uid"], tag, goal)
            return ("MOVE", {"direction": dir_name(
                step[0] - pos[0], step[1] - pos[1])})
        return None

    def _yield_core(self, pos, obs, uid):
        """站 Core 格 → 让出：空格优先；没有空格时，相邻格只要还有 1 个
        空槽（容量 2，Core 实体占 1 槽，站着的空载 Worker 再占 1 槽 →
        满载 Worker 进格必被 CELL_UNIT_LIMIT，线上 69011 tick 实证）也
        可以进；四邻全满时挤**完全由 Worker 占的格**（满载 Worker 想进
        Core 格、空载 Worker 走 exit_cave 出洞 → 引擎链式 leaving / 2 环
        交换会连环让开；Vanguard/Ranger 不让路，不能挤）。方向按 tick
        轮换防死磕同一格。"""
        counts = {}
        for uu in obs.units:
            p = tuple(uu["pos"])
            counts[p] = counts.get(p, 0) + 1
        start = obs.tick % 4
        dirs = ((1, 0), (-1, 0), (0, 1), (0, -1))
        free_cand = None
        for i in range(4):
            dx, dy = dirs[(start + i) % 4]
            cand = (pos[0] + dx, pos[1] + dy)
            if self._is_terrain_obstacle(*cand):
                continue
            n = counts.get(cand, 0)
            if n == 0:
                self._dbg(uid, "yield_core")
                return ("MOVE", {"direction": dir_name(dx, dy)})
            if n == 1 and free_cand is None:
                free_cand = (dx, dy)
        if free_cand is not None:
            self._dbg(uid, "yield_core")
            return ("MOVE", {"direction": dir_name(*free_cand)})
        for i in range(4):
            dx, dy = dirs[(start + i) % 4]
            cand = (pos[0] + dx, pos[1] + dy)
            if self._is_terrain_obstacle(*cand):
                continue
            blockers = [uu for uu in obs.units if tuple(uu["pos"]) == cand]
            if blockers and all(uu["utype"] == "WORKER" for uu in blockers):
                self._dbg(uid, "yield_core")
                return ("MOVE", {"direction": dir_name(dx, dy)})
        return None  # 无空格/空槽且无可交换的满载 Worker：等

    def _cave_exit_target(self, pos):
        """洞内空载 Worker 的撤出目标：洞口必经格外（BFS>3）最近的地形格。

        只把目标定在洞口必经格（BFS=3）会让空载 Worker 在洞口打转（洞口
        被满载队列占满 → 进不去 → 下一 Tick 又选回来，形成 A-B 振荡），
        满载交货通道始终被空载堵死。注意 BFS=3 正是洞口必经格本身，条件
        必须严格 >3 才真正越过洞口（此前用 >2 仍会命中洞口格，环形搜索
        第一圈就返回，洞内 Worker 永远撤不出去——线上 69015 tick 死锁
        实证）。目标定到洞外走廊后，普通 A* 会把路径完整算到洞外，第一步
        朝洞外走，即使被满格挡一 Tick 也不会回头。"""
        core_pos = self._core_cell
        best_d, best = None, None
        for r in range(1, 13):
            for dy in range(-r, r + 1):
                for dx in (r, -r):
                    cand = (pos[0] + dx, pos[1] + dy)
                    if cand == core_pos:      # Core 格不在口袋 dict（BFS 排除）
                        continue              # → get() 返 99 会被误当洞外
                    bd = self._core_pocket_dist.get(cand, 99)
                    if bd > 3 and not self._is_persistent_obstacle(*cand):
                        if best is None or bd < best_d or (
                                bd == best_d
                                and self._dist(pos, cand) < self._dist(pos, best)):
                            best_d, best = bd, cand
                for dx in range(-r + 1, r):
                    for dy in (r, -r):
                        cand = (pos[0] + dx, pos[1] + dy)
                        if cand == core_pos:
                            continue
                        bd = self._core_pocket_dist.get(cand, 99)
                        if bd > 3 and not self._is_terrain_obstacle(*cand):
                            if best is None or bd < best_d or (
                                    bd == best_d
                                    and self._dist(pos, cand) < self._dist(pos, best)):
                                best_d, best = bd, cand
            if best is not None:
                break
        return best

    def _exit_cave(self, u, pos, obs, uid):
        """洞内（口袋 BFS<=3，含洞口必经格）的空载 Worker 撤出洞外，
        让出满载交货通道。

        洞内只有约 8~10 格，被空载/守家单位占满时，满载 Worker 的进 Core
        通道被"满格假墙"截断 → 有货不交（普通 A* 把满格当硬墙，连洞口
        都出不去 → 全员 wait）。目标取洞外最近地形格（BFS>2）而非洞口
        必经格，避免空载在洞口打转；移动用无视满格的 A*，即使必经格被
        占满也提交意图，靠引擎 leaving/2 环交换解链；实在无路则等。"""
        if not self._funnel_cells:
            return None
        d = self._core_pocket_dist.get(pos, 99)
        if d > 3:
            return None
        target = self._cave_exit_target(pos)
        if target is None:
            return None
        step = self._pf_ignore_full().next_step(pos, target)
        if step:
            self._dbg(uid, "exit_cave", target)
            return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                   step[1] - pos[1])})
        return None

    def _vault_hold(self, u, obs, core_pos, radii, tag):
        """守库驻位：分层收缩到 Core 周围，固定目标且每格只驻一人。

        Worker 在外环，Ranger 在中环，Vanguard 在内环。目标选择避开资源格、
        Core 格和当前满格，并保留跨 Tick 粘性，防止32个单位反复换位。
        """
        if core_pos is None:
            return None
        uid = u["uid"]
        pos = tuple(u["pos"])
        r_min, r_max = radii
        occupied = {tuple(uu["pos"]): uu["uid"] for uu in obs.units}
        # 洞内守家（Ranger）：存在洞口必经格时，优先驻洞内（BFS<=3）且能
        # 打到洞口必经格的格，避免驻到墙外（线上见守家 Ranger 站在围墙外）。
        cave_mode = u["utype"] == "RANGER" and bool(self._funnel_cells)
        d_lo = 2 if cave_mode else r_min

        def valid(cand):
            d = self._dist(cand, core_pos)
            if not (d_lo <= d <= r_max):
                return False
            if cand in self._vault_taken or cand in self.mem.resources:
                return False
            if self._is_terrain_obstacle(*cand):
                return False
            # 守库/守家单位只驻 Core 口袋（地形连通区）内：曼哈顿环跨墙
            # 选点会把单位丢到墙外/不可达格。BFS<=1 是 Core 门口格，留给
            # 满载 Worker 交货通道，守卫不占。
            if cand not in self._core_pocket:
                return False
            if self._core_pocket_dist.get(cand, 99) <= 1:
                return False
            # 洞内（存在洞口必经格）时，非 Ranger 守卫不驻"洞口必经格"
            # 本身：那是满载 Worker 进出洞的单格走廊，堵住会让满载全队
            # WAIT（线上死锁实证）。只禁必经格、不禁 BFS<=2 的洞内驻位格
            # ——洞形地貌下洞口只有 1~2 格，把整个 BFS<=2 禁掉会让守家
            # Vanguard 无格可驻。Ranger 例外（洞内射击位依赖 BFS 2~3）。
            if self._funnel_cells and u["utype"] != "RANGER" \
                    and cand in self._funnel_cells:
                return False
            other = occupied.get(cand)
            if other is not None and other != uid:
                return False
            return cand == pos or cand not in self._full_cells

        goal = self._vault_goal.get(uid)
        if goal is not None and not valid(goal):
            self._vault_goal.pop(uid, None)
            goal = None

        if goal is None:
            preferred = (r_min + r_max) // 2
            candidates = []
            for dx in range(-r_max, r_max + 1):
                for dy in range(-r_max, r_max + 1):
                    cand = (core_pos[0] + dx, core_pos[1] + dy)
                    if valid(cand):
                        if cave_mode:
                            bd = self._core_pocket_dist.get(cand, 99)
                            if bd <= 3 and self._funnel_shots.get(cand, 0) >= 1:
                                # 洞内且能打到洞口：分数越低越优先
                                score = (-1000 * self._funnel_shots[cand]
                                         + 50 * (bd - 2)
                                         + 20 * self._funnel_exposed.get(cand, 0))
                                if cand in self._funnel_cells:
                                    score += 100   # 别站在洞口格本身
                            elif bd <= 3:
                                score = 1000 + bd  # 洞内但打不到洞口：兜底
                            else:
                                score = 5000 + bd  # 墙外：强烈避免
                        else:
                            score = abs(self._dist(cand, core_pos) - preferred)
                        # Coordinate salt gives equal-distance units stable but
                        # different choices without relying on randomized hash().
                        salt = ((cand[0] * 73856093) ^ (cand[1] * 19349663) ^
                                self._uid_num(uid)) & 0xffffffff
                        candidates.append((score,
                                           abs(self._dist(cand, core_pos) - preferred),
                                           self._dist(pos, cand), salt, cand))
            for _score, _ring_cost, _travel, _salt, cand in sorted(candidates):
                # 可达性按"无视满格"的 Pathfinder 判定：口袋内地形连通即
                # 可选（满格只是暂时被 Worker 占用，排队即可到达），否则
                # 守家单位会被满格假墙挡在墙外。
                if cand == pos or self._pf_ignore_full().find(pos, cand) is not None:
                    goal = cand
                    self._vault_goal[uid] = cand
                    break

        if goal is None:
            self._dbg(uid, tag, core_pos)
            return None
        self._vault_taken.add(goal)
        if pos == goal:
            self._dbg(uid, tag, goal)
            return None
        step = self.pf.next_step(pos, goal)
        if step:
            self._dbg(uid, tag, goal)
            return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                   step[1] - pos[1])})
        self._vault_goal.pop(uid, None)
        self._dbg(uid, tag, core_pos)
        return None

    def _parking_goal(self, u, pos, obs, core_pos, defense):
        """idle 战斗单位驻留点（势场）：Core 半径环带内选低分格。

        score = 距离惩罚（偏好 park_radius 环带）+ Worker 交通惩罚
        （Core 四邻重罚，防堵满载 Worker 交货通道）+ 本 tick 已认领
        （同 tick 单位分散选格）。返回目标格（20 tick 粘性防抖动）或
        None（无候选：原地待命）。防御状态不 parking（回防是刻意行为）。
        """
        if core_pos is None or defense:
            return None
        g = self.genes
        uid = u["uid"]
        # 洞内守家（Ranger）：存在洞口必经格时，parking 也优先洞内驻点
        # （与 _vault_hold 同一评分，防 idle Ranger 驻到围墙外）。
        cave_mode = u["utype"] == "RANGER" and bool(self._funnel_cells)
        ent = self._park_goal.get(uid)
        if ent and obs.tick - ent[1] < 20:
            goal = ent[0]
            occupied_by_other = any(tuple(uu["pos"]) == tuple(goal)
                                    and uu["uid"] != uid for uu in obs.units)
            if not self._is_obstacle(*goal) and not occupied_by_other \
                    and goal in self._core_pocket \
                    and self._core_pocket_dist.get(goal, 99) > 1 \
                    and not (self._funnel_cells and u["utype"] != "RANGER"
                             and goal in self._funnel_cells):
                self._park_taken.add(goal)
                return goal
            del self._park_goal[uid]
        fail = self._park_fail.get(uid)
        fail_cell = fail[0] if fail and obs.tick - fail[1] < 20 else None
        r_max = int(g["park_radius"]) + 2
        occupied = {tuple(uu["pos"]) for uu in obs.units}
        cx, cy = core_pos
        best, best_score = None, None
        for r in range(2, r_max + 1):
            ring = []
            for dx in range(-r, r + 1):
                ring.append((cx + dx, cy - r))
                ring.append((cx + dx, cy + r))
            for dy in range(-r + 1, r):
                ring.append((cx - r, cy + dy))
                ring.append((cx + r, cy + dy))
            for cand in ring:
                if self._is_obstacle(*cand):
                    continue
                # 驻留点限定 Core 口袋内、且避开 Core 门口格（BFS<=1）：
                # 几何环跨墙选点会把守卫丢到墙外；门口留给满载 Worker。
                if cand not in self._core_pocket:
                    continue
                if self._core_pocket_dist.get(cand, 99) <= 1:
                    continue
                if self._funnel_cells and u["utype"] != "RANGER" \
                        and cand in self._funnel_cells:
                    continue
                if cand == fail_cell:
                    continue
                if cand in self._park_taken or cand in occupied:
                    continue
                d = self._dist(cand, core_pos)
                # 口袋 BFS 距离作小权重偏置：曼哈顿同环时优先选洞内/更近
                # 的格（洞内 8 格可驻防，且不把守卫分到 BFS 20+ 的绕远格）
                bfs_d = self._core_pocket_dist.get(cand, 99)
                if cave_mode:
                    if bfs_d <= 3 and self._funnel_shots.get(cand, 0) >= 1:
                        score = (-1000 * self._funnel_shots[cand]
                                 + 50 * (bfs_d - 2)
                                 + 20 * self._funnel_exposed.get(cand, 0))
                        if cand in self._funnel_cells:
                            score += 100
                    elif bfs_d <= 3:
                        score = 1000 + bfs_d
                    else:
                        score = 5000 + bfs_d
                else:
                    score = abs(d - g["park_radius"]) * g["park_spread_w"] \
                        + 0.3 * bfs_d * g["park_spread_w"]
                if d <= 1:
                    score += 100 * g["park_traffic_w"]
                if best_score is None or score < best_score:
                    best_score, best = score, cand
            if best is not None and (cave_mode or
                                     best_score <= 1.0 * g["park_spread_w"]):
                break   # 内环已有偏好半径上的点，无需扩大
        if best is None:
            return None
        self._park_taken.add(best)
        self._park_goal[uid] = (best, obs.tick)
        return best

    def _idle_spread(self, u, pos, obs, core_pos, defense):
        """idle 散开：战斗单位叠格（满格）或停在 Core 四邻堵门时挪开一格。
        满格堵在 Core 门口会让满载 Worker 的 approach 全被占 → 交货延迟/
        死锁。防御状态不散开（回防是刻意行为）。"""
        if defense or core_pos is None:
            return None
        same_cell = sum(1 for uu in obs.units if tuple(uu["pos"]) == pos) > 1
        near_core = self._dist(pos, core_pos) <= 1
        if not same_cell and not near_core:
            return None
        self._spread_until[u["uid"]] = obs.tick + 10   # 防 rally 拉回门口振荡
        occupied = {tuple(uu["pos"]) for uu in obs.units}
        # 方向按 uid + tick 错开（用户反馈：两先锋叠格散开都选同一方向 →
        # 挤同一格 → MOVE_BLOCKED → 触发脱离振荡）——每单位起始方向不同、
        # 每 tick 轮换，避免死磕同一方向
        start = (self._uid_num(u["uid"]) + obs.tick) % 4
        for i in range(4):
            dx, dy = ((1, 0), (-1, 0), (0, 1), (0, -1))[(start + i) % 4]
            cand = (pos[0] + dx, pos[1] + dy)
            if cand in occupied or cand in self._spread_taken:
                continue
            if self._is_terrain_obstacle(*cand) or cand in self.mem.temp_blocked:
                continue
            if near_core and self._dist(cand, core_pos) <= 1:
                continue   # 挪开后不能还堵在 Core 门口
            self._spread_taken.add(cand)
            self._dbg(u["uid"], "spread")
            return ("MOVE", {"direction": dir_name(dx, dy)})
        return None  # 四邻全被占/障碍：等

    def _idle_patrol(self, u, obs, core_pos):
        """idle 战斗单位巡逻（代替"原地待命"）：按 uid 稳定分配到 0/1/2
        号巡逻圈，每圈半径递增，单位沿圈顺时针绕行巡防。三圈起始相位错开
        120°，同圈单位按 uid 拉开，避免挤在同一格；单位持续移动而非在
        Core 门口聚集等待。仅在 HOME 守家/巡逻/战斗目标均空闲且非防御态
        时（各战斗分支已提前 return）由 idle 尾调用。"""
        if core_pos is None:
            return None
        uid = u["uid"]
        pos = tuple(u["pos"])
        pr = int(round(self.genes["patrol_radius"]))
        # 三圈半径：内/中/外，基于 patrol_radius 递增加大覆盖；外圈封顶 14
        # 保持防御半径（explore 限距 15 附近，不会因巡逻而彻底放弃防线）。
        radii = (max(3, pr // 2), pr, min(pr + 4, 14))
        ring = self._uid_num(uid) % 3
        radius = radii[ring]
        cx, cy = core_pos
        # 收集半径带（±1）上可走（非障碍）的巡逻航点，按角度排序成环
        span = radius + 1
        band = []
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                d = abs(dx) + abs(dy)
                if d < radius - 1 or d > radius + 1:
                    continue
                cand = (cx + dx, cy + dy)
                if self._is_obstacle(*cand):
                    continue
                band.append((math.atan2(dy, dx), cand))
        if not band:
            return None
        band.sort()
        N = len(band)
        # 起始相位：uid + 圈号错开，使三圈分散、同圈单位互不重叠
        base = (self._uid_num(uid) + ring * (N // 3)) % N
        cur = self._patrol_goal.get(uid)
        cells = [c for _, c in band]
        if cur is not None and cur in cells and self._dist(pos, cur) <= 0:
            # 到达当前航点 → 顺时针推进到下一个（沿圈绕行）
            idx = cells.index(cur)
            self._patrol_goal[uid] = band[(idx + 1) % N][1]
        elif cur is None or cur not in cells:
            self._patrol_goal[uid] = band[base][1]
        target = self._patrol_goal[uid]
        step = self.pf.next_step(pos, target)
        if step:
            self._dbg(uid, "patrol", target)
            return ("MOVE", {"direction": dir_name(step[0] - pos[0],
                                                   step[1] - pos[1])})
        # A* 无路（航点临时被占/墙外）：清粘性，下 tick 重选相邻航点
        self._patrol_goal.pop(uid, None)
        return None

    def _core_approach(self, core_pos):
        """Core 四邻中非障碍的格（Core 格本身是 A* 禁区，进格走显式分支）。"""
        out = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (core_pos[0] + dx, core_pos[1] + dy)
            if not self._is_obstacle(*c):
                out.append(c)
        return out

    def _core_approach_goal(self, pos, core_pos):
        """离 pos 最近的 Core 四邻可达格（排除满格，无则 None）。"""
        appr = [c for c in self._core_approach(core_pos)
                if c not in self._full_cells]
        if not appr:
            return None
        return min(appr, key=lambda c: self._dist(pos, c))

    def _rally_goal(self, pos, core_pos, bpos):
        """集结目标：Core 格是 A* 禁区 → 集结到 Core 四邻最近可达格。"""
        if core_pos is None:
            return bpos
        return self._core_approach_goal(pos, core_pos)

    @staticmethod
    def _dist(a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def _away_from(self, pos, targets):
        dx = sum(pos[0] - t[0] for t in targets)
        dy = sum(pos[1] - t[1] for t in targets)
        if dx == 0 and dy == 0:
            return (pos[0] + 1, pos[1])
        return (pos[0] + (1 if dx > 0 else -1), pos[1] + (1 if dy > 0 else -1))

    def _escape_step(self, pos, threats, obs, core_pos=None):
        """Pick one legal cardinal step that maximizes distance from threats.

        This deliberately checks terrain rather than the A* obstacle predicate:
        entering our Core cell is a legal and often useful escape, while A*
        treats that cell as a transit exclusion to preserve the delivery lane.
        """
        enemy_cells = {tuple(e["pos"]) for e in obs.enemies}
        choices = []
        for order, (dx, dy) in enumerate(((1, 0), (-1, 0), (0, 1), (0, -1))):
            cand = (pos[0] + dx, pos[1] + dy)
            if self._is_terrain_obstacle(*cand) or cand in enemy_cells \
                    or self._projected_occupancy(cand, obs) >= 2:
                continue
            danger_dist = min(self._dist(cand, t) for t in threats)
            home_dist = self._dist(cand, core_pos) if core_pos is not None else 0
            choices.append((danger_dist, -home_dist, -order, cand))
        if not choices:
            return None
        best = max(choices)[-1]
        current_danger = min(self._dist(pos, t) for t in threats)
        if min(self._dist(best, t) for t in threats) < current_danger:
            return None
        return best

    def _dir_toward(self, a, b):
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        if abs(dx) >= abs(dy):
            return "RIGHT" if dx > 0 else "LEFT"
        return "DOWN" if dy > 0 else "UP"

    def _beacon_carried_by_me(self, obs):
        for u in obs.units:
            if u["carries_beacon"]:
                return True
        return False
