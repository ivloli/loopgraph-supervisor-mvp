# DSH-first LoopGraph Supervisor TRD

版本：v0.5 RSI MVP  
实现语言：Python 3.10+（A）与 TypeScript（B）  
官方运行时：`deepseek-harness-sdk`  
持久化：SQLite WAL  
HTTP：Python 标准库 `http.server`

## 1. 目标与面试要求

项目目标不是实现一次模型调用，而是通过 DeepSeek Harness 构建一个受控的 Recursive Self-Improvement（RSI）最小闭环：Agent 产生候选结果，Verifier 产生证据，Supervisor 根据策略决定重试、请求人工、晋级或回滚。

项目方特别要求“开发者必须清楚代码做了什么以及为什么这么做”。因此本项目有两类事实：

1. DSH 事实：Agent session、工具调用、workspace 修改、最终回复。
2. Supervisor 事实：工作流状态、attempt、验证结果、改进提案、HITL、版本和决策记录。

Supervisor 不把 Codex、Claude Code 或 DSH 当作决策主体。它们是执行工具；Supervisor policy 才决定是否允许下一步。

## 2. 核心术语

### DSH-first

DSH 指 DeepSeek Harness。DSH-first 表示第一优先级直接适配官方 `DeepSeekHarness`，而不是只调用 DeepSeek Chat API，也不是自己重写一个 Agent Runtime。

### Harness-neutral

Supervisor 只依赖 `AgentExecutor` 和 `Verifier` 协议。DeepSeek adapter 是默认实现，未来可替换为 Codex、Claude Code、内部 Agent 或测试 fake。

### RSI MVP

受限的递归式自我改进，不允许 Agent 无边界修改自身。每一轮改进必须经过：验证证据、改进提案、风险判断和必要的人工确认。

本项目的“self”指 Supervisor 的可版本化实现：Python policy/Verifier/orchestration code 与 `LoopSpec` graph artifact；不是 DSH runtime、模型权重或无约束的自我修改。最小递归关系为：

```text
Candidate(n)
  -> independent verifier / quality gate
  -> structured failure evidence
  -> ImprovementProposal(n+1)
  -> same workflow session + human feedback
  -> Candidate(n+1)
```

真实任务反馈或明确人工请求是 RSI 的起点：verifier 失败、回归等 Host-owned evidence 达到触发阈值时，或操作员主动提交改进目标时，才允许创建 `EvolutionTrigger`、`ImprovementProposal` 和后续 candidate。无反馈、无目标的机械 retry 不算 RSI。

在代码演进场景中，`Candidate(n)` 是基于当前 canonical Git parent 的 Supervisor/LoopSpec 修改候选；它必须经过隔离测试、验证、PR review 和人工审批，merge 后才成为 `Candidate(n+1)` 的新 baseline。

### Decision Ledger

结构化决策账本。每个关键状态转换都保存问题、决定、理由、证据、备选方案、风险和预期效果。它不是模型隐藏思维链，而是可验证的决策摘要。

## 3. 设计决策记录

### D-001：选择 Python

决定：Python 作为主开发语言。

理由：官方 DSH SDK 直接提供 Python 包和 `DeepSeekHarness` 入口。若使用 Go，需要通过 Python bridge 或子进程协议间接调用，额外引入 session 映射、进程崩溃、stdout 协议和错误转换问题，这些不是本题重点。

### D-002：Supervisor 不直接依赖 DSH SDK

决定：只有 `adapters.py` 的 `DeepSeekHarnessAgent` 依赖官方 SDK，Supervisor 依赖 `AgentExecutor` 协议。

理由：保持 Harness-neutral；让 fake 测试不需要 API key；让未来替换 Codex/Claude Code 时不修改状态机。

补充：A adapter 必须投影 DSH Agent-runtime facts，而不能只返回 final response。当前保存 session id、finish reason、event/notification type counts、tool activity types、event stream hash 和 workspace changed files；不保存完整 payload，避免把 prompt、工具输出或 secret 复制进 Supervisor audit store。Fake adapter 不生成 `dsh_runtime` evidence。

### D-003：保留 SQLite

决定：DSH session 日志之外，Supervisor 继续维护自己的 SQLite。

理由：DSH 记录 Agent 的对话和工具事实，不能直接表达 workflow 当前节点、验证策略、HITL 决策和版本 active pointer。两套状态职责不同。

### D-004：使用 session_id 作为 Agent 上下文边界

决定：默认 session id 为 `workflow-{workflow_id}`。

