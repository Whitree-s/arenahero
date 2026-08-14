"""桥接层：把 arena-evolve 的决策产出（Observation + plan）转成 monitor.html 兼容的 stream 帧。

monitor.html 直接消费 stream/ 下的：
  - shard_*.jsonl  每 tick 一帧（动态态势）
  - map.json       地图记忆快照（障碍/已探索，永不淘汰）

本模块是「决策算法无关」的：live（官方 SDK Turn→Observation）和 local（ahsim
Game→Observation）两种路径产出的 Observation 结构一致，故桥接代码只需写一份。
保留：死亡 ❌ 标记、禁止/危险区圆圈、单位状态面板（W/V/R 计数 + 下一步生产）。
"""

import json
import os
from collections import Counter
import uuid


class _SafeEncoder(json.JSONEncoder):
    """JSON 编码器：自动处理 UUID 等非 JSON 原生类型，防止 json.dump 报错。"""
    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return str(obj)
        # 其他不可序列化类型：尝试转字符串
        try:
            return str(obj)
        except Exception:
            return repr(obj)

SHARD_SIZE = 200          # 每个分片文件的帧数
MAX_SHARDS = 12           # 环形缓冲：仅保留最近 N 个分片
MAP_SNAPSHOT_EVERY = 25   # 每 N tick 写一次 map.json（障碍/已探索快照）

UT_CN = {"VANGUARD": "先锋", "RANGER": "游侠", "WORKER": "工人", "CORE": "核心"}

