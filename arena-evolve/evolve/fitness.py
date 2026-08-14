"""适应度评估：个体（基因）在多场对局中的综合表现。

每场：N 玩家 FFA（默认 8，模拟真实世界的高竞争密度），指定玩家位置使用
被测基因，其余是分层对手——RandomBot（弱）/ 默认基因 Heuristic（中等）/
激进变体（强攻型，模拟真实世界的纯攻击玩家），三层各约 1/3。
取多场（不同世界种子）的平均适应度，降低单场方差。
"""

import math
import random
import statistics

from ahsim.game import Game
from strategies.heuristic import HeuristicStrategy, make_default_genes
from strategies.randombot import RandomBot

BOUNDS = (-128, 127, -128, 127)


def _aggressive_genes():
    """激进强攻型对手：早出兵、不恋家、爱打 Core（社区 #213 的屠杀型）。

    只使用策略里真正读取的基因——aggression / engage_distance / swarm_size
    当前在 HeuristicStrategy 中没有任何引用，设了等于没设。
    """
    g = make_default_genes()
    g["army_trigger"] = 2.20        # 兵力劣势也照打（默认 0.70 会退守）
    g["attack_core_first"] = 0.80   # 优先直取敌方 Core
    g["worker_ratio"] = 0.30        # 少采多打
    g["vanguard_share"] = 0.75      # 以近战为主，压制力强
    g["defense_radius"] = 5.0       # 回防半径小 = 不轻易撤回
    g["patrol_radius"] = 4.0        # 巡逻圈收紧，兵力更多投在外线
    g["flee_hp"] = 0.10             # 残血也不退
    g["max_population"] = 32.0      # 160 容量：守库时允许两次减员仍保住 150
    return g


def _balanced_genes():
    """均衡型对手：介于默认发育型与激进强攻之间（中等强度）。

    开源版移除了对敌方 agent 源码直跑的依赖（farmer_adapter），原
    "中等/老玩家"对手角色改由均衡基因策略承担。
    """
    g = make_default_genes()
    g["army_trigger"] = 1.40        # 中等出兵门槛（默认 0.70 偏保守）
    g["attack_core_first"] = 0.40   # 偶而直取敌方 Core
    g["worker_ratio"] = 0.45        # 采打平衡，略偏战斗
    g["flee_hp"] = 0.30             # 低血量才撤退
    return g


class StaticStrategy:
    """死玩家/挂机：Core 存在但完全不行动（模拟真实世界里的无操作死 Core）。

    真实世界玩家稀疏，大部分时间是孤军发育，偶尔遇到挂机/弃坑的 Core。
    挂机玩家的 Core 可以被攻击/掠夺（loot），但不会反击。
    """

    def __init__(self, seed=0):
        pass

    def decide(self, obs):
        return {"core": None, "units": {}}

    def reset(self):
        pass


def _make_strategy(genes, slot, bounds=BOUNDS):
    """构造被测策略或真实世界风格对手（稀疏战场 + 挂机死 Core）。

    构成（6 玩家）：slot 0 被测；1 中等；2 激进强攻；3/4 挂机死 Core；
    5 RandomBot（弱）。真实世界玩家稀疏、多数时间孤军发育、偶遇死 Core。
    """
    if genes is not None:
        return HeuristicStrategy(genes=genes, bounds=bounds)
    if slot in (3, 4):
        return StaticStrategy()                          # 挂机死 Core
    if slot == 1:
        return HeuristicStrategy(genes=_balanced_genes(),  # 均衡（中等）
                                 bounds=bounds)
    if slot == 2:
        return HeuristicStrategy(genes=_aggressive_genes(),  # 激进强攻
                                 bounds=bounds)
    return RandomBot(seed=slot * 7 + 1)                  # 弱


