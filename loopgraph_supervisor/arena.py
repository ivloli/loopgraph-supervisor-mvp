from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .holdout import FilesystemEvalTask, hash_tree


@dataclass(frozen=True)
class ArenaArm:
    side: str
    task_hash: str
    fixture_hash: str
    output_hash: str
    candidate_hash: str
    passed: bool
    exit_code: int | None
    stdout: str
    stderr: str
    contamination: tuple[str, ...]

    def receipt_hash(self) -> str:
        payload = "\0".join((self.side, self.task_hash, self.fixture_hash, self.output_hash, self.candidate_hash, str(self.passed), str(self.exit_code), *self.contamination))
        return hashlib.sha256(payload.encode()).hexdigest()


class FilesystemArena:
    """Runs verifier argv in sibling copies without exposing the holdout root."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).resolve() if root else Path(tempfile.mkdtemp(prefix="loopgraph-arena-"))

    def run(self, task: FilesystemEvalTask, side: str, candidate_hash: str, mutate=None) -> ArenaArm:
        if not side or side.startswith("/") or ".." in side.split("/"):
            raise ValueError("arena side must be a safe relative id")
        if mutate is not None:
            raise ValueError("untrusted candidate mutation is disabled; use a DSH sandbox adapter")
        workspace = self.root / task.task_hash / side
        if workspace.exists():
            raise ValueError(f"arena arm already exists: {workspace}")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(task.fixture_root, workspace, symlinks=True)
        before = hash_tree(workspace)
        if before != task.fixture_hash:
            raise ValueError("holdout fixture changed after task freeze")
        stdout: str
        stderr: str
        try:
            result = subprocess.run(task.verifier, cwd=workspace, capture_output=True, text=True, timeout=task.timeout_seconds, check=False, env={"PATH": os.environ.get("PATH", "")})
            exit_code: int | None = result.returncode
            stdout, stderr = result.stdout[-4000:], result.stderr[-4000:]
        except subprocess.TimeoutExpired as error:
            exit_code = None
            timeout_stdout = error.stdout
            stdout = cast(str, timeout_stdout)[-4000:] if isinstance(timeout_stdout, str) else ""
            stderr = "verifier timeout"
        contamination = []
        for relative in task.forbidden_paths:
            if (workspace / relative).exists():
                contamination.append(relative)
        output_hash = hash_tree(workspace)
        return ArenaArm(side, task.task_hash, task.fixture_hash, output_hash, candidate_hash, exit_code == 0 and not contamination, exit_code, stdout, stderr, tuple(sorted(set(contamination))))
