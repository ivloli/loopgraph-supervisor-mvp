from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from .builder import CandidateBuilder, CandidateBuildRequest
from .candidate_store import CandidateStore
from .candidates import CandidateManifest, CandidateProof
from .evaluation import LoopSpecEvalTask, compare_specs
from .loopspec import LoopSpec
from .spec_store import LoopSpecStore, from_document


class HoldoutProvider(Protocol):
    """Host-owned task source that is never passed to the candidate proposer."""

    def validation_tasks(self, target_id: str) -> tuple[LoopSpecEvalTask, ...]: ...

    def canary_tasks(self, target_id: str) -> tuple[LoopSpecEvalTask, ...]: ...


@dataclass(frozen=True)
class FrozenLoopSpecCandidate:
    manifest: CandidateManifest
    spec: LoopSpec


class LoopSpecEvolutionService:
    """Host authority for candidate intake and proof generation; it cannot activate candidates."""

    def __init__(self, specs: LoopSpecStore, candidates: CandidateStore, holdout: HoldoutProvider):
        self.specs = specs
        self.candidates = candidates
        self.holdout = holdout

    def intake(self, candidate_id: str, document: dict[str, object], rationale: str) -> FrozenLoopSpecCandidate:
        spec = from_document(document)
        active = self.specs.active(spec.spec_id)
        if active is None:
            raise ValueError("LoopSpec candidate has no active predecessor")
        if spec.revision != active.revision + 1 or spec.predecessor_hash != active.content_hash():
            raise ValueError("LoopSpec candidate revision is not bound to the active predecessor")
        manifest = CandidateManifest(
            candidate_id=candidate_id,
            target_id=spec.spec_id,
            kind="loopspec",
            predecessor_revision=active.revision,
            predecessor_hash=active.content_hash(),
            candidate_hash=spec.content_hash(),
            changed_paths=(f"loopspecs/{spec.spec_id}/{spec.revision}.json",),
            rationale=rationale,
        )
        with self.specs.store.db:
            self.specs.save(spec, status="CANDIDATE", commit=False)
            self.candidates.save(manifest, commit=False)
        return FrozenLoopSpecCandidate(manifest, spec)

    def propose(self, builder: CandidateBuilder, candidate_id: str, improvement_request: str = "", target_id: str = "coding-supervisor") -> FrozenLoopSpecCandidate:
        active = self.specs.active(target_id)
        if active is None:
            raise ValueError("no active LoopSpec for Builder proposal")
        validation_context: tuple[dict[str, object], ...] = tuple(
            {"task_id": task.task_id, "initial_node": task.initial_node, "outcomes": list(task.outcomes), "expected_targets": list(task.expected_targets)}
            for task in self.holdout.validation_tasks(active.spec_id)
        )
        request = CandidateBuildRequest(candidate_id, active.document(), validation_context, improvement_request=improvement_request)
        result = builder.build(request)
        if result.candidate_id != candidate_id or result.kind != "loopspec":
            raise ValueError("Phase 2 LoopSpec service only accepts loopspec candidates")
        candidate_document = dict(result.document)
        # Identity and predecessor binding are Host-owned; the Builder proposes graph content only.
        candidate_document["spec_id"] = active.spec_id
        candidate_document["revision"] = active.revision + 1
        candidate_document["predecessor_hash"] = active.content_hash()
        return self.intake(result.candidate_id, candidate_document, result.rationale)

    def evaluate(self, candidate_id: str) -> CandidateManifest:
        manifest = self.candidates.get(candidate_id)
        if manifest is None or manifest.kind != "loopspec" or manifest.status != "QUARANTINED":
            raise ValueError("candidate must be a quarantined LoopSpec manifest")
        baseline = self.specs.active(manifest.target_id)
        candidate = self.specs.revision(manifest.target_id, manifest.predecessor_revision + 1)
        if baseline is None or candidate is None:
            raise ValueError("candidate or predecessor spec is missing")
        if baseline.revision != manifest.predecessor_revision or baseline.content_hash() != manifest.predecessor_hash:
            raise ValueError("candidate evaluation predecessor no longer matches active LoopSpec")
        if candidate.content_hash() != manifest.candidate_hash or candidate.predecessor_hash != manifest.predecessor_hash:
            raise ValueError("candidate evaluation artifact does not match its manifest")
        validation_tasks = self.holdout.validation_tasks(manifest.target_id)
        canary_tasks = self.holdout.canary_tasks(manifest.target_id)
        _, validation = compare_specs(baseline, candidate, validation_tasks, "validation")
        _, canary = compare_specs(baseline, candidate, canary_tasks, "canary")
        proof = CandidateProof(
            manifest.target_id,
            manifest.predecessor_hash,
            manifest.candidate_hash,
            hashlib.sha256("".join(sorted(task.content_hash() for task in (*validation_tasks, *canary_tasks))).encode()).hexdigest(),
            validation.proof_hash(),
            canary.proof_hash(),
            validation.pass_rate,
            canary.pass_rate,
            validation.regression_count + canary.regression_count,
            tuple(hashlib.sha256(task.task_id.encode()).hexdigest() for task in canary_tasks),
            (validation.proof_hash(), canary.proof_hash()),
        )
        if proof.validation_pass_rate < 1 or proof.canary_pass_rate < 1 or proof.regression_count:
            return self.candidates.transition(candidate_id, "REJECTED", proof)
        with self.candidates.store.db:
            self.candidates.transition(candidate_id, "VALIDATED", proof, commit=False)
            return self.candidates.transition(candidate_id, "PROMOTION_REVIEW", commit=False)

    def activate(self, candidate_id: str, *, human_approved: bool) -> CandidateManifest:
        """Activate one proven candidate through the Host-owned revision registry."""
        if not human_approved:
            raise ValueError("LoopSpec activation requires explicit human approval")
        manifest = self.candidates.get(candidate_id)
        if manifest is None or manifest.status != "PROMOTION_REVIEW" or manifest.proof is None:
            raise ValueError("candidate must be in promotion review with proof")
        active = self.specs.active(manifest.target_id)
        candidate = self.specs.revision(manifest.target_id, manifest.predecessor_revision + 1)
        if active is None or candidate is None or active.content_hash() != manifest.predecessor_hash or candidate.content_hash() != manifest.candidate_hash:
            raise ValueError("candidate activation is stale or does not match its proof")
        with self.candidates.store.db:
            self.specs.save(candidate, status="ACTIVE", commit=False, allow_human_activation=True)
            return self.candidates.transition(candidate_id, "ACTIVE", manifest.proof, commit=False, human_approved=True)
