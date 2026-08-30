from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any


def run_quality_gates(workspace: str | Path, acceptance: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Run explicitly configured host-owned test and coverage gates."""
    root = Path(workspace)
    evidence: list[dict[str, Any]] = []
    feedback: list[str] = []
    test_gate = acceptance.get("python_test_gate")
    if test_gate is not None:
        command = _argv(test_gate.get("command"), ["python", "-m", "pytest", "-q"])
        result, error = _run(command, root, int(test_gate.get("timeout_seconds", 300)))
        if error is not None:
            feedback.append(f"Python Test Gate failed: {error}")
        if result is None:
            result = subprocess.CompletedProcess(command, 1, "", error or "quality gate failed")
        passed = result.returncode == 0
        evidence.append(_command_evidence("python_test", command, result, passed))
        if not passed:
            feedback.append(f"Python Test Gate failed: {shlex.join(command)}")

    coverage_gate = acceptance.get("coverage_gate")
    if coverage_gate is not None:
        command = _argv(coverage_gate.get("command"), ["python", "-m", "coverage", "run", "-m", "pytest", "-q"])
        result, error = _run(command, root, int(coverage_gate.get("timeout_seconds", 300)))
        report_path = _safe_report_path(root, str(coverage_gate.get("report", ".coverage.json")))
        if error is not None:
            result = subprocess.CompletedProcess(command, 1, "", error)
        if result is None:
            result = subprocess.CompletedProcess(command, 1, "", "quality gate failed")
        report_command = _argv(coverage_gate.get("report_command"), ["python", "-m", "coverage", "json", "-o", str(report_path)])
        report_path.unlink(missing_ok=True)
        report_result, report_error = _run(report_command, root, int(coverage_gate.get("timeout_seconds", 300))) if result.returncode == 0 else (None, None)
        report = _read_report(report_path) if report_result is not None and report_result.returncode == 0 else None
        totals = report.get("totals", {}) if report else {}
        line_percent = float(totals.get("percent_covered", 0.0))
        branch_percent = _branch_percent(totals)
        minimum = float(coverage_gate.get("minimum_percent", 0.0))
        baseline = coverage_gate.get("baseline_percent")
        regression_limit = float(coverage_gate.get("max_regression_percent", 0.0))
        regression_passed = baseline is None or line_percent >= float(baseline) - regression_limit
        branch_required = bool(coverage_gate.get("require_branch", False))
        branch_minimum = coverage_gate.get("branch_minimum_percent")
        branch_passed = (not branch_required or branch_percent is not None) and (branch_minimum is None or branch_percent is not None and branch_percent >= float(branch_minimum))
        passed = result.returncode == 0 and report_error is None and report is not None and line_percent >= minimum and regression_passed and branch_passed
        evidence.append({"type": "coverage", "tool": "coverage.py", "command": command, "report_command": report_command, "exit_code": result.returncode, "report_exit_code": None if report_result is None else report_result.returncode, "line_percent": line_percent, "branch_percent": branch_percent, "minimum_percent": minimum, "baseline_percent": baseline, "max_regression_percent": regression_limit, "regression_passed": regression_passed, "branch_required": branch_required, "branch_minimum_percent": branch_minimum, "branch_passed": branch_passed, "report_hash": _hash(report_path) if report_path.is_file() else None, "passed": passed})
        if not passed:
            feedback.append("Coverage Gate failed")
    return not feedback, evidence, feedback


def _argv(value: object, default: list[str]) -> list[str]:
    if value is None:
        return default
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("quality gate command must be an argv list")


def _run(command: list[str], root: Path, timeout: int) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    try:
        return subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout, check=False), None
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return None, f"{type(error).__name__}: {error}"


def _safe_report_path(root: Path, report: str) -> Path:
    path = Path(report)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("coverage report must be a safe path inside the workspace")
    result = root / path
    try:
        result.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("coverage report must remain inside the workspace") from error
    return result


def _command_evidence(kind: str, command: list[str], result: subprocess.CompletedProcess[str], passed: bool) -> dict[str, Any]:
    return {"type": kind, "command": command, "exit_code": result.returncode, "passed": passed, "stdout_hash": hashlib.sha256(result.stdout.encode()).hexdigest(), "stderr_hash": hashlib.sha256(result.stderr.encode()).hexdigest(), "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _branch_percent(totals: dict[str, Any]) -> float | None:
    covered = totals.get("covered_branches")
    total = totals.get("num_branches")
    return None if not isinstance(covered, int) or not isinstance(total, int) or total == 0 else covered / total * 100


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
