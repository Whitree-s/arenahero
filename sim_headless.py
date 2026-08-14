#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arena Hero 无头仿真器（不需要官方 SDK / 联网）。

用 arena_agent.plan_turn_v2 驱动一个自建的小世界，跑 N 个 tick，
把每 tick 的态势与事件写入 event_log.json，供 visualize_events.html 回放。

用法：
    python sim_headless.py            # 默认 150 tick
    python sim_headless.py 300        # 指定 tick 数
"""
from __future__ import annotations

import os
import sys
import json
import time
from uuid import uuid4

# 让 arena_agent 能 import 到桩版 arena_hero
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stubs"))
sys.path.insert(0, HERE)

# 仿真用的参数（覆盖 agent 默认，让战斗/探索更快触发）
os.environ["AH_TARGET_WORKERS"] = os.environ.get("AH_TARGET_WORKERS", "12")
os.environ["AH_MIN_WORKERS_FOR_COMBAT"] = os.environ.get("AH_MIN_WORKERS_FOR_COMBAT", "4")
os.environ["AH_MAX_VANGUARDS"] = os.environ.get("AH_MAX_VANGUARDS", "3")
os.environ["AH_MAX_RANGERS"] = os.environ.get("AH_MAX_RANGERS", "2")
os.environ["AH_DEFEND"] = os.environ.get("AH_DEFEND", "1")
os.environ["AH_BEACON"] = os.environ.get("AH_BEACON", "0")  # 仿真里不玩 beacon
os.environ["AH_EVENT_STREAM"] = os.environ.get("AH_EVENT_STREAM", "event_stream.jsonl")  # 实时流

from arena_agent import (  # noqa: E402
    plan_turn_v2, AgentState, manhattan, UnitType, BeaconStatus, Direction,
)

VISION = 7          # 单位视野半径（曼哈顿）
CORE = (0, 0)
SECTOR = 32


class Event:
    def __init__(self, event_type, position, amount=None):
        self.event_type = event_type
        self.position = tuple(position)
        self.resource_amount = amount


class FakeUnit:
    def __init__(self, world, unit_type, pos, hp):
        self.id = uuid4()
        self.world = world
        self.unit_type = unit_type
        self.position = tuple(pos)
        self.cargo = 0
        self.hp = hp
        self.max_hp = hp

    # —— 决策函数会调用的方法 —— #
    def move(self, d):
        dx, dy = d.delta
        self.position = (self.position[0] + dx, self.position[1] + dy)

    def harvest(self):
        if self.cargo < 3 and self.position in self.world.resources:
            self.world.resources.discard(self.position)
            self.cargo += 1
            self.world.events.append(Event("HARVEST_SUCCEEDED", self.position, 1))

    def deposit(self):
        if self.cargo > 0:
            amt = self.cargo
            self.cargo = 0
            self.world.collected += amt
            self.world.events.append(Event("DEPOSIT_SUCCEEDED", self.position, amt))

    def heal(self):
        self.hp = self.max_hp

    def shoot(self, target):
        if hasattr(target, "hp"):
            target.hp -= 1

    def sweep(self, d):
        # 简化：清扫方向上最近的敌人
        best = None
        best_d = 99
        for e in self.world.enemies:
            dd = manhattan(self.position, e.position)
            if dd <= 1 and dd < best_d:
                best, best_d = e, dd
        if best:
            best.hp -= 1

    def pickup_beacon(self):
        pass


class FakeCore:
    def __init__(self, world):
        self.world = world
        self.position = CORE
        self.hp = 5
        self.shield = 5
        self.view = type("V", (), {"state": "NORMAL"})()

    def spawn(self, ut):
        cost = 1
        if self.world.resources_amt < cost:
            return
        self.world.resources_amt -= cost
        if ut == UnitType.WORKER:
            u = FakeUnit(self.world, UnitType.WORKER, CORE, 2)
            self.world.workers.append(u)
        elif ut == UnitType.VANGUARD:
            u = FakeUnit(self.world, UnitType.VANGUARD, CORE, 4)
            self.world.vanguards.append(u)
        else:
            u = FakeUnit(self.world, UnitType.RANGER, CORE, 2)
            self.world.rangers.append(u)
        self.world.units.append(u)
        self.world.population += 1

    def heal(self):
        self.hp = 5

    def repair_shield(self):
        self.shield = 5

    def cancel_move(self):
        self.view.state = "NORMAL"


class FakeTurn:
    def __init__(self, world, tick):
        self.world = world
        self.tick = tick
        self.core = world.core
        self.workers = world.workers
        self.vanguards = world.vanguards
        self.rangers = world.rangers
        self.units = world.units
        self.visible_enemies = world.visible_enemies
        self.resource_cells = world.visible_resources
        self.obstacle_cells = world.obstacles
        self.terrain = world.terrain
        self.events = world.events
        self.beacon = type("B", (), {"status": BeaconStatus.NONE})()
        self.resources = world.resources_amt
        self.resource_capacity = 999
        self.state = type("S", (), {"population": world.population})()

    def clear(self):
        pass

    def submit(self):
        pass


def make_resources():
    """在核心附近与远处扇区撒一些资源点。"""
    res = set()
    # 核心附近的资源带
    for x in range(-8, 9):
        for y in range(-8, 9):
            if abs(x) + abs(y) <= 6 and (x, y) != CORE:
                if (x * 7 + y * 13) % 3 == 0:
                    res.add((x, y))
    # 远处扇区（制造探索动机）
    for cx, cy in [(1, 1), (-1, 1), (1, -1), (-1, -1), (2, 0), (0, 2)]:
        bx, by = cx * SECTOR, cy * SECTOR
        for i in range(6):
            res.add((bx + i, by + (i % 2)))
    return res


class World:
    def __init__(self):
        self.resources = make_resources()
        self.resources_amt = 30
        self.collected = 0
        self.obstacles = {
            (-3, 2), (-3, 3), (-3, 4), (-2, 3), (4, -3), (4, -2), (4, -1),
        }
        self.units = []
        self.workers = []
        self.vanguards = []
        self.rangers = []
        self.population = 0
        self.core = FakeCore(self)
        # 初始 6 个工人
        for _ in range(6):
            self.spawn_at_start(UnitType.WORKER, CORE, 2)
        # 3 个敌人，放在远处，会逐步逼近并攻击最近的我方单位
        self.enemies = [
            FakeUnit(self, UnitType.WORKER, (22, 18), 6),
            FakeUnit(self, UnitType.WORKER, (-20, 15), 6),
            FakeUnit(self, UnitType.WORKER, (15, -22), 6),
        ]
        self.explored = set()
        self.events = []
        self.visible_resources = set()
        self.visible_enemies = []
        self.terrain = []

    def spawn_at_start(self, ut, pos, hp):
        u = FakeUnit(self, ut, pos, hp)
        self.units.append(u)
        if ut == UnitType.WORKER:
            self.workers.append(u)
        self.population += 1

    def compute_visibility(self):
        self.visible_resources = {
            r for r in self.resources
            if any(manhattan(r, u.position) <= VISION for u in self.units
                   if u.unit_type != UnitType.CORE)
        }
        self.visible_enemies = [
            e for e in self.enemies
            if any(manhattan(e.position, u.position) <= VISION for u in self.units
                   if u.unit_type != UnitType.CORE)
        ]
        # 记录已探索扇区（任一我方单位所在扇区）
        s = set()
        for u in self.units:
            if u.unit_type == UnitType.CORE:
                continue
            s.add((u.position[0] // SECTOR, u.position[1] // SECTOR))
        self.explored |= s
        self.terrain = [type("T", (), {"positions": [[cx * SECTOR + 16, cy * SECTOR + 16]]})()
                        for (cx, cy) in self.explored]

    def step_enemies(self):
        """敌人追最近的我方单位；近距离（<=2）攻击 -1 HP。"""
        for e in self.enemies:
            if e.hp <= 0:
                continue
            target = None
            best = 99
            for u in self.units:
                if u.unit_type == UnitType.CORE:
                    continue
                d = manhattan(e.position, u.position)
                if d < best:
                    target, best = u, d
            if target is None:
                continue
            # 远时每 tick 走 2 步，近时 1 步（避免追不上会跑的单位）
            steps = 2 if best > 3 else 1
            for _ in range(steps):
                dx = target.position[0] - e.position[0]
                dy = target.position[1] - e.position[1]
                if dx == 0 and dy == 0:
                    break
                if abs(dx) >= abs(dy) and dx != 0:
                    step = (1 if dx > 0 else -1, 0)
                else:
                    step = (0, 1 if dy > 0 else -1)
                e.position = (e.position[0] + step[0], e.position[1] + step[1])
            # 近距离攻击：最近的我方单位 -1 HP
            t2 = None
            td = 99
            for u in self.units:
                if u.unit_type == UnitType.CORE:
                    continue
                d = manhattan(e.position, u.position)
                if d <= 2 and d < td:
                    t2, td = u, d
            if t2 and t2.hp > 0:
                t2.hp -= 1


def run(n_ticks: int, live: bool = False):
    world = World()
    state = AgentState()
    # 实时模式下清空旧流文件，保证从头开始监控
    stream_path = os.path.join(HERE, "event_stream.jsonl")
    if os.path.exists(stream_path):
        try:
            os.remove(stream_path)
        except Exception:
            pass
    for t in range(1, n_ticks + 1):
        world.events = []
        world.compute_visibility()
        turn = FakeTurn(world, t)
        try:
            plan_turn_v2(turn, state)
        except Exception as exc:
            print(f"[tick {t}] 决策异常: {exc!r}")
        # 仿真侧：敌人行动（攻击/移动）
        world.step_enemies()
        # 清理死亡敌人
        world.enemies = [e for e in world.enemies if e.hp > 0]
        if live:
            time.sleep(0.12)  # 放慢节奏，便于实时观察小地图

    out = os.path.join(HERE, "event_log.json")
    meta = {
        "generated_at": time.time(),
        "note": "Arena Hero 无头仿真事件日志",
        "core_pos": list(CORE),
        "ticks": n_ticks,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(state.event_log.to_dict(meta), f, ensure_ascii=False)

    # 简单统计
    headings = {}
    for ws in state.worker_states.values():
        if ws.scout_heading is not None:
            headings[str(ws.scout_heading)] = headings.get(str(ws.scout_heading), 0) + 1
    print(f"已写出 {out}")
    print(f"总 tick={n_ticks}  采集总量={world.collected}  资源={world.resources_amt}")
    print(f"探索工人的定向航向分布(应互不重复): {headings}")
    print(f"已探索扇区数={len(world.explored)}")
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    live = "--live" in args
    args = [a for a in args if a != "--live"]
    ticks = int(args[0]) if args else 400
    run(ticks, live=live)
