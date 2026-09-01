# RSI v2 Evidence

This document records the first public LoopSpec RSI promotion for this repository.

## Candidate

```text
baseline tag: loopspec-rsi-v1
baseline commit: fdebd0d611e61314fc5f2c839303e06ffbd59683
candidate commit: de411bc67dc6e231c9c1f74c58e55b8084a88ad3
merge commit: 1bd24d067591d6db0d55b372ef5eec6196ca6a0a
release tag: loopspec-rsi-v2
pull request: https://github.com/ivloli/loopgraph-supervisor-mvp/pull/1
```

The candidate changed exactly one path:

```text
configs/loopspecs/coding-supervisor/v2.json
```

The semantic change was:

```text
v1 execute/fail -> execute
v2 execute/fail -> human_gate
```

The v2 canonical LoopSpec hash is:

```text
5b5ebe301a5533deb33f3bba0d3cb87ae8f56996b913f7e56f6007de7edb5c8d
```

## Gates

The candidate passed:

- Python LoopSpec loader and semantic graph validation;
- TypeScript LoopSpec loader and semantic graph validation;
- shared A/B transition vectors;
- red/green behavior check: v1 failed the v2 expectation and v2 passed;
- Git changed-path scope;
- `dsh-doublecheck` delivery gate;
- GitHub Actions CI on the pull request head.

The final CI run was `33389111054`. It ran Ruff, mypy, Python tests, Docker container gate, TypeScript build, and Node tests.

## Human Gate

The candidate was held at `PROMOTION_REVIEW` until a human approval was explicitly recorded in the DSH sidecar. The approval was bound to the candidate commit and LoopSpec hash before the candidate commit was created.

The local DSH sidecar is not committed because it contains session history and environment-specific paths. The public evidence above is intentionally limited to reproducible Git objects, hashes, PR metadata, and CI results.

## Post-activation Canary

After v2 activation, a real DSH Agent session was given a task whose authoritative payment schema was intentionally absent. The Agent correctly reported a deterministic failure instead of inventing a schema. The active v2 graph routed:

```text
execute/fail -> human_gate
```

Observed result:

```text
status: WAITING_HITL
hitl reason: FAILURE_REVIEW
attempt: 1
automatic retry: none
```

The validation workflow was then rejected and cleaned up. The release tag remains the immutable record of the v2 LoopSpec; the canary workflow did not modify the release.

## Limits

This is bounded policy evolution, not evidence of model training or broad generalization. The current proof covers a real DSH-generated LoopSpec candidate and deterministic Host-owned checks. It does not claim that arbitrary Supervisor Python code can be safely self-modified.
