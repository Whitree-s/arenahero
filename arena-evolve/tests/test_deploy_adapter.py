"""LiveAdapter 离线测试：用 SDK 真实模型伪造 Turn，验证正式世界适配层。

正式世界的事件名/字段和模拟器不同，这层翻译错了策略就是瞎的，但线上没法回归，
所以在这里离线锁住。

运行：.venv/bin/python tests/test_deploy_adapter.py
"""

import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arena_hero import Turn
from arena_hero.actions import CancelMoveAction, CommandPlan, DropBeaconAction
from arena_hero.enums import BeaconStatus, CoreState, PlayerStatus, UnitType
from arena_hero.models import (
    ChampionBeacon, CoreView, PlayerState, ResolutionEvent, TerrainView, UnitView,
)

from deploy import LiveAdapter
from strategies.heuristic import HeuristicStrategy, make_default_genes

W_ID = UUID("00000000-0000-0000-0000-0000000000a1")
C_ID = UUID("00000000-0000-0000-0000-0000000000c1")
E_ID = UUID("00000000-0000-0000-0000-0000000000e1")


def make_turn(*, tick=10, worker_pos=(5, 5), core_pos=(4, 5), events=(),
              beacon_status=None, carrier_id=None, obstacles=(), resources=()):
    objects = [
        UnitView(kind="UNIT", id=W_ID, controlled=True, position=worker_pos,
                 hp=2, unit_type=UnitType.WORKER, cargo=0),
        CoreView(kind="CORE", id=C_ID, controlled=True, owner_username="tester",
                 position=core_pos, hp=5, shield=5, state=CoreState.NORMAL),
    ]
    if obstacles:
        objects.append(TerrainView(kind="OBSTACLE", positions=tuple(obstacles)))
    if resources:
        objects.append(TerrainView(kind="RESOURCE", positions=tuple(resources)))
    state = PlayerState(
        status=PlayerStatus.ACTIVE, respawn_at_tick=None, resources=7,
        population=1,
        # rules v0.14 / SDK 0.2.9：population_tier/upkeep_next_tick 已删除
        champion_beacon=ChampionBeacon(position=(0, 0), status=beacon_status,
                                       carrier_id=carrier_id),
        objects=tuple(objects), events=tuple(events),
    )
    return Turn(tick=tick, state=state, submitter=lambda *a, **k: None)


def mk_adapter():
    return LiveAdapter(HeuristicStrategy(genes=make_default_genes(), bounds=None))


# ----------------------------------------------------------------------
def test_move_failed_becomes_move_blocked():
    """UNIT_MOVE_FAILED(actor_id) + 上一 tick 提交的方向 → MOVE_BLOCKED。"""
    a = mk_adapter()
    a._sent_dirs = {W_ID: "RIGHT"}          # 模拟上一 tick 提交过 MOVE RIGHT
    ev = ResolutionEvent(event_id=UUID(int=1), tick=9,
                         event_type="UNIT_MOVE_FAILED",
                         reason_code="MOVE_BLOCKED_TERRAIN",
                         actor_id=W_ID, target_id=None, position=(5, 5),
                         values=None)
    out = a.translate_events(make_turn(events=[ev]))
    assert len(out) == 1, out
    assert out[0] == {"type": "MOVE_BLOCKED", "obj_id": W_ID,
                      "direction": "RIGHT",
                      "reason": "MOVE_BLOCKED_TERRAIN"}, out[0]


def test_move_failed_without_known_direction_is_skipped():
    """方向不明时必须跳过，否则会把错误的格子学成障碍。"""
    a = mk_adapter()
    ev = ResolutionEvent(event_id=UUID(int=2), tick=9,
                         event_type="UNIT_MOVE_FAILED", reason_code="MOVE_CONTESTED",
                         actor_id=W_ID, target_id=None, position=(5, 5), values=None)
    out = a.translate_events(make_turn(events=[ev]))
    assert out == [], out


def test_move_succeeded_becomes_moved():
    a = mk_adapter()
    ev = ResolutionEvent(event_id=UUID(int=3), tick=9,
                         event_type="UNIT_MOVE_SUCCEEDED", reason_code=None,
                         actor_id=W_ID, target_id=None, position=(6, 5), values=None)
    out = a.translate_events(make_turn(events=[ev]))
    assert out[0]["type"] == "MOVED" and out[0]["obj_id"] == W_ID, out


def test_core_move_failed_uses_migration_direction():
    a = mk_adapter()
    a._core_dir = "UP"
    ev = ResolutionEvent(event_id=UUID(int=4), tick=9,
                         event_type="CORE_MOVE_FAILED",
                         reason_code="CORE_DESTINATION_TERRAIN_BLOCKED",
                         actor_id=C_ID, target_id=None, position=(4, 5), values=None)
    out = a.translate_events(make_turn(events=[ev]))
    assert out[0] == {"type": "MOVE_BLOCKED", "obj_id": C_ID, "direction": "UP",
                      "reason": "CORE_DESTINATION_TERRAIN_BLOCKED"}, out


def test_carries_beacon_derived_from_carrier_id():
    a = mk_adapter()
    turn = make_turn(beacon_status=BeaconStatus.CARRIED, carrier_id=W_ID)
    obs = a.build_observation(turn)
    assert obs.units[0]["carries_beacon"] is True
    turn2 = make_turn(beacon_status=BeaconStatus.CARRIED, carrier_id=E_ID)
    obs2 = a.build_observation(turn2)
    assert obs2.units[0]["carries_beacon"] is False


