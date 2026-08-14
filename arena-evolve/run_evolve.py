"""进化入口：跑 N 代遗传算法，输出每代最佳基因与适应度。

用法：.venv/bin/python run_evolve.py [--generations N] [--pop N] [--workers N]
"""

import argparse
import json
import os
from pathlib import Path
import time

from evolve.ga import GA
from status import EVOLVE_STATUS, write_status
from strategies.heuristic import GENES, make_default_genes


class Reporter:
    """把进化进度写成状态快照，供 monitor.py 实时展示。"""

    def __init__(self, path, args, eval_seeds):
        self.path = path
        self.started = time.time()
        self.gen_seconds = []
        self.state = {
            "kind": "evolve", "phase": "baseline",
            "generation": 0, "generations": args.generations,
            "done": 0, "total": args.pop, "history": [], "baseline": None,
            "best_genes": None, "best_detail": None,
            "default_genes": make_default_genes(),
            "gene_bounds": {n: [lo, hi] for n, _d, lo, hi in GENES},
            "config": {"pop": args.pop, "players": args.players,
                       "seeds": list(eval_seeds), "max_ticks": args.max_ticks,
                       "workers": args.workers, "prescreen": args.prescreen,
                       "live": args.live, "multistage": args.multistage,
                       "mid_ticks": args.mid_ticks,
                       "risk_lambda": args.risk_lambda,
                       "holdout_seeds": args.holdout_seeds,
                       "seed_rollover": args.seed_rollover,
                       "long_check_ticks": args.long_check_ticks},
        }
        self.flush()

    def flush(self):
        s = self.state
        s["elapsed"] = time.time() - self.started
        # ETA：已完成代的均速 × 剩余代 + 当前代剩余部分
        if self.gen_seconds:
            per_gen = sum(self.gen_seconds) / len(self.gen_seconds)
            frac = (s["done"] / s["total"]) if s["total"] else 0
            remain = per_gen * (s["generations"] - s["generation"] - frac)
            s["eta"] = max(0.0, remain)
        else:
            s["eta"] = None
        write_status(self.path, s)

    def progress(self, done, total):
        self.state["done"] = done
        self.state["total"] = total
        self.flush()

    def generation_done(self, gen, best, avg, genes, detail, seconds):
        self.gen_seconds.append(seconds)
        s = self.state
        # history 附带每代冠军的 detail（harvested/deposited/res/...）供前端画曲线
        s["history"].append({"gen": gen, "best": best, "avg": avg,
                             "detail": detail or {}})
        if s["best_genes"] is None or best >= max(h["best"] for h in s["history"]):
            s["best_genes"] = genes
            s["best_detail"] = detail
        s["generation"] = gen + 1
        self.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generations", type=int, default=8,
                    help="0 = 无限循环（手动/收敛停止）")
    ap.add_argument("--patience", type=int, default=0,
                    help="连续 N 代 best 无提升则停止（0 = 不早停）")
    ap.add_argument("--stop-file", default="results/evolve_stop",
                    help="手动停止标记文件（无限模式下存在即优雅停止）")
    ap.add_argument("--pop", type=int, default=16)
    ap.add_argument("--workers", type=int, default=0,
                    help="并行进程数（0=自动：保留 2 个 CPU 核心）")
    ap.add_argument("--seeds", type=str, default="42,43,44,45")
    ap.add_argument("--seed-pool", type=str, default="",
                    help="滚动种子池（逗号分隔，配合 --seed-rollover；"
                         "默认 = eval_seeds，即不滚动）")
    ap.add_argument("--seed-rollover", type=int, default=0,
                    help="每 N 代从种子池滚动一批新种子（P0#13 过拟合缓解："
                         "固定种子集合会让基因只适应几张地图；滚动后代间 best "
                         "不可直接比，收敛看 holdout 分数）")
    ap.add_argument("--max-ticks", type=int, default=800)
    ap.add_argument("--players", type=int, default=8,
                    help="每局玩家数（被测基因 + 分层对手）。baseline 与进化"
                         "必须用同一个值，否则分数不可比")
    ap.add_argument("--prescreen", type=float, default=0.0,
                    help="预筛比例：先用 1 个种子跑全种群，仅前该比例的个体补齐"
                         "其余种子（0.5 约省 1.6x 时间，代价是偶尔漏掉一个好个体）")
    ap.add_argument("--risk-lambda", type=float, default=0.0,
                    help="风险调整：选择按 fitness - λ×跨种子std 排序"
                         "（>0 惩罚偶尔爆高经常崩盘的策略）")
    ap.add_argument("--holdout-seeds", type=str, default="",
                    help="独立验证种子（逗号分隔，不进选择压力）：每代冠军"
                         "用这些种子复评并记录，防记忆过拟合固定种子")
    ap.add_argument("--long-check-ticks", type=int, default=0,
                    help="每代冠军的长局复评时长（multistage 模式，固定种子）："
                         "看稳态指标（经济枯竭/侦察死区在 5k+ tick 才暴露，"
                         "进化压力内的短局看不到）；0 = 关闭")
    ap.add_argument("--long-check-seeds", type=str, default="77",
                    help="长局复评种子（默认 77，不进选择压力）")
    ap.add_argument("--live", action="store_true",
                    help="线上环境模式：老玩家带兵出生（发育差）+ 偏远环带")
    ap.add_argument("--multistage", action="store_true",
                    help="多阶段评估：0.5×短局(0→600) + "
                         "0.5×真实续局(600→600+mid-ticks)")
    ap.add_argument("--mid-ticks", type=int, default=600,
                    help="多阶段中局时长")
    ap.add_argument("--size", type=int, default=512,
                    help="live 模式世界大小（线上推断 4096+，取 768 更接近比例）")
    ap.add_argument("--out", type=str, default="evolve_results.json")
    ap.add_argument("--init-genes", default=None,
                    help="初始种群基因 JSON（warm start）：围绕该基因扰动生成"
                         "初始种群（代替默认基因——v7 解封基因是已知好起点，"
                         "省去前 5-8 代的爬升）")
    ap.add_argument("--status", default=EVOLVE_STATUS,
                    help="实时进度快照路径，供 monitor.py 读取；传空串关闭")
    args = ap.parse_args()
    if args.workers <= 0:
        args.workers = max(1, (os.cpu_count() or 2) - 2)

    eval_seeds = tuple(int(s) for s in args.seeds.split(","))
    seed_pool = tuple(int(s) for s in args.seed_pool.split(",")) \
        if args.seed_pool else eval_seeds
    os.makedirs("results", exist_ok=True)
    rep = Reporter(args.status, args, eval_seeds) if args.status else None

    ga = GA(pop_size=args.pop, workers=args.workers, eval_seeds=eval_seeds,
            max_ticks=args.max_ticks, seed=0, prescreen=args.prescreen,
            num_players=args.players, live=args.live, size=args.size,
            multistage=args.multistage, mid_ticks=args.mid_ticks,
            risk_lambda=args.risk_lambda,
            holdout_seeds=tuple(int(s) for s in args.holdout_seeds.split(","))
            if args.holdout_seeds else (),
            seed_pool=seed_pool, seed_rollover=args.seed_rollover,
            long_check_ticks=args.long_check_ticks,
            long_check_seeds=tuple(int(s) for s in
                                   args.long_check_seeds.split(",")),
            init_genes=(json.load(open(args.init_genes))
                        if args.init_genes else None),
            progress=rep.progress if rep else None)

    # 评估默认基因作为基线（必须与进化完全同配置：玩家数/种子/tick 数）
    print("=== 评估默认基因（baseline）===")
    t0 = time.time()
    base_fit, base_detail = ga.evaluate_genes_by_seed(
        make_default_genes(), eval_seeds)
    print(f"baseline fitness: {base_fit:.1f} detail: {base_detail}")
    print(f"  ({time.time()-t0:.1f}s)")
    if rep:
        rep.state["baseline"] = base_fit
        rep.state["baseline_detail"] = base_detail
        rep.state["phase"] = "evolving"
        rep.flush()

    ga.init_population()

    history = []
    stop_file = Path(args.stop_file)
    if stop_file.exists():
        stop_file.unlink()   # 启动时清旧标记
    gens = args.generations if args.generations > 0 else None
    gen = 0
    best_overall = -1e9
    best_overall_genes = None
    best_overall_gen = None
    best_overall_is_holdout = False
    stale = 0
    while gens is None or gen < gens:
        t0 = time.time()
        if rep:
            rep.state["generation"] = gen
            rep.state["done"] = 0
            rep.flush()
        best_fit, best_genes, avg = ga.evaluate(generation=gen, verbose=True)
        dt = time.time() - t0
        print(f"gen {gen}: best={best_fit:.1f} avg={avg:.1f} ({dt:.0f}s)")
        if ga.last_holdout:
            print(f"  [holdout] {ga.last_holdout[0]:.1f}")
        history.append({"gen": gen, "best": best_fit, "avg": avg,
                        "holdout": ga.last_holdout[0] if ga.last_holdout
                        else None,
                        "seeds": list(ga.eval_seeds),
                        "genes": best_genes})
        # 跨代比较/落盘/早停的分数：滚动种子模式下代间 best 不可比
        # （各代种子不同），必须用 holdout（固定种子纵向可比）；
        # 未配置 holdout 时退回代内 best（旧行为）
        # A holdout score is the deployment selection metric whenever it is
        # configured, regardless of whether seed rollover is also enabled.
        # Using training ``best`` here made the checkpoint and final JSON pick
        # a different genome than the champion printed at shutdown.
        cmp_fit = ga.last_holdout[0] if ga.last_holdout else best_fit
        # 最佳基因持续落盘（无限模式随时可取当前 best 部署）
        if cmp_fit > best_overall:
            best_overall = cmp_fit
            best_overall_genes = dict(best_genes)
            best_overall_gen = gen
            best_overall_is_holdout = ga.last_holdout is not None
            stale = 0
            _out_p = args.out if args.out.startswith("results/") \
                else os.path.join("results", args.out)
            os.makedirs(os.path.dirname(_out_p), exist_ok=True)
            with open(_out_p, "w") as f:
                json.dump({"baseline_fitness": base_fit,
                           "best": best_overall, "gen": gen,
                           "best_is_holdout": best_overall_is_holdout,
                           "best_gen": best_overall_gen,
                           "best_genes": best_overall_genes,
                           "best_metric": best_overall,
                           "history": history[-200:]}, f, indent=2,
                          ensure_ascii=False)
        else:
            stale += 1
        if rep:
            best_idx = max(range(len(ga.fitness)), key=lambda i: ga.fitness[i])
            rep.generation_done(gen, best_fit, avg, best_genes,
                                ga.last_details[best_idx] if ga.last_details else None,
                                dt)
        # 停止条件：固定代数完成 / 收敛早停 / 手动停止文件
        if gens is not None and gen >= gens - 1:
            break
        if args.patience and stale >= args.patience:
            print(f"收敛早停：连续 {stale} 代无提升")
            break
        if stop_file.exists():
            print("手动停止：检测到停止标记")
            break
        ga.next_generation()
        gen += 1
    ga.close()
    if rep:
        rep.state["phase"] = "done"
        rep.flush()

    # 输出最佳（滚动种子模式下按 holdout 选——代内 best 跨代不可比）
    if any(h.get("holdout") is not None for h in history):
        final_best = max(history, key=lambda h: h.get("holdout") or -1e9)
        print("(按 holdout 分数选择冠军)")
    else:
        final_best = max(history, key=lambda h: h["best"])
    print("\n=== 最佳基因 ===")
    for name, val in final_best["genes"].items():
        print(f"  {name}: {val:.3f}")
    out_path = args.out if args.out.startswith("results/") \
        else os.path.join("results", args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    selection_metric = ("holdout" if any(h.get("holdout") is not None
                                         for h in history) else "best")
    final_metric = final_best.get(selection_metric, final_best.get("best"))
    with open(out_path, "w") as f:
        json.dump({"schema_version": 2,
                   "rules_version": "v0.14",
                   "sdk_version": "0.2.9",
                   "baseline_fitness": base_fit,
                   "best": final_metric,
                   "best_gen": final_best["gen"],
                   "best_genes": final_best["genes"],
                   "best_metric": final_metric,
                   "selection_metric": selection_metric,
                   "best_is_holdout": selection_metric == "holdout",
                   "history": history},
                  f, indent=2, ensure_ascii=False)
    print(f"结果已保存: {out_path}")


if __name__ == "__main__":
    main()
