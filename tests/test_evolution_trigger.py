from loopgraph_supervisor.evolution_trigger import EvolutionTriggerStore, HumanFeedback, TaskFeedback, trigger_from_feedback, trigger_from_human
from loopgraph_supervisor.store import SQLiteStore


def test_evolution_requires_observed_task_failure():
    feedback = (TaskFeedback("workflow-1", 1, True, "passed"),)

    assert trigger_from_feedback("coding-supervisor", feedback) is None


def test_failed_task_feedback_creates_host_owned_trigger():
    feedback = (TaskFeedback("workflow-1", 1, False, "verifier rejected fallback"),)

    trigger = trigger_from_feedback("coding-supervisor", feedback)

    assert trigger is not None
    assert trigger.evidence == feedback
    assert "observed task failure" in trigger.reason


def test_human_can_trigger_rsi_without_waiting_for_failure_threshold():
    trigger = trigger_from_human("coding-supervisor", "DDHH", "Remove synthetic fallback but preserve human_gate")

    assert trigger.source == "human_feedback"
    assert trigger.evidence == (HumanFeedback("DDHH", "Remove synthetic fallback but preserve human_gate"),)


def test_task_and_human_triggers_survive_store_reconstruction():
    store = EvolutionTriggerStore(SQLiteStore(":memory:"))
    trigger = trigger_from_human("coding-supervisor", "DDHH", "Review retry policy")
    store.save("trigger-1", trigger)

    restored = EvolutionTriggerStore(store.store).get("trigger-1")

    assert restored == trigger
