# DSH-first LoopGraph Supervisor

一个以官方 DeepSeek Harness 为首要运行时、保持 Harness-neutral、支持持久恢复和人工解释的 RSI MVP。

## 项目目标

```text
用户目标
  -> DeepSeek Harness 执行
  -> Verifier 验证
  -> 失败反思和改进提案
  -> 自动重试或 HITL
  -> 验证通过后版本晋级
```

系统不仅记录 Agent 做了什么，还记录 Supervisor 为什么允许这次执行、为什么重试、为什么等待人工、为什么晋级或回滚。

当前提升对象是 Supervisor Python/Policy/Verifier 代码和 Git 管理的 LoopSpec artifact。Supervisor engine 从版本化 JSON 加载 LoopGraph；项目不声称模型训练或无约束泛化。RSI 必须由任务 verifier feedback 或明确人工请求触发，并经过 Graph Gate、验证、PR 和人工晋级。

第一次真实 LoopSpec RSI 晋级记录在 [docs/RSI_V2_EVIDENCE.md](docs/RSI_V2_EVIDENCE.md)：真实 DSH candidate 经过 Graph Gate、独立验证、GitHub PR、人工审批和 `loopspec-rsi-v2` tag。全新机器安装与运行步骤见 [docs/NEW_MACHINE_RUNBOOK.md](docs/NEW_MACHINE_RUNBOOK.md)。

## 官方 DSH SDK

本项目使用官方仓库和 SDK：

```text
https://github.com/deepseek-ai/deepseek-harness
```

推荐使用 locked 环境安装：

```bash
uv sync --extra dev --locked
```

`deepseek-harness-sdk==0.1.1rc1` 被 exact pin，完整 Python 解析记录在 `uv.lock`；B 的 Node 依赖记录在 package-local `package-lock.json`。

## 本地运行 Demo

Fake adapter 必须显式启用，绝不会由 SDK 失败自动降级：

```bash
export DSH_MODE=fake
python -m loopgraph_supervisor.main
curl -X POST http://127.0.0.1:8080/workflows \
  -H 'content-type: application/json' \
  -d '{"id":"demo-1","goal":"produce an artifact","max_attempts":3}'
curl http://127.0.0.1:8080/workflows/demo-1
```

## 使用真实 DeepSeek Harness

```bash
export DSH_MODEL=deepseek-v4-flash
export DSH_SESSION_ROOT="$PWD/.dsh-sessions"
python -m loopgraph_supervisor.main
```

真实 adapter 在 `loopgraph_supervisor/adapters.py` 的 `DeepSeekHarnessAgent`，直接调用官方 `DeepSeekHarness`。SDK 是默认模式；没有依赖、凭据或有效模型路由时会显式失败。

A 把 DSH 当作 Agent runtime，而不是 Chat API adapter。每次真实 run 会保存 bounded runtime facts：session id、SDK/model、finish reason、event/notification 类型统计、工具事件类型、event stream hash 和 workspace changed files。完整 event payload 不写入 Supervisor SQLite；Host verifier evidence 单独保存，不能由 DSH runtime facts 替代。

生产环境不要把 key 写入 shell history 或 Git。真实隔离 Builder 使用仓库外的 mode-0600 relay secret 文件。

## A 版 Web UI 与 CLI

启动 Python Supervisor 后访问：

```text
http://127.0.0.1:8080
```

Web UI 展示 workflow 图、Decision Ledger、验证证据、版本链和 HITL 操作。CLI 使用同一个 HTTP API：

```bash
loopgraph list
loopgraph status <workflow-id>
loopgraph start <workflow-id> "goal" \
  --workspace /absolute/git/repo \
  --verify "pytest -q" \
  --allow src/app.py \
  --allow tests/test_app.py
loopgraph hitl <workflow-id> approve
loopgraph rollback <workflow-id> <version-id>
loopgraph evolve coding-supervisor "Review verifier feedback and propose a bounded policy improvement" --reviewer DDHH
```

可变 coding workflow 默认从 clean source repo 创建独立 worktree，路径位于 `~/.dsh/loopgraph-worktrees`。DSH、验证、candidate commit 和 rollback 都发生在该 workflow 的 worktree 中。

## 用 tmux 同时启动 A/B

```bash
cd /Users/hechuan/Git_repos/loopgraph-supervisor
export DEEPSEEK_API_KEY='你的 key'
./scripts/tmux-dev.sh
```

tmux 窗口：

