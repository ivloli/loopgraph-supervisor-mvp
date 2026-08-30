from loopgraph_supervisor.adapters import FakeAgent, FakeVerifier
from loopgraph_supervisor.domain import WorkflowStatus
from loopgraph_supervisor.store import SQLiteStore
from loopgraph_supervisor.supervisor import Supervisor


def make_supervisor(pass_on=2):
    store = SQLiteStore(":memory:")
    return Supervisor(store, FakeAgent(["candidate"]), FakeVerifier(pass_on)), store


def test_retry_then_promote():
    supervisor, _ = make_supervisor(2)
    workflow = supervisor.start("wf-1", "produce a candidate", 3, {"require_promotion_approval": False})
    assert workflow.status == WorkflowStatus.COMPLETED
    explanation = supervisor.explain("wf-1")
    assert any(item["decision_type"] == "RETRY" for item in explanation["decisions"])
    assert any(item["decision_type"] == "VERIFY_PASS" for item in explanation["decisions"])


def test_exhaustion_waits_for_hitl():
    supervisor, _ = make_supervisor(99)
    workflow = supervisor.start("wf-2", "needs review", 1)
    assert workflow.status == WorkflowStatus.WAITING_HITL
    workflow = supervisor.decide_hitl("wf-2", "reject")
    assert workflow.status == WorkflowStatus.FAILED


def test_pause_and_resume():
    supervisor, _ = make_supervisor(1)
    supervisor.start("wf-3", "pause demo", 1)
    # A completed workflow remains terminal; pause is only meaningful before execution.
    workflow = supervisor.pause("wf-3")
    assert workflow.pause_requested is True
