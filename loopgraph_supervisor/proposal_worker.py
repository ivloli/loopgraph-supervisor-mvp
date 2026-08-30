from __future__ import annotations

import json
from dataclasses import dataclass

from .builder import CandidateBuilder
from .evolution import FrozenLoopSpecCandidate, LoopSpecEvolutionService
from .evolution_run import EvolutionRun, EvolutionRunStore
from .evolution_trigger import EvolutionTrigger, EvolutionTriggerStore, HumanFeedback, TaskFeedback


@dataclass(frozen=True)
class ProposalResult:
    trigger_id: str
    candidate_id: str
    status: str
    candidate: FrozenLoopSpecCandidate


class EvolutionProposalWorker:
    """Consumes durable RSI triggers and creates quarantined candidates only."""

    def __init__(self, triggers: EvolutionTriggerStore, evolution: LoopSpecEvolutionService, builder: CandidateBuilder, runs: EvolutionRunStore | None = None):
        self.triggers = triggers
        self.evolution = evolution
        self.builder = builder
        self.runs = runs or EvolutionRunStore(triggers.store)

    def consume(self, trigger_id: str) -> ProposalResult:
        trigger = self.triggers.claim(trigger_id)
        candidate_id = f"evolution-{trigger_id.replace(':', '-') }"
        baseline = self.evolution.specs.active(trigger.target_id)
        if baseline is None:
            self.triggers.finish(trigger_id, "FAILED")
            raise ValueError("evolution trigger target has no active baseline")
        run_id = f"run:{trigger_id}"
        self.runs.save(EvolutionRun(run_id, trigger_id, trigger.target_id, baseline.revision, baseline.content_hash(), candidate_id=candidate_id, status="PROCESSING"))
        try:
            candidate = self.evolution.propose(self.builder, candidate_id, self._request_context(trigger_id, trigger), target_id=trigger.target_id)
            manifest = self.evolution.evaluate(candidate_id)
            candidate = FrozenLoopSpecCandidate(manifest, candidate.spec)
        except Exception:
            self.runs.save(EvolutionRun(run_id, trigger_id, trigger.target_id, baseline.revision, baseline.content_hash(), candidate_id=candidate_id, status="FAILED"))
            self.triggers.finish(trigger_id, "FAILED")
            raise
        self.triggers.finish(trigger_id, "CONSUMED")
        if candidate.manifest.status != "PROMOTION_REVIEW" or candidate.manifest.proof is None:
            raise RuntimeError("evaluated candidate has no proof")
        self.runs.save(EvolutionRun(run_id, trigger_id, trigger.target_id, baseline.revision, baseline.content_hash(), candidate_id=candidate_id, candidate_hash=candidate.manifest.candidate_hash, proof_hash=candidate.manifest.proof.validation_proof_hash, status="PROMOTION_REVIEW"))
        return ProposalResult(trigger_id, candidate_id, "PROMOTION_REVIEW", candidate)

    @staticmethod
    def _request_context(trigger_id: str, trigger: EvolutionTrigger) -> str:
        evidence: list[dict[str, object]] = []
        for item in trigger.evidence:
            if isinstance(item, HumanFeedback):
                evidence.append({"source": "human", "reviewer": item.reviewer, "comment": item.comment})
            elif isinstance(item, TaskFeedback):
                evidence.append({"source": "task", "workflow_id": item.workflow_id, "attempt": item.attempt, "passed": item.passed, "feedback": item.feedback})
        return json.dumps({"trigger_id": trigger_id, "source": trigger.source, "reason": trigger.reason, "evidence": evidence}, ensure_ascii=False, sort_keys=True)
