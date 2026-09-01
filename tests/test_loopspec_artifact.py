import json

import pytest

from loopgraph_supervisor.loopspec import coding_spec_revision, default_coding_spec, load_active_loopspec, load_loopspec
from loopgraph_supervisor.supervisor import Supervisor


def test_default_loopgraph_is_loaded_from_versioned_artifact():
    spec = default_coding_spec()

    assert spec.revision == 2
    assert spec.next_node("verify", "retry") == "execute"
    assert spec.next_node("verify", "exhausted") == "hitl"
    assert spec.next_node("execute", "fail") == "hitl"


def test_invalid_external_loopgraph_fails_closed(tmp_path):
    artifact = tmp_path / "invalid.json"
    document = default_coding_spec().document()
    document["entrypoint"] = "missing"
    artifact.write_text(json.dumps(document))

    with pytest.raises(ValueError, match="entrypoint"):
        load_loopspec(artifact)


def test_future_schema_and_unknown_fields_fail_closed(tmp_path):
    document = default_coding_spec().document()
    document["schema_version"] = 2
    artifact = tmp_path / "future.json"
    artifact.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="schema version"):
        load_loopspec(artifact)

    document["schema_version"] = 1
    document["unknown"] = True
    artifact.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="unexpected shape"):
        load_loopspec(artifact)


def test_runtime_role_resolution_does_not_depend_on_node_id():
    spec = default_coding_spec()
    renamed = type(spec)(spec.spec_id, spec.revision, spec.entrypoint, tuple(type(node)("quality-check" if node.role == "verify" else node.id, node.kind, node.role) for node in spec.nodes), tuple(type(edge)("quality-check" if edge.source == "verify" else edge.source, "quality-check" if edge.target == "verify" else edge.target, edge.outcomes) for edge in spec.edges), spec.max_iterations, spec.predecessor_hash)

    assert Supervisor._spec_node_for_role(renamed, "verify") == "quality-check"


def test_shared_transition_vectors_match_python_interpreter():
    from loopgraph_supervisor.loopspec_interpreter import LoopSpecInterpreter

    spec = coding_spec_revision(1)
    vectors = json.loads((__import__("pathlib").Path(__file__).parents[1] / "configs/loopspecs/coding-supervisor/test-vectors.json").read_text())
    for vector in vectors:
        source = Supervisor._spec_node_for_role(spec, vector["source_role"])
        expected = Supervisor._spec_node_for_role(spec, vector["expected_role"])
        assert LoopSpecInterpreter(spec).transition(source, vector["outcome"], vector["iteration"]).target == expected


def test_shared_v1_content_hash_matches_published_vector():
    expected = (__import__("pathlib").Path(__file__).parents[1] / "configs/loopspecs/coding-supervisor/v1.sha256").read_text().strip()
    assert coding_spec_revision(1).content_hash() == expected


def test_all_committed_revisions_form_a_valid_chain_and_active_manifest():
    root = __import__("pathlib").Path(__file__).parents[1] / "configs/loopspecs/coding-supervisor"
    revisions = sorted((path for path in root.glob("v*.json") if path.stem[1:].isdigit()), key=lambda path: int(path.stem[1:]))
    specs = [load_loopspec(path) for path in revisions]
    assert [spec.revision for spec in specs] == list(range(1, len(specs) + 1))
    for previous, candidate in zip(specs, specs[1:]):
        assert candidate.predecessor_hash == previous.content_hash()
    active = load_active_loopspec(root / "active.json")
    assert active.document() == specs[-1].document()
    assert active.content_hash() == "5b5ebe301a5533deb33f3bba0d3cb87ae8f56996b913f7e56f6007de7edb5c8d"
