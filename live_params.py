"""实时调参覆盖层：地图网页 → stream/live_params.json → agent 每 tick 热覆盖 genes。

设计原则：
- 只做"覆盖"，绝不写进化结果文件(evolve_v7_best.json)；关闭滑块即回默认基因。
- 严格 clamp + 类型校验：任何越界/非法值被忽略或 clamp，绝不抛出导致 agent 崩溃。
- 读带 mtime 缓存（decide 每单位每 tick 调用，不能每次读盘）。

命名空间：
- "new"：新决策（arena-evolve / HeuristicStrategy）的参数
- "old"：旧决策（arena_agent.py）的参数（本期未接入，预留）
"""
import json
import os

_BASE = os.path.dirname(os.path.abspath(__file__))
_STREAM_DIR = os.path.join(_BASE, "stream")
_LIVE_PATH = os.path.join(_STREAM_DIR, "live_params.json")

# 命名空间 -> 参数名 -> (min, max)。越界值被 clamp。
SPECS = {
    "new": {
        "worker_ratio": (0.10, 0.90),        # 探索单位占比（工人是主力探索者）
        "patrol_radius": (3.0, 12.0),         # 战斗单位活动半径
        "revisit_ticks": (40.0, 400.0),       # 重复探索周期
        "beacon_go_range": (5.0, 45.0),       # 主动远征触发距离
        "defense_radius": (4.0, 20.0),        # 防御敏感度
        "idle_explore_radius": (0.0, 60.0),   # 探索距离上限（本次新增基因）
        "scout_search_limit": (10.0, 80.0),   # 探索搜索半径（本次新增基因）
    },
}

# 滑块默认值（与 evolve_v7_best.json 部署值一致），用于"恢复默认"。
DEFAULTS = {
    "new": {
        "worker_ratio": 0.5463523563273454,
        "patrol_radius": 7.5,
        "revisit_ticks": 243.23470250665162,
        "beacon_go_range": 15.172532787271086,
        "defense_radius": 11.0797481548136,
        "idle_explore_radius": 15.0,
        "scout_search_limit": 40.0,
    },
}


def _clamp_value(name, value, ns):
    spec = SPECS.get(ns, {}).get(name)
    if spec is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    lo, hi = spec
    if v < lo:
        v = lo
    elif v > hi:
        v = hi
    return v


def write_params(raw):
    """校验+clamp+原子写入。raw 形如 {"new": {...}, "old": {...}}。
    返回写入后的归一化数据（仅含合法参数）。非法结构也安全（忽略非法部分）。"""
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    for ns, params in raw.items():
        if ns not in SPECS:
            continue  # 未知命名空间忽略（便于扩展，不报错）
        if not isinstance(params, dict):
            continue
        cleaned = {}
        for name, value in params.items():
            v = _clamp_value(name, value, ns)
            if v is not None:
                cleaned[name] = v
        if cleaned:
            out[ns] = cleaned
    os.makedirs(_STREAM_DIR, exist_ok=True)
    tmp = _LIVE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _LIVE_PATH)  # 原子替换，避免 agent 读到半截文件
    return out


_cache = {"mtime": 0, "data": {}}


def get_params(namespace="new"):
    """读取该命名空间的覆盖参数（已 clamp）。文件不存在/损坏 → {}。带 mtime 缓存。"""
    global _cache
    try:
        mtime = os.path.getmtime(_LIVE_PATH)
    except OSError:
        _cache = {"mtime": 0, "data": {}}
        return {}
    if _cache.get("mtime") == mtime and namespace in _cache.get("data", {}):
        return dict(_cache["data"][namespace])
    data = {}
    try:
        with open(_LIVE_PATH) as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}
    norm = {}
    for ns, params in data.items():
        if ns not in SPECS or not isinstance(params, dict):
            continue
        for name, value in params.items():
            v = _clamp_value(name, value, ns)
            if v is not None:
                norm.setdefault(ns, {})[name] = v
    _cache = {"mtime": mtime, "data": norm}
    return dict(norm.get(namespace, {}))


def apply_to_genes(strat, namespace="new"):
    """把覆盖参数应用到策略对象的 genes（运行期热覆盖）。无合法参数则不动。"""
    params = get_params(namespace)
    if not params:
        return False
    for name, value in params.items():
        strat.genes[name] = value
    return True