# 线上环境对手出生档案：模拟永久世界里的"老玩家"（发育不同步）——
# 被测新号 1 Worker+5 资源开局，老玩家开局就带人口/兵力/资源，且分散
# 在四周（独立出生中心，间距 90-180 格——线上我们与邻居 30-40 格，但
# 6 个玩家全挤 60 格直径会让出生区资源被分光，新号直接死局）。
# 玩家间距按真实世界数据校准（2026-08-06：敌方联盟 10.8 万格探索 +
# 34 个已知玩家 Core 位置统计）：最近邻平均 50 格、中位 50、最短 20。
# 旧配置我们最近邻 72 格且对手太稀（90-360）——真实世界玩家扎堆
# （20-70 格密布）。新配置：我们最近邻 50 格（正东），对手 50-150 分布。
LIVE_SPAWN_PROFILE = {
    # 对手构成按真实玩家校准（2026-08-06：敌方联盟 4 账号 72 单位统计
    # = 7W/6R/6V，战斗单位 61%——旧配置对手 worker 67% 太和平，评估低估
    # 冲突强度，基因 worker 比例虚高）
    1: {"center": (-46, 128), "res": 20,
        "units": {"WORKER": 7, "RANGER": 6, "VANGUARD": 6}},   # 老玩家（19 人口模板，近邻 50 格）
    2: {"center": (-146, 118), "res": 10,
        "units": {"WORKER": 7, "RANGER": 6, "VANGUARD": 6}},   # 老玩家（19 人口模板，近邻 51 格）
    3: {"center": (-50, 175), "res": 5, "units": {"WORKER": 2}},  # 弃坑残骸（挂机，66 格）
    4: {"center": (30, 100), "res": 5, "units": {"WORKER": 2}},
    5: {"center": (-220, -180)},   # 新生弱号（远离，不抢出生区资源）
}

# 出生站点（被测者每局轮换，P0#16 消除固定 slot 结构性偏差）：
# 站点集合与上面校准一致，被测者占 my_slot 站，对手按相对环序分配
# 角色（2 老玩家 / 2 挂机 / 1 新生），保持最近邻 ~50 格密度校准。
# 6 玩家取前 6 站；8 玩家取全部 8 站（追加站保持同类密度）。
SPAWN_SITES = [(-96, 128), (-46, 128), (-146, 118), (-50, 175),
               (30, 100), (-220, -180), (-160, 200), (70, 30)]
# 对手角色（按相对环序，slot 无关）
ROLE_OLD1, ROLE_OLD2, ROLE_OLD3 = 0, 1, 2
ROLE_STATIC1, ROLE_STATIC2 = 3, 4
ROLE_NEW1, ROLE_NEW2 = 5, 6


def _roles_for(num_players):
    """按玩家数生成对手角色序列（6 玩家：2 老 + 2 挂 + 1 新；
    8 玩家：3 老 + 2 挂 + 2 新——对手数 = num_players - 1）。"""
    n_opp = num_players - 1
    old = 2 if n_opp <= 5 else 3
    static = 2
    new = n_opp - old - static
    roles = [ROLE_OLD1, ROLE_OLD2, ROLE_OLD3][:old]
    roles += [ROLE_STATIC1, ROLE_STATIC2][:static]
    roles += [ROLE_NEW1, ROLE_NEW2][:new]
    return roles


def _strategy_for_role(role, bounds):
    """按角色建对手策略（角色与 slot 解耦——旧实现按 slot 硬编码，
    slot 轮换后角色会错位）。"""
    if role == ROLE_OLD1:
        return HeuristicStrategy(genes=_balanced_genes(), bounds=bounds)  # 均衡
    if role == ROLE_OLD2:
        return HeuristicStrategy(genes=_aggressive_genes(), bounds=bounds)  # 激进
    if role == ROLE_OLD3:
        return HeuristicStrategy(genes=_aggressive_genes(), bounds=bounds)
    if role in (ROLE_STATIC1, ROLE_STATIC2):
        return StaticStrategy()                     # 挂机死 Core
    return RandomBot(seed=role * 7 + 1)             # 新生弱号


def _profile_for_role(role, site):
    """按角色建出生档案（与 LIVE_SPAWN_PROFILE 同语义）。"""
    if role in (ROLE_OLD1, ROLE_OLD2, ROLE_OLD3):
        res = 20 if role == ROLE_OLD1 else 10
        return {"center": site, "res": res,
                "units": {"WORKER": 7, "RANGER": 6, "VANGUARD": 6}}
    if role in (ROLE_STATIC1, ROLE_STATIC2):
        return {"center": site, "res": 5, "units": {"WORKER": 2}}
    return {"center": site, "res": 5}


