"""引擎移动语义单元测试（防止回归）。

运行：.venv/bin/python -m pytest tests/ -q  或直接 python tests/test_engine.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ahsim.world import World
from ahsim.engine import Engine
from ahsim.entities import Player, Unit, Core


def mk_player(core_pos=(-20, 0)):
    p = Player(0)
    p.core = Core(0, core_pos)
    return p


def resolve(players, plans, world=None):
    if world is None:
        world = World(size=64, seed=1, plain=True)
    e = Engine(world)
    return e.resolve(players, plans, ("ground", 0, 0), 1)


def test_single_move():
    p = mk_player()
    u = Unit(0, "WORKER", (1, 1))
    p.units[u.uid] = u
    resolve({0: p}, {0: {"core": None, "units": {u.uid: ("MOVE", {"direction": "RIGHT"})}}})
    assert u.pos == (2, 1)


def test_same_player_exchange():
    p = mk_player()
    a = Unit(0, "WORKER", (1, 1))
    b = Unit(0, "WORKER", (2, 1))
    p.units[a.uid] = a
    p.units[b.uid] = b
    resolve({0: p}, {0: {"core": None, "units": {
        a.uid: ("MOVE", {"direction": "RIGHT"}),
        b.uid: ("MOVE", {"direction": "LEFT"})}}})
    assert a.pos == (2, 1) and b.pos == (1, 1)


def test_chain_move():
    p = mk_player()
    a = Unit(0, "WORKER", (1, 1))
    b = Unit(0, "WORKER", (2, 1))
    c = Unit(0, "WORKER", (3, 1))
    p.units[a.uid] = a
    p.units[b.uid] = b
    p.units[c.uid] = c
    resolve({0: p}, {0: {"core": None, "units": {
        a.uid: ("MOVE", {"direction": "RIGHT"}),
        b.uid: ("MOVE", {"direction": "RIGHT"}),
        c.uid: ("MOVE", {"direction": "DOWN"})}}})
    assert a.pos == (2, 1) and b.pos == (3, 1) and c.pos == (3, 2)


def test_cross_player_exchange_fails():
    p1 = Player(0)
    p1.core = Core(0, (-20, 0))
    pa = Unit(0, "WORKER", (1, 1))
    p1.units[pa.uid] = pa
    p2 = Player(1)
    p2.core = Core(1, (20, 0))
    pb = Unit(1, "WORKER", (2, 1))
    p2.units[pb.uid] = pb
    resolve({0: p1, 1: p2}, {
        0: {"core": None, "units": {pa.uid: ("MOVE", {"direction": "RIGHT"})}},
        1: {"core": None, "units": {pb.uid: ("MOVE", {"direction": "LEFT"})}}})
    assert pa.pos == (1, 1) and pb.pos == (2, 1)


def test_cross_player_contest_fails():
    p1 = Player(0)
    p1.core = Core(0, (-20, 0))
    pa = Unit(0, "WORKER", (1, 1))
    p1.units[pa.uid] = pa
    p2 = Player(1)
    p2.core = Core(1, (20, 0))
    pb = Unit(1, "WORKER", (2, 2))
    p2.units[pb.uid] = pb
    resolve({0: p1, 1: p2}, {
        0: {"core": None, "units": {pa.uid: ("MOVE", {"direction": "RIGHT"})}},
        1: {"core": None, "units": {pb.uid: ("MOVE", {"direction": "UP"})}}})
    assert pa.pos == (1, 1) and pb.pos == (2, 2)


def test_obstacle_blocks():
    w = World(size=64, seed=1, plain=True)
    w.terrain[1 + w.offset][5 + w.offset] = 2  # 手动放障碍：(5,1)
    p = mk_player()
    u = Unit(0, "WORKER", (4, 1))  # (5,1) 是障碍 → 移动被挡
    p.units[u.uid] = u
    resolve({0: p}, {0: {"core": None, "units": {u.uid: ("MOVE", {"direction": "RIGHT"})}}}, w)
    assert u.pos == (4, 1)


def test_core_self_destruct_clears_fleet_and_respawns():
    """v0.14：自毁移除整支舰队，并在同一 Tick 立即重生。"""
    p = mk_player(core_pos=(0, 0))
    worker = Unit(0, "WORKER", (1, 0))
    worker.cargo = 2
    p.units[worker.uid] = worker
    p.core.resources = 20
    _beacon, events = resolve(
        {0: p}, {0: {"core": ("SELF_DESTRUCT", {}), "units": {}}})
    assert p.core is not None
    assert len(p.units) == 1
    assert next(iter(p.units.values())).utype == "WORKER"
    assert all(u.uid != worker.uid for u in p.units.values())
    assert any(e.get("type") == "CORE_DESTROYED"
               and e.get("reason") == "SELF_DESTRUCT" for e in events)
    assert any(e.get("type") == "CORE_RESPAWNED" for e in events)


def test_strategy_finishes_four_worker_bootstrap_before_frontline():
    """高库存也不能让开局经济在四名 Worker 之前提前转 Vanguard。"""
    from ahsim.game import Game
    from strategies.heuristic import HeuristicStrategy
    from strategies.randombot import RandomBot

    strategy = HeuristicStrategy(bounds=(-128, 127, -128, 127))
    game = Game({0: strategy, 1: RandomBot(seed=2)}, size=256, seed=7,
                spawn_profile={0: {"res": 10}}, max_ticks=1)
    obs = game.build_observation(game.players[0])
    assert strategy.decide(obs)["core"] == (
        "SPAWN", {"unit_type": "WORKER"})


def test_defense_spawn_uses_dynamic_v014_prices():
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (1, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    obs = _strategy_observation(tick=1, unit=worker, resources=13)
    obs.population = 20
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    # At population 20 Ranger costs 16 and Vanguard costs 13.
    assert strategy._decide_core(obs, (0, 0), 1.0, None, True, (50, 50)) == (
        "SPAWN", {"unit_type": "VANGUARD"})


def test_threat_ratio_excludes_core_durability():
    """Core can absorb damage but cannot be counted as a second fighting force."""
    from strategies.heuristic import HeuristicStrategy
    vanguard = {"uid": 1, "utype": "VANGUARD", "pos": (1, 0),
                "hp": 4, "cargo": 0, "carries_beacon": False}
    enemy = ((2, 0), 4, "VANGUARD", 2)
    obs = _strategy_observation(tick=1, unit=vanguard)
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    ratio = strategy._threat_ratio(obs, (0, 0), [enemy], [])

    # Equal mobile forces plus the near-Core risk premium must not look like
    # the old 4 / (4 + Core 10) = 0.286 overwhelming advantage.
    assert ratio >= 1.0, ratio


def test_fighter_deficit_saves_for_combat_unit_instead_of_worker():
    """The agent2 18W/4-fighter composition must not buy a 19th Worker."""
    from strategies.heuristic import HeuristicStrategy
    workers = [
        {"uid": i, "utype": "WORKER", "pos": (i - 9, 8),
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for i in range(1, 19)
    ]
    fighters = [
        {"uid": 101, "utype": "VANGUARD", "pos": (1, 0),
         "hp": 4, "cargo": 0, "carries_beacon": False},
        {"uid": 102, "utype": "VANGUARD", "pos": (0, 1),
         "hp": 4, "cargo": 0, "carries_beacon": False},
        {"uid": 103, "utype": "RANGER", "pos": (3, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False},
        {"uid": 104, "utype": "RANGER", "pos": (0, 3),
         "hp": 2, "cargo": 0, "carries_beacon": False},
    ]
    obs = _strategy_observation(tick=1, unit=workers[0], resources=7)
    obs.units = workers + fighters
    obs.population = 22
    obs.core["capacity"] = 110
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    assert strategy._decide_core(
        obs, (0, 0), 0.0, None, False, (50, 50)) is None
    obs.core["resources"] = 13
    assert strategy._decide_core(
        obs, (0, 0), 0.0, None, False, (50, 50)) == (
            "SPAWN", {"unit_type": "VANGUARD"})


def test_production_builds_four_workers_then_complete_home_squad():
    """Recovery economy must establish 4W, then fill the permanent 1V1R guard."""
    from strategies.heuristic import HeuristicStrategy
    workers = [
        {"uid": i, "utype": "WORKER", "pos": (i, 2),
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for i in range(1, 5)
    ]
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    def spawn_for(units, resources=20):
        obs = _strategy_observation(
            tick=1, unit=units[0], resources=resources)
        obs.units = units
        obs.population = len(units)
        return strategy._decide_core(
            obs, (0, 0), 0.0, None, False, (50, 50))

    first_vanguard = {
        "uid": 101, "utype": "VANGUARD", "pos": (1, 0),
        "hp": 4, "cargo": 0, "carries_beacon": False,
    }
    second_vanguard = {
        "uid": 102, "utype": "VANGUARD", "pos": (0, 1),
        "hp": 4, "cargo": 0, "carries_beacon": False,
    }
    assert spawn_for(workers[:3]) == (
        "SPAWN", {"unit_type": "WORKER"})
    assert spawn_for(workers) == ("SPAWN", {"unit_type": "VANGUARD"})
    assert spawn_for(workers + [first_vanguard]) == (
        "SPAWN", {"unit_type": "RANGER"})
    assert spawn_for(workers + [first_vanguard, second_vanguard]) == (
        "SPAWN", {"unit_type": "RANGER"})


def test_home_guard_ids_persist_and_replace_casualties():
    """The 1V1R home assignment stays stable and promotes a survivor on loss."""
    from strategies.heuristic import HeuristicStrategy
    units = [
        {"uid": 101, "utype": "VANGUARD", "pos": (1, 0),
         "hp": 4, "cargo": 0, "carries_beacon": False},
        {"uid": 102, "utype": "VANGUARD", "pos": (2, 0),
         "hp": 4, "cargo": 0, "carries_beacon": False},
        {"uid": 103, "utype": "VANGUARD", "pos": (8, 0),
         "hp": 4, "cargo": 0, "carries_beacon": False},
        {"uid": 201, "utype": "RANGER", "pos": (3, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False},
        {"uid": 202, "utype": "RANGER", "pos": (9, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False},
    ]
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    obs = _strategy_observation(tick=1, unit=units[0])
    obs.units = units
    obs.population = len(units)
    strategy.decide(obs)

    assert strategy._home_vanguards == {101}
    assert strategy._home_rangers == {201}

    survivors = [u for u in units if u["uid"] != 101]
    survivors[0]["pos"] = (20, 0)  # Existing guard remains assigned when far.
    obs = _strategy_observation(tick=2, unit=survivors[0])
    obs.units = survivors
    obs.population = len(survivors)
    strategy.decide(obs)

    assert strategy._home_vanguards == {103}
    assert strategy._home_rangers == {201}
    # 仅最近的 103 被提拔为守家先锋；较远的 102 仍是普通先锋（非 home_vanguard）
    assert strategy._decisions[102]["tag"] != "home_vanguard"
    assert strategy._decisions[103]["tag"] == "home_vanguard"


def test_agent2_siege_replay_limits_interceptors_and_uses_melee_screen():
    """Regression for tick 64356: defend locally without a full-army chase."""
    from strategies.heuristic import HeuristicStrategy
    workers = [
        {"uid": i, "utype": "WORKER", "pos": (-9 + i, 8),
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for i in range(1, 19)
    ]
    fighters = [
        {"uid": 101, "utype": "VANGUARD", "pos": (1, 0),
         "hp": 4, "cargo": 0, "carries_beacon": False},
        {"uid": 102, "utype": "VANGUARD", "pos": (0, 1),
         "hp": 4, "cargo": 0, "carries_beacon": False},
        {"uid": 103, "utype": "RANGER", "pos": (3, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False},
        {"uid": 104, "utype": "RANGER", "pos": (0, 3),
         "hp": 2, "cargo": 0, "carries_beacon": False},
    ]
    enemies = [
        {"uid": 201 + i, "utype": "VANGUARD", "pos": pos, "hp": 4}
        for i, pos in enumerate(((8, 0), (8, 1), (7, 2), (6, 3)))
    ]
    obs = _strategy_observation(
        tick=64356, unit=workers[0], enemies=enemies, resources=0)
    obs.units = workers + fighters
    obs.population = 22
    obs.core["capacity"] = 110
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert strategy._siege_active
    assert strategy._attack_point is None
    assert len(strategy._block_workers) == 3, strategy._block_workers
    fighter_tags = {strategy._decisions[u["uid"]]["tag"] for u in fighters}
    assert fighter_tags <= {
        "siege_vanguard", "siege_ranger", "shoot",
        "siege_intercept_vanguard", "siege_ranger_angle",
        "siege_ranger_advance", "siege_ranger_cover",
        "siege_ranger_cover_advance",
    }, fighter_tags
    assert not fighter_tags.intersection({"attack", "track", "raid"}), fighter_tags
    assert all(strategy._decisions[uid]["tag"] == "siege_block"
               for uid in strategy._block_workers), strategy._decisions
    # Seven resources would afford another dynamic-price Worker, but siege
    # reserves them for a Vanguard/Ranger instead.
    obs.core["resources"] = 7
    assert strategy._decide_core(
        obs, (0, 0), 2.0, None, True, (50, 50)) is None
    assert plan["core"] is None


def test_ranger_doorstep_siege_evacuates_workers_and_sends_interceptors():
    """A ranged door blocker must draw fighters, never a Worker screen."""
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (0, 4),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    vanguards = [
        {"uid": uid, "utype": "VANGUARD", "pos": pos,
         "hp": 4, "cargo": 0, "carries_beacon": False}
        for uid, pos in ((101, (0, 1)), (102, (1, 0)), (103, (-1, 0)))
    ]
    rangers = [
        {"uid": uid, "utype": "RANGER", "pos": pos,
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for uid, pos in ((201, (1, 2)), (202, (-1, 2)), (203, (2, 1)),
                         (204, (-2, 1)))
    ]
    enemy = {"uid": 301, "utype": "RANGER", "pos": (0, 6), "hp": 2}
    obs = _strategy_observation(
        tick=67046, unit=worker, enemies=[enemy], resources=0)
    obs.units = [worker] + vanguards + rangers
    obs.population = len(obs.units)
    obs.core["capacity"] = obs.population * 5
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert strategy._siege_active
    assert strategy._defense_type == "RANGER"
    assert strategy._block_workers == set(), strategy._block_workers
    assert plan["units"][1] == ("MOVE", {"direction": "UP"}), plan
    assert strategy._decisions[1]["tag"] == "flee"
    vanguard_tags = [strategy._decisions[u["uid"]]["tag"]
                      for u in vanguards]
    assert vanguard_tags.count("siege_intercept_vanguard") == 2, vanguard_tags
    assert "siege_vanguard" in vanguard_tags, vanguard_tags
    ranger_tags = [strategy._decisions[u["uid"]]["tag"] for u in rangers]
    active_rangers = {
        "shoot", "siege_ranger_angle", "siege_ranger_advance",
        "siege_ranger_cover", "siege_ranger_cover_advance",
    }
    assert any(tag in active_rangers for tag in ranger_tags), ranger_tags
    assert sum(tag in active_rangers for tag in ranger_tags) <= 3, ranger_tags


def test_ranger_prefers_hidden_corner_cover_that_keeps_a_clear_shot():
    """Supercover side obstacles hide a Ranger without blocking diagonal fire."""
    from strategies.heuristic import HeuristicStrategy
    ranger = {"uid": 1, "utype": "RANGER", "pos": (0, 1),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 2, "utype": "RANGER", "pos": (2, 2), "hp": 2}
    obs = _strategy_observation(tick=1, unit=ranger, enemies=[enemy])
    # The Core is the spotter.  From the candidate (0,0), obstacle (1,0)
    # blocks the enemy's supercover view but is beside the (1,1) shot ray.
    obs.core["pos"] = (-2, 2)
    obs.obstacles = {(1, 0)}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert plan["units"][1] == ("MOVE", {"direction": "UP"}), plan
    assert strategy._decisions[1]["tag"] == "siege_ranger_cover"
    assert not strategy._known_enemy_sees((0, 0), obs)
    assert strategy._shot_valid(ranger, (0, 0), (2, 2))
    # Cover relative to the target is not enough when another hostile supplies
    # vision; the estimate must use the union of all known enemy sources.
    obs.enemies.append(
        {"uid": 3, "utype": "WORKER", "pos": (0, 2), "hp": 2})
    assert strategy._known_enemy_sees((0, 0), obs)


def test_supercover_vision_sees_obstacle_but_not_cell_behind_corner():
    from ahsim.vision import supercover_line, visible_cells
    obstacles = {(1, 0)}
    visible = set(visible_cells(
        0, 0, 4, lambda x, y: (x, y) in obstacles))

    assert supercover_line(0, 0, 2, 2) == [
        (0, 0), (1, 0), (0, 1), (1, 1),
        (2, 1), (1, 2), (2, 2),
    ]
    assert (1, 0) in visible
    assert (2, 2) not in visible


def test_siege_persists_across_visibility_gap():
    """A one-Tick visibility gap cannot send defenders back into pursuit."""
    from strategies.heuristic import HeuristicStrategy
    ranger = {"uid": 1, "utype": "RANGER", "pos": (3, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 2, "utype": "VANGUARD", "pos": (8, 0), "hp": 4}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy.decide(_strategy_observation(
        tick=100, unit=ranger, enemies=[enemy], resources=0))

    plan = strategy.decide(_strategy_observation(
        tick=101, unit=ranger, enemies=[], resources=0))

    assert strategy._siege_active
    assert strategy._decisions[1]["tag"] == "siege_ranger"
    assert 1 not in plan["units"], plan


def test_overwhelmed_damaged_core_starts_emergency_migration():
    """With no guard left, a damaged Core must attempt escape before death."""
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (0, 1),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemies = [
        {"uid": 2, "utype": "VANGUARD", "pos": (2, 0), "hp": 4},
        {"uid": 3, "utype": "VANGUARD", "pos": (3, 0), "hp": 4},
    ]
    obs = _strategy_observation(
        tick=200, unit=worker, enemies=enemies, resources=0)
    obs.core.update({"hp": 2, "shield": 1})
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    action = strategy.decide(obs)["core"]

    assert action == ("START_MOVE", {"direction": "LEFT"}), action


def test_fractional_block_count_is_not_truncated_to_zero():
    """A gene in (0, 1) still assigns one offensive blocking Worker."""
    from strategies.heuristic import HeuristicStrategy
    workers = [
        {"uid": i, "utype": "WORKER", "pos": (i, 1),
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for i in range(1, 5)
    ]
    enemy = {"uid": 9, "utype": "WORKER", "pos": (15, 0), "hp": 2}
    obs = _strategy_observation(tick=1, unit=workers[0], enemies=[enemy])
    obs.units = workers
    obs.population = len(workers)
    strategy = HeuristicStrategy(
        genes={"block_count": 0.815}, bounds=(-64, 63, -64, 63))

    strategy.decide(obs)

    assert len(strategy._block_workers) == 1, strategy._block_workers


def test_reward_goal_locks_core_and_unit_resource_spending():
    """达到 150 后禁止 Core 支出和 Unit 治疗，确保兑换库存不回落。"""
    from strategies.heuristic import HeuristicStrategy, RESOURCE_GOAL
    ranger = {"uid": 1, "utype": "RANGER", "pos": (0, 0),
              "hp": 1, "cargo": 0, "carries_beacon": False}
    obs = _strategy_observation(
        tick=500, unit=ranger, resources=RESOURCE_GOAL)
    obs.population = 32
    obs.core.update({"capacity": 160, "hp": 1, "shield": 0})
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert plan["core"] is None, plan["core"]
    assert plan["units"].get(ranger["uid"], (None,))[0] != "HEAL", plan
    assert strategy._vault_active


def test_population_30_to_32_uses_cheap_workers_for_capacity_buffer():
    """守库缓冲人口只补便宜 Worker，不在高价段购买 Ranger。"""
    from strategies.heuristic import HeuristicStrategy
    units = [
        {"uid": uid, "utype": "WORKER", "pos": (uid, 1),
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for uid in range(1, 18)
    ]
    obs = _strategy_observation(tick=400, unit=units[0], resources=11)
    obs.units = units
    obs.population = 30
    obs.core["capacity"] = 150
    strategy = HeuristicStrategy(
        genes={"worker_ratio": 0.1, "vanguard_share": 0.0},
        bounds=(-64, 63, -64, 63))

    action = strategy._decide_core(obs, (0, 0), 0.0, None, False, (50, 50))

    assert action == ("SPAWN", {"unit_type": "WORKER"}), action


def test_vault_worker_stops_harvesting_and_returns_to_staging_ring():
    """满仓后空载 Worker 不再采矿/侦察，而是回到 Core 外围分散待命。"""
    from strategies.heuristic import HeuristicStrategy, RESOURCE_GOAL
    worker = {"uid": 1, "utype": "WORKER", "pos": (20, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    obs = _strategy_observation(tick=500, unit=worker, resources=RESOURCE_GOAL)
    obs.population = 32
    obs.core["capacity"] = 160
    obs.resources = {(20, 0)}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert plan["units"][worker["uid"]][0] == "MOVE", plan
    assert strategy._decisions[worker["uid"]]["tag"] == "vault_worker"
    goal = strategy._decisions[worker["uid"]]["goal"]
    assert 6 <= abs(goal[0]) + abs(goal[1]) <= 10, goal


def test_vault_loaded_worker_fills_population_buffer_capacity():
    """进入守库态后已载货 Worker 仍可把 150 补到 160，形成减员缓冲。"""
    from strategies.heuristic import HeuristicStrategy, RESOURCE_GOAL
    worker = {"uid": 1, "utype": "WORKER", "pos": (0, 0),
              "hp": 2, "cargo": 1, "carries_beacon": False}
    obs = _strategy_observation(tick=500, unit=worker, resources=RESOURCE_GOAL)
    obs.population = 32
    obs.core["capacity"] = 160
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert plan["units"][worker["uid"]] == ("DEPOSIT", {}), plan


def test_vault_mode_exits_after_redemption():
    """兑换使库存降到目标以下时，下一 Tick 自动恢复采集。"""
    from strategies.heuristic import HeuristicStrategy, RESOURCE_GOAL
    worker = {"uid": 1, "utype": "WORKER", "pos": (5, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    full = _strategy_observation(tick=500, unit=worker, resources=RESOURCE_GOAL)
    full.population = 32
    full.core["capacity"] = 160
    strategy.decide(full)

    redeemed = _strategy_observation(tick=501, unit=worker, resources=0)
    redeemed.population = 32
    redeemed.core["capacity"] = 160
    redeemed.resources = {(5, 0)}
    plan = strategy.decide(redeemed)

    assert not strategy._vault_active
    assert plan["units"][worker["uid"]] == ("HARVEST", {}), plan


def test_zero_resource_core_can_migrate_out_of_starvation():
    """迁移免费；库存为 0 时也必须能离开已经枯竭的区域。"""
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (11, 10),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    obs = _strategy_observation(tick=500, unit=worker, resources=0)
    obs.core["pos"] = (10, 10)
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy._starve_since = 1
    strategy.mem.area_seen = {(x, 0): 1 for x in range(31)}

    action = strategy._decide_core(
        obs, (10, 10), 0.0, None, False, (50, 50))

    assert action is not None and action[0] == "START_MOVE", action


def test_workers_claim_a_visible_resource_one_to_one():
    """近矿快捷分支也必须遵守资源点单 Worker 认领。"""
    from strategies.heuristic import HeuristicStrategy
    workers = [
        {"uid": 1, "utype": "WORKER", "pos": (2, 1),
         "hp": 2, "cargo": 0, "carries_beacon": False},
        {"uid": 2, "utype": "WORKER", "pos": (1, 2),
         "hp": 2, "cargo": 0, "carries_beacon": False},
    ]
    obs = _strategy_observation(tick=1, unit=workers[0], resources=0)
    obs.units = workers
    obs.resources = {(2, 2)}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    destinations = []
    for worker in workers:
        action = plan["units"].get(worker["uid"])
        if action and action[0] == "MOVE":
            dx, dy = {"RIGHT": (1, 0), "LEFT": (-1, 0),
                      "UP": (0, -1), "DOWN": (0, 1)}[action[1]["direction"]]
            if (worker["pos"][0] + dx, worker["pos"][1] + dy) == (2, 2):
                destinations.append(worker["uid"])
    assert len(destinations) == 1, plan


def test_vanguard_sweeps_adjacent_enemy_before_defense_move():
    """回防状态下相邻敌人仍应先执行确定命中的 SWEEP。"""
    from strategies.heuristic import HeuristicStrategy
    vanguard = {"uid": 1, "utype": "VANGUARD", "pos": (3, 0),
                "hp": 4, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 2, "utype": "RANGER", "pos": (2, 0), "hp": 2}
    obs = _strategy_observation(tick=1, unit=vanguard, enemies=[enemy])
    obs.core["pos"] = (0, 0)
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    action = strategy.decide(obs)["units"][1]

    assert action == ("SWEEP", {"direction": "LEFT"}), action


def test_ranger_disengages_from_adjacent_vanguard_before_shooting():
    """A 2 HP Ranger should preserve distance instead of trading one shot."""
    from strategies.heuristic import HeuristicStrategy
    ranger = {"uid": 1, "utype": "RANGER", "pos": (1, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 2, "utype": "VANGUARD", "pos": (2, 0), "hp": 4}
    obs = _strategy_observation(tick=1, unit=ranger, enemies=[enemy])
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    action = strategy.decide(obs)["units"][1]

    assert action == ("MOVE", {"direction": "LEFT"}), action
    assert strategy._decisions[1]["tag"] == "ranger_disengage"


def test_idle_vanguard_explores_away_from_core_instead_of_rallying():
    """非守家 Vanguard 空闲时去远处探索，不在 Core 门口聚集/拉扯。"""
    from strategies.heuristic import HeuristicStrategy
    vanguards = [
        {"uid": uid, "utype": "VANGUARD", "pos": pos,
         "hp": 4, "cargo": 0, "carries_beacon": False}
        for uid, pos in ((1, (2, 0)), (2, (0, 2)), (3, (1, 0)))
    ]
    obs = _strategy_observation(tick=1, unit=vanguards[0], resources=0)
    obs.units = vanguards
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy._home_vanguards = {1, 2}

    plan = strategy.decide(obs)

    # 空闲优先探索（explore 在 park 之前），目标应远离 Core 而非在门口
    assert strategy._decisions[3]["tag"] == "explore", strategy._decisions
    goal = strategy._decisions[3]["goal"]
    assert goal and strategy._dist(goal, (0, 0)) >= 2, goal
    assert plan["units"][3][0] == "MOVE", plan


def test_stacked_vanguards_spread_to_distinct_cells_before_parking():
    """叠格的非守家 Vanguard 不能沿相同 parking 路径永久同步移动。"""
    from strategies.heuristic import HeuristicStrategy
    vanguards = [
        {"uid": uid, "utype": "VANGUARD", "pos": pos,
         "hp": 4, "cargo": 0, "carries_beacon": False}
        for uid, pos in (
            (1, (2, 0)), (2, (0, 2)), (3, (3, 0)), (4, (3, 0))
        )
    ]
    obs = _strategy_observation(tick=1, unit=vanguards[0], resources=0)
    obs.units = vanguards
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy._home_vanguards = {1, 2}

    plan = strategy.decide(obs)

    assert strategy._decisions[3]["tag"] == "spread", strategy._decisions
    assert strategy._decisions[4]["tag"] == "spread", strategy._decisions
    directions = {plan["units"][uid][1]["direction"] for uid in (3, 4)}
    assert len(directions) == 2, plan


def test_rangers_bracket_a_one_hp_target_without_duplicate_current_shots():
    """One shot covers current position; spare fire covers an escape cell."""
    from strategies.heuristic import HeuristicStrategy
    rangers = [
        {"uid": 1, "utype": "RANGER", "pos": (0, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False},
        {"uid": 2, "utype": "RANGER", "pos": (0, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False},
    ]
    enemy = {"uid": 3, "utype": "WORKER", "pos": (2, 0), "hp": 1}
    obs = _strategy_observation(tick=1, unit=rangers[0], enemies=[enemy], resources=0)
    obs.units = rangers
    obs.core["pos"] = (-5, 0)
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    shots = [a for a in plan["units"].values() if a[0] == "SHOOT"]
    cells = [tuple(action[1]["expected_cell"]) for action in shots]
    assert cells.count((2, 0)) == 1, plan
    assert len(shots) == 2 and len(set(cells)) == 2, plan


def test_ranger_explore_path_is_not_overridden_by_oscillation_breaker():
    """粘性探索路径上的 A-B-A 由路径层恢复，不能再随机推离。"""
    from strategies.heuristic import HeuristicStrategy
    rangers = [
        {"uid": uid, "utype": "RANGER", "pos": (0, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for uid in (1, 2, 3)
    ]
    obs = _strategy_observation(tick=10, unit=rangers[2], resources=0)
    obs.units = rangers
    obs.core["pos"] = (-5, 0)
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy._unit_hist[3] = [(0, 0), (1, 0)]
    strategy._explore_goal[3] = ((10, 0), 1)

    strategy.decide(obs)

    assert strategy._decisions[3]["tag"] != "osc_break", strategy._decisions


def test_ranger_sticky_explore_goal_skips_candidate_search():
    """粘性目标有效时不能每 Tick 重跑昂贵的探索候选 BFS。"""
    from strategies.heuristic import HeuristicStrategy
    rangers = [
        {"uid": uid, "utype": "RANGER", "pos": (3, 0),
         "hp": 2, "cargo": 0, "carries_beacon": False}
        for uid in (1, 2, 3)
    ]
    obs = _strategy_observation(tick=10, unit=rangers[2], resources=0)
    obs.units = rangers
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy._explore_goal[3] = ((10, 0), 9)

    def unexpected_search(*_args, **_kwargs):
        raise AssertionError("sticky explore goal should skip candidate search")

    strategy._nearest_unvisited = unexpected_search
    plan = strategy.decide(obs)

    assert strategy._decisions[3]["tag"] == "explore", strategy._decisions
    assert plan["units"][3][0] == "MOVE", plan


def _strategy_observation(*, tick, unit, enemies=(), resources=0):
    from ahsim.observation import Observation
    return Observation(
        player_id=0, tick=tick,
        core={"uid": 99, "pos": (0, 0), "hp": 5, "shield": 5,
              "resources": resources, "migration": None, "capacity": 10},
        units=[unit], enemies=list(enemies), enemy_cores=[],
        resources=set(), obstacles=set(),
        beacon={"position": [50, 50], "status": "UNKNOWN"},
        population=1, visible_cells=set(), prev_events=[])


def test_worker_scout_goal_remains_sticky():
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (1, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy.decide(_strategy_observation(tick=1, unit=worker))
    first = strategy._scout_goal[1]
    strategy._scout_stage[1] = (strategy._scout_stage.get(1, 0) + 4) % 8
    strategy.decide(_strategy_observation(tick=2, unit=worker))
    assert strategy._scout_goal[1] == first


def test_never_harvested_worker_enters_hunger_search():
    from strategies.heuristic import HeuristicStrategy
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy._starve_since = 1
    strategy._scout_taken = set()
    obs = _strategy_observation(
        tick=250, unit={"uid": 1, "utype": "WORKER", "pos": (1, 0),
                        "hp": 2, "cargo": 0, "carries_beacon": False})
    goal = strategy._patrol_point(obs, 1, (0, 0))
    assert strategy._dist(goal, (0, 0)) in (8, 16, 24, 32, 40)


def test_wounded_ranger_returns_from_outside_rally_radius():
    from strategies.heuristic import HeuristicStrategy
    ranger = {"uid": 1, "utype": "RANGER", "pos": (10, 0),
              "hp": 1, "cargo": 0, "carries_beacon": False}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    plan = strategy.decide(_strategy_observation(tick=1, unit=ranger,
                                                  resources=5))
    assert plan["units"][1] == ("MOVE", {"direction": "LEFT"})


def test_low_hp_retreat_overrides_manual_backoff_and_oscillation():
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (5, 0),
              "hp": 1, "cargo": 0, "carries_beacon": False}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    strategy._manual_goto[1] = (10, 0)
    strategy._move_backoff[1] = 20
    strategy._unit_hist[1] = [(5, 0), (5, 1)]

    plan = strategy.decide(_strategy_observation(
        tick=10, unit=worker, resources=5))

    assert plan["units"][1] == ("MOVE", {"direction": "LEFT"}), plan
    assert strategy._decisions[1]["tag"] == "retreat_low_hp"


def test_low_hp_unit_returns_to_core_cover_without_heal_resources():
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (5, 0),
              "hp": 1, "cargo": 0, "carries_beacon": False}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(_strategy_observation(
        tick=10, unit=worker, resources=0))

    assert plan["units"][1] == ("MOVE", {"direction": "LEFT"}), plan
    assert strategy._decisions[1]["tag"] == "retreat_low_hp"


def test_low_hp_retreat_uses_a_full_cell_that_is_being_vacated():
    from strategies.heuristic import HeuristicStrategy
    wounded = {"uid": 69, "utype": "WORKER", "pos": (-67, 158),
               "hp": 1, "cargo": 0, "carries_beacon": False}
    leaving = {"uid": 1, "utype": "WORKER", "pos": (-67, 157),
               "hp": 2, "cargo": 0, "carries_beacon": False}
    holding = {"uid": 2, "utype": "RANGER", "pos": (-67, 157),
               "hp": 2, "cargo": 0, "carries_beacon": False}
    obs = _strategy_observation(tick=66129, unit=wounded, resources=5)
    obs.core["pos"] = (-68, 155)
    obs.units = [wounded, leaving, holding]
    obs.obstacles = {(-68, 158), (-66, 158), (-68, 156)}
    strategy = HeuristicStrategy(bounds=(-256, 255, -256, 255))
    strategy.mem.obstacles.update(obs.obstacles)
    strategy._core_cell = tuple(obs.core["pos"])
    strategy._full_cells = {(-67, 157)}
    strategy._planned_departures[(-67, 157)] = 1

    active, action = strategy._low_hp_retreat(
        wounded, obs, tuple(obs.core["pos"]), max_hp=2)

    assert active
    assert action == ("MOVE", {"direction": "UP"}), action


def test_wounded_worker_is_not_selected_for_siege_screen():
    from strategies.heuristic import HeuristicStrategy
    workers = [
        {"uid": uid, "utype": "WORKER", "pos": pos,
         "hp": hp, "cargo": 0, "carries_beacon": False}
        for uid, pos, hp in (
            (1, (1, 1), 1), (2, (2, 1), 2),
            (3, (3, 1), 2), (4, (4, 1), 2), (5, (5, 1), 2))
    ]
    enemy = {"uid": 9, "utype": "VANGUARD", "pos": (8, 0), "hp": 4}
    obs = _strategy_observation(
        tick=10, unit=workers[0], enemies=[enemy], resources=5)
    obs.units = workers
    obs.population = len(workers)
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    strategy.decide(obs)

    assert 1 not in strategy._block_workers
    assert len(strategy._block_workers) == 3, strategy._block_workers


def test_worker_keeps_evading_after_fighter_leaves_vision():
    from strategies.heuristic import HeuristicStrategy
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    worker = {"uid": 1, "utype": "WORKER", "pos": (0, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 2, "utype": "RANGER", "pos": (3, 0), "hp": 2}
    first = _strategy_observation(tick=10, unit=worker, enemies=[enemy])
    first.core["pos"] = (-20, 0)
    assert strategy.decide(first)["units"][1] == (
        "MOVE", {"direction": "LEFT"})

    worker = {**worker, "pos": (-1, 0)}
    second = _strategy_observation(tick=11, unit=worker, enemies=[])
    second.core["pos"] = (-20, 0)
    plan = strategy.decide(second)

    assert plan["units"][1] == ("MOVE", {"direction": "LEFT"}), plan
    assert strategy._decisions[1]["tag"] == "flee_memory"


def test_escape_can_share_a_cell_with_one_ally():
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (0, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    ally = {"uid": 2, "utype": "WORKER", "pos": (-1, 0),
            "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 3, "utype": "VANGUARD", "pos": (2, 0), "hp": 4}
    obs = _strategy_observation(tick=10, unit=worker, enemies=[enemy])
    obs.core["pos"] = (-20, 0)
    obs.units = [worker, ally]
    obs.population = 2
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert plan["units"][1] == ("MOVE", {"direction": "LEFT"}), plan


def test_isolated_ranger_disengages_from_enemy_ranger_before_shooting():
    from strategies.heuristic import HeuristicStrategy
    ranger = {"uid": 1, "utype": "RANGER", "pos": (0, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 2, "utype": "RANGER", "pos": (3, 0), "hp": 2}
    obs = _strategy_observation(tick=10, unit=ranger, enemies=[enemy])
    obs.core["pos"] = (-20, 0)
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))

    plan = strategy.decide(obs)

    assert plan["units"][1] == ("MOVE", {"direction": "LEFT"}), plan
    assert strategy._decisions[1]["tag"] == "ranger_disengage"


def test_worker_evades_nearby_fighter_without_global_threat_ratio():
    from strategies.heuristic import HeuristicStrategy
    worker = {"uid": 1, "utype": "WORKER", "pos": (1, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = {"uid": 2, "utype": "RANGER", "pos": (3, 0), "hp": 2}
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    plan = strategy.decide(_strategy_observation(tick=1, unit=worker,
                                                  enemies=[enemy]))
    assert plan["units"][1] == ("MOVE", {"direction": "LEFT"})


def test_rangers_split_fire_between_lead_current_and_turn_cells():
    from strategies.heuristic import HeuristicStrategy
    strategy = HeuristicStrategy(bounds=(-64, 63, -64, 63))
    ranger = {"uid": 1, "utype": "RANGER", "pos": (0, 0),
              "hp": 2, "cargo": 0, "carries_beacon": False}
    enemy = [((2, 0), 2, "WORKER", 2)]
    strategy.mem.enemy_prev[2] = (1, 0)
    strategy.mem.enemy_dir_streak[2] = 1
    assert strategy._best_shot(ranger, (0, 0), enemy, []) == (3, 0)
    assert strategy._shot_modes[1] == "shoot_lead"
    strategy._shot_claims[(3, 0)] = 1
    assert strategy._best_shot(ranger, (0, 0), enemy, []) == (2, 0)
    assert strategy._shot_modes[1] == "shoot"
    strategy._shot_claims[(2, 0)] = 1
    assert strategy._best_shot(ranger, (0, 0), enemy, []) == (1, 0)
    assert strategy._shot_modes[1] == "shoot_bracket"


def test_prescreen_combines_cross_seed_variance():
    from evolve.fitness import combine_details
    a = {"score": 10.0, "fitness_std": 0.0,
         "fitness_worst": 10.0, "fitness_p10": 10.0}
    b = {"score": 20.0, "fitness_std": 0.0,
         "fitness_worst": 20.0, "fitness_p10": 20.0}
    combined = combine_details([(a, 1, 10.0), (b, 1, 20.0)])
    assert round(combined["fitness_std"], 6) == round(50 ** 0.5, 6)
    assert combined["fitness_worst"] == 10.0
    assert combined["fitness_p10"] == 10.0


def test_multistage_early_snapshot_fields_accumulate_across_seeds():
    from evolve.fitness import _agg_from
    acc = {"harvested": 0, "deposited": 0, "damage": 0, "pop": 0,
           "res": 0, "lost": 0, "respawn": 0, "beacon": 0,
           "alive_ticks": 0, "heal_cost": 0, "repair_cost": 0,
           "spawn_cost": 0, "overflow_destroyed": 0,
           "resources_lost": 0}
    _agg_from({"final_population": 4, "final_resources": 6}, acc)
    _agg_from({"final_population": 8, "final_resources": 10}, acc)
    assert acc["pop"] == 12
    assert acc["res"] == 16


def test_dynamic_spawn_price_v014():
    """rules v0.14：动态单位价格——前 20 单位基础价，21+ 按
    round_half_up(base × 1.3^k) 涨价（官方表：Worker 5/7/8/11）。"""
    from ahsim.config import unit_cost
    assert unit_cost(5, 19) == 5      # N=19：基础价（第 20 个）
    assert unit_cost(5, 20) == 7      # N=20：第 21 个，k=1
    assert unit_cost(5, 24) == 7
    assert unit_cost(5, 25) == 8      # k=2
    assert unit_cost(10, 20) == 13
    assert unit_cost(12, 30) == 26    # k=3
    assert unit_cost(5, 100) == 433   # 官方高人口价格表
    assert unit_cost(10, 100) == 865
    assert unit_cost(12, 100) == 1038
    w = World(size=64, seed=1, plain=True)
    e = Engine(w)
    p = Player(0)
    p.core = Core(0, (0, 0))
    p.core.resources = 100
    for i in range(20):
        u = Unit(0, "WORKER", (1 + i // 2, i % 2))
        p.units[u.uid] = u
    plan = {0: {"core": ("SPAWN", {"unit_type": "WORKER"}), "units": {}}}
    _beacon, events = e.resolve({0: p}, plan, ("ground", 0, 0), 1)
    spawned = [ev for ev in events if ev.get("type") == "UNIT_SPAWNED"]
    assert spawned and spawned[0].get("cost") == 7, events
    assert p.core.resources == 100 - 7


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
