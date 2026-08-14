"""策略基类：Strategy 接口、A* 寻路、视野记忆。"""

import heapq

from ahsim.config import DIRECTIONS
from ahsim.observation import Observation

# 资源记忆过期阈值：资源点会被消耗、补给换位，超过该 tick 数未再确认
# 就当不存在（参考 Drew-Z arena-hero-agent RESOURCE_MEMORY_TTL=64；
# 我们侦察覆盖较稀，取 256 折中——线上死锁的旧点 500+ tick 仍会被清）。
RESOURCE_MEMORY_TTL = 256


class Strategy:
    name = "base"

    def reset(self):
        """新对局开始时调用。"""

    def decide(self, obs: Observation) -> dict:
        raise NotImplementedError


# ----------------------------------------------------------------------
# 视野记忆
# ----------------------------------------------------------------------
class Memory:
    """障碍永久、资源为过期观察。"""

    def __init__(self):
        self.obstacles = set()
        self.resources = {}      # (x, y) -> 最近看到该资源存在的 tick
        self.empty_since = {}    # (x, y) -> 最近确认该格无资源的 tick
        self._last_res_clean = 0  # 资源记忆过期清理计时（每 16 tick 一次）
        self._hit_count = {}     # (x, y) -> 累计撞墙次数（>=2 确认永久障碍）
        self.last_enemy_pos = {}  # key -> (pos, tick)，key 为 uid 或 "core_<player>"
        self.seen_beacon = False
        self.visited = set()     # 看到过的格（用于探索）
        self.move_fail = {}      # uid -> (direction, tick, reason) 最近被挡的移动
        self.new_obstacles = False  # 本 Tick 是否有新学习的障碍
        self.area_seen = {}      # (x, y) -> 最近可见 tick（区域回访）
        self.chunk_seen = {}     # (cx, cy) -> chunk 最近可见 tick（侦察扩散）
        self.harvest_chunks = {}  # (cx, cy) -> 最近成功采集 tick（补给回访）
        self.harvest_points = {}  # (x, y) -> 最近成功采集 tick（精确格回访：
                                  #  官方补给位置 chunk 内随机重选，原格附近
                                  #  大概率仍有资源，逐格记忆扑空率高）
        self.unreachable = set()  # A* 确认不可达的目标点（临时：200 tick 重试）
        self.isolated = set()     # 孤立格（4 邻全障碍）：物理上永远不可达，永久保留
        self.enemy_forgotten = set()  # 已遗忘的敌人 key（超时未确认）
        self.enemy_prev = {}     # key -> 上一帧位置（用于 Ranger 预判射击）
        self.enemy_dir = {}          # key -> 最近移动方向（预判射击方向稳定性）
        self.enemy_dir_streak = {}   # key -> 同方向连续帧数（≥2 才预判）
        self.stationary = {}     # key -> {pos, count, first, last} 静止目标检测
                                 # （连续多次同位置观察 → 疑似死玩家/驻留单位）
        self.temp_blocked = {}   # (x,y) -> [expire_tick, hits] 撞墙临时避让，2 次确认永久
        self.core_blocked = {}   # direction -> tick：Core 迁移被挡的方向（避障换向）

    def to_dict(self):
        """序列化记忆（用于崩溃恢复）。"""
        return {
            "obstacles": sorted(map(list, self.obstacles)),
            "resources": [[list(k), v] for k, v in sorted(self.resources.items())],
            "empty_since": [[list(k), v] for k, v in sorted(self.empty_since.items())],
            "last_enemy_pos": [[str(k), [list(p), v]]
                               for k, (p, v) in sorted(self.last_enemy_pos.items(),
                                                        key=lambda kv: str(kv[0]))],
            "visited": sorted(map(list, self.visited)),
            "area_seen": [[list(k), v] for k, v in sorted(self.area_seen.items())],
            "unreachable": sorted(map(list, self.unreachable)),
            "isolated": sorted(map(list, self.isolated)),
            "stationary": [[str(k), dict(v)]
                            for k, v in sorted(self.stationary.items(),
                                               key=lambda kv: str(kv[0]))],
            "harvest_chunks": [[list(k), v] for k, v in
                                sorted(self.harvest_chunks.items())],
            "harvest_points": [[list(k), v] for k, v in
                                sorted(self.harvest_points.items())],
        }

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.obstacles = {tuple(c) for c in d.get("obstacles", [])}
        m.resources = {tuple(k): v for k, v in d.get("resources", [])}
        m.empty_since = {tuple(k): v for k, v in d.get("empty_since", [])}
        m.last_enemy_pos = {}
        for k, (p, v) in d.get("last_enemy_pos", []):
            key = int(k) if k.lstrip("-").isdigit() else k
            m.last_enemy_pos[key] = (tuple(p), v)
        m.visited = {tuple(c) for c in d.get("visited", [])}
        m.area_seen = {tuple(k): v for k, v in d.get("area_seen", [])}
        m.unreachable = {tuple(c) for c in d.get("unreachable", [])}
        m.isolated = {tuple(c) for c in d.get("isolated", [])}
        for k, v in d.get("stationary", []):
            key = int(k) if k.lstrip("-").isdigit() else k
            v = dict(v)
            # 恢复的 pos 是 JSON list，必须转 tuple：否则 _pick_raid_target 返回
            # list 进 _raid_point → pf.find key=(start,list) 崩溃，且
            # rec[0] != st["pos"]（tuple vs list）恒不等使 raid 永远失效
            if isinstance(v.get("pos"), list):
                v["pos"] = tuple(v["pos"])
            m.stationary[key] = v
        m.harvest_chunks = {tuple(k): v for k, v in d.get("harvest_chunks", [])}
        m.harvest_points = {tuple(k): v for k, v in d.get("harvest_points", [])}
        return m

    def _track_stationary(self, key, pos, tick):
        """静止检测：同一位置连续观察则计数；位置变化即移除标记。"""
        st = self.stationary.get(key)
        if st is not None and st["pos"] == pos:
            st["count"] += 1
            st["last"] = tick
        else:
            self.stationary[key] = {"pos": pos, "count": 1,
                                    "first": tick, "last": tick}

    def prune(self, core_pos=None, max_dist=120, max_entries=10000):
        """长跑内存淘汰：区域/访问记忆按距 Core 距离裁剪 + 总量上限；
        已遗忘的敌人真正删除（不再只标记）。持久世界几万 tick 后防存档膨胀。"""
        # 1) 真正删除已遗忘的敌人（释放 last_enemy_pos / enemy_prev）
        if self.enemy_forgotten:
            for k in list(self.enemy_forgotten):
                self.last_enemy_pos.pop(k, None)
                self.enemy_prev.pop(k, None)
                self.enemy_dir.pop(k, None)
                self.enemy_dir_streak.pop(k, None)
            self.enemy_forgotten.clear()
        # 2) 敌人记忆总量上限（保留最近看到的）
        if len(self.last_enemy_pos) > 200:
            stale = sorted(self.last_enemy_pos, key=lambda k: -self.last_enemy_pos[k][1])[200:]
            for k in stale:
                self.last_enemy_pos.pop(k, None)
                self.enemy_prev.pop(k, None)
                self.enemy_dir.pop(k, None)
                self.enemy_dir_streak.pop(k, None)
        # 3) 区域/访问记忆：超距裁剪 + 上限（超上限删最旧）
        if core_pos is not None:
            far = [c for c in self.area_seen
                   if abs(c[0] - core_pos[0]) + abs(c[1] - core_pos[1]) > max_dist]
            for c in far:
                self.area_seen.pop(c, None)
                self.visited.discard(c)
        if len(self.area_seen) > max_entries:
            oldest = sorted(self.area_seen, key=lambda c: self.area_seen[c])[:len(self.area_seen) - max_entries]
            for c in oldest:
                self.area_seen.pop(c, None)
                self.visited.discard(c)
        # 4) 临时不可达标记定期全清：A* 判定可能受临时障碍（temp_blocked）/视野
        #    限制影响，永久保留会耗尽侦察/采集候选（线上曾积累 27+ 点后全员
        #    WAIT）。每轮 prune（200 tick）清空重试，真不可达会再次快速失败。
        #    **孤立格（isolated，4 邻全障碍）物理上永远不可达，不参与清理** ——
        #    逻辑正确的前提下它不会变可达，反复重试纯属浪费 A*。
        self.unreachable.clear()

        # 5) 封闭区域检测：障碍环可能围出多格"岛"（内部格 4 邻未必全障碍，
        #    但从 Core 出发永远进不去）。从 Core 沿已探索空地 BFS，
        #    已探索但不在可达域的空地格 → 整体判为封闭区（永久不可达）。
        #    - 只在已探索格（area_seen）内判定：未探索格可能还有通路，不判死；
        #    - 只用永久障碍（不含 temp_blocked）：临时障碍不误判；
        #    - isolated 每次 prune 重建（非累积）：障碍记忆被反证修正后
        #      封闭区自动解除。
        if core_pos is not None:
            start = tuple(core_pos)
            reach = {start}
            frontier = [start]
            # BFS 允许穿越未探索格（当空地）：否则探索区被未探索带分隔时
            # （视野可越过缺口看到内部）会把可达区域误判为封闭。
            # 未探索格只限探索区包围盒内（外面不参与，防漫游爆炸）。
            if self.area_seen:
                xs = [c[0] for c in self.area_seen]
                ys = [c[1] for c in self.area_seen]
                bx0, bx1, by0, by1 = min(xs) - 8, max(xs) + 8, min(ys) - 8, max(ys) + 8
            else:
                bx0 = bx1 = by0 = by1 = 0
            # 提前退出：所有已探索格都进入 reach 时结束（不能只数 reach 大小
            # —— 穿越的未探索格也会撑大 reach，但目标格可能还没到）
            seen_left = len(self.area_seen) - (1 if start in self.area_seen else 0)
            while frontier and seen_left > 0:
                cx, cy = frontier.pop()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nb = (cx + dx, cy + dy)
                    if nb in reach or nb in self.obstacles:
                        continue
                    in_seen = nb in self.area_seen
                    if not in_seen:
                        # 未探索格：只在探索区包围盒内穿越
                        if not (bx0 <= nb[0] <= bx1 and by0 <= nb[1] <= by1):
                            continue
                    reach.add(nb)
                    if in_seen:
                        seen_left -= 1
                    frontier.append(nb)
            self.isolated = {c for c in self.area_seen
                             if c not in reach and c not in self.obstacles}

    def mark_unreachable(self, pos):
        """A* 确认无路后归类：4 邻全在障碍记忆里 → 孤立格（永久）；
        否则 → 临时不可达（可能受 temp_blocked/假障碍影响，200 tick 重试）。
        注意只用永久障碍判定孤立（不含 temp_blocked），避免临时障碍
        导致误判孤立后永久占坑。"""
        x, y = pos
        nb = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
        if all(c in self.obstacles for c in nb):
            self.isolated.add(pos)
            self.unreachable.discard(pos)
        else:
            self.unreachable.add(pos)
            self.isolated.discard(pos)

    def is_unreachable(self, pos):
        """临时或永久不可达都算不可达（候选过滤用）。"""
        return pos in self.unreachable or pos in self.isolated

    def update(self, obs: Observation):
        before = len(self.obstacles)
        self.obstacles.update(obs.obstacles)
        if len(self.obstacles) > before:
            # 视野发现新障碍 → 使 A* 缓存失效（否则缓存路径会穿过
            # 后发现的障碍 → 无限撞墙卡死）
            self.new_obstacles = True
        if obs.visible_cells is not None:
            self.visited.update(obs.visible_cells)
            now = obs.tick
            for c in obs.visible_cells:
                self.area_seen[c] = now
                self.chunk_seen[(c[0] // 32, c[1] // 32)] = now
            # 假障碍反证：本次视野内可见、但不在障碍列表中的格 → 从记忆移除。
            # （旧版曾把 MOVE_BLOCKED 的格永久记入 obstacles：排队采集的
            #  资源格/单位占位格被误记为障碍 → A* 假不可达。障碍记忆只增
            #  不减会永远带着这些假障碍，这里用视野反证自愈。）
            seen_now = set(obs.visible_cells)
            false_obs = {c for c in self.obstacles
                         if c in seen_now and c not in obs.obstacles}
            if false_obs:
                self.obstacles -= false_obs
                self.new_obstacles = True   # 移除也会改变可达性 → bump A*
            # 敌方 Core 位置反证：视野内看到该位置无 Core → 立即遗忘。
            # （打掉僵尸 Core 后旧记忆要等 forget_ticks×CORE_FORGET_MULT
            #  才过期，期间战斗单位一直追旧位置/打静止目标空转）
            ec_pos = {tuple(ec["pos"]) for ec in obs.enemy_cores}
            for k in list(self.last_enemy_pos):
                if not str(k).startswith("core_"):
                    continue
                p, _t = self.last_enemy_pos[k]
                if p in seen_now and p not in ec_pos:
                    self.last_enemy_pos.pop(k, None)
                    self.enemy_prev.pop(k, None)
                    self.enemy_dir.pop(k, None)
                    self.enemy_dir_streak.pop(k, None)
                    self.enemy_forgotten.add(k)
            for k in list(self.stationary):
                if not str(k).startswith("core_"):
                    continue
                p = self.stationary[k].get("pos")
                if p is None:
                    continue
                p = tuple(p)   # 恢复自 memory.json 时可能是 list → 先转 tuple
                if p in seen_now and p not in ec_pos:
                    self.stationary.pop(k, None)
        for r in obs.resources:  # 只记自然点（cargo 掉落堆不计配额、会消失，不记）
            self.resources[r] = obs.tick
            self.empty_since.pop(r, None)
        # 过期清理：资源点会被消耗/补给换位，记忆超过 TTL 未再确认就当不存在
        # （参考 Drew-Z RESOURCE_MEMORY_TTL=64；否则旧点占坑导致"永远够不着"）
        if obs.tick - self._last_res_clean >= 16:
            self._last_res_clean = obs.tick
            for cell in list(self.resources):
                if obs.tick - self.resources[cell] > RESOURCE_MEMORY_TTL:
                    del self.resources[cell]
        # 采集成功事件 → 记录 chunk，供侦察回访（资源 4 tick 补给在已消耗 chunk）
        if obs.prev_events:
            for ev in obs.prev_events:
                et = ev.get("type", "")
                if et in ("HARVESTED", "HARVEST_SUCCEEDED"):
                    pos = ev.get("pos") or ev.get("cell") or ev.get("at")
                    if pos is not None:
                        self.harvest_chunks[(pos[0] // 32, pos[1] // 32)] = obs.tick
                        self.harvest_points[tuple(pos)] = obs.tick
                elif et in ("HARVEST_FAILED", "HARVEST_FAIL"):
                    # NOT_RESOURCE_CELL：记忆点与实际不符（补给换位/已采空），
                    # 立即移除避免无效重试；RESOURCE_DEPLETED：同 tick 竞争输
                    # 了（UUID 仲裁），保留记忆下 tick 重试；reason 缺失靠
                    # 视野确认空逻辑兜底。
                    if ev.get("reason") == "NOT_RESOURCE_CELL":
                        pos = ev.get("pos") or ev.get("cell") or ev.get("at")
                        if pos is not None:
                            self.resources.pop(tuple(pos), None)
                            # 已过补给周期的推测点也清（避免空跑回访）
                            if obs.tick - self.harvest_points.get(tuple(pos), obs.tick) > 4:
                                self.harvest_points.pop(tuple(pos), None)
        # 视野内确认无资源的格：从记忆移除（被采空/消失，与官方当前状态同步）
        if obs.visible_cells:
            for cell in list(self.resources):
                if cell in obs.visible_cells and cell not in obs.resources:
                    del self.resources[cell]
            # 补给推测点同理：确认空且已过补给周期（4 tick）→ 补给没回到
            # 原格（换位），保留只会让 Worker 空跑；刚采空的格（<4 tick）
            # 正处于补给窗口，保留等补给
            for cell in list(self.harvest_points):
                if obs.tick - self.harvest_points[cell] > 4 \
                        and cell in obs.visible_cells \
                        and cell not in obs.resources:
                    del self.harvest_points[cell]
        # 敌人逐帧位置跟踪（始终记录上一帧位置，两帧可见才能预测移动方向）
        # 同时统计方向连续帧数（dir_streak）：方向稳定 ≥2 帧才值得预判射击
        #（敌人 zigzag/转向时预测必空——连续 SHOT_MISSED 的主因）
        for e in obs.enemies:
            key = e["uid"]
            pos = tuple(e["pos"])
            prev = self.last_enemy_pos.get(key)
            if prev is not None:
                self.enemy_prev[key] = prev[0]
                d = (pos[0] - prev[0][0], pos[1] - prev[0][1])
                if abs(d[0]) + abs(d[1]) == 1:
                    if self.enemy_dir.get(key) == d:
                        self.enemy_dir_streak[key] = self.enemy_dir_streak.get(key, 0) + 1
                    else:
                        self.enemy_dir_streak[key] = 1
                    self.enemy_dir[key] = d
                else:
                    self.enemy_dir_streak[key] = 0
                    self.enemy_dir.pop(key, None)
            self.last_enemy_pos[key] = (pos, obs.tick)
            self._track_stationary(key, pos, obs.tick)
            # 重新看到 → 解除遗忘（可能只是短暂离开视野）
            self.enemy_forgotten.discard(key)
        # 普通敌方单位反证：记忆位置在视野内、但该 uid 不在当前视野中
        # → 它已不在那里（死了/离开），立即遗忘，而不是等 forget_ticks
        # 让战斗单位打 147 tick 空位（用户反馈：打掉的单位还在被追踪）
        if obs.visible_cells is not None:
            seen_keys = {e["uid"] for e in obs.enemies}
            for k in list(self.last_enemy_pos):
                if str(k).startswith("core_"):
                    continue
                p, _t = self.last_enemy_pos[k]
                if k not in seen_keys and tuple(p) in seen_now:
                    self.last_enemy_pos.pop(k, None)
                    self.enemy_prev.pop(k, None)
                    self.enemy_dir.pop(k, None)
                    self.enemy_dir_streak.pop(k, None)
                    self.enemy_forgotten.add(k)
            for k in list(self.stationary):
                if str(k).startswith("core_"):
                    continue
                p = self.stationary[k].get("pos")
                if p is None:
                    continue
                p = tuple(p)
                if k not in seen_keys and p in seen_now:
                    self.stationary.pop(k, None)
        # 敌方 Core 也做静止检测（死玩家识别）
        for c in obs.enemy_cores:
            key = "core_" + str(c["owner"])
            pos = tuple(c["pos"])
            self.last_enemy_pos[key] = (pos, obs.tick)
            self._track_stationary(key, pos, obs.tick)
        # 记录可见但当前无资源的格（可能已被采，4 tick 后可能补给回来）
        if obs.visible_cells is not None:
            for r in list(self.resources):
                if r in obs.visible_cells and r not in obs.resources:
                    self.empty_since.setdefault(r, obs.tick)
        # 移动反馈：被挡方向格进入临时障碍（A* 短期避开）；
        # 同一格撞 2 次 → 确认永久障碍（真实地形）；仅 1 次可能是单位冲突。
        # Core 的迁移被挡单独记录方向（START_MOVE 不走 A*，需显式换向）。
        if obs.prev_events:
            uid_pos = {u["uid"]: u["pos"] for u in obs.units}
            core_uid = obs.core.get("uid") if obs.core else None
            for ev in obs.prev_events:
                if ev.get("type") == "MOVE_BLOCKED":
                    uid = ev["obj_id"]
                    reason = ev.get("reason") or ""
                    self.move_fail[uid] = (ev["direction"], obs.tick, reason)
                    if uid == core_uid:
                        self.core_blocked[ev["direction"]] = obs.tick
                    pos = uid_pos.get(uid)
                    if pos is None:
                        continue
                    dx, dy = DIRECTIONS[ev["direction"]]
                    cell = (pos[0] + dx, pos[1] + dy)
                    # reason_code 事件化（存 move_fail 供统计/诊断）。
                    # 标记决策保持旧逻辑：A/B 实验证明 reason 直标有害——
                    #  TERRAIN 永久化会让 Core 撞资源格/cargo 堆的误判无法
                    #  自愈（fitness -10）；LIMIT/OCCUPIED 无条件短时效会把
                    #  排队（我方占位格）变成绕行（fitness -8）。旧逻辑的
                    #  occupied 保护 + 满格判断 + hit_count 渐进已更精细。
                    # 唯一净收益：竞争类明确不标记（它们不是障碍问题，
                    # 引擎的链式/环交换自己会解开）。
                    if reason in ("MOVE_CONTESTED", "MOVE_SWAP_BLOCKED",
                                  "MOVE_DEPENDENCY_FAILED"):
                        continue
                    # ---- 撞墙避让（TERRAIN/LIMIT/OCCUPIED 与旧逻辑同路） ----
                    if cell not in self.obstacles and cell not in self.resources:
                        # 单位占位格不标记：MOVE_BLOCKED 经常是"目标格被
                        # 其他单位/Core 占着"（排队采集/存放）——把占位格
                        # 当障碍会让 A* 判定 Core/资源点不可达 → 满载
                        # Worker 全体 WAIT → 经济死锁。
                        occupied = {tuple(v) for v in uid_pos.values()}
                        if core_uid is not None and obs.core is not None:
                            occupied.add(tuple(obs.core["pos"]))
                        if cell in occupied:
                            # 但**满格**（容量 2 已满，官方规则：进入被占格
                            # 要求占用者全部离开且最终容量允许，满格时除非
                            # 两占用者同时离开否则必败）物理上进不去：短时效
                            # 避让让 A* 绕行——否则缓存路径永远撞同一满格 →
                            # 无限 UNIT_MOVE_FAILED/CELL_UNIT_LIMIT。
                            if sum(1 for v in uid_pos.values() if v == cell) >= 2:
                                self.temp_blocked[cell] = [obs.tick + 4, 0]
                                self.new_obstacles = True
                            continue
                        # 撞墙只做临时避让，且**资源点格不标记**：MOVE_BLOCKED
                        # 经常是"资源格被其他 Worker 占着"（排队采集），
                        # 把资源点当障碍会让 A* 判定目标不可达 → 采集瘫痪。
                        # 撞得越频繁避让越久（4→32 tick），过期后重试。
                        n = self._hit_count.get(cell, 0) + 1
                        self._hit_count[cell] = n
                        self.temp_blocked[cell] = [
                            obs.tick + min(4 * n, 32), n]
                        # 临时障碍增删都会改变可达性：必须 bump A* 版本，
                        # 否则缓存里的"不可达"结论在障碍过期后依然有效
                        # → 单位被假不可达永久卡死。
                        self.new_obstacles = True
                elif ev.get("type") == "MOVED" and ev.get("obj_id") in self.move_fail:
                    del self.move_fail[ev["obj_id"]]
        for ec in obs.enemy_cores:
            self.last_enemy_pos["core_" + str(ec["owner"])] = (tuple(ec["pos"]), obs.tick)
        if obs.beacon.get("status") != "UNKNOWN":
            self.seen_beacon = True


# ----------------------------------------------------------------------
# A* 寻路（4 方向，障碍记忆）
# ----------------------------------------------------------------------
class Pathfinder:
    # 搜索失败时 A* 会把整个连通分量 flood 一遍（256² 地图上约 30ms）。
    # 实测这类失败只占 3% 的调用却吃掉 97% 的寻路时间，故给展开数设预算：
    # 超预算即判定不可达。2000 远大于任何一次成功搜索的实际展开数，
    # 因此只砍掉无谓的 flood，不改变寻路结果。
    _EXPAND_BUDGET = 2000

    def __init__(self, bounds, is_obstacle, goal_ok=None):
        """bounds: (x_lo, x_hi, y_lo, y_hi) 或 None（无限世界）。
        goal_ok: 可选回调 (cell)->bool，目标格豁免障碍检查（排队语义：
        目标格被自己人占满时 A* 仍可达，走过去排队等对方离开）。"""
        self.bounds = bounds
        self.is_obstacle = is_obstacle
        self.goal_ok = goal_ok
        self._cache = {}       # (start, goal) -> (version, path)
        self._version = 0

    def bump_version(self):
        """障碍记忆变化时调用：使旧缓存失效（延迟清理）。"""
        self._version += 1

    def clear_cache(self):
        self._cache.clear()

    def _in_bounds(self, x, y):
        if self.bounds is None:
            return True
        x_lo, x_hi, y_lo, y_hi = self.bounds
        return x_lo <= x <= x_hi and y_lo <= y <= y_hi

    def find(self, start, goal, max_steps=300):
        """返回从 start 到 goal 的路径（不含 start、含 goal）。"""
        # 入口规范化：调用方可能传 list（obs 的 pos / stationary 恢复值），
        # 内部 key=(start,goal) 与 g_score/heap 都要求 tuple（2026-08-07
        # agent2 线上崩溃：_raid_point=list → key 含 list → 每 tick TypeError）
        start = tuple(start)
        goal = tuple(goal)
        if start == goal:
            return []
        key = (start, goal)
        entry = self._cache.get(key)
        if entry is not None and entry[0] == self._version:
            return entry[1]
        gx, gy = goal
        heap = [(0, 0, 0, start)]
        g_score = {start: 0}
        came = {}
        found = False
        expanded = 0
        budget = self._EXPAND_BUDGET
        while heap:
            f, _h, _c, cur = heapq.heappop(heap)
            if cur == goal:
                found = True
                break
            expanded += 1
            if expanded > budget:
                break          # 预算耗尽 → 当作不可达
            if g_score[cur] > max_steps:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cur[0] + dx, cur[1] + dy)
                if not self._in_bounds(*nxt):
                    continue
                if self.is_obstacle(*nxt):
                    # 目标格豁免：目标满格（容量 2 已满）时仍可达——
                    # 排队等对方离开（否则 next_step 贪心退化会绕过
                    # A* 判定一步步撞向满格 → 目标旁振荡）
                    if nxt == goal and self.goal_ok and self.goal_ok(nxt):
                        pass
                    else:
                        continue
                ng = g_score[cur] + 1
                if ng < g_score.get(nxt, 1 << 30):
                    g_score[nxt] = ng
                    h = abs(nxt[0] - gx) + abs(nxt[1] - gy)
                    # 加权 A* + 平局打破：f/h 相同时优先切比雪夫距离大的格
                    # （更接近直线）；再相同按固定方向序（E,S,N,W）——
                    # deterministic tie-break（防 A↔B 横跳振荡）
                    c = max(abs(nxt[0] - gx), abs(nxt[1] - gy))
                    came[nxt] = cur
                    heapq.heappush(heap, (ng + h * 3 // 2, h, -c, nxt))
        if not found:
            self._cache[key] = (self._version, None)
            return None
        path = []
        cur = goal
        while cur != start:
            path.append(cur)
            cur = came[cur]
        path.reverse()
        self._cache[key] = (self._version, path)
        return path

    def next_step(self, start, goal, max_steps=120):
        """返回向 goal 的下一步（或 None）。"""
        path = self.find(start, goal, max_steps)
        if path:
            return path[0]
        # A* 失败（目标不可达/太远）：退化为直线贪心（避障）
        x, y = start
        gx, gy = goal
        cands = []
        if gx > x:
            cands.append((x + 1, y))
        elif gx < x:
            cands.append((x - 1, y))
        if gy > y:
            cands.append((x, y + 1))
        elif gy < y:
            cands.append((x, y - 1))
        for c in cands:
            if not self.is_obstacle(*c):
                return c
        # 全部阻挡：尝试四方向任一空地
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (x + dx, y + dy)
            if self._in_bounds(*c) and not self.is_obstacle(*c):
                return c
        return None

    @staticmethod
    def direction_to(start, goal):
        """相邻两步之间的方向。"""
        dx = goal[0] - start[0]
        dy = goal[1] - start[1]
        if abs(dx) > abs(dy):
            return "RIGHT" if dx > 0 else "LEFT"
        if dy != 0:
            return "DOWN" if dy > 0 else "UP"
        return "RIGHT" if dx > 0 else "LEFT"


def dir_name(dx, dy):
    for name, (ndx, ndy) in DIRECTIONS.items():
        if (ndx, ndy) == (dx, dy):
            return name
    return None
