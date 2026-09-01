import pytest

from loopgraph_supervisor.builder import CandidateBuildRequest, CandidateBuildResult
from loopgraph_supervisor.candidate_store import CandidateStore
from loopgraph_supervisor.evaluation import LoopSpecEvalTask
from loopgraph_supervisor.evolution import LoopSpecEvolutionService
from loopgraph_supervisor.evolution_trigger import EvolutionTriggerStore
from loopgraph_supervisor.loopspec import coding_spec_revision, default_coding_spec
from loopgraph_supervisor.proposal_worker import EvolutionProposalWorker
from loopgraph_supervisor.spec_store import LoopSpecStore
from loopgraph_supervisor.store import SQLiteStore


class Holdout:
    def validation_tasks(self, target_id):
        return (LoopSpecEvalTask("visible", "validation", "verify", ("approve",), ("hitl",)),)

    def canary_tasks(self, target_id):
        return (LoopSpecEvalTask("private", "canary", "verify", ("exhausted",), ("hitl",)),)


class Builder:
    def build(self, request: CandidateBuildRequest) -> CandidateBuildResult:
        assert request.improvement_request
        document = default_coding_spec().document()
        return CandidateBuildResult(request.candidate_id, "loopspec", "human request applied", document, "session")


def make_worker():
    store = SQLiteStore(":memory:")
    specs = LoopSpecStore(store)
    specs.save(coding_spec_revision(1), status="ACTIVE")
    triggers = EvolutionTriggerStore(store)
    evolution = LoopSpecEvolutionService(specs, CandidateStore(store), Holdout())
    return EvolutionProposalWorker(triggers, evolution, Builder()), triggers


def test_worker_consumes_human_trigger_into_promotion_review_candidate():
    worker, triggers = make_worker()
    trigger_id, _ = triggers.create_human_request("coding-supervisor", "DDHH", "review the LoopGraph")

    result = worker.consume(trigger_id)

    assert result.status == "PROMOTION_REVIEW"
    assert result.candidate.manifest.status == "PROMOTION_REVIEW"
    assert triggers.status(trigger_id) == "CONSUMED"
    run = worker.runs.by_trigger(trigger_id)
    assert run is not None
    assert run.status == "PROMOTION_REVIEW"
    assert run.candidate_id == result.candidate_id
    assert run.baseline_spec_revision == 1


def test_worker_does_not_consume_a_trigger_twice():
    worker, triggers = make_worker()
    trigger_id, _ = triggers.create_human_request("coding-supervisor", "DDHH", "review")
    worker.consume(trigger_id)

    with pytest.raises(ValueError, match="not pending"):
        worker.consume(trigger_id)
