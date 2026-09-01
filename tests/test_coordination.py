import pytest

from loopgraph_supervisor.coordination import CandidateApproval, CandidateClaim, CandidateCoordinator, CoordinatedCandidate, CoordinationStore
from loopgraph_supervisor.store import SQLiteStore


def candidate(candidate_id, parent, action):
    return CoordinatedCandidate(candidate_id, parent, f"{candidate_id}-head", ("supervisor_policy.py",), (CandidateClaim("synthetic_fallback", action),))


def test_opposite_ai_candidates_are_blocked_from_forming_independent_prs():
    coordinator = CandidateCoordinator("commit-v1")
    coordinator.register(candidate("candidate-remove", "commit-v1", "remove"))
    result = coordinator.register(candidate("candidate-retain", "commit-v1", "retain"))

    assert result.conflict_subjects == ("synthetic_fallback",)
    assert result.conflicting_candidate_ids == ("candidate-remove", "candidate-retain")
    assert result.can_open_pr is False
    with pytest.raises(ValueError, match="conflicting"):
        coordinator.select("candidate-remove")


def test_selected_candidate_is_the_only_one_eligible_for_a_pr():
    coordinator = CandidateCoordinator("commit-v1")
    result = coordinator.register(candidate("candidate-change", "commit-v1", "change"))
    selected = coordinator.select("candidate-change")

    assert result.can_open_pr is False
    assert selected.selected_candidate_id == "candidate-change"
    assert selected.can_open_pr is True


def test_approval_is_bound_to_the_selected_candidate_head():
    store = CoordinationStore(SQLiteStore(":memory:"))
    proposal = candidate("candidate-change", "commit-v1", "change")
    store.register(proposal)
    store.select("commit-v1", "candidate-change")

    store.authorize_pr("candidate-change", CandidateApproval("candidate-change", "commit-v1", "candidate-change-head", "human"))
    with pytest.raises(ValueError, match="stale"):
        store.authorize_pr("candidate-change", CandidateApproval("candidate-change", "commit-v1", "old-head", "human"))


def test_durable_approval_reloads_state_after_a_new_conflicting_candidate():
    database = SQLiteStore(":memory:")
    store = CoordinationStore(database)
    proposal = candidate("candidate-remove", "commit-v1", "remove")
    store.register(proposal)
    store.select("commit-v1", "candidate-remove")
    store.register(candidate("candidate-retain", "commit-v1", "retain"))

    with pytest.raises(ValueError, match="selected as the coordinated PR"):
        store.authorize_pr("candidate-remove", CandidateApproval("candidate-remove", "commit-v1", "candidate-remove-head", "human"))


def test_stale_candidate_cannot_enter_current_coordination_set():
    coordinator = CandidateCoordinator("commit-v2")
    with pytest.raises(ValueError, match="stale"):
        coordinator.register(candidate("old", "commit-v1", "remove"))


def test_coordination_and_selection_survive_store_reconstruction():
    database = SQLiteStore(":memory:")
    store = CoordinationStore(database)
    store.register(candidate("candidate-change", "commit-v1", "change"))
    store.select("commit-v1", "candidate-change")

    restored = CoordinationStore(database).result("commit-v1")

    assert restored.selected_candidate_id == "candidate-change"
    assert restored.can_open_pr is True