理由：retry 时 Agent 能看到同一任务上下文和前一轮反馈；Supervisor 通过 attempt 记录每次执行。若需要隔离实验，可把 attempt 拼入 session id。

### D-005：execution token 幂等

决定：使用 `{workflow_id}:{attempt}` 作为 execution token。

理由：进程崩溃恢复时先检查 attempts 表。已有结果则复用，不重复调用 DSH。外部 workspace 副作用仍需 DSH/adapter 自己提供幂等保证，不能宣称跨系统 exactly-once。

### D-006：安全暂停而非强杀

决定：Pause 先写 `pause_requested`，在节点边界转换成 `PAUSED`。

理由：强行终止正在运行的 DSH 调用可能留下未知 workspace 状态；安全边界更容易恢复和审计。

### D-007：决策记录显式保存理由和证据

决定：每个关键动作都产生 DecisionRecord。

理由：仅保存模型最终文本无法回答“为什么重试/晋级/回滚”。结构化理由便于人审、测试和 API 展示。记录的是理由摘要，不是不可验证的隐藏思维链。

### D-008：Verifier 与 DSH 解耦

决定：Verifier 是独立 port，默认提供 `CommandVerifier`，可执行项目测试并采集 exit code、stdout、stderr 和 diff 证据；未来可替换为静态检查器或另一个 Agent。

理由：Agent 的自我判断不能作为唯一验收标准，验证必须可以由外部证据决定。

### D-009：Git 作为代码 artifact 的版本事实

决定：对于 Git workspace，workflow 创建时记录 baseline SHA 并创建 BASELINE version，晋级时创建 candidate commit，回滚时切换到目标 commit。

理由：DSH 的 final response 只能说明 Agent 的叙述，不能可靠地恢复文件状态。Git commit 可以保存代码快照、变更范围和父版本。把 baseline 也作为 version，candidate 才有明确 parent，回滚才有目标。Supervisor 同时创建 Git tag，避免回滚后 candidate commit 失去可达引用。回滚前如果 workspace dirty（包含 staged 和 untracked 文件），系统拒绝操作，避免覆盖未审计修改。

### D-010：保留 A/B 两种实现边界

决定：A 是 Python 外部 Supervisor，通过官方 SDK 驱动 DSH runtime；B 是运行在 DSH 内部的 TypeScript/Cordis 插件。两者共享 versioned LoopSpec schema、graph semantics 和 transition vectors，但不共享持久化实现。

理由：A 证明 Harness-neutral 外部控制平面和独立 API；B 证明 DSH-native commands、Agent lifecycle 和社区插件组合。把两者混成一个跨语言运行时会增加故障边界，也无法清楚解释职责。

### D-011：验证和晋级必须 fail-closed

决定：模型的 `LOOPGRAPH_RESULT` 只是候选声明。Supervisor 必须独立执行 acceptance commands，要求明确的 gate verdict 和有界 Git scope；B 默认在 commit 前进入 `WAITING_HITL/PROMOTION_REVIEW`。

理由：Agent 自报、缺失 verifier 或未安装插件不能成为成功证据。人工必须在 AI 代码进入 Git 历史前检查 diff、测试和风险。

### D-012：B 的兼容持久化使用 sidecar ledger

决定：DSH Session 保存 Agent/工具历史；B 的 policy facts 保存到 `$DSH_HOME/loopgraph/<session-id>.jsonl`。每个 sidecar 使用独占跨进程 writer lock、单调 sequence 和 SHA-256 前向 checksum chain；append 在返回前 fsync，并在持锁状态下修复 torn final line。旧版无 checksum 记录仍可重放。

理由：已发布的 DSH `0.1.1-rc.2` 会丢弃自定义 log-only event 的 `ignorable` marker，直接写入会导致重启加载失败。后续可迁移到 DSH storage-domain 或确认支持 marker 的 Session API。

### D-013：A Web UI 使用同进程静态 SPA

决定：A 的 Python HTTP 服务直接提供 Web UI 静态资源和 JSON API；CLI 也调用同一 API。Streamlit只作为可选 demo，不作为主控制面。

理由：Streamlit适合快速数据界面，但会引入第二个进程、缓存和状态同步语义。轻量 SPA 可以与现有 API、HITL 和恢复状态共享唯一事实来源。

A Web UI 使用固定侧边栏和六个中文功能页：运行总览、决策账本、验证证据、版本与回滚、活动日志、新建任务。前端只做 API projection 和人工命令提交，不保存 authoritative workflow state。

### D-014：A 的每个可变 workflow 使用独立 Git worktree

