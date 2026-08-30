import subprocess

import pytest

from loopgraph_supervisor.adapters import CommandVerifier, FakeAgent, FakeVerifier
from loopgraph_supervisor.domain import AgentInput, AgentOutput, Workflow, WorkflowStatus
from loopgraph_supervisor.git_workspace import GitWorkspace
from loopgraph_supervisor.store import SQLiteStore
from loopgraph_supervisor.supervisor import Supervisor
from loopgraph_supervisor.workspace import WorkspaceManager


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_command_verifier_rejects_empty_contract(tmp_path):
    result = CommandVerifier().verify(AgentOutput({"response": "done"}, "done"), {"workspace": str(tmp_path), "commands": []})
    assert result.passed is False
    assert result.evidence[0]["type"] == "acceptance_contract"


def test_sdk_adapter_rejects_empty_response(monkeypatch, tmp_path):
    class Result:
        final_response = ""

    class Harness:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def run(self, prompt, session_id): return Result()

    import deepseek_harness  # type: ignore[import-untyped]
    monkeypatch.setattr(deepseek_harness, "DeepSeekHarness", Harness)
    from loopgraph_supervisor.adapters import DeepSeekHarnessAgent
    request = AgentInput("wf-empty", "goal", 1, acceptance={"workspace": str(tmp_path)})
    try:
        DeepSeekHarnessAgent(str(tmp_path), str(tmp_path / "sessions")).execute(request)
        assert False, "empty response must fail"
    except RuntimeError as error:
        assert "empty final response" in str(error)


def test_git_promotion_is_idempotent_after_commit(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "LoopGraph Test")
    git(tmp_path, "config", "user.email", "loopgraph@example.test")
    source = tmp_path / "artifact.txt"
    source.write_text("baseline\n")
    git(tmp_path, "add", "artifact.txt")
    git(tmp_path, "commit", "-m", "baseline")
    source.write_text("candidate\n")

    workspace = GitWorkspace(str(tmp_path))
    first = workspace.promote("wf-idempotent", 1)
    second = workspace.promote("wf-idempotent", 1)

    assert first == second
    assert workspace.changed_files() == []


def test_git_scope_and_fingerprint_cover_renames_and_spaces(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "LoopGraph Test")
    git(tmp_path, "config", "user.email", "loopgraph@example.test")
    (tmp_path / "old name.txt").write_text("baseline\n")
    git(tmp_path, "add", "old name.txt")
    git(tmp_path, "commit", "-m", "baseline")
    git(tmp_path, "mv", "old name.txt", "new name.txt")
    workspace = GitWorkspace(str(tmp_path))

    assert workspace.changed_files() == ["new name.txt", "old name.txt"]
    before = workspace.candidate_fingerprint()
    (tmp_path / "new name.txt").write_text("changed\n")
    assert workspace.candidate_fingerprint() != before


def test_workspace_manager_creates_run_isolated_worktrees(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "LoopGraph Test")
    git(source, "config", "user.email", "loopgraph@example.test")
    (source / "artifact.txt").write_text("baseline\n")
    git(source, "add", "artifact.txt")
    git(source, "commit", "-m", "baseline")
    manager = WorkspaceManager(str(tmp_path / "worktrees"))

    first = manager.prepare("workflow-one", str(source))
    second = manager.prepare("workflow-two", str(source))
    (tmp_path / "worktrees" / "workflow-one" / "artifact.txt").write_text("one\n")

    assert first != second
    assert (source / "artifact.txt").read_text() == "baseline\n"
    assert (tmp_path / "worktrees" / "workflow-two" / "artifact.txt").read_text() == "baseline\n"


def test_started_execution_intent_enters_uncertain_recovery_gate():
    store = SQLiteStore(":memory:")
    workflow = Workflow("wf-recover", "recover task", attempt=1, acceptance={"require_promotion_approval": False})
    store.create_workflow(workflow)
    store.save_contract(workflow.id, workflow.acceptance)
    request = AgentInput(workflow.id, workflow.goal, 1)
    store.start_execution(workflow.id, 1, "wf-recover:1", request.__dict__)
    agent = FakeAgent(["candidate"])

    supervisor = Supervisor(store, agent, FakeVerifier(pass_on=1))
    result = supervisor.run(workflow.id)

    assert result.status == WorkflowStatus.UNCERTAIN
    assert result.attempt == 1
    assert agent.calls == 0

    recovered = supervisor.recover_uncertain(workflow.id, "verify-existing")
    assert recovered.status == WorkflowStatus.COMPLETED
    assert recovered.attempt == 1
    assert agent.calls == 0


