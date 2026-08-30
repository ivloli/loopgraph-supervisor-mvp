import sys

from loopgraph_supervisor.sandbox import SubprocessSandbox


def test_subprocess_sandbox_returns_explicit_non_hostile_receipt(tmp_path):
    result = SubprocessSandbox().run([sys.executable, "-c", "print('ok')"], workspace=tmp_path)

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.receipt.isolation_level == "UNTRUSTED_UNSANDBOXED"
    assert result.receipt.holdout_mounted is False


def test_subprocess_sandbox_kills_timeout_process_group(tmp_path):
    result = SubprocessSandbox(timeout_seconds=1).run([sys.executable, "-c", "import time; time.sleep(10)"], workspace=tmp_path)

    assert result.timed_out is True
    assert result.exit_code is None
