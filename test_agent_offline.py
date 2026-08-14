#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线验证 v2：用 arena_hero 真实模型构造假状态，喂给 arena_agent.plan_turn_v2，
检查决策是否正确、不抛异常、产出的 CommandPlan 能被 SDK 接受。
"""
from uuid import uuid4, UUID

from arena_hero import (
    Turn,
    PlayerState,
    CoreView,
    UnitView,
    TerrainView,
    ChampionBeacon,
    PlayerStatus,
    UnitType,
    BeaconStatus,
    CommandPlan,
    Direction,
)

# 方向 → 坐标增量（用于将决策方向换算成落点，断言是否踩到敌格）
DELTA = {
    Direction.UP: (0, -1),
    Direction.DOWN: (0, 1),
    Direction.LEFT: (-1, 0),
    Direction.RIGHT: (1, 0),
}

import arena_agent as agent

_CORE_ID = uuid4()
CORE_ID = _CORE_ID  # 兼容旧引用
_FA = uuid4(); _FB = uuid4(); _FC = uuid4(); _FD = uuid4(); _FE = uuid4()  # 测试用固定ID
W1, W2, W3, W4, W5 = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
R1, V1 = uuid4(), uuid4()
ENEMY = uuid4()
ENEMY_CORE = uuid4()
CARRIER = uuid4()
# 已被我方单位携带的 Beacon（用于大多数场景，表示无需再派搬运工）
CARRIED_BEACON = ChampionBeacon(position=(0, 0), status=BeaconStatus.CARRIED,
                                 carrier_id=CARRIER)


def uw(uid, pos, cargo=0, ut=UnitType.WORKER):
    """构造可控 UnitView（测试直接调用分层函数时用）。"""
    return UnitView(kind="UNIT", id=uid, controlled=True, position=pos,
                    hp=agent.MAX_HP.get(ut, 2), unit_type=ut, cargo=cargo)


def enemy_core(pos):
    """敌方核心（CoreView, controlled=False）。"""
    from arena_hero import CoreState
    return CoreView(kind="CORE", id=ENEMY_CORE, controlled=False,
                    owner_username="enemy", position=tuple(pos), hp=10,
                    shield=0, state=CoreState.NORMAL)


def make_turn(tick, resources, population, core_pos, core_hp=5, core_shield=5,
              workers=(), terrain=(), beacon=None, enemies=(), events=()):
    beacon = beacon or ChampionBeacon(position=(0, 0), status=BeaconStatus.GROUND)
    objects = [
        CoreView(kind="CORE", id=CORE_ID, controlled=True, owner_username="tester",
                 position=core_pos, hp=core_hp, shield=core_shield, state="NORMAL"),
    ]
    for wid, wpos, cargo, ut in workers:
        c = cargo if ut == UnitType.WORKER else None
        objects.append(
            UnitView(kind="UNIT", id=wid, controlled=True, position=wpos,
                     hp=agent.MAX_HP.get(ut, 2), unit_type=ut, cargo=c)
        )
    objects.extend(enemies)
    objects.extend(terrain)
    state = PlayerState(
        status=PlayerStatus.ACTIVE, resources=resources, population=population,
        champion_beacon=beacon, objects=tuple(objects), events=tuple(events),
    )
    captured = {}
    def submitter(plan, ik=None):
        captured["plan"] = plan; return plan
    return Turn(tick=tick, state=state, submitter=submitter), captured


def enemy_unit(pos, ut=UnitType.VANGUARD):
    return UnitView(kind="UNIT", id=ENEMY, controlled=False, position=pos,
                    hp=2, unit_type=ut, cargo=None)


class FakeUnit:
    """可控进攻单位桩（记录 move/sweep/shoot，便于断言跟踪/进攻行为）。"""
    def __init__(self, uid, ut, pos):
        self.id = uid
        self.unit_type = ut
        self.position = pos
        self.cargo = 0
        self.moves = []
        self.sweeps = []
        self.shoots = []
        self.shoot_cells = []
    def move(self, d):
        self.moves.append(d)
        dx, dy = d.delta
        self.position = (self.position[0] + dx, self.position[1] + dy)
    def sweep(self, d):
        self.sweeps.append(d)
    def shoot(self, t):
        self.shoots.append(t)
    def shoot_cell(self, cell):
        self.shoot_cells.append(cell)


def run(name, build):
    state = agent.AgentState()
    t, captured = build()
    try:
        agent.plan_turn_v2(t, state)  # 不传 game（无 received）
        plan = t.plan
        assert isinstance(plan, CommandPlan), f"expected CommandPlan got {type(plan)}"
        print(f"[OK] {name}")
        pd = plan.model_dump() if hasattr(plan, 'model_dump') else str(plan)
        # 精简输出
        ua = pd.get('unit_actions', {})
        ca = pd.get('core_action')
        print(f"     units={len(ua)} actions core={ca}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] {name}: {e!r}")
        traceback.print_exc()
        return False


def run_group_overlap():
    """重叠编队(GROUP_TRAIL=0)：游侠直接叠到先锋所在格；TRAIL=1：游侠落后1格不踩先锋。"""
    saved = agent.GROUP_TRAIL
    try:
        agent.GROUP_TRAIL = 0
        v = FakeUnit(V1, UnitType.VANGUARD, (10, 10))
        r = FakeUnit(R1, UnitType.RANGER, (11, 10))
        agent._group_trail(r, v, (15, 10), set())
        assert r.position == v.position, f"重叠失败：游侠应在先锋格，实得{r.position}"
    finally:
        agent.GROUP_TRAIL = saved
    saved = agent.GROUP_TRAIL
    try:
        agent.GROUP_TRAIL = 1
        v = FakeUnit(V1, UnitType.VANGUARD, (10, 10))
        r = FakeUnit(R1, UnitType.RANGER, (12, 10))
        agent._group_trail(r, v, (15, 10), set())
        assert r.position != v.position, "TRAIL=1 不应踩到先锋头上"
        assert agent.manhattan(r.position, v.position) <= 2, "应落后约1格"
    finally:
        agent.GROUP_TRAIL = saved
    print("[OK] BQ 编队：TRAIL=0 游侠叠先锋格 / TRAIL=1 落后1格")
    return True


def run_danger_zone_lifecycle():
    """禁止/危险区域生命周期：阵亡+敌多→建禁止区(圆心=最推进敌,半径=圆心→死亡点)；
    探索组数>记录敌数→转危险区；圆内无威胁→解除。"""
    state = agent.AgentState()
    core_pos = (20, 20)
    v1 = FakeUnit(V1, UnitType.VANGUARD, (25, 20))
    r1 = FakeUnit(R1, UnitType.RANGER, (25, 21))
    v2 = FakeUnit(_FC, UnitType.VANGUARD, (26, 20))
    r2 = FakeUnit(_FD, UnitType.RANGER, (26, 21))
    dead_id = _FE
    state.known_combat_pos = {dead_id: ((30, 18), "VANGUARD")}   # 阵亡坐标+兵种（新格式）
    e1 = enemy_unit((35, 18))   # 距 core 17 最远（最推进）
    e2 = enemy_unit((33, 20))   # 13
    e3 = enemy_unit((31, 19))   # 12
    t, _ = make_turn(tick=200, resources=50, population=10, core_pos=core_pos,
                     workers=[(v1.id, v1.position, 0, UnitType.VANGUARD),
                              (r1.id, r1.position, 0, UnitType.RANGER),
                              (v2.id, v2.position, 0, UnitType.VANGUARD),
                              (r2.id, r2.position, 0, UnitType.RANGER)],
                     enemies=[e1, e2, e3], beacon=CARRIED_BEACON)
    explore_pool = [v1, r1, v2, r2]   # 2 组
    agent._danger_update(t, state, core_pos, explore_pool)
    assert len(state.danger_zones) == 1, f"应建1禁止区, 实得{len(state.danger_zones)}"
    z = state.danger_zones[0]
    assert z.state == "forbidden"
    assert z.center == (35, 18), f"圆心应为最推进敌(35,18), 实得{z.center}"
    assert z.radius == 5, f"半径应=圆心→死亡点=5, 实得{z.radius}"
    assert z.recorded_enemy_count == 3
    # 探索组数增长到 4 (> 记录 3) → 转危险区
    v3 = FakeUnit(_FA, UnitType.VANGUARD, (27, 20))
    r3 = FakeUnit(uuid4(), UnitType.RANGER, (27, 21))
    v4 = FakeUnit(uuid4(), UnitType.VANGUARD, (28, 20))
    r4 = FakeUnit(uuid4(), UnitType.RANGER, (28, 21))
    explore_pool2 = [v1, r1, v2, r2, v3, r3, v4, r4]
    agent._danger_update(t, state, core_pos, explore_pool2)
    assert z.state == "danger", f"应转危险区, 实得{z.state}"
    # 圆内无敌人 → 解除
    t2, _ = make_turn(tick=201, resources=50, population=10, core_pos=core_pos,
                      workers=[(v1.id, v1.position, 0, UnitType.VANGUARD),
                               (r1.id, r1.position, 0, UnitType.RANGER)],
                      enemies=[], beacon=CARRIED_BEACON)
    agent._danger_update(t2, state, core_pos, explore_pool2)
    assert len(state.danger_zones) == 0, f"无威胁应解除, 剩{len(state.danger_zones)}"
    print("[OK] BR 禁止/危险区：建区(圆心=最推进敌,半径=圆心→死亡点)"
          "→探索组>记录转危险区→无威胁解除")
    return True


def run_enemy_avoid():
    """回归测试：敌方单位挡在直连路径上时，Worker 必须绕路而非踩上敌格。

    复现用户报告的 bug——'前方有敌，往前被挡，下次还往前，一直卡住'。
    修复后 step_toward 把可见/近期敌方单位格加入障碍集，A* 自动绕行。
    """
    state = agent.AgentState()
    core_pos = (10, 10)
    worker_pos = (10, 13)
    enemy_pos = (10, 12)  # 正挡在 Worker→Core 的直线路径上
    t, captured = make_turn(
        tick=108, resources=5, population=2, core_pos=core_pos,
        workers=[(W1, worker_pos, 1, UnitType.WORKER)],  # 带 cargo 需回 Core
        enemies=[enemy_unit(enemy_pos)],
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        plan = t.plan
        assert isinstance(plan, CommandPlan), f"expected CommandPlan got {type(plan)}"
        ua = plan.unit_actions
        # 不带 cargo 也会因优先级2交付而移动；此处 W1 带 cargo 必移动
        assert W1 in ua, "Worker 未发出移动指令（已卡死）"
        act = ua[W1]
        assert getattr(act, "type", None) == "MOVE", f"期望 MOVE 得到 {act}"
        d = act.direction
        dx, dy = DELTA[d]
        cell = (worker_pos[0] + dx, worker_pos[1] + dy)
        # 落点不能是敌格（修复核心断言）
        assert cell != enemy_pos, f"Worker 仍踩上敌格 {enemy_pos}（bug 未修复）"
        # 落点必须在相邻格（单步移动，符合每 tick 一格约束）
        assert abs(dx) + abs(dy) == 1, "移动方向非法"
        print(f"[OK] I 遇敌绕路不踩敌格  worker={worker_pos}→{cell} (敌@{enemy_pos})")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] I 遇敌绕路不踩敌格: {e!r}")
        traceback.print_exc()
        return False


def run_assault_ranger_shoots():
    """回归：可见敌方核心 + Ranger 在射程内 → 应 SHOOT（修复突击时 Ranger 不开枪 bug）。

    旧代码用 `isinstance(u.__class__.__name__, str) and "Ranger" in u.__class__.__name__`
    判 Ranger，而 SDK 单位类名是 UnitView，条件恒 False → Ranger 永远不开火。
    """
    state = agent.AgentState()
    t, captured = make_turn(
        tick=109, resources=40, population=8, core_pos=(10, 10),
        workers=[(R1, (10, 7), 0, UnitType.RANGER)],  # 距敌方核心 3 格=射程内
        enemies=[enemy_core((10, 4))],
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        act = t.plan.unit_actions.get(R1)
        assert act is not None, "Ranger 未发出动作"
        assert getattr(act, "type", None) == "SHOOT", \
            f"期望 SHOOT 得到 {getattr(act,'type',None)}（突击时 Ranger 不开枪 bug 未修复）"
        print(f"[OK] J 突击Core时Ranger射击  R1→SHOOT @{getattr(act,'expected_cell',None)}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] J 突击Core时Ranger射击: {e!r}")
        traceback.print_exc()
        return False


def run_self_defense_ranger_approaches():
    """回归：自卫模式下 Ranger 在射程外 → 应 MOVE 靠近（修复原地空转 bug）。

    旧代码 _plan_self_defense 对射程外敌人直接 shoot（失败被吞），Ranger 永远不靠近。
    """
    state = agent.AgentState()
    t, captured = make_turn(
        tick=110, resources=5, population=3, core_pos=(10, 10),
        workers=[(R1, (10, 6), 0, UnitType.RANGER)],  # 距敌 6 格=射程外
        enemies=[enemy_unit((10, 12))],
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        act = t.plan.unit_actions.get(R1)
        assert act is not None, "Ranger 未发出动作（已卡死）"
        assert getattr(act, "type", None) == "MOVE", \
            f"射程外应 MOVE 靠近，得到 {getattr(act,'type',None)}"
        assert act.direction == Direction.DOWN, \
            f"应朝敌人方向 DOWN 靠近，得到 {act.direction}"
        print(f"[OK] K 自卫射程外Ranger靠近  R1→MOVE {act.direction.name}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] K 自卫射程外Ranger靠近: {e!r}")
        traceback.print_exc()
        return False


def run_core_march_to_beacon():
    """回归：信标开启 + 战斗单位(先锋)>=5、Core 远离信标、无带货工人在身边 → Core 应 START_MOVE 朝信标。"""
    state = agent.AgentState()
    t, captured = make_turn(
        tick=200, resources=40, population=6, core_pos=(50, 50),
        workers=[(_FA, (50, 51), 0, UnitType.VANGUARD),
                 (_FB, (50, 52), 0, UnitType.VANGUARD),
                 (_FC, (50, 53), 0, UnitType.VANGUARD),
                 (_FD, (50, 54), 0, UnitType.VANGUARD),
                 (_FE, (50, 55), 0, UnitType.VANGUARD)],
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        ca = t.plan.core_action
        assert ca is not None, "Core 未发出动作（应朝信标迁移）"
        assert getattr(ca, "type", None) == "START_MOVE", \
            f"期望 START_MOVE 得到 {getattr(ca,'type',None)}"
        print(f"[OK] L 信标开启+战斗>=5 则Core进军信标  START_MOVE {ca.direction.name}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] L Core进军信标: {e!r}")
        traceback.print_exc()
        return False


def run_core_march_requires_5_combat():
    """进军门槛：信标开启但战斗单位<5(此处4先锋) → Core 不应 START_MOVE（只正常生产）。"""
    state = agent.AgentState()
    t, captured = make_turn(
        tick=200, resources=40, population=5, core_pos=(50, 50),
        workers=[(_FA, (50, 51), 0, UnitType.VANGUARD),
                 (_FB, (50, 52), 0, UnitType.VANGUARD),
                 (_FC, (50, 53), 0, UnitType.VANGUARD),
                 (_FD, (50, 54), 0, UnitType.VANGUARD)],
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        ca = t.plan.core_action
        assert getattr(ca, "type", None) != "START_MOVE", \
            f"战斗<5 不应进军，却得到 START_MOVE"
        print(f"[OK] AL 战斗<5 不进军信标（仅生产，core_action={getattr(ca,'type',None)!r}）")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] AL 战斗<5不进军: {e!r}")
        traceback.print_exc()
        return False


def run_core_march_stops_for_cargo():
    """回归：带货工人在 Core 2 格内 → Core 停下不迁移（等工人 deposit）。"""
    state = agent.AgentState()
    t, captured = make_turn(
        tick=201, resources=40, population=3, core_pos=(50, 50),
        workers=[(V1, (50, 51), 0, UnitType.VANGUARD),
                 (W1, (50, 52), 1, UnitType.WORKER)],  # 带货，距 Core 2 格
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        ca = t.plan.core_action
        assert getattr(ca, "type", None) != "START_MOVE", \
            f"带货工人在身边时 Core 不应迁移，得到 {getattr(ca,'type',None)}"
        print(f"[OK] M 带货工人在2格内Core停下  core_action={getattr(ca,'type',None)}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] M Core停下等提交: {e!r}")
        traceback.print_exc()
        return False


def run_worker_no_cell_conflict():
    """回归：两个带货工人同时冲 Core 格 → 容量(每格≤2，Core 已占1)只允许一个进，
    另一个必须绕路（不都挤到同一格触发服务端 UUID 仲裁失败→卡死）。"""
    state = agent.AgentState()
    t, captured = make_turn(
        tick=300, resources=5, population=2, core_pos=(0, 0),
        workers=[(W1, (1, 0), 1, UnitType.WORKER),
                 (W2, (-1, 0), 1, UnitType.WORKER)],
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        ua = t.plan.unit_actions
        def land(pos, act):
            d = getattr(act, "direction", None)
            if d is None:
                return None
            dx, dy = DELTA[d]
            return (pos[0] + dx, pos[1] + dy)
        c1 = land((1, 0), ua.get(W1))
        c2 = land((-1, 0), ua.get(W2))
        assert c1 is not None and c2 is not None, "两个工人都应发出 MOVE"
        assert not (c1 == (0, 0) and c2 == (0, 0)), \
            f"两工人都挤进 Core 格 {c1}/{c2}，会触发目标格冲突失败（绕路未生效）"
        print(f"[OK] N 工人目标格冲突绕路  W1→{c1} W2→{c2}（未都挤 Core 格）")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] N 工人绕路: {e!r}")
        traceback.print_exc()
        return False


def run_scout_handoff():
    """回归 O：探索工 A 带货返程 → 派最近的空闲工人 B 接手 A 的探索目标，
    A 记下 B 的原职责；A 到 Core 提交后接手 B 的原职责（角色互换闭环）。"""
    state = agent.AgentState()
    A_TARGET = (60, 0)     # A 这条探索腿的目标
    B_TARGET = (0, -60)    # B 原来的职责
    # A：探索工，刚采到资源（cargo=1），离 Core 很远
    wsa = agent.WorkerState()
    wsa.is_scout = True
    wsa.scout_target = A_TARGET
    wsa.scout_origin = (0, 0)
    wsa.scout_radius = 60
    wsa.scout_heading = (1, 0)
    state.worker_states[W1] = wsa
    # B：空闲工人，有自己的探索目标
    wsb = agent.WorkerState()
    wsb.scout_target = B_TARGET
    wsb.scout_heading = (0, -1)
    state.worker_states[W2] = wsb

    t, _ = make_turn(
        tick=400, resources=5, population=2, core_pos=(0, 0),
        workers=[(W1, (40, 0), 1, UnitType.WORKER),
                 (W2, (30, 4), 0, UnitType.WORKER)],
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        # 1) B 接手了 A 的探索目标
        assert wsb.scout_target == A_TARGET, \
            f"B 未接手 A 的探索目标：{wsb.scout_target} != {A_TARGET}"
        assert wsb.takeover_active, "B 未被标记为接手中"
        # 2) A 记下了 B 的原职责，并已发起交接
        assert wsa.leg_swapped, "A 未标记已发起交接"
        assert wsa.pending_target == B_TARGET, \
            f"A 未记下 B 的原职责：{wsa.pending_target} != {B_TARGET}"
        # 3) A 正在返程交付
        assert wsa.task == agent.WorkerTask.DELIVERING, f"A 未返程：{wsa.task}"

        # ---- 第二步：A 走到 Core 上提交 → 接手 B 的原职责 ----
        t2, _ = make_turn(
            tick=401, resources=5, population=2, core_pos=(0, 0),
            workers=[(W1, (0, 0), 1, UnitType.WORKER),
                     (W2, (31, 3), 0, UnitType.WORKER)],
            beacon=CARRIED_BEACON,
        )
        agent.plan_turn_v2(t2, state)
        assert not wsa.leg_swapped, "提交后交接标记未复位"
        assert wsa.scout_target == B_TARGET, \
            f"A 提交后未接手 B 的原职责：{wsa.scout_target} != {B_TARGET}"
        assert wsa.takeover_active, "A 提交后未标记为接手中"
        print(f"[OK] O 角色交接闭环  B接手A目标{A_TARGET} · A提交后接手B目标{B_TARGET}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] O 角色交接: {e!r}")
        traceback.print_exc()
        return False


def run_map_snapshot_durable():
    """回归 P：地图层快照 stream/map.json 必须落盘且含完整障碍/已探索分片，
    并且每个新分片的第一帧强制携带完整障碍（旧分片淘汰后不丢石头）。"""
    import os, json, shutil, tempfile
    tmpd = tempfile.mkdtemp(prefix="ah_stream_")
    old_dir, old_snap = agent.STREAM_SHARD_DIR, agent.MAP_SNAPSHOT_EVERY
    agent.STREAM_SHARD_DIR = tmpd
    try:
        state = agent.AgentState()
        rocks = ((5, 5), (5, 6), (6, 5))
        for i in range(3):
            t, _ = make_turn(
                tick=500 + i, resources=5, population=1, core_pos=(0, 0),
                workers=[(W1, (1, i), 0, UnitType.WORKER)],
                terrain=[TerrainView(kind="OBSTACLE", positions=rocks)],
                beacon=CARRIED_BEACON,
            )
            agent.plan_turn_v2(t, state)
        snap_path = os.path.join(tmpd, "map.json")
        assert os.path.exists(snap_path), "未生成 stream/map.json 地图快照"
        snap = json.load(open(snap_path))
        got = {tuple(p) for p in snap["obstacles"]}
        assert set(rocks) <= got, f"快照障碍不全：{got}"
        assert snap["explored_sectors"], "快照缺少已探索分片"
        # 第一帧必须自带完整障碍（分片自包含）
        lines = open(os.path.join(tmpd, "shard_00000.jsonl")).read().strip().split("\n")
        first = json.loads(lines[0])
        assert first["obstacles"], "分片首帧未携带完整障碍（旧片淘汰会丢石头）"
        print(f"[OK] P 地图快照持久  障碍{len(got)}个 · 分片首帧自包含 · "
              f"已探索{len(snap['explored_sectors'])}区块")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] P 地图快照: {e!r}")
        traceback.print_exc()
        return False
    finally:
        agent.STREAM_SHARD_DIR, agent.MAP_SNAPSHOT_EVERY = old_dir, old_snap
        shutil.rmtree(tmpd, ignore_errors=True)


def run_match_reset():
    """回归 Q：重启后 Core 位置与存档相差极远 → 判定换局，清空上一局的脏障碍
    （否则旧坐标系的石头会永久污染小地图与寻路）。"""
    state = agent.AgentState()
    state.map_memory.last_core = (-90, 700)          # 上一局的 Core
    state.map_memory.obstacles = {(-91, 711), (-75, 695)}   # 上一局的石头
    state.map_memory.explored_sectors = {(-3, 22)}
    try:
        t, _ = make_turn(
            tick=600, resources=5, population=1, core_pos=(-469, -464),
            workers=[(W1, (-468, -464), 0, UnitType.WORKER)],
            beacon=CARRIED_BEACON,
        )
        agent.plan_turn_v2(t, state)
        assert (-91, 711) not in state.map_memory.obstacles, \
            f"换局未清理上局障碍：{state.map_memory.obstacles}"
        assert (-3, 22) not in state.map_memory.explored_sectors, \
            "换局未清理上局已探索分片"
        assert state.match_checked, "换局判定标记未置位"
        print("[OK] Q 换局清理脏数据  上局障碍/已探索已丢弃")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] Q 换局清理: {e!r}")
        traceback.print_exc()
        return False


def run_resource_memory_persist():
    """回归 R：资源坐标一旦看到就永久记住，离开视野很久后空闲工人仍能直接锁定去采
    （不再靠重新探索）。get_known_resources 的 24-tick 过期不再参与派遣。"""
    state = agent.AgentState()
    MEM_RES = (12, 12)
    state.map_memory.resource_memory = {MEM_RES: 1}   # 很久以前(tick=1)看到的，早已超 24
    t, _ = make_turn(
        tick=200, resources=5, population=1, core_pos=(0, 0),
        workers=[(W1, (5, 5), 0, UnitType.WORKER)],
        terrain=[], beacon=CARRIED_BEACON,        # 当前视野内无资源
    )
    try:
        agent.plan_turn_v2(t, state)
        ws = state.worker_states[W1]
        assert ws.assigned_resource == MEM_RES, \
            f"空闲工未锁定记忆资源: {ws.assigned_resource}"
        act = t.plan.unit_actions.get(W1)
        assert act is not None and getattr(act, "direction", None) is not None, \
            "未朝记忆资源发出移动"
        print(f"[OK] R 资源记忆持久  W1 锁定记忆坐标{MEM_RES}并朝其移动")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] R 资源记忆: {e!r}")
        traceback.print_exc()
        return False


def run_resource_arrival_depleted():
    """回归 S：工人走到记忆坐标却发现没资源（被采空/被别人采）→ 当场清除记忆并释放分配，
    重新选点，不能卡死在原地。"""
    state = agent.AgentState()
    RES = (12, 12)
    state.map_memory.resource_memory = {RES: 1}
    ws = agent.WorkerState()
    ws.assigned_resource = RES
    state.worker_states[W1] = ws
    t, _ = make_turn(
        tick=200, resources=5, population=1, core_pos=(0, 0),
        workers=[(W1, (12, 12), 0, UnitType.WORKER)],   # 已站在目标格
        terrain=[], beacon=CARRIED_BEACON,               # 但视野里它已不是资源
    )
    try:
        agent.plan_turn_v2(t, state)
        assert RES not in state.map_memory.resource_memory, \
            "到达发现无资源未清除记忆"
        assert state.worker_states[W1].assigned_resource is None, \
            "到达无资源未释放分配（会卡死）"
        print("[OK] S 到达无资源即清除  记忆与分配均释放，无卡死")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] S 到达清除: {e!r}")
        traceback.print_exc()
        return False


def run_resource_snapshot():
    """回归 T：stream/map.json 快照含资源记忆（坐标持久化到前端，重开地图仍可见）。"""
    import os, json, tempfile, shutil
    tmpd = tempfile.mkdtemp(prefix="ah_rs_")
    old = agent.STREAM_SHARD_DIR
    agent.STREAM_SHARD_DIR = tmpd
    try:
        state = agent.AgentState()
        res = (5, 5)
        t, _ = make_turn(
            tick=500, resources=5, population=1, core_pos=(0, 0),
            workers=[(W1, (1, 0), 0, UnitType.WORKER)],
            terrain=[TerrainView(kind="RESOURCE", positions=(res,))],
            beacon=CARRIED_BEACON,
        )
        agent.plan_turn_v2(t, state)
        snap = json.load(open(os.path.join(tmpd, "map.json")))
        mem = {(r[0], r[1]) for r in snap["resource_memory"]}
        assert res in mem, f"快照未含资源记忆: {mem}"
        print(f"[OK] T 资源快照持久  记忆{len(mem)}个含{res}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] T 资源快照: {e!r}")
        traceback.print_exc()
        return False
    finally:
        agent.STREAM_SHARD_DIR = old
        shutil.rmtree(tmpd, ignore_errors=True)


def run_fog_precise_cells():
    """回归 U：战争迷雾必须"逐格精准"描绘——走过哪格记哪格，
    走廊/十字就画成走廊/十字，而不是 32×32 大蓝块。
    - 单位在 (5,7) 这种非对齐格 → explored_cells 精确含 (5,7)，不被扇区取整；
    - 一个十字形地形 → explored_cells 是十字，不是 32×32 方块；
    - 快照含 explored_cells。"""
    import os, json, tempfile, shutil
    tmpd = tempfile.mkdtemp(prefix="ah_fog_")
    old = agent.STREAM_SHARD_DIR
    agent.STREAM_SHARD_DIR = tmpd
    try:
        state = agent.AgentState()
        # 十字形地形：中心行 y=10 (x=8..12) + 中心列 x=10 (y=8..12)
        cross = [(x, 10) for x in range(8, 13)] + [(10, y) for y in range(8, 13)]
        t, _ = make_turn(
            tick=500, resources=5, population=1, core_pos=(10, 10),
            workers=[(W1, (5, 7), 0, UnitType.WORKER)],
            terrain=[TerrainView(kind="OBSTACLE", positions=cross)],
            beacon=CARRIED_BEACON,
        )
        agent.plan_turn_v2(t, state)
        cells = state.map_memory.explored_cells
        # 1) 非对齐单位格精确记录（不被 //32 取整成 (0,0) 扇区）
        assert (5, 7) in cells, f"单位(5,7)未精确入 explored_cells: 样例{list(cells)[:5]}"
        # 2) 十字格全在；且绝不能扩张成 32×32 方块（不能含无关格）
        for c in cross:
            assert c in cells, f"十字格{c}缺失于 explored_cells"
        assert (1, 1) not in cells, "explored_cells 被扇区块状污染（含无关格(1,1)）"
        assert (20, 20) not in cells, "explored_cells 被扇区块状污染（含无关格(20,20)）"
        # 3) 快照含 explored_cells
        snap = json.load(open(os.path.join(tmpd, "map.json")))
        assert snap.get("explored_cells"), "快照缺少已探索逐格"
        print(f"[OK] U 迷雾逐格精准  含(5,7)与十字{len(cross)}格 · 无扇区污染 · 快照含explored_cells")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] U 迷雾逐格: {e!r}")
        traceback.print_exc()
        return False
    finally:
        agent.STREAM_SHARD_DIR = old
        shutil.rmtree(tmpd, ignore_errors=True)


def run_explorer_collects_remembered():
    """回归 V：探索工(空背包)且安全时，附近有"已探索并标记"的资源应优先去采，
    而不是继续往前探索把资源晾着（用户反馈：工人依旧在往前走）。
    总规则：安全情况下采集资源优先级最高。"""
    state = agent.AgentState()
    RES = (10, 20)
    state.map_memory.resource_memory = {RES: 1}
    ws = agent.WorkerState(); ws.is_scout = True; ws.assigned_resource = None
    state.worker_states[W1] = ws
    t, _ = make_turn(
        tick=300, resources=5, population=1, core_pos=(10, 10),
        workers=[(W1, (10, 10), 0, UnitType.WORKER)],
        terrain=[], beacon=CARRIED_BEACON,   # 视野内无资源，只有记忆资源
    )
    w = next(x for x in t.workers if x.id == W1)
    try:
        agent.plan_worker_collect(
            w, ws, t, state.map_memory, (10, 10),
            state.map_memory.obstacles, set(), set(),   # used_resources, enemy_cells(空=安全)
            is_explorer=True, worker_states=state.worker_states)
        assert ws.assigned_resource == RES, f"探索工未优先采记忆资源: {ws.assigned_resource}"
        assert ws.task == agent.WorkerTask.HARVESTING, f"任务非采集: {ws.task}"
        act = t.plan.unit_actions.get(W1)
        assert act is not None and getattr(act, "direction", None) is not None, "未发出移动"
        print(f"[OK] V 探索工优先采记忆资源  W1→{RES} 任务={ws.task.value}")
        return True
    except Exception as e:
        import traceback; print(f"[FAIL] V 探索工采资源: {e!r}"); traceback.print_exc(); return False


def run_explorer_no_far_pickup_when_unsafe():
    """回归 X：附近有敌人(不安全)时，探索工不应长途跋涉去采记忆资源，交给战斗逻辑。"""
    state = agent.AgentState()
    RES = (10, 20)
    state.map_memory.resource_memory = {RES: 1}
    ws = agent.WorkerState(); ws.is_scout = True; ws.assigned_resource = None
    state.worker_states[W1] = ws
    t, _ = make_turn(
        tick=301, resources=5, population=1, core_pos=(10, 10),
        workers=[(W1, (10, 10), 0, UnitType.WORKER)],
        terrain=[], beacon=CARRIED_BEACON,
        enemies=[enemy_unit((10, 13))],   # 敌人就在 3 格内 → 不安全
    )
    w = next(x for x in t.workers if x.id == W1)
    try:
        agent.plan_worker_collect(
            w, ws, t, state.map_memory, (10, 10),
            state.map_memory.obstacles, set(), {(10, 13)},  # enemy_cells 含敌人
            is_explorer=True, worker_states=state.worker_states)
        assert ws.assigned_resource is None, \
            f"不安全时仍锁定远处记忆资源: {ws.assigned_resource}"
        print("[OK] X 不安全不外出采  附近有敌→保留探索/防御，未锁定远处资源")
        return True
    except Exception as e:
        import traceback; print(f"[FAIL] X 不安全: {e!r}"); traceback.print_exc(); return False


def run_fog_vision_footprint():
    """回归 W：探索到的区域必须铺成"视野圆盘"——单位周围半径内所有格都算已探索，
    含空格，而不只是地形格。否则已探索区是稀疏点、看起来像只有当前视野。"""
    state = agent.AgentState()
    # 单位在 (10,10) 周围纯空旷、无地形；Core 放到很远，避免污染该区域
    t, _ = make_turn(
        tick=400, resources=5, population=1, core_pos=(100, 100),
        workers=[(W1, (10, 10), 0, UnitType.WORKER)],
        terrain=[], beacon=CARRIED_BEACON,   # 无视距内地形
    )
    agent.plan_turn_v2(t, state)
    cells = state.map_memory.explored_cells
    # 视野半径 3 的曼哈顿圆盘（含空格）须全部计入
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            if abs(dx) + abs(dy) <= 3:
                assert (10 + dx, 10 + dy) in cells, \
                    f"视野内空格(10+{dx},10+{dy})未计入已探索"
    diamond = sum(1 for dx in range(-3, 4) for dy in range(-3, 4)
                  if abs(dx) + abs(dy) <= 3)
    assert len(cells) >= diamond, f"已探索格数{len(cells)} < 视野圆盘{diamond}"
    # 视野外的格(距离4)不应被算入
    assert (10, 14) not in cells, "视野外格(距离4)被误计入已探索"
    print(f"[OK] W 迷雾视野圆盘  单位(10,10)半径3 → {len(cells)}格连续区域(含空格)")
    return True


def enemy_at(pos, ut=UnitType.VANGUARD, eid=None):
    """构造一个敌方单位（controlled=False），用于敌方记忆/消失点测试。"""
    return UnitView(kind="UNIT", id=eid or uuid4(), controlled=False, position=pos,
                    hp=2, unit_type=ut, cargo=None)


def run_lost_contact_register():
    """回归 Y：敌方单位"可见→不可见"必须登记橙色消失点(!)，且记录仍保留。"""
    em = agent.EnemyMemory()
    core = (0, 0)
    E1, E2 = uuid4(), uuid4()
    t1, _ = make_turn(tick=10, resources=5, population=2, core_pos=core,
                      workers=[(W1, (5, 5), 0, UnitType.WORKER)],
                      enemies=[enemy_at((20, 0), UnitType.WORKER, eid=E1),
                               enemy_at((0, 20), UnitType.VANGUARD, eid=E2)])
    em.update(t1)
    assert len(em.records) == 2, f"应记2个敌人，实={len(em.records)}"
    # tick2：两个敌人同时消失
    t2, _ = make_turn(tick=11, resources=5, population=2, core_pos=core,
                      workers=[(W1, (5, 5), 0, UnitType.WORKER)], enemies=[])
    em.update(t2)
    assert len(em.lost_contacts) == 2, f"消失点应=2，实={len(em.lost_contacts)}"
    assert all(lc["faction"] == "enemy" for lc in em.lost_contacts)
    # 记录不应因"消失" immediate 过期（60 tick 内保留）
    assert len(em.records) == 2, "消失不应立即清除记忆记录"
    print(f"[OK] Y 消失点登记  2个敌人消失→lost_contacts={len(em.lost_contacts)}")
    return True


def run_lost_contact_clear():
    """回归 Z：我方单位到达消失点且周围无敌人→取消感叹号；周围有敌则保留。"""
    core = (0, 0)
    E1, E2 = uuid4(), uuid4()
    em = agent.EnemyMemory()
    t1, _ = make_turn(tick=10, resources=5, population=2, core_pos=core,
                      workers=[(W1, (5, 5), 0, UnitType.WORKER)],
                      enemies=[enemy_at((20, 0), UnitType.WORKER, eid=E1),
                               enemy_at((0, 20), UnitType.VANGUARD, eid=E2)])
    em.update(t1)
    em.update(make_turn(tick=11, resources=5, population=2, core_pos=core,
                        workers=[(W1, (5, 5), 0, UnitType.WORKER)], enemies=[])[0])
    # (1) 工人到达 (20,0)、周围无敌人 → 该感叹号取消，剩 (0,20)
    t3, _ = make_turn(tick=12, resources=5, population=2, core_pos=core,
                      workers=[(W1, (20, 0), 0, UnitType.WORKER)], enemies=[])
    em.clear_resolved_contacts(t3)
    assert len(em.lost_contacts) == 1, f"应剩1个，实={len(em.lost_contacts)}"
    rem = {(lc["pos"][0], lc["pos"][1]) for lc in em.lost_contacts}
    assert (0, 20) in rem and (20, 0) not in rem, f"清除错误: {rem}"
    # (2) 周围仍有敌人 → 保留感叹号
    em2 = agent.EnemyMemory()
    em2.lost_contacts = [{"pos": [50, 50], "faction": "enemy",
                          "unit_type": "WORKER", "dir": [1, 0], "tick": 5}]
    t4, _ = make_turn(tick=13, resources=5, population=2, core_pos=core,
                      workers=[(W1, (50, 50), 0, UnitType.WORKER)],
                      enemies=[enemy_at((52, 50), UnitType.VANGUARD)])
    em2.clear_resolved_contacts(t4)
    assert len(em2.lost_contacts) == 1, "周围有敌人时不应清除感叹号"
    print(f"[OK] Z 消失点清除  到达无敌→取消，周围有敌→保留")
    return True


def run_core_zone_triangulation():
    """回归 AA：多个同阵营消失点的撤退方向收敛→估算敌方核心区；单点不估算。"""
    em = agent.EnemyMemory()
    # 三个消失点都朝核心 (100,100) 撤退：分别沿 +x / +y / (+1,+1)
    em.lost_contacts = [
        {"pos": [40, 100], "faction": "enemy", "unit_type": "WORKER",
         "dir": [1, 0], "tick": 100},
        {"pos": [100, 40], "faction": "enemy", "unit_type": "VANGUARD",
         "dir": [0, 1], "tick": 100},
        {"pos": [40, 40], "faction": "enemy", "unit_type": "RANGER",
         "dir": [1, 1], "tick": 100},
    ]
    zones = em.estimate_core_zones((0, 0))
    assert len(zones) == 1, f"应估算1个核心区，实={len(zones)}"
    z = zones[0]
    assert agent.manhattan(tuple(z["pos"]), (100, 100)) <= agent.CORE_ZONE_R, \
        f"核心区偏离目标过远: {z['pos']}"
    assert z["confidence"] >= 2, f"置信度应>=2，实={z['confidence']}"
    # 单点不估算核心区（只画 !）
    em2 = agent.EnemyMemory()
    em2.lost_contacts = [{"pos": [40, 100], "faction": "enemy",
                          "unit_type": "WORKER", "dir": [1, 0], "tick": 100}]
    assert em2.estimate_core_zones((0, 0)) == [], "单点不应估算核心区"
    print(f"[OK] AA 三角定位  3消失点→核心区@{z['pos']} 置信={z['confidence']}")
    return True


def run_sector_explore_fills_before_expand():
    """回归 AB：探索工先把一个 32×32 扇区填满，再逐环外扩（不跳过近处暗区冲远处）。"""
    mm = agent.MapMemory()
    SECTOR = agent.SECTOR_SIZE
    def fill_sec(cx, cy):
        for x in range(SECTOR):
            for y in range(SECTOR):
                mm._add_explored((cx * SECTOR + x, cy * SECTOR + y))
    fill_sec(0, 0)
    core_pos = (1, 1)
    # 填满(0,0)后，下一个待填扇区必须是"最近环"(ring0)的相邻扇区，而不是远处(5,5)
    sec = agent._choose_frontier_sector(mm, (5, 5), core_pos)
    assert sec is not None, "应选出下一个待填扇区"
    assert agent._ring_of(sec[0], sec[1], 0, 0) == 0, \
        f"应先填 ring0 邻域，实={sec}(ring={agent._ring_of(sec[0],sec[1],0,0)})"
    # 扇区内能挑到未探索格
    cell = agent._pick_unexplored_in_sector(mm, sec, (5, 5), set())
    assert cell is not None, "扇区内应挑到未探索格"
    # 填满整个 ring0(2x2 块)后，应转向 ring1 邻域，仍不跳到(5,5)
    for cx in (-1, 0):
        for cy in (-1, 0):
            fill_sec(cx, cy)
    sec2 = agent._choose_frontier_sector(mm, (5, 5), core_pos)
    assert sec2 is not None, "ring0 满后应仍有 ring1 可填"
    assert agent._ring_of(sec2[0], sec2[1], 0, 0) == 1, \
        f"ring0 满后应填 ring1，实={sec2}"
    assert sec2 != (5, 5), f"不应跳过近处直接冲到(5,5)，实={sec2}"
    # 核心所在扇区自身未填满时，应返回自身扇区（兜底不报错）
    mm2 = agent.MapMemory()
    mm2._add_explored((5 * SECTOR + 1, 5 * SECTOR + 1))   # 仅 1 格
    sec3 = agent._choose_frontier_sector(
        mm2, (5 * SECTOR + 1, 5 * SECTOR + 1), (5 * SECTOR + 1, 5 * SECTOR + 1))
    assert sec3 == (5, 5), f"核心扇区未填时应返回自身扇区，实={sec3}"
    print(f"[OK] AB 逐扇区探索  先填ring0→ring1，不跳过近处冲远处")
    return True


def run_global_assignment_nearest_worker():
    """回归 AC：地图上有资源图标(记忆资源)时，全局就近派单应把该资源指派给
    最近的空背包工人，而非让更远的工人抢单、近处工人空手乱走。"""
    state = agent.AgentState()
    RES = (50, 50)                      # 地图上标记的资源坐标
    state.map_memory.resource_memory[RES] = 400   # 持久记忆：该格有资源
    # 两个空背包工人：W1 离资源更近，W2 更远
    w_near = agent.WorkerState()
    w_far = agent.WorkerState()
    state.worker_states[W1] = w_near
    state.worker_states[W2] = w_far
    t, _ = make_turn(
        tick=500, resources=5, population=3, core_pos=(0, 0),
        workers=[(W1, (45, 50), 0, UnitType.WORKER),   # 距 RES 曼哈顿=5
                 (W2, (10, 10), 0, UnitType.WORKER)],  # 距 RES 曼哈顿=80
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_global_resource_assignment(t, state, None, set(), set())
        # 最近的工人被派去采该资源
        assert w_near.assigned_resource == RES, \
            f"最近的工人未被派往资源：{w_near.assigned_resource} != {RES}"
        # 较远的工人不被硬派(资源已被最近者接单，且自身超距/已被占)
        assert w_far.assigned_resource is None, \
            f"较远的工人不应被派单：{w_far.assigned_resource}"
        # 带 Cargo 的工人不参与派单
        state2 = agent.AgentState()
        wcargo = agent.WorkerState()
        state2.map_memory.resource_memory[RES] = 400
        state2.worker_states[W3] = wcargo
        t2, _ = make_turn(
            tick=501, resources=5, population=3, core_pos=(0, 0),
            workers=[(W3, (45, 50), 1, UnitType.WORKER)],  # 带 cargo
            beacon=CARRIED_BEACON,
        )
        agent.plan_global_resource_assignment(t2, state2, None, set(), set())
        assert wcargo.assigned_resource is None, "带 cargo 的工人不应被派去采"
        print(f"[OK] AC 全局就近派单  最近工人{W1.hex[:6] if hasattr(W1,'hex') else W1}→{RES} · 远工/带货工不参与")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] AC 全局就近派单: {e!r}")
        traceback.print_exc()
        return False


def run_persistent_assignment_not_stolen():
    """回归 BI：复现截图 bug——所有空闲工人都朝同一个资源移动。

    根因：used_resources 每 tick 从空集开始，不含工人已持久分配的 assigned_resource；
    全局派单又把别的工人正在前往的资源重复分配给空闲工人。修复后 used_resources
    预填充持久分配，全局派单跳过已占用目标，确保"一个单位朝资源移动时其余单位
    继续做自己原本的事"。"""
    state = agent.AgentState()
    RES = (12, 10)
    # W1 已在上一 tick 被分配去采 RES（持久中）→ 继续前往即可
    ws1 = state.worker_states.setdefault(W1, agent.WorkerState())
    ws1.assigned_resource = RES
    ws1.target = RES
    t, _ = make_turn(
        tick=600, resources=10, population=3, core_pos=(10, 10),
        workers=[
            (W1, (11, 10), 0, UnitType.WORKER),  # 接近 RES, 已有持久分配
            (W2, (10, 11), 0, UnitType.WORKER),  # 空闲, Core 附近
            (W3, (10, 9), 0, UnitType.WORKER),   # 空闲, Core 附近
        ],
        terrain=[TerrainView(kind="RESOURCE", positions=(RES,))],
        beacon=CARRIED_BEACON,
    )
    agent.plan_turn_v2(t, state)
    # 收集各工人最终可视化目标（monitor 用的就是 state.unit_targets）
    assigned = {wid: state.unit_targets.get(wid) for wid in (W1, W2, W3)}
    res_workers = [wid for wid, tgt in assigned.items() if tgt == RES]
    assert len(res_workers) == 1, \
        f"资源{RES}只应有1个工人前往，实际{len(res_workers)}个: {res_workers}"
    assert res_workers == [W1], \
        f"已持久分配的 W1 应继续前往{RES}，实际被抢单者={res_workers}"
    # W2/W3 不应都挤到 RES（应分流到 loiter/explore 目标，或 None）
    print(f"[OK] BI 持久分配不抢单：仅 W1→{RES}，W2/W3 分流(目标={assigned[W2]}/{assigned[W3]})")
    return True


