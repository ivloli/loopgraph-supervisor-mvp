# Changelog

All notable architectural and behavioral changes are recorded here. Dates describe the local development sequence; the project has not been released publicly yet.

## Unreleased - Hardening

### Added

- File-level implementation plan in `PLAN.md`.
- Independent acceptance-command execution in B with exit-code/stdout/stderr evidence.
- `dsh-doublecheck@0.8.0` in the DSH Web profile.
- Default B promotion HITL: verified candidates enter `WAITING_HITL/PROMOTION_REVIEW`; `/loop approve` creates the Git commit.
- Compact `/loop logs [count]` command backed by a durable sidecar ledger.
- A Web Control Room served by the Python API and a `loopgraph` CLI client.
- Modern Chinese A dashboard with a dark sidebar and tabs for overview, Decision Ledger, verification evidence, versions, activity, and workflow creation.
- A per-workflow detached Git worktree manager.
- A durable execution-intent table and deterministic promotion reconciliation.
- B unique run ids, workflow-owned version registry, Git workspace lock, fsync, and torn-tail recovery.
- B structured retry proposals carrying Gate evidence and human feedback.
- B reject compensation that archives reports and restores the bounded candidate to baseline.
- A uncertain-execution recovery center with four explicit Web/API/CLI actions.
- B uncertain restart recovery through DSH user-question buttons and `/loop recover` fallback.
- tmux launch scripts for A, B, and operational logs.
- Optional human approval comments on `/loop approve`, persisted in candidate evidence and the promotion Decision Ledger record.
- B sidecar records now use a per-session cross-process writer lock, monotonic sequence numbers, and a SHA-256 checksum chain while retaining legacy-ledger read compatibility.
- Community Plugin Hub candidates were source-audited against the rc.2 and LoopGraph contracts; worktree, auto-review, and permission-rule candidates remain uninstalled because their current lifecycle, peer, or audit semantics do not satisfy the promotion boundary.
- Promotion requests now bind the reviewed attempt and spec revision to a content fingerprint; post-Gate candidate changes invalidate approval.
- Python dependency locking with an exact DSH SDK pin, Ruff, mypy, installable console entry points, and GitHub Actions CI.

### Changed

- B gate behavior changed from fail-open to fail-closed when doublecheck is absent or does not return an explicit deliverable verdict.
- B no longer writes custom unmarked events into the DSH Session log. Policy facts live under `$DSH_HOME/loopgraph`; DSH Session remains authoritative for Agent/tool history.
- Git porcelain parsing now preserves the first path character.
- A defaults to the official SDK; fake mode must be explicit.
- A rejects empty acceptance-command contracts.
- B rollback accepts workflow version ids instead of arbitrary Git SHAs.
- B reject changed from status-only termination to auditable baseline restoration; retry explicitly preserves the diff for RSI rework.
- B serializes mutating human commands with a per-session operation lock, rejects illegal resume/rollback source states, and discards stale verification results after pause or workflow changes.
- B now fails closed when `workflowName` is configured instead of launching a named workflow whose terminal result, retries, restart state, and workspace output are not yet safely adopted.
- A now treats DSH exceptions before durable result as unknown outcomes and enters `UNCERTAIN` instead of allowing an automatic new attempt.
- Phase 1 extracted the coding graph into immutable LoopSpec v1 with a deterministic interpreter, SQLite revision registry, validation/canary route evaluation, and a written retrospective at `docs/PHASE_1_RETROSPECTIVE.md`.
- Phase 2 began with proof-bound candidate manifests and a SQLite quarantine registry for LoopSpec, Supervisor, Verifier, and Policy candidates; generation and activation remain gated for the next milestone.
- Phase 2 added host-owned validation/canary task boundaries and LoopSpec candidate intake/evaluation; passing candidates stop at promotion review, failing candidates are rejected, and active v1 is never changed by evaluation alone.
- Phase 2 added a filesystem holdout repository and trusted Arena receipts bound to target, predecessor, candidate, task set, fixture, verifier, and output hashes; untrusted candidate mutation remains fail-closed until a real DSH sandbox adapter exists.
- Phase 2 added strict CandidateBuilder intake and permanently disabled the unsafe host-process adapter; the real path now requires a concrete live-gated Docker runtime and controlled relay.
- Added executable rc.2 Seatbelt and Docker Builder security gates with persisted receipts; rc.2 fails blind reads while the Docker substrate passes.
- Added exact-version official Linux runtime provenance, complete Docker stdio SDK initialize, live daemon/image gates, and an internal-only fixed DeepSeek relay whose secret mount owns the real credential.
- Completed the first credentialed real-model Builder run through quarantine, Host-owned binding, validation, and canary proof; it reached `PROMOTION_REVIEW` without activation.
- Added durable multi-candidate coordination and contradiction detection for the PR2/PR3/PR4 failure mode; only one Host-selected candidate from the canonical parent may proceed to a formal PR.
- Added a Git-backed PR preparation gate that revalidates current coordination, approval head, candidate parent, and changed paths before any Git hosting adapter may create a PR.
- Added versioned LoopGraph JSON loading and semantic graph validation, including reachability, termination, node-kind outcomes, and coding-supervisor governance requirements.
- Added human/task `EvolutionTrigger` persistence, the `/evolution/triggers` and `loopgraph evolve` entry points, and a claim-based proposal worker that creates quarantined candidates without activation; external DSH calls remain at-least-once across worker crashes.
- Added Host-owned Python Test/Coverage Gate evidence and durable `EvolutionRun` correlation for trigger, baseline, candidate, and promotion-review state.
- Added explicit GitHub PR/review and post-activation canary/rollback adapters with EvolutionRun lifecycle binding.

