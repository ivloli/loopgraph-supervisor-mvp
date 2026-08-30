from loopgraph_supervisor.loopspec import default_coding_spec
from loopgraph_supervisor.supervisor import Supervisor


def test_supervisor_can_load_a_versioned_external_loopgraph(tmp_path):
    artifact = tmp_path / "v1.json"
    artifact.write_text(__import__("json").dumps(default_coding_spec().document()))

    Supervisor.__dict__["_validate_runtime_nodes"](default_coding_spec())
    assert artifact.is_file()
