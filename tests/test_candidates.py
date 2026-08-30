import pytest

from loopgraph_supervisor.candidate_store import CandidateStore
from loopgraph_supervisor.candidates import CandidateManifest, CandidateProof, with_proof
from loopgraph_supervisor.store import SQLiteStore


def candidate():
    return CandidateManifest("candidate-1", "coding-supervisor", "loopspec", 1, "a" * 64, "b" * 64, ("spec.yaml",), "Improve retry routing")


def proof():
    return CandidateProof("coding-supervisor", "a" * 64, "b" * 64, "1" * 64, "c" * 64, "d" * 64, 1, 1, 0, ("holdout-1",), ("e" * 64,))


def test_candidate_is_quarantined_until_proof_is_attached():
    item = candidate()
    assert item.status == "QUARANTINED"
    validated = with_proof(item, proof())
    assert validated.status == "VALIDATED"
    assert validated.proof == proof()

    with pytest.raises(ValueError, match="proof"):
        CandidateManifest(**{**item.__dict__, "status": "ACTIVE"})


def test_candidate_registry_preserves_manifest_identity_and_transitions():
    registry = CandidateStore(SQLiteStore(":memory:"))
    item = candidate()
    registry.save(item)
    registry.transition(item.candidate_id, "VALIDATED", proof())
    reviewed = registry.transition(item.candidate_id, "PROMOTION_REVIEW")

    assert reviewed.status == "PROMOTION_REVIEW"
    stored = registry.get(item.candidate_id)
    assert stored is not None
    assert reviewed.manifest_hash() == stored.manifest_hash()
    with pytest.raises(ValueError, match="immutable"):
        registry.save(CandidateManifest(**{**item.__dict__, "rationale": "changed"}))

    with pytest.raises(ValueError, match="illegal"):
        registry.transition(item.candidate_id, "QUARANTINED")
    with pytest.raises(ValueError, match="proof is immutable"):
        registry.transition(item.candidate_id, "REJECTED", CandidateProof("coding-supervisor", "a" * 64, "b" * 64, "2" * 64, "e" * 64, "f" * 64, 1, 1, 0, ("hidden",), ("a" * 64,)))
    with pytest.raises(ValueError, match="illegal"):
        registry.transition(item.candidate_id, "ACTIVE")
