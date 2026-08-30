from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxReceipt:
    backend: str
    isolation_level: str
    workspace: str
    timeout_enforced: bool
    process_group_isolated: bool
    network_isolated: bool
    holdout_mounted: bool
    passed: bool


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    receipt: SandboxReceipt


class SubprocessSandbox:
    """Lightweight MVP runner; not a hostile-code security boundary."""

    def __init__(self, *, timeout_seconds: int = 30, output_limit: int = 64 * 1024):
        if timeout_seconds < 1 or output_limit < 1:
            raise ValueError("sandbox limits must be positive")
        self.timeout_seconds = timeout_seconds
        self.output_limit = output_limit

    def run(self, command: list[str], *, workspace: str | Path, environment: dict[str, str] | None = None) -> SandboxResult:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("sandbox command must be a non-empty argv list")
        root = Path(workspace).resolve()
        if not root.is_dir() or root.is_symlink():
            raise ValueError("sandbox workspace must be an existing non-symlink directory")
        receipt = SandboxReceipt("subprocess", "UNTRUSTED_UNSANDBOXED", str(root), True, os.name == "posix", False, False, False)
        process = subprocess.Popen(command, cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=os.name == "posix")
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            return SandboxResult(process.returncode, stdout[: self.output_limit], stderr[: self.output_limit], False, receipt)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            stdout, stderr = process.communicate()
            return SandboxResult(None, stdout[: self.output_limit], stderr[: self.output_limit], True, receipt)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