def _build_live_setup(genes, num_players, my_slot, bounds):
    """slot/出生位轮换（P0#16）：被测者占 SPAWN_SITES[my_slot]，对手
    按相对环序分配角色/站点。返回 (strategies, spawn_center, profile)。"""
    sites = SPAWN_SITES[:num_players]
    me_pos = sites[my_slot]
    strategies = {}
    profile = {}
    roles = _roles_for(num_players)
    others = [(my_slot + 1 + i) % num_players for i in range(num_players - 1)]
    for slot, role in zip(others, roles):
        site = sites[slot]
        strategies[slot] = _strategy_for_role(role, bounds)
        profile[slot] = _profile_for_role(role, site)
    strategies[my_slot] = HeuristicStrategy(genes=genes, bounds=bounds)
    return strategies, me_pos, profile


def _risk_metrics(scores):
    """多局分数 → 风险指标（P0#12：只看均值会选"偶尔爆高、经常崩盘"的策略）。"""
    if not scores:
        return {"fitness_std": 0.0, "fitness_worst": 0.0, "fitness_p10": 0.0}
    if len(scores) < 2:
        return {"fitness_std": 0.0, "fitness_worst": scores[0],
                "fitness_p10": scores[0]}
    s = sorted(scores)
    n = len(s)
    p10 = s[max(0, int(n * 0.1) - 1)]
    return {"fitness_std": statistics.stdev(scores), "fitness_worst": s[0],
            "fitness_p10": p10}


def _detail_of(st):
    """单局 results → 聚合 detail 同字段（key 映射 + 账本）。"""
    d = {
        "harvested": st.get("harvested", 0),
        "deposited": st.get("deposited", 0),
        "damage": st.get("damage_dealt", 0),
        "pop": st.get("final_population", 0),
        "res": st.get("final_resources", 0),
        "lost": st.get("units_lost", 0),
        "respawn": st.get("respawn_count", 0),
        "beacon": st.get("beacon_ticks", 0),
        "alive_ticks": st.get("ticks_alive", 0),
    }
    for k in ("heal_cost", "repair_cost", "spawn_cost",
              "overflow_destroyed", "resources_lost"):
        d[k] = st.get(k, 0)
    return d


_COST_KEYS = ("heal_cost", "repair_cost", "spawn_cost",
              "overflow_destroyed", "resources_lost")


def _agg_from(st, acc):
    """把单局 results 累加进聚合 dict（key 映射：results 的 final_population
    等 → 聚合名 pop/res/lost/...——2026-08-07 修复：旧实现按同名循环累加，
    pop/res/lost 永远统计为 0）。"""
    acc["harvested"] += st.get("harvested", 0)
    acc["deposited"] += st.get("deposited", 0)
    acc["damage"] += st.get("damage_dealt", 0)
    # ``acc`` 汇总多个 seed，快照字段也必须先累加再在调用方取平均。
    # 直接赋值会只保留最后一个 seed，随后仍除以 seed 数，系统性低估
    # early 阶段的人口和资源。
    acc["pop"] += st.get("final_population", 0)
    acc["res"] += st.get("final_resources", 0)
    acc["lost"] += st.get("units_lost", 0)
    acc["respawn"] += st.get("respawn_count", 0)
    acc["beacon"] += st.get("beacon_ticks", 0)
    acc["alive_ticks"] += st.get("ticks_alive", 0)
    for k in _COST_KEYS:
        acc[k] += st.get(k, 0)


def _delta_from(st1, st2, acc):
    """续局增量（st2 − st1）累加进聚合 dict；pop/res 用 st2 终局快照。"""
    acc["harvested"] += st2.get("harvested", 0) - st1.get("harvested", 0)
    acc["deposited"] += st2.get("deposited", 0) - st1.get("deposited", 0)
    acc["damage"] += st2.get("damage_dealt", 0) - st1.get("damage_dealt", 0)
    acc["pop"] = st2.get("final_population", 0)
    acc["res"] = st2.get("final_resources", 0)
    acc["lost"] += st2.get("units_lost", 0) - st1.get("units_lost", 0)
    acc["respawn"] += st2.get("respawn_count", 0) - st1.get("respawn_count", 0)
    acc["beacon"] += st2.get("beacon_ticks", 0) - st1.get("beacon_ticks", 0)
    acc["alive_ticks"] += st2.get("ticks_alive", 0) - st1.get("ticks_alive", 0)
    for k in _COST_KEYS:
        acc[k] += st2.get(k, 0) - st1.get(k, 0)


