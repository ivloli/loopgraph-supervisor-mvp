from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

CandidateKind = Literal["loopspec", "supervisor", "verifier", "policy"]
CandidateStatus = Literal["QUARANTINED", "VALIDATED", "PROMOTION_REVIEW", "ACTIVE", "REJECTED"]


@dataclass(frozen=True)
class CandidateProof:
    target_id: str
    predecessor_hash: str
    candidate_hash: str
    task_set_hash: str
    validation_proof_hash: str
    canary_proof_hash: str
    validation_pass_rate: float
    canary_pass_rate: float
    regression_count: int
    holdout_task_ids: tuple[str, ...]
    evaluation_evidence_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("candidate proof requires a target id")
        if not all(re.fullmatch(r"[a-f0-9]{64}", value) for value in (self.predecessor_hash, self.candidate_hash, self.task_set_hash, self.validation_proof_hash, self.canary_proof_hash)):
            raise ValueError("candidate proof hashes must be SHA-256 digests")
        if not 0 <= self.validation_pass_rate <= 1 or not 0 <= self.canary_pass_rate <= 1:
            raise ValueError("candidate proof pass rates must be between 0 and 1")
        if self.regression_count < 0:
            raise ValueError("candidate proof regression count cannot be negative")
        if not self.holdout_task_ids:
            raise ValueError("candidate proof requires held-out task ids")
        if not self.evaluation_evidence_hashes or not all(re.fullmatch(r"[a-f0-9]{64}", value) for value in self.evaluation_evidence_hashes):
            raise ValueError("candidate proof requires SHA-256-bound evaluation receipts")


@dataclass(frozen=True)
class CandidateManifest:
    candidate_id: str
    target_id: str
    kind: CandidateKind
    predecessor_revision: int
    predecessor_hash: str
    candidate_hash: str
    changed_paths: tuple[str, ...]
    rationale: str
    proof: CandidateProof | None = None
    status: CandidateStatus = "QUARANTINED"

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.target_id or not self.rationale.strip():
            raise ValueError("candidate requires ids and a rationale")
        if self.predecessor_revision < 1 or not re.fullmatch(r"[a-f0-9]{64}", self.predecessor_hash) or not re.fullmatch(r"[a-f0-9]{64}", self.candidate_hash):
            raise ValueError("candidate predecessor and content hashes must be SHA-256-bound")
        if not self.changed_paths or any(not path or path.startswith("/") or ".." in path.split("/") for path in self.changed_paths):
            raise ValueError("candidate changed_paths must be non-empty safe relative paths")
        if self.status in {"VALIDATED", "PROMOTION_REVIEW", "ACTIVE"} and self.proof is None:
            raise ValueError("validated candidate states require evaluation proof")

    def document(self) -> dict[str, object]:
        proof = None if self.proof is None else {
            "target_id": self.proof.target_id,
            "predecessor_hash": self.proof.predecessor_hash,
            "candidate_hash": self.proof.candidate_hash,
            "task_set_hash": self.proof.task_set_hash,
            "validation_proof_hash": self.proof.validation_proof_hash,
            "canary_proof_hash": self.proof.canary_proof_hash,
            "validation_pass_rate": float(self.proof.validation_pass_rate),
            "canary_pass_rate": float(self.proof.canary_pass_rate),
            "regression_count": self.proof.regression_count,
            "holdout_task_ids": list(self.proof.holdout_task_ids),
            "evaluation_evidence_hashes": list(self.proof.evaluation_evidence_hashes),
        }
        return {
            "schema_version": 1,
            "candidate_id": self.candidate_id,
            "target_id": self.target_id,
            "kind": self.kind,
            "predecessor_revision": self.predecessor_revision,
            "predecessor_hash": self.predecessor_hash,
            "candidate_hash": self.candidate_hash,
            "changed_paths": list(self.changed_paths),
            "rationale": self.rationale,
            "proof": proof,
            "status": self.status,
        }

    def manifest_hash(self) -> str:
        encoded = json.dumps(self.document(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def with_proof(candidate: CandidateManifest, proof: CandidateProof) -> CandidateManifest:
    if candidate.status != "QUARANTINED" or candidate.proof is not None:
        raise ValueError("proof can only be attached once to a quarantined candidate")
    if proof.target_id != candidate.target_id or proof.predecessor_hash != candidate.predecessor_hash or proof.candidate_hash != candidate.candidate_hash:
        raise ValueError("candidate proof does not bind this manifest")
    if proof.validation_pass_rate < 1 or proof.canary_pass_rate < 1 or proof.regression_count:
        raise ValueError("candidate proof does not satisfy validation and canary policy")
    return CandidateManifest(**{**candidate.__dict__, "proof": proof, "status": "VALIDATED"})