决定：共享源仓库只用于创建 baseline；Agent 修改、验证和 candidate commit 在 workflow 独立 worktree 中完成。

理由：clean-check 不能阻止并发 workflow 或人工修改互相污染。A 能控制 SDK cwd，因此已经采用独立 worktree。B 的 `dsh-workflow` seam 仍需 workflow-level lane ownership 和 result-adoption 合同；现有 task-lane adapter 不足以支撑 LoopGraph 验证与晋级，因此 B 当前使用 Git common-dir lock 并对 named delegation fail closed。

## 4. 总体架构

### 4.1 LoopSpec JSON 与执行图

`LoopSpec` 是 Git 管理的声明式有向图。`nodes` 定义安全内核支持的 handler 类型，`edges` 定义唯一的 `source + outcome -> target` 路由。JSON 先经过 schema/结构校验，再经过 `LoopGraphValidator` 的可达性、终止性和治理规则校验，最后才允许进入 registry 或 candidate evaluation。

当前 node kind 穷举：

| kind | 职责 | 允许的核心 outcome |
|---|---|---|
| `dsh_execute` | 调用 Agent/DSH | `pass` |
| `verifier` | 执行独立验证 | `fail`、`retry`、`approve`、`auto_promote`、`exhausted` |
| `human_gate` | 等待人工决策 | `approve`、`retry`、`reject` |
| `promotion` | 执行 Host-owned 晋级 | `pass` |
| `terminal` | 完成/失败终点 | 无 |

当前 outcome 穷举：`pass`、`fail`、`retry`、`approve`、`auto_promote`、`reject`、`exhausted`。具体图可以只使用其中一部分，但所有使用的 route 必须唯一；coding-supervisor profile 还必须包含 `dsh_execute`、`verifier`、`human_gate`、`promotion` 和至少一个 `terminal`。

Graph Gate 必须拒绝：未知 node kind、重复 node id、未知 edge endpoint、重复 `source + outcome`、不存在 entrypoint、不可达节点、无出口的非 terminal、带出口的 terminal、没有 terminal 的图，以及无法到达 terminal 的节点。受 `max_iterations` 约束的 retry cycle 是允许的。

示例 artifact 位于 `configs/loopspecs/coding-supervisor/v1.json`；它表达 `execute -> verify -> retry/HITL -> promotion -> complete`，对应的运行时解释由 `LoopSpecInterpreter` 完成，而不是由 Supervisor Python 硬编码具体图拓扑。

```text
HTTP / CLI
    |
    v
main.build_supervisor()
    |
    +-- SQLiteStore
    +-- DeepSeekHarnessAgent or FakeAgent
    +-- CommandVerifier or explicit FakeVerifier
    |
    v
Supervisor.run()
    |
    +-- EXECUTE -> DSH session
    +-- VERIFY -> evidence
    +-- RETRY -> ImprovementProposal
    +-- HITL -> human decision
    +-- PROMOTE -> Version
    +-- EXPLAIN -> Decision Ledger
```

## 5. 目录结构

```text
pyproject.toml                         项目元数据、SDK 和 pytest 依赖
loopgraph_supervisor/main.py           进程入口和依赖组装
loopgraph_supervisor/api.py            HTTP 路由入口和 JSON 编解码
loopgraph_supervisor/domain.py         状态、节点、输入输出和决策数据类
loopgraph_supervisor/ports.py          AgentExecutor、Verifier 协议
loopgraph_supervisor/adapters.py       DSH 官方 SDK adapter、fake、Verifier
loopgraph_supervisor/store.py          SQLite schema 和 durable repository
loopgraph_supervisor/supervisor.py     LoopGraph 状态机和策略
tests/test_supervisor.py               无密钥行为测试
```

## 6. 代码入口和调用链

### 6.1 进程入口：`main.py:main`

执行 `python -m loopgraph_supervisor.main` 时进入 `main()`：

1. 调用 `build_supervisor()`。
2. 创建 `SQLiteStore`。
3. 默认创建 `DeepSeekHarnessAgent`；只有显式 `DSH_MODE=fake` 才使用 FakeAgent。
4. SDK 模式使用 `CommandVerifier`；fake 模式仅用于无密钥测试。
5. 创建 `Supervisor`。
6. 调用 `api.serve()` 启动 HTTP server。

### 6.2 依赖组装：`main.py:build_supervisor`

这是唯一决定具体 adapter 的入口。默认 `DSH_MODE=sdk`；fake 是显式 demo/test 选项，SDK 错误不会自动转成模拟成功。

