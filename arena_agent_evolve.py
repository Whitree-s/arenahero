"""Arena Hero 新决策入口：保留地图层，换用 arena-evolve 的进化策略。

  - 决策算法：arena-evolve/strategies/heuristic.py 的 HeuristicStrategy（27 基因，
    经遗传算法实证调参），通过 LiveAdapter 接入官方 SDK 或 ahsim 本地引擎。
  - 地图/可视化层：完全复用现有 serve_map.py + monitor.html + stream/ 格式，
    本文件只做「Observation+plan → stream 帧」的桥接（见 bridge_stream.StreamWriter）。

用法：
  本地试运行（不连正式世界，无需 API key，用于验证桥接 + monitor 渲染）：
      python arena_agent_evolve.py --local --genes arena-evolve/genes/evolve_v7_best.json --ticks 200

  正式世界部署：
      ARENA_HERO_API_KEY=xxx python arena_agent_evolve.py --genes arena-evolve/genes/evolve_v7_best.json
"""

import argparse
import json
import os
import sys
import time

# arena-evolve 子模块路径（目录名含连字符，不能直接 import，手动加搜索路径）
HERE = os.path.dirname(os.path.abspath(__file__))
EVOLVE_DIR = os.path.join(HERE, "arena-evolve")
sys.path.insert(0, EVOLVE_DIR)

from deploy import LiveAdapter, load_genes          # noqa: E402
from strategies.heuristic import HeuristicStrategy   # noqa: E402
from bridge_stream import StreamWriter               # noqa: E402

GENES_DEFAULT = os.path.join(EVOLVE_DIR, "genes", "evolve_v7_best.json")


