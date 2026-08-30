# B 版端到端验证手册

## 0. 为什么需要两条路径

DeepSeek Harness 的 `/loop` 是通过 `ctx.commands` 注册的 human command。官方 `headless` profile 是 one-shot runner，默认没有 command adapter。因此：

- Web E2E 验证真实 `/loop` 命令、HITL 和 UI 插件组合。
- 官方源码 test harness 验证插件在真实 Cordis/Agent/Session 运行时中的加载和事件持久化。
- Python A 版继续验证完整外部控制平面。

不能把 `dsh --profile headless "/loop start ..."` 当作 B 版测试，它不会经过 command registry。

## 1. 预检查

```bash
cd /Users/hechuan/Git_repos/loopgraph-supervisor/packages/dsh-loopgraph-supervisor
npm install
npm run build
npm test
```

预期：TypeScript 编译成功，ledger 单元测试通过。

## 2. 安装 DSH 与插件

如果本机没有全局 `dsh`，使用官方安装方式得到 CLI，再执行：

```bash
dsh plugin --profile web add ./packages/dsh-loopgraph-supervisor
dsh plugin --profile web add dsh-doublecheck
dsh plugin --profile web add dsh-flowglass
dsh plugin --profile web add @necokeine/dsh-git
dsh --profile web --dump-config
```

如果要加入 workflow 和 background agents：

```bash
dsh plugin --profile web add "github:omdsh-dev/dsh_workflow#main"
dsh plugin --profile web add dsh-background-agents
```

`dsh-background-agents` 需要 profile 已经提供 continuable subagent provider。生产环境不要直接依赖 `main`，应替换为审查过的 commit。

## 3. Web 真实 E2E

设置 API key 后启动 Web：

```bash
export DEEPSEEK_API_KEY=sk-...
dsh web --no-open
```

在 DSH Web 会话中执行：

```text
/loop start {"goal":"修复 fixtures/rsi-sample 的失败测试，只修改 calculator.py","maxAttempts":2,"acceptance":{"commands":["python -m pytest -q"],"allowedFiles":["calculator.py"]}}
```

验证命令：

```text
/loop status
/loop logs
/loop explain
```

需要看到：

```text
RUNNING -> EXECUTE
LoopGraph ledger: PLAN
LoopGraph ledger: EXECUTE
assistant/message
LoopGraph ledger: evidence
LoopGraph ledger: VERIFY / PROMOTE
```

若挂载 `dsh-doublecheck`，`/loop` 会调用 `/gate run`，解释链中应出现 `doublecheck_gate` evidence。若 workspace 为 Git 仓库，应出现 `git_scope` 和 `git_candidate` evidence。

## 4. Pause / Resume E2E

在 DSH Agent 正在工作时执行：

```text
/loop pause
/loop status
/loop resume
/loop retry
/loop explain
```

预期：

```text
PAUSED
  -> agent.cancel(..., { keepInbox: true })
  -> RUNNING
  -> 新一轮 DSH followup
```

这验证的是协作式暂停，不是强杀进程。DSH 可能已经完成当前 tool call，Supervisor 在边界处暂停。

## 5. HITL E2E

成功晋级场景先让 acceptance、Gate 和 Git scope 全部通过，确认状态为 `WAITING_HITL/PROMOTION_REVIEW`，然后执行：

```text
/loop status
/loop explain
/loop approve 已检查 diff、测试和启动行为，同意晋级
```

预期状态和证据：

```text
WAITING_HITL/PROMOTION_REVIEW -> COMPLETED/PROMOTE
git_candidate.humanComment 保存完整评论
HUMAN_APPROVE_PROMOTE evidence 包含 human_comment
```

失败审查场景让 DSH 返回以下 marker 并耗尽 retry budget：

```text
LOOPGRAPH_RESULT: {"status":"fail","summary":"verification evidence is incomplete"}
```

确认状态为 `WAITING_HITL/FAILURE_REVIEW`；该状态必须拒绝 `/loop approve`，只允许人工选择 retry 或 reject：

```text
/loop retry 补齐独立验证证据
/loop reject
```

## 6. Rollback E2E

从 `git log` 取得目标 commit：

```bash
git -C fixtures/rsi-sample log --oneline -5
git -C fixtures/rsi-sample status --short
```

在 Web 会话执行：

```text
/loop rollback <commit-sha>
```

预期：

- dirty workspace 被拒绝
- clean workspace 切换到目标 commit
- 写入 ROLLBACK DecisionRecord
- 写入 commit evidence

## 7. 重启恢复 E2E

1. 启动 Web 并执行 `/loop start`。
2. 在 DSH 有 session 日志后关闭进程。
3. 用同一个 profile 重启 DSH。
4. 重新打开原 session。
5. 执行 `/loop explain`。

验证 DSH Session 正常加载，并且插件通过 `foldState()` 从 `$DSH_HOME/loopgraph/<session-id>.jsonl` 恢复，而不是依赖旧进程的 WeakMap。

## 8. 官方源码无 key 自动化路径

官方仓库已有 `examples/headless-agent/tests/harness.ts`，它用真实 Cordis、Agent Loop、Session、Bash 和 JSONL persistence 组装测试 harness。把本插件加入该 harness 后，测试应：

1. mount `dsh-loopgraph-supervisor`
2. `ctx.agentLoop.create(...)`
3. 通过 `ctx.commands.execute(agent, '/loop start ...')` 直接调用 command registry
4. 等待 `agent/status` 变为 `idle`
5. 检查 `$DSH_HOME/loopgraph/<session-id>.jsonl` 中的 state/decision/evidence
6. 等待 `agent/turn-stopping` 触发后处理，再检查 VERIFY/PROMOTE evidence
7. flush persistence
8. 恢复 DSH session，再 fold LoopGraph sidecar ledger

The LoopGraph JSONL ledger under `$DSH_HOME/loopgraph` must survive restart and reconstruct the same state. DSH Session reload must succeed independently; custom unmarked events must never be written into the Session log.

这条路径不需要 API key 时可以使用官方 LLM replay/testkit；需要真实模型时再由 `DEEPSEEK_API_KEY` 开启真实测试。

## 9. 最终验收清单

- B 包 `tsc` 构建通过
- B 包单元测试通过
- Web profile 能加载插件
- `/loop start` 能进入 DSH Agent
- LoopGraph sidecar ledger 写入成功
- `/loop explain` 可重放决策
- doublecheck gate 可被调用
- Git scope/candidate evidence 可见
- pause/resume 可用
- HITL approve/reject 可用
- rollback dirty workspace 会拒绝
- rollback clean workspace 会切换 commit
- DSH 重启后可从 sidecar ledger 恢复，且 Session 历史正常加载