### 6.3 HTTP 入口：`api.py:APIHandler.do_POST`

路由映射：

```text
POST /workflows                       -> Supervisor.start
POST /workflows/{id}/pause             -> Supervisor.pause
POST /workflows/{id}/resume            -> Supervisor.resume
POST /workflows/{id}/hitl              -> Supervisor.decide_hitl
POST /workflows/{id}/rollback          -> Supervisor.rollback
```

`api.py:APIHandler.do_GET` 将 `GET /workflows/{id}` 映射到 `Supervisor.explain`，返回 workflow、decision、proposal 和 event，满足“项目方能看懂发生了什么”。

### 6.4 创建入口：`Supervisor.start`

1. 创建 `Workflow`，初始节点是 `AGENT_EXECUTE`。
2. 写入 durable workflow。
3. 写入 `workflow_started` event。
4. 写入 PLAN DecisionRecord，解释为什么开始。
5. 调用 `run`。

### 6.5 执行入口：`Supervisor.run`

这是恢复入口和主循环。它每轮重新从 SQLite 读取 workflow，避免只依赖内存对象，然后按 `current_node` 分派：

```python
AGENT_EXECUTE   -> _execute
VERIFY          -> _verify
VERSION_PROMOTE -> _promote
```

当状态是 `PAUSED`、`WAITING_HITL`、`COMPLETED` 或 `FAILED` 时退出。服务重启后再次调用 `run(id)` 即可恢复。

### 6.6 Agent 节点：`Supervisor._execute`

1. 增加 attempt。
2. 生成 execution token。
3. 查询 attempts 表。
4. 如果已有结果，写 RECOVER 决策并复用。
5. 如果没有结果，构造 `AgentInput` 并调用 `AgentExecutor.execute`。
6. 保存 artifact、session id 和 request。
7. 推进到 VERIFY。

### 6.7 DSH 入口：`DeepSeekHarnessAgent.execute`

1. 延迟导入 `deepseek_harness.DeepSeekHarness`。
2. 使用 `provider`、`model`、`cwd`、`session_root`、`cordis` 创建官方 Harness。
3. 生成 `session_id=workflow-{workflow_id}`。
4. 将 goal、验证反馈和改进提案拼成下一轮 prompt。
5. 调用 `harness.run(prompt, session_id=session_id)`。
6. 把 `result.final_response` 转成 `AgentOutput`。

延迟导入的理由：fake 测试和代码审查不需要安装 SDK 或 API key；只有真实 SDK 模式才加载外部依赖。

### 6.8 验证节点：`Supervisor._verify`

1. 按 execution token 读取 attempt。
2. 调用 `Verifier.verify`。
3. 保存 verification evidence。
4. 通过：生成 VERIFY_PASS 决策，进入 PROMOTE。
5. 未通过但还有额度：创建 ImprovementProposal，生成 RETRY 决策，回到 EXECUTE。
6. 达到上限：创建 HITL request，生成 HITL_REQUIRED 决策，进入 WAITING_HITL。

### 6.9 改进提案：`ImprovementProposal`

它描述问题、假设、修改动作、预期证据和风险等级。提案不是自动执行脚本，而是让人和 Supervisor 都能理解下一轮要改变什么。

### 6.10 晋级节点：`Supervisor._promote`

1. 读取已验证 attempt。
2. 生成新 Version，parent 指向旧 active version。
3. 保存 artifact。
4. 更新 active version。
5. 将 workflow 置为 COMPLETED。
6. 写 state transition event。

### 6.11 HITL 入口：`Supervisor.decide_hitl`

1. 查找未解决 HITL request。
2. 校验决定只能是 approve/retry/reject。
3. 先记录人工决定。
4. approve 跳转 PROMOTE。
5. retry 跳转 EXECUTE。
6. reject 跳转 FAILED。
7. 写 HITL DecisionRecord。

### 6.12 回滚入口：`Supervisor.rollback`

验证目标 version 属于当前 workflow，读取其中的 `candidate_commit` 或 `baseline_commit`，确认 workspace 没有未提交修改，然后执行 `git switch --detach <target_commit>`。最后更新 active pointer，保留旧版本和 rollback event。系统不会使用无条件 `reset --hard` 覆盖 dirty workspace。

### 6.13 解释入口：`Supervisor.explain`

聚合 workflow 当前状态、events、decisions 和 proposals。它是面试演示中最重要的查询：项目方可以从一个响应看清系统做了什么以及为什么做。

## 7. 状态机