def run_online(api_key, genes, stream_dir="stream", tick_limit=None, verbose=False):
    """正式世界：复用 LiveAdapter 主循环，仅把写状态换成写 stream 帧。"""
    from arena_hero import ArenaHeroClient
    strat = HeuristicStrategy(genes=genes, bounds=None)
    adapter = LiveAdapter(strat)
    writer = StreamWriter(stream_dir=stream_dir)
    # 把旧决策/上一局积累的地图注入 Memory（共享数据，切换不丢地图）
    writer.seed(strat)
    last_save_tick = 0
    # 优雅停机：保存记忆
    import signal

    def _on_exit(signum, _frame):
        print(f"[evolve] 收到信号 {signum}，退出", flush=True)
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _on_exit)
        signal.signal(signal.SIGINT, _on_exit)
    except (ValueError, OSError):
        pass

    while True:
        try:
            with ArenaHeroClient(api_key=api_key) as game:
                for turn in game.turns():
                    if tick_limit and turn.tick > tick_limit:
                        print(f"[evolve] 达到 tick 上限 {tick_limit}，退出", flush=True)
                        return
                    adapter.detect_gap(turn)
                    obs = adapter.build_observation(turn)
                    plan = None
                    try:
                        plan = strat.decide(obs)
                        adapter.apply_plan(turn, plan)
                        t_sub = time.time()
                        turn.submit()
                        adapter.submit_ms = (time.time() - t_sub) * 1000
                    except Exception as exc:
                        adapter._sent_dirs.clear()
                        print(f"[evolve] tick {turn.tick} 处理失败: {exc}", flush=True)
                    # 桥接：写 monitor 兼容帧（旁路，绝不影响对局）
                    try:
                        writer.write(obs, plan, strat, tick=turn.tick)
                    except Exception as exc:
                        print(f"[evolve] 写流失败（忽略）: {exc}", flush=True)
                    if verbose and turn.tick % 25 == 0:
                        print(f"[evolve] tick {turn.tick}: pop={obs.population} "
                              f"res={obs.core['resources'] if obs.core else 0} "
                              f"reconnects={adapter.reconnects}", flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            cause = getattr(exc, "__cause__", None)
            msg = f"{exc} | 底层: {cause}" if cause else f"{exc}，10s 后重试"
            print(f"[evolve] 连接层异常: {msg}", flush=True)
            time.sleep(10)
            continue
        return


def run_local(genes, stream_dir="stream", ticks=200, seed=42, players=8,
              tick_delay=0.0, verbose=True):
    """本地模拟器：同一基因跑完整对局，写 stream 帧供 monitor 离线验证。"""
    from ahsim.game import Game
    from strategies.randombot import RandomBot

    class _Recording:
        def __init__(self, inner):
            self.inner = inner
            self.last_plan = None
        def decide(self, obs):
            self.last_plan = self.inner.decide(obs)
            return self.last_plan
        def __getattr__(self, k):
            return getattr(self.inner, k)

    bounds = (-128, 127, -128, 127)
    me = _Recording(HeuristicStrategy(genes=genes, bounds=bounds))
    strategies = {0: me}
    for i in range(1, players):
        strategies[i] = (RandomBot(seed=i * 7 + 1) if i % 3 == 0
                         else HeuristicStrategy(bounds=bounds))
    g = Game(strategies=strategies, size=256, seed=seed, max_ticks=ticks)
    writer = StreamWriter(stream_dir=stream_dir)
    writer.seed(me.inner)  # 本地验证也注入共享地图（若有）
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
        try:
            writer.write(obs, me.last_plan, me.inner, tick=g.tick)
        except Exception as exc:
            print(f"[evolve] 写流失败: {exc}", flush=True)
        if tick_delay:
            time.sleep(tick_delay)
        if verbose and g.tick % 25 == 0:
            st = g.results()[0]
            print(f"[local] tick {g.tick}: pop={st['final_population']} "
                  f"harvest={st['harvested']} dmg={st['damage_dealt']} "
                  f"alive={st['alive']}", flush=True)

    st = g.results()[0]
    print(f"[local] 结束 {ticks} ticks: pop={st['final_population']} "
          f"harvest={st['harvested']} dmg={st['damage_dealt']} alive={st['alive']}",
          flush=True)


def main():
    ap = argparse.ArgumentParser(description="Arena Hero（arena-evolve 策略 + 现有地图层）")
    ap.add_argument("--genes", default=GENES_DEFAULT, help="基因 JSON 路径")
    ap.add_argument("--local", action="store_true", help="本地模拟试运行（无需 API key）")
    ap.add_argument("--ticks", type=int, default=200, help="本地运行 tick 数")
    ap.add_argument("--players", type=int, default=8, help="本地玩家数")
    ap.add_argument("--tick-delay", type=float, default=0.0, help="本地每 tick 停顿秒数")
    ap.add_argument("--stream-dir", default="stream", help="stream 输出目录")
    ap.add_argument("--tick-limit", type=int, default=None, help="正式世界运行到该 tick 退出")
    ap.add_argument("--api-key", default=os.environ.get("ARENA_HERO_API_KEY", ""))
    ap.add_argument("--verbose", action="store_true", help="每 25 tick 打印诊断")
    args = ap.parse_args()

    genes = load_genes(args.genes)
    if args.local:
        run_local(genes, stream_dir=args.stream_dir, ticks=args.ticks,
                  players=args.players, tick_delay=args.tick_delay, verbose=args.verbose)
        return
    if not args.api_key:
        print("缺少 API key：设置 ARENA_HERO_API_KEY 环境变量（launchd 进程不继承交互 shell "
              "环境，请在 plist 的 EnvironmentVariables 写入，或先执行 "
              "`launchctl setenv ARENA_HERO_API_KEY xxx`）。本进程 30s 后退出，"
              "KeepAlive 会自动重试——配好 key 后会自动连上官方世界。",
              flush=True)
        time.sleep(30)
        sys.exit(1)
    run_online(args.api_key, genes, stream_dir=args.stream_dir,
               tick_limit=args.tick_limit, verbose=args.verbose)


if __name__ == "__main__":
    main()
