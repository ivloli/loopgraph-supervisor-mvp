# DSH Native Plugin TRD

## 1. Positioning

This package is implementation B: a native TypeScript Cordis plugin mounted inside DeepSeek Harness. Implementation A remains the Python external Supervisor and is not replaced.

The plugin uses official DSH extension seams instead of reimplementing the agent runtime:

```text
DSH agent loop / tools / workspace / session persistence
                    |
                    v
        dsh-loopgraph-supervisor plugin
                    |
        LoopGraph state + Decision Ledger
```

## 2. Why the plugin exists

The Python version proves the full control-plane semantics independently. The plugin version proves the same concepts can be hosted in DSH itself and can observe the exact session and agent that perform the work.

Keeping both versions is deliberate: the state machine remains testable and Harness-neutral, while the plugin adds DSH-native commands, events, and UI integration points.

## 3. Official extension points used

`ctx.commands.register()` registers `/loop` as a direct human command. Command output does not become a model message unless the handler explicitly calls `agent.followup()`.

The exported `Config` is a Schemastery schema. This is required by Cordis so `maxAttempts`, `requirePromotionApproval`, and optional `workflowName` can be validated and changed from `cordis.yml` without editing TypeScript.

`ctx.on('agent/created')` and `ctx.on('agent/disposed')` attach and release the in-memory index for live agents.

`ctx.on('session/event')` observes DSH session activity without replacing the official agent loop. Candidate post-processing is triggered from the official `agent/turn-stopping` seam, where the final assistant message for the turn is stable; this avoids performing async verification from the fire-and-forget session event feed.

The published DSH runtime cannot safely persist custom log-only events, so the plugin does not extend `SessionEventMap` in this compatibility version. DSH Session remains authoritative for Agent/tool history; the sidecar JSONL ledger is authoritative for LoopGraph policy state.

`agent.followup()` queues the actual DSH work. `agent.cancel(..., { keepInbox: true })` implements cooperative pause: the active turn is cancelled, but pending work is retained.

`workflowName` remains a reserved configuration seam. `/loop start` currently fails closed when it is set: consuming `dsh-workflow.startNamed()` safely requires terminal outcome correlation, mode-preserving retries, external pause/resume, restart reconciliation, shared-workspace locking, and an explicit isolated-result adoption contract. Direct current-Agent execution remains the supported path.

## 4. Durable event model

```ts
interface LoopGraphEvent {
  workflowId: string
  kind: 'state' | 'decision' | 'evidence'
  state?: Partial<LoopState>
  decision?: DecisionRecord
  evidence?: Record<string, unknown>
}
```

The event is log-only, so it does not pollute the model-visible message history. Stored records add a monotonic sequence, the previous record checksum, and a SHA-256 checksum. A per-session exclusive writer lock serializes append operations across processes; each append is fsynced before the lock is released. `foldState()` verifies and replays the chain, accepts legacy records written before checksums were introduced, and repairs only a torn final line before a subsequent append. The WeakMap is only a live cache; it is never the source of truth.

## 5. Function entry points

`src/index.ts:apply(ctx, config)` is the plugin entry loaded by Cordis. It registers event listeners and `/loop`.

`src/index.ts:getState(agent)` reads the live cache and folds persisted events.

`src/index.ts:transition(agent, state, next, reason)` writes a state event and an evidence event. It is the only state transition helper.

`src/index.ts:emitDecision(agent, state, record)` writes a Decision Ledger event and updates the cache.

`src/index.ts:parseStart(rawInput, defaultMaxAttempts)` parses text or JSON command input into a goal, retry budget, and acceptance contract.

`src/index.ts:workflowPrompt(state)` converts durable workflow state into the next explicit DSH instruction.

`src/index.ts:/loop handler` is the human-control entry for start/status/logs/explain/pause/resume/approve/reject/rollback. `logs` returns a compact projection of the latest durable records (20 by default, 50 maximum) so users do not need a browser ZIP and the Web UI is not asked to render recursive state snapshots.

Mutating commands are serialized by a per-session operation lock. Long-running acceptance, Gate, and Git-scope awaits re-check workflow id, attempt, status, and node before applying their results, so a pause or newer transition cannot be overwritten by stale verification output.

`src/ledger.ts:appendLoopEvent()` appends one sequenced, checksummed JSON line under `$DSH_HOME/loopgraph/<session-id>.jsonl` while holding the sidecar's cross-process writer lock.

The current published DSH `0.1.1-rc.2` runtime drops the `ignorable` option passed to `Session.append()` for log-only custom events, which makes such sessions fail strict reload. Therefore this compatible MVP keeps DSH's native Session as the Agent/tool history and stores LoopGraph policy facts in a separate durable JSONL ledger. A future adapter can move these facts into DSH storage-domain or marked session events when the host capability is verified at runtime.

`src/ledger.ts:foldState()` reconstructs state after session resume.

## 6. Current MVP behavior

```text
/loop start
  -> write IDLE state
  -> write PLAN decision
  -> transition RUNNING/EXECUTE
  -> agent.followup()

/loop pause
  -> write PAUSED state
  -> agent.cancel(keepInbox=true)

/loop resume
  -> write RUNNING state
  -> agent.followup()

/loop approve [human approval comment]
  -> require WAITING_HITL/PROMOTION_REVIEW
  -> require matching attempt + spec revision + candidate fingerprint
  -> create human-approved Git candidate commit
  -> persist the optional comment in evidence and Decision Ledger
  -> write COMPLETED/PROMOTE state

/loop reject
  -> require WAITING_HITL
  -> write FAILED state
```

