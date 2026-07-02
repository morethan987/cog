# Cog × SCBench 集成设计文档

> 状态：**已实施（见 §9 实施回填）**。本文档原设计稿已落地，当前代码可直接跑 baseline/cog 对照实验。
> 后续修改以源码为准，并及时回填到 §9。
>
> 范围：把 `cog`（认知模型 CLI）接入 `slop-code-bench`（SCBench）benchmark，构成
> "有 cog / 无 cog"的对照实验，并完整记录轨迹与统计数据。

---

## 目录

- [0. 任务分解](#0-任务分解)
- [1. 背景：两个系统各是什么](#1-背景两个系统各是什么)
- [2. SCBench 测试流程剖析](#2-scbench-测试流程剖析)
- [3. 轨迹与产物：现有数据记录全景](#3-轨迹与产物现有数据记录全景)
- [4. cog 的使用遥测：.cog/usage.jsonl](#4-cog-的使用遥测cogusagejsonl)
- [5. 集成设计：把 cog 接入 benchmark](#5-集成设计把-cog-接入-benchmark)
- [6. 统计与量化方案](#6-统计与量化方案)
- [7. 实施清单（分阶段）](#7-实施清单分阶段)
- [8. 风险与待确认项](#8-风险与待确认项)

---

## 0. 任务分解

本次需求拆成三块，对应文档的 §5 / §5 / §6：

| # | 需求 | 文档位置 |
|---|------|---------|
| 1 | 在配置文件（如 `deepseek_run_smoke.yaml`）新增选项，控制是否启用 cog | §5.2 |
| 2 | 把 cog 融入 benchmark 执行流程（二进制可用 + 工作流注入 + 状态捕获） | §5.1–§5.6 |
| 3 | 详细轨迹信息与统计信息记录，便于量化 cog 表现 | §3 + §4 + §6 |

**核心结论先说**（后面逐条展开）：

1. **轨迹数据已经足够丰富，不需要额外埋点**。SCBench 在 `infer.log`（结构化 JSONL，逐条记录 agent 的 thinking / tool_use / tool_result / token）和 `checkpoint_N/agent/stdout.jsonl`（claude 原始 stream-json）里完整记录了 agent 的每一步操作。agent 调用的每一次 `cog` 命令（作为 Bash tool call）都在其中。
2. **cog 自带遥测** `.cog/usage.jsonl`，记录每次 `cog` 调用的命令、耗时、退出码、workflow 相变。只需在每个 checkpoint 结束后把 `/workspace/.cog/` 拷出到产物目录即可。
3. **接入点是现成的**：`ClaudeCodeConfig` 已暴露 `append_system_prompt` / `extra_args` / `env` / `allowed_tools`，base Docker 镜像已装好 Rust 工具链。无需大改框架，主要是配置编排 + 一个 checkpoint 后置钩子。

---

## 1. 背景：两个系统各是什么

### 1.1 SCBench 的测试范式

SCBench（SlopCodeBench）评测的是 **coding agent 在"迭代规格细化"下的表现**：agent 先实现一个 spec，然后随着 spec 不断扩展（checkpoint N → N+1）在**自己之前写的代码**上继续修改。这暴露了单次评测看不到的行为：

- **路径依赖**（path dependence）：同样的最终 spec，不同实现路径产出质量不同。
- **不收敛**（non-convergence）：改着改着 regress 了。
- **显式处理 vs 结构稳定性的权衡**。

每个 problem 有 N 个 checkpoint（实测的 `file_backup` 有 4 个），每个 checkpoint 有：`checkpoint_N.md`（规格说明）+ `test_checkpoint_N.py`（测试）。agent 在隔离的 Docker 容器里实现，每个 checkpoint 跑测试、打快照、算质量指标。

> 证据：`README.md` L15、`CLAUDE.md` L7–8、L150–168（agent 执行流）、实测产物 `.../file_backup/checkpoint_{1..4}`。

### 1.2 cog 的设计理念

cog 是给 LLM coding agent 用的**外部认知模型**。核心论点（`docs/vision/01-problem-and-thesis.md`）：

> LLM 生成代码的根本缺陷不是质量，而是**没有持久的外部认知模型**。人脑维护代码时隐含着因果链、不变量、设计意图；LLM 每个 session 从零开始重建理解，导致短视修补、认知漂移、腐败加速、归因缺失。

cog 的做法：为代码构建一个可构建、可追溯、可更新、唯一的**外部认知模型**，存放在 `.cog/cog.db`（SQLite）。它解决"**为什么这样写**"的归因问题，不解决"代码好不好"的评分问题（评分信号在 coding 场景下不可靠，见 §1.2 of thesis）。

三个基元（`docs/vision/02-cognitive-model.md`）：

| 基元 | 含义 | 例 |
|------|------|----|
| **Entity** | 被认知的代码符号 | `auth::login` |
| **Assertion** | 关于 entity 的信念（contract/intent/invariant/fragility/correction） | "login 密码错返回 None" |
| **Evidence** | 支撑 assertion 的观察 | `code:auth::login` |

两条关系线：entity 关系（contains/calls/uses，自动扫描或手声明）和 assertion 关系（depends_on，agent 推理构建）。核心机制是 **TMS 级联**（基于 Doyle 1979）：撤回一条 assertion 时，沿 depends_on 边 BFS 传播 Uncertain。

**工作流是四相循环**（`skills/cog/WORKFLOWS.md`）：

```
BUILD (sync+index) → ENRICH (assert+depend+impact) → REASON (experiment) → DESCEND (实现+verify) ↺
```

`cog next` 是单一引导点：读取 workflow 状态机 + 模型统计，建议下一步做什么。

### 1.3 为什么两者天然契合

SCBench 测的正是"跨 checkpoint 的认知连续性"——这正是 cog 的核心价值主张：

- **SCBench 的 checkpoint N+1 = 在 N 的代码上改**。无 cog 时，agent 在 checkpoint N+1 几乎是从零重建对已有代码的理解（典型的认知不连续）。
- **cog 的断言（尤其是 correction / fragility）跨 checkpoint 持久**。checkpoint N 里记下"这里用 dict 而不是 list 是因为顺序敏感"，N+1 时 `cog query` 直接拿回，不必重新理解。
- **SCBench 关注的"code erosion"（腐败）= cog 要缓解的问题**。一个量化的对照实验。

cog 自身的失败模式分析（`docs/concepts/01-failure-modes.md`）已经基于历史 benchmark 轨迹归纳了三类：抽象坍塌、级联发现、沉没成本陷阱——这说明 cog 在 benchmark 场景下的失效边界已被研究过，对照实验可以直接验证这些边界。

---

## 2. SCBench 测试流程剖析

> 目的：定位 cog 的接入点。证据来自源码 `src/slop_code/` + 实测产物 + 官方文档。

### 2.1 执行管线（端到端）

```
slop-code run --config deepseek_run_smoke.yaml
   │
   ▼
entrypoints/config/loader.py          ← OmegaConf 加载 YAML，优先级合并
   │   (CLI flag > config file > default)
   ▼
ResolvedRunConfig                      ← 全部解析成 dict/Path
   │
   ▼
problem_runner/driver.py::run_problems ← 按 problem 并行/串行
   │   (每个 problem 一个 worker)
   ▼
worker.py::run_agent_on_problem        ← 单 problem 的 checkpoint 循环
   │   ├─ 创建 Session（workspace + runtime）
   │   ├─ for checkpoint in problem.checkpoints:
   │   │     ├─ 渲染 prompt（jinja，注入 checkpoint_N.md 的 spec）
   │   │     ├─ agent.run(task)          ← ★ agent 在这里干活
   │   │     ├─ 评估测试（pytest via uvx）
   │   │     ├─ 打快照（snapshot/ + agent/workspace/）
   │   │     └─ 算质量指标（quality_analysis/）
   │   └─ 汇总 result.json
   ▼
outputs/<save_template>/               ← 所有产物落盘
```

> 证据：`CLAUDE.md` L144–168（Session→Workspace→Runtime 流）、`entrypoints/problem_runner/driver.py`、`worker.py::run_agent_on_problem`、实测 `run_info.yaml`（summary.checkpoints / duration / total_cost / total_usage）。

### 2.2 Docker 隔离机制

agent **运行在 Docker 容器内**，不是直接在宿主机上跑。这是集成的关键约束。

**镜像构建**（两段式）：

1. **base 层** `execution/docker_runtime/setup_base.docker.j2`——基于 `ghcr.io/astral-sh/uv:python3.12-trixie-slim`，装 build-essential / curl / git / Rust（rustup）/ Node（nvm）/ MinIO / Docker CLI 等。镜像里虽有 Rust 工具链，但**我们不在容器里编译**——宿主机 `cargo build --release` 出二进制后直接挂载/拷入（§5.3）。
   - 产物镜像名 `slop-code:{env_name}`（如 `slop-code:claude_code-2.0.51-python3.12`）。
2. **agent 层** `agents/claude_code/docker.j2`——`FROM base`，切非 root 用户，`npm install -g @anthropic-ai/claude-code@{{version}}`。

**容器运行**（`docker_runtime/streaming.py` + `exec.py`）：

- workspace 目录（宿主机临时目录，如 `/tmp/tmpXXXX`）挂载到容器的 `/workspace`（读写）。
- static assets 挂载到 `/static/<name>`（只读）。
- 通过 `docker exec` 在长生命周期容器内执行命令。
- 实测的 agent 命令（从 `infer.log` L40）：
  ```
  docker exec ... d945431c... \
    /bin/sh -c 'claude --output-format stream-json --verbose \
      --model claude-sonnet-4-5-20250929 --max-turns 100 \
      --permission-mode bypassPermissions --print -- "<task>"'
  ```
- `--permission-mode bypassPermissions` 意味着 agent **可以无确认地执行任意 bash**（包括调用 `cog`）。

> 证据：`docs/execution/docker.md`（Volume 合并顺序、生命周期）、`setup_base.docker.j2`、`claude_code/docker.j2`、实测 `infer.log` L40–47、`run_info.yaml` 的 `image` 字段。

### 2.3 一个 checkpoint 内部发生什么

```
[checkpoint N]
  1. 渲染 prompt：just-solve.jinja 模板 + checkpoint_N.md spec
     → prompt.txt（实测 8.5KB）
  2. agent.run(prompt)
     ├─ claude 在容器内启动，stream-json 输出
     ├─ agent 解析每个 payload（thinking/text/tool_use/tool_result）
     ├─ 累计 token usage、cost、steps
     └─ 输出到 checkpoint_N/agent/stdout.jsonl（原始）+ infer.log（结构化）
  3. 评估：把 test_checkpoint_N.py 复制进 workspace，uvx 跑 pytest
     → checkpoint_N/evaluation.json + evaluation/{report.json,stdout,stderr}
  4. 快照：
     ├─ snapshot/         （提交的代码，应用 ignore_globs）
     └─ agent/workspace/  （完整 workspace 拷贝）
  5. 质量指标：quality_analysis/{overall_quality.json, symbols.jsonl, files.jsonl, ast_grep.jsonl}
  6. diff：checkpoint_N/diff.json（与 N-1 的代码差异）
```

> 证据：实测 `.../file_backup/checkpoint_1/` 目录结构（snapshot/ backup_scheduler.py、agent/stdout.jsonl 177KB、evaluation.json、quality_analysis/ 等）、`infer.log` L26–48（checkpoint 循环日志）、`snapshot.py`。

### 2.4 配置体系（三层 + 优先级）

SCBench 的配置是 **Hydra 风格**，按角色分目录：

| 配置层 | 文件 | 作用 | cog 接入点？ |
|--------|------|------|-------------|
| **run config** | `deepseek_run_smoke.yaml`（用户写的） | 顶层：agent/env/prompt/model/thinking/problems/save_dir | ★ Part 1 的 toggle 放这里 |
| **agent config** | `configs/agents/claude_code_deepseek.yaml` | agent 类型、binary、版本、**env / extra_args / append_system_prompt / allowed_tools / cost_limits** | ★ Part 2 的 prompt 注入放这里 |
| **environment config** | `configs/environments/docker-python3.12-uv.yaml` | docker image、workdir、mount、setup 命令、entry command | ★ Part 2 的二进制可用性放这里 |
| **prompt config** | `configs/prompts/*.jinja` | 任务 prompt 模板 | 可选：cog-guided 专用模板 |

优先级：`CLI key=value > CLI flag > config file > default`（`docs/commands/run.md` L72–77）。

**关键约束**：`RunConfig` 和 `ResolvedRunConfig` 都是 Pydantic 模型且 `extra="forbid"`（`run_config.py` L91, L160）——意味着往 run config YAML 里加**新字段必须在 `RunConfig` 模型里加对应字段**，否则加载报错。但 `agent` / `environment` 字段是 `dict[str, Any]`（L94–95），agent config 里的 `append_system_prompt` 等是**透传到 agent 的**，不在 RunConfig 的 forbid 范围内。

> 证据：`run_config.py` L91/160/94–95、`agents/claude_code/agent.py` L108–125（ClaudeCodeConfig 字段）、`docs/commands/run.md`。

---

（下接 §3 轨迹与产物、§4 cog 遥测、§5 集成设计、§6 统计方案）

---

## 3. 轨迹与产物：现有数据记录全景

> 目的：回答"框架是否记录完整 agent 操作轨迹"——是的，且非常完整。
> 证据来自实测产物 `outputs/anthropic/claude_code-2.0.51_just-solve_20260701T1034/`。

### 3.1 输出目录结构（实测）

一次 run 的完整产物（以 `file_backup` 单 problem 为例）：

```
outputs/<model>/<agent>-<ver>_<prompt>_<timestamp>/        ← save_template 渲染结果
├── config.yaml                 ← 合并后的最终配置（含 prompt 全文、env 全量）
├── environment.yaml            ← 解析后的环境配置
├── problem_catalog.json
├── result.json                 ← ★ run 级聚合：cost/tokens/steps/pass_rates/cc/erosion/ratios
├── checkpoint_results.jsonl    ← ★ 每个 checkpoint 一行（pass_rate/test_counts/loc/cost...）
├── run_agent.log               ← 顶层 structlog
├── evaluation.log
└── <problem>/                  ← 如 file_backup/
    ├── run_info.yaml           ← problem 级 summary（duration/cost/steps/pass_rate）
    ├── problem.yaml
    ├── infer.log               ← ★★★ 结构化轨迹（JSONL，见 §3.2）
    └── checkpoint_N/
        ├── checkpoint.yaml
        ├── prompt.txt          ← 渲染后的完整 prompt（8.5KB）
        ├── diff.json           ← 与上一 checkpoint 的代码 diff
        ├── inference_result.json  ← 本 checkpoint 的 cost/tokens/steps
        ├── snapshot/           ← 提交的代码（应用 ignore_globs）
        ├── agent/
        │   ├── stdout.jsonl    ← ★★★ claude 原始 stream-json（完整对话，177KB+）
        │   ├── stderr.log
        │   └── workspace/      ← ★ 完整 workspace 拷贝（含隐藏目录）
        ├── evaluation.json     ← 测试结果摘要
        ├── evaluation/
        │   ├── report.json     ← 完整 pytest 报告（80KB+）
        │   ├── stdout.txt
        │   └── stderr.txt
        └── quality_analysis/
            ├── overall_quality.json
            ├── symbols.jsonl   ← AST 符号
            ├── files.jsonl
            └── ast_grep.jsonl
```

### 3.2 轨迹数据：两份完整记录

#### `infer.log` —— 结构化、逐事件

每行一个 JSON，记录 benchmark 框架视角下 agent 的每一步。实测样本（`infer.log` L49–83）：

```json
// agent 的 thinking
{"msg_id":"...","type":"assistant","content":"[{\"type\":\"thinking\",\"thinking\":\"The user wants me to implement a backup scheduler...\"}","event":"Received payload","timestamp":"...","level":"debug"}
// token / cost 累计（每步）
{"tokens":"TokenUsage(input=17220,output=0,cache_read=0,...)","cost":0.05166,"steps":0,"event":"Received step",...}
// tool 调用（如 agent 跑 bash）
{"msg_id":"...","type":"assistant","content":"[{\"type\":\"tool_use\",\"name\":\"Bash\",\"input\":{\"command\":\"ls /workspace/\"}}]","event":"Received payload",...}
// tool 结果
{"msg_id":null,"type":"user","content":"[{\"tool_use_id\":\"...\",\"type\":\"tool_result\",\"content\":\"files\"}]","event":"Received payload",...}
```

**这意味着**：agent 调用的每一次 `cog ...` 命令（都是 `Bash` tool_use）都被完整记录——包括命令原文、输出、耗时上下文。这是后续做"cog 使用 vs 结果"相关性分析的**主数据源**。

#### `agent/stdout.jsonl` —— 原始、未加工

claude 以 `--output-format stream-json` 输出的原始流，一行一个完整事件对象。比 infer.log 更原始（未截断、未过滤），含完整的 `message` 结构。适合需要还原完整对话上下文的分析。

#### 从轨迹里提取 cog 调用的方法

```python
# 伪代码：从 stdout.jsonl 里提取所有 cog 命令
import json
for line in open("checkpoint_N/agent/stdout.jsonl"):
    evt = json.loads(line)
    for block in evt.get("message", {}).get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "Bash":
            cmd = block["input"].get("command", "")
            if "cog " in cmd:
                yield {"cmd": cmd, "ts": evt["timestamp"], ...}
```

### 3.3 评估与质量数据

**`checkpoint_results.jsonl`**（每 checkpoint 一行，实测字段）：

| 字段 | 含义 |
------|------|
| `strict_pass_rate` / `core_pass_rate` / `isolated_pass_rate` | 三种通过率 |
| `total_tests` / `passed_tests` | 测试总数/通过数 |
| `core/functionality/error/regression_total` + `_passed` | 按 GroupType 拆分（REGRESSION = 之前 checkpoint 的测试，**这是 erosion 的直接信号**） |
| `duration` / `cost` / `steps` | 本 checkpoint 耗时/花费/步数 |
| `cache_read/write` / `input` / `output` / `reasoning` | token 用量 |
| `loc` / `sloc` | 代码规模 |

**`result.json`**（run 级聚合，实测字段）：

| 字段 | 含义 | erosion 相关性 |
------|------|---------------|
| `pass_rates` | 各 GroupType 的通过率 | `regression` 通过率下降 = erosion |
| `cc` | 圈复杂度统计（high_count / high_mean / max） | 复杂度膨胀 |
| `ratios.lint` / `ratios.violation_pct` | lint 问题比例 | 代码卫生 |
| `erosion` | SCBench 的专门 erosion 指标（单 problem 时为 null，需多 problem 聚合） | **核心指标** |
| `ratios.rubric` / `verbosity` | LLM judge 打分（需额外跑 `metrics judge`） | 主观质量 |
| `checkpoints_solved` / `checkpoints_iso_solved` / `checkpoints_core_solved` | 各口径通过数 | 收敛性 |

> 实测数据印证 erosion：`checkpoint_1` strict_pass_rate=0.875，到 `checkpoint_4` 跌到 **0.618**；core 从 1.0 跌到 0.0。REGRESSION 通过率（前一 checkpoint 的测试在当前是否还过）是 erosion 最直接的度量。

### 3.4 结论：轨迹已足够，无需额外埋点

SCBench 已经记录了：

- ✅ **完整 agent 操作轨迹**（infer.log + stdout.jsonl，含每次 tool call）
- ✅ **逐 checkpoint 评估结果**（test pass rates，按 group 拆分）
- ✅ **逐 checkpoint 质量指标**（complexity / loc / lint）
- ✅ **逐 checkpoint 成本**（token / cost / steps / duration）
- ✅ **代码 diff**（diff.json）
- ✅ **完整代码快照**（snapshot/ + agent/workspace/）

**唯一缺的是 cog 侧的数据**——但这正是 cog 自己的 `.cog/` 提供的（§4）。所以我们不需要在 SCBench 框架里埋点，只需要：**把 `.cog/` 在每个 checkpoint 后拷出来**（§5.5）。

---

## 4. cog 的使用遥测：.cog/usage.jsonl

> cog 每次被调用都会向 `.cog/usage.jsonl` 追加一条事件。这是量化"agent 到底有没有、用了多少 cog"的**一手数据**。

### 4.1 记录了什么（UsageEvent schema）

源码 `src/usage/event.rs`：

```rust
pub struct UsageEvent {
    pub ts: DateTime<Utc>,           // 调用时刻
    pub command: String,             // 命令动词：assert / sync / query / ...
    pub ok: bool,                    // 是否成功
    pub exit_code: Option<i32>,      // 退出码
    pub duration_ms: u64,            // 耗时（毫秒）
    pub has_drift: bool,             // sync 是否检测到代码漂移
    pub phase_from: Option<String>,  // workflow 相变前
    pub phase_to: Option<String>,    // workflow 相变后
    pub args: Value,                 // 结构化参数（entity ref / id / kind / flag），非自由文本
    pub metrics: Option<Value>,      // 命令附带的结构化负载（如 sync 的关系分解）
}
```

**写入是 best-effort 的**（`src/usage/recorder.rs`）：任何 I/O / 序列化失败只 stderr 警告，绝不打断触发它的命令。`COG_USAGE=off|0|false` 可完全关闭。

### 4.2 cog 还记录什么（除 usage 外）

`.cog/` 目录完整内容（这才是要拷出来的全部）：

| 文件 | 内容 | 分析价值 |
|------|------|---------|
| `usage.jsonl` | 上面的事件流 | **cog 调用频率/耗时/成功率** |
| `cog.db` | SQLite 主库（entities / assertions / evidence / relations / changelog） | **认知模型的完整状态** |
| `workflow_state.json` | workflow 状态机当前相 | agent 处在哪个阶段 |
| `experiments/<id>.json` | 每个实验的快照 | agent 做了哪些假设推演 |
| `backups/*.db` | VACUUM INTO 快照 | 显式备份 |
| `cog.db-wal` / `cog.db-shm` | WAL 文件 | （SQLite 并发控制） |

### 4.3 如何读取

```bash
cog usage          # 内置聚合：invocations / ok / errored / sessions / reads / writes / by_command
cog stats          # 模型统计：entity / assertion / relation 计数
cog export --format json   # 完整模型快照
```

或直接解析（适合离线批量分析，不依赖 cog 二进制）：

```python
import json, sqlite3
# usage.jsonl → pandas
events = [json.loads(l) for l in open(".cog/usage.jsonl")]
# cog.db → 直接 SQL 查询
con = sqlite3.connect(".cog/cog.db")
assertions = con.execute("SELECT * FROM assertions").fetchall()
changelog = con.execute("SELECT * FROM changelog ORDER BY ts").fetchall()
```

`analyze.rs` 还提供 session 切分（间隔 >30 分钟算新 session）和 by_command / by_action 聚合。

### 4.4 量化"认知层是否被有效使用"的维度

从 usage.jsonl 能直接算出的指标：

- **采用率**：cog 命令数 / 总 tool call 数（配合 stdout.jsonl）
- **命令分布**：query vs assert vs experiment 的比例（只读 vs 写入的认知投入）
- **读写比**：`is_read()` 统计的 reads vs writes
- **耗时占比**：`sum(duration_ms where command=cog)` / checkpoint 总耗时
- **错误率**：`errored / total`
- **workflow 流动性**：phase 转换次数（agent 有没有走完 BUILD→ENRICH→DESCEND）

从 cog.db 能算出的指标：

- **断言覆盖度**：有 assertion 的 entity 占比（`cog index` 的 coverage）
- **知识积累曲线**：按时间排序 changelog，看 assertion 数随 checkpoint 增长
- **试错代价**：retract 次数 / assert 次数（沉没成本陷阱的量化，见 §1.2 failure-modes）
- **fragility 发现**：被 assert 又被 correction 取代的断言数

---

## 5. 集成设计：把 cog 接入 benchmark

### 5.1 总体策略：三层接入

cog 要"有效接入"，必须同时解决三件事，缺一不可：

```
① 二进制可用      ② 工作流注入        ③ 状态捕获
容器里有 cog 可执行   agent 知道何时/如何用   每个 checkpoint 把 .cog/ 拷出
```

| 层 | 问题 | 接入点（现有机制） | 改动性质 |
----|------|-------------------|---------|
| ① 二进制 | cog 不在容器里 | environment config 的 docker 层 / 运行时 volume 挂载 | 配置（或镜像层） |
| ② 工作流 | agent 不知道 cog 存在 | agent config 的 `append_system_prompt` + workspace 内的 `CLAUDE.md`/`AGENTS.md`/skill | 配置 + 静态文件 |
| ③ 状态捕获 | `.cog/` 在容器销毁后丢失 | checkpoint 后置钩子拷贝 `/workspace/.cog` → 产物目录 | **需加代码钩子** |

### 5.2 Part 1 交付物：配置开关设计

**需求**：在 `deepseek_run_smoke.yaml` 这类 run config 里加一个开关，控制是否启用 cog。

#### 方案 A（推荐）：run config 顶层 `cog` 块

在 `RunConfig` Pydantic 模型新增一个可选字段（需改 `run_config.py`，属代码改动）：

```yaml
# deepseek_run_smoke.yaml
agent: ./configs/agents/claude_code_deepseek.yaml
environment: docker-python3.12-uv
prompt: just-solve
model: { provider: deepseek, name: deepseek-v4-flash }
thinking: high
pass_policy: all-cases
problems: [file_backup]

# 新增
cog:
  enabled: true              # 主开关：true 启用 cog，false / 缺省 = 原始 baseline
  binary_path: ../../target/release/cog   # 宿主机预编译二进制（挂载进容器）
  capture_state: true        # 每个 checkpoint 后拷出 .cog/
```

对应 `RunConfig` 字段：

```python
# run_config.py 新增（RunConfig + ResolvedRunConfig 都要加）
class CogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    binary_path: str | None = None      # 默认 ../../target/release/cog（相对 repo 根）
    capture_state: bool = True

# RunConfig 里：
cog: CogConfig = Field(default_factory=CogConfig)
```

**为什么放 run config 而非 agent config**：对照实验要保证"同一个 agent、同一个 model、同一个 prompt"，只切 cog 开关。放 run config 才能 `cog.enabled: true` vs `false` 做最小差异对照；agent config 里塞 cog 会绑死 agent。

**`save_template` 建议**带上 cog 标记，避免产物目录撞名：

```yaml
save_template: ${model.provider}/${agent.type}-${agent.version}_${prompt}_cog${cog.enabled:0}_${now:%Y%m%dT%H%M}
# baseline 产出  ..._just-solve_cog0_...
# 实验组产出   ..._just-solve_cog1_...
```

#### 方案 B（零代码改动，备选）：纯靠 agent config

不改框架，直接在 agent config 里设 `append_system_prompt`（已被 `ClaudeCodeConfig` 支持，`agent.py` L887）：

```yaml
# configs/agents/claude_code_deepseek_cog.yaml （新文件，复制 + 加 cog 指令）
append_system_prompt: |
  You have access to `cog`, a cognitive model CLI at /usr/local/bin/cog.
  Before changing code: run `cog sync`, `cog query <entity>`, `cog impact <entity>`.
  Record contracts/fragilities with `cog assert`. Run `cog next` for guidance.
```

配合一个 baseline agent config 和一个 cog agent config，run config 各引一个。
**缺点**：开/关要改 `agent:` 引用，且二进制挂载仍需单独处理；不如方案 A 干净。但**完全不需要改 Python 代码**，适合先快速验证。

#### 建议

**先 B 后 A**：先用方案 B 跑通端到端（验证 prompt 注入 + 二进制挂载 + .cog 捕获链路），再把开关沉淀进 RunConfig（方案 A）做规模化对照。

### 5.3 让 cog 在容器内可用

agent 在 Docker 内运行，cog 必须在容器里。**不在容器里编译**——宿主机一次 `cargo build --release`，把产物 `target/release/cog` 送进容器即可。两种送法：

#### 送法 ①（开发期推荐）：运行时挂载

host 上 `cargo build --release` 产出 `target/release/cog`，spawn 时挂进容器：

```yaml
# environment config 的 docker.extra_mounts，或运行时注入
docker:
  extra_mounts:
    /path/to/cog/target/release/cog: /usr/local/bin/cog:ro
```

- 改了 cog 代码，`cargo build` 后立即生效，**零镜像重建**。
- 接入点：`DockerConfig.extra_mounts`（`docs/execution/docker.md` L53–60），或 worker spawn 时传 `mounts=`。

#### 送法 ②（复现/分发）：拷进镜像层

把预编译二进制 `COPY` 进镜像（`docker.j2` 或 `setup_base.docker.j2` 末尾）：

```dockerfile
COPY cog /usr/local/bin/cog
RUN chmod +x /usr/local/bin/cog
```

- 镜像自包含，任何人 pull 即可复现。
- 缺点：每次 cog 改动要重建镜像。仅当需要分发/固定版本时用。

#### 唯一要注意的：glibc 可移植性

Rust 默认动态链接 glibc（cog 的 SQLite 走 `bundled` 特性，已静态进二进制；唯一外部依赖就是 glibc）。**只要构建机的 glibc 版本 ≤ 容器的 glibc 版本**，二进制就能跑。

- 容器是 `trixie-slim`（Debian 13，glibc 2.40+）。
- 若宿主机 glibc 更新（如 Arch rolling），二进制可能在容器旧 glibc 上报 `version GLIBC_2.xx not found`。
- 解法（任选其一，按需）：
  - 在与容器同 glibc 的环境里构建：`docker run --rm -v "$PWD:/src" ghcr.io/astral-sh/uv:python3.12-trixie-slim sh -c 'apt-get update && apt-get install -y cargo && cd /src && cargo build --release'`，产出的二进制保证可移植进同类容器。
  - 用 musl 静态目标：`rustup target add x86_64-unknown-linux-musl && cargo build --release --target x86_64-unknown-linux-musl`，产物完全静态、零 glibc 依赖。
  - 阶段 0 先手动起容器验证一次，跑不通再处理——大多数情况直接能跑。

#### 验证可用性

容器起来后、agent 跑前，在 setup 或 resume_commands 里加一行自检（`configs/environments/*.yaml` 的 setup）：

```yaml
setup:
  resume_commands:
    - python -m venv .venv || true
    - cog --version || echo "WARN: cog not available"   # ← 自检
```

### 5.4 把 cog 工作流注入 agent

光装二进制不够——agent 不知道有它、不知道何时用。下面三种注入手段叠加使用（不是独立的实验组）；效果不好就加强注入，不必预设对照维度：

**默认同时做层 1 + 层 3**：层 1（`append_system_prompt`）作为系统级提醒，层 3（`cog-guided.jinja`）作为**主入口**——因为 agent 对任务 prompt 里的"第一步必须..."执行率远高于 system message 里的参考说明。`loader.py` 在 `cog.enabled=true` 时自动把 `prompt: just-solve` 切换为 `prompt: cog-guided`，保证 baseline 和 cog 组除 prompt 外完全一致。层 2 是可选的加强手段，仅当效果不够时叠加。

#### 层 1：`append_system_prompt`（辅助提醒）

`ClaudeCodeConfig.append_system_prompt` → claude 的 `--append-system-prompt` flag（`agent.py` L887）。放一段简明指令，在系统消息层不断提醒 agent 有 cog 可用：

```yaml
append_system_prompt: |
  ## Required cognitive tool: `cog`

  You have access to `cog` at `/usr/local/bin/cog`. It is a persistent cognitive model of the codebase, stored in `.cog/cog.db`. You are required to use it to record and reason about the code you are working on, especially across checkpoints.

  What `cog` records:
  - `contract`: what a piece of code promises to callers
  - `intent`: why a non-obvious design choice was made
  - `invariant`: a property that must always hold, or a bug results
  - `fragility`: a known risk or trap that is not visible in the code
  - `correction`: a mistake that was made and how it was fixed

  Your workflow on every checkpoint:
  1. BUILD: `cog sync --init` (first time) or `cog sync` (refresh). Then `cog index --uncovered` to see what lacks knowledge.
  2. ENRICH: Before changing code, run `cog query <entity>` and `cog impact <entity>` on the entities you plan to touch. Record what you learn with `cog assert <entity> --kind <kind> --claim "<one sentence>" --grounds "code:<entity>"`.
  3. REASON: For changes that affect multiple entities or public interfaces, run `cog experiment try <entity> --kind <kind> --claim "<plan>" --grounds "plan:<desc>" --desc "<desc>"`. Evaluate the risk before editing code.
  4. DESCEND: After editing, run `cog sync` and `cog verify --scan`. Record corrections with `cog assert --kind correction` and retract stale claims with `cog retract <id> --reason "<why>"`.

  When you are unsure what to do next, run `cog next`. It reads the workflow state and tells you whether to survey, enrich, reason, or descend.

  Rules:
  - Record WHY, not WHAT. The code already states what it does.
  - One assertion per sentence.
  - Entity names are `::-qualified` (e.g. `backup_scheduler::parse_schedule`).
  - Ground assertions with `code:<entity>` or `test:<path>` when possible.
  - Never delete or reset `.cog/cog.db`.
  - `cog` does not capture runtime behavior; always verify with tests.
  - Do not over-model trivial code. Ask: "If this assumption changed, would it cause a hard-to-find bug?"
  - Do not use `cog` to avoid reading code or running tests.
```

#### 层 2：workspace 内的 `AGENTS.md` / `CLAUDE.md`（中等）

claude code 会自动读取 workspace 根目录的 `CLAUDE.md` / `AGENTS.md`（实测：benchmark repo 自身就有 `CLAUDE.md` 和 `AGENTS.md`，说明这套 agent 读这些文件）。在 agent 工作前，往 `/workspace/` 放一份精简的 cog 使用指南 + skill 文件：

- 复制 `skills/cog/SKILL.md` → `/workspace/.cog_skill.md`（或注入 `CLAUDE.md`）。
- 这给 agent 完整的命令参考、工作流四相、断言分类、grounds 格式。

**接入点**：worker 在创建 workspace 后、agent.run 前，把 skill 文件 copytree 进去；或作为 static asset 挂载。

#### 层 3：prompt 模板（主入口，必须）

创建 `configs/prompts/cog-guided.jinja`，把 cog 工作流**强制嵌入任务流程**。这是让 agent 真正执行 cog 命令的最强手段，因为任务指令被 agent 视为必须完成的步骤，而 system message 容易被当成背景参考。

`cog-guided.jinja` 完整内容：

```jinja
Implement a program that 100% solves the specification.

You MUST use the `cog` cognitive model CLI at `/usr/local/bin/cog` throughout this task. `cog` maintains a persistent model of the codebase in `.cog/cog.db`. The model has THREE paths and you MUST use all three on every checkpoint:

1. ASCEND (code space to latent space): `cog sync` then `cog query` / `cog impact` / `cog trace` / `cog index` to build and read the structural model.
2. REASON (horizontal path within the latent space): `cog experiment try` to test a planned change in an in-memory sandbox BEFORE writing code, then read its risk and blind spots.
3. DESCEND (latent space to code space): edit code, then `cog sync` + `cog verify` + record corrections.

The REASON path is mandatory. Do NOT skip straight from reading the model to writing code. Testing your plan with `cog experiment` first is what catches contradictions and blind spots before they become bugs.

{% if not is_continuation -%}
This is the first checkpoint. Follow this exact order:
1. ASCEND: `cog sync --init` to create `.cog/cog.db`, then `cog index --uncovered`.
2. SURVEY: `cog query <entity>`, `cog impact <entity>`, and `cog trace <entity>` on the entities you will touch.
3. REASON: For every non-trivial change, run `cog experiment try <entity> --kind <kind> --claim "<planned change>" --grounds "plan:<desc>" --desc "<desc>"`. Read the risk score and blind entities. Revise the plan if risk is high. For multi-step plans use `cog experiment start` + `hypothesize` + `evaluate` + `commit` or `discard`.
4. SCOUT: Read the actual code to confirm your plan survives details the model does not hold.
5. DESCEND: Write the code. Then `cog sync` and `cog verify --scan`.
6. RECORD: `cog assert` contracts, invariants, fragilities, intent. `cog retract` any claim the code invalidated.
7. Use a virtual environment and ensure a `requirements.txt` is present with any dependencies you need.
{% else -%}
This checkpoint extends prior work. The cognitive model already exists. Follow this exact order:
1. ASCEND: `cog sync` to refresh the model, then `cog query` the entities you will modify.
2. SURVEY: `cog impact <entity>` and `cog trace <entity>` on the entities you will touch.
3. REASON: `cog experiment try <entity> --kind <kind> --claim "<planned change>" --grounds "plan:<desc>" --desc "<desc>"`. Evaluate before editing. Revise if risk is high.
4. SCOUT: Read the code to confirm assumptions.
5. DESCEND: Edit the code. Then `cog sync` and `cog verify --scan`.
6. RECORD: `cog assert --kind correction` for what changed. `cog retract <id>` for stale claims. `cog recover` if a cascade left assertions uncertain.
7. Keep using the same virtual environment; update `requirements.txt` as needed.
{% endif -%}

Rules for `cog`:
- Record WHY, not WHAT. The code already states what it does.
- One assertion per sentence.
- Entity names are `::-qualified` (e.g. `backup_scheduler::parse_schedule`).
- Ground assertions with `code:<entity>` or `test:<path>` when possible.
- Link entities with `cog depend <a> --on <b> --kind calls|uses` when you find a relationship sync missed.
- Never delete or reset `.cog/cog.db`.
- `cog` does not capture runtime behavior; always verify with tests.
- When stuck, run `cog next`.
- Do not use `cog` to avoid reading code or running tests.

Your task is:
{{ spec.strip() }}
```

**三条路径的设计依据**：cog 的认知模型有三条路径——上升（代码空间→潜空间，sync/query/impact/trace）、水平推理（潜空间内，experiment 沙盘推演）、下降协议（潜空间→代码空间，SCOUT/PROBE/COMPLETE）。初版 prompt 只覆盖了上升和下降，agent 完全不用 `experiment`（实测 0 次）。改为强制三路径后，agent 必须在写代码前先用 `cog experiment try` 测试计划，补齐水平推理路径。

#### 不预设注入强度对照

注入强度（层 1/2/3 用多少）**不作为独立实验维度**：当前默认层 1 + 层 3。若 agent 仍不用 cog，再叠加层 2；若仍不够，再强化 prompt 措辞。这本身是迭代的正常部分，不必为"轻注入 vs 重注入"单独设计对照。

### 5.5 Part 3 交付物：每个 checkpoint 捕获 .cog 状态

**问题**：`.cog/` 在容器内 `/workspace/.cog/`，容器销毁后丢失。需要每个 checkpoint 后拷到产物目录。

#### 现状：.cog/ 可能已被部分捕获

实测的 snapshot ignore_globs（`infer.log` L16）：
```
{.claude/*, .ruff_cache/*, tests/assets, .venv/*, files/*, node_modules/*,
 files, __pycache__/*, .pytest_cache/*, *.pyc, tests/assets/*,
 .evaluation_tests/*, venv/*, .mypy_cache/*, .opencode/*}
```
**`.cog/*` 不在其中**——所以 `.cog/` 会被打进 `snapshot/`（提交快照）和留在 `agent/workspace/`（完整拷贝）里。但这有两个问题：
1. 不可靠：依赖快照逻辑不排除它，未来若加 `.cog/*` 到 ignore 就丢。
2. 不聚合：散在各 checkpoint 的 workspace 里，分析时要逐个解包。

#### 方案：显式后置钩子（推荐）

在 checkpoint 循环里、snapshot 之后，加一步显式拷贝：

```python
# 伪代码：worker.py 或 runner 在 agent.run + 评估 + snapshot 之后
import shutil
from pathlib import Path

def capture_cog_state(workspace: Path, checkpoint_out: Path):
    """把容器内 /workspace/.cog 拷到 checkpoint 产物目录。"""
    src = workspace / ".cog"
    dst = checkpoint_out / "cog_state"
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        # WAL checkpoint 前确保数据落盘：进容器跑一次 cog 或 sqlite checkpoint
    else:
        # baseline（cog 未启用）时写个标记文件，方便后续脚本统一处理
        (checkpoint_out / "cog_state").mkdir(exist_ok=True)
        (checkpoint_out / "cog_state" / ".disabled").touch()
```

产物布局：

```
checkpoint_N/
└── cog_state/                  ← ★ 新增
    ├── usage.jsonl             ← cog 调用遥测
    ├── cog.db (+ -wal/-shm)    ← 认知模型完整状态
    ├── workflow_state.json     ← workflow 当前相
    └── experiments/            ← 假设推演快照
```

#### 注意事项

- **WAL 落盘**：SQLite 在 WAL 模式下，agent 跑完到拷贝之间最好触发一次 checkpoint。最稳的是在拷贝前让 agent（或钩子）在容器内跑 `cog stats` 或 `echo "PRAGMA wal_checkpoint(FULL);" | sqlite3 .cog/cog.db`，确保 `-wal` 合并进 `.db`。否则要连 `-wal`/`-shm` 一起拷。
- **拷贝时机**：必须在容器还活着时（snapshot 之后、cleanup 之前）。接入点是 worker 的 checkpoint 收尾或 `Session` 的 snapshot 钩子。
- **persist across checkpoints**：`.cog/cog.db` 应跨 checkpoint 累积（agent 在 checkpoint N 记的知识，N+1 还要用）。容器复用同一个 workspace 即可保证——实测容器是长生命周期的（`docker exec` 多次调用同一容器）。

### 5.6 对照实验的变量控制

要让 A/B 可比，**只切 cog，其余全固定**：

| 变量 | baseline | 实验组 | 备注 |
------|---------|--------|------|
| agent | 同一 `claude_code-2.0.51` | 同 | |
| model | 同（如 deepseek-v4-flash） | 同 | |
| prompt | 同一模板（如 `just-solve`） | 同 | cog 注入走 agent config 的 `append_system_prompt`，与 prompt 模板解耦；baseline 不加这段 |
| thinking | 同 | 同 | |
| problems | 同一组 | 同 | |
| seed | 同（实测 seed=42） | 同 | `run_info.yaml` 有 seed |
| **cog.enabled** | false | true | 唯一差异 |
| **注入内容** | 无 | 完整 cog 工作流指令 | 唯一差异 |

**建议跑多 seed**（方差）：SCBench 自带 `configs/runs/variance.yaml`，可对同一 problem 跑多 seed 取统计量，避免单次偶然。

**关键混淆项——token 开销**：cog 命令本身耗 token（读输出、写 claim）。这会让实验组的 input/output token 偏高。这不是 bug，是 cog 的真实成本，**必须如实记录并纳入效率对比**（§6）。但不能因此判定 cog 无效——要看质量/收敛是否提升足以抵消。

---

## 6. 统计与量化方案

### 6.1 指标矩阵

把 cog 的价值拆成四个可量化维度。每个指标标注**数据来源**和**计算方法**。

#### 维度 1：任务结果（cog 是否让代码更对）

| 指标 | 来源 | 算法 | erosion 相关性 |
------|------|------|---------------|
| core pass rate | `checkpoint_results.jsonl.core_pass_rate` | 直接读 | 核心，是否基本功能还在 |
| strict pass rate | `...strict_pass_rate` | 直接读 | 全量通过率 |
| **regression pass rate** | `regression_passed / regression_total` | 直接读 | **★ erosion 核心信号**：之前 checkpoint 的测试现在还过吗 |
| isolated pass rate | `...isolated_pass_rate` | 直接读 | 只跑当前 checkpoint 测试 |
| checkpoints_core_solved | `result.json` | 直接读 | 多少 checkpoint core 全过 |
| erosion 指标 | `result.json.erosion`（多 problem 时非 null） | SCBench 内置 | 跨 problem 聚合的腐败度量 |

**对照逻辑**：若 cog 有效，实验组的 regression pass rate 和 checkpoints_solved 应**高于** baseline，尤其在 checkpoint 3/4（spec 累积变复杂时）。

#### 维度 2：效率（cog 是否让 agent 更省/更快）

| 指标 | 来源 | 算法 |
------|------|------|
| 总 cost | `result.json.costs.total` / `run_info.yaml.total_cost` | 直接读 |
| 总 tokens | `result.json.tokens` | input+output+cache_read |
| 总 steps | `result.json.steps` / `run_info.yaml.total_steps` | 直接读 |
| 总 duration | `run_info.yaml.summary.duration_seconds` | 直接读 |
| 每 checkpoint 成本 | `checkpoint_results.jsonl.cost` | 直接读 |
| **首次通过成本** | 累加到第一次 core_pass_rate=1.0 的 checkpoint 的 cost | 需脚本累加 |
| cog 自身 token 开销 | `stdout.jsonl` 里 cog 命令的 input/output | 脚本解析（§3.2） |
| cog 自身耗时占比 | `sum(usage.jsonl.duration_ms) / duration_seconds*1000` | 脚本 |

**对照逻辑**：cog 可能**增加**总 token（认知开销）但**减少**总步数/迭代轮次（少走弯路）。看 cost-per-solved-checkpoint 而非绝对 cost。

#### 维度 3：认知层使用（agent 到底用没用 cog、用得对不对）

| 指标 | 来源 | 算法 |
------|------|------|
| cog 调用总数 | `usage.jsonl` | `len(events)` |
| 采用率 | `usage.jsonl` + `stdout.jsonl` | `cog_calls / total_tool_calls` |
| 命令分布 | `usage.jsonl.command` | `Counter(command)` |
| 读写比 | `usage.jsonl` + `is_read()` | `reads / writes` |
| cog 错误率 | `usage.jsonl.ok` | `sum(not ok)/total` |
| workflow 流动 | `usage.jsonl.phase_to` | 相变序列（BUILD→ENRICH→...） |
| **断言覆盖度** | `cog.db` | 有 assertion 的 entity / 总 entity |
| 知识积累曲线 | `cog.db.changelog`（按 ts 排序） | 累积 assertion 数随 checkpoint |
| **试错代价** | `cog.db.changelog` action | `retract / assert` 比例 |
| fragility 发现数 | `cog.db` assertions kind=fragility | count |

**对照逻辑**：这是 cog 组独有的维度。判断 cog 是"被有效使用"还是"装了没用 / 乱用"。
（failure-modes 文档的三类失败——抽象坍塌/级联发现/沉没成本——可以直接映射：沉没成本陷阱 = 高 retract/assert 比）。

#### 维度 4：代码质量（cog 是否让代码更干净）

| 指标 | 来源 | 算法 |
------|------|------|
| 圈复杂度 | `result.json.cc` | high_count/high_mean/max 统计 |
| LOC/SLOC | `checkpoint_results.jsonl.loc/sloc` | 直接读 |
| lint 比例 | `result.json.ratios.lint` | 直接读 |
| 违规率 | `result.json.ratios.violation_pct` | 直接读 |
| LLM judge | `result.json.ratios.rubric`（需跑 `metrics judge`） | 主观质量分 |

### 6.2 离线分析脚本结构

一次 run 跑完后，用脚本把产物聚合成一张对照表。建议放在 `benchmark/slop-code-bench/scripts/` 或 cog 仓库下：

```
analyze_run.py <run_dir>
  ├─ 解析 result.json / checkpoint_results.jsonl     → 任务结果 + 效率
  ├─ 解析 每个checkpoint/agent/stdout.jsonl           → cog 调用计数（轨迹侧）
  ├─ 解析 每个checkpoint/cog_state/usage.jsonl        → cog 调用计数（遥测侧，交叉验证）
  ├─ 打开 cog_state/cog.db                            → 断言覆盖 / changelog 曲线
  └─ 输出 run_summary.json（一张扁平表）
```

**交叉验证价值**：`stdout.jsonl`（agent 实际发出的命令）和 `usage.jsonl`（cog 实际执行的命令）应一一对应。两边对账能发现"cog 命令是否被正确执行"或"agent 是否只问不写"。

### 6.3 期望的对照结论形态

跑完 baseline + cog 两组（最好各多 seed），产出一张表：

```
problem | mode      | coresolved | regression_rate | cost  | tokens | cog_calls | coverage | retract/assert
--------|-----------|------------|-----------------|-------|--------|-----------|----------|---------------
backup  | baseline | 2/4        | 0.80→0.71       | $5.12 | 328K   | -         | -        | -
backup  | cog      | 4/4        | 0.80→0.85       | $5.40 | 345K   | 34        | 0.42     | 0.15
```

这样的表才能回答"cog 到底有没有用、在哪个维度有用、代价是什么"。

---

## 7. 实施清单（分阶段）

> 按依赖顺序排列。每步标注"改动性质"：[cfg]=仅配置 / [code]=需改 Python / [host]=宿主机操作。

### 阶段 0：准备（host）

- [host] 在 cog 仓库根 `cargo build --release`，确认 `target/release/cog` 产出。
- [host] 手动起一个 benchmark 容器，`docker run ... -v target/release/cog:/usr/local/bin/cog ...`，进容器跑 `cog --version`、`cog sync --init`、`cog assert ...`，确认二进制在容器内可用。**这是整个集成的地基，先单独验证**。

### 阶段 1：最小可用（方案 B，零代码改动）

- [cfg] 复制 `configs/agents/claude_code_deepseek.yaml` → `..._cog.yaml`，加 `append_system_prompt`（§5.4 层1）。
- [cfg] 复制 `deepseek_run_smoke.yaml` → `deepseek_run_smoke_cog.yaml`，`agent:` 指向 cog 版 agent config；在 environment config 的 `docker.extra_mounts` 挂载 cog 二进制（§5.3 方案①）。
- [host] 跑 `slop-code run --config deepseek_run_smoke_cog.yaml`，确认：agent 在 stdout.jsonl 里真的调了 cog、`.cog/` 在 workspace 里生成了。
- [host] **手动**把 `.cog/` 从 workspace 临时目录拷出来（阶段 1 先手动验证，不做自动化钩子）。

### 阶段 2：状态捕获自动化

- [code] 在 `worker.py` / runner 的 checkpoint 收尾加 `capture_cog_state()`（§5.5），把 `/workspace/.cog` 拷到 `checkpoint_N/cog_state/`。
- [code] 加 WAL checkpoint（拷贝前确保数据落盘）。
- [host] 重跑，确认每个 `checkpoint_N/cog_state/` 都有 usage.jsonl + cog.db，且内容跨 checkpoint 累积。

### 阶段 3：配置开关沉淀（方案 A）

- [code] `run_config.py` 加 `CogConfig` 字段（§5.2 方案 A），`RunConfig` + `ResolvedRunConfig` 同步。
- [code] 在配置解析/runner 里把 `cog.enabled` 串联到：二进制挂载、append_system_prompt 注入、capture 钩子开关。
- [cfg] 把 `save_template` 改成带 `cog${cog.enabled}` 标记。
- [host] 跑 `cog.enabled: true` 和 `false` 各一次，确认产物目录分离、唯一差异是 cog。

### 阶段 4：分析与对照

- [code] 写 `analyze_run.py`（§6.2）。
- [host] 跑多 problem × 多 seed × {baseline, cog} 全组合。
- [host] 聚合成对照表（§6.3），形成结论。

### 阶段 5（按需）：加强注入

仅当阶段 4 对照显示 cog 组效果不理想时执行——不是预设的对照维度：

- [cfg] 加 `cog-guided.jinja` prompt（§5.4 层3），把 cog 工作流强制嵌入任务流程。
- [cfg] workspace 内放 skill 文件（§5.4 层2），给 agent 完整命令参考。
- 重跑对照，看加强注入后结果是否改善。

---

## 8. 风险与待确认项

### 8.1 技术风险

| 风险 | 影响 | 缓解 |
------|------|------|
| **cog 二进制在容器内跑不了**（glibc/动态库不匹配） | 整个集成失败 | 阶段 0 先手动起容器验证（§5.3 的 glibc 可移植性）；glibc 不匹配就用 musl 静态构建或在匹配环境里编译 |
| **token 开销吃掉 cog 收益** | 实验组 cost 高、看不出收益 | 如实记录，用 cost-per-solved 而非绝对 cost 评判；看质量/收敛是否补偿 |
| **agent 根本不调用 cog** | 实验组和 baseline 无差异，白跑 | 阶段 1 先小规模验证 agent 确实会调；不够就叠加层 3（prompt 模板主注入）和层 2 |
| **agent 乱用 cog**（只问不写、沉没成本陷阱） | cog 组反而更差 | 这本身是有价值的发现（印证 failure-modes）；用 usage.jsonl 的读写比/retract 率诊断 |
| **WAL 未落盘导致 .cog/cog.db 不完整** | 分析时数据残缺 | capture 前 `PRAGMA wal_checkpoint(FULL)` 或连 -wal 一起拷 |
| **snapshot 把 .cog/ 打进提交快照** | 污染 diff 分析 | 把 `.cog/*` 加入 snapshot ignore_globs（只影响提交快照，不影响 cog_state 拷贝） |

### 8.2 实验设计待确认

- **对照的公平性**：cog 组多了一套 cog 工作流指令（即便 baseline 也加等量"中性"指令？）。需决定 baseline 是否给等长安慰剂 prompt，以隔离"指令本身"和"cog 本身"的效果。**建议**：baseline 用原 `just-solve`，cog 组注入 cog 指令；若担心指令长度本身的影响，额外跑一组"等长中性 prompt + 无 cog"作为第二对照。
- **problem 选择**：smoke 先单 problem（file_backup）验证链路；正式对照应覆盖 Easy/Medium/Hard 多个 problem，因为 cog 对"结构变更类"问题最有效（failure-modes §3 正面案例），对"运行时协议类"可能无效甚至负效。
- **多 seed**：单 seed 偶然性大。SCBench 有 variance 机制，正式实验至少 3–5 seed。
- **checkpoint 数**：cog 的价值在长 checkpoint 链累积后才显著（checkpoint 1 大家都差不多）。优先选 checkpoint 多的 problem。

### 8.3 待人工确认

1. **二进制挂载路径**：cog 仓库相对 benchmark 目录的位置，`binary_path` 默认值待定（当前假设 `../../target/release/cog`）。
2. **容器内 PATH**：挂到 `/usr/local/bin/cog` 是否在 agent 的非 root 用户 PATH 里（base 层 `ln -s` 到 `/usr/local/bin`，应可用，但需验证 `agent` 用户）。
3. **snapshot ignore_globs 是否需加 `.cog/*`**：取决于是否想让 `.cog/` 出现在提交代码快照里（一般不想，会污染 diff；但 cog_state 单独拷贝不受影响）。
4. **claude 是否自动读 workspace 的 CLAUDE.md**：实测 benchmark repo 自己有 CLAUDE.md/AGENTS.md，但需确认 `--print` 单轮模式下是否加载（`--print` 可能不走交互式加载逻辑）。若不自动加载，注入只能靠 `append_system_prompt`。

---

## 9. 实施回填（已落地代码对照）

> 本节记录实际实现与 §5/§7 设计的差异，避免文档与源码脱节。
> 状态：**已实施并验证通过**。

### 9.1 修改文件清单

| 文件 | 改动性质 | 内容 |
|------|---------|------|
| `src/slop_code/entrypoints/config/run_config.py` | code | 新增 `CogConfig`；`RunConfig` / `ResolvedRunConfig` 新增 `cog: CogConfig` 字段；支持 `cog: true` 简写 |
| `src/slop_code/entrypoints/config/loader.py` | code | 解析 `cog` 配置；启用时切换 `just-solve` → `cog-guided`、注入 `append_system_prompt`、在 environment dict 里加 `docker.extra_mounts` 挂载 `cog` 二进制到 `/usr/local/bin/cog`、给 `save_template` 加 `_cog` 标记 |
| `src/slop_code/entrypoints/commands/run_agent.py` | code | 使用 `run_cfg.environment` 解析 `EnvironmentSpec`，确保 loader 添加的 `extra_mounts` 真正进入容器创建 |
| `src/slop_code/agent_runner/runner.py` | code | checkpoint 后钩子 `_capture_cog_state` 拷贝 `workspace/.cog` → `checkpoint_N/cog_state/`，并写入 `capture_info.json` |
| `deepseek_run_*.yaml`（5 个） | cfg | 每个文件加一行 `cog: true` |
| `configs/prompts/cog-guided.jinja` | cfg | 新 prompt 模板，把 cog 工作流嵌入任务指令（主注入手段） |
| `run_baseline.sh` / `run_cog.sh` | script | 硬编码 `cog=false` / `cog=true` CLI 覆盖，可选参数 `[CONFIG]` 默认 `deepseek_run_smoke.yaml` |
| `configs/agents/claude_code_deepseek_cog.yaml` | deleted | 已删除，避免重复配置 |
| `deepseek_run_smoke_cog.yaml` | deleted | 已删除，避免重复配置 |

### 9.2 与设计的差异

1. **不复制整套 run config / agent config**。原 §5.2 方案 A 建议 `save_template` 用 `${cog.enabled:0}` 区分目录；实际实现改为在 loader 里动态给模板插入 `_cog` 标记。baseline 与 cog 用**同一份 YAML**，通过 shell 脚本里的 `cog=true`/`cog=false` CLI 覆盖切换，减少文件重复。
2. **CLI 覆盖值是字符串**。`slop-code run cog=true` 里的 `true` 被 OmegaConf 当作字符串 `"true"`，因此 `loader.py` 加了一个 `_coerce_cog_config` 函数，同时处理 `bool` / `str`（`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`） / `dict` 三种形态。
3. **默认二进制路径**。`CogConfig.binary_path` 默认 `../../target/release/cog`（相对 benchmark 根目录）。在 loader 里会按 `Path.cwd()` 解析为绝对路径，并检查二进制存在；不存在时抛出 `FileNotFoundError`。
4. **prompt 被单引号包裹后注入**（workaround）：`claude_code/agent.py` 在把 CLI 参数列表拼成 shell 命令时没有用 `shlex.quote`（`" ".join(cli_args)`），多行 prompt 里的换行/空格会被 `/bin/sh -c` 当成命令分隔，导致 `--append-system-prompt` 参数丢失并出现 `cog: not found` / `A: not found` 等 shell 报错。为了不改 benchmark 的 agent 代码，`loader.py` 在注入 `append_system_prompt` 时先用单引号把整段 prompt 包起来（内部单引号做 ` '\''` 转义）。这样 agent 的 `" ".join` 会产生 `--append-system-prompt '...完整 prompt...'`，shell 会把它作为一个参数传给 Claude，prompt 保持完整的多行文本和空格。`shlex.split` 验证可完整还原原始 prompt。
5. **无需额外 WAL checkpoint 调用**。capture 钩子在容器仍存活时（`session.finish_checkpoint` 之后）直接 `shutil.copytree` 整个 `.cog/` 目录，包含 `cog.db` / `-wal` / `-shm` / `usage.jsonl` / `experiments/` / `workflow_state.json`。
7. **prompt 模板是主注入手段**。原 §5.4 把层 1（`append_system_prompt`）当默认、层 3 当备选；实测 agent 对 system message 里的 cog 指令执行率很低，因此改为**层 3 主 + 层 1 辅**。`loader.py` 在 `cog.enabled=true` 且原 `prompt` 为 `just-solve` 时自动切换为 `cog-guided`，baseline 仍走 `just-solve`。
8. **`env_data` 修改需要被运行命令使用**。最初 `loader.py` 把 `extra_mounts` 写进 `env_data`，但 `run_agent.py` 用 `run_cfg.environment_config_path` 重新从文件解析，导致挂载丢失。已改为 `run_agent.py` 使用 `run_cfg.environment`（加载后的 dict），挂载现在真正生效。
9. **snapshot 仍可能包含 `.cog/`**。当前没有把它加入 `ignore_globs`；如果需要避免污染提交快照，后续再补（不影响 `cog_state/` 单独拷贝）。

### 9.3 验证结果

- `python -c "import ast; ..."`：所有改动的 Python 文件语法通过。
- `uv run python` 加载 `deepseek_run_smoke.yaml`：
  - `cog.enabled=True` → `prompt` 自动切换为 `cog-guided`、解析出的 `EnvironmentSpec.docker.extra_mounts` 包含 `/home/morethan/GitHub/cog/target/release/cog` → `/usr/local/bin/cog` 的挂载、`agent.append_system_prompt` 被注入、`save_template` 含 `_cog`。
  - `cog=false`（CLI 覆盖） → 保持 `just-solve`、无挂载、不注入、目录无 `_cog` 标记。

### 9.4 如何跑对照实验

```bash
# 在 benchmark/slop-code-bench/ 目录下
./run_baseline.sh [deepseek_run_smoke.yaml]   # 无 cog
./run_cog.sh [deepseek_run_smoke.yaml]        # 启用 cog

# 或直接传 CLI 覆盖（效果同上）
uv run slop-code run --config deepseek_run_smoke.yaml cog=false
uv run slop-code run --config deepseek_run_smoke.yaml cog=true
```

产物目录会自动带 `_cog` 或 `_nocog` 区分（若原模板含 `${now:...}`，则变成 `_cog_${now:...}`）。

---

> **文档结束。** 设计部分保留原有分析；实施细节以 §9 和源码为准。