- `A`：Python Supervisor，`http://127.0.0.1:8080`
- `B`：DSH Web + native plugin，`http://127.0.0.1:3081`
- `logs`：常用检查命令

切换窗口：`Ctrl-b` 后按 `n` / `p`，或按数字。离开但保持运行：`Ctrl-b d`。重新进入：`tmux attach -t loopgraph`。停止全部：`tmux kill-session -t loopgraph`。

也可单独启动：

```bash
./scripts/start-a.sh
./scripts/start-b.sh
```

## API

- `POST /workflows` 创建并执行工作流
- `GET /workflows/{id}` 查询状态、事件、决策和改进提案
- `POST /workflows/{id}/pause` 请求安全暂停
- `POST /workflows/{id}/resume` 恢复执行
- `POST /workflows/{id}/hitl` 提交 `approve`、`retry` 或 `reject`
- `POST /workflows/{id}/rollback` 请求体为 `{"version_id":"..."}`
- `POST /evolution/triggers` 持久化人工 RSI 请求并返回 `EVOLUTION_REQUESTED`

Git workspace 在 workflow 创建时登记 baseline version，晋级时创建 candidate commit。回滚时如果 workspace 没有未提交修改，Supervisor 会切换到目标 commit；如果 workspace dirty，会拒绝回滚，避免覆盖人工修改。

coding workflow 应提供验收契约：

```json
{
  "id": "rsi-sample-1",
  "goal": "修复 calculator.average 对空列表的处理，并运行测试。",
  "max_attempts": 3,
  "acceptance": {
    "workspace": "/absolute/path/to/loopgraph-supervisor/fixtures/rsi-sample",
    "commands": ["pytest -q"]
  }
}
```

`CommandVerifier` 要求至少一个验收命令，并记录 exit code、stdout、stderr 和 Git 证据。Git worktree 会记录 baseline SHA、检查 changed files，并在晋级时创建可恢复的 tagged candidate commit。

代码 candidate 可在 acceptance contract 中启用 Host-owned Test/Coverage Gate：`python_test_gate` 运行 pytest，`coverage_gate` 读取 coverage.py JSON 报告并检查最低 line coverage、branch coverage 和相对 baseline 的 regression。两者的命令、结果和 report hash 都进入 verification evidence。

`loop evolve` 只创建人工 `EvolutionTrigger`；proposal worker 消费后才调用 DSH Builder 并创建 quarantine candidate。它不会直接修改 active LoopGraph、merge 或发布版本。GitHub PR 和 activation adapter 需要显式调用，不会由 API 服务自动产生远程副作用。

完整面试讲解、A/B 总体架构、流程图、Gin 两版本复盘、自研与插件候选取舍矩阵见 [INTERVIEW_DEMO.md](INTERVIEW_DEMO.md)，技术设计和决策理由见 [TRD.md](TRD.md)。

全新 macOS/Linux 机器的安装、Docker gate、A/B 启动、真实 SDK smoke test、停止和故障排查见 [docs/NEW_MACHINE_RUNBOOK.md](docs/NEW_MACHINE_RUNBOOK.md)。

当前硬化计划见 [PLAN.md](PLAN.md)，架构与行为演进见 [CHANGELOG.md](CHANGELOG.md)。未完成项不会视为已交付能力。

DSH-native TypeScript 插件 B 版位于 [packages/dsh-loopgraph-supervisor](packages/dsh-loopgraph-supervisor)，其入口、Cordis seam 和事件模型见 [PLUGIN_TRD.md](packages/dsh-loopgraph-supervisor/PLUGIN_TRD.md)。Python 版 A 作为完整外部 Supervisor reference 保留。

A/B 共享 versioned LoopSpec schema 和 transition vectors。A 使用 Python interpreter/SQLite，B 使用 TypeScript interpreter/checksummed sidecar；B 的 `/loop evolve` 通过当前 DSH Agent runtime 生成下一 revision，并复用现有 Doublecheck、Git scope、HITL 和 recovery 管线。

## 测试

```bash
uv run ruff check .
uv run mypy loopgraph_supervisor tests
uv run pytest -q
cd packages/dsh-loopgraph-supervisor
npm ci
npm run build
npm test
```

CI 使用相同 lockfile 和命令，但不使用 API key，因此真实 DSH E2E 与 deterministic CI 证据分层陈述，fake 单测不作为真实 Harness 运行证明。

详细架构、函数入口、状态转换、数据表和每个关键决策的理由见 [TRD.md](TRD.md)。