```text
AGENT_EXECUTE
      |
      v
VERIFY -- pass --> VERSION_PROMOTE --> COMPLETED
  |
  +-- fail + budget --> ImprovementProposal -> AGENT_EXECUTE
  |
  +-- fail + exhausted --> WAIT_HITL / WAITING_HITL
                                      |
                        approve ------+--> VERSION_PROMOTE
                        retry ---------+--> AGENT_EXECUTE
                        reject --------+--> FAILED
```

## 8. SQLite 数据模型

`workflows` 保存当前 durable 状态。  
`workflow_contracts` 保存 workspace、验收命令和任务约束。  
`events` 保存追加式状态历史。  
`attempts` 保存一次 DSH 调用及 execution token。  
`verifications` 保存 verifier 结果和证据。  
`decisions` 保存问题、理由、证据、备选、风险和预期效果。  
`proposals` 保存 RSI 改进提案。  
`hitl_requests` 保存人工请求和决定。  
`versions` 保存 artifact 版本链和 active pointer；Git workspace 版本会额外保存 `candidate_commit`。

## 9. 恢复语义

1. 服务启动并连接同一个 SQLite 文件。
2. 读取 workflow 的 current node。
3. RUNNING workflow 调用 `Supervisor.run`。
4. EXECUTE 节点按 execution token 查询 attempt。
5. 已保存结果则不重复调用 DSH。
6. VERIFY 节点继续验证已保存 artifact。
7. WAITING_HITL 不自动执行，只等待人工决定。
8. 终态不再执行。

## 10. 可解释性要求

每个 DecisionRecord 至少包含：

- question：系统要回答的问题
- decision：实际选择
- rationale：简洁理由
- evidence：验证、事件或状态证据
- alternatives：未选择方案及原因
- risk：选择的风险
- expected_effect：希望产生的效果

这让代码不是“调用 Codex 然后相信结果”，而是“调用 DSH，验证结果，再根据显式策略决定下一步”。

## 11. 安全与边界

- 不把模型隐藏思维链当作唯一审计依据，只保存可复核的决策摘要。
- 真实 DSH workspace 应使用隔离目录或 disposable checkout。
- `danger-full-access` 组合必须在隔离 workspace 运行。
- DSH session 日志和 Supervisor SQLite 都可能包含敏感内容，应纳入访问控制。
- 当前实现是单进程单 worker；多实例需要 lease/CAS。

## 12. 测试和面试演示

已有测试覆盖：成功重试、HITL reject、解释记录和 terminal pause flag。

面试演示顺序：

1. 用 fake 模式创建 workflow。
2. 展示第一次验证失败。
3. 展示 Decision Ledger 中的 RETRY 原因和 evidence。
4. 展示第二次验证通过和 VERIFY_PASS。
5. 展示 version promotion。
6. 用 `GET /workflows/{id}` 展示完整解释链。
7. 再演示达到上限后的 HITL。
8. 最后说明把 fake adapter 切换成 `DeepSeekHarnessAgent` 只改变依赖组装，不改变 Supervisor。

状态事件必须包含 `from_node`、`to_node`、attempt 和 status，不能只记录目标节点；这样恢复和审计可以还原真实状态转换。

## 13. 真实 Coding Task 验收

`fixtures/rsi-sample` 是一个隔离的最小任务：`calculator.average([])` 当前会抛出除零异常，测试要求返回 `0`。

创建 workflow 时传入：

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

Supervisor 会把 acceptance contract 写入 `workflow_contracts`，同时传给 DSH 和 `CommandVerifier`。Verifier 记录测试命令的 exit code、stdout、stderr 和 `git diff --check`。只有真实测试通过，才能进入 `VERIFY_PASS` 和版本晋级。

## 14. Native Plugin B 版本

Python 版 A 保留为完整外部 Supervisor reference。TypeScript 插件 B 位于 `packages/dsh-loopgraph-supervisor`，不是把 Python 代码机械翻译成 TypeScript，而是使用 DSH 原生扩展点：

B 的 ledger、operation lock、Doublecheck、Git scope、HITL 和 recovery 仍由原生 plugin 实现；graph authority 已迁移到与 A 相同的 LoopSpec JSON。B 的 TypeScript loader/interpreter/validator 读取 active spec，`/loop evolve` 让当前 DSH Agent 只生成下一 revision，plugin 验证 predecessor 后才允许进入现有晋级管线。

```text
ctx.commands.register('/loop')
ctx.on('agent/created' / 'agent/disposed')
ctx.on('session/event')
agent.followup(...)
agent.cancel(..., { keepInbox: true })
$DSH_HOME/loopgraph/<session-id>.jsonl
```

