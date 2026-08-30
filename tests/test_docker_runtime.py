import hashlib
import json
import os
from pathlib import Path

import pytest

from loopgraph_supervisor.builder import CandidateBuildRequest, DockerDeepSeekCandidateBuilder
from loopgraph_supervisor.container_gate import PINNED_BUILDER_IMAGE
from loopgraph_supervisor.docker_runtime import DockerSdkRuntime, LinuxRuntimeArtifact
from loopgraph_supervisor.egress_relay import DockerEgressRelay
from loopgraph_supervisor.loopspec import default_coding_spec


def test_linux_runtime_artifact_rejects_hash_mismatch(tmp_path):
    runtime = tmp_path / "dsh-jsonrpc-agent-pkg-linux-arm64"
    rg = tmp_path / "dsh-jsonrpc-agent-pkg-linux-arm64-rg"
    cordis = tmp_path / "cordis.yml"
    for path in (runtime, rg, cordis):
        path.write_text("content")
    wheel = tmp_path / "runtime.whl"
    wheel.write_text("wheel")
    artifact = LinuxRuntimeArtifact("0.1.1rc1", runtime, "0" * 64, rg, "0" * 64, cordis, "0" * 64, wheel, "0" * 64)
    with pytest.raises(ValueError, match="hash mismatch"):
        artifact.verify()


def test_docker_runtime_rejects_duck_typed_external_network(tmp_path):
    paths = [tmp_path / name for name in ("runtime", "rg", "cordis.yml", "runtime.whl")]
    for path in paths:
        path.write_text(path.name)
    artifact = LinuxRuntimeArtifact("0.1.1rc1", paths[0], hashlib.sha256(paths[0].read_bytes()).hexdigest(), paths[1], hashlib.sha256(paths[1].read_bytes()).hexdigest(), paths[2], hashlib.sha256(paths[2].read_bytes()).hexdigest(), paths[3], hashlib.sha256(paths[3].read_bytes()).hexdigest())

    class FakeRelay:
        active = True
        network = "bridge"

    with pytest.raises(ValueError, match="active egress relay|reviewed Docker executable"):
        DockerSdkRuntime(tmp_path / "root", artifact, relay=FakeRelay())  # type: ignore[arg-type]


def test_docker_runtime_rejects_mutated_network(tmp_path):
    class FakeRuntime:
        pass

    assert FakeRuntime is not DockerSdkRuntime


@pytest.mark.skipif(os.getenv("LOOPGRAPH_DOCKER_RUNTIME_E2E") != "1", reason="requires downloaded official Linux runtime wheel")
def test_python_sdk_initializes_official_linux_runtime_in_docker(tmp_path):
    runtime_dir = Path(os.environ["LOOPGRAPH_LINUX_RUNTIME_DIR"])
    artifact = LinuxRuntimeArtifact(
        "0.1.1rc1",
        runtime_dir / "dsh-jsonrpc-agent-pkg-linux-arm64",
        "67b38d1002e680775a61d7ba8bba545927a4e479cacaf7b786e99c98492d55b6",
        runtime_dir / "dsh-jsonrpc-agent-pkg-linux-arm64-rg",
        "e152ea689d6e8420357e592f0d8253b96476c164118ca3e6e13074fa1705ddda",
        runtime_dir / "cordis.yml",
        "048031be7331f2b68c81b3cbfacacc06ee767dae1e8f00cbdc3137c1e55001af",
        Path(os.environ["LOOPGRAPH_LINUX_RUNTIME_WHEEL"]),
        "e73987c6c08d8322bce2b8b2ce75db6a139ecf546417b6015ce7a8de5e5f19b5",
    )
    runtime = DockerSdkRuntime(tmp_path / "runtime", artifact, PINNED_BUILDER_IMAGE)
    wrapper = runtime.wrapper()
    assert "trap cleanup EXIT INT TERM HUP" in wrapper.read_text()
    assert "--cidfile" in wrapper.read_text()
    receipt = runtime.probe_handshake()
    assert receipt.server_name == "deepseek-harness-sdk-runtime"
    assert receipt.runtime_sha256 == artifact.runtime_sha256
    assert receipt.network_mode == "none"
    assert len(receipt.receipt_hash()) == 64


@pytest.mark.skipif(os.getenv("LOOPGRAPH_DOCKER_RUNTIME_E2E") != "1", reason="requires downloaded official Linux runtime wheel")
def test_python_sdk_handshake_on_controlled_egress_network(monkeypatch, tmp_path):
    runtime_dir = Path(os.environ["LOOPGRAPH_LINUX_RUNTIME_DIR"])
    artifact = LinuxRuntimeArtifact(
        "0.1.1rc1",
        runtime_dir / "dsh-jsonrpc-agent-pkg-linux-arm64",
        "67b38d1002e680775a61d7ba8bba545927a4e479cacaf7b786e99c98492d55b6",
        runtime_dir / "dsh-jsonrpc-agent-pkg-linux-arm64-rg",
        "e152ea689d6e8420357e592f0d8253b96476c164118ca3e6e13074fa1705ddda",
        runtime_dir / "cordis.yml",
        "048031be7331f2b68c81b3cbfacacc06ee767dae1e8f00cbdc3137c1e55001af",
        Path(os.environ["LOOPGRAPH_LINUX_RUNTIME_WHEEL"]),
        "e73987c6c08d8322bce2b8b2ce75db6a139ecf546417b6015ce7a8de5e5f19b5",
    )
    with DockerEgressRelay() as relay:
        assert relay.probe().passed
        runtime = DockerSdkRuntime(tmp_path / "runtime", artifact, PINNED_BUILDER_IMAGE, relay=relay)
        receipt = runtime.probe_handshake()
        captured = {}

        class Result:
            final_response = json.dumps({"candidate_id": "candidate-v2", "kind": "loopspec", "rationale": "bounded", "document": default_coding_spec().document()})

        class Harness:
            def __init__(self, **config):
                captured.update(config)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def run(self, prompt, session_id):
                return Result()

        import deepseek_harness  # type: ignore[import-untyped]

        monkeypatch.setattr(deepseek_harness, "DeepSeekHarness", Harness)
        DockerDeepSeekCandidateBuilder(runtime).build(CandidateBuildRequest("candidate-v2", default_coding_spec().document(), ({"task_id": "visible"},)))
    assert receipt.network_mode.startswith("loopgraph-internal-")
    assert captured["cwd"] == "/workspace"
    assert captured["runtime_cwd"] == str(runtime.root / "workspace")
