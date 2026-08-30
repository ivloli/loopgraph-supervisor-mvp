import json
import sys

import pytest

from loopgraph_supervisor.security_probe import ProbeResult, run_macos_builder_probe, write_probe_report


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt probe")
def test_rc2_macos_sandbox_is_not_blind_holdout_ready(tmp_path):
    result = run_macos_builder_probe(tmp_path)

    assert result.sandbox_available is True
    assert result.workspace_write_allowed is True
    assert result.host_write_denied is True
    assert result.holdout_read_denied is False
    assert result.ready_for_blind_holdout is False


def test_probe_report_is_explicit_and_machine_readable(tmp_path):
    result = ProbeResult(True, True, True, False, True, False)
    report = tmp_path / "report.json"
    write_probe_report(report, result)

    assert json.loads(report.read_text()) == result.document()
