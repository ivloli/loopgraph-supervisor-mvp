import json

import pytest

from loopgraph_supervisor.loopspec import default_coding_spec, load_loopspec
from loopgraph_supervisor.supervisor import Supervisor


def test_default_loopgraph_is_loaded_from_versioned_artifact():
    spec = default_coding_spec()

    assert spec.revision == 1
    assert spec.next_node("verify", "retry") == "execute"
    assert spec.next_node("verify", "exhausted") == "hitl"


def test_invalid_external_loopgraph_fails_closed(tmp_path):
    artifact = tmp_path / "invalid.json"
    document = default_coding_spec().document()
    document["entrypoint"] = "missing"
    artifact.write_text(json.dumps(document))

    with pytest.raises(ValueError, match="entrypoint"):
        load_loopspec(artifact)


def test_runtime_role_resolution_does_not_depend_on_node_id():
    spec = default_coding_spec()
    renamed = type(spec)(spec.spec_id, spec.revision, spec.entrypoint, tuple(type(node)("quality-check" if node.role == "verify" else node.id, node.kind, node.role) for node in spec.nodes), tuple(type(edge)("quality-check" if edge.source == "verify" else edge.source, "quality-check" if edge.target == "verify" else edge.target, edge.outcomes) for edge in spec.edges), spec.max_iterations, spec.predecessor_hash)

    assert Supervisor._spec_node_for_role(renamed, "verify") == "quality-check"