def evaluate_live(genes, num_players=6, max_ticks=1500, seeds=(42, 43, 44),
                  my_slot=0, size=512, spawn_center=(-96, 128), stats=None):
    """线上环境评估：老玩家带兵出生（发育差）+ 偏远环带 + 分层对手。

    模拟线上真实开局：我们是刚出生的新号，周围是发育了几千 tick 的
    老玩家（slot 1/2 带兵）、弃坑残骸（3/4 挂机）和同样新生的弱号（5）。
    stats: 可选的 GameStats 实例（逐 tick 收集 reason 分布/排队指标）。
    """
    half = size // 2
    bounds = (-half, half - 1, -half, half - 1)
    total = 0.0
    agg = {"harvested": 0, "deposited": 0, "damage": 0, "pop": 0, "res": 0,
           "lost": 0, "respawn": 0, "alive_ticks": 0, "beacon": 0}
    per_game = []
    for seed in seeds:
        # slot/出生位轮换（P0#16）：消除固定 slot 的结构性偏差（旧实现
        # 被测者永远 slot 0，UID 生成顺序与仲裁都系统性偏袒它）
        slot = (my_slot + seed) % num_players
        strategies, spawn_center, profile = _build_live_setup(
            genes, num_players, slot, bounds)
        g = Game(strategies=strategies, size=size, seed=seed,
                 max_ticks=max_ticks, spawn_center=spawn_center,
                 spawn_profile=profile)
        if stats is not None:
            # 逐 tick 循环喂统计（与 g.run() 同语义：tick 递增 + step）
            while g.tick < max_ticks:
                g.tick += 1
                g.step()
                if g.players[slot].core is None:
                    break
                stats.sample(g.build_observation(g.players[slot]))
        else:
            g.run()
        st = g.results()[slot]
        agg["harvested"] += st["harvested"]
        agg["deposited"] += st.get("deposited", 0)
        agg["damage"] += st["damage_dealt"]
        agg["pop"] += st["final_population"]
        agg["res"] += st["final_resources"]
        agg["lost"] += st["units_lost"]
        agg["respawn"] += st["respawn_count"]
        agg["beacon"] += st["beacon_ticks"]
        # 真实存活 tick（不是“最终存活就给满”的占位近似）
        agg["alive_ticks"] += st.get("ticks_alive",
                                     max_ticks if st["alive"] else 0)
        # 成本账本（进选择压力）
        for k in ("heal_cost", "repair_cost", "spawn_cost",
                  "overflow_destroyed", "resources_lost"):
            agg[k] = agg.get(k, 0) + st.get(k, 0)
        per_game.append(fitness_from_detail(_detail_of(st), max_ticks))
    n = len(seeds)
    detail = {k: v / n for k, v in agg.items()}
    detail.update(_risk_metrics(per_game))
    return fitness_from_detail(detail, max_ticks), detail


