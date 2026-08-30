from __future__ import annotations

from typing import cast

from .candidates import CandidateKind, CandidateManifest, CandidateProof, CandidateStatus
from .store import SQLiteStore, decode, encode


class CandidateStore:
    """Durable quarantine and proof registry for self-improvement candidates."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_candidates (candidate_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, manifest_hash TEXT NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.store.db.commit()

    def save(self, candidate: CandidateManifest, commit: bool = True) -> None:
        if candidate.status != "QUARANTINED" or candidate.proof is not None:
            raise ValueError("new candidate manifests must enter quarantine without proof")
        existing = self.store.db.execute("SELECT manifest_hash FROM evolution_candidates WHERE candidate_id=?", (candidate.candidate_id,)).fetchone()
        if existing is not None:
            if existing["manifest_hash"] != candidate.manifest_hash():
                raise ValueError("candidate manifests are immutable")
            return
        self.store.db.execute(
            "INSERT INTO evolution_candidates VALUES(?,?,?,?,datetime('now'))",
            (candidate.candidate_id, candidate.target_id, candidate.manifest_hash(), encode(candidate.document()),),
        )
        if commit:
            self.store.db.commit()

    def get(self, candidate_id: str) -> CandidateManifest | None:
        row = self.store.db.execute("SELECT document FROM evolution_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        return None if row is None else from_document(decode(row["document"], {}))

    def transition(self, candidate_id: str, status: CandidateStatus, proof: CandidateProof | None = None, commit: bool = True, human_approved: bool = False) -> CandidateManifest:
        candidate = self.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        allowed: dict[CandidateStatus, set[CandidateStatus]] = {
            "QUARANTINED": {"VALIDATED", "REJECTED"},
            "VALIDATED": {"PROMOTION_REVIEW", "REJECTED"},
            "PROMOTION_REVIEW": {"ACTIVE", "REJECTED"},
            "ACTIVE": set(),
            "REJECTED": set(),
        }
        if status not in allowed[candidate.status]:
            raise ValueError(f"illegal candidate transition: {candidate.status} -> {status}")
        if status == "ACTIVE" and not human_approved:
            raise ValueError("illegal candidate activation requires explicit human approval")
        if candidate.proof is not None and proof is not None and proof != candidate.proof:
            raise ValueError("candidate proof is immutable once attached")
        effective_proof = proof or candidate.proof
        if effective_proof is not None and (effective_proof.target_id != candidate.target_id or effective_proof.predecessor_hash != candidate.predecessor_hash or effective_proof.candidate_hash != candidate.candidate_hash):
            raise ValueError("candidate proof does not bind this manifest")
        if status in {"VALIDATED", "PROMOTION_REVIEW", "ACTIVE"} and proof is None and candidate.proof is None:
            raise ValueError("candidate transition requires proof")
        if status in {"VALIDATED", "PROMOTION_REVIEW"} and effective_proof is not None and (effective_proof.validation_pass_rate < 1 or effective_proof.canary_pass_rate < 1 or effective_proof.regression_count):
            raise ValueError("candidate proof does not satisfy validation and canary policy")
        updated = CandidateManifest(**{**candidate.__dict__, "status": status, "proof": effective_proof})
        cursor = self.store.db.execute("UPDATE evolution_candidates SET manifest_hash=?, document=? WHERE candidate_id=? AND manifest_hash=?", (updated.manifest_hash(), encode(updated.document()), candidate_id, candidate.manifest_hash()))
        if cursor.rowcount != 1:
            raise RuntimeError("candidate changed concurrently during transition")
        if commit:
            self.store.db.commit()
        return updated


def from_document(document: dict[str, object]) -> CandidateManifest:
    raw_proof = document.get("proof")
    if raw_proof is None:
        proof = None
    else:
        proof_document = cast(dict[str, object], raw_proof)
        proof = CandidateProof(
            target_id=str(proof_document["target_id"]),
            predecessor_hash=str(proof_document["predecessor_hash"]),
            candidate_hash=str(proof_document["candidate_hash"]),
            task_set_hash=str(proof_document["task_set_hash"]),
            validation_proof_hash=str(proof_document["validation_proof_hash"]),
            canary_proof_hash=str(proof_document["canary_proof_hash"]),
            validation_pass_rate=float(cast(float | int, proof_document["validation_pass_rate"])),
            canary_pass_rate=float(cast(float | int, proof_document["canary_pass_rate"])),
            regression_count=int(cast(str | int, proof_document["regression_count"])),
            holdout_task_ids=tuple(cast(list[str], proof_document["holdout_task_ids"])),
            evaluation_evidence_hashes=tuple(cast(list[str], proof_document["evaluation_evidence_hashes"])),
        )
    return CandidateManifest(
        candidate_id=str(document["candidate_id"]),
        target_id=str(document["target_id"]),
        kind=cast(CandidateKind, document["kind"]),
        predecessor_revision=int(cast(str | int, document["predecessor_revision"])),
        predecessor_hash=str(document["predecessor_hash"]),
        candidate_hash=str(document["candidate_hash"]),
        changed_paths=tuple(cast(list[str], document["changed_paths"])),
        rationale=str(document["rationale"]),
        proof=proof,
        status=cast(CandidateStatus, document["status"]),
    )
