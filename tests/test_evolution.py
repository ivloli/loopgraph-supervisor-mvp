import pytest

from loopgraph_supervisor.candidate_store import CandidateStore
from loopgraph_supervisor.evaluation import LoopSpecEvalTask
from loopgraph_supervisor.evolution import LoopSpecEvolutionService
from loopgraph_supervisor.loopspec import LoopSpec, default_coding_spec
from loopgraph_supervisor.spec_store import LoopSpecStore
from loopgraph_supervisor.store import SQLiteStore


class Builder:
    def __init__(self, document):
        self.document = document

    def build(self, request):
        from loopgraph_supervisor.builder import CandidateBuildResult

        return CandidateBuildResult(request.candidate_id, "loopspec", "host binds identity", self.document, "test")


class PrivateHoldout:
    def validation_tasks(self, target_id):
        assert target_id == "coding-supervisor"
        return (LoopSpecEvalTask("visible-validation", "validation", "verify", ("approve",), ("hitl",)),)

    def canary_tasks(self, target_id):
        assert target_id == "coding-supervisor"
        return (LoopSpecEvalTask("private-canary", "canary", "verify", ("exhausted",), ("hitl",)),)


def setup_service():
    store = SQLiteStore(":memory:")
    specs = LoopSpecStore(store)
    baseline = default_coding_spec()
    specs.save(baseline, status="ACTIVE")
    return LoopSpecEvolutionService(specs, CandidateStore(store), PrivateHoldout()), baseline


def test_intake_freezes_a_predecessor_bound_candidate_without_holdout_content():
    service, baseline = setup_service()
    candidate = LoopSpec(baseline.spec_id, 2, baseline.entrypoint, baseline.nodes, baseline.edges, baseline.max_iterations, baseline.content_hash())

    frozen = service.intake("candidate-v2", candidate.document(), "Keep behavior while proving the v2 pipeline")

    assert frozen.manifest.status == "QUARANTINED"
    assert frozen.manifest.predecessor_hash == baseline.content_hash()
    assert "private-canary" not in str(frozen.manifest.document())


def test_proven_candidate_stops_at_promotion_review():
    service, baseline = setup_service()
    candidate = LoopSpec(baseline.spec_id, 2, baseline.entrypoint, baseline.nodes, baseline.edges, baseline.max_iterations, baseline.content_hash())
    service.intake("candidate-v2", candidate.document(), "Prove equivalent routing")

    reviewed = service.evaluate("candidate-v2")

    assert reviewed.status == "PROMOTION_REVIEW"
    assert reviewed.proof is not None
    assert reviewed.proof.holdout_task_ids == ("733cd343e6b2cb99448a92b3dd69c0f3173d0ab6823892c0b3c89e735facb90b",)
    active = service.specs.active(baseline.spec_id)
    assert active is not None
    assert active.revision == 1


def test_propose_host_binds_identity_fields():
    service, baseline = setup_service()
    document = LoopSpec(baseline.spec_id, 999, baseline.entrypoint, baseline.nodes, baseline.edges, baseline.max_iterations, "wrong").document()
    frozen = service.propose(Builder(document), "candidate-v2")
    assert frozen.spec.revision == 2
    assert frozen.spec.predecessor_hash == baseline.content_hash()


def test_regressing_candidate_is_rejected_and_never_activated():
    service, baseline = setup_service()
    candidate = LoopSpec(
        baseline.spec_id,
        2,
        baseline.entrypoint,
        baseline.nodes,
        tuple(edge for edge in baseline.edges if not (edge.source == "verify" and "exhausted" in edge.outcomes)),
        baseline.max_iterations,
        baseline.content_hash(),
    )
    with pytest.raises(ValueError, match="missing required outcomes"):
        service.intake("bad-v2", candidate.document(), "Bad candidate used to prove rejection")
    active = service.specs.active("coding-supervisor")
    assert active is not None
    assert active.revision == 1


def test_activation_requires_human_approval_and_updates_active_revision():
    service, baseline = setup_service()
    candidate = LoopSpec(baseline.spec_id, 2, baseline.entrypoint, baseline.nodes, baseline.edges, baseline.max_iterations, baseline.content_hash())
    service.intake("candidate-v2", candidate.document(), "equivalent")
    service.evaluate("candidate-v2")
    with pytest.raises(ValueError, match="human approval"):
        service.activate("candidate-v2", human_approved=False)
    activated = service.activate("candidate-v2", human_approved=True)
    assert activated.status == "ACTIVE"
    active = service.specs.active(baseline.spec_id)
    assert active is not None
    assert active.revision == 2