def evaluate_multistage(genes, seeds=(42, 43, 44, 45), num_players=6,
                         my_slot=0, size=512, spawn_center=(-96, 128),
                         w_early=0.5, w_mid=0.5, mid_ticks=600):
    """多阶段评估（2026-08-07 重构为真实 continuation，P0#9/#10）：
    短局（0→600 从 0 发育）+ 中局（600→600+mid_ticks 真实续局）。

    修复旧实现两个问题：
    1. 中局重新开局（对手新鲜低人口、无记忆/世界继承）→ 独立剧本，
       策略学不到"发育中后期"的连续决策；现在用 Game.resume + 复用
       策略实例，地形/资源/单位/记忆/Beacon/对手状态全部延续。
    2. 阶段权重直接加累计量（mid_ticks 变大 mid 分自然变大）→
       fitness 的累计字段按阶段时长归一化到 600 tick 刻度（见
       fitness_from_detail），w_early/w_mid 恢复真实重要性语义。
    """
    half = size // 2
    bounds = (-half, half - 1, -half, half - 1)
    early = {"harvested": 0, "deposited": 0, "damage": 0, "pop": 0, "res": 0,
             "lost": 0, "respawn": 0, "alive_ticks": 0, "beacon": 0,
             "heal_cost": 0, "repair_cost": 0, "spawn_cost": 0,
             "overflow_destroyed": 0, "resources_lost": 0}
    mid = dict(early)
    per_game = []
    for seed in seeds:
        # slot/出生位轮换（P0#16）
        slot = (my_slot + seed) % num_players
        strategies, spawn_center, profile = _build_live_setup(
            genes, num_players, slot, bounds)
        # 短局：从 0 发育
        g1 = Game(strategies=strategies, size=size, seed=seed, max_ticks=600,
                  spawn_center=spawn_center, spawn_profile=profile)
        g1.run()
        st = g1.results()[slot]
        _agg_from(st, early)
        # 中局：真实续局（复用策略实例，世界/单位/记忆/Beacon 全继承）
        g2 = Game.resume(strategies, g1, mid_ticks)
        g2.run()
        st2 = g2.results()[slot]
        # 续局增量 = st2 − st1（stats 是累计值；pop/res 用 st2 终局快照）
        inc = dict.fromkeys(mid, 0)
        _delta_from(st, st2, inc)
        for k in mid:
            mid[k] += inc[k]
        # 单局分数（阶段加权）→ 风险指标
        d1 = _detail_of(st)
        # ``inc`` is already in the aggregate detail schema produced by
        # ``_delta_from``.  Passing it through ``_detail_of`` again looks
        # harmless but maps damage/pop/res/lost/alive_ticks to zero, making
        # the mid-stage risk score ignore the actual continuation.
        d2 = dict(inc)
        per_game.append(w_early * fitness_from_detail(d1, 600)
                        + w_mid * fitness_from_detail(d2, mid_ticks))
    n = len(seeds)
    def avg(d):
        return {k: v / n for k, v in d.items()}
    e, m = avg(early), avg(mid)
    f_e = fitness_from_detail(e, 600)
    f_m = fitness_from_detail(m, mid_ticks)
    detail = {**{f"early_{k}": v for k, v in e.items()},
              **{f"mid_{k}": v for k, v in m.items()},
              "early_fitness": f_e, "mid_fitness": f_m,
              "total_fitness": w_early * f_e + w_mid * f_m}
    detail.update(_risk_metrics(per_game))
    return w_early * f_e + w_mid * f_m, detail


def evaluate_individual(genes, num_players=8, max_ticks=600, seeds=(42, 43, 44),
                        my_slot=0):
    """评估一个基因：在多种子对局中的平均适应度。

    返回 (fitness, detail) —— detail 含各项原始指标（便于分析）。
    """
    total = 0.0
    agg = {"harvested": 0, "deposited": 0, "damage": 0, "pop": 0, "res": 0,
           "lost": 0, "respawn": 0, "alive_ticks": 0, "beacon": 0,
           "heal_cost": 0, "repair_cost": 0, "spawn_cost": 0,
           "overflow_destroyed": 0, "resources_lost": 0}
    per_game = []
    for seed in seeds:
        slot = (my_slot + seed) % num_players   # P0#16 slot 轮换
        strategies = {}
        for i in range(num_players):
            strategies[i] = _make_strategy(genes if i == slot else None, i)
        g = Game(strategies=strategies, size=256, seed=seed,
                 max_ticks=max_ticks)
        g.run()
        st = g.results()[slot]
        agg["harvested"] += st["harvested"]
        agg["deposited"] += st.get("deposited", 0)
        agg["damage"] += st["damage_dealt"]
        agg["pop"] += st["final_population"]
        agg["res"] += st["final_resources"]
        agg["lost"] += st["units_lost"]
        agg["respawn"] += st["respawn_count"]
        agg["beacon"] += st["beacon_ticks"]
        agg["alive_ticks"] += st.get("ticks_alive",
                                     max_ticks if st["alive"] else 0)
        for k in ("heal_cost", "repair_cost", "spawn_cost",
                  "overflow_destroyed", "resources_lost"):
            agg[k] += st.get(k, 0)
        per_game.append(fitness_from_detail(_detail_of(st), max_ticks))
    n = len(seeds)
    detail = {k: v / n for k, v in agg.items()}
    detail.update(_risk_metrics(per_game))
    return fitness_from_detail(detail, max_ticks), detail


