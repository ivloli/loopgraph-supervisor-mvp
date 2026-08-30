import json

import pytest

from loopgraph_supervisor.builder import (
    CandidateBuildRequest,
    CandidateBuildResult,
    DeepSeekCandidateBuilder,
    DockerDeepSeekCandidateBuilder,
    builder_prompt,
    parse_builder_response,
)
from loopgraph_supervisor.builder_workspace import prepare_builder_workspace
from loopgraph_supervisor.candidate_store import CandidateStore
from loopgraph_supervisor.evaluation import default_eval_tasks
from loopgraph_supervisor.evolution import LoopSpecEvolutionService
from loopgraph_supervisor.loopspec import LoopSpec, default_coding_spec
from loopgraph_supervisor.spec_store import LoopSpecStore
from loopgraph_supervisor.store import SQLiteStore


class ValidationOnlyHoldout:
    def validation_tasks(self, target_id):
        return tuple(task for task in default_eval_tasks() if task.split == "validation")

    def canary_tasks(self, target_id):
        return tuple(task for task in default_eval_tasks() if task.split == "canary")


class FakeCandidateBuilder:
    def __init__(self, document):
        self.document = document
        self.seen = None

    def build(self, request):
        self.seen = request
        return CandidateBuildResult(request.candidate_id, "loopspec", "Equivalent v2", self.document, "fake-builder")


def test_builder_workspace_contains_validation_but_no_canary(tmp_path):
    active = default_coding_spec()
    workspace, request = prepare_builder_workspace(tmp_path, "candidate-v2", active, default_eval_tasks())

    visible = (workspace / "validation-context.json").read_text()
    assert "success-to-human" in visible
    assert "private-canary" not in visible
    assert "exhaustion-to-human" not in visible
    assert all("canary" not in json.dumps(item).lower() for item in request.validation_context)


def test_builder_response_requires_exact_json_shape():
    with pytest.raises(ValueError, match="JSON object"):
        parse_builder_response("```json\n{}\n```", "candidate-v2", "loopspec")
    with pytest.raises(ValueError, match="unexpected shape"):
        parse_builder_response('{"candidate_id":"candidate-v2"}', "candidate-v2", "loopspec")


def test_host_process_builder_is_permanently_disabled():
    with pytest.raises(ValueError, match="permanently disabled"):
        DeepSeekCandidateBuilder()


def test_builder_response_rejects_duplicate_keys_and_non_finite_numbers():
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_builder_response('{"candidate_id":"candidate-v2","candidate_id":"other","kind":"loopspec","rationale":"x","document":{}}', "candidate-v2", "loopspec")
    with pytest.raises(ValueError, match="non-finite"):
        parse_builder_response('{"candidate_id":"candidate-v2","kind":"loopspec","rationale":"x","document":{"revision":NaN}}', "candidate-v2", "loopspec")


def test_builder_prompt_rejects_holdout_metadata():
    with pytest.raises(ValueError, match="forbidden holdout"):
        builder_prompt(CandidateBuildRequest("candidate", default_coding_spec().document(), ({"task_id": "private-canary"},)))


def test_docker_builder_rejects_untrusted_runtime_launcher(tmp_path):
    class Runtime:
        root = tmp_path

        def wrapper(self):
            path = tmp_path / "runtime-wrapper"
            path.write_text("#!/bin/sh\n")
            return path

    with pytest.raises(ValueError, match="receipt-gated"):
        DockerDeepSeekCandidateBuilder(Runtime())


def test_builder_output_is_host_parsed_and_enters_quarantine():
    store = SQLiteStore(":memory:")
    specs = LoopSpecStore(store)
    active = default_coding_spec()
    specs.save(active, status="ACTIVE")
    candidate = LoopSpec(active.spec_id, 2, active.entrypoint, active.nodes, active.edges, active.max_iterations, active.content_hash())
    builder = FakeCandidateBuilder(candidate.document())
    service = LoopSpecEvolutionService(specs, CandidateStore(store), ValidationOnlyHoldout())
    frozen = service.propose(builder, "candidate-v2")

    assert frozen.manifest.status == "QUARANTINED"
    assert builder.seen is not None
    assert all("canary" not in json.dumps(item).lower() for item in builder.seen.validation_context)
    assert {item["task_id"] for item in builder.seen.validation_context} == {"success-to-human", "failure-to-retry"}
    current = service.specs.active(active.spec_id)
    assert current is not None
    assert current.revision == 1