### Fixed

- DSH restart failures caused by unmarked `loopgraph/event` records.
- False Git scope rejection where `calculator.py` was parsed as `alculator.py`.
- Web UI stalls caused by rendering recursive full-state logs.

### Known Limitations

- DSH side effects remain at-least-once across the pre-result crash window.
- Git/ledger/SQLite operations are not one distributed transaction.
- B direct mode uses a workspace lock; full worktree isolation requires the dsh-workflow isolation adapter.

## 0.4 - DSH-native Plugin B

### Added

- TypeScript/Cordis plugin under `packages/dsh-loopgraph-supervisor`.
- DSH `/loop` command surface, `agent/turn-stopping` post-processing, Decision Ledger, Git evidence, and restart recovery.
- Verified official DSH CLI/Web profile loading.
- Optional adapters for `dsh-workflow`; integration boundaries documented for doublecheck, background agents, flowglass, and Git UI.

### Evidence

- Real DSH Web coding tasks completed.
- Candidate commits `80a4928` and `c9d5f2d` created in the isolated fixture repository during development.
- Session export ZIP integrity checked with `unzip -t`.

## 0.3 - Git Versioned RSI Loop

### Added

- Acceptance contracts and independent `CommandVerifier` in A.
- Baseline/candidate versions, changed-file scope evidence, Git candidate commit, and rollback.
- Real DeepSeek Harness SDK coding-task E2E against `fixtures/rsi-sample`.

### Changed

- Promotion stopped relying on a non-empty assistant response and required command evidence for the demonstrated coding path.

## 0.2 - Python SDK Supervisor A

### Added

- Python Supervisor using the official `deepseek-harness-sdk`.
- SQLite workflow/event/attempt/HITL/version records.
- Decision Ledger, improvement proposals, HTTP API, pause/resume, and fake test adapters.

### Corrected

- DSH was redefined correctly as DeepSeek Harness, not Durable State History.
- The implementation stopped treating the DeepSeek Chat API as equivalent to the Harness runtime.

## 0.1 - Discarded Go Prototype

- Initial Go prototype modeled the durable state machine but was discarded after confirming that DSH is an Agent runtime with an official Python SDK and native TypeScript/Cordis plugin surface.
- This prototype is not part of the current codebase and must not be presented as a delivered implementation.