# arena-evolve Direction 枚举转屏幕方向向量 [dx, dy]
# SDK 屏幕坐标：X 向右+，Y 向下+；UP=-Y, DOWN=+Y
_DIR_VEC = {
    "UP": [0, -1], "DOWN": [0, 1], "LEFT": [-1, 0], "RIGHT": [1, 0],
    # 整数形式（部分旧 SDK 版本）
    0: [0, -1], 1: [0, 1], 2: [-1, 0], 3: [1, 0],
}


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class StreamWriter:
    def __init__(self, stream_dir="stream"):
        self.dir = stream_dir
        os.makedirs(self.dir, exist_ok=True)
        self.shard_seq = 0
        self.cur = []
        self.obs_hash = None
        self.map_since = MAP_SNAPSHOT_EVERY
        # 跨 tick 状态
        self.prev_own = {}        # uid -> (x, y, utype) 我方战斗单位（V/R）已知位置
        self.danger_zones = []     # 禁止/危险区列表（持久）
        # 事件流状态跟踪（用于 HP/cargo 变化检测）
        self.prev_cargo = {}       # uid -> last cargo（检测采集/交付）
        self.prev_hp = {}          # uid -> last hp（检测被攻击）
        self.prev_enemy_hp = {}    # eid -> last enemy hp（检测攻击）
        # 启动时加载已有地图快照（共享数据：旧决策写的 map.json 含障碍/已探索/资源记忆）。
        # 这样切到新决策时不会丢失旧决策积累的地图，且新决策写回的 map.json 旧决策也能读回。
        self._shared = self._load_shared_map()
        # 写初始 index.json（monitor poll() 首先依赖此文件发现 shard 范围）
        self._write_index()

    # ------------------------------------------------------------------
    def _write_index(self):
        """原子写 stream/index.json：告诉 monitor 当前 shard 序列范围。"""
        path = os.path.join(self.dir, "index.json")
        seq = max(0, self.shard_seq - 1)  # 已落盘的最大序号
        self._atomic_write(path, {"kept_min": 0, "kept_max": seq})

    # ------------------------------------------------------------------
    def _load_shared_map(self):
        """启动时读取已有 stream/map.json（旧决策或上一局写的快照），
        返回 {obstacles, visited, resources} 供 seed() 注入 Memory。
        文件不存在/损坏则返回 None（全新开局，不注入）。"""
        path = os.path.join(self.dir, "map.json")
        if not os.path.exists(path):
            return None
        try:
            m = json.load(open(path, encoding="utf-8"))
            return {
                "obstacles": [tuple(p) for p in (m.get("obstacles") or [])],
                "visited":   [tuple(p) for p in (m.get("explored_cells") or [])],
                "resources": {tuple(p[:2]): p[2] for p in (m.get("resource_memory") or [])},
            }
        except Exception:
            return None

    def seed(self, strat):
        """把启动前加载的共享地图注入 arena-evolve 的 Memory（障碍/已探索/资源记忆），
        实现新旧决策地图数据共享。须在 strat 创建后、主循环前调用一次。

        - 障碍/已探索：直接并入（arena-evolve 只在"亲眼见非障碍"时才反证删除，
          旧决策的永久障碍地形永不变，安全）。
        - 资源记忆：注入一个很远未来的 tick（10**12），使其不被 RESOURCE_MEMORY_TTL
          立即过期；被重新看到时刷新为真实 tick，被证实无资源时移除。
        """
        if not self._shared or strat is None:
            return
        mem = getattr(strat, "mem", None)
        if mem is None:
            return
        mem.obstacles.update(self._shared["obstacles"])
        mem.visited.update(self._shared["visited"])
        mem.resources.update({(x, y): 10**12 for (x, y) in self._shared["resources"]})

    # ------------------------------------------------------------------
    def write(self, obs, plan, strat, tick=None):
        """obs: arena-evolve Observation；plan: strat.decide(obs) 的返回；
        strat: HeuristicStrategy（用于读 _decisions 目标 + mem 地图记忆）。"""
        tick = tick if tick is not None else getattr(obs, "tick", 0)
        dec = getattr(strat, "_decisions", {}) or {}
        plan_units = (plan or {}).get("units") or {}
        plan_core = (plan or {}).get("core")

        # ---- core ----
        core = None
        if getattr(obs, "core", None):
            c = obs.core
            core = {
                "pos": list(c["pos"]), "hp": c["hp"], "shield": c["shield"],
                "resources": c.get("resources", 0), "capacity": c.get("capacity", 0),
                "migration": list(c["migration"]) if c.get("migration") else None,
            }

        # ---- units ----
        units = []
        own_combat_now = {}
        for u in getattr(obs, "units", []):
            raw_uid = u["uid"]                # 原始 UID（可能是 UUID 对象或字符串）
            uid = str(raw_uid)                 # 统一字符串版，用于输出
            ut = u["utype"]
            d = None
            # plan 键可能是原始 UID 对象（arena-evolve）或字符串（旧决策），两种都试
            pa = plan_units.get(raw_uid) or plan_units.get(uid)
            if pa and isinstance(pa, tuple) and len(pa) == 2 and isinstance(pa[1], dict):
                atype, args = pa
                # arena-evolve Direction 枚举的 value 为字符串 "UP"/"DOWN"/"LEFT"/"RIGHT"；
                # 旧 decision SDK 阶段也可能是整数 0/1/2/3。统一转成 [dx, dy]。
                dir_str = args.get("direction")
                d = _DIR_VEC.get(dir_str) or _DIR_VEC.get(str(dir_str).upper()) if dir_str else None
                # SWEEP 同上
                if d is None and atype == "SWEEP" and dir_str is not None:
                    d = _DIR_VEC.get(dir_str) or _DIR_VEC.get(str(dir_str).upper())
                # SHOOT：从单位位置指向 expected_cell
                if d is None and atype == "SHOOT" and args.get("expected_cell"):
                    ec = args["expected_cell"]
                    dx = ec[0] - u["pos"][0]; dy = ec[1] - u["pos"][1]
                    if dx != 0 or dy != 0:
                        # 归一化为 4 方向
                        d = [int(dx > 0) - int(dx < 0), int(dy > 0) - int(dy < 0)]
                # HARVEST/DEPOSIT/ATTACK_CORE：未移动但有方向意图，从 pos→goal/target 推算
                if d is None:
                    tgt = args.get("cell") or args.get("target") or args.get("expected_cell")
                    if not tgt:
                        # 用决策 goal（键可能是 UUID 对象或字符串）
                        dd_entry = dec.get(raw_uid) or dec.get(uid)
                        goal = (dd_entry or {}).get("goal") if dd_entry else None
                        if goal: tgt = goal
                    if tgt:
                        dx = tgt[0] - u["pos"][0]; dy = tgt[1] - u["pos"][1]
                        if dx != 0 or dy != 0:
                            d = [int(dx > 0) - int(dx < 0), int(dy > 0) - int(dy < 0)]
            dd = dec.get(raw_uid) or dec.get(uid) or {}
            goal = dd.get("goal")
            # dir 兜底：实在没动作意图，用 pos→goal 给一个朝向
            if d is None and goal:
                dx = goal[0] - u["pos"][0]; dy = goal[1] - u["pos"][1]
                if dx != 0 or dy != 0:
                    d = [int(dx > 0) - int(dx < 0), int(dy > 0) - int(dy < 0)]
            units.append({
                "id": uid, "type": ut, "pos": list(u["pos"]),
                "hp": u["hp"], "cargo": u["cargo"],
                "dir": (list(d) if isinstance(d, (list, tuple)) else d),
                "target": list(goal) if goal else None,
                "tag": dd.get("tag"),
            })
            if ut in ("VANGUARD", "RANGER"):
                own_combat_now[uid] = (u["pos"][0], u["pos"][1], ut)

        # ---- enemies（含敌方核心）----
        enemies = []
        for e in getattr(obs, "enemies", []):
            enemies.append({"id": str(e["uid"]), "type": e["utype"],
                            "pos": list(e["pos"]), "hp": e["hp"]})
        for ec in getattr(obs, "enemy_cores", []) or []:
            enemies.append({"id": str(ec["uid"]), "type": "ENEMY_CORE",
                            "pos": list(ec["pos"]), "hp": ec["hp"]})

        # ---- obstacles（变化检测，减小体积）----
        obs_set = set(map(tuple, getattr(obs, "obstacles", set())))
        oh = hash(frozenset(obs_set)) if obs_set else 0
        obs_changed = (oh != self.obs_hash)
        obstacles_out = [list(p) for p in obs_set] if (obs_changed or not self.cur) else None
        self.obs_hash = oh

        # ---- resources_cells ----
        resources_cells = [list(p) for p in getattr(obs, "resources", set())]

        # ---- 死亡检测（我方战斗单位 id 消失即阵亡）----
        died = [uid for uid in self.prev_own if uid not in own_combat_now]
        death_events = []
        for uid in died:
            x, y, ut = self.prev_own[uid]
            death_events.append({"pos": [x, y], "unit_type": ut, "tick": tick})
        self.prev_own = own_combat_now

        # ---- 禁止/危险区状态机（简化自旧逻辑，用 obs 直接判断）----
        self._update_danger_zones(core, enemies, death_events, own_combat_now)

        # ---- 事件流（阵亡 + SDK 事件 + 决策语义 + 状态变化）----
        evs = []

        # 1) 阵亡事件
        for dv in death_events:
            cn = UT_CN.get(dv["unit_type"], dv["unit_type"])
            evs.append({"type": "death", "unit": None, "pos": dv["pos"],
                        "detail": f"{cn}阵亡"})

        # 2) SDK 上一 tick 事件（obs.prev_events = turn.events 翻译结果）
        #    HARVEST_SUCCEEDED → harvest, DEPOSIT_SUCCEEDED → deposit_move/deposit,
        #    CORE_SPAWN_SUCCEEDED → spawn
        prev_events = getattr(obs, "prev_events", None) or []
        for pe in prev_events:
            et = pe.get("type", "")
            pos = pe.get("pos") or [0, 0]
            actor = pe.get("actor")
            detail = ""
            if et == "HARVEST_SUCCEEDED":
                ev_type = "harvest"
                amt = (pe.get("values") or [None])[0] if pe.get("values") else None
                detail = f"采集+{amt}" if amt else "采集成功"
            elif et == "DEPOSIT_SUCCEEDED":
                ev_type = "deposit_move"
                detail = "存放资源"
            elif et == "CORE_SPAWN_SUCCEEDED":
                ev_type = "deposit_succeeded"
                ut = (pe.get("values") or [None])[0] if pe.get("values") else None
                detail = f"生产{ut}" if ut else "生产单位"
            elif et.startswith("MOVE_FAILED"):
                continue  # 移动失败不显示
            elif et == "ATTACK":
                ev_type = "attack"
                target = pe.get("target_is_own")
                detail = "攻击敌人" if not target else "被攻击"
            else:
                continue  # 忽略其他 SDK 事件
            evs.append({"type": ev_type, "unit": actor, "pos": list(pos),
                        "detail": detail})

        # 3) 决策标签语义事件（从 _decisions 提取探索/采集/攻击/撤退等）
        #    live 模式下 prev_events 通常为空，必须从 _decisions 标签重建事件流
        dec = getattr(strat, "_decisions", None) or {}
        for uid, d in dec.items():
            tag = d.get("tag", "") if isinstance(d, dict) else ""
            # uid 可能是原始 UUID 对象（obs.units[].uid），而 units[].id 已转字符串，
            # 统一 str(uid) 比较，否则永远不匹配 → 事件流为空。
            suid = str(uid)
            upos = next((u["pos"] for u in units if u["id"] == suid), None)
            if not upos:
                continue
            cn = UT_CN.get(next((u["type"] for u in units if u["id"] == suid), ""), "")
            if tag in ("scout", "osc_break", "scout_chunk"):
                evs.append({"type": "explore", "unit": suid, "pos": upos,
                            "detail": f"{cn}侦察"})
            elif tag in ("goto_resource", "harvest", "move_to_resource"):
                evs.append({"type": "harvest", "unit": suid, "pos": upos,
                            "detail": f"{cn}去资源"})
            elif tag in ("return_core", "deposit_move", "goto_core"):
                evs.append({"type": "deposit_move", "unit": suid, "pos": upos,
                            "detail": f"{cn}回核心"})
            elif tag in ("attack", "raid", "shoot", "fire", "kite"):
                evs.append({"type": "attack", "unit": suid, "pos": upos,
                            "detail": f"{cn}攻击"})
            elif tag in ("flee", "retreat"):
                evs.append({"type": "flee", "unit": suid, "pos": upos,
                            "detail": f"{cn}撤退"})
            elif tag in ("block", "patrol"):
                evs.append({"type": "patrol", "unit": suid, "pos": upos,
                            "detail": f"{cn}巡逻"})
            # collect/deposit 等其他标签略过（避免刷屏）

        # 4) 单位 cargo 变化检测（兜底事件：prev cargo → current cargo）
        for u in units:
            uid = u["id"]
            cur_cargo = u.get("cargo", 0)
            prev_cargo = self.prev_cargo.get(uid, 0)
            if cur_cargo > prev_cargo and cur_cargo > 0:
                evs.append({"type": "harvest", "unit": uid, "pos": u["pos"],
                            "detail": f"采集+{cur_cargo - prev_cargo}"})
            elif prev_cargo > 0 and cur_cargo == 0:
                evs.append({"type": "deposit_succeeded", "unit": uid, "pos": u["pos"],
                            "detail": "回核心交付"})
            self.prev_cargo[uid] = cur_cargo

        # 5) HP 变化（被攻击/攻击敌人）
        for u in units:
            uid = u["id"]
            cur_hp = u.get("hp", 0)
            prev_hp = self.prev_hp.get(uid)
            if prev_hp is not None and prev_hp > cur_hp:
                evs.append({"type": "attacked", "unit": uid, "pos": u["pos"],
                            "detail": f"HP {prev_hp}→{cur_hp}"})
            self.prev_hp[uid] = cur_hp
        for e in enemies:
            eid = e["id"]
            cur_hp = e.get("hp", 0)
            prev_hp = self.prev_enemy_hp.get(eid)
            if prev_hp is not None and prev_hp > cur_hp:
                evs.append({"type": "attack", "unit": eid, "pos": e.get("pos", [0, 0]),
                            "detail": f"敌方 HP {prev_hp}→{cur_hp}"})
            self.prev_enemy_hp[eid] = cur_hp

        # 6) 遭遇敌人（当前 tick 可见敌方）—— 限制频率避免刷屏
        if enemies and (tick % 10 == 0 or not getattr(self, "_encounter_tick", None)
                        or tick - self._encounter_tick >= 20):
            for e in enemies[:2]:
                evs.append({"type": "encounter_enemy", "unit": None,
                            "pos": e.get("pos", [0, 0]),
                            "detail": f"发现{e.get('type','enemy')}"})
            self._encounter_tick = tick

        # ---- 单位计数 ----
        uc = Counter(u["utype"] for u in getattr(obs, "units", []))
        unit_counts = {"worker": uc.get("WORKER", 0),
                       "vanguard": uc.get("VANGUARD", 0),
                       "ranger": uc.get("RANGER", 0)}

        # ---- 下一步生产（取本 tick 实际提交的 Core 生产指令）----
        next_prod = None
        if plan_core and plan_core[0] == "SPAWN" and isinstance(plan_core[1], dict):
            next_prod = plan_core[1].get("unit_type")

        # ---- 信标（公开位置，用于前端渲染；arena_hero 中世界坐标固定 (0,0)）----
        beacon = None
        bc = getattr(obs, "beacon", None)
        if isinstance(bc, dict) and bc.get("position"):
            beacon = list(bc["position"])

        frame = {
            "tick": tick,
            "core": core,
            "beacon": beacon,
            "resources": core["resources"] if core else 0,
            "units": units,
            "enemies": enemies,
            "obstacles": obstacles_out,
            "resources_cells": resources_cells,
            "events": evs,
            "danger_zones": [{"center": list(z["center"]), "radius": z["radius"],
                              "state": z["state"], "recorded": z["recorded"]}
                             for z in self.danger_zones],
            "death_events": death_events,
            "unit_counts": unit_counts,
            "next_production": next_prod,
        }
        self._emit(frame, obs_changed, strat)
        return frame

    # ------------------------------------------------------------------
    def _update_danger_zones(self, core, enemies, death_events, own_combat_now):
        enemy_combat = len([e for e in enemies if e["type"] != "ENEMY_CORE"])
        own_alive = len(own_combat_now)
        # 新阵亡 + 敌战斗单位 > 我方存活战斗单位 → 建禁止区
        if death_events and core is not None and enemy_combat > 0:
            if enemy_combat > own_alive:
                # 圆心 = 最推进（距我方 Core 曼哈顿最远）的敌方战斗单位
                combat = [e for e in enemies if e["type"] != "ENEMY_CORE"]
                center = max(combat, key=lambda e: _manhattan(e["pos"], core["pos"]))["pos"]
                last_death = max([d["pos"] for d in death_events],
                                 key=lambda p: _manhattan(p, core["pos"]))
                radius = _manhattan(center, last_death)
                self.danger_zones.append({
                    "center": (center[0], center[1]), "radius": radius,
                    "state": "forbidden", "recorded": enemy_combat,
                    "deaths": [d["pos"] for d in death_events],
                })
        # 转换：我方存活战斗单位 > 记录敌数 → 危险区
        for z in self.danger_zones:
            if z["state"] == "forbidden" and own_alive > z["recorded"]:
                z["state"] = "danger"
        # 解除：圆内已无敌方单位
        self.danger_zones = [
            z for z in self.danger_zones
            if any(_manhattan(e["pos"], z["center"]) <= z["radius"]
                   for e in enemies if e["type"] != "ENEMY_CORE")
        ]

    # ------------------------------------------------------------------
    def _atomic_write(self, path, data):
        """原子写入：先写 .tmp 再 os.replace；失败时 fallback 直接写目标文件。
        某些环境下（如 launchd 服务）os.replace 可能静默失败，
        fallback 确保数据一定能被 monitor 读到。
        写后校验：验证 .tmp 是合法 JSON 才替换目标，防止进程被信号中断
        导致截断文件覆盖有效数据。
        """
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, cls=_SafeEncoder)
        except Exception as e:
            import sys
            print(f"[bridge] 写入 {tmp} 失败（可能进程被中断）: {e}", file=sys.stderr)
            return  # 不替换目标，保留旧的有效数据
        # 校验：确认写入完整（防止被 SIGTERM 等信号截断）
        try:
            with open(tmp, "r", encoding="utf-8") as f:
                json.load(f)
        except (json.JSONDecodeError, ValueError):
            import sys
            print(f"[bridge] 警告: {tmp} 写入截断，跳过本次更新（保留旧数据）", file=sys.stderr)
            return
        try:
            os.replace(tmp, path)
        except Exception as e:
            import sys
            print(f"[bridge] os.replace 失败 ({path}): {e}，尝试直接复制", file=sys.stderr)
            try:
                import shutil
                shutil.copy2(tmp, path)
            except Exception as e2:
                print(f"[bridge] 复制也失败: {e2}", file=sys.stderr)

    def _emit(self, frame, obs_changed, strat):
        self.cur.append(frame)
        # 实时帧：每 tick 写 stream/latest.json，monitor 优先读取它
        lp = os.path.join(self.dir, "latest.json")
        self._atomic_write(lp, frame)
        # 事件流全量文件（旧 EventLog 格式，供 feed 回放 + 外部消费者）
        # 只保留最近 500 条事件，防止长局无限增长
        self._append_event_log(frame)
        if len(self.cur) >= SHARD_SIZE:
            self._flush_shard()
        self.map_since += 1
        if obs_changed or self.map_since >= MAP_SNAPSHOT_EVERY:
            self.map_since = 0
            self._write_map(frame, strat)

    def _append_event_log(self, frame):
        """追加当前帧事件到 stream/event_log.json（旧 EventLog.to_dict 格式）。

        格式：{"meta": {...}, "ticks": [{tick, events, ...}, ...]}
        monitor 的 renderFeed 从 TICKS[].events 读，event_log.json 是冗余全量备份。
        """
        path = os.path.join(self.dir, "event_log.json")
        MAX_EVTICKS = 500
        try:
            existing = {"meta": {}, "ticks": []}
            if os.path.exists(path):
                try:
                    existing = json.load(open(path))
                except Exception:
                    pass
            ticks = existing.get("ticks", [])
            # 追加当前帧（精简版：只保留 events + tick）
            ticks.append({
                "tick": frame["tick"],
                "events": frame.get("events", []),
                "core": frame.get("core"),
                "unit_counts": frame.get("unit_counts"),
            })
            # 环形裁剪
            if len(ticks) > MAX_EVTICKS:
                ticks = ticks[-MAX_EVTICKS:]
            self._atomic_write(path, {"meta": existing.get("meta", {}), "ticks": ticks})
        except Exception as e:
            import sys
            print(f"[bridge] _append_event_log 异常: {e}", file=sys.stderr)

    def _flush_shard(self):
        path = os.path.join(self.dir, f"shard_{self.shard_seq:05d}.jsonl")
        try:
            data = "\n".join(json.dumps(fr, ensure_ascii=False) for fr in self.cur) + "\n"
            # 分片用同样的原子+fallback 策略
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
            except Exception:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data)
        except Exception as e:
            import sys
            print(f"[bridge] _flush_shard 异常: {e}", file=sys.stderr)
        self.shard_seq += 1
        self.cur = []
        self._write_index()  # 更新 index.json 让 monitor 发现新 shard
        # 环形缓冲：删除最旧分片
        try:
            files = sorted(os.path.join(self.dir, f)
                           for f in os.listdir(self.dir)
                           if f.startswith("shard_") and f.endswith(".jsonl"))
            for old in files[:-MAX_SHARDS]:
                os.remove(old)
        except Exception:
            pass

    def _write_map(self, frame, strat):
        """写 stream/map.json：与已有数据**合并**（union），只增不减。

        新旧决策共享同一份 map.json：
        - 旧决策写的丰富地图（数千格探索+资源记忆+障碍）不会被新决策覆盖清零
        - 新决策发现的新区域会追加进去
        - 切换决策时地图完全一致，不受影响
        """
        # 1) 从 arena-evolve Memory / 帧提取当前决策"看到"的数据
        cur_obs, cur_exp, cur_res = set(), set(), {}
        mem = getattr(strat, "mem", None)
        if mem is not None:
            cur_obs = getattr(mem, "obstacles", set()) or set()
            cur_exp = getattr(mem, "visited", set()) or set()
            cur_res = getattr(mem, "resources", {}) or {}
        else:
            cur_obs = {tuple(p) for p in (frame.get("obstacles") or [])}

        # 2) 读已有 map.json（上次任何决策写的快照），做 union 合并
        path = os.path.join(self.dir, "map.json")
        prev_exp, prev_obs, prev_res = set(), set(), {}
        try:
            if os.path.exists(path):
                old = json.load(open(path))
                prev_exp = {tuple(p) for p in (old.get("explored_cells") or [])}
                prev_obs = {tuple(p) for p in (old.get("obstacles") or [])}
                for r in (old.get("resource_memory") or []):
                    if len(r) >= 3:
                        key = (r[0], r[1])
                        # 保留最新的 tick
                        if key not in prev_res or r[2] > prev_res[key]:
                            prev_res[key] = r[2]
        except Exception:
            pass

        # 3) 合并：explored/obstacles 做 union（只增不减）；resource_memory 做增量合并
        #    + 视野反证清理（当前视野覆盖的格若不在 cur_res 中 → 资源已被采空/消失，从记忆删除）
        merged_exp = prev_exp | cur_exp
        merged_obs = prev_obs | cur_obs
        merged_res = {**prev_res}
        for pos, tick in cur_res.items():
            key = (pos[0], pos[1]) if isinstance(pos, (list, tuple)) else pos
            if key not in merged_res or tick > merged_res[key]:
                merged_res[key] = tick
        # 视野反证：当前探索到的格(cur_exp)中，如果不再被策略记为资源(cur_res)，
        # 说明该位置资源已被采完或消失 → 从持久化记忆中清除，避免地图上显示幽灵资源
        if cur_exp:
            expired = [k for k in merged_res if k in cur_exp and k not in cur_res]
            for k in expired:
                del merged_res[k]

        snap = {
            "tick": frame["tick"],
            "sector_size": 32,
            "core": frame["core"]["pos"] if frame.get("core") else None,
            "obstacles": [list(p) for p in merged_obs],
            "resource_memory": [[k[0], k[1], v] for k, v in merged_res.items()],
            "explored_cells": [list(p) for p in merged_exp],
            "explored_sectors": [],
        }
        self._atomic_write(path, snap)
