from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, cast

from .loopspec import LoopSpec, Outcome
from .loopspec_interpreter import LoopSpecInterpreter

Split = Literal["validation", "canary"]


@dataclass(frozen=True)
class LoopSpecEvalTask:
    task_id: str
    split: Split
    initial_node: str
    outcomes: tuple[str, ...]
    expected_targets: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.task_id or not self.outcomes or len(self.outcomes) != len(self.expected_targets):
            raise ValueError("LoopSpec evaluation tasks require a non-empty id and aligned outcomes/targets")

    def content_hash(self) -> str:
        payload = json.dumps({"task_id": self.task_id, "split": self.split, "initial_node": self.initial_node, "outcomes": self.outcomes, "expected_targets": self.expected_targets}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    split: Split
    passed: bool
    actual_targets: tuple[str, ...]
    expected_targets: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class EvaluationReport:
    candidate_hash: str
    split: Split
    results: tuple[TaskResult, ...]
    pass_rate: float
    regression_count: int

    def proof_hash(self) -> str:
        payload = {
            "candidate_hash": self.candidate_hash,
            "split": self.split,
            "results": [result.__dict__ for result in self.results],
            "pass_rate": self.pass_rate,
            "regression_count": self.regression_count,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evaluate_spec(spec: LoopSpec, tasks: tuple[LoopSpecEvalTask, ...], split: Split) -> EvaluationReport:
    selected = tuple(task for task in tasks if task.split == split)
    if not selected:
        raise ValueError(f"no LoopSpec evaluation tasks for split {split}")
    interpreter = LoopSpecInterpreter(spec)
    results: list[TaskResult] = []
    for task in selected:
        actual: list[str] = []
        error: str | None = None
        try:
            for iteration, outcome in enumerate(task.outcomes):
                actual.append(interpreter.transition(task.initial_node if iteration == 0 else actual[-1], cast("Outcome", outcome), iteration).target)
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
        results.append(TaskResult(task.task_id, task.split, error is None and tuple(actual) == task.expected_targets, tuple(actual), task.expected_targets, error))
    passed = sum(result.passed for result in results)
    return EvaluationReport(spec.content_hash(), split, tuple(results), passed / len(results), 0)


def compare_specs(baseline: LoopSpec, candidate: LoopSpec, tasks: tuple[LoopSpecEvalTask, ...], split: Split = "validation") -> tuple[EvaluationReport, EvaluationReport]:
    baseline_report = evaluate_spec(baseline, tasks, split)
    candidate_report = evaluate_spec(candidate, tasks, split)
    regressions = sum(
        baseline_result.passed and not candidate_result.passed
        for baseline_result, candidate_result in zip(baseline_report.results, candidate_report.results)
    )
    return baseline_report, EvaluationReport(candidate_report.candidate_hash, split, candidate_report.results, candidate_report.pass_rate, regressions)


def default_eval_tasks() -> tuple[LoopSpecEvalTask, ...]:
    return (
        LoopSpecEvalTask("success-to-human", "validation", "verify", ("approve",), ("hitl",)),
        LoopSpecEvalTask("failure-to-retry", "validation", "verify", ("retry",), ("execute",)),
        LoopSpecEvalTask("exhaustion-to-human", "canary", "verify", ("exhausted",), ("hitl",)),
    )