def test_dsh_exception_enters_uncertain_instead_of_automatic_new_attempt():
    class AmbiguousAgent:
        calls = 0

        def execute(self, request):
            self.calls += 1
            raise ConnectionError("remote may have executed before disconnect")

    store = SQLiteStore(":memory:")
    agent = AmbiguousAgent()
    supervisor = Supervisor(store, agent, FakeVerifier(pass_on=1))

    result = supervisor.start("wf-ambiguous", "do one remote operation", 2, {"require_promotion_approval": False})

    assert result.status == WorkflowStatus.UNCERTAIN
    assert result.attempt == 1
    assert agent.calls == 1
    intent = store.get_open_execution(result.id)
    assert intent is not None
    assert intent["status"] == "STARTED"


def test_approval_is_bound_to_verified_candidate_fingerprint(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "LoopGraph Test")
    git(tmp_path, "config", "user.email", "loopgraph@example.test")
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("baseline\n")
    git(tmp_path, "add", "artifact.txt")
    git(tmp_path, "commit", "-m", "baseline")

    class WritingAgent:
        def execute(self, request):
            artifact.write_text("candidate\n")
            return AgentOutput({"response": "candidate"}, "candidate", "review-session")

    store = SQLiteStore(":memory:")
    supervisor = Supervisor(store, WritingAgent(), FakeVerifier(pass_on=1))
    result = supervisor.start("wf-bound-approval", "review exact candidate", 1, {"workspace": str(tmp_path), "isolate": False, "allowed_files": ["artifact.txt"]})
    assert result.status == WorkflowStatus.WAITING_HITL

    original_contract = dict(result.acceptance)
    store.save_contract(result.id, {**original_contract, "allowed_files": ["artifact.txt", "unreviewed.txt"]})
    with pytest.raises(ValueError, match="spec revision"):
        supervisor.decide_hitl(result.id, "approve")
    store.save_contract(result.id, original_contract)

    artifact.write_text("changed after review\n")

    with pytest.raises(ValueError, match="candidate changed after review"):
        supervisor.decide_hitl(result.id, "approve")
    assert store.get_workflow(result.id).status == WorkflowStatus.WAITING_HITL


def test_matching_approval_binding_promotes_exact_candidate(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "LoopGraph Test")
    git(tmp_path, "config", "user.email", "loopgraph@example.test")
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("baseline\n")
    git(tmp_path, "add", "artifact.txt")
    git(tmp_path, "commit", "-m", "baseline")

    class WritingAgent:
        def execute(self, request):
            artifact.write_text("candidate\n")
            return AgentOutput({"response": "candidate"}, "candidate", "realistic-session")

    store = SQLiteStore(":memory:")
    supervisor = Supervisor(store, WritingAgent(), FakeVerifier(pass_on=1))
    waiting = supervisor.start("wf-valid-approval", "promote exact candidate", 1, {"workspace": str(tmp_path), "isolate": False, "allowed_files": ["artifact.txt"]})
    assert waiting.status == WorkflowStatus.WAITING_HITL

    completed = supervisor.decide_hitl(waiting.id, "approve")

    assert completed.status == WorkflowStatus.COMPLETED
    assert GitWorkspace(str(tmp_path)).changed_files() == []
    assert store.get_version(completed.id, completed.active_version) is not None


def test_failed_verification_hitl_cannot_be_approved():
    supervisor = Supervisor(SQLiteStore(":memory:"), FakeAgent(["candidate"]), FakeVerifier(pass_on=99))
    result = supervisor.start("wf-failed-approval", "must not promote", 1)
    assert result.status == WorkflowStatus.WAITING_HITL
    with pytest.raises(ValueError, match="verified promotion_review"):
        supervisor.decide_hitl(result.id, "approve")