def fitness_from_detail(detail, max_ticks=800):
    """目标：保证存活的同时攒下更多资源。

    2026-08-07 架构评审 P0#7/#11 重设计：
    - 消除重复计分：终局存量权重 1.5→1.0（deposited 已含全部进账，res 只作
      安全垫，不再同一份资源三倍奖励）
    - 人口效率化：只奖到 40 人口（rules v0.14 删除维护费，旧 19 红线已废除；
      动态价格在 deposited 中隐式体现——人口多=生产贵=交付少）
    - 线上成本进选择压力：治疗/修盾/溢出销毁/资源被掠夺显式扣分（这些成本
      以前只发生在引擎里，fitness 看不到，策略可以通过频繁治疗/冲人口
      获得表面收益）
    - 时间归一化（P0#10）：累计类字段（采集/交付/伤害/损失/成本）除以
      max_ticks/600——折算到"每 600 tick 速率"刻度，多阶段评估的
      w_early/w_mid 才代表真实重要性（否则 mid_ticks 变长 mid 分自然变大）。
      快照类字段（pop/res）不缩放；alive 已按 max_ticks 归一化。
    """
    t = max_ticks / 600.0
    return (
        detail["harvested"] / t * 0.6        # 采集效率（经济基础）
        + detail["deposited"] / t * 1.2      # 交付量（Core 总收入）
        + detail["res"] * 1.0            # 终局储备（安全垫，不重复计分）
        + min(detail["pop"], 40.0) * 0.8    # 兵力（只奖到 40；动态价格自然约束更高人口）
        + detail["beacon"] / t * 0.05        # Beacon 辅助
        + detail["alive_ticks"] / max_ticks * 2.0   # 存活保底（真实 ticks）
        + detail["damage"] / t * 0.3         # 战斗降为手段
        - detail["lost"] / t * 0.8           # 单位损失
        - detail["respawn"] / t * 2.0        # 重生惩罚
        # ---- 成本账本（P0#11）----
        - detail.get("heal_cost", 0) / t * 0.15        # 治疗成本（被打多/乱治疗）
        - detail.get("repair_cost", 0) / t * 0.1       # 修盾成本
        - detail.get("overflow_destroyed", 0) / t * 0.5  # 容量溢出纯浪费
        - detail.get("resources_lost", 0) / t * 1.0   # 被掠夺/摧毁
    )


def combine_details(parts):
    """按种子数加权合并多段 detail，等价于一次性跑完全部种子。

    ``parts`` accepts ``(detail, n_seeds)`` or
    ``(detail, n_seeds, mean_fitness)``.  The latter lets prescreen combine
    variance with the pooled-sample formula instead of averaging standard
    deviations, which systematically understates cross-seed risk.
    """
    normalized = [(p[0], p[1], p[2] if len(p) > 2 else None) for p in parts]
    total_n = sum(n for _d, n, _mean in normalized)
    keys = normalized[0][0]
    out = {k: sum(d[k] * n for d, n, _mean in normalized) / total_n
           for k in keys}
    out["fitness_worst"] = min(
        d.get("fitness_worst", float("inf")) for d, _n, _mean in normalized)
    out["fitness_p10"] = min(
        d.get("fitness_p10", float("inf")) for d, _n, _mean in normalized)
    if all(mean is not None for _d, _n, mean in normalized) and total_n > 1:
        pooled_mean = sum(mean * n for _d, n, mean in normalized) / total_n
        sum_sq = sum(
            max(0, n - 1) * d.get("fitness_std", 0.0) ** 2
            + n * (mean - pooled_mean) ** 2
            for d, n, mean in normalized)
        out["fitness_std"] = math.sqrt(sum_sq / (total_n - 1))
    return out


def evaluate_default():
    """评估默认基因（baseline），作为进化起点参考。"""
    return evaluate_individual(make_default_genes(), seeds=(42, 43, 44, 45))
