import os
import subprocess
from typing import Any

from .domain import AgentInput, AgentOutput, Verification
from .quality_gate import run_quality_gates
from .runtime_facts import summarize_dsh_run


class DeepSeekHarnessAgent:
    """Official DSH SDK adapter. The SDK import is lazy so fake tests need no API key."""

    def __init__(self, workspace: str, session_root: str, cordis: str | None = None, model: str | None = None):
        self.workspace = workspace
        self.session_root = session_root
        self.cordis = cordis
        self.model: str = model or os.getenv("DSH_MODEL") or "deepseek-v4-flash"

    def execute(self, request: AgentInput) -> AgentOutput:
        from deepseek_harness import DeepSeekHarness  # type: ignore[import-untyped]

        workspace = request.acceptance.get("workspace", self.workspace)
        config: dict[str, Any] = {"provider": "deepseek-official", "model": self.model, "max_tokens": 49152, "cwd": workspace, "session_root": self.session_root}
        if self.cordis:
            config["cordis"] = self.cordis
        session_id = f"workflow-{request.workflow_id}"
        prompt = request.goal
        if request.feedback:
            prompt += f"\n\nPrevious verification feedback:\n{request.feedback}"
        if request.proposal:
            prompt += f"\n\nApproved improvement proposal:\n{request.proposal}"
        if request.acceptance:
            prompt += f"\n\nAcceptance contract:\n{request.acceptance}"
        with DeepSeekHarness(**config) as harness:
            result = harness.run(prompt, session_id=session_id)
        response = result.final_response or ""
        if not response.strip():
            raise RuntimeError("DeepSeek Harness returned an empty final response; refusing synthetic success")
        runtime = summarize_dsh_run(result, model=self.model, workspace=workspace, expected_session_id=session_id)
        return AgentOutput({"response": response, "runtime": runtime}, response, session_id)


class CommandVerifier:
    def verify(self, output: AgentOutput, acceptance: dict) -> Verification:
        workspace = acceptance.get("workspace", os.getcwd())
        commands = acceptance.get("commands", [])
        evidence = []
        feedback = []
        passed = bool(commands)
        if not commands:
            evidence.append({"type": "acceptance_contract", "passed": False, "reason": "at least one acceptance command is required"})
            feedback.append("No acceptance commands were configured")
        for command in commands:
            result = subprocess.run(command, cwd=workspace, shell=True, capture_output=True, text=True, timeout=300)
            command_passed = result.returncode == 0
            passed = passed and command_passed
            evidence.append({"type": "command", "command": command, "exit_code": result.returncode, "passed": command_passed, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]})
            if not command_passed:
                feedback.append(f"{command} failed with exit code {result.returncode}: {result.stdout[-2000:]} {result.stderr[-2000:]}")
        quality_passed, quality_evidence, quality_feedback = run_quality_gates(workspace, acceptance)
        evidence.extend(quality_evidence)
        feedback.extend(quality_feedback)
        passed = passed and quality_passed
        if os.path.exists(os.path.join(workspace, ".git")):
            diff = subprocess.run("git diff --check", cwd=workspace, shell=True, capture_output=True, text=True, timeout=60)
            diff_passed = diff.returncode == 0
            evidence.append({"type": "git_diff_check", "exit_code": diff.returncode, "passed": diff_passed, "output": diff.stdout + diff.stderr})
            passed = passed and diff_passed
        else:
            evidence.append({"type": "git_diff_check", "passed": True, "not_applicable": True, "reason": "workspace is not a git repository"})
        if not passed and not feedback:
            feedback.append("git diff --check failed")
        return Verification(passed, "\n".join(feedback) if feedback else "All acceptance commands and diff checks passed", evidence)


class FakeAgent:
    def __init__(self, responses: list[str] | None = None):
        self.responses = responses or ["demo response"]
        self.calls = 0

    def execute(self, request: AgentInput) -> AgentOutput:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return AgentOutput({"response": response, "attempt": request.attempt}, response, f"fake-{request.workflow_id}")


class FakeVerifier:
    def __init__(self, pass_on: int = 2):
        self.pass_on = pass_on
        self.calls = 0

    def verify(self, output: AgentOutput, acceptance: dict) -> Verification:
        self.calls += 1
        passed = self.calls >= self.pass_on
        return Verification(passed, "accepted" if passed else "missing acceptance evidence", [{"rule": "fake_threshold", "passed": passed}])
