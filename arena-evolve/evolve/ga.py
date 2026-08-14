"""遗传算法：进化启发式策略的基因参数。

流程：
1. 初始化种群（默认基因 ± 随机扰动）
2. 每代：并行评估所有个体（多种子对局）
3. 锦标赛选择 → 均匀交叉 → 高斯变异 → 生成下一代
4. 精英保留（最优个体原样进入下一代）
"""

import random
from multiprocessing import Pool

from strategies.heuristic import GENES, make_default_genes

from .fitness import evaluate_individual, evaluate_live, evaluate_multistage, fitness_from_detail, combine_details


def gene_bounds():
    return {name: (lo, hi) for name, _d, lo, hi in GENES}


def _eval_job(job):
    """Pool worker 入口：带序号，乱序回收后能归位（imap_unordered 才能报进度）。"""
    idx, args = job
    return idx, evaluate_individual(*args)


def _eval_live_job(job):
    idx, args = job
    return idx, evaluate_live(*args)


def _eval_multi_job(job):
    idx, args = job
    return idx, evaluate_multistage(*args)


class GA:
    def __init__(self, pop_size=24, elites=2, tournament=3, mut_sigma=0.12,
                 crossover=0.8, seed=0, num_players=8, max_ticks=800,
                 eval_seeds=(42, 43), workers=4, prescreen=0.0, progress=None,
                 live=False, size=512, multistage=False, mid_ticks=600,
                 risk_lambda=0.0, holdout_seeds=(), seed_pool=(),
                 seed_rollover=0, long_check_ticks=0,
                 long_check_seeds=(77,), init_genes=None):
        """risk_lambda: 风险调整选择权重（P0#12）。>0 时按
        fitness - risk_lambda × 跨种子 std 排序——惩罚“偶尔爆高、经常
        崩盘”的策略。0 = 纯均值（旧行为）。
        holdout_seeds: 独立验证种子（P0#13）：每代冠军用这些种子复评，
        记录 last_holdout/avg_holdout——不参与选择，防固定种子记忆过拟合。
        seed_pool/seed_rollover: 滚动种子（P0#13 完整实现）：每 rollover 代
        从 pool 换一批 eval_seeds 数量级的新种子，让选择压力持续面对新地图；
        滚动后代间 best 不可直接比（纵向指标看 holdout），且评估缓存按
        (基因, seeds) 键控，换批时清空。"""
        self.live = live
        self.multistage = multistage
        self.mid_ticks = mid_ticks
        self.size = size
        self.risk_lambda = risk_lambda
        self.holdout_seeds = tuple(holdout_seeds)
        self.last_holdout = None    # 最近一代冠军的 holdout (fitness, detail)
        self.seed_pool = tuple(seed_pool) or tuple(eval_seeds)
        self.seed_rollover = seed_rollover
        self.long_check_ticks = long_check_ticks
        self.long_check_seeds = tuple(long_check_seeds)
        self.last_long_check = None   # 最近冠军的长局复评 (fitness, detail)
        self.init_genes = init_genes  # warm start：初始种群围绕该基因扰动
        self.pop_size = pop_size
        self.elites = elites
        self.tournament = tournament
        self.mut_sigma = mut_sigma
        self.crossover = crossover
        self.rng = random.Random(seed)
        self.num_players = num_players
        self.max_ticks = max_ticks
        self.eval_seeds = tuple(eval_seeds)
        self.workers = workers
        # 预筛：先用 1 个种子跑全种群，只有排名靠前的这个比例才补齐其余种子。
        # 0 = 关闭（每个个体都跑满全部种子）。
        self.prescreen = prescreen
        self.bounds = gene_bounds()
        self.pop = None
        self.fitness = None
        self.last_details = None   # 最近一次评估的各项原始指标（供监控展示）
        self._pool = None
        self._eval_cache = {}   # (基因指纹, seeds) -> (fitness, detail)
        # progress(done, total)：每有一个个体评估完就回调一次，供实时监控用
        self.progress = progress
        self._done = 0
        self._total = 0

    # ------------------------------------------------------------------
    def init_population(self):
        default = make_default_genes()
        if self.init_genes:
            # warm start：围绕给定基因小扰动（v7 解封基因是已知好起点，
            # 省去前几代从默认基因爬升的过程）
            base = dict(default)
            base.update(self.init_genes)
            self.pop = [self._mutate(dict(base), 0.15) for _ in range(self.pop_size)]
        else:
            self.pop = [self._mutate(dict(default), 0.25) for _ in range(self.pop_size)]
        self.fitness = [None] * self.pop_size

    def _mutate(self, genes, sigma_mult=1.0):
        for name, _d, lo, hi in GENES:
            if self.rng.random() < 0.4:
                sigma = (hi - lo) * self.mut_sigma * sigma_mult
                genes[name] = min(hi, max(lo, genes[name] + self.rng.gauss(0, sigma)))
        return genes

    # ------------------------------------------------------------------
    # 评估调度：进程池常驻 + 基因指纹缓存 + 可选预筛
    # ------------------------------------------------------------------
    def _get_pool(self):
        if self._pool is None:
            self._pool = Pool(self.workers)
        return self._pool

    def close(self):
        """释放常驻进程池（跑完一次进化后调用）。"""
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None

    @staticmethod
    def _genome_key(genes):
        return tuple(round(genes[name], 6) for name, _d, _lo, _hi in GENES)

    def _tick_progress(self, n=1):
        self._done += n
        if self.progress is not None:
            try:
                self.progress(self._done, self._total)
            except Exception:
                pass    # 监控回调不能影响进化

    def _run(self, items, *, track_progress=True, mid_ticks=None,
             w_early=0.5, w_mid=0.5, cache_tag=None):
        """items: [(idx, genes, seeds)] -> {idx: (fitness, detail)}。

        相同基因 + 相同种子的评估结果直接复用（精英每代原样保留，否则会白跑）。
        """
        out = {}
        jobs, order = [], []
        for idx, genes, seeds in items:
            base_key = (self._genome_key(genes), seeds)
            ck = base_key if cache_tag is None else (cache_tag,) + base_key
            hit = self._eval_cache.get(ck)
            if hit is not None:
                out[idx] = hit
                if track_progress:
                    self._tick_progress()
                continue
            if self.multistage:
                # 位置参数必须与 evaluate_multistage 签名严格对齐
                # （2026-08-07 修复：此前漏传 mid_ticks，个体用默认 600 而
                 # baseline 用 --mid-ticks，评估口径不一致）
                jobs.append((genes, seeds, self.num_players, 0, self.size,
                             (-96, 128), w_early, w_mid,
                             self.mid_ticks if mid_ticks is None else mid_ticks))
            elif self.live:
                jobs.append((genes, self.num_players, self.max_ticks, seeds, 0,
                            self.size, (-96, 128)))
            else:
                jobs.append((genes, self.num_players, self.max_ticks, seeds, 0))
            order.append((idx, ck))
        if jobs:
            res = [None] * len(jobs)
            fn = _eval_multi_job if self.multistage else (
                _eval_live_job if self.live else _eval_job)
            run_fn = evaluate_multistage if self.multistage else (
                evaluate_live if self.live else evaluate_individual)
            if self.workers > 1 and len(jobs) > 1:
                # imap_unordered：谁先算完先回收，进度才能实时刷
                for i, r in self._get_pool().imap_unordered(
                        fn, list(enumerate(jobs))):
                    res[i] = r
                    if track_progress:
                        self._tick_progress()
            else:
                for i, j in enumerate(jobs):
                    res[i] = run_fn(*j)
                    if track_progress:
                        self._tick_progress()
            for (idx, ck), r in zip(order, res):
                self._eval_cache[ck] = r
                out[idx] = r
        return out

    def evaluate_genes_by_seed(self, genes, seeds=None):
        """评估一个基因，并把每个 seed 作为独立任务提交到常驻进程池。

        训练阶段通常以“一个个体 = 一个多 seed 任务”为单位，避免额外的
        合并开销。Holdout/基线/稳态复评只有一个个体，拆 seed 后才能利用
        全部 workers；单 seed缓存也能跨代复用不变的 champion 复评结果。
        """
        seeds = tuple(self.eval_seeds if seeds is None else seeds)
        if not seeds:
            raise ValueError("at least one evaluation seed is required")
        got = self._run([(i, genes, (seed,))
                         for i, seed in enumerate(seeds)],
                        track_progress=False)
        parts = [got[i] for i in range(len(seeds))]
        fitness = sum(f for f, _d in parts) / len(parts)
        detail = combine_details([(d, 1, f) for f, d in parts])
        return fitness, detail

    def evaluate_long_check_by_seed(self, genes, seeds, mid_ticks):
        """长局稳态复评的并行版本（与训练缓存隔离）。"""
        seeds = tuple(seeds)
        if not seeds:
            raise ValueError("at least one long-check seed is required")
        tag = ("long-check", int(mid_ticks))
        got = self._run([(i, genes, (seed,))
                         for i, seed in enumerate(seeds)],
                        track_progress=False, mid_ticks=mid_ticks,
                        w_early=0.0, w_mid=1.0, cache_tag=tag)
        parts = [got[i] for i in range(len(seeds))]
        fitness = sum(f for f, _d in parts) / len(parts)
        detail = combine_details([(d, 1, f) for f, d in parts])
        return fitness, detail

    def _evaluate_all(self):
        """返回 [(fitness, detail)]，顺序与 self.pop 一致。"""
        seeds = self.eval_seeds
        use_prescreen = bool(self.prescreen and len(seeds) > 1 and self.pop_size > 2)
        keep = max(2, int(round(self.pop_size * self.prescreen))) if use_prescreen else 0
        self._done = 0
        self._total = self.pop_size + keep
        if not use_prescreen:
            got = self._run([(i, g, seeds) for i, g in enumerate(self.pop)])
            return [got[i] for i in range(self.pop_size)]
        # 预筛：全员先跑第一个种子，只有前 prescreen 比例的个体补齐其余种子。
        # 补齐后按种子数加权合并，幸存者的分数与跑满全部种子完全一致。
        # 淘汰者分数置 -inf（P0#14 口径统一）：单 seed 分数与多 seed 均值
        # 方差不同，直接混比会让锦标赛选到“单局运气好”的个体；-inf 保证
        # 淘汰者永远不被选择/成为精英（best/avg 计算也会过滤）。
        first, rest = seeds[:1], seeds[1:]
        stage1 = self._run([(i, g, first) for i, g in enumerate(self.pop)])
        survivors = sorted(range(self.pop_size),
                           key=lambda i: -stage1[i][0])[:keep]
        stage2 = self._run([(i, self.pop[i], rest) for i in survivors])
        results = [None] * self.pop_size
        for i in range(self.pop_size):
            if i in survivors:
                detail = combine_details([
                    (stage1[i][1], len(first), stage1[i][0]),
                    (stage2[i][1], len(rest), stage2[i][0]),
                ])
                # multistage 的 detail 是 early_/mid_ 前缀结构（无裸 harvested
                # 键），fitness_from_detail 会 KeyError——直接用它已算好的总分；
                # 非 multistage 的 detail 是聚合口径，走 fitness_from_detail
                # （2026-08-07 prescreen 崩溃修复）
                if self.multistage:
                    fit = detail.get("total_fitness")
                    if fit is None:
                        fit = fitness_from_detail(detail, self.max_ticks)
                else:
                    fit = fitness_from_detail(detail, self.max_ticks)
                results[i] = (fit, detail)
            else:
                results[i] = (float("-inf"), stage1[i][1])
        return results

    # ------------------------------------------------------------------
    def evaluate(self, generation=0, verbose=False):
        """并行评估整个种群。返回 (best_fitness, best_genes, avg_fitness)。"""
        if self.seed_rollover > 0 and len(self.seed_pool) > len(self.eval_seeds):
            # 滚动种子：每 seed_rollover 代从池子取下一批（环绕）
            batch = (generation // self.seed_rollover) * len(self.eval_seeds)
            active = tuple(sorted(
                self.seed_pool[(batch + i) % len(self.seed_pool)]
                for i in range(len(self.eval_seeds))))
            if active != self.eval_seeds:
                self.eval_seeds = active
                self._eval_cache.clear()   # 旧种子条目全部失效
                if verbose:
                    print(f"  [seeds] 滚动到 {self.eval_seeds}")
        results = self._evaluate_all()
        self.fitness = [r[0] for r in results]
        self.last_details = [r[1] for r in results]
        if self.risk_lambda:
            # 风险调整：均值 - λ × 跨种子 std（P0#12）
            self.fitness = [
                (f if f == float("-inf") else
                 f - self.risk_lambda * d.get("fitness_std", 0.0))
                for f, d in zip(self.fitness, self.last_details)]
        valid = [f for f in self.fitness if f != float("-inf")]
        best_idx = max(range(len(self.fitness)),
                       key=lambda i: self.fitness[i])
        avg = sum(valid) / len(valid) if valid else 0.0
        # 独立验证（P0#13）：冠军用 holdout 种子复评，不参与选择
        self.last_holdout = None
        if self.holdout_seeds:
            try:
                best_genes = self.pop[best_idx]
                fh, dh = self.evaluate_genes_by_seed(
                    best_genes, self.holdout_seeds)
                self.last_holdout = (fh, dh)
            except Exception as exc:
                print(f"  [holdout] 评估失败: {exc}")
        # 长局复评（稳态确认，不进选择压力）：冠军用 long_check_ticks 的
        # 续局跑固定种子，看稳态指标（harvested/pop 趋势）——1200 tick
        # 的评估看不到 5k+ tick 才暴露的经济枯竭/侦察死区
        self.last_long_check = None
        if self.long_check_ticks > 0 and self.multistage:
            try:
                fh, dh = self.evaluate_long_check_by_seed(
                    self.pop[best_idx], self.long_check_seeds,
                    self.long_check_ticks)
                self.last_long_check = (fh, dh)
            except Exception as exc:
                print(f"  [long-check] 评估失败: {exc}")
        if verbose:
            print(f"  gen {generation}: best={self.fitness[best_idx]:.1f} "
                  f"avg={avg:.1f} best_detail={results[best_idx][1]}")
            if self.last_holdout:
                fh = self.last_holdout[0]
                print(f"  [holdout] best 独立种子复评: {fh:.1f}")
            if self.last_long_check:
                fh, dh = self.last_long_check
                print(f"  [long-check] 冠军 {self.long_check_ticks}t 稳态: "
                      f"fitness {fh:.1f} | 续局 harvested {dh['mid_harvested']:.1f} "
                      f"deposited {dh['mid_deposited']:.1f} pop {dh['mid_pop']:.1f} "
                      f"res {dh['mid_res']:.1f}")
        return self.fitness[best_idx], self.pop[best_idx], avg

    # ------------------------------------------------------------------
    def next_generation(self):
        """选择/交叉/变异生成下一代（精英保留）。"""
        order = sorted(range(len(self.fitness)), key=lambda i: -self.fitness[i])
        new_pop = [dict(self.pop[i]) for i in order[:self.elites]]
        while len(new_pop) < self.pop_size:
            p1 = self._tournament()
            p2 = self._tournament()
            child = dict(p1)
            if self.rng.random() < self.crossover:
                for name in self.bounds:
                    if self.rng.random() < 0.5:
                        child[name] = p2[name]
            child = self._mutate(child)
            new_pop.append(child)
        self.pop = new_pop
        self.fitness = [None] * self.pop_size

    def _tournament(self):
        best = None
        for _ in range(self.tournament):
            i = self.rng.randrange(self.pop_size)
            if best is None or self.fitness[i] > best[1]:
                best = (self.pop[i], self.fitness[i])
        return best[0]
