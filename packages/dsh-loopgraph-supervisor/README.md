# dsh-loopgraph-supervisor

DSH-native TypeScript plugin MVP for the LoopGraph Supervisor.

## Scope

This plugin is the DSH-native B implementation. The Python Supervisor in the repository remains the reference implementation for the complete external control plane. This package focuses on native Cordis integration:

- `ctx.commands` registers `/loop`
- `agent/*` lifecycle hooks track agent ownership
- `session/event` observes DSH session activity
- a JSONL ledger under `$DSH_HOME/loopgraph` records durable workflow state, decisions, and evidence
- `agent.followup()` schedules DSH execution in the current session
- `agent.cancel(..., { keepInbox: true })` provides cooperative pause behavior
- the plugin loads and validates the same versioned LoopSpec JSON contract as A, persists the active spec/hash in the ledger, and routes transitions through the TypeScript interpreter
- `/loop logs [count]` prints a compact projection of the latest durable events (default 20, max 50)
- reserved `workflowName` configuration seam; named-workflow delegation currently fails closed until terminal outcome, retry, pause/resume, restart reconciliation, and workspace adoption are implemented

## Install from a local checkout

```bash
dsh plugin --profile web add ./packages/dsh-loopgraph-supervisor
dsh --profile web --dump-config
```

Restart the DSH profile, then use:

```text
/loop start Fix the failing tests and run the acceptance command.
/loop status
/loop logs
/loop explain
/loop pause
/loop resume
/loop retry [human feedback]
/loop evolve <LoopSpec improvement request>
/loop recover verify-existing
/loop recover retry-same-attempt
/loop recover restore-baseline
/loop recover abort-preserve
/loop approve [human approval comment]
/loop reject
```

JSON start input can include an acceptance contract:

```text
/loop start {"goal":"Fix the failing tests","maxAttempts":3,"acceptance":{"commands":["pytest -q"],"allowedFiles":["src/app.py","tests/test_app.py"]}}
```

Configure `loopSpecPath` to the active Git-managed artifact before using native evolution. If omitted, the packaged `loopspec.active.json` selects the released v2 artifact:

```yaml
config:
  maxAttempts: 3
  requirePromotionApproval: true
  loopSpecPath: /absolute/repository/configs/loopspecs/coding-supervisor/v1.json
```

Then request an evolution:

```text
/loop evolve Escalate explicit verifier failure to human review while preserving bounded retry.
```

DSH writes only the next revision (for example `v2.json`). The plugin validates schema, graph semantics, revision, predecessor hash, Doublecheck, Git scope, and the immutable candidate snapshot. `/loop approve` activates the candidate spec in durable plugin state; it never lets DSH write the active pointer.

## Important boundary

The plugin does not replace DSH's agent loop. DSH still owns model requests, tools, workspace, approvals, and session persistence. This plugin owns LoopGraph state, decision events, verification gates, and Git evidence.

## Verified plugin composition

The companion `examples/install-all-plugins.sh` installs the verified community capabilities:

- `dsh-workflow`: named workflow capsules, durable run graph, pause/resume/rerun and governance
- `dsh-doublecheck`: requirements grill, test evidence and delivery gate; `/loop` calls `/gate run` when available
- `dsh-background-agents`: continuable children and durable team rooms for workflows that need delegation
- `dsh-flowglass`: passive Web UI flow visualization of the session/run events
- `@necokeine/dsh-git`: passive Web UI Git status/commit/push surface

The plugin keeps its own policy and Decision Ledger. These integrations are capability providers, not hidden replacements for the policy.

## Promotion policy

The Supervisor independently executes every configured acceptance command. Empty commands, command failures, a missing or ambiguous `dsh-doublecheck` verdict, and an invalid Git scope all fail closed. After all automated evidence passes, the default policy enters `WAITING_HITL`; `/loop approve [human approval comment]` is valid only for this verified promotion-review state and creates the Git candidate commit. An optional comment is stored in the candidate evidence and `HUMAN_APPROVE_PROMOTE` Decision Ledger record, but does not change the deterministic Git commit subject. `/loop retry` and `/loop reject` handle failed review states.

`retry` preserves the candidate diff and injects the prior gate report plus optional human feedback into the next DSH turn. `reject` archives known gate/spec reports into the durable ledger, restores only contract-approved candidate paths to the recorded baseline, removes archived workspace report copies, verifies there are no unknown out-of-scope changes, and then releases the workspace lock.

Human evolution feedback and promotion approval are separate. `/loop evolve` asks the current DSH Agent runtime to create a candidate LoopSpec; it never directly changes the active graph or grants promotion permission.

If DSH restarts while a workflow is still `RUNNING`, the plugin changes it to `UNCERTAIN` instead of automatically repeating the turn. The Web profile uses `ctx.userQuestions` to present four recovery buttons; the `/loop recover` commands provide the same operations for automation or a UI-less adapter.

完整的 Web、HITL、Git、重启恢复和官方源码 test harness 验证步骤见 [E2E_RUNBOOK.md](E2E_RUNBOOK.md)。