The successful path is deliberately gated:

```text
DSH candidate marker
  -> independently execute acceptance commands
  -> require an explicit dsh-doublecheck deliverable verdict
  -> verify a non-empty bounded Git scope
  -> WAITING_HITL / PROMOTION_REVIEW
  -> human /loop approve
  -> Git candidate commit
  -> COMPLETED / PROMOTE
```

Missing or ambiguous verification capabilities fail closed; a generic assistant success marker is only a candidate declaration, never promotion evidence by itself.

The promotion-review state stores a canonical SHA-256 revision of `{goal,maxAttempts,acceptance}` and a SHA-256 fingerprint of the reviewed Git HEAD plus changed file contents. `/loop approve` recomputes both under the operation lock. A changed candidate or contract cannot consume the old approval.

### RSI recursion and terminal compensation

`/loop retry [human feedback]` preserves the current candidate and writes an `improvement_proposal` evidence record containing the prior Gate problem, a bounded hypothesis, expected evidence, and human feedback. The next DSH turn receives this proposal and the unchanged acceptance contract.

`/loop reject` is terminal: it archives known Gate/spec reports into the ledger, restores only `allowedFiles` to `baselineCommit`, removes archived workspace copies, records a `reject_cleanup` evidence record, and releases the Git common-dir lock. Unknown out-of-scope changes make cleanup fail loud instead of being deleted.

## 7. Verified community plugins

Optional integrations were checked against GitHub/npm:

- `dsh-workflow`: `omdsh-dev/dsh_workflow`, workflow capsules and durable run graph
- `dsh-doublecheck`: `PerryLink/dsh-doublecheck`, requirements/test/delivery quality gate
- `dsh-background-agents`: `PerryLink/dsh-background-agents`, continuable child agents and durable team rooms
- `dsh-flowglass`: `Iwctwbh/dsh-flowglass`, Web UI flow visualization
- `@necokeine/dsh-git`: `necokeine/dsh-git`, Web UI Git status/commit/push

None replaces the LoopGraph Supervisor core. They are optional capability adapters.

The discovery source `dsh-plugin.org` is a community-maintained, non-official catalog. Its `verified` label is useful for discovery but is not sufficient for adoption. Candidates must still pass source review, npm peer resolution, exact-DSH-version loading, restart recovery, and LoopGraph composition E2E.

### Integration matrix

| Plugin | Integration mode | Why |
|---|---|---|
| `dsh-workflow` | Evaluated seam; delegation disabled | Its `WorkflowRun.done`, `show`, and `subscribe` APIs are suitable building blocks, but the full retry/restart/workspace-adoption lifecycle is not yet implemented; `workflowName` fails closed |
| `dsh-doublecheck` | Required `/gate run` command for automatic promotion path | Reuse deterministic requirements/test/review gate evidence; missing/ambiguous verdict fails closed |
| `dsh-background-agents` | Composition dependency used by workflow/agent tools | Let DSH workflows delegate to continuable children without owning child lifecycle |
| `dsh-flowglass` | Passive observer | Render DSH session/workflow events in Web UI |
| `@necokeine/dsh-git` | Passive Git UI | Expose status/commit/push to the human; policy remains in this plugin |

### Evaluated catalog candidates

| Candidate | Decision | Evidence and re-evaluation condition |
|---|---|---|
| `dsh-workflow-worktree` | Reject for B | Its adapter contract binds a child agent to a real worktree, but task-scoped lanes dispose before LoopGraph verifies/promotes them and the plugin intentionally has no merge/adopt handoff. Manifest writes are not cross-process durable, tests hard-code the author's Windows dependency paths, and the real-engine test was not reproducible. Re-evaluate only after workflow-level lane ownership, explicit result adoption, durable metadata, and rc.2 lineage-aware E2E exist. |
| `dsh-auto-review@0.6.0` | Reject on DSH rc.2 | Repository typecheck and 233 tests pass against rc.2 packages, but published peers require `>=0.1.0-rc.8`; standard install is unsupported. On rc.2, `ignorable` audit events degrade to memory, and generic approval verdicts are not candidate/attempt-scoped delivery review evidence. Keep `dsh-doublecheck` local review authoritative. Re-evaluate after installable peers and candidate-scoped Gate integration ship. |
| `dsh-permission-rules@0.5.6` | Lab-only; no current benefit | Runtime tests target rc.2, but published peers still require rc.8 and durable audit is disabled on rc.2. Its `allow` action delegates to the downstream sandbox policy, so it cannot eliminate Go cache escalation prompts; broad shell-string allow rules would add risk. Re-evaluate after a normally installable release or a narrower sandbox allowlist capability. |
| `dsh-checkpoint-rewind@0.5.5` | Observe | Published peers require rc.8. Its three-state snapshots remain relevant after a DSH upgrade, but it cannot replace workflow-owned Git versions or uncertain-execution policy. |
| `dsh-revdiff` / `dsh-file-review` | Optional UX only | Structured human diff comments can improve review ergonomics, but do not provide promotion authority. `dsh-revdiff` is terminal-only and not published to npm; browser review candidates target newer rc peers. |

No catalog candidate was installed into the active `web` profile. The active profile remains limited to `dsh-doublecheck@0.8.0` and the linked LoopGraph plugin, preserving the demonstrated rc.2 composition.
