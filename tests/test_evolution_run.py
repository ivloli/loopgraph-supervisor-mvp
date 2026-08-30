import pytest

from loopgraph_supervisor.evolution_run import EvolutionRun, EvolutionRunStore
from loopgraph_supervisor.store import SQLiteStore


def test_evolution_run_records_pr_merge_and_activation_lifecycle():
    store = EvolutionRunStore(SQLiteStore(":memory:"))
    store.save(EvolutionRun("run-1", "trigger-1", "coding-supervisor", 1, "a" * 64, "candidate", "b" * 64, status="PROMOTION_REVIEW"))

    store.attach_pr("run-1", 7, "c" * 40)
    store.record_merge("run-1", "d" * 40)
    store.record_activation("run-1", "v2", True)

    run = store.get("run-1")
    assert run is not None
    assert (run.pr_number, run.pr_head, run.merge_commit, run.active_version, run.status) == (7, "c" * 40, "d" * 40, "v2", "ACTIVE")


def test_evolution_run_rejects_stale_lifecycle_transition():
    store = EvolutionRunStore(SQLiteStore(":memory:"))
    store.save(EvolutionRun("run-1", "trigger-1", "coding-supervisor", 1, "a" * 64, status="REQUESTED"))

    with pytest.raises(ValueError, match="ready for PR"):
        store.attach_pr("run-1", 1, "c" * 40)
