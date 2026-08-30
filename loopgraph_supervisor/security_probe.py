from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProbeResult:
    sandbox_available: bool
    workspace_write_allowed: bool
    host_write_denied: bool
    holdout_read_denied: bool
    ambient_secret_absent: bool
    ready_for_blind_holdout: bool

    def document(self) -> dict[str, bool]:
        return asdict(self)


def run_macos_builder_probe(root: str | Path) -> ProbeResult:
    """Probe rc.2's local macOS file policy without model credentials."""

    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        return ProbeResult(False, False, False, False, False, False)
    root = Path(root).resolve()
    workspace = root / "workspace"
    host = root / "host"
    holdout = root / "holdout"
    for directory in (workspace, host, holdout):
        directory.mkdir(parents=True, exist_ok=False)
    (holdout / "answer.txt").write_text("blind-answer")
    profile = "\n".join(("(version 1)", "(allow default)", "(deny file-write*)", '(allow file-write* (literal "/dev/null"))', f'(allow file-write* (subpath "{workspace}"))'))

    def run(script: str, env=None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", sandbox_exec, "-p", profile, "/bin/sh", "-c", script], capture_output=True, text=True, check=False, env=env)

    workspace_write = run(f'printf ok > "{workspace / "write.txt"}"').returncode == 0
    host_write = run(f'printf bad > "{host / "write.txt"}"')
    holdout_read = run(f'cat "{holdout / "answer.txt"}"')
    secret = run('test -z "$UNRELATED_SECRET"', env={"PATH": "/usr/bin:/bin", "UNRELATED_SECRET": "must-not-leak"})
    result = ProbeResult(
        True,
        workspace_write,
        host_write.returncode != 0 and not (host / "write.txt").exists(),
        holdout_read.returncode != 0,
        secret.returncode == 0,
        False,
    )
    return ProbeResult(**{**result.__dict__, "ready_for_blind_holdout": result.workspace_write_allowed and result.host_write_denied and result.holdout_read_denied and result.ambient_secret_absent})


def write_probe_report(path: str | Path, result: ProbeResult) -> None:
    Path(path).write_text(json.dumps(result.document(), indent=2, sort_keys=True))
