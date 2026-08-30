from __future__ import annotations

import json
from pathlib import Path

from .builder import CandidateBuildRequest
from .evaluation import LoopSpecEvalTask
from .loopspec import LoopSpec


def prepare_builder_workspace(root: str | Path, candidate_id: str, active: LoopSpec, validation_tasks: tuple[LoopSpecEvalTask, ...]) -> tuple[Path, CandidateBuildRequest]:
    if not candidate_id or candidate_id.startswith("/") or ".." in candidate_id.split("/"):
        raise ValueError("candidate id must be a safe relative id")
    workspace = Path(root).resolve() / candidate_id
    workspace.mkdir(parents=True, exist_ok=False)
    validation_context: tuple[dict[str, object], ...] = tuple(
        {"task_id": task.task_id, "initial_node": task.initial_node, "outcomes": list(task.outcomes), "expected_targets": list(task.expected_targets)}
        for task in validation_tasks
        if task.split == "validation"
    )
    if not validation_context:
        raise ValueError("Builder requires visible validation context")
    (workspace / "active-spec.json").write_text(json.dumps(active.document(), ensure_ascii=False, indent=2))
    (workspace / "validation-context.json").write_text(json.dumps(validation_context, ensure_ascii=False, indent=2))
    return workspace, CandidateBuildRequest(candidate_id, active.document(), validation_context)