B 版使用 DSH 的 Agent、Command 和 Session 生命周期 seam，但鉴于已发布的 DSH `0.1.1-rc.2` 会丢弃自定义 log-only event 的 `ignorable` marker，状态、决策和证据写入 `$DSH_HOME/loopgraph/<session-id>.jsonl`。每次 append 在 per-session 跨进程锁内分配 sequence、链接前一条 checksum、写入并 fsync；恢复会验证完整 checksum chain，并仅容忍和修复 torn final line。DSH Session 只保存 Agent/工具历史，WeakMap 只做 live cache，恢复时通过 `foldState()` 重放 sidecar ledger。这样既不污染官方 Session 格式，也保持 DSH-native 控制入口。

社区插件的核实结果和边界写在 `packages/dsh-loopgraph-supervisor/PLUGIN_TRD.md`：`dsh-workflow` 负责 workflow capsule/run 产品，`dsh-doublecheck` 负责交付质量门禁，`dsh-background-agents` 负责 continuable 子 Agent，`dsh-flowglass` 负责可视化，`@necokeine/dsh-git` 负责 Web Git 面板。它们都是可选能力，不替代本项目的 LoopGraph policy、Decision Ledger 或版本策略。

完整组合安装脚本是 `packages/dsh-loopgraph-supervisor/examples/install-all-plugins.sh`。生产环境应把 `main` 分支改成经过审查的 commit/tag，并先确认 DSH compatibility matrix。`dsh-background-agents` 还要求 profile 中存在 continuable subagent provider；没有 provider 时插件会 fail loud，不能假装已经具备后台恢复能力。

B 版端到端验证手册是 `packages/dsh-loopgraph-supervisor/E2E_RUNBOOK.md`。特别注意：官方 headless profile 默认没有 `ctx.commands` adapter，因此 `/loop` 命令必须通过 Web E2E 或官方源码 test harness 的 `ctx.commands.execute()` 验证；不能把 headless CLI 的普通任务入口误当成 slash command 入口。

## 15. Hardening Decisions

### D-015：外部执行采用 durable intent + at-least-once

A 在调用 DSH 前写入 execution intent 和 attempt/token；恢复时重用同一身份。它避免悄悄跳到新 attempt，但不能把跨进程 DSH/workspace 副作用描述为 exactly-once。Git promotion 使用确定性 tag 和 HEAD subject reconciliation，允许 commit 后、version 落盘前的恢复。

若重启发现 `STARTED` intent 无 durable result，A 不再自动调用 DSH，而是进入 `UNCERTAIN`。Web/API/CLI 提供 `verify-existing`、`retry-same-attempt`、`restore-baseline`、`abort-preserve` 四种显式恢复策略。

### D-016：A 使用 worktree，B 使用 workspace lock

A 能控制 SDK 的 cwd，因此每个 workflow 创建独立 detached worktree。B 运行在现有 DSH session cwd 中，直接模式使用 Git common-dir lock 阻止不同 session 同时修改。`dsh-workflow` named delegation 当前 fail closed；重新启用前必须实现 workflow-level isolation、terminal outcome、retry/pause/resume、restart reconciliation 和 result adoption。

### D-017：A UI、CLI 和 API 共用控制平面

A 的 Web UI 是 Python HTTP 服务托管的静态 SPA，CLI 是同一 JSON API 的客户端。没有引入 Streamlit 作为第二个状态进程；Streamlit仍可作为只读 demo adapter，但不拥有 workflow 状态。

### D-018：版本必须属于 workflow

A rollback 通过 `(workflow_id, version_id)` 查询。B 为每个 start 生成唯一 workflow id，登记 baseline/promoted/rollback version；`/loop rollback` 只接受当前 workflow registry 中的 version id，不接受任意 SHA。

### D-019：Reject 补偿与 Retry 递归语义

决定：`retry` 保留 candidate diff，归纳 Gate 报告和人工反馈为结构化 Improvement Proposal，并注入下一 attempt；`reject` 归档报告后，将 contract 允许的 candidate path 恢复到 baseline，删除已归档的已知临时报告，再释放 workspace lock。

理由：retry 是 RSI 的递归改进边；reject 是终止边。若 reject 后仍把 AI 修改留在 workspace，失败状态会污染下一 run；若直接删除报告而不归档，则失去可解释证据。范围外未知修改会阻止自动补偿，避免覆盖人工工作。

### D-020：B 的不确定恢复使用 DSH User Questions

