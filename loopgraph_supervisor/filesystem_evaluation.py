from __future__ import annotations

import hashlib

from .arena import FilesystemArena
from .candidates import CandidateProof
from .holdout import FilesystemHoldoutRepository


def evaluate_filesystem_candidate(
    repository: FilesystemHoldoutRepository,
    arena: FilesystemArena,
    target_id: str,
    baseline_hash: str,
    candidate_hash: str,
) -> CandidateProof:
    receipt_hashes: list[str] = []
    canary_ids: list[str] = []
    validation_passes = 0
    canary_passes = 0
    validation_tasks = repository.tasks(target_id, "validation")
    canary_tasks = repository.tasks(target_id, "canary")
    regressions = 0
    for split, tasks in (("validation", validation_tasks), ("canary", canary_tasks)):
        for task in tasks:
            baseline = arena.run(task, f"{split}-{task.task_hash}-baseline", baseline_hash)
            candidate = arena.run(task, f"{split}-{task.task_hash}-candidate", candidate_hash)
            receipt_hashes.extend((baseline.receipt_hash(), candidate.receipt_hash()))
            if baseline.passed and not candidate.passed:
                regressions += 1
            if split == "validation" and candidate.passed:
                validation_passes += 1
            if split == "canary":
                canary_ids.append(hashlib.sha256(task.task_id.encode()).hexdigest())
                if candidate.passed:
                    canary_passes += 1
    validation_proof = hashlib.sha256("".join(receipt_hashes[: len(validation_tasks) * 2]).encode()).hexdigest()
    canary_proof = hashlib.sha256("".join(receipt_hashes[len(validation_tasks) * 2 :]).encode()).hexdigest()
    task_set_hash = hashlib.sha256("".join(task.task_hash for task in (*validation_tasks, *canary_tasks)).encode()).hexdigest()
    return CandidateProof(
        target_id,
        baseline_hash,
        candidate_hash,
        task_set_hash,
        validation_proof,
        canary_proof,
        validation_passes / len(validation_tasks),
        canary_passes / len(canary_tasks),
        regressions,
        tuple(canary_ids),
        tuple(receipt_hashes),
    )
