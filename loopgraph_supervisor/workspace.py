import os
import re
import subprocess
from pathlib import Path

from .git_workspace import GitWorkspace


class WorkspaceManager:
    def __init__(self, root: str | None = None):
        configured = root or os.getenv("LOOPGRAPH_WORKTREE_ROOT") or str(Path.home() / ".dsh" / "loopgraph-worktrees")
        self.root = Path(configured)

    def prepare(self, workflow_id: str, source: str) -> str:
        source_path = Path(source).resolve()
        git = GitWorkspace(str(source_path))
        if not git.available:
            raise ValueError("isolated coding workflow requires a Git workspace")
        if git.changed_files():
            raise ValueError("source workspace must be clean before creating an isolated workflow")
        safe_id = re.sub(r"[^a-zA-Z0-9._-]", "-", workflow_id)
        if not safe_id:
            raise ValueError("workflow id cannot produce an empty worktree name")
        target = (self.root / safe_id).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = GitWorkspace(str(target))
            if existing.available:
                return str(target)
            raise ValueError(f"worktree target exists but is not a Git worktree: {target}")
        result = subprocess.run(["git", "-C", str(source_path), "worktree", "add", "--detach", str(target), "HEAD"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "failed to create isolated Git worktree")
        return str(target)
