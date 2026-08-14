"""正式世界部署适配器：把进化的 HeuristicStrategy 接入官方 arena-hero SDK。

用法：
    export ARENA_HERO_API_KEY=your-key
    .venv/bin/python deploy.py --genes results/evolve_final.json

--genes 指向 run_evolve.py 输出 JSON 的 "history"（取 fitness 最高者），
或直接传一个 gene JSON 文件（形如 {"name": value, ...}）。

本地试运行（不连正式世界）：
    .venv/bin/python deploy.py --local --genes results/evolve_final.json
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arena_hero import ArenaHeroClient, Direction, UnitType
from arena_hero.models import CoreView

from ahsim.observation import Observation
from ahsim.vision import visible_cells
from status import LIVE_STATUS, write_status
from strategies.heuristic import HeuristicStrategy, normalize_genes

VISION = {"WORKER": 3, "VANGUARD": 4, "RANGER": 5, "CORE": 5}
VIEW_HALF = 64          # 监控地图视窗半径（以 Core 为中心）
HISTORY_PATH = "results/agent_history.jsonl"   # 线上运行统计（供后续进化分析）
EVENTS_PATH = "results/agent_events.jsonl"     # 每 tick 一行结构化事件（供后端解析层）
KEY_EVENTS_PATH = "results/agent_key_events.log"  # 人类可读关键事件日志（采集/交付/进攻/击杀）

# 战斗 tag（用于"出发进攻"检测）
COMBAT_TAGS = {"attack", "track", "raid", "sweep", "harass", "kite"}


def _uid_short(uid, n=6):
    """UUID/对象 → 短标识（前 n 位）。"""
    if uid is None:
        return "?"
    s = str(uid)
    return s[:n]


# ----------------------------------------------------------------------
# 基因加载
# ----------------------------------------------------------------------
def load_genes(path):
    """Load a current-rules genome from an evolution artifact.

    New artifacts carry ``best_genes`` selected with the same metric that was
    used for holdout/rolling seeds.  For old artifacts we retain the history
    fallback, but normalize it through the current v0.14 bounds so legacy
    population/self-destruct genes cannot silently override policy freezes.
    """
    data = json.load(open(path))
    if isinstance(data.get("best_genes"), dict):
        genes = data["best_genes"]
        metric = data.get("best_metric", data.get("best"))
        print(f"[deploy] 使用 gen {data.get('best_gen', '?')} 的已选冠军"
              f" (metric {float(metric):.1f})" if metric is not None
              else "[deploy] 使用结果文件中的已选冠军")
    elif "history" in data:
        history = [h for h in data["history"] if isinstance(h, dict)
                   and isinstance(h.get("genes"), dict)]
        if not history:
            raise ValueError("evolution result has no usable history")
        metric_key = "holdout" if any(h.get("holdout") is not None for h in history) \
            else "best"
        best = max(history, key=lambda h: h.get(metric_key)
                   if h.get(metric_key) is not None else -float("inf"))
        genes = best["genes"]
        metric = best.get(metric_key, best.get("best"))
        print(f"[deploy] 使用 gen {best['gen']} 的基因 ({metric_key} {metric:.1f})")
    else:
        genes = data
    return normalize_genes(genes)


# ----------------------------------------------------------------------
# Observation 构建（模拟器格式 → 正式世界 state）
# ----------------------------------------------------------------------
def _ev_type(e):
    """事件类型：兼容 SDK ResolutionEvent（.event_type）与 dict（.get）。"""
    if hasattr(e, "event_type"):
        return e.event_type
    if isinstance(e, dict):
        return e.get("type", "")
    return ""


class LiveAdapter:
    """把 SDK 的 Turn 转成模拟器 Observation，并回填策略决策。"""

    # 官方事件名 → 模拟器事件名（Memory 只消费移动类事件）
    MOVE_FAILED = ("UNIT_MOVE_FAILED", "CORE_MOVE_FAILED")
    MOVE_OK = ("UNIT_MOVE_SUCCEEDED", "CORE_MOVE_SUCCEEDED")

    def __init__(self, strategy):
        self.strategy = strategy
        self._last_tick = None
        self._sent_dirs = {}     # uid -> 上一 tick 提交的 MOVE 方向
        self._core_dir = None    # Core 最近一次已知的迁移方向
        self.reconnects = 0      # 检测到的断线次数
        self.missed_ticks = 0    # 累计错过的 tick 数
        self.recent_events = deque(maxlen=150)   # 监控用：跨 tick 的事件滚动窗口
        self._last_combat_tag = {}   # uid -> 上 tick 决策 tag（出发进攻检测）
        self._known_own_ids = set()  # 上 Tick IDs；死亡目标不在当前 observation 中
        self.submit_ms = 0.0
        self.started_at = time.time()
        # 运行统计（供后续进化/策略分析；心跳时落盘 agent_history.jsonl）
        self.stats = {
            "harvests": 0,          # 成功采集次数
            "harvest_amount": 0,    # 采集资源总量
            "deposits": 0,          # 成功存放次数
            "deposited": 0,         # 存入 Core 资源总量
            "loot_captured": 0,     # 击败敌方 Core 抢资源次数
            "loot_amount": 0,
            "cores_destroyed": 0,   # 摧毁敌方 Core 次数
            "units_destroyed": 0,   # 摧毁敌方单位次数
            "heals": 0,             # 治疗成功次数
            "event_types": Counter(),  # 全部事件类型分布（学习用）
        }

    def _log_key_event(self, ev, et, my_ids, obs=None):
        """人类可读关键事件日志（采集/交付/进攻/击杀/生产/受伤）。
        旁路：异常绝不影响对局。"""
        try:
            msg = self._fmt_key_event(ev, et, my_ids, obs)
            if not msg:
                return
            ts = time.strftime("%H:%M:%S")
            line = f"[{ts} tick {ev.tick}] {msg}"
            with open(KEY_EVENTS_PATH, "a") as f:
                f.write(line + "\n")
            print(line, flush=True)
        except Exception:
            pass

    def _fmt_key_event(self, ev, et, my_ids, obs=None):
        """格式化关键事件；非关键事件返回 None。"""
        vals = ev.values or {}
        pos = f"@({ev.position[0]},{ev.position[1]})" if ev.position else ""
        who = _uid_short(ev.actor_id)
        tgt = _uid_short(ev.target_id)
        if et == "HARVEST_SUCCEEDED":
            return f"🟢 采集 +{vals.get('amount', 1)} {pos}（{who}）"
        if et == "DEPOSIT_SUCCEEDED":
            return f"📦 交付 +{vals.get('amount', 1)} → Core（{who}）"
        if et == "CORE_SPAWN_SUCCEEDED":
            return f"🏭 生产单位 {pos}（{who}）"
        if et == "SHOT_HIT":
            return f"🎯 命中 {tgt} -{vals.get('damage', 1)} {pos}（{who}）"
        if et == "UNIT_DAMAGED" and vals.get("hp") == 0:
            if ev.target_id in my_ids:
                return f"☠️ 我方单位阵亡 {tgt} {pos}"
            owner = ""
            if obs is not None:
                for e in obs.enemies or []:
                    if e["uid"] == ev.target_id:
                        owner = f"（{e.get('owner') or '?'}）"
                        break
            return f"💥 摧毁敌方单位 {tgt}{owner} {pos}"
        if et == "DESTRUCTION_PARTICIPATION":
            kind = "Core" if ev.reason_code == "CORE" else "单位"
            return f"🏆 参与摧毁敌{kind} {tgt} {pos}"
        if et == "CORE_DAMAGED":
            return f"🚨 我方 Core 受伤 -{vals.get('damage', 1)} {pos}"
        if et == "CORE_DESTROYED":
            if ev.target_id in my_ids:
                return f"☠️ 我方 Core 被摧毁 {pos}（{ev.reason_code}）"
            return f"🏆 摧毁敌 Core {tgt} {pos}"
        if et == "BEACON_PICKED_UP":
            return f"🚩 拾取信标 {pos}（{who}）"
        if et == "CORE_MOVE_STARTED":
            return f"🚚 Core 开始迁移 {pos}"
        if et == "CORE_MOVE_SUCCEEDED":
            return f"🚚 Core 迁移完成 {pos}"
        if et == "UNIT_HEALED":
            return f"💚 治疗 {who} {pos}"
        return None

    def _log_attack_launch(self, tick, decisions):
        """出发进攻日志：单位 tag 从非战斗变为战斗（attack/track/raid 等）。"""
        try:
            for uid, d in (decisions or {}).items():
                tag = d.get("tag")
                prev = self._last_combat_tag.get(uid)
                if tag in COMBAT_TAGS and prev not in COMBAT_TAGS:
                    goal = d.get("goal")
                    gpos = f"→ @({goal[0]},{goal[1]})" if goal else ""
                    ts = time.strftime("%H:%M:%S")
                    line = (f"[{ts} tick {tick}] ⚔️ 出发进攻 {gpos} "
                            f"（{_uid_short(uid)} {tag}）")
                    with open(KEY_EVENTS_PATH, "a") as f:
                        f.write(line + "\n")
                    print(line, flush=True)
                self._last_combat_tag[uid] = tag
        except Exception:
            pass

    def _tick_stats(self, ev):
        """从官方事件更新运行统计（宽松匹配事件名，不中断主流程）。"""
        st = self.stats
        et = ev.event_type
        st["event_types"][et] += 1
        amt = ev.resource_amount
        if et == "HARVEST_SUCCEEDED":
            st["harvests"] += 1
            if amt: st["harvest_amount"] += amt
        elif et == "DEPOSIT_SUCCEEDED":
            st["deposits"] += 1
            if amt: st["deposited"] += amt
        elif et == "CORE_RESOURCES_CAPTURED":
            st["loot_captured"] += 1
            if amt: st["loot_amount"] += amt
        elif "DESTROYED" in et or "KILLED" in et:
            if "CORE" in et:
                st["cores_destroyed"] += 1
            else:
                st["units_destroyed"] += 1
        elif "HEAL_SUCCEEDED" in et:
            st["heals"] += 1

    def write_event(self, tick, obs, plan, events=None):
        """每 tick 一行结构化事件日志（agent_events.jsonl）。

        含单位级决策历史（decisions: uid8 -> {tag, goal}）——便于离线
        回放分析"某单位某 tick 为什么往那走"（结合 units 位置轨迹）。
        后端解析层读这个文件做查询/聚合（/api/events、/api/stats）。
        轻量 ~500B/行，一次 append，不影响对局。
        """
        try:
            dec = self.strategy._decisions if hasattr(self.strategy, "_decisions") else {}
            row = {
                "tick": tick, "ts": round(time.time(), 1),
                "pop": obs.population,
                "res": obs.core["resources"] if obs.core else None,
                "core": [obs.core["pos"][0], obs.core["pos"][1],
                         obs.core["hp"], obs.core["shield"]] if obs.core else None,
                "units": [[str(u["uid"])[:8], u["utype"], u["pos"][0], u["pos"][1],
                           u["hp"], u["cargo"], 1 if u["carries_beacon"] else 0]
                          for u in obs.units],
                "decisions": {str(u["uid"])[:8]: {
                    "tag": (dec.get(u["uid"]) or {}).get("tag"),
                    "goal": (dec.get(u["uid"]) or {}).get("goal")}
                    for u in obs.units},
                "enemies": len(obs.enemies),
                "events": [_ev_type(e) for e in (events or [])],
                "plan": ",".join(a for a, _ in (plan or {}).get("units", {}).values())
                        if plan else "",
            }
            with open(EVENTS_PATH, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"[deploy] 事件日志写入失败（忽略）: {exc}", flush=True)

    def write_history(self, tick, obs):
        """心跳时把运行统计追加到 agent_history.jsonl（每 25 tick 一行）。

        数据供后续进化改进用：采集/经济/战斗/探索的时间序列。
        """
        try:
            st = self.stats
            row = {
                "tick": tick, "ts": round(time.time(), 1),
                "uptime": round(time.time() - self.started_at, 1),
                "pop": obs.population,
                "res": obs.core["resources"] if obs.core else None,
                "explored": len(self.strategy.mem.visited),
                "beacon": obs.beacon.get("status") if obs.beacon else None,
                "harvests": st["harvests"], "harvest_amount": st["harvest_amount"],
                "deposits": st["deposits"], "deposited": st["deposited"],
                "loot": st["loot_captured"], "loot_amount": st["loot_amount"],
                "cores_destroyed": st["cores_destroyed"],
                "units_destroyed": st["units_destroyed"],
                "heals": st["heals"],
                "reconnects": self.reconnects, "missed_ticks": self.missed_ticks,
            }
            with open(HISTORY_PATH, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"[deploy] 历史写入失败（忽略）: {exc}", flush=True)

    def detect_gap(self, turn):
        """检测 tick 不连续（断线错过）：返回错过的 tick 数；首次进入返回 0。"""
        if self._last_tick is None:
            self._last_tick = turn.tick
            return 0
        gap = turn.tick - self._last_tick - 1
        self._last_tick = turn.tick
        if gap > 0:
            self.reconnects += 1
            self.missed_ticks += gap
            # 断线期间世界推进：清临时目标（保留永久障碍/资源记忆——
            # 障碍地形永不变，断线不使它失效）
            self.strategy.reset_transient()
            self._sent_dirs.clear()   # 中间 tick 的指令结果已丢失，方向不再可信
            self._core_dir = None
            print(f"[deploy] ⚠ 检测到断线（错过 {gap} tick，累计 {self.missed_ticks}）"
                  f"，已清临时目标（保留世界记忆）", flush=True)
        return gap

    def translate_events(self, turn, obs=None):
        """turn.events 就是上一 Tick 的解析结果，直接转成模拟器格式。

        官方移动事件只带 actor_id + reason_code，不带方向，所以方向要从
        我们上一 tick 实际提交的指令里反查——否则 Memory 学不到障碍。
        obs: 可选（我方观察，用于关键事件日志的敌我判断）。
        """
        out = []
        current_ids = set()
        if obs is not None:
            for u in obs.units or []:
                current_ids.add(u["uid"])
            if obs.core:
                current_ids.add(obs.core["uid"])
        my_ids = current_ids | self._known_own_ids
        for ev in turn.events:
            et = ev.event_type
            actor = ev.actor_id
            self._tick_stats(ev)
            self._log_key_event(ev, et, my_ids, obs)
            self.recent_events.append({
                "tick": ev.tick, "type": et, "reason": ev.reason_code,
                "actor": str(ev.actor_id) if ev.actor_id is not None else None,
                "target": str(ev.target_id) if ev.target_id is not None else None,
                "target_is_own": ev.target_id in my_ids if ev.target_id else None,
                "pos": list(ev.position) if ev.position else None,
                "values": ev.values or None,
            })
            if et in self.MOVE_FAILED:
                direction = (self._core_dir if et.startswith("CORE")
                             else self._sent_dirs.get(actor))
                if direction is None:
                    continue      # 方向不明就跳过：宁可不学，也不能学错格子
                out.append({"type": "MOVE_BLOCKED", "obj_id": actor,
                            "direction": direction,
                            "reason": ev.reason_code})
            elif et in self.MOVE_OK:
                d = {"type": "MOVED", "obj_id": actor}
                if ev.position is not None:
                    d["to"] = (ev.position[0], ev.position[1])
                out.append(d)
            else:
                # 其余事件 Memory 目前不消费，原样保留（含官方字段名）
                d = {"type": et, "reason": ev.reason_code}
                if actor is not None:
                    d["actor_id"] = actor
                if ev.target_id is not None:
                    d["target_id"] = ev.target_id
                out.append(d)
        if obs is not None:
            self._known_own_ids = current_ids
        return out

    def build_observation(self, turn):
        state = turn.state
        carrier_id = turn.beacon.carrier_id
        # 自己单位
        units = []
        vis_cells = set()
        obstacles = set(turn.obstacle_cells)
        for w in list(turn.workers) + list(turn.vanguards) + list(turn.rangers):
            v = w.view
            pos = (v.position[0], v.position[1])
            units.append({
                "uid": v.id, "utype": v.unit_type.value, "pos": pos,
                "hp": v.hp, "cargo": v.cargo or 0,
                # UnitView 没有 carries_beacon 字段，由 Beacon 的 carrier_id 推出
                "carries_beacon": carrier_id is not None and v.id == carrier_id,
            })
            r = VISION[v.unit_type.value]
            # 遮挡同时纳入记忆障碍：服务端本 tick 只报告"当前可见"障碍，
            # 口袋内圈障碍被外圈挡住时不在 turn.obstacle_cells 里；若只用
            # 它做遮挡，本地视野会"穿透"外圈看到内圈格 → 反证逻辑把刚记住
            # 的内圈障碍误删（webui 表现：离开视野就消失）。记忆障碍当遮挡
            # 后，内圈格不再被算作可见 → 不会被反证删除。
            vis_cells.update(visible_cells(pos[0], pos[1], r,
                                           lambda x, y: (x, y) in obstacles
                                           or (x, y) in self.strategy.mem.obstacles))
        # 自己 Core
        core_obs = None
        if turn.core is not None:
            c = turn.core.view
            pos = (c.position[0], c.position[1])
            core_obs = {"uid": c.id, "pos": pos, "hp": c.hp, "shield": c.shield,
                        "resources": turn.resources,
                        "migration": (c.move_direction.value, c.move_progress)
                        if c.move_direction is not None else None,
                        "capacity": turn.resource_capacity}
            vis_cells.update(visible_cells(pos[0], pos[1], VISION["CORE"],
                                           lambda x, y: (x, y) in obstacles
                                           or (x, y) in self.strategy.mem.obstacles))
        # 敌人（可见对象）
        enemies = []
        enemy_cores = []
        for obj in turn.visible_enemies:
            if isinstance(obj, CoreView):
                enemy_cores.append({"uid": obj.id, "pos": (obj.position[0], obj.position[1]),
                                    "hp": obj.hp, "shield": obj.shield,
                                    "owner": obj.owner_username})
            else:
                enemies.append({"uid": obj.id, "utype": obj.unit_type.value,
                                "pos": (obj.position[0], obj.position[1]), "hp": obj.hp,
                                "owner": getattr(obj, "owner_username", None)})
        # Beacon
        b = turn.beacon
        beacon = {"position": [b.position[0], b.position[1]]}
        if b.status is not None:
            beacon["status"] = b.status  # "GROUND" / "CARRIED"
            if b.carrier_id is not None:
                beacon["carrier_id"] = b.carrier_id
        else:
            beacon["status"] = "UNKNOWN"
        # 上一 tick 事件（先翻译，再刷新方向记录，保证反查用的是当时提交的方向）
        obs = Observation(
            player_id=0, tick=turn.tick,
            core=core_obs, units=units,
            enemies=enemies, enemy_cores=enemy_cores,
            resources=set((p[0], p[1]) for p in turn.resource_cells),
            obstacles=obstacles, beacon=beacon,
            population=state.population,
            visible_cells=vis_cells, prev_events=[],
            status=state.status.value if hasattr(state, "status")
            else ("RESPAWNING" if core_obs is None else "ACTIVE"),
            respawn_at_tick=getattr(state, "respawn_at_tick", None),
        )
        prev = self.translate_events(turn, obs=obs)
        obs.prev_events = prev
        if turn.core is not None and turn.core.view.move_direction is not None:
            self._core_dir = turn.core.view.move_direction.value
        return obs

    def apply_plan(self, turn, plan):
        # 记录本 tick 提交的移动方向：官方移动失败事件不带方向，下一 tick
        # 要靠这份记录才能推出被挡的格子。
        self._sent_dirs = {}
        for uid, (atype, args) in (plan.get("units") or {}).items():
            u = turn.unit(uid)
            if atype == "MOVE":
                self._sent_dirs[uid] = args["direction"]
                u.move(Direction(args["direction"]))
            elif atype == "HARVEST":
                u.harvest()
            elif atype == "DEPOSIT":
                u.deposit()
            elif atype == "SWEEP":
                u.sweep(Direction(args["direction"]))
            elif atype == "SHOOT":
                u.shoot_cell((args["expected_cell"][0], args["expected_cell"][1]))
            elif atype == "HEAL":
                u.heal()
            elif atype == "SELF_DESTRUCT":
                u.self_destruct()
            elif atype == "PICKUP_BEACON":
                u.pickup_beacon()
            elif atype == "DROP_BEACON":
                u.drop_beacon()
        ca = plan.get("core")
        if ca and turn.core is not None:
            atype, args = ca
            if atype == "SPAWN":
                turn.core.spawn(UnitType(args["unit_type"]))
            elif atype == "HEAL":
                turn.core.heal()
            elif atype == "REPAIR_SHIELD":
                turn.core.repair_shield()
            elif atype == "START_MOVE":
                self._core_dir = args["direction"]
                turn.core.start_move(Direction(args["direction"]))
            elif atype == "CANCEL_MOVE":
                turn.core.cancel_move()
            elif atype == "PICKUP_BEACON":
                turn.core.pickup_beacon()
            elif atype == "DROP_BEACON":
                turn.core.drop_beacon()
            elif atype == "SELF_DESTRUCT":
                turn.core.self_destruct()

    # ------------------------------------------------------------------
    # 监控快照
    # ------------------------------------------------------------------
    def build_status(self, tick, obs, plan, player_status="ACTIVE",
                     carrier_known=False):
        """给 monitor.py 用的运行快照。只读内存，不做任何阻塞操作。

        故意不接收 SDK 的 Turn：这样本地模拟试运行也能产出同样格式的快照，
        面板不用等上线就能验证。
        """
        mem = self.strategy.mem
        core = obs.core
        cx, cy = core["pos"] if core else (obs.units[0]["pos"] if obs.units else (0, 0))
        x0, y0 = cx - VIEW_HALF, cy - VIEW_HALF
        span = VIEW_HALF * 2 + 1

        def inview(pos):
            return x0 <= pos[0] < x0 + span and y0 <= pos[1] < y0 + span

        # 地图用 Agent 自己的记忆（线上没有上帝视角，这就是它真实"知道"的世界）
        view = {
            "origin": [x0, y0], "size": span,
            "obstacles": [[c[0], c[1]] for c in mem.obstacles if inview(c)],
            "resources": [[c[0], c[1]] for c in mem.resources if inview(c)],
        }
        acts = Counter(a for a, _ in plan.get("units", {}).values()) if plan else Counter()
        p_units = (plan or {}).get("units") or {}

        def _unit_dir(u):
            """本 tick 提交的方向：从策略 plan 里取（MOVE 才有）。"""
            act = p_units.get(u["uid"])
            if act and act[0] == "MOVE":
                return str(act[1].get("direction", ""))
            return ""

        beacon = obs.beacon
        return {
            "kind": "live",
            "tick": tick,
            "uptime": time.time() - self.started_at,
            "player_status": str(player_status),
            "reconnects": self.reconnects, "missed_ticks": self.missed_ticks,
            "submit_ms": round(self.submit_ms, 1),
            "core": None if not core else {
                "pos": list(core["pos"]), "hp": core["hp"], "shield": core["shield"],
                "resources": core["resources"], "capacity": core["capacity"],
                "migration": core["migration"],
            },
            "population": obs.population,
            "composition": dict(Counter(u["utype"] for u in obs.units)),
            "beacon": {
                "position": beacon["position"], "status": beacon.get("status"),
                "mine": any(u["carries_beacon"] for u in obs.units),
                "carrier_known": bool(carrier_known),
            },
            "units": [{"uid": str(u["uid"])[:8], "type": u["utype"],
                       "pos": list(u["pos"]), "hp": u["hp"], "cargo": u["cargo"],
                       "beacon": u["carries_beacon"],
                       "dir": _unit_dir(u),
                       "tag": (self.strategy._decisions.get(u["uid"]) or {}).get("tag"),
                       "goal": (self.strategy._decisions.get(u["uid"]) or {}).get("goal")}
                      for u in obs.units],
            "enemies": [{"uid": str(e["uid"])[:8], "type": e["utype"],
                         "pos": list(e["pos"]), "hp": e["hp"]}
                        for e in obs.enemies],
            "enemy_cores": [{"pos": list(c["pos"]), "hp": c["hp"],
                             "shield": c["shield"], "owner": str(c["owner"])}
                            for c in obs.enemy_cores],
            "view": view,
            "plan": {"core": plan.get("core")[0] if plan and plan.get("core") else None,
                     # 单位级指令：uid8 -> {act, dir}（前端按单位列出已下发指令）
                     "units": {str(uid)[:8]: {"act": act,
                                              "dir": args.get("direction", "")
                                              if isinstance(args, dict) else "",
                                              "tag": (self.strategy._decisions.get(uid) or {}).get("tag"),
                                              "goal": (self.strategy._decisions.get(uid) or {}).get("goal")}
                               for uid, (act, args) in
                               (plan or {}).get("units", {}).items()},
                     "summary": dict(acts)},
            "memory": {"obstacles": len(mem.obstacles), "resources": len(mem.resources),
                       "area": len(mem.area_seen), "unreachable": len(mem.unreachable),
                       "enemies": len(mem.last_enemy_pos)},
            # 地图分层：已探索区域 + 记忆地形（战争迷雾用）
            "explored": sorted(map(list, mem.visited)),
            "known_obstacles": sorted(map(list, mem.obstacles)),
            "known_resources": sorted(map(list, mem.resources)),
            "events": list(self.recent_events)[-80:],
        }


# ----------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------
def run_online(api_key, genes, tick_limit=None, resume=None, save_memory=None,
               verbose=False, status_path=LIVE_STATUS, db_path=None):
    strat = HeuristicStrategy(genes=genes, bounds=None)
    if resume:
        try:
            import json as _json
            strat.load_memory(_json.load(open(resume)))
            print(f"[deploy] 已从 {resume} 恢复记忆 "
                  f"(障碍 {len(strat.mem.obstacles)} 格, 资源 {len(strat.mem.resources)} 点, "
                  f"区域 {len(strat.mem.area_seen)} 格)", flush=True)
        except Exception as exc:
            print(f"[deploy] 记忆恢复失败（忽略，从零开始）: {exc}", flush=True)

    def _save_memory(reason=""):
        if not save_memory:
            return
        try:
            import json as _json
            with open(save_memory, "w") as f:
                _json.dump(strat.mem.to_dict(), f)
            print(f"[deploy] 记忆已保存 {save_memory} {reason}", flush=True)
        except Exception as exc:
            print(f"[deploy] 记忆保存失败: {exc}", flush=True)

    # SIGTERM/SIGINT 时保存记忆再退出（优雅停机）
    def _on_exit(signum, _frame):
        _save_memory(f"(signal {signum})")
        raise SystemExit(0)
    try:
        import signal as _sig
        _sig.signal(_sig.SIGTERM, _on_exit)
        _sig.signal(_sig.SIGINT, _on_exit)
    except (ValueError, OSError):
        pass  # 非主线程等无法注册信号

    adapter = LiveAdapter(strat)
    last_save_tick = 0
    # 人工指令通道：commands.json（前端 monitor API 写入，agent 轮询应用）
    commands_path = Path(save_memory).with_name("commands.json") if save_memory else None
    applied_cmds = set()      # 已应用的指令 id（不重复应用）
    _cmd_mtime = 0

    def _apply_commands(force=False, tick=999999999):
        """读取并应用未执行的指令。返回新应用数。

        tick: 当前对局 tick（写资源记忆用；启动时 force 无 turn，用大值
        表示"新鲜"，之后由视野/TTL 自然管理）。
        """
        nonlocal _cmd_mtime
        if commands_path is None or not commands_path.exists():
            return 0
        try:
            mt = commands_path.stat().st_mtime
            if not force and mt == _cmd_mtime:
                return 0
            data = json.loads(commands_path.read_text(encoding="utf-8") or "{}")
            cmds = data.get("commands", [])
            n = 0
            for c in cmds:
                cid = c.get("id")
                if not cid or cid in applied_cmds:
                    continue
                ctype = c.get("type")
                pos = tuple(c.get("pos") or ())
                try:
                    if ctype == "mark_resource" and len(pos) == 2:
                        # 直接写入资源记忆：走正常 TTL/采集/视野反证流程
                        strat.mem.resources[pos] = tick
                        print(f"[deploy] 指令: 标记资源 {pos}", flush=True)
                    elif ctype == "mark_obstacle" and len(pos) == 2:
                        strat.mem.obstacles.add(pos)
                        strat.mem.new_obstacles = True
                        print(f"[deploy] 指令: 标记障碍 {pos}", flush=True)
                    elif ctype == "goto" and len(pos) == 2 and c.get("uid"):
                        strat._manual_goto[c["uid"]] = pos
                        print(f"[deploy] 指令: 单位 {c['uid'][:8]} → {pos}", flush=True)
                    elif ctype == "cancel_goto":
                        strat._manual_goto.pop(c.get("uid"), None)
                        print(f"[deploy] 指令: 取消单位 {str(c.get('uid'))[:8]} 目标", flush=True)
                    elif ctype == "clear_marks":
                        # 清空全部人工标记（等价于移除标记过的格）
                        strat.mem.resources.clear()
                        strat.mem.obstacles.clear()
                        strat.mem.unreachable.clear()
                        strat.mem.isolated.clear()
                        strat.mem.new_obstacles = True
                        print(f"[deploy] 指令: 清空标记", flush=True)
                    elif ctype == "remove_mark" and len(pos) == 2:
                        # 取消单个标记：从资源/障碍记忆移除该格（视野重新看到会
                        # 自然重记——那是事实，不压制）
                        strat.mem.resources.pop(pos, None)
                        if pos in strat.mem.obstacles:
                            strat.mem.obstacles.discard(pos)
                            strat.mem.new_obstacles = True
                        print(f"[deploy] 指令: 取消标记 {pos}", flush=True)
                    else:
                        print(f"[deploy] 指令 {cid} 类型无效: {ctype}", flush=True)
                except Exception as exc:
                    print(f"[deploy] 指令 {cid} 应用失败: {exc}", flush=True)
                # 无论成功失败都标记已处理：失败不重试（用户可重发），
                # 避免重启后 force 重放旧指令导致标记"复活"
                applied_cmds.add(cid)
                n += 1
            _cmd_mtime = mt
            # 已处理的指令从文件移除（goto 未完成保留，重启后可重新下发）
            done = getattr(strat, "_manual_done", None)
            if done:
                strat._manual_done = []
            todo = [c for c in cmds
                    if c.get("id") not in applied_cmds
                    or (c.get("type") == "goto"
                        and c.get("uid") not in (done or []))]
            if len(todo) != len(cmds):
                commands_path.write_text(
                    json.dumps({"commands": todo}), encoding="utf-8")
            return n
        except Exception as exc:
            print(f"[deploy] 指令应用失败: {exc}", flush=True)
            return 0

    _apply_commands(force=True)
    # 线上数据存储（SQLite 全量落盘；失败不干扰对局）
    db = None
    if db_path:
        try:
            from dbstore import DBStore
            db = DBStore(db_path)
        except Exception as exc:
            print(f"[deploy] DB 初始化失败（忽略，仅 JSONL）: {exc}", flush=True)
    # 外层保护：SDK 的 turns() 通常无限重连；若异常退出则重建 client 重试
    while True:
        try:
            with ArenaHeroClient(api_key=api_key) as game:
                for turn in game.turns():
                    if tick_limit and turn.tick > tick_limit:
                        print(f"[deploy] 达到 tick 上限 {tick_limit}，退出", flush=True)
                        _save_memory()
                        return
                    adapter.detect_gap(turn)
                    obs = adapter.build_observation(turn)
                    # Core 重生：清临时目标（保留世界记忆——障碍永久有效）
                    if obs.core is not None and turn.state.events:
                        if any(getattr(ev, "event_type", "") == "CORE_DESTROYED"
                               for ev in turn.state.events):
                            strat.reset_transient()
                            print(f"[deploy] tick {turn.tick}: Core 重生，"
                                  f"已清临时目标（保留障碍/资源记忆 "
                                  f"{len(strat.mem.obstacles)} 格）", flush=True)
                    if verbose:
                        # 诊断：每 tick 打印核心状态
                        units = [(u["uid"], u["utype"], u["pos"], u["cargo"])
                                 for u in obs.units]
                        print(f"[dbg] tick {turn.tick}: core={obs.core['pos'] if obs.core else 'DEAD'}"
                              f" res={obs.core['resources'] if obs.core else 0}"
                              f" vis_res={len(obs.resources)} units={units}",
                              flush=True)
                        # 资源诊断：每 25 tick 输出视野内资源分布（含距 Core 距离）
                        if turn.tick % 25 == 0 and obs.core:
                            cp = obs.core["pos"]
                            rl = sorted(
                                (abs(r[0] - cp[0]) + abs(r[1] - cp[1]), r)
                                for r in obs.resources)
                            near = sum(1 for d, _ in rl if d <= 22)
                            memn = len(strat.mem.resources)
                            print(f"[resdbg] tick {turn.tick}: vis={len(rl)} near22={near}"
                                  f" mem={memn}"
                                  f" res={[(d, list(r)) for d, r in rl[:10]]}",
                                  flush=True)
                    # 人工指令轮询：每 tick 检查（线上 15s/tick，25 tick ≈ 6 分钟
                    # 感知延迟太长；每 tick = 最多 15 秒生效）
                    _apply_commands(tick=turn.tick)
                    plan = None
                    try:
                        plan = strat.decide(obs)
                        adapter.apply_plan(turn, plan)
                        # 出发进攻日志（tag 非战斗→战斗）
                        adapter._log_attack_launch(
                            turn.tick,
                            getattr(strat, "_decisions", {}) or {})
                        t_sub = time.time()
                        turn.submit()
                        dt_sub = time.time() - t_sub
                        adapter.submit_ms = dt_sub * 1000
                        if dt_sub > 2.0:
                            print(f"[deploy] tick {turn.tick} 提交慢: {dt_sub:.1f}s",
                                  flush=True)
                    except Exception as exc:
                        # 单个 tick 失败不退出：记录后继续（SDK 对提交有重试，
                        # 这里只兜底策略/提交的意外异常）
                        # 本 tick 没提交成功 → 方向记录作废，避免下一 tick 误判障碍
                        adapter._sent_dirs.clear()
                        print(f"[deploy] tick {turn.tick} 处理失败: {exc}", flush=True)
                    if status_path:
                        try:
                            write_status(status_path, adapter.build_status(
                                turn.tick, obs, plan,
                                player_status=turn.state.status,
                                carrier_known=turn.beacon.carrier_id is not None))
                        except Exception:
                            pass   # 监控是旁路，绝不影响对局
                        adapter.write_event(turn.tick, obs, plan,
                                            turn.events)
                    if db is not None:
                        # SQLite 全量落盘：ticks + units + events（单事务）
                        # 注意：官方 turn.events 是上一 tick 的解析结果，ev.tick
                        # 与 turn.tick 存在偏移 → 过滤条件需宽容（PK 幂等去重）
                        try:
                            evs = [
                                (e["type"], e.get("reason"), e.get("actor"),
                                 tuple(e["pos"]) if e.get("pos") else None,
                                 (e.get("values") or {}).get("amount"))
                                for e in adapter.recent_events
                                if turn.tick - (e.get("tick") or 0) <= 1]
                            db.write_tick_full(
                                turn.tick, time.time(), obs, plan,
                                adapter.submit_ms, evs,
                                strat.mem,
                                getattr(strat, "_decisions", {}) or {})
                            if turn.tick % 25 == 0:
                                db.write_mem_snap(turn.tick, strat.mem)
                        except Exception as exc:
                            print(f"[deploy] DB 写入失败（忽略）: {exc}",
                                  flush=True)
                    if turn.tick - last_save_tick >= 500:
                        last_save_tick = turn.tick
                        _save_memory()
                    if turn.tick % 25 == 0:
                        print(f"[deploy] tick {turn.tick}: pop={obs.population} "
                              f"res={obs.core['resources'] if obs.core else 0} "
                              f"reconnects={adapter.reconnects}", flush=True)
                        adapter.write_history(turn.tick, obs)
        except KeyboardInterrupt:
            _save_memory()
            raise
        except Exception as exc:
            # 打印底层原因（如 SDK 校验失败的具体字段），便于诊断连接层问题
            cause = getattr(exc, "__cause__", None)
            if cause is not None:
                print(f"[deploy] 连接层异常: {exc} | 底层: {cause}",
                      flush=True)
            else:
                print(f"[deploy] 连接层异常: {exc}，10s 后重试（策略记忆保留）",
                      flush=True)
            time.sleep(10)
            continue
        return  # turns() 正常退出（仅当 client 关闭）


def run_local(genes, ticks=800, seed=42, players=8, status_path=None,
              tick_delay=0.0):
    """本地模拟器试运行同一基因，验证部署前行为。

    带 --status 时按正式世界同样的格式逐 tick 写快照，于是不用等上线，
    monitor.py 的「线上 Agent」页签就能先验证一遍。
    """
    from ahsim.game import Game
    from strategies.randombot import RandomBot
    from strategies.heuristic import HeuristicStrategy as SimStrategy

    class _Recording:
        """包一层记录最近一次决策，供状态快照的「本 tick 决策」面板展示。"""

        def __init__(self, inner):
            self.inner = inner
            self.last_plan = None

        def decide(self, obs):
            self.last_plan = self.inner.decide(obs)
            return self.last_plan

        def __getattr__(self, k):
            return getattr(self.inner, k)

    bounds = (-128, 127, -128, 127)
    me = _Recording(SimStrategy(genes=genes, bounds=bounds))
    strategies = {0: me}
    for i in range(1, players):
        strategies[i] = (RandomBot(seed=i * 7 + 1) if i % 3 == 0
                         else SimStrategy(bounds=bounds))
    g = Game(strategies=strategies, size=256, seed=seed, max_ticks=ticks)

    if not status_path:
        g.run()
    else:
        adapter = LiveAdapter(me.inner)
        for _ in range(ticks):
            g.tick += 1
            g.step()
            p = g.players[0]
            if p.core is None and not p.units:
                continue
            obs = g.build_observation(p)
            adapter.recent_events.extend(
                {"tick": g.tick, "type": e["type"], "reason": e.get("reason"),
                 "pos": list(e["at"]) if isinstance(e.get("at"), tuple) else None,
                 "values": None}
                for e in g.last_events if e.get("player", 0) == 0)
            write_status(status_path, adapter.build_status(
                g.tick, obs, me.last_plan))
            if tick_delay:
                time.sleep(tick_delay)

    st = g.results()[0]
    print(f"[local] {ticks} ticks: pop={st['final_population']} "
          f"harvest={st['harvested']} dmg={st['damage_dealt']} "
          f"alive={st['alive']}")


def main():
    ap = argparse.ArgumentParser(description="Arena Hero 正式世界部署")
    ap.add_argument("--genes", required=True, help="基因 JSON 路径")
    ap.add_argument("--local", action="store_true", help="本地模拟试运行")
    ap.add_argument("--ticks", type=int, default=800, help="本地运行的 tick 数")
    ap.add_argument("--players", type=int, default=8, help="本地试运行的玩家数")
    ap.add_argument("--tick-delay", type=float, default=0.0,
                    help="本地试运行每 tick 停顿秒数（配合监控面板慢放观察）")
    ap.add_argument("--resume", default=None, help="恢复记忆 JSON（重启后不迷路）")
    ap.add_argument("--save-memory", default=None,
                    help="定期把记忆写入此路径（配合 --resume 崩溃恢复）")
    ap.add_argument("--tick-limit", type=int, default=None,
                    help="运行到该 tick 后退出（观察期用）")
    ap.add_argument("--api-key", default=os.environ.get("ARENA_HERO_API_KEY", ""))
    ap.add_argument("--verbose", action="store_true", help="每 tick 打印诊断日志")
    ap.add_argument("--status", default=LIVE_STATUS,
                    help="每 tick 写运行状态快照供 monitor.py 读取；传空串关闭")
    ap.add_argument("--db", default=None,
                    help="SQLite 全量数据落盘路径（如 results/agent.db）；默认随 --status 目录")
    args = ap.parse_args()

    db_path = args.db
    if db_path is None and args.status:
        db_path = os.path.join(os.path.dirname(args.status) or ".", "agent.db")

    genes = load_genes(args.genes)
    if args.local:
        run_local(genes, ticks=args.ticks, players=args.players,
                  status_path=args.status or None, tick_delay=args.tick_delay)
        return
    if not args.api_key:
        print("缺少 API key：设置 ARENA_HERO_API_KEY 或 --api-key")
        sys.exit(1)
    run_online(args.api_key, genes, tick_limit=args.tick_limit,
               resume=args.resume, save_memory=args.save_memory,
               verbose=args.verbose, status_path=args.status or None,
               db_path=db_path)


if __name__ == "__main__":
    main()
