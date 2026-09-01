from loopgraph_supervisor.adapters import FakeAgent
from loopgraph_supervisor.domain import AgentOutput, Verification
from loopgraph_supervisor.evolution_trigger import EvolutionTriggerStore, TaskFeedback, failure_signature, normalize_feedback
from loopgraph_supervisor.store import SQLiteStore
from loopgraph_supervisor.supervisor import Supervisor


def test_feedback_signature_normalizes_order_and_whitespace():
    first = (TaskFeedback("one", 1, False, "Missing  contract"), TaskFeedback("two", 1, False, "missing contract"))
    second = (TaskFeedback("two", 1, False, "MISSING CONTRACT"), TaskFeedback("one", 1, False, "missing contract"))
    assert normalize_feedback(" Missing   contract ") == "missing contract"
    assert failure_signature(first) == failure_signature(second)


def test_repeated_task_failures_create_one_trigger():
    store = EvolutionTriggerStore(SQLiteStore(":memory:"))
    failures = (TaskFeedback("one", 1, False, "missing contract"), TaskFeedback("two", 1, False, "missing contract"))
    created = store.create_task_feedback_request("coding-supervisor", failures)
    assert created is not None
    assert store.create_task_feedback_request("coding-supervisor", failures) is None


def test_supervisor_records_trigger_after_repeated_failures():
    class AlwaysFail:
        def verify(self, output: AgentOutput, acceptance: dict) -> Verification:
            return Verification(False, "same deterministic verifier failure", [])

    store = SQLiteStore(":memory:")
    supervisor = Supervisor(store, FakeAgent(["candidate"]), AlwaysFail())
    supervisor.start("wf-one", "task", 1)
    supervisor.start("wf-two", "task", 1)
    assert store.db.execute("SELECT COUNT(*) FROM evolution_triggers WHERE source='task_feedback'").fetchone()[0] == 1
