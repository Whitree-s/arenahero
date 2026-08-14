# 模拟器规则保真度核对记录

模拟器（ahsim/engine.py）的目标：与正式世界（doc.arenahero.io，gameplay rules v0.14）
逐条对齐，保证进化结果可迁移。本文记录核对过程与结论。

> 核对方式：用户抓取官方全部 9 页规则 + reference/numbers + api/resolution-results
> + changelog，逐条对照代码（最近一次为 2026-08-06，Python SDK 0.2.9）。

## 已核对无误的核心规则

### 数值（reference/numbers）
- Core 5HP / 5 盾 / 持标 10 盾
- 三兵种：Worker HP2/视野3/造价5、Vanguard HP4/视野4/造价10（近战1）、Ranger HP2/视野5/造价12
- Ranger 八方向 1-3 格，(3,3) 算 3 格
- 前 20 个单位保持基础价；第 21 个起每五人口区间按 1.3 倍动态涨价，精确分数最终一次 round-half-up
- 容量 max(10, pop×5)；资源配额 max(2, floor(128/(8+ring)))
- 出生 20-30 曼哈顿；格容量 2；无维护费和人口保护机制

### 机制
- 战斗快照后同时结算；致命伤不可治疗
- 新兵出生当 Tick 不可被打
- 单位不挡射线，只有障碍挡（v0.7 穿透射击）
- 最低 HP + UUID 选靶（v0.13）
- 生产价格使用同 Tick 自毁和战斗死亡后的实际存活人口（v0.14）
- Core 无条件自毁在战斗后（v0.12）
- Core 自毁移除所有单位、掉落 Worker cargo/Beacon，并同 Tick 尝试重生
- 摧毁 Core 掠夺归最高伤害者，赢家 Core 须存活（v0.9）
- 同点采集最低 UUID 独占；cargo 堆同 Tick 独占
- Beacon 动作在采集之前（当 Tick 即享加成）
- 视野用 supercover 线，穿角两格都算
- 移动依赖图：换位/链式/环处理符合 MOVE_SWAP_BLOCKED / MOVE_DEPENDENCY_FAILED 语义
- 战斗后恢复 HP（v0.10）；Ranger 按格射击（v0.13）
- SDK 0.2.9 已删除 `population_tier` / `upkeep_next_tick`，Ranger `SHOOT` 保留 `expected_cell`

## 曾修复的规则偏差（按优先级）

1. **Beacon 坐标被隐藏** → 官方"坐标永远公开，只有 status/carrier_id 受视野限制"
   - 影响最大：坐标隐藏导致 Beacon 战术在进化中从未被争夺
2. **Beacon 掉落当 Tick 可拾取** → 官方"掉落当 Tick 不可再被捡起"（防接力）
3. **丢失 Beacon 盾值未钳制** → 官方"丢标后盾钳制回 5"
4. **缺 CORE_RESOURCE_OVERFLOW_DESTROYED** → 人口下降容量超限，超出资源立即销毁
5. **RESOURCE 地形未赋值** → 迁移中的 Core 不可入资源格（terrain_kind 返回 RESOURCE）
6. **缺 CANCEL_MOVE** → Core 动作表有，CORE_MOVE_CANCELLED 事件
7. **cargo 堆同 Tick 可被多个 Worker 取** → 同源竞争最低 UUID 独占
8. **评估信号：Core 摧毁未计 units_lost** → 舰队移除时计入损失（fitness 惩罚真实生效）
9. **v0.14 仍按 Ranger 固定价格决定是否生产** → 按实际兵种和实时人口计算，买不起时降级
10. **Core 自毁留下幽灵单位且无法重生** → 清空舰队并进入同 Tick 重生流程
11. **参考策略仍构造 SDK 0.2.8 状态** → 删除旧字段并传入真实可见地形与 Core 移动状态

## 已知近似（有意为之，非错误）

- 世界 256×256 有界（官方 signed int64 无界）→ 4-8 玩家挤中央，交战密度高于真实
  （高密度进化 8 人已部分补偿；迁移到真实世界 aggression 偏激进）
- 世界生成用自建确定性 PRNG（官方 HMAC 种子私有），地形形态不同但自洽

## 确定性要求（进化公平性）

1. 字符串 key 的 dict/set 迭代必须 sorted（PYTHONHASHSEED 随机化导致非确定）
2. `Game.__init__` 必须重置 `ahsim.entities._NEXT_ID`（全局 uid 计数器跨运行漂移）
3. 修复后验证：同种子两次运行结果逐位一致（含随机 hash seed）