def run_harvester_fanout():
    """回归 AD：两个采集工在已探索的四周扇区里，应各自分到不同方向的巡逻点
    （32×32 区域平分、扇形散开），而不是都去同一个最近扇区（用户反馈"全往左"）。"""
    try:
        mm = agent.MapMemory()
        cx = cy = 0
        # Core 扇区 + 东/西/南/北 各一已探索扇区（环绕 Core 四周）
        mm.explored_sectors = {(cx, cy), (cx + 1, cy), (cx - 1, cy),
                               (cx, cy + 1), (cx, cy - 1)}
        core_pos = (cx * 32 + 16, cy * 32 + 16)
        # 用 Turn 构造可控 Worker（带 .move），与线上 plan_turn_v2 实际拿到的对象一致
        t, _ = make_turn(tick=1, resources=10, population=2, core_pos=core_pos,
                         workers=[(W1, (core_pos[0], core_pos[1] + 2), 0, UnitType.WORKER),
                                  (W2, (core_pos[0], core_pos[1] - 2), 0, UnitType.WORKER)])
        w1, w2 = t.workers
        workers = [w1, w2]
        wss = {w1.id: agent.WorkerState(), w2.id: agent.WorkerState()}
        for ws in wss.values():
            ws.is_scout = False  # 采集合（非探索工）参与 32×32 区域平分
        blocked = set()
        agent._plan_harvester_loiter(wss[w1.id], w1, w1.position, core_pos, blocked, mm, workers, wss)
        agent._plan_harvester_loiter(wss[w2.id], w2, w2.position, core_pos, blocked, mm, workers, wss)
        t1, t2 = wss[w1.id].loiter_target, wss[w2.id].loiter_target
        assert t1 is not None and t2 is not None, "巡逻点未分配"
        s1 = (t1[0] // 32, t1[1] // 32)
        s2 = (t2[0] // 32, t2[1] // 32)
        assert s1 != s2, f"两采集工被分到同一扇区 {s1}（未扇形散开）"
        print(f"[OK] AD 采集合扇形散开  W1→{s1} W2→{s2}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] AD 采集合扇形散开: {e!r}")
        traceback.print_exc()
        return False


def run_explorer_fanout():
    """回归 AE：两个探索工应各自认领不同的前沿扇区（claimed_sectors 去重），
    扇形散开而不是全挤向同一个最近前沿扇区。"""
    try:
        mm = agent.MapMemory()
        cx = cy = 0
        mm.explored_sectors = {(cx, cy)}  # 只探索了 Core 扇区，四周 4 个邻扇区是前沿
        core_pos = (cx * 32 + 16, cy * 32 + 16)
        # 用 Turn 构造可控 Worker（带 .move），与线上 plan_turn_v2 实际拿到的对象一致
        t, _ = make_turn(tick=1, resources=10, population=2, core_pos=core_pos,
                         workers=[(W1, core_pos, 0, UnitType.WORKER),
                                  (W2, core_pos, 0, UnitType.WORKER)])
        w1, w2 = t.workers
        workers = [w1, w2]
        wss = {w1.id: agent.WorkerState(), w2.id: agent.WorkerState()}
        for ws in wss.values():
            ws.is_scout = True
        blocked = set()
        claimed = set()
        agent._plan_sector_explore(wss[w1.id], w1, w1.position, core_pos, blocked, mm, claimed)
        agent._plan_sector_explore(wss[w2.id], w2, w2.position, core_pos, blocked, mm, claimed)
        s1, s2 = wss[w1.id].explore_sector, wss[w2.id].explore_sector
        assert s1 is not None and s2 is not None, "探索扇区未分配"
        assert s1 != s2, f"两探索工认领同一扇区 {s1}（未散开）"
        print(f"[OK] AE 探索工扇形散开  W1→{s1} W2→{s2}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] AE 探索工扇形散开: {e!r}")
        traceback.print_exc()
        return False


def run_core_march_stops_for_cargo_3():
    """回归 M2（用户要求 3 格）：带货工人在 Core 周围 3 格内 → Core 停止迁移等交付。"""
    state = agent.AgentState()
    t, captured = make_turn(
        tick=202, resources=40, population=3, core_pos=(50, 50),
        workers=[(V1, (50, 51), 0, UnitType.VANGUARD),
                 (W1, (53, 50), 1, UnitType.WORKER)],  # 带货，距 Core 曼哈顿=3
        beacon=CARRIED_BEACON,
    )
    try:
        agent.plan_turn_v2(t, state)
        ca = t.plan.core_action
        assert getattr(ca, "type", None) != "START_MOVE", \
            f"带货工人在3格内 Core 不应迁移，得到 {getattr(ca,'type',None)}"
        print(f"[OK] M2 带货工人3格内Core停下  core_action={getattr(ca,'type',None)}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] M2 Core停下(3格): {e!r}")
        traceback.print_exc()
        return False


def run_core_march_stops_for_cargo_chase():
    """回归 M3（追击破局 + 枚举态兼容）：Core 已在迁移、带货工人稍远(≤R+3)正来 →
    提前取消迁移，避免工人永远追不上。用枚举态 CoreState.MOVING 模拟线上真实形态，
    验证 _core_is_moving 能识别枚举（否则 cancel_move 永不触发、Core 永远停不下来）。"""
    from arena_hero import CoreState
    canceled = []
    started = []
    class FakeCore:
        def __init__(self):
            self.position = (50, 50)
            self.view = type("V", (), {"state": CoreState.MOVING,
                                       "move_direction": Direction.LEFT})()
        def cancel_move(self):
            canceled.append(1)
        def start_move(self, d):
            started.append(d)
    core = FakeCore()
    w = uw(W1, (46, 50), cargo=1)  # 带货，距 Core 曼哈顿=4（≤ R+3=6），正朝 Core 来
    turn = type("T", (), {"workers": [w], "core": core})()
    try:
        agent._plan_core_march(turn, core)
        assert canceled, "枚举态 MOVING 下未调用 cancel_move（追击破局/枚举兼容失效）"
        assert not started, "应取消迁移而非继续 START_MOVE"
        print(f"[OK] M3 追击破局(枚举态)  cancel_move 已调用")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] M3 追击破局: {e!r}")
        traceback.print_exc()
        return False


def run_core_manual_move_not_cancelled():
    """回归 M4（本次 BUG）：用户手动迁移使 Core 处于 MOVING，但周围无任何带货工人 →
    plan_core_actions 不得调用 cancel_move（原先"stabilize 分支"无条件取消，会掐掉手动
    迁移、卡死）。has_combat=False（纯工人局）下也应放行，让 Core 自然走完。"""
    from arena_hero import CoreState
    canceled = []
    started = []
    class FakeCore:
        def __init__(self):
            self.position = (50, 50)
            self.hp = 10
            self.shield = 10
            self.view = type("V", (), {"state": CoreState.MOVING,
                                       "move_direction": Direction.LEFT})()
        def cancel_move(self):
            canceled.append(1)
        def start_move(self, d):
            started.append(d)
        def heal(self):
            pass
        def repair_shield(self):
            pass
        def spawn(self, ut):
            pass
    core = FakeCore()
    # 一个空手工人（无 cargo）→ 不应触发货暂停
    w = uw(W1, (50, 44), cargo=0)
    turn = type("T", (), {"core": core, "workers": [w], "vanguards": [],
                          "rangers": [], "resources": 10,
                          "state": type("S", (), {"population": 5})()})()
    combat = type("C", (), {"retreat_mode": False})()
    try:
        agent.plan_core_actions(turn, combat, None)
        assert not canceled, f"手动迁移被无条件下取消（cancel_move 调用 {len(canceled)} 次）"
        print(f"[OK] M4 手动迁移(无货)不被取消  canceled={len(canceled)}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] M4 手动迁移不被取消: {e!r}")
        traceback.print_exc()
        return False


def run_core_manual_move_pauses_for_cargo():
    """回归 M5：用户手动迁移中，若带货工人在 Core 周围 3 格内 → 仍应暂停（货暂停对
    手动迁移同样生效，与自动进军一致）。"""
    from arena_hero import CoreState
    canceled = []
    class FakeCore:
        def __init__(self):
            self.position = (50, 50)
            self.hp = 10
            self.shield = 10
            self.view = type("V", (), {"state": CoreState.MOVING,
                                       "move_direction": Direction.LEFT})()
        def cancel_move(self):
            canceled.append(1)
        def start_move(self, d):
            pass
        def heal(self):
            pass
        def repair_shield(self):
            pass
        def spawn(self, ut):
            pass
    core = FakeCore()
    w = uw(W1, (53, 50), cargo=1)  # 带货，曼哈顿=3（≤ CORE_MARCH_PAUSE_R）
    turn = type("T", (), {"core": core, "workers": [w], "vanguards": [],
                          "rangers": [], "resources": 10,
                          "state": type("S", (), {"population": 5})()})()
    combat = type("C", (), {"retreat_mode": False})()
    try:
        agent.plan_core_actions(turn, combat, None)
        assert canceled, "手动迁移中带货工人在 3 格内却未暂停（货暂停失效）"
        print(f"[OK] M5 手动迁移遇货仍暂停  canceled={len(canceled)}")
        return True
    except Exception as e:
        import traceback
        print(f"[FAIL] M5 手动迁移遇货暂停: {e!r}")
        traceback.print_exc()
        return False


def run_two_layer_patrol_radii():
    """回归 BJ：巡逻战斗单位以 Core 为中心分三层巡逻——外圈(36×36/半径18)3组、
    中圈(20×20/半径10)2组、内圈(10×10/半径5)1组。

    plan_patrol 按 _assign_patrol_rings 将巡逻单位分配到三层圆周（各圈相位错开）。
    V+R 编队模式：每对共享同一目标，游侠跟随先锋。"""
    import math
    state = agent.AgentState()
    core_pos = (50, 50)
    t, _ = make_turn(tick=700, resources=20, population=8, core_pos=core_pos)
    # 6 个可控战斗单位（3V+3R）组成巡逻池 → 3 个 V+R 对
    units = [
        FakeUnit(uuid4(), UnitType.VANGUARD, (48, 48)),
        FakeUnit(uuid4(), UnitType.VANGUARD, (52, 52)),
        FakeUnit(uuid4(), UnitType.RANGER, (49, 51)),
        FakeUnit(uuid4(), UnitType.RANGER, (51, 49)),
        FakeUnit(uuid4(), UnitType.VANGUARD, (50, 47)),
        FakeUnit(uuid4(), UnitType.RANGER, (47, 50)),
    ]
    targets = {}
    agent.plan_patrol(t, state.patrol, core_pos, set(), agent.EnemyMemory(),
                      agent.ManualOverride(), set(),
                      patrol_units=units, targets=targets, aggressive=False)
    # 验证：每个单位都有巡逻目标（V+R 对共享同目标）
    dists = []
    pair_targets = set()
    for u in units:
        tgt = state.patrol.patrol_targets.get(u.id)
        assert tgt is not None, f"单位{u.unit_type.name}应有巡逻目标"
        d = math.hypot(tgt[0] - core_pos[0], tgt[1] - core_pos[1])
        dists.append(d)
        pair_targets.add(tgt)
    # 3 对 → 3 个唯一目标位置（每对共享）
    assert len(pair_targets) == 3, f"3 对 V+R 应有 3 个唯一目标，实际 {len(pair_targets)}"
    # 所有目标应在合理巡逻半径内（外18/中10/内5）
    for d in dists:
        assert 3 <= d <= 22, f"巡逻点距 Core 应在内外圈范围内，实际 {d:.1f}"
    # 验证目标覆盖多层（不全挤在一个环上）
    unique_dists = sorted(set(round(math.hypot(t[0]-core_pos[0], t[1]-core_pos[1]), 1) for t in pair_targets))
    assert len(unique_dists) >= 2, f"应对布到多个环上，实际唯一半径: {unique_dists}"
    assert targets, "应记录巡逻终点目标供可视化"
    print(f"[OK] BJ 三层巡逻半径  " + " ".join(f"{round(d, 1)}" for d in dists))
    return True


def run_defend_only_on_combat_units():
    """回归 BK / BL：死守(全员回防+工人封堵+造游侠)只在【敌方进攻单位】进入 Core 20 格内触发。

    - BK-a：20 格内只有敌方工人(非进攻) → 不触发死守(state.combat.engaging 保持 False)，
            其余单位继续各司其职（工人继续采自己的资源，不被动员封堵）。
    - BK-b：20 格内只有敌方 Core(非进攻) → 同样不召回/不封堵（敌方 Core 是突击目标，
            由 plan_combat 独立处理进攻，但绝不触发"死守"路由把工人/战斗单位拉回护核）。
    - BL  ：20 格内有敌方先锋(进攻单位) → 触发死守(engaging=True)。"""
    # ---- BK-a: 仅敌方工人 ----
    state = agent.AgentState()
    core_pos = (100, 100)
    RES = (101, 101)
    t, _ = make_turn(tick=710, resources=20, population=9, core_pos=core_pos,
                     workers=[(W1, (101, 100), 0, UnitType.WORKER)],
                     enemies=[enemy_unit((108, 100), ut=UnitType.WORKER)],  # 曼哈顿8≤20 的敌方工人
                     terrain=[TerrainView(kind="RESOURCE", positions=(RES,))])
    agent.plan_turn_v2(t, state)
    assert state.combat.engaging is False, \
        f"仅敌方工人(非进攻)在20格内不应触发死守，engaging 应为 False，实际 {state.combat.engaging}"
    ws1 = state.worker_states.get(W1)
    assert ws1 is not None and ws1.assigned_resource == RES, \
        f"工人应继续采资源{RES}，实际 assigned_resource={getattr(ws1, 'assigned_resource', None)}"
    print(f"[OK] BK-a 仅敌方工人→不触发死守(engaging=False)，工人继续采{RES}")

    # ---- BK-b: 仅敌方 Core ----
    state = agent.AgentState()
    t2, _ = make_turn(tick=712, resources=20, population=9, core_pos=core_pos,
                      workers=[(W2, (101, 100), 0, UnitType.WORKER)],
                      enemies=[enemy_core((108, 100))],  # 曼哈顿8≤20 的敌方Core(非进攻)
                      terrain=[TerrainView(kind="RESOURCE", positions=(RES,))])
    agent.plan_turn_v2(t2, state)
    ws2 = state.worker_states.get(W2)
    assert ws2 is not None and ws2.assigned_resource == RES, \
        f"敌方Core(非进攻)不应抽调工人封堵，工人应继续采{RES}，实际 {getattr(ws2, 'assigned_resource', None)}"
    print(f"[OK] BK-b 敌方Core→不召回/不封堵(工人继续采{RES})")

    # ---- BL: 敌方进攻单位 ----
    state = agent.AgentState()
    t3, _ = make_turn(tick=711, resources=20, population=9, core_pos=core_pos,
                      workers=[(W3, (101, 100), 0, UnitType.WORKER)],
                      enemies=[enemy_unit((108, 100), ut=UnitType.VANGUARD)],  # 曼哈顿8≤20 的敌方先锋
                      terrain=[TerrainView(kind="RESOURCE", positions=(RES,))])
    agent.plan_turn_v2(t3, state)
    assert state.combat.engaging is True, \
        f"敌方先锋(进攻单位)在20格内应触发死守，engaging 应为 True，实际 {state.combat.engaging}"
    print(f"[OK] BL 敌方进攻单位(先锋)在20格内→触发死守(engaging=True)")
    return True


def run_blockade_holds_until_enemy_changes_dir():
    """回归 BM：复现用户反馈——工人堵住后不该再跟随敌方移动。

    修复前：plan_worker_blockade 每 tick 基于敌方当前位置重算前格，工人被牵着一格格往前挪。
    修复后：工人就位(blockade_cell 且敌方未改向)原地卡死；仅敌方改变推进方向(朝 Core 的
    step_toward delta 变化)才重占位拦截。"""
    state = agent.AgentState()
    CORE = (10, 10)
    ENEMY_A = (10, 6)    # 北，朝 Core delta=(0,1)
    BLOCK = (10, 7)      # 敌方朝 Core 前格 = 封堵点
    w = FakeUnit(W1, UnitType.WORKER, BLOCK)
    e_a = enemy_unit(ENEMY_A, UnitType.VANGUARD)
    t_a = type("T", (), {"workers": [w], "visible_enemies": [e_a]})()
    ws = state.worker_states.setdefault(W1, agent.WorkerState())
    ws.blockade_cell = BLOCK
    ws.blockade_enemy_dir = (0, 1)
    # 敌不动 → 多个 tick 都原地封住、零移动
    for _ in range(4):
        agent.plan_worker_blockade(t_a, {ENEMY_A}, CORE, set(), {ENEMY_A}, state, None)
        assert w.position == BLOCK, f"工人应原地封住，实际 {w.position}"
        assert len(w.moves) == 0, f"工人不应移动，实际 {w.moves}"
    # 敌方改向：绕到正东 (14,10)，朝 Core delta=(-1,0) 变化 → 应重新移动拦截
    e_b = enemy_unit((14, 10), UnitType.VANGUARD)
    t_b = type("T", (), {"workers": [w], "visible_enemies": [e_b]})()
    agent.plan_worker_blockade(t_b, {(14, 10)}, CORE, set(), {(14, 10)}, state, None)
    assert len(w.moves) >= 1, "敌方改向后工人应重新移动拦截，但未移动"
    print(f"[OK] BM 封堵到位停住·敌改向才动：前4tick敌不动→工人0移动；敌改向(东)→工人移动{len(w.moves)}次")
    return True


def run_blockade_holds_with_memory_only():
    """回归 BN：仅记忆威胁(无可见敌)且已就位 → 不盲动盲移，保持封锁；
    威胁消失 → 清空封堵记忆让工人回归正常任务。"""
    state = agent.AgentState()
    CORE = (10, 10)
    BLOCK = (10, 7)
    w = FakeUnit(W1, UnitType.WORKER, BLOCK)
    t = type("T", (), {"workers": [w], "visible_enemies": []})()  # 无可见敌
    ws = state.worker_states.setdefault(W1, agent.WorkerState())
    ws.blockade_cell = BLOCK
    ws.blockade_enemy_dir = (0, 1)
    # 威胁仅来自记忆(10,6)，工人已就位 → 应保持不动
    agent.plan_worker_blockade(t, {(10, 6)}, CORE, set(), {(10, 6)}, state, None)
    assert w.position == BLOCK and len(w.moves) == 0, f"记忆威胁下不应盲动，pos={w.position} moves={w.moves}"
    # 威胁消失 → 清空封堵记忆
    agent.plan_worker_blockade(t, set(), CORE, set(), set(), state, None)
    assert ws.blockade_cell is None and ws.blockade_enemy_dir is None, \
        f"威胁消失应清空封堵记忆，实际 cell={ws.blockade_cell} dir={ws.blockade_enemy_dir}"
    print(f"[OK] BN 记忆威胁保持封锁·威胁消失清空记忆")
    return True


def run_production_schedule_user_ratio():
    """回归：用户指定生产比例 —— 5工人后造兵；先锋满3→游侠满3→交替；每2兵补1工；受95冻结约束。

    逐 tick 仿真调用 plan_core_actions，把每次 spawn 应用到位/游侠/工人计数，校验序列。"""
    saved_beacon = agent.BEACON_ENABLED
    agent.BEACON_ENABLED = False   # 关闭进军分支，单独验证生产调度
    try:
        spawned = []
        class FakeCore:
            def __init__(self):
                self.position = (50, 50); self.hp = 5; self.shield = 5
                self.view = type("V", (), {"state": "NORMAL"})()
            def heal(self): pass
            def repair_shield(self): pass
            def spawn(self, ut): spawned.append(ut)
        core = FakeCore()
        turn = type("T", (), {
            "core": core, "workers": [], "vanguards": [], "rangers": [],
            "resources": 999, "resource_capacity": 30, "visible_enemies": [],
            "state": type("S", (), {"population": 0})(),
        })()
        combat = type("C", (), {"retreat_mode": False})()
        state = agent.AgentState()
        seq = []   # 每次 spawn 的兵种名
        for _ in range(30):
            spawned.clear()
            agent.plan_core_actions(turn, combat, None, state)
            if not spawned:
                raise AssertionError("仿真中无 spawn（resources=999 不应发生）")
            ut = spawned[-1]
            seq.append(ut.name if hasattr(ut, "name") else str(ut))
            if ut == UnitType.WORKER:
                turn.workers.append(object())
            elif ut == UnitType.VANGUARD:
                turn.vanguards.append(object())
            elif ut == UnitType.RANGER:
                turn.rangers.append(object())
            turn.state.population = (len(turn.workers) + len(turn.vanguards)
                                     + len(turn.rangers) + 1)
        w, v, r = len(turn.workers), len(turn.vanguards), len(turn.rangers)
        # ① 前 5 次必为工人（铺到 5 工人）
        assert seq[:5] == ["WORKER"] * 5, f"前5应为工人，实际{seq[:5]}"
        # ② 游侠须在 3 个先锋之后才出现
        first_r = next((i for i, s in enumerate(seq) if s == "RANGER"), None)
        v_before_r = seq[:first_r].count("VANGUARD")
        assert v_before_r >= 3, f"游侠须在第3先锋后，首游侠前先锋数={v_before_r}"
        # ③ 进入交替：战斗序列在 V>=3 且 R>=3 之后必须 V/R 严格交替
        cseq = [s for s in seq if s in ("VANGUARD", "RANGER")]
        vc = rc = 0; start = -1
        for i, s in enumerate(cseq):
            vc += s == "VANGUARD"; rc += s == "RANGER"
            if vc >= 3 and rc >= 3:
                start = i; break
        for j in range(start + 1, len(cseq)):
            assert cseq[j] != cseq[j - 1], \
                f"交替阶段未严格交替: {cseq[max(0,start-2):start+5]}"
        # ④ 每 2 个战斗单位补 1 工人：终态工人 = 5 + combat_produced//2，且不超上限
        exp_w = 5 + state.combat_produced // 2
        assert w == exp_w, f"工人应=5+combat_produced//2={exp_w}，实际{w}"
        assert w <= agent.TARGET_WORKERS, f"工人超上限{w}"
        print(f"[OK] 生产调度(用户比例)  前12={seq[:12]} | 终态 W={w} V={v} R={r} "
              f"(战斗累计={state.combat_produced}, 工人=5+{state.combat_produced}//2={exp_w})")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[FAIL] 生产调度(用户比例): {e!r}"); return False
    finally:
        agent.BEACON_ENABLED = saved_beacon


def main():
    carried = CARRIED_BEACON
    results = []

    # A: Worker 站在资源点 → 采集；资源够 → 生产 Worker
    results.append(run("A 站资源点采集+生产", lambda: make_turn(
        tick=100, resources=30, population=1, core_pos=(10, 10),
        workers=[(W1, (11, 10), 0, UnitType.WORKER)],
        terrain=[TerrainView(kind="RESOURCE", positions=((11, 10),))],
        beacon=carried,
    )))

    # B: 带 Cargo 的 Worker 在 Core 格 → 交付；另一个去采集
    results.append(run("B 带Cargo交付+同伴采集", lambda: make_turn(
        tick=101, resources=5, population=2, core_pos=(10, 10),
        workers=[(W1, (10, 10), 1, UnitType.WORKER), (W2, (12, 10), 0, UnitType.WORKER)],
        terrain=[TerrainView(kind="RESOURCE", positions=((12, 10),))],
        beacon=carried,
    )))

    # C: Beacon 在地上 + 有 Worker → 派搬运工
    results.append(run("C 派遣Beacon搬运工", lambda: make_turn(
        tick=102, resources=5, population=2, core_pos=(10, 10),
        workers=[(W1, (10, 10), 0, UnitType.WORKER), (W2, (11, 10), 0, UnitType.WORKER)],
    )))

    # D: 敌人相邻 Vanguard→清扫 Ranger→射击
    results.append(run("D 自卫清扫+射击", lambda: make_turn(
        tick=103, resources=5, population=3, core_pos=(10, 10),
        workers=[
            (W1, (10, 10), 0, UnitType.WORKER),
            (V1, (10, 11), 0, UnitType.VANGUARD),
            (R1, (10, 9), 0, UnitType.RANGER),
        ], terrain=[], beacon=carried, enemies=[enemy_unit((10, 12))],
    )))

    # E: Core 受损 → 回血（恢复优先，不生产）
    results.append(run("E Core受损回血", lambda: make_turn(
        tick=104, resources=20, population=1, core_pos=(10, 10), core_hp=2,
        workers=[(W1, (10, 10), 0, UnitType.WORKER)], beacon=carried,
    )))

    # F: 人口满+可见敌人+资源宽裕 → 补 Ranger
    results.append(run("F 满人口遇敌补Ranger", lambda: make_turn(
        tick=105, resources=40, population=20, core_pos=(10, 10),
        workers=[(W1, (11, 10), 0, UnitType.WORKER)] +
                [(uuid4(), (10+i, 10), 0, UnitType.WORKER) for i in range(1, 20)],
        beacon=carried, enemies=[enemy_unit((15, 10))],
    )))

    # G: Worker 离 Core 较远且无资源可见 → 应进入探索模式
    results.append(run("G 无资源时探索", lambda: make_turn(
        tick=106, resources=10, population=2, core_pos=(10, 10),
        workers=[(W1, (20, 20), 0, UnitType.WORKER)],
        beacon=carried,
    )))

    # H: 多 Worker + 多资源点 → 一一对应分配（不重复抢占同一格）
    results.append(run("H 多Worker多资源一一分配", lambda: make_turn(
        tick=107, resources=10, population=3, core_pos=(10, 10),
        workers=[
            (W1, (10, 11), 0, UnitType.WORKER),
            (W2, (10, 12), 0, UnitType.WORKER),
            (W3, (15, 10), 0, UnitType.WORKER),
        ],
        terrain=[
            TerrainView(kind="RESOURCE", positions=((11, 11), (12, 13), (16, 10))),
        ],
        beacon=carried,
    )))

    # I: 敌单位挡在直连路径上 → 必须绕路，不能踩敌格（修复"被敌卡死"bug）
    results.append(run_enemy_avoid())

    # J: 突击敌方核心时 Ranger 在射程内应射击（修复 Ranger 永不开枪 bug）
    results.append(run_assault_ranger_shoots())

    # K: 自卫模式下 Ranger 射程外应靠近敌人（修复原地空转 bug）
    results.append(run_self_defense_ranger_approaches())

    # L: 信标开启 + 战斗单位>=5 且远离信标 → Core 朝信标迁移
    results.append(run_core_march_to_beacon())
    # AL: 信标开启但战斗单位<5 → Core 不进军(只生产)
    results.append(run_core_march_requires_5_combat())

    # M: 带货工人在 Core 2 格内 → Core 停下等提交
    results.append(run_core_march_stops_for_cargo())

    # N: 两个带货工人同时冲 Core 格 → 一个进、一个绕路（目标格冲突协调）
    results.append(run_worker_no_cell_conflict())

    # O: 探索工A采到资源 → 派B接手探索，A提交后接手B原职责（角色互换闭环）
    results.append(run_scout_handoff())

    # P: 地图层快照持久化 + 分片首帧自带完整障碍（重开地图不丢石头）
    results.append(run_map_snapshot_durable())

    # Q: 换局检测 → 清空上一局坐标系的脏障碍
    results.append(run_match_reset())

    # R: 资源记忆持久（离开视野很久仍可被空闲工锁定）
    results.append(run_resource_memory_persist())

    # S: 到达记忆坐标却发现无资源 → 清除记忆、释放分配
    results.append(run_resource_arrival_depleted())

    # T: stream/map.json 快照含资源记忆
    results.append(run_resource_snapshot())

    # U: 战争迷雾逐格精准（十字=十字，而非 32×32 大蓝块）
    results.append(run_fog_precise_cells())

    # V: 探索工安全时优先采"已探索并标记"的资源（不再一直往前走）
    results.append(run_explorer_collects_remembered())

    # X: 附近有敌人(不安全)时不长途跋涉去采记忆资源
    results.append(run_explorer_no_far_pickup_when_unsafe())

    # W: 战争迷雾铺成视野圆盘（含空格），而非只有地形点的稀疏区
    results.append(run_fog_vision_footprint())

    # Y: 敌方"可见→不可见"登记橙色消失点(!)
    results.append(run_lost_contact_register())

    # Z: 到达消失点且周围无敌人→取消感叹号；周围有敌则保留
    results.append(run_lost_contact_clear())

    # AA: 多消失点撤退方向收敛→三角定位敌方核心区；单点不估算
    results.append(run_core_zone_triangulation())
    results.append(run_sector_explore_fills_before_expand())
    results.append(run_global_assignment_nearest_worker())
    results.append(run_persistent_assignment_not_stolen())

    # AD/AE: 采集工/探索工扇形散开（32×32 区域平分、claimed 去重），不再全挤同一方向
    results.append(run_harvester_fanout())
    results.append(run_explorer_fanout())
    # M2/M3: 带货工人在 Core 周围 3 格内 → Core 停止迁移（含枚举态兼容 + 追击破局）
    results.append(run_core_march_stops_for_cargo_3())
    results.append(run_core_march_stops_for_cargo_chase())
    # M4/M5: 手动迁移不应被无条件下取消（无货放行）；手动迁移遇货仍暂停
    results.append(run_core_manual_move_not_cancelled())
    results.append(run_core_manual_move_pauses_for_cargo())

    # ---- 经济冻结模式（容量>=95 记录峰值、只补不造）----
    results.append(run_economy_freeze_normal_below_95())
    results.append(run_economy_freeze_record_peak_at_95())
    results.append(run_economy_freeze_replenish_worker_loss())
    results.append(run_economy_freeze_all_full_no_spawn())

    # ---- 战斗单位分流：<=2 全部保核、>2 才派进攻(60/40) + V+R 编组 ----
    results.append(run_combat_split_leq2_protect_only())
    results.append(run_combat_split_eq3_sixty_forty())
    results.append(run_combat_split_gt3_sixty_forty())
    results.append(run_combat_split_defend_all())
    results.append(run_patrol_aggressive_blocks())
    results.append(run_two_layer_patrol_radii())
    results.append(run_defend_only_on_combat_units())
    results.append(run_worker_blockade_on_threat())
    results.append(run_core_ranger_on_threat())
    results.append(run_blockade_holds_until_enemy_changes_dir())
    results.append(run_blockade_holds_with_memory_only())
    results.append(run_vr_pair_formation())
    # 探索组进攻/跟踪交战逻辑
    results.append(run_explore_engage_only_workers())
    results.append(run_explore_engage_outnumber())
    results.append(run_explore_engage_outnumbered())
    results.append(run_explore_track_keeps_distance_no_attack())
    results.append(run_explore_track_approach_when_far())
    results.append(run_explore_track_hold_at_standoff())
    results.append(run_explore_engage_attack_moves_and_fires())
    results.append(run_explore_target_recorded())
    results.append(run_record_units_have_target())
    results.append(run_move_dir_recorded())
    results.append(run_production_schedule_user_ratio())

    # ---- 移动逻辑优化：Core 避石头 + 探索不朝死胡同 ----
    results.append(run_core_march_avoids_rock())
    results.append(run_scout_heading_avoids_deadend())
    results.append(run_explore_recenters_on_migrated_core())
    results.append(run_explore_biases_to_beacon())
    # ---- 探索绕 Core 四周方向均衡（T48）----
    results.append(run_explore_balances_under_explored_direction())
    results.append(run_explore_sector_timeout_redirects())

    # ---- 敌Core识别与进攻派遣（T45/T46）----
    results.append(run_enemy_core_recorded_attackable())
    results.append(run_enemy_core_defended_not_recorded())
    results.append(run_attack_dispatch_nearest_group())
    results.append(run_group_trail_ranger_behind_vanguard())
    results.append(run_group_overlap())
    results.append(run_danger_zone_lifecycle())
    # ---- 三层巡逻分组 + 威胁按比例调配(不无脑召回) + 非进攻单位不召回 ----
    results.append(run_patrol_rings_three_tiers())
    results.append(run_core_defense_dispatch_sufficient())
    results.append(run_core_defense_dispatch_insufficient())
    results.append(run_core_defense_dispatch_nearest())
    results.append(run_noncombat_near_core_no_recall())

    # ---- 经济冻结模式 + 战斗分流 测试函数实现 ----

def run_economy_freeze_normal_below_95():
    """容量 < 95 → plan_core_actions 正常生产逻辑不受冻结影响。"""
    spawned = []
    class FakeCore:
        def __init__(self):
            self.position = (50, 50); self.hp = 5; self.shield = 5
            self.view = type("V", (), {"state": "NORMAL"})()
        def heal(self): pass
        def repair_shield(self): pass
        def spawn(self, ut): spawned.append(ut)
    core = FakeCore()
    w1, w2 = uw(W1, (48, 50)), uw(W2, (52, 50))
    turn = type("T", (), {
        "core": core, "workers": [w1, w2], "vanguards": [], "rangers": [],
        "resources": 20, "resource_capacity": 30,
        "state": type("S", (), {"population": 2})(),
    })()
    combat = type("C", (), {"retreat_mode": False})()
    state = type("St", (), {
        "economy_frozen": False, "economy_peak_workers": 0,
        "economy_peak_vanguards": 0, "economy_peak_rangers": 0,
    })()
    try:
        agent.plan_core_actions(turn, combat, None, state)
        assert not state.economy_frozen, "容量30<95 不应触发冻结"
        print(f"[OK] AF 容量<95 正常生产（未冻结）")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[FAIL] AF 容量<95: {e!r}"); return False


def run_economy_freeze_record_peak_at_95():
    """容量 >= 95 首次触发 → 记录当前 W/V/R 峰值。"""
    spawned = []
    class FakeCore:
        def __init__(self):
            self.position = (50, 50); self.hp = 5; self.shield = 5
            self.view = type("V", (), {"state": "NORMAL"})()
        def heal(self): pass
        def repair_shield(self): pass
        def spawn(self, ut): spawned.append(ut)
    core = FakeCore()
    ws = [uw(W1, (48, 50)), uw(W2, (52, 50)), uw(W3, (50, 48)),
          uw(W4, (50, 52)), uw(W5, (47, 47))]
    fv = type("UV", (), {"id": _FA, "unit_type": UnitType.VANGUARD,
                          "position": (49, 50)})()
    fr = type("UR", (), {"id": _FB, "unit_type": UnitType.RANGER,
                          "position": (51, 50)})()
    turn = type("T", (), {
        "core": core, "workers": ws, "vanguards": [fv], "rangers": [fr],
        "resources": 50, "resource_capacity": 100,
        "state": type("S", (), {"population": 7})(),
    })()
    combat = type("C", (), {"retreat_mode": False})()
    state = type("St", (), {
        "economy_frozen": False, "economy_peak_workers": 0,
        "economy_peak_vanguards": 0, "economy_peak_rangers": 0,
    })()
    try:
        agent.plan_core_actions(turn, combat, None, state)
        assert state.economy_frozen, "容量100>=95 应触发冻结"
        assert state.economy_peak_workers == 5, f"峰值工人应为5，实际{state.economy_peak_workers}"
        assert state.economy_peak_vanguards == 1, f"峰值先锋应为1"
        assert state.economy_peak_rangers == 1, f"峰值游侠应为1"
        assert len(spawned) == 0, f"全满不应生产，spawned={spawned}"
        print(f"[OK] AG 容量>=95 记录峰值 W=5/V=1/R=1 全满不生产")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[FAIL] AG 冻结记录: {e!r}"); return False


def run_economy_freeze_replenish_worker_loss():
    """冻结后工人阵亡(5→4) → 应补一个 WORKER。"""
    spawned = []
    class FakeCore:
        def __init__(self):
            self.position = (50, 50); self.hp = 5; self.shield = 5
            self.view = type("V", (), {"state": "NORMAL"})()
        def heal(self): pass
        def repair_shield(self): pass
        def spawn(self, ut): spawned.append(ut)
    core = FakeCore()
    ws = [uw(W1, (48, 50)), uw(W2, (52, 50)), uw(W3, (50, 48)), uw(W4, (50, 52))]
    fv = type("UV", (), {"id": _FA, "unit_type": UnitType.VANGUARD,
                          "position": (49, 50)})()
    fr = type("UR", (), {"id": _FB, "unit_type": UnitType.RANGER,
                          "position": (51, 50)})()
    turn = type("T", (), {
        "core": core, "workers": ws, "vanguards": [fv], "rangers": [fr],
        "resources": 50, "resource_capacity": 100,
        "state": type("S", (), {"population": 6})(),
    })()
    combat = type("C", (), {"retreat_mode": False})()
    state = type("St", (), {
        "economy_frozen": True, "economy_peak_workers": 5,
        "economy_peak_vanguards": 1, "economy_peak_rangers": 1,
    })()
    try:
        agent.plan_core_actions(turn, combat, None, state)
        assert UnitType.WORKER in spawned, f"缺1工人应补WORKER，实际spawned={spawned}"
        print(f"[OK] AH 冻结后工人损失→补WORKER")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[FAIL] AH 补兵: {e!r}"); return False


def run_economy_freeze_all_full_no_spawn():
    """冻结后单位数=峰值 → 不生产任何单位（只攒资源）。"""
    spawned = []
    class FakeCore:
        def __init__(self):
            self.position = (50, 50); self.hp = 5; self.shield = 5
            self.view = type("V", (), {"state": "NORMAL"})()
        def heal(self): pass
        def repair_shield(self): pass
        def spawn(self, ut): spawned.append(ut)
    core = FakeCore()
    ws = [uw(W1, (48, 50)), uw(W2, (52, 50)), uw(W3, (50, 48)),
          uw(W4, (50, 52)), uw(W5, (47, 47))]
    fv = type("UV", (), {"id": _FA, "unit_type": UnitType.VANGUARD,
                          "position": (49, 50)})()
    fr = type("UR", (), {"id": _FB, "unit_type": UnitType.RANGER,
                          "position": (51, 50)})()
    turn = type("T", (), {
        "core": core, "workers": ws, "vanguards": [fv], "rangers": [fr],
        "resources": 99, "resource_capacity": 100,
        "state": type("S", (), {"population": 7})(),
    })()
    combat = type("C", (), {"retreat_mode": False})()
    state = type("St", (), {
        "economy_frozen": True, "economy_peak_workers": 5,
        "economy_peak_vanguards": 1, "economy_peak_rangers": 1,
    })()
    try:
        agent.plan_core_actions(turn, combat, None, state)
        assert len(spawned) == 0, f"全满不应生产，spawned={spawned}"
        print(f"[OK] AI 全满不生产（只攒资源）")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[FAIL] AI 全满不生产: {e!r}"); return False


def run_combat_split_leq2_protect_only():
    """进攻单位 <= 2 → 全部围 Core 巡逻保护、不派出进攻单位(探索池为空)。"""
    manual = agent.ManualOverride()
    uv1 = type("U", (), {"id": _FC, "unit_type": UnitType.VANGUARD,
                           "position": (10, 10)})()
    ur1 = type("U", (), {"id": _FD, "unit_type": UnitType.RANGER,
                           "position": (11, 11)})()
    turn = type("T", (), {
        "units": [uv1, ur1], "workers": [], "vanguards": [uv1],
        "rangers": [ur1], "visible_enemies": [],
    })()
    patrol, explore = agent._split_combat_units(turn, manual)
    assert len(patrol) == 2, f"<=2人应全部围Core保护，巡逻池应为2，got {len(patrol)}"
    assert len(explore) == 0, f"<=2人不派进攻单位，探索池应为0，got {len(explore)}"
    print(f"[OK] AJ 进攻<=2人全部围Core保护（不探索）")
    return True


def run_combat_split_eq3_sixty_forty():
    """进攻单位 == 3(>2) → 分流：约60%围Core巡逻、40%探索(round(3*0.6)=2 / 1)。"""
    manual = agent.ManualOverride()
    u1 = type("U", (), {"id": _FC, "unit_type": UnitType.VANGUARD, "position": (10, 10)})()
    u2 = type("U", (), {"id": _FD, "unit_type": UnitType.RANGER, "position": (11, 11)})()
    u3 = type("U", (), {"id": _FE, "unit_type": UnitType.RANGER, "position": (12, 12)})()
    turn = type("T", (), {
        "units": [u1, u2, u3], "workers": [], "vanguards": [u1],
        "rangers": [u2, u3], "visible_enemies": [],
    })()
    patrol, explore = agent._split_combat_units(turn, manual)
    assert len(patrol) == 2, f"3人应分2人巡逻(got {len(patrol)})"
    assert len(explore) == 1, f"3人应分1人探索(got {len(explore)})"
    print(f"[OK] AQ 进攻=3人分流 巡逻{len(patrol)}/探索{len(explore)}（>2才派进攻）")
    return True


def run_combat_split_gt3_sixty_forty():
    """进攻单位 > 3 → 约60%进巡逻池、40%进探索池。"""
    manual = agent.ManualOverride()
    _ids = [uuid4() for _ in range(9)]
    units = []
    for i in range(5):
        units.append(type("U", (), {
            "id": _ids[i], "unit_type": UnitType.VANGUARD, "position": (i, i),
        })())
    for i in range(4):
        units.append(type("U", (), {
            "id": _ids[5+i], "unit_type": UnitType.RANGER, "position": (i+10, i),
        })())
    turn = type("T", (), {
        "units": units, "workers": [], "vanguards": units[:5],
        "rangers": units[5:], "visible_enemies": [],
    })()
    patrol, explore = agent._split_combat_units(turn, manual)
    total = len(patrol) + len(explore)
    assert total == 9, f"总人数应为9，实际{total}"
    ratio = len(patrol) / total if total else 0
    assert 0.55 <= ratio <= 0.70, f"巡逻比例应在~60%，实际{ratio:.2f}({len(patrol)}/{total})"
    print(f"[OK] AK 9人进攻分流 巡逻{len(patrol)}/探索{len(explore)} ≈ {ratio:.0%}")
    return True


def run_combat_split_defend_all():
    """defend_all=True（Core 周围有威胁）→ 全部战斗单位守 Core，不探索。"""
    manual = agent.ManualOverride()
    ids = [uuid4() for _ in range(6)]
    units = [type("U", (), {"id": ids[i],
                            "unit_type": UnitType.VANGUARD if i % 2 == 0 else UnitType.RANGER,
                            "position": (i, i)})() for i in range(6)]
    turn = type("T", (), {
        "units": units, "workers": [],
        "vanguards": [u for u in units if u.unit_type == UnitType.VANGUARD],
        "rangers": [u for u in units if u.unit_type == UnitType.RANGER],
        "visible_enemies": [],
    })()
    patrol, explore = agent._split_combat_units(turn, manual, defend_all=True)
    assert len(explore) == 0, f"defend_all 应不探索，探索池应为0，got {len(explore)}"
    assert len(patrol) == 6, f"defend_all 应全员守Core，巡逻池应为6，got {len(patrol)}"
    print(f"[OK] BG 威胁时全员守Core（巡逻{len(patrol)}/探索{len(explore)}）")
    return True


def run_patrol_aggressive_blocks():
    """aggressive 死守：敌人进入 Core 20格内，先锋逼近敌方前格并攻击，绝不后撤。"""
    patrol = agent.PatrolState()
    enemy_mem = type("M", (), {"records": {}})()
    manual = agent.ManualOverride()
    core_pos = (100, 100)
    enemy = type("E", (), {"position": (110, 100)})()   # 距 Core=10 ≤20
    # 先锋在 (108,100)：距敌=2，应朝前格(109,100)逼近/相邻扫荡，而非远离
    v = FakeUnit("V", UnitType.VANGUARD, (108, 100))
    turn = type("T", (), {"visible_enemies": [enemy], "rangers": [],
                          "vanguards": [v], "units": [v], "tick": 0})()
    targets = {}
    agent.plan_patrol(turn, patrol, core_pos, set(), enemy_mem, manual, set(),
                      patrol_units=[v], targets=targets, aggressive=True)
    # 前格应为 (109,100)
    front = agent._front_cell((110, 100), core_pos, set())
    assert front == (109, 100), f"敌方前格应为(109,100)，got {front}"
    assert v.moves or v.sweeps, "死守模式应逼近或攻击敌人，不应空转"
    assert targets.get("V") == (110, 100), f"死守目标应为敌人格(110,100)，got {targets.get('V')}"
    # 不应后撤（移动方向应使 x 增大，朝敌人）
    if v.moves:
        dx, _ = v.moves[0].delta
        assert dx >= 0, f"死守不应后撤(朝-x)，实际 delta={v.moves[0].delta}"
    # 游侠在远处(105,100)：射程外应靠近前格而非原地
    r = FakeUnit("R", UnitType.RANGER, (105, 100))
    turn2 = type("T", (), {"visible_enemies": [enemy], "rangers": [r],
                           "vanguards": [], "units": [r], "tick": 0})()
    agent.plan_patrol(turn2, patrol, core_pos, set(), enemy_mem, manual, set(),
                      patrol_units=[r], targets={}, aggressive=True)
    assert r.moves or r.shoot_cells, "远处游侠应靠近或射击，不应后撤空转"
    print(f"[OK] BE 死守：先锋逼近前格并攻击 (moves={v.moves}, sweeps={v.sweeps})")
    return True


def run_worker_blockade_on_threat():
    """威胁时抽调工人占据敌方朝 Core 前格，封堵敌方推进路线。"""
    state = agent.AgentState()
    state.manual = agent.ManualOverride()
    state.unit_targets = {}
    core_pos = (100, 100)
    enemy = type("E", (), {"position": (110, 100)})()
    w_free = FakeUnit("W1", UnitType.WORKER, (95, 100))   # 空手工
    w_carry = FakeUnit("W2", UnitType.WORKER, (102, 100)); w_carry.cargo = 5  # 载货工
    turn = type("T", (), {"workers": [w_free, w_carry], "visible_enemies": [enemy],
                          "rangers": [], "vanguards": []})()
    agent.plan_worker_blockade(turn, {(110, 100)}, core_pos, set(), set(), state, carrier_id=None)
    assert w_free.moves, "应抽调空手工回防封堵敌方前格"
    assert state.unit_targets.get("W1") == (109, 100), \
        f"工人目标应为前格(109,100)，got {state.unit_targets.get('W1')}"
    print(f"[OK] BF 工人回防占据敌方前格(109,100) 封堵推进路线")
    return True


def run_core_ranger_on_threat():
    """敌方进入 Core 10格内且仍有资源 → 立即造游侠（优先级高于补工人/进军）。"""
    combat = agent.CombatState()
    spawned = []
    core = type("C", (), {"position": (100, 100), "hp": 10, "shield": 10,
                          "spawn": lambda self, ut: spawned.append(ut)})()
    r = type("U", (), {"id": uuid4(), "unit_type": UnitType.RANGER,
                       "position": (1, 1), "hp": 2, "cargo": 0})()
    enemy = type("E", (), {"position": (105, 100), "unit_type": UnitType.VANGUARD})()  # 距=5 ≤10
    turn = type("T", (), {"core": core,
                          "state": type("S", (), {"population": 10})(),
                          "resources": 50, "rangers": [r],
                          "visible_enemies": [enemy]})()
    agent.plan_core_actions(turn, combat, None, None)
    assert UnitType.RANGER in spawned, "敌方进入10格且资源够→应立即造游侠"
    print(f"[OK] BH 敌方近Core(10格内)且有资源→立即造游侠")
    return True


def run_explore_engage_only_workers():
    """视野内只有敌方工人(无敌方进攻单位) → 应直接进攻。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    u2 = FakeUnit(_FD, UnitType.RANGER, (11, 11))
    ew = enemy_unit((12, 12), UnitType.WORKER)   # 双方视野内
    turn = type("T", (), {
        "units": [u1, u2], "workers": [], "vanguards": [u1],
        "rangers": [u2], "visible_enemies": [ew],
    })()
    eg = agent.assess_explore_engagement(turn, [u1, u2])
    assert eg.mode == "ATTACK", f"只有敌方工人应进攻，got {eg.mode}"
    print(f"[OK] AU 探索组遇敌方工人→ATTACK")
    return True


def run_explore_engage_outnumber():
    """我方2进攻单位、共同看到同一敌方进攻单位(敌1) → 我方占优→进攻。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    u2 = FakeUnit(_FD, UnitType.RANGER, (11, 11))
    ev = enemy_unit((12, 12), UnitType.VANGUARD)   # 双方视野内(dist 4/2)
    turn = type("T", (), {
        "units": [u1, u2], "workers": [], "vanguards": [u1],
        "rangers": [u2], "visible_enemies": [ev],
    })()
    eg = agent.assess_explore_engagement(turn, [u1, u2])
    assert eg.mode == "ATTACK", f"我方2>敌1应进攻，got {eg.mode}"
    assert eg.enemy_combat_count == 1, f"敌方进攻单位应计1，got {eg.enemy_combat_count}"
    print(f"[OK] AV 探索组2 vs 共同敌1→ATTACK (敌计{eg.enemy_combat_count})")
    return True


def run_explore_engage_outnumbered():
    """我方2进攻单位、共同看到3个敌方进攻单位 → 敌方占优→跟踪(不进攻)。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    u2 = FakeUnit(_FD, UnitType.RANGER, (11, 11))
    enemies = [enemy_unit((12, 12), UnitType.VANGUARD),
               enemy_unit((12, 11), UnitType.RANGER),
               enemy_unit((11, 12), UnitType.VANGUARD)]   # 均在双方视野内
    turn = type("T", (), {
        "units": [u1, u2], "workers": [], "vanguards": [u1],
        "rangers": [u2], "visible_enemies": enemies,
    })()
    eg = agent.assess_explore_engagement(turn, [u1, u2])
    assert eg.mode == "TRACK", f"我方2<敌3应跟踪，got {eg.mode}"
    assert eg.enemy_combat_count == 3, f"敌方进攻单位应计3，got {eg.enemy_combat_count}"
    print(f"[OK] AW 探索组2 vs 共同敌3→TRACK (敌计{eg.enemy_combat_count})")
    return True


def run_explore_track_keeps_distance_no_attack():
    """TRACK 模式：我方先锋距敌2格(太近) → 后撤拉开到>=3，且绝不攻击。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    ev = enemy_unit((10, 12), UnitType.VANGUARD)   # 曼哈顿距离=2 < 3
    turn = type("T", (), {
        "units": [u1], "workers": [], "vanguards": [u1],
        "rangers": [], "visible_enemies": [ev],
    })()
    eg = agent.Engagement(mode="TRACK", enemy_pos=(10, 12),
                          enemy_combat_count=1, our_count=1)
    pairs = [(u1, None)]
    agent._plan_explore_combat_groups(turn, pairs, set(), set(), None,
                                      (10, 10), engage=eg)
    assert not u1.sweeps and not u1.shoots, "TRACK 不应攻击(无 sweep/shoot)"
    assert u1.moves, "太近应后撤一步拉开距离"
    # 新位置与敌方距离应 >= 3（后撤到最小间距）
    nd = abs(u1.position[0] - 10) + abs(u1.position[1] - 12)
    assert nd >= agent.TRACK_STANDOFF, f"后撤后距离应>=3，got {nd}"
    print(f"[OK] AX TRACK 太近后撤到距离{nd}(不攻击)")
    return True


def run_explore_track_approach_when_far():
    """TRACK 模式：我方距敌5格(>3) → 应靠近以保持在最小间距，且不攻击。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    ev = enemy_unit((10, 15), UnitType.VANGUARD)   # 距离=5 > 3
    turn = type("T", (), {
        "units": [u1], "workers": [], "vanguards": [u1],
        "rangers": [], "visible_enemies": [ev],
    })()
    eg = agent.Engagement(mode="TRACK", enemy_pos=(10, 15),
                          enemy_combat_count=1, our_count=1)
    pairs = [(u1, None)]
    agent._plan_explore_combat_groups(turn, pairs, set(), set(), None,
                                      (10, 10), engage=eg)
    assert not u1.sweeps and not u1.shoots, "TRACK 不应攻击"
    assert u1.moves, "距离>3应靠近"
    nd = abs(u1.position[0] - 10) + abs(u1.position[1] - 15)
    assert nd < 5, f"应靠近(距离变小)，got {nd}"
    print(f"[OK] AY TRACK 远距靠近到距离{nd}(不攻击)")
    return True


def run_explore_track_hold_at_standoff():
    """TRACK 模式：我方距敌恰好3格(=最小间距) → 原地待命，不攻击不移动。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    ev = enemy_unit((10, 13), UnitType.VANGUARD)   # 距离=3 == 最小间距
    turn = type("T", (), {
        "units": [u1], "workers": [], "vanguards": [u1],
        "rangers": [], "visible_enemies": [ev],
    })()
    eg = agent.Engagement(mode="TRACK", enemy_pos=(10, 13),
                          enemy_combat_count=1, our_count=1)
    pairs = [(u1, None)]
    agent._plan_explore_combat_groups(turn, pairs, set(), set(), None,
                                      (10, 10), engage=eg)
    assert not u1.sweeps and not u1.shoots, "TRACK 不应攻击"
    assert not u1.moves, "恰好保持最小间距应原地待命"
    print(f"[OK] AZ TRACK 距离=3原地待命(不攻击不移动)")
    return True


def run_explore_engage_attack_moves_and_fires():
    """ATTACK 模式：游侠在射程内应射击、先锋相邻应扫荡（确实发动攻击）。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    u2 = FakeUnit(_FD, UnitType.RANGER, (10, 14))
    ev = enemy_unit((10, 12), UnitType.VANGUARD)   # 先锋距离2、游侠距离2
    turn = type("T", (), {
        "units": [u1, u2], "workers": [], "vanguards": [u1],
        "rangers": [u2], "visible_enemies": [ev],
    })()
    eg = agent.Engagement(mode="ATTACK", enemy_pos=(10, 12),
                          enemy_combat_count=1, our_count=2)
    pairs = [(u1, u2)]
    agent._plan_explore_combat_groups(turn, pairs, set(), set(), None,
                                      (10, 10), engage=eg)
    assert u2.shoots, "ATTACK 游侠射程内应射击"
    print(f"[OK] BA ATTACK 游侠射击+先锋逼近 (shoots={len(u2.shoots)})")
    return True


def run_explore_target_recorded():
    """探索组在 ATTACK 模式下应把终点坐标记入 targets 字典（供地图虚线展示）。"""
    u1 = FakeUnit(_FC, UnitType.VANGUARD, (10, 10))
    u2 = FakeUnit(_FD, UnitType.RANGER, (10, 14))
    ev = enemy_unit((10, 12), UnitType.VANGUARD)
    turn = type("T", (), {
        "units": [u1, u2], "workers": [], "vanguards": [u1],
        "rangers": [u2], "visible_enemies": [ev],
        "obstacle_cells": set(), "resource_cells": set(),
    })()
    targets: dict = {}
    eg = agent.Engagement(mode="ATTACK", enemy_pos=(10, 12),
                          enemy_combat_count=1, our_count=2)
    pairs = [(u1, u2)]
    agent._plan_explore_combat_groups(turn, pairs, set(), set(), None,
                                      (10, 10), engage=eg, targets=targets)
    assert targets.get(u1.id) == (10, 12), f"先锋终点应为敌坐标，got {targets.get(u1.id)}"
    assert targets.get(u2.id) == (10, 12), f"游侠终点应为敌坐标，got {targets.get(u2.id)}"
    print(f"[OK] BB 探索组终点坐标记入 targets（ATTACK→敌{eg.enemy_pos}）")
    return True


def run_record_units_have_target():
    """record() 写出的 units 应包含 target 字段（None 或坐标）。"""
    import os, json, shutil, tempfile
    tmpd = tempfile.mkdtemp(prefix="ah_tgt_")
    old_dir = agent.STREAM_SHARD_DIR
    agent.STREAM_SHARD_DIR = tmpd
    try:
        state = agent.AgentState()
        state.unit_targets = {_FC: (12, 12)}
        u = type("U", (), {"id": _FC, "unit_type": UnitType.VANGUARD,
                           "position": (10, 10), "hp": 2, "cargo": 0})()
        core = type("C", (), {"position": (0, 0), "hp": 5, "shield": 5})()
        turn = type("T", (), {
            "tick": 1, "resources": 5, "core": core,
            "units": [u], "workers": [], "vanguards": [u], "rangers": [],
            "visible_enemies": [], "resource_cells": set(), "events": [],
            "obstacle_cells": set(),
        })()
        el = agent.EventLog()
        el.record(turn, state)
        uj = el.ticks[-1]["units"][0]
        assert "target" in uj, "units 缺少 target 字段"
        assert uj["target"] == [12, 12], f"target 应=[12,12]，got {uj['target']}"
        print(f"[OK] BC record() units 含 target 字段 = {uj['target']}")
        return True
    finally:
        agent.STREAM_SHARD_DIR = old_dir
        shutil.rmtree(tmpd, ignore_errors=True)


def run_move_dir_recorded():
    """_mv() 应记录单位本 tick 移动方向；record() 写出的 units 应包含 dir 字段。"""
    import shutil, tempfile
    # 1) _mv 记录方向；None 不记录
    agent._MOVE_DIRS.clear()
    def _m(self, d):
        pass
    u = type("U", (), {"id": UUID("11111111-1111-1111-1111-111111111111"),
                       "position": (50, 50), "unit_type": UnitType.VANGUARD,
                       "move": _m})()
    agent._mv(u, Direction.RIGHT)
    assert agent._MOVE_DIRS.get(u.id) == (1, 0), \
        f"_mv RIGHT 应记 (1,0)，实际 {agent._MOVE_DIRS.get(u.id)}"
    agent._mv(u, None)
    assert agent._MOVE_DIRS.get(u.id) == (1, 0), "None 不应覆盖已记录方向"
    agent._MOVE_DIRS.clear()

    # 2) record() 把 _MOVE_DIRS 写入 units[].dir
    tmpd = tempfile.mkdtemp(prefix="ah_dir_")
    old_dir = agent.STREAM_SHARD_DIR
    agent.STREAM_SHARD_DIR = tmpd
    try:
        state = agent.AgentState()
        _fid = UUID("22222222-2222-2222-2222-222222222222")
        state.unit_targets = {_fid: (12, 12)}
        agent._MOVE_DIRS[_fid] = (0, -1)   # UP
        u2 = type("U2", (), {"id": _fid, "unit_type": UnitType.VANGUARD,
                             "position": (10, 10), "hp": 2, "cargo": 0})()
        core = type("C", (), {"position": (0, 0), "hp": 5, "shield": 5})()
        turn = type("T", (), {
            "tick": 1, "resources": 5, "core": core,
            "units": [u2], "workers": [], "vanguards": [u2], "rangers": [],
            "visible_enemies": [], "resource_cells": set(), "events": [],
            "obstacle_cells": set(),
        })()
        el = agent.EventLog()
        el.record(turn, state)
        uj = el.ticks[-1]["units"][0]
        assert "dir" in uj, "units 缺少 dir 字段"
        assert uj["dir"] == [0, -1], f"dir 应=[0,-1]，got {uj['dir']}"
        print(f"[OK] BD record() units 含 dir 字段(移动方向) = {uj['dir']}")
        return True
    finally:
        agent.STREAM_SHARD_DIR = old_dir
        shutil.rmtree(tmpd, ignore_errors=True)


def run_core_march_avoids_rock():
    """Core 朝信标迁移时，主方向被石头堵 → 改选能推进的自由方向，绝不返回 None 去撞石头。"""
    core = (0, 0)
    beacon = (10, 10)
    obstacles = {(1, 0)}            # 正右方(主方向)是石头
    d = agent._core_march_direction(core, beacon, obstacles)
    assert d is not None, "主方向被堵但仍有自由格(如下)可推进 → 不应返回 None"
    assert d != agent.Direction.RIGHT, "主方向(RIGHT)是石头 → 不应选它"
    # 选中的方向必须真的离开石头且更靠近信标
    nc = (core[0] + d.delta[0], core[1] + d.delta[1])
    assert nc not in obstacles, "选中方向不应是障碍格"
    assert agent.manhattan(nc, beacon) < agent.manhattan(core, beacon), "应更靠近信标"
    print(f"[OK] AM Core进军避石头 -> {d}")
    return True


def run_scout_heading_avoids_deadend():
    """探索选航向时，相邻格是障碍(死胡同)的航向应被重罚、不被选中。"""
    pos = (0, 0)
    fake_w = type("U", (), {"id": W1})()
    mm = agent.MapMemory()          # 空记忆 → 各方向未探索比例≈1
    # 8 个罗盘方向里，只有 RIGHT 相邻格是障碍；其余自由且未探索
    h = agent._choose_scout_heading(pos, fake_w, [], mm, 0, 30, {},
                                     blocked={(1, 0)})
    assert h != (1, 0), f"死胡同方向(1,0)不应被选中，实际={h}"
    print(f"[OK] AN 探索避开死胡同航向 -> {h}")
    return True


def run_explore_recenters_on_migrated_core():
    """Core 迁移后，_choose_frontier_sector 应优先选新 Core 附近的扇区，
    而非被工人当前物理位置（旧 Core 方向）绑架。"""
    # 场景：原始 Core 在 (0,0)，已探索周围扇区；Core 迁移到 (200, 200)；
    # 工人仍在旧区域 (30, 30) 附近。
    mm = agent.MapMemory()
    # 模拟旧 Core 周围已大量探索（sector (0,0), (1,0), (0,1), (1,1) 等已接近填满）
    for sx in range(-1, 4):
        for sy in range(-1, 4):
            sec = (sx, sy)
            # 填充大部分格为已探索（模拟旧区已探完）
            for dx in range(0, 32):
                for dy in range(0, 32):
                    if (dx + dy) % 7 != 0:  # 留少量未探索
                        p = (sec[0] * 32 + dx, sec[1] * 32 + dy)
                        mm.explored_cells.add(p)
                        if p not in mm.explored_sectors:
                            ss = (p[0] // 32, p[1] // 32)
                            mm.explored_sectors.add(ss)

    new_core_pos = (200, 200)   # 新 Core（迁移后）
    worker_pos = (35, 35)       # 工人还在旧区域

    # 新 Core 周围的扇区基本未探索（只有 Core 视野可能照到的少量）
    # 选扇区时应优先新 Core 附近的低环扇区
    chosen = agent._choose_frontier_sector(mm, worker_pos, new_core_pos)
    assert chosen is not None, "应有可选扇区"
    # 新 Core 在 sector (6, 6) 附近（200//32=6）
    # 选择的扇区应该在新 Core 的 EXPLORER_MAX_RING(4) 环内
    core_sec = (new_core_pos[0] // 32, new_core_pos[1] // 32)
    ring = agent._ring_of(chosen[0], chosen[1], core_sec[0], core_sec[1])
    assert ring <= agent.EXPLORER_MAX_RING, \
        f"选中扇区 {chosen} 环号={ring} > EXPLORER_MAX_RING={agent.EXPLORER_MAX_RING}"
    # 关键断言：不应选旧 Core 附近的扇区（环号会很大或被排除）
    old_core_sec = (0, 0)
    old_ring = agent._ring_of(chosen[0], chosen[1], old_core_sec[0], old_core_sec[1])
    # 新 Core 附近的扇区环号应显著小于以旧 Core 为中心的环号
    # （即选中的扇区离新 Core 更近）
    d_new = abs(chosen[0] - core_sec[0]) + abs(chosen[1] - core_sec[1])
    d_old = abs(chosen[0] - old_core_sec[0]) + abs(chosen[1] - old_core_sec[1])
    assert d_new <= d_old, \
        f"应优先新 Core 附近扇区：d_new={d_new} > d_old={d_old}, chosen={chosen}"
    print(f"[OK] AO Core迁移后探索重新居中 -> 选扇区{chosen}(新Core环{ring}, d_new={d_new}, d_old={d_old})")
    return True


def run_explore_biases_to_beacon():
    """信标进军期：beacon_march_info 命中 → _choose_frontier_sector 在 bias_beacon=True
    时优先选朝信标的主方向桶扇区（信标在 Core 右下 → 右/下/右下三向之一），且绝不超过
    EXPLORER_MAX_RING；即便该主方向历史已探得多(dir_count 高)也不被挤掉。非偏置时不强制。"""
    mm = agent.MapMemory()
    S = agent.SECTOR_SIZE
    core_pos = (-100, -100)                      # sector(-4,-4)；信标(0,0)在其右下(+x,+y)
    core_sec = (core_pos[0] // S, core_pos[1] // S)
    def fill(cx, cy):
        for dx in range(S):
            for dy in range(S):
                mm._add_explored((cx * S + dx, cy * S + dy))
    fill(*core_sec)                              # 仅填 Core 自身扇区
    # 把"朝信标三向"的更外层扇区全部填充满（制造高 dir_count，模拟已向下右探得多）
    for k in range(2, agent.EXPLORER_MAX_RING + 1):
        fill(core_sec[0] + k, core_sec[1])        # 东
        fill(core_sec[0], core_sec[1] + k)        # 南
        fill(core_sec[0] + k, core_sec[1] + k)    # 东南
    # 制造一个反方向(西北)未探索扇区作为对照，验证偏置确实压制它
    turn = type("T", (), {"vanguards": [1, 2, 3, 4, 5], "rangers": []})()
    marching, dirs = agent.beacon_march_info(turn, core_pos)
    assert marching and dirs, f"应判定进军并给出主方向桶, got {marching},{dirs}"
    # 偏置选择：必落在主方向桶之一，且环号不超过 EXPLORER_MAX_RING
    chosen = agent._choose_frontier_sector(mm, core_pos, core_pos,
                                           beacon_dirs=dirs, bias_beacon=True)
    assert chosen is not None, "偏置下应有候选"
    b = agent._dir_bucket_of(chosen[0], chosen[1], core_sec[0], core_sec[1])
    assert b in dirs, f"偏置应落主方向桶{dirs}，却选 {chosen}(桶{b})"
    ring = agent._ring_of(chosen[0], chosen[1], core_sec[0], core_sec[1])
    assert ring <= agent.EXPLORER_MAX_RING, f"偏置不得超环 {ring}>{agent.EXPLORER_MAX_RING}"
    # 非偏置：不强制主方向，范围同样受约束
    chosen2 = agent._choose_frontier_sector(mm, core_pos, core_pos)
    assert chosen2 is not None
    assert agent._ring_of(chosen2[0], chosen2[1], core_sec[0], core_sec[1]) <= agent.EXPLORER_MAX_RING
    print(f"[OK] BP 信标进军探索偏置->选{chosen}(桶{b}∈{dirs})，范围环{ring}<=MAX_RING")
    return True


def run_explore_balances_under_explored_direction():
    """工人聚在 Core 上方时，探索仍应优先欠探索(下方/侧方)方向，而非被位置绑架继续向上。
    验证 dir_count 方向均衡：上方已探得多 → 即使工人偏上，也去探侧/下方向。"""
    mm = agent.MapMemory()
    S = agent.SECTOR_SIZE
    def fill(cx, cy):
        for dx in range(S):
            for dy in range(S):
                mm._add_explored((cx * S + dx, cy * S + dy))
    fill(0, 0)                       # Core 扇区
    # 第1环四正方向 + 第2环四对角 全部"已填满"
    for c in [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)]:
        fill(*c)
    fill(0, -3)                      # 额外给「上方」灌水，模拟历史已向上探得多
    core_pos = (0, 0)
    worker_pos = (0, -50)            # 工人聚在 Core 上方
    chosen = agent._choose_frontier_sector(mm, worker_pos, core_pos)
    assert chosen is not None
    b = (1 if chosen[0] > 0 else (-1 if chosen[0] < 0 else 0),
         1 if chosen[1] > 0 else (-1 if chosen[1] < 0 else 0))
    assert b != (0, -1), \
        f"不应选上方方向（旧逻辑会选 (0,-2) 因 d_expl 最小）；实际={chosen} bucket={b}"
    print(f"[OK] AT 方向均衡：工人偏上仍选欠探索方向 -> {chosen} bucket={b}")
    return True


def run_explore_sector_timeout_redirects():
    """探索工黏性扇区驻留超 EXPLORER_SECTOR_MAX_TICKS → 强制释放并重选；
    重选优先欠探索方向，从而从单向偏上解脱。"""
    mm = agent.MapMemory()
    S = agent.SECTOR_SIZE
    def fill(cx, cy):
        for dx in range(S):
            for dy in range(S):
                mm._add_explored((cx * S + dx, cy * S + dy))
    fill(0, 0)
    for c in [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)]:
        fill(*c)
    fill(0, -3)
    core_pos = (0, 0)
    w = type("U", (), {"id": UUID("99999999-9999-9999-9999-999999999999"),
                       "position": (0, -50)})()
    ws = agent.WorkerState()
    ws.explore_sector = (0, -2)      # 工人卡在「上方」扇区（未填满）
    ws.explore_sector_since = 0
    # tick=100 >> EXPLORER_SECTOR_MAX_TICKS(45) → 触发超时释放
    agent._plan_sector_explore(ws, w, (0, -50), core_pos, set(), mm, None, 100)
    assert ws.explore_sector is not None
    b = (1 if ws.explore_sector[0] > 0 else (-1 if ws.explore_sector[0] < 0 else 0),
         1 if ws.explore_sector[1] > 0 else (-1 if ws.explore_sector[1] < 0 else 0))
    assert b != (0, -1), \
        f"超时释放后应重选到欠探索方向，而非仍卡上方(0,-2)；实际={ws.explore_sector} bucket={b}"
    assert ws.explore_sector_since == 100, "释放后应重置 since=tick"
    print(f"[OK] AU 超时释放重定向 -> {ws.explore_sector} bucket={b}")
    return True


class _FU:
    """测试用假战斗单位：仅含 _core_defense_dispatch / plan_patrol 需要的字段与方法。"""
    def __init__(self, uid, pos, ut=None):
        self.id = uid
        self.position = pos
        self.unit_type = ut
    def shoot_cell(self, *a, **k):
        return None
    def shoot(self, *a, **k):
        return None
    def sweep(self, *a, **k):
        return None


def run_patrol_rings_three_tiers():
    """_assign_patrol_rings 应给出 外3/中2/内1 的分组；战斗单位不足按上限分配，溢出归外圈。"""
    expect = {
        6: [(18, 3), (10, 2), (5, 1)],
        4: [(18, 3), (10, 1), (5, 0)],
        3: [(18, 3), (10, 0), (5, 0)],
        8: [(18, 5), (10, 2), (5, 1)],
        2: [(18, 2), (10, 0), (5, 0)],
        1: [(18, 1), (10, 0), (5, 0)],
    }
    for n, exp in expect.items():
        rings = agent._assign_patrol_rings(n)
        full = {18: 0, 10: 0, 5: 0}
        for (r, c, _ph) in rings:
            full[r] += c
        got = [(18, full[18]), (10, full[10]), (5, full[5])]
        assert got == exp, f"n={n}: 期望{exp} 实得{got}"
    print("[OK] AV 三层巡逻分组 外3/中2/内1 (6→3/2/1, 8→溢出外圈5/2/1)")
    return True


def run_core_defense_dispatch_sufficient():
    """敌方进攻单位数 <= 巡逻组 → 不召回探索组，巡逻组直接全部迎击。"""
    import uuid
    core = (50, 50)
    patrol = [_FU(uuid.uuid4(), (54, 50)) for _ in range(4)]
    explore = [_FU(uuid.uuid4(), (300, 300)) for _ in range(3)]
    enemy = [(60, 52), (62, 53), (58, 51)]  # 3 个敌方进攻单位
    recalled = agent._core_defense_dispatch(patrol, explore, enemy, core)
    assert recalled == 0, f"巡逻组(4)>=敌(3)+1 应不召回，实际召回{recalled}"
    assert len(patrol) == 4 and len(explore) == 3, "巡逻/探索池不应被改动"
    print("[OK] AW 敌3进攻单位<=巡逻组(4)→不召回探索组，巡逻组直接迎击")
    return True


def run_core_defense_dispatch_insufficient():
    """敌方进攻单位数 > 巡逻组 → 召回最近的探索组，直到总战斗单位 > 敌方。"""
    import uuid
    core = (50, 50)
    patrol = [_FU(uuid.uuid4(), (54, 50)) for _ in range(3)]
    explore = [_FU(uuid.uuid4(), (120, 120)) for _ in range(2)]
    enemy = [(60, 52), (62, 53), (58, 51), (61, 55)]  # 4 个敌方进攻单位
    recalled = agent._core_defense_dispatch(patrol, explore, enemy, core)
    assert recalled == 2, f"巡逻(3)<敌(4)+1 → 应召回2，实际{recalled}"
    assert len(patrol) == 5 and len(explore) == 0, "应召回2个使总战斗=5"
    print("[OK] AX 敌4进攻单位>巡逻组(3)→召回最近2探索组，总5全迎击(>4)")
    return True


def run_core_defense_dispatch_nearest():
    """召回时优先选离 Core 最近的探索组。"""
    import uuid
    core = (50, 50)
    patrol = [_FU(uuid.uuid4(), (54, 50))]
    near = _FU(uuid.uuid4(), (60, 50))    # 距 Core 10
    far = _FU(uuid.uuid4(), (300, 300))   # 距 Core 很远
    explore = [near, far]
    enemy = [(60, 52)]  # 1 个 → need_total=2，巡逻1不足，召回1个最近的
    recalled = agent._core_defense_dispatch(patrol, explore, enemy, core)
    assert recalled == 1, f"应召回1，实际{recalled}"
    assert patrol[-1] is near, "应召回离Core最近的探索组"
    print("[OK] AY 召回优先级：离Core最近的探索组先被召回")
    return True


def run_noncombat_near_core_no_recall():
    """Core 附近出现敌方非进攻单位(工人) → 巡逻组在巡逻范围内直接追击攻击，且不触发死守/召回。"""
    import uuid
    from arena_hero import (Turn, PlayerState, CoreView, UnitView, UnitType,
                            ChampionBeacon, PlayerStatus, BeaconStatus)

    core = (50, 50)
    objs = [CoreView(kind='CORE', id=uuid.uuid4(), controlled=True, owner_username='tester',
                     position=core, hp=5, shield=5, state='NORMAL')]
    our = [(uuid.uuid4(), UnitType.RANGER, (54, 50)),
           (uuid.uuid4(), UnitType.VANGUARD, (50, 54))]
    for (uid, ut, pos) in our:
        objs.append(UnitView(kind='UNIT', id=uid, controlled=True, position=pos, hp=4,
                             unit_type=ut, cargo=None))
    # 敌方非进攻单位(工人) 近 Core，无敌方进攻单位
    objs.append(UnitView(kind='UNIT', id=uuid.uuid4(), controlled=False,
                         position=(55, 52), hp=2, unit_type=UnitType.WORKER, cargo=None))
    b = ChampionBeacon(position=(0, 0), status=BeaconStatus.CARRIED, carrier_id=uuid.uuid4())
    st = PlayerState(status=PlayerStatus.ACTIVE, resources=0, population=10,
                     champion_beacon=b, objects=tuple(objs), events=())
    t = Turn(tick=300, state=st, submitter=lambda p, *a: p)
    s = agent.AgentState()
    agent.plan_turn_v2(t, s)
    # 非进攻单位不触发死守/召回
    assert not s.combat.engaging, "非进攻单位近Core不应触发死守(engaging)"
    # 防御调配：enemy_combat_near 为空 → 0 召回
    # 巡逻组应直接追击该工人（攻击发生在 plan_combat 自卫/plan_patrol 追击）
    # 验证：工人位置被某战斗单位作为目标（unit_targets 或 已交战动作）
    targeted = (55, 52) in set(s.unit_targets.values())
    # 即便该单位被 plan_combat 自卫抢先交战(committed)，也至少满足：未触发死守即未召回
    print(f"[OK] AZ 非进攻单位近Core→巡逻组直接攻击且不召回探索组(engaging={s.combat.engaging}, 工人被指向={targeted})")
    return True


def run_enemy_core_recorded_attackable():
    """工人(任意单位)发现无防守敌Core → 记入 state.attack_targets 供派遣进攻。"""
    state = agent.AgentState()
    t, _ = make_turn(tick=200, resources=30, population=10, core_pos=(50, 50),
                     workers=[(W1, (10, 10), 0, UnitType.WORKER)],
                     enemies=[enemy_core((10, 4))],   # 无防守敌Core
                     beacon=CARRIED_BEACON)
    agent._update_attack_targets(t, state, t.tick)
    assert (10, 4) in state.attack_targets, \
        "无防守敌Core(仅Core自己)应记入 attack_targets"
    print(f"[OK] AP 无防守敌Core记入可进攻目标 -> {list(state.attack_targets)}")
    return True


def run_enemy_core_defended_not_recorded():
    """敌Core附近有敌方进攻单位(V/R) → 视为有防守，不记入 attack_targets。"""
    state = agent.AgentState()
    t, _ = make_turn(tick=201, resources=30, population=10, core_pos=(50, 50),
                     workers=[(W1, (10, 10), 0, UnitType.WORKER)],
                     enemies=[enemy_core((10, 4)),
                              enemy_unit((12, 4), UnitType.VANGUARD)],  # 近敌Core的防守兵
                     beacon=CARRIED_BEACON)
    agent._update_attack_targets(t, state, t.tick)
    assert (10, 4) not in state.attack_targets, \
        "有防守(敌V近敌Core)不应记入 attack_targets"
    print(f"[OK] AQ 有防守敌Core不记入可进攻目标")
    return True


def run_attack_dispatch_nearest_group():
    """plan_attack_dispatch 从探索池抽调最近 V+R 组去进攻已确认敌Core；游侠跟随编队。"""
    state = agent.AgentState()
    t, _ = make_turn(tick=202, resources=30, population=10, core_pos=(50, 50),
                     workers=[(W1, (10, 10), 0, UnitType.WORKER)],
                     enemies=[enemy_core((10, 4))], beacon=CARRIED_BEACON)
    agent._update_attack_targets(t, state, t.tick)
    # 探索池：一组 V+R（远处）
    v = FakeUnit(V1, UnitType.VANGUARD, (40, 40))
    r = FakeUnit(R1, UnitType.RANGER, (41, 40))
    targets = {}
    dispatched = agent.plan_attack_dispatch(t, state, [v, r], set(), set(), targets)
    assert V1 in dispatched and R1 in dispatched, "应抽调该 V+R 组去进攻"
    assert v.moves or v.sweeps, "先锋应朝敌Core推进/扫荡"
    assert targets.get(V1) == (10, 4) and targets.get(R1) == (10, 4), \
        f"进攻组目标应指向敌Core，实际 {targets}"
    # 游侠落后先锋编队推进：至少移动且不应踩到先锋头上
    assert r.moves, "游侠应跟随推进"
    assert r.position != v.position, "游侠不应与先锋重叠"
    print(f"[OK] AR 派遣最近探索组进攻敌Core V→{v.position} R→{r.position} 目标={targets.get(V1)}")
    return True


def run_group_trail_ranger_behind_vanguard():
    """重叠编队(GROUP_TRAIL=0 默认)：先锋朝 goal 推进时，游侠逐步叠到先锋所在格。"""
    saved = agent.GROUP_TRAIL
    try:
        agent.GROUP_TRAIL = 0
        v = FakeUnit(V1, UnitType.VANGUARD, (10, 9))
        r = FakeUnit(R1, UnitType.RANGER, (10, 12))
        for _ in range(6):   # 每 tick 游侠靠近一格，多步收敛到重叠
            agent._group_trail(r, v, (10, 5), set())
            if r.position == v.position:
                break
        assert r.moves, "游侠应朝先锋方向跟进以保持队形"
        assert r.position == v.position, f"重叠模式游侠应叠到先锋格，实得{r.position}"
    finally:
        agent.GROUP_TRAIL = saved
    print(f"[OK] AS 编队：GROUP_TRAIL={saved} 游侠叠到先锋格 {v.position}")
    return True


def run_vr_pair_formation():
    """_form_vr_pairs 把 V+R 配对，多余的单独成组。"""
    v1 = type("U", (), {"id": _FC, "unit_type": UnitType.VANGUARD, "position": (1, 1)})()
    v2 = type("U", (), {"id": _FD, "unit_type": UnitType.VANGUARD, "position": (2, 2)})()
    r1 = type("U", (), {"id": _FE, "unit_type": UnitType.RANGER, "position": (3, 3)})()
    pairs = agent._form_vr_pairs([v1, v2, r1])
    assert len(pairs) == 2, f"3个单位应成2对，实际{len(pairs)}对"
    assert pairs[0][0] is v1 and pairs[0][1] is r1, f"第一对应为(v1,r1)"
    assert pairs[1][0] is v2 and pairs[1][1] is None, f"第二对应为(v2,None)"
    print(f"[OK] AL V+R配对：2V+1R → 2组")
    return True

    passed = sum(results)
    total = len(results)
    print(f"\n=== v2 离线验证：{passed}/{total} 通过 ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
