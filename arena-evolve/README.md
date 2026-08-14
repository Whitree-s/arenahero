# arena-evolve

Arena Hero 自演进 Agent 的研究与部署仓库：确定性模拟器、参数化启发式策略、
遗传算法进化，以及接入正式世界的部署适配器。

> **免责声明**：本项目为社区项目，非 Arena Hero 官方产品，与官方无隶属关系。
> 使用本项目时，请自行遵守 [Arena Hero](https://doc.arenahero.io/) 的服务条款。

## 项目元数据

> 机器可读摘要，供 AI 工具与自动化流程解析。

```yaml
name: arena-evolve
description: 自演进游戏 Agent —— 确定性模拟器 + 进化策略 + 正式世界部署
status: "community project, not an official Arena Hero product"
language:
  core: "Python >= 3.10，仅标准库，零第三方运行时依赖"
  deploy: "Python >= 3.11，依赖官方 arena-hero SDK (== 0.2.9)"
license: MIT
entrypoints:
  - run_evolve.py    # 遗传算法进化入口，输出最优基因（JSON）
  - deploy.py        # 正式世界部署适配器，加载基因接入官方 SDK
modules:
  ahsim:     确定性规则引擎，对齐官方 gameplay rules v0.14
  strategies: 参数化启发式策略（27 个可进化基因）与基准对手
  evolve:    遗传算法（锦标赛选择 / 均匀交叉 / 高斯变异 / 精英保留）
  genes:     部署用基因文件（evolve_v7_best.json）
  tests:     单元测试（71 项，pytest）
data_formats:
  evolve_results.json: 进化输出，含 baseline_fitness / best / best_genes / history
  genes/*.json:        基因文件，{"name": value, ...} 形式的 27 个策略参数
links:
  official_docs: "https://doc.arenahero.io/"
  official_sdk: "https://pypi.org/project/arena-hero/"
  official_skill: "https://github.com/arena-hero/arena-hero-skill"
related_projects:
  arena-hero-agent: "https://github.com/Drew-Z/arena-hero-agent"
  arena-crazy-attack: "https://github.com/VelvetEvening/Arena-Crazy-Attack"
  arenahero-nearly-perfect-guide: "https://github.com/VelvetEvening/ArenaHero-nearly-perfect-guide"
community:
  linux_do: "https://linux.do/"
```

## 架构

### 模块职责

| 模块 | 职责 | 依赖 |
|---|---|---|
| `ahsim/` | 确定性游戏引擎（世界生成、视野、解析、循环） | 标准库 |
| `strategies/` | 策略接口、参数化启发式策略、基准对手 | `ahsim` |
| `evolve/` | 适应度评估与遗传算法 | `ahsim`, `strategies` |
| `run_evolve.py` | 进化入口（多进程评估） | `evolve` |
| `deploy.py` | 正式世界部署适配器 | `ahsim`, `strategies`, `arena-hero` |
| `tests/` | 引擎与进化单元测试 | pytest |

### 文件索引

| 文件 | 说明 |
|---|---|
| `ahsim/config.py` | 游戏常量（与官方文档逐条核对） |
| `ahsim/world.py` | 世界生成（双向坐标、主连通分量、chunk 配额资源层） |
| `ahsim/vision.py` | supercover 视野与 Ranger 射击线 |
| `ahsim/entities.py` | Core / Unit / Player |
| `ahsim/engine.py` | 解析顺序（移动依赖图、战斗快照、动态生产、Beacon、重生） |
| `ahsim/game.py` | 游戏循环（观察构建 → 策略决策 → 解析） |
| `ahsim/observation.py` | 策略观察（仅可见信息，与正式世界 state 一致） |
| `strategies/base.py` | Strategy 接口、A* 寻路（版本化缓存）、视野记忆 |
| `strategies/heuristic.py` | 参数化启发式策略（27 个基因，GENES 声明边界） |
| `strategies/randombot.py` | 基准对手 |
| `evolve/fitness.py` | 适应度评估（N 玩家 FFA + 分层对手） |
| `evolve/ga.py` | 遗传算法主循环 |
| `run_evolve.py` | 进化入口与 CLI 参数 |
| `deploy.py` | 部署适配器与 CLI 参数 |
| `requirements.txt` | 部署依赖（arena-hero==0.2.9） |
| `docs/STRATEGY.md` | 策略行为与进化评估配置说明 |
| `docs/RULES-ALIGNMENT.md` | 模拟器与官方规则的保真度核对记录 |

## 环境要求

| 组件 | 版本 | 用途 |
|---|---|---|
| Python（核心） | >= 3.10 | 模拟器 / 进化 / 策略 |
| Python（部署） | >= 3.11 | `deploy.py`（arena-hero SDK 要求） |
| arena-hero SDK | == 0.2.9 | 仅部署需要 |
| git | 任意 | 获取代码 |

## 快速上手

以下步骤按顺序执行。分为两条路径：**体验路径**（第 2、3 步，无需任何凭证）
与**部署路径**（第 4 步，需要 Arena Hero API key）。

### 1. 获取代码

```bash
git clone https://github.com/Torther/arena-evolve.git arena-evolve
cd arena-evolve
```

### 2. 运行进化实验（无需凭证）

```bash
python3 run_evolve.py --generations 2 --pop 8 --seeds 42,43 --max-ticks 400
```

预期输出：末行 `结果已保存: results/evolve_results.json`。

查看进化结果：

```bash
python3 -c "import json; d=json.load(open('results/evolve_results.json')); print('best fitness:', round(d['best'],1), '| 基因数:', len(d['best_genes']))"
```

完整 8 代进化（默认参数）在普通笔记本上约需数分钟至十几分钟：
`python3 run_evolve.py`。

### 3. 本地试运行（无需凭证）

使用仓库附带的基因文件，在本地模拟器中完整执行一局（不连接正式世界）：

```bash
python3 -m pip install -r requirements.txt   # 安装官方 SDK（需网络）
python3 deploy.py --local --genes genes/evolve_v7_best.json
```

预期输出：`[local] 800 ticks: pop=... harvest=... dmg=... alive=True`。

### 4. 部署到正式世界（需要 API key）

```bash
export ARENA_HERO_API_KEY=<你的 key>
python3 deploy.py --genes genes/evolve_v7_best.json
```

部署后 agent 每 15 秒一个 tick 持续运行，运行日志与统计写入 `results/`
（`agent_history.jsonl` / `agent_events.jsonl` / `live_status.json`）。

### 5. 运行测试

```bash
python3 -m pip install pytest
python3 -m pytest tests/ -q
```

预期输出：`71 passed`。

## 部署说明

`deploy.py` 将进化策略接入官方 `arena-hero` SDK，运行于正式世界。部署路径与
模拟器共用同一套 `HeuristicStrategy` 代码（规则版本 v0.14 / SDK 0.2.9）。

| 参数 | 说明 |
|---|---|
| `--genes <file>` | 基因来源：`run_evolve.py` 输出的 JSON（自动取 fitness 最高者），或 `{"name": value, ...}` 形式的基因文件 |
| `--local` | 本地试运行模式，不连接正式世界 |
| `--api-key <key>` | 指定 API key（**不推荐**：命令行参数对所有进程可见，可能泄露；请使用 `ARENA_HERO_API_KEY` 环境变量） |

说明：

- 运行统计写入 `results/`，供后续进化分析。
- 部署适配器为单账号模式，不包含多账号协作功能。

## 基因文件

`genes/evolve_v7_best.json` 为正式部署所使用的进化基因（27 个策略参数，
纯数值，无任何敏感信息）。使用方式：

```bash
python3 deploy.py --genes genes/evolve_v7_best.json
```

基因文件会被 `normalize_genes` 规范化到当前规则（v0.14）：冻结基因强制取
边界值（如 `max_population` 冻结为 32），旧规则遗留字段自动丢弃。自行进化
产生的 `best_genes` 使用相同的 `{"name": value, ...}` 格式即可替换。

## 可复现性

进化过程由种子体系完全驱动，相同命令行参数可复现相同结果。

| 参数 | 作用 |
|---|---|
| `--seeds` | 评估种子（默认 `42,43,44,45`），每局一张地图 |
| `--holdout-seeds` | 独立验证种子，不参与选择压力，用于检测过拟合 |
| `--seed-pool` / `--seed-rollover` | 滚动种子池，周期性更换评估地图 |
| `--max-ticks` | 每局模拟 tick 数（默认 800） |

世界生成、策略决策与对手随机性均受种子控制（见 `tests/test_world_determinism.py`）。

## 扩展开发

新增策略的步骤：

1. 继承 `strategies/base.py` 的 `Strategy` 接口，实现 `decide(obs)`（返回
   `{"core": ...动作, "units": {uid: ...动作}}`）与 `reset()`。
2. 在 `evolve/fitness.py` 的 `_make_strategy` / `_strategy_for_role` 中注册为
   被测策略或对手角色。
3. 若参数需参与进化，在 `strategies/heuristic.py` 的 `GENES` 中声明
   `(name, 默认值, 下界, 上界)`，进化算法自动纳入变异与交叉。

## 规则保真度

模拟器与官方文档（gameplay v0.14 / Python SDK 0.2.9）逐条核对，
详见 [docs/RULES-ALIGNMENT.md](docs/RULES-ALIGNMENT.md)。已修复的偏差包括：
v0.14 五人口区间动态涨价、Beacon 坐标公开与当 Tick 不可拾取、Core 自毁清空
舰队并同 Tick 重生、CANCEL_MOVE、cargo 堆同 Tick 独占、人口下降容量超限销毁等。

## 参考资源

### 官方

| 资源 | 链接 | 说明 |
|---|---|---|
| 官方文档站 | https://doc.arenahero.io/ | gameplay 规则（v0.14）与 HTTP / WebSocket API 文档 |
| 官方 Python SDK | https://pypi.org/project/arena-hero/ | 版本 0.2.9，`deploy.py` 的运行时依赖 |
| 官方 Skill | https://github.com/arena-hero/arena-hero-skill | 官方提供的 AI 构建 Agent 技能 |
| 官方示例前端 | https://app.arenahero.io/arena | 官方参考实现，非唯一玩法 |

### 相关项目

| 项目 | 链接 | 说明 |
|---|---|---|
| arena-hero-agent | https://github.com/Drew-Z/arena-hero-agent | 社区无人值守 Agent：确定性资源优先策略、分层威胁控制、自动更新，支持 Docker / systemd 部署 |
| Arena-Crazy-Attack | https://github.com/VelvetEvening/Arena-Crazy-Attack | 社区长期控制 Agent：静态资源任务分配，激进攻击型策略 |
| ArenaHero-nearly-perfect-guide | https://github.com/VelvetEvening/ArenaHero-nearly-perfect-guide | 社区长期控制 Agent：进攻型策略（"进攻才是最好的防守"） |

> 以上为 Arena Hero 社区的开源 Agent 实现，供策略与工程参考。

### 社区

| 社区 | 链接 |
|---|---|
| LINUX DO | https://linux.do/ |

## 许可

[MIT](LICENSE)
