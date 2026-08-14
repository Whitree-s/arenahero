# 策略说明（Strategy Reference）

> 本文档描述 `strategies/heuristic.py` 中 `HeuristicStrategy` 的实现策略，
> 以及 `evolve/` 进化评估的对手分层与算法配置。内容以代码为准。

```yaml
scope: "HeuristicStrategy 行为与进化评估配置"
strategy_file: strategies/heuristic.py
fitness_file: evolve/fitness.py
ga_file: evolve/ga.py
gene_count: 27
population_cap: 32        # 守库模式人口上限（v0.14 冻结）
resource_goal: 150        # 守库兑换目标
home_guard: "2 Vanguard + 1 Ranger"
```

## 1. 总体框架

`HeuristicStrategy` 是一个分兵种、多优先级的参数化启发式策略，全部行为由
27 个可进化基因控制。每个 tick 的决策流程：

```
decide(obs)
├── 记忆维护（视野更新 / 敌人遗忘 / 临时障碍清理 / 长跑淘汰）
├── 状态同步（守家小队补员 / 饥饿检测 / 冲突退避 / 目标认领重置）
├── 守库模式判定（资源 ≥150 → 冻结经济）
├── Core 决策（_decide_core）
└── 逐单位决策（_decide_unit）
    ├── Worker（采集 / 侦察 / 撤退 ...）
    ├── Vanguard（守家 / 战斗 / 驻位 ...）
    └── Ranger（射击 / 风筝 / 巡逻 ...）
```

## 2. 宏观行为模式

| 行为 | 触发条件 | 内容 |
|---|---|---|
| 守库模式 | Core 资源 ≥ 150（`RESOURCE_GOAL`） | 冻结 Core 全部支出（治疗 / 生产）；人口固定 32、容量 160；Worker 回撤 6-10 环、Vanguard 2-4 环、Ranger 3-6 环分层驻守；兑换后资源归零自动恢复采集 |
| 永久守家小队 | 常驻 | 稳定保留 2 Vanguard + 1 Ranger（半径 2-3 环），阵亡后由最近战斗单位接替，不参与远征 |
| 冲突退避 | 连续 3 次移动失败 | 短停 2 tick，打破单位间同步互堵（长局 99% 移动失败为 DEPENDENCY 型互等） |
| 饥饿检测 | 连续无采集成功 | 扩大侦察环带寻找新资源区（出生区资源枯竭场景） |

## 3. Core 决策优先级

1. 迁移中 → 不动作；资源 ≥ 150 → 冻结（攒兑换，不治疗不生产）
2. 治疗：HP < `core_heal_hp` × 最大 HP → `HEAL`
3. 修盾：盾 < `shield_repair_hp` × 容量 → `REPAIR_SHIELD`
4. 防御状态：仅当资源足以购买战斗单位时才生产（不回退到便宜 Worker 稀释守军比例）
5. 正常生产：按 守家缺员 → 战斗缺口 → `worker_ratio` 比例 依次决策兵种；
   人口 30→32 用便宜 Worker 补容量保险（两名 Worker 比高价战斗单位更快把容量
   从 150 推到 160）

## 4. 兵种行为

### Worker

| 标签 | 行为 |
|---|---|
| `goto_resource` / `harvest` / `deposit` | 采集交付链路，资源任务 80 tick 粘性防振荡 |
| `scout` | 侦察：按 uid 固定 8 方向槽 + 环带轮换（10/20/30），饥饿时扩大到 40 |
| `return_core` | 低血撤退（`worker_flee`） |
| `revisit` | 区域回访（`revisit_ticks` 周期） |
| `block` / `siege_block` | 进攻时堵路（`block_count`） |
| `harass` | 空闲时拦截敌方 Worker 压制经济（`harass_workers`） |
| `hold_full` / `wait_resource` | 满载 / 目标不可达时等待 |

### Vanguard

| 标签 | 行为 |
|---|---|
| `home_vanguard` / `vault_vanguard` | 守家 / 守库环带驻守 |
| `sweep` / `attack` | 邻格攻击 / 兵力占优追击（`army_trigger`） |
| `siege_vanguard` | 围敌方 Core |
| `raid` | 突袭确认静止的敌方 Core（连续观察 ≥ `raid_min_obs` 次、距离 ≤ `raid_max_dist`） |
| `explore` | 空闲侦察（离家 ≤ 15 格，12 tick 目标粘性） |
| `spread` | 叠格散开（优先于探索，防同步移动） |
| `park` | idle 驻位环（`park_radius` 势场，避 Core 门口 Worker 通道） |
| 让位 | 堵 Core 门口时挪开 |

### Ranger

| 标签 | 行为 |
|---|---|
| `shoot_stationary` | supercover 射击线 + 命中格伤害认领 |
| `kite` | 风筝（`kite_range`） |
| `patrol` | 环形巡逻（`patrol_radius`） |
| `explore` / `track` | 侦察 / 追踪 |
| `ranger_disengage` | 脱离危险距离 |
| `siege_ranger` | 围点辅助 |

## 5. 记忆与寻路基础设施