def test_beacon_status_compares_as_plain_string():
    """策略里是 obs.beacon["status"] == "GROUND"，StrEnum 必须能直接比。"""
    a = mk_adapter()
    obs = a.build_observation(make_turn(beacon_status=BeaconStatus.GROUND))
    assert obs.beacon["status"] == "GROUND"
    obs2 = a.build_observation(make_turn())
    assert obs2.beacon["status"] == "UNKNOWN"


def test_v014_observation_and_status_have_no_upkeep_fields():
    """Removed v0.13 maintenance fields must not leak into local adapters."""
    a = mk_adapter()
    turn = make_turn()
    obs = a.build_observation(turn)
    assert not hasattr(obs, "population_tier")
    assert not hasattr(obs, "upkeep_next_tick")
    status = a.build_status(turn.tick, obs, {"units": {}, "core": None})
    assert "upkeep_next" not in status


def test_events_come_from_current_turn_not_stale_copy():
    """turn.events 已经是上一 Tick 的结果，不能再延迟一拍。"""
    a = mk_adapter()
    a._sent_dirs = {W_ID: "RIGHT"}
    ev = ResolutionEvent(event_id=UUID(int=5), tick=9,
                         event_type="UNIT_MOVE_FAILED", reason_code="X",
                         actor_id=W_ID, target_id=None, position=(5, 5), values=None)
    obs = a.build_observation(make_turn(events=[ev]))
    assert obs.prev_events and obs.prev_events[0]["type"] == "MOVE_BLOCKED"


def test_memory_learns_obstacle_end_to_end():
    """整条链路：连续两次撞同一格 → 临时避让（temp_blocked），
    不学成永久障碍——MOVE_BLOCKED 无法区分地形与单位占位，
    永久化会产生假障碍把可达资源围死。"""
    a = mk_adapter()
    for _ in range(2):
        a._sent_dirs = {W_ID: "RIGHT"}
        ev = ResolutionEvent(event_id=UUID(int=6), tick=9,
                             event_type="UNIT_MOVE_FAILED",
                             reason_code="MOVE_BLOCKED_TERRAIN",
                             actor_id=W_ID, target_id=None, position=(5, 5),
                             values=None)
        obs = a.build_observation(make_turn(events=[ev]))
        a.strategy.decide(obs)
    assert (6, 5) in a.strategy.mem.temp_blocked, a.strategy.mem.temp_blocked
    assert (6, 5) not in a.strategy.mem.obstacles, a.strategy.mem.obstacles


def test_dead_own_unit_is_classified_from_previous_observation():
    """The dead Unit is absent from the new state but remains ours for logging."""
    from types import SimpleNamespace

    a = mk_adapter()
    a._known_own_ids = {W_ID, C_ID}
    messages = []
    a._log_key_event = lambda ev, et, ids, obs=None: messages.append(
        a._fmt_key_event(ev, et, ids, obs))
    death = ResolutionEvent(
        event_id=UUID(int=7), tick=10, event_type="UNIT_DAMAGED",
        reason_code="ATTACK", actor_id=None, target_id=W_ID,
        position=(5, 5), values={"damage": 1, "hp": 0})
    empty_obs = SimpleNamespace(units=[], core={"uid": C_ID}, enemies=[])

    a.translate_events(make_turn(events=[death]), obs=empty_obs)

    assert messages and "我方单位阵亡" in messages[0], messages
    assert a.recent_events[-1]["target_is_own"] is True


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



def test_memory_from_dict_stationary_pos_tuple():
    """回归：memory.json 恢复的 stationary.pos 必须是 tuple——
    否则 _pick_raid_target 返回 list → _raid_point → pf.find key 含 list
    崩溃（2026-08-07 agent2 线上每 tick TypeError）。"""
    from strategies.base import Memory
    m = Memory.from_dict({
        "stationary": [["core_abc", {"pos": [1, 2], "count": 3,
                                     "first": 10, "last": 20}]]})
    st = m.stationary["core_abc"]
    assert isinstance(st["pos"], tuple), st["pos"]
    assert st["pos"] == (1, 2)


def test_pathfinder_accepts_list_pos():
    """回归：pf.find 必须接受 list 坐标（调用方可能传 list）。"""
    from strategies.base import Pathfinder
    pf = Pathfinder(is_obstacle=lambda x, y: False, bounds=(-64, 63, -64, 63))
    path = pf.find([0, 0], [3, 0])
    assert path and tuple(path[0]) == (1, 0)


def test_legacy_result_cannot_override_frozen_v014_genes():
    import json
    import tempfile
    from deploy import load_genes

    payload = {"history": [{"gen": 3, "best": 99.0, "genes": {
        "max_population": 19, "selfdestruct_pop": 19,
        "worker_ratio": 0.6, "obsolete_gene": 123}}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json") as fh:
        json.dump(payload, fh)
        fh.flush()
        genes = load_genes(fh.name)
    assert genes["max_population"] == 32
    assert genes["selfdestruct_pop"] == 999
    assert genes["worker_ratio"] == 0.6
    assert "obsolete_gene" not in genes



if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
