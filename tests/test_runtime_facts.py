from dataclasses import dataclass
from typing import cast

from loopgraph_supervisor.domain import Workflow
from loopgraph_supervisor.runtime_facts import summarize_dsh_run
from loopgraph_supervisor.store import SQLiteStore


@dataclass
class Notification:
    method: str


class Result:
    session_id = "workflow-wf"
    finish_reason = "stop"
    events = [
        {"type": "agent/inbox/spliced", "data": {"private": "not persisted directly"}},
        {"type": "tool/result", "data": {"output": "sensitive"}},
        {"type": "assistant/message", "data": {}},
    ]
    notifications = [Notification("session.event"), Notification("session.status")]


def test_runtime_facts_capture_agent_activity_without_payloads(tmp_path):
    facts = summarize_dsh_run(Result(), model="deepseek-v4-flash", workspace=str(tmp_path), expected_session_id="workflow-wf")

    assert facts["event_count"] == 3
    assert cast(dict[str, int], facts["event_types"])["tool/result"] == 1
    assert facts["tool_event_types"] == ["tool/result"]
    assert facts["notification_methods"] == {"session.event": 1, "session.status": 1}
    assert facts["payloads_persisted"] is False
    assert "sensitive" not in str(facts)


def test_runtime_facts_are_observable_from_durable_attempts():
    store = SQLiteStore(":memory:")
    workflow = Workflow("wf", "goal")
    store.create_workflow(workflow)
    store.save_attempt("wf", 1, "wf:1", {"goal": "goal"}, {"response": "done", "runtime": {"type": "dsh_runtime", "session_id": "workflow-wf"}}, "workflow-wf")

    explanation = store.explain("wf")

    assert explanation["attempts"][0]["runtime"] == {"type": "dsh_runtime", "session_id": "workflow-wf"}
    assert "output" not in explanation["attempts"][0]