决定：B 重启时若 sidecar 状态仍为 RUNNING，转为 `UNCERTAIN/UNCERTAIN_RECOVERY`，通过 `ctx.userQuestions` 弹出“验证现状 / 同 Token 重试 / 恢复 Baseline / 终止并保留现场”四个按钮；无 UI provider 时显示 `/loop recover <action>` 备用命令。

理由：恢复不确定 attempt 需要由人选择风险策略。把这些策略做成 UI 选项可以避免操作者必须记忆命令，同时仍保留可自动化的 command surface。

### D-021：B 的控制操作和异步验证必须防止 stale transition

决定：`start/pause/resume/retry/recover/approve/reject/rollback` 通过 per-session operation lock 串行化。Acceptance、Gate 和 Git scope 等外部 await 返回后，必须再次确认 workflow id、attempt、status 和 node 仍匹配；否则丢弃 stale result。`resume` 只接受 `PAUSED`，`rollback` 只接受 `COMPLETED`。

理由：单条 ledger append 的 writer lock 只能防止 JSON 行交错，不能把多事件状态转换变成原子操作。没有 operation lock 和 await 后状态守卫时，人工 pause/reject 可能被旧 verifier 结果覆盖。

### D-022：人工晋级必须绑定被审查的 spec 和 candidate

决定：A/B 在进入 `PROMOTION_REVIEW` 时保存 attempt、canonical spec revision 和 candidate content fingerprint。Approve 在持锁状态下重新计算并比较；验证失败类 HITL 不允许 approve。

理由：只检查 `WAITING_HITL` 会形成 confused-deputy 漏洞。Gate 后若 allowed file 被人工或其他进程修改，旧审批不能授权新内容进入 Git 历史。

### D-023：DSH 异常默认视为 unknown outcome

决定：A 在 DSH 调用抛错且 durable result 尚未写入时保留 `STARTED` intent，进入 `UNCERTAIN`，不自动创建下一 attempt。

理由：连接断开不等于远端未执行。把普通异常标记为可安全重试会重复外部副作用；只有人工接受风险或 verifier 确认现状后才能继续。

### D-024：先建立 LoopSpec 与 Candidate 合同，再接入 DSH Builder

决定：Phase 1 先在 A 中完成 immutable LoopSpec、deterministic interpreter、revision registry 和 route-level validation。Phase 2 先定义四类 candidate manifest 与 proof binding，再允许 DSH Builder 生成候选。

理由：没有稳定的候选身份、前驱版本和独立 proof，Builder 生成的修改无法区分“改进 Supervisor”与“改了业务 fixture”，也无法安全恢复或复盘。

Phase 2 当前已实现 candidate manifest、proof contract、quarantine registry、LoopGraph artifact loader 和 semantic validation gate；真实 active candidate activation 仍需人工晋级路径。

第一次真实 LoopSpec RSI promotion 使用 `loopspec-rsi-v1 -> PR #1 -> loopspec-rsi-v2`，公开证据见 `docs/RSI_V2_EVIDENCE.md`。该证据证明一次受约束的 policy evolution，不证明模型训练或任意 Supervisor Python code evolution。

### D-026：LoopGraph 必须是版本化 artifact，不得 hard-code

决定：Supervisor Python 只提供通用 LoopSpec loader、interpreter 和安全 handler registry。节点、边、outcome 路由和 iteration budget 存放在 Git 管理的 `configs/loopspecs/<target>/<revision>.json` 中，并通过 immutable hash、predecessor binding 和 active revision 管理。

理由：如果流程图写死在 Python 分支中，DSH 无法独立提议、比较、验证或回滚图变更；同时也会把业务流程和安全内核混在一起。artifact 可演进，但未知 node kind、无限循环、绕过 HITL 和直接修改 active pointer 仍由 Host 内核拒绝。

### D-027：RSI 必须由真实任务反馈触发

决定：Supervisor 只有在真实任务执行产生持久化 verifier feedback，并满足 Host-owned failure/feedback threshold 时，才创建 evolution trigger。trigger 必须携带 workflow、attempt、反馈和 target；没有任务证据不能启动 RSI。

理由：自我改进必须有外部可验证的失败或改进信号。否则 DSH 只是无目标地重写自身，无法判断候选是否解决了真实问题，也无法区分 RSI 与普通 retry。

补充：人工可以绕过自动 failure threshold 主动创建 `human_feedback` trigger。该 trigger 必须持久化 reviewer 和 comment，并只启动 DSH candidate proposal；它不能绕过 Graph Gate、验证、PR 或人工 approval。