- A* 版本化缓存：障碍变化才失效；Core 格为禁区，集结到四邻
- 敌人记忆：`forget_ticks` 后遗忘；追击超过 25 tick 未确认 → 放弃（防横跳）
- 目标认领：战斗目标（每格 ≤2）、资源点、驻位点、探索点、射击格均为本 tick
  认领，防全员扑同一目标
- 满格管理：移动目标格被占时按最小冲突代价决策

## 6. 进化评估的对手分层（`evolve/fitness.py`）

模拟对局为 N 玩家 FFA（默认 8 玩家）。对手构成：

| 角色 | 策略 | 说明 |
|---|---|---|
| 被测 | 进化中的基因 | slot 轮换消除位置偏差 |
| 老玩家 ×2-3 | 均衡（`_balanced_genes`）/ 激进（`_aggressive_genes`） | 带兵出生、发育不同步 |
| 弃坑残骸 ×2 | `StaticStrategy` | 挂机死 Core（可掠夺） |
| 新生弱号 ×1-2 | `RandomBot` | 远端弱对手 |

出生点位与人口档案按真实世界数据校准（最近邻 ~50 格密度）。

## 7. 进化算法（`evolve/ga.py`）

| 组件 | 配置 |
|---|---|
| 种群 | 默认 24；初始化 = 默认基因 ± 25% 高斯扰动（warm start 时为给定基因 ± 15%） |
| 选择 | 锦标赛（`tournament=3`） |
| 精英保留 | 前 2 名直接进入下一代 |
| 交叉 | 概率 0.8，均匀交叉（逐基因 50% 取自另一亲本） |
| 变异 | 40% 概率，高斯扰动 σ = 基因范围 × 0.12 |
| 防过拟合 | `--holdout-seeds` 独立验证集（不进选择压力）；`--seed-rollover` 滚动种子池 |
| 加速 | 进程池评估 + 基因指纹缓存 + 预筛（`--prescreen`） |

## 8. 基因参数表（27 个）

| 基因 | 默认值 | 边界 | 含义 |
|---|---|---|---|
| `worker_ratio` | 0.55 | 0.10-0.90 | 目标 Worker 占人口比例 |
| `army_trigger` | 0.70 | 0.20-3.00 | 敌方/己方兵力比低于此才进攻（越高越敢打） |
| `flee_hp` | 0.35 | 0.00-0.80 | 单位低血（比例）回 Core |
| `heal_hp` | 0.65 | 0.20-1.00 | 单位血量低于此且在 Core 旁 → HEAL |
| `core_heal_hp` | 0.40 | 0.00-1.00 | Core HP 低于此 → HEAL |
| `shield_repair_hp` | 0.35 | 0.00-1.00 | Core 盾低于此且资源足 → REPAIR_SHIELD |
| `spawn_worker_until` | 8.0 | 2.0-25.0 | 资源低于此值时优先造 Worker |
| `vanguard_share` | 2/3 | 冻结 | v0.14 固定 2V1R，保留字段兼容旧结果 |
| `rally_radius` | 6.0 | 2.0-15.0 | 集结半径 |
| `beacon_go_range` | 18.0 | 5.0-45.0 | 距 Beacon 小于此就去抢 |
| `max_population` | 32 | 冻结 | 32 人口容量 160，满仓守库 |
| `attack_core_first` | 0.55 | 0.00-1.00 | 进攻时优先 Core vs 单位 |
| `kite_range` | 2.0 | 1.0-3.0 | Ranger 风筝距离 |
| `worker_flee` | 0.70 | 0.00-1.00 | Worker 遇敌撤退倾向 |
| `patrol_radius` | 6.0 | 3.0-12.0 | 巡逻 Ranger 环形半径 |
| `defense_radius` | 10.0 | 4.0-20.0 | Core 防御响应距离 |
| `forget_ticks` | 80.0 | 20.0-300.0 | 敌人消失多久后标记遗忘 |
| `revisit_ticks` | 150.0 | 40.0-400.0 | 区域回访周期 |
| `selfdestruct_pop` | 999.0 | 冻结 | 自毁转 Ranger 阈值（v0.14 禁用） |
| `block_count` | 0.0 | 0.0-6.0 | 进攻时堵路的 Worker 数 |
| `harass_workers` | 0.0 | 0.0-1.0 | 空闲 Worker 拦截敌方 Worker |
| `raid_stationary` | 0.5 | 0.0-1.0 | 突袭静止 Core 倾向 |
| `raid_min_obs` | 3.0 | 2.0-8.0 | 静止确认所需连续观察次数 |
| `raid_max_dist` | 40.0 | 20.0-60.0 | 突袭最大距离 |
| `park_radius` | 3.0 | 2.0-8.0 | idle 驻留环带半径 |
| `park_spread_w` | 1.0 | 0.0-5.0 | 驻留点偏离偏好半径的惩罚权重 |
| `park_traffic_w` | 2.0 | 0.0-10.0 | Core 四邻（Worker 交通）惩罚权重 |

## 9. 当前线上基因（`genes/evolve_v7_best.json`）

仓库附带的基因文件为正式部署实例使用的进化结果。加载时经 `normalize_genes`
规范化到当前规则（v0.14）：冻结基因强制取边界值，旧规则遗留字段自动丢弃。
