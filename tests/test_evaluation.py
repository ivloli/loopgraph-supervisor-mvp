import pytest

from loopgraph_supervisor.evaluation import compare_specs, default_eval_tasks, evaluate_spec
from loopgraph_supervisor.loopspec import LoopSpec, default_coding_spec


def test_default_spec_passes_validation_and_canary_tasks():
    spec = default_coding_spec()

    validation = evaluate_spec(spec, default_eval_tasks(), "validation")
    canary = evaluate_spec(spec, default_eval_tasks(), "canary")

    assert validation.pass_rate == 1
    assert canary.pass_rate == 1
    assert len(validation.proof_hash()) == 64


def test_candidate_report_counts_regressions_against_baseline():
    baseline = default_coding_spec()
    candidate = LoopSpec(
        spec_id=baseline.spec_id,
        revision=2,
        predecessor_hash=baseline.content_hash(),
        entrypoint=baseline.entrypoint,
        max_iterations=baseline.max_iterations,
        nodes=baseline.nodes,
        edges=tuple(edge for edge in baseline.edges if edge.target != "execute"),
    )

    baseline_report, candidate_report = compare_specs(baseline, candidate, default_eval_tasks())

    assert baseline_report.pass_rate == 1
    assert candidate_report.pass_rate < 1
    assert candidate_report.regression_count == 1


def test_evaluation_requires_the_requested_split():
    with pytest.raises(ValueError, match="no LoopSpec evaluation tasks"):
        evaluate_spec(default_coding_spec(), default_eval_tasks(), "missing")  # type: ignore[arg-type]