### D-030：人工 feedback 与 approval 分离

决定：人工 `evolve`/PR comment 是新的 candidate 请求；人工 `approve` 是对已验证具体 candidate head 的晋级授权。两者使用不同的事件和状态，不能把 feedback 当成 approval，也不能让 DSH 自己批准。

理由：人工反馈可以纠正目标、约束和假设，允许及时进入 RSI；但发布权必须绑定 candidate id、parent commit、candidate head、proof 和当前 PR 状态。candidate 重新生成或 PR head 改变后，旧 approval 自动失效。

### D-031：`/loop evolve` 持久化人工 RSI 请求

决定：A 提供 `POST /evolution/triggers` 和 CLI `evolve` 入口。请求包含 target、reviewer 和 comment，由 `EvolutionTriggerStore` 持久化为 `human_feedback` trigger，并返回 `EVOLUTION_REQUESTED`。`EvolutionProposalWorker` 消费 pending trigger 后调用 DSH Builder 并创建 quarantine candidate；入口和 worker 都不修改 active spec、不创建 Git commit，也不授予晋级权限。trigger 状态为 `PENDING -> PROCESSING -> CONSUMED/FAILED`。

理由：人工可以在观察到单个问题时主动要求 RSI，不必等待自动 failure threshold；但“请求重新提出 candidate”和“批准具体 candidate head”必须保持独立。worker 重新读取当前 canonical baseline，并沿用 Graph Gate、验证、PR 和 approval 约束；正常路径不会重复消费同一 trigger，worker 崩溃后的 DSH 外部调用仍是 at-least-once，需要显式 requeue/reconciliation。

### D-028：Supervisor 自身代码通过 Git/PR 演进

决定：正式 RSI MVP 的 target 是 Supervisor 主仓库中的 Python/LoopSpec artifact。DSH 只生成 candidate，Host 负责隔离 worktree、测试、proof、canonical parent、PR head 和审批绑定；人工 review PR 后才允许 merge/tag/activation。DSH runtime 和模型权重不作为 candidate 修改。

理由：真实版本、审查、可观察性和回滚都必须落在 Git 历史中。多个 AI proposal 可以探索，但同一 parent 下只有经过 Host coordination 的一个 candidate 能成为正式 PR；PR head 变化会使旧审批失效。

### D-029：LoopSpec 必须通过完整图语义 Gate

决定：所有从 JSON 加载或写入 `LoopSpecStore` 的图都必须通过 `LoopGraphValidator`。Gate 除了 JSON/schema 和局部结构，还检查 entrypoint 可达性、全节点可达性、terminal 规则、非 terminal 出口、终止路径、node kind 必要 outcome，以及 coding-supervisor 所需的 verifier/HITL/promotion 节点。

理由：合法 JSON 不等于可执行或治理安全的图。LoopGraph 可以包含受 `max_iterations` 约束的 retry cycle，但不能包含孤立节点、无 terminal 图、死节点、terminal 出口或缺少人工治理路径的 coding-supervisor 图。validator 在 artifact load 和 registry save 两处执行，避免候选或晋级绕过语义校验。

### D-025：DSH Builder 只允许 live-gated Docker 路径

决定：永久禁用 host-process Builder。真实 Builder 必须使用 exact-version Linux runtime、当前 Docker daemon/image 的 live isolation gate、internal-only network 和固定 DeepSeek relay。历史 receipt 只作为审计证据，不作为执行授权。

理由：无密钥 receipt、任意 network 参数、默认 bridge relay 和容器内真实 API key 都可形成绕过。现在 runtime 只能绑定当前 active relay 对象；relay 使用专用双网络并从只读 secret mount 注入凭据，Builder 只看到 placeholder key。

补充：Phase 2 已加入 host-owned `HoldoutProvider` 边界。Candidate intake 不接收 canary 内容；评测时由 Host 独立获取 validation/canary tasks 并生成 proof。当前隔离是接口边界，尚未升级为独立进程和不可读文件系统边界。

## 16. Remaining Failure Boundaries

- DSH 已产生外部副作用、结果尚未落盘时，恢复仍是相同 token/session 的 at-least-once 重试。
- A 的 SQLite 状态和 Git commit 不是一个分布式事务；确定性 tag reconciliation降低了该窗口，但仍需故障注入测试。
- `dsh-workflow` 提供 `WorkflowRun.done/show/subscribe` seam，但完整生命周期和 workspace adoption 尚未实现；`workflowName` 配置因此明确 fail closed。
