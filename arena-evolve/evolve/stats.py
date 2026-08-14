"""对局统计收集：MOVE 失败 reason 分布 / Worker 排队 / Core 被占时长。

接入方式：evaluate_live(..., stats=stats) —— 循环里每 tick 喂 sample()，
评估结束调用 summary() 输出报告（与 fitness 一起打印，数据驱动后续优化）。
"""

from collections import Counter


class GameStats:
    def __init__(self):
        self.move_fail_reasons = Counter()   # MOVE_BLOCKED reason 分布
        self.terrain_blocked = 0             # 撞地形障碍总 tick（TERRAIN 类）
        self.core_occupied_ticks = 0         # Core 格被非满载单位占的总 tick
        self.core_occupied_events = 0        # 连续占格事件次数
        self._core_occ_streak = 0
        self._core_occ_max_streak = 0
        self.queue_unit_ticks = 0            # 满载 Worker 距 Core<=4 的累计单位-tick
        self.queue_sightings = 0             # 满载 Worker 距 Core<=4 的观测次数
        self.deposited = 0                   # DEPOSIT 成功次数
        self.harvested = 0                   # 采集成功次数
        self.heal_count = 0                  # 单位治疗次数（消耗 Core 资源）
        self.spawn_count = 0                 # 生产次数（消耗 Core 资源）
        self.core_damaged = 0                # Core 受伤次数（触发维修/治疗消耗）
        # ---- KPI ----
        self.move_ok = 0                     # 移动成功次数（MOVED 事件）
        self.move_fail = 0                   # 移动失败次数（MOVE_BLOCKED 事件）
        self.core_zero_res_ticks = 0         # Core 资源=0 的总 tick
        self._zero_streak = 0
        self._zero_max = 0
        self.worker_full_ticks = 0           # Worker 满载累计 tick（往返时长近似）
        self._full_from = {}                 # uid -> 满载起始 tick
        self.worker_full_trips = 0           # 满载→空载的完成次数

    def sample(self, obs):
        """每 tick 调用一次（obs = 我方 build_observation）。"""
        if obs.core is None:
            return
        core = tuple(obs.core["pos"])
        # 1) 移动成功/失败 + reason 分布
        for ev in obs.prev_events or []:
            et = ev.get("type")
            if et == "MOVE_BLOCKED":
                self.move_fail += 1
                self.move_fail_reasons[ev.get("reason") or "(unknown)"] += 1
            elif et == "MOVED":
                self.move_ok += 1
            elif et in ("DEPOSITED", "DEPOSIT"):
                self.deposited += 1
            elif et in ("HARVESTED", "HARVEST_SUCCEEDED"):
                self.harvested += 1
            elif et == "UNIT_HEALED":
                self.heal_count += 1
            elif et == "UNIT_SPAWNED":
                self.spawn_count += 1
            elif et == "CORE_DAMAGED":
                self.core_damaged += 1
        # 2) Core 格被非满载单位占（DEPOSIT 中的满载 Worker 是正常行为）
        on_core = [u for u in obs.units if tuple(u["pos"]) == core
                   and not (u["utype"] == "WORKER" and u["cargo"] > 0)]
        if on_core:
            self.core_occupied_ticks += 1
            self._core_occ_streak += 1
            self._core_occ_max_streak = max(self._core_occ_max_streak,
                                            self._core_occ_streak)
        elif self._core_occ_streak > 0:
            self.core_occupied_events += 1
            self._core_occ_streak = 0
        # 3) 满载 Worker 排队负载（距 Core<=4 的单位-tick）
        for u in obs.units:
            if u["utype"] == "WORKER" and u["cargo"] > 0 \
                    and abs(u["pos"][0] - core[0]) + abs(u["pos"][1] - core[1]) <= 4:
                self.queue_unit_ticks += 1
                self.queue_sightings += 1
        # 4) Core 资源=0 连续时长（KPI：经济枯竭风险）
        if obs.core["resources"] == 0:
            self.core_zero_res_ticks += 1
            self._zero_streak += 1
            self._zero_max = max(self._zero_max, self._zero_streak)
        else:
            self._zero_streak = 0
        # 5) Worker 满载→空载周期（往返时长近似 KPI）
        now_full = {}
        for u in obs.units:
            if u["utype"] == "WORKER" and u["cargo"] > 0:
                now_full[u["uid"]] = True
                if u["uid"] not in self._full_from:
                    self._full_from[u["uid"]] = obs.tick
        for uid in list(self._full_from):
            if uid not in now_full:
                self.worker_full_ticks += obs.tick - self._full_from[uid]
                self.worker_full_trips += 1
                del self._full_from[uid]

    def summary(self):
        """输出统计报告（与 fitness 一起展示）。"""
        total_fail = sum(self.move_fail_reasons.values())
        dist = {r: round(n / total_fail * 100, 1) if total_fail else 0
                for r, n in self.move_fail_reasons.most_common()}
        return {
            "move_fail_total": total_fail,
            "reason_dist": dist,
            "core_occupied_ticks": self.core_occupied_ticks,
            "core_occupied_events": self.core_occupied_events,
            "core_occ_max_streak": self._core_occ_max_streak,
            "queue_avg_tick": (self.queue_unit_ticks /
                               max(1, self.queue_sightings)),
            "queue_unit_ticks": self.queue_unit_ticks,
            "deposited": self.deposited,
            "harvested": self.harvested,
            "heal_count": self.heal_count,
            "spawn_count": self.spawn_count,
            "core_damaged": self.core_damaged,
            "move_ok": self.move_ok,
            "move_fail": self.move_fail,
            "move_success_rate": (self.move_ok / max(1, self.move_ok + self.move_fail)),
            "core_zero_res_max": self._zero_max,
            "core_zero_res_ticks": self.core_zero_res_ticks,
            "worker_roundtrip_avg": (self.worker_full_ticks /
                                     max(1, self.worker_full_trips)),
            "worker_full_trips": self.worker_full_trips,
            "heal_per_1000tick": self.heal_count,
        }

    def print_report(self, label="对局统计"):
        s = self.summary()
        print(f"\n== {label} ==")
        print(f"  MOVE 失败总数: {s['move_fail_total']}")
        for r, pct in s["reason_dist"].items():
            print(f"    {r}: {pct}%")
        print(f"  Core 被非满载单位占: {s['core_occupied_ticks']} tick"
              f"（{s['core_occupied_events']} 次，最长连续 {s['core_occ_max_streak']} tick）")
        print(f"  满载 Worker 排队: 平均 {s['queue_avg_tick']:.2f} 单位/观测"
              f"（累计 {s['queue_unit_ticks']} 单位-tick）")
        print(f"  DEPOSIT {s['deposited']} 次 / HARVEST {s['harvested']} 次")
        print(f"  治疗 {s['heal_count']} 次 / 生产 {s['spawn_count']} 次 / Core 受伤 {s['core_damaged']} 次")
        print(f"  MOVE 成功率: {s['move_success_rate']*100:.1f}%"
              f"（成功 {s['move_ok']} / 失败 {s['move_fail']}）")
        print(f"  Core 资源=0: 最长连续 {s['core_zero_res_max']} tick"
              f"（累计 {s['core_zero_res_ticks']} tick）")
        print(f"  Worker 满载周期: 平均 {s['worker_roundtrip_avg']:.1f} tick"
              f"（{s['worker_full_trips']} 次）")
