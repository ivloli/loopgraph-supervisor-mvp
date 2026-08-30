from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Split = Literal["validation", "canary"]


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        raise ValueError(f"fixture root is not a directory: {root}")
    for path in sorted(item for item in root.rglob("*") if item.is_file() or item.is_symlink()):
        relative = path.relative_to(root).as_posix()
        digest.update(b"\0path\0" + relative.encode())
        stat = path.lstat()
        digest.update(b"\0mode\0" + str(stat.st_mode).encode())
        if path.is_symlink():
            raise ValueError(f"holdout fixtures cannot contain symlinks: {relative}")
        else:
            digest.update(b"\0file\0" + path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class FilesystemEvalTask:
    task_id: str
    target_id: str
    split: Split
    prompt: str
    fixture_root: Path
    verifier: tuple[str, ...]
    verifier_hash: str
    timeout_seconds: float
    forbidden_paths: tuple[str, ...]
    task_hash: str
    fixture_hash: str


class FilesystemHoldoutRepository:
    """Loads immutable tasks from a host-only root outside candidate workspaces."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def tasks(self, target_id: str, split: Split) -> tuple[FilesystemEvalTask, ...]:
        if not target_id or target_id.startswith("/") or ".." in target_id.split("/"):
            raise ValueError("holdout target id must be a safe relative id")
        split_root = self.root / target_id / split
        if not split_root.is_dir():
            raise ValueError(f"holdout split does not exist: {target_id}/{split}")
        tasks = tuple(self._load_task(path, target_id, split) for path in sorted(split_root.iterdir()) if path.is_dir())
        if not tasks:
            raise ValueError(f"holdout split has no tasks: {target_id}/{split}")
        return tasks

    def _load_task(self, directory: Path, target_id: str, split: Split) -> FilesystemEvalTask:
        descriptor_path = directory / "task.json"
        fixture_root = (directory / "fixture").resolve()
        if not descriptor_path.is_file() or fixture_root.parent != directory.resolve():
            raise ValueError(f"invalid holdout task layout: {directory}")
        document: dict[str, Any] = json.loads(descriptor_path.read_text())
        raw_verifier = document.get("verifier", [])
        if not isinstance(raw_verifier, list):
            raise ValueError("holdout verifier must be a non-empty argv array")
        verifier = tuple(raw_verifier)
        forbidden = tuple(document.get("forbidden_paths", []))
        if document.get("schema_version") != 1 or document.get("id") != directory.name or not document.get("prompt"):
            raise ValueError(f"invalid holdout task descriptor: {descriptor_path}")
        if not verifier or any(not isinstance(item, str) or not item for item in verifier):
            raise ValueError("holdout verifier must be a non-empty argv array")
        executable = Path(verifier[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("holdout verifier executable must be an absolute frozen file")
        verifier_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        if any(path.startswith("/") or ".." in path.split("/") for path in forbidden):
            raise ValueError("holdout forbidden paths must be safe relative paths")
        fixture_hash = hash_tree(fixture_root)
        canonical = json.dumps({**document, "target_id": target_id, "split": split, "fixture_hash": fixture_hash, "verifier_hash": verifier_hash}, sort_keys=True, separators=(",", ":"))
        task_hash = hashlib.sha256(canonical.encode()).hexdigest()
        return FilesystemEvalTask(directory.name, target_id, split, document["prompt"], fixture_root, verifier, verifier_hash, float(document.get("timeout_seconds", 30)), forbidden, task_hash, fixture_hash)
