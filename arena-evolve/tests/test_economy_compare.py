"""对比测试：原始策略 vs 修复版（经济死锁修复验证，多场景）。

用法：.venv/bin/python tests/test_economy_compare.py [--ticks N] [--players N] [--center X,Y]

场景：
- 默认：spawn_center=(0,0) 中央富矿区（模拟器原配置）
- --center -96,128：线上 chunk ring 6 同类出生区（quota 9）
- --players 10：增加竞争玩家（更多 bot 抢资源）
"""
import argparse
import importlib.util
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ahsim.game import Game
from evolve.fitness import _make_strategy, BOUNDS


def load_module(name, path, base_mod=None):
    # 包内相对导入 → 绝对导入（测试脚本从包外加载）
    src = open(path).read()
    if base_mod:
        src = src.replace("from .base import", f"from strategies.{base_mod} import")
    tmp = "/tmp/" + name + "_mod.py"
    with open(tmp, "w") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location(name, tmp)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run_once(strategy_cls, genes, seed, max_ticks=600, players=6,
             spawn_center=(0, 0), size=256, rivals="mix"):
    half = size // 2
    bounds = (-half, half - 1, -half, half - 1)
    strategies = {}
    for i in range(players):
        if i == 0:
            strategies[i] = strategy_cls(genes=genes, bounds=bounds)
        elif rivals == "random":
            from strategies.randombot import RandomBot
            strategies[i] = RandomBot(seed=i * 7 + 1)
        else:
            strategies[i] = _make_strategy(None, i, bounds=bounds)
    g = Game(strategies=strategies, size=size, seed=seed, max_ticks=max_ticks,
             spawn_center=spawn_center)
    g.run()
    return g.results()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=600)
    ap.add_argument("--players", type=int, default=6)
    ap.add_argument("--center", default="0,0")
    ap.add_argument("--seeds", default="42,43,44,45")
    ap.add_argument("--size", type=int, default=256)
    ap.add_argument("--rivals", default="mix",
                    help="mix=分层对手 / random=全 RandomBot（纯经济验证）")
    args = ap.parse_args()
    cx, cy = map(int, args.center.split(","))
    spawn_center = (cx, cy)
    seeds = [int(s) for s in args.seeds.split(",")]

    genes = json.load(open("/tmp/evolve_v4_best.json"))
    orig = load_module("orig_heuristic", "strategies/_orig_heuristic.py",
                       base_mod="_orig_base")
    fixed = load_module("fixed_heuristic", "strategies/heuristic.py",
                        base_mod="base")

    print(f"场景: center={spawn_center} players={args.players} "
          f"ticks={args.ticks} size={args.size} rivals={args.rivals} seeds={seeds}")
    print(f"{'seed':>5} {'版本':>6} {'存活':>4} {'harvest':>8} {'deposit':>8} "
          f"{'res':>4} {'pop':>4} {'lost':>5} {'fitness':>8}")
    agg = {"orig": [0, 0, 0, 0, 0, 0], "fixed": [0, 0, 0, 0, 0, 0]}
    for seed in seeds:
        for name, mod in (("orig", orig), ("fixed", fixed)):
            st = run_once(mod.HeuristicStrategy, genes, seed,
                          max_ticks=args.ticks, players=args.players,
                          spawn_center=spawn_center, size=args.size,
                          rivals=args.rivals)
            fit = st["harvested"] + st["damage_dealt"] * 0.5 \
                + st["final_population"] * 3 + st["final_resources"] * 2
            print(f"{seed:>5} {name:>6} {str(st['alive']):>4} "
                  f"{st['harvested']:>8} {st.get('deposited', 0):>8} "
                  f"{st['final_resources']:>4} {st['final_population']:>4} "
                  f"{st['units_lost']:>5} {fit:>8.1f}")
            for i, k in enumerate(("harvested", "deposited", "res", "pop",
                                   "lost", "fit")):
                v = {"harvested": st["harvested"], "deposited": st.get("deposited", 0),
                     "res": st["final_resources"], "pop": st["final_population"],
                     "lost": st["units_lost"], "fit": fit}[k]
                agg[name][i] += v
    print("\n=== 合计（%d seeds）===" % len(seeds))
    for name in ("orig", "fixed"):
        a = agg[name]
        print(f"{name:>5}: harvested={a[0]} deposited={a[1]} res={a[2]} "
              f"pop={a[3]} lost={a[4]} fit={a[5]:.1f}")


if __name__ == "__main__":
    main()
