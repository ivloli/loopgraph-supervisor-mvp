import json

from loopgraph_supervisor.quality_gate import run_quality_gates


def test_python_test_gate_records_structured_evidence(tmp_path):
    passed, evidence, feedback = run_quality_gates(tmp_path, {"python_test_gate": {"command": ["python", "-c", "print('ok')"]}})

    assert passed is True
    assert feedback == []
    assert evidence[0]["type"] == "python_test"
    assert evidence[0]["stdout_hash"]


def test_coverage_gate_enforces_minimum_and_baseline(tmp_path):
    report = tmp_path / ".coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 82.0, "covered_branches": 8, "num_branches": 10}}))
    report_command = ["python", "-c", "import json;json.dump({'totals':{'percent_covered':82.0,'covered_branches':8,'num_branches':10}},open('.coverage.json','w'))"]
    passed, evidence, feedback = run_quality_gates(tmp_path, {"coverage_gate": {"command": ["python", "-c", "print('run')"], "report_command": report_command, "minimum_percent": 80, "baseline_percent": 85, "max_regression_percent": 0}})

    assert passed is False
    assert evidence[0]["line_percent"] == 82.0
    assert evidence[0]["branch_percent"] == 80.0
    assert feedback == ["Coverage Gate failed"]


def test_coverage_gate_rejects_report_path_escape(tmp_path):
    try:
        run_quality_gates(tmp_path, {"coverage_gate": {"report": "../outside.json"}})
    except ValueError as error:
        assert "safe path" in str(error)
    else:
        raise AssertionError("coverage report path escape must fail closed")
