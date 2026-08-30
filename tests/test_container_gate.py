import os

import pytest

from loopgraph_supervisor.container_gate import (
    PINNED_BUILDER_IMAGE,
    PINNED_NODE_IMAGE,
    ContainerGateResult,
    DockerBuilderSecurityGate,
    load_container_gate_receipt,
    write_container_gate_receipt,
)


def test_container_gate_requires_digest_pinned_image():
    with pytest.raises(ValueError, match="pinned by digest"):
        DockerBuilderSecurityGate("node:22-alpine")


def test_container_gate_receipt_binds_image_and_result(tmp_path):
    result = ContainerGateResult(PINNED_NODE_IMAGE, True, True, True, True, True, True, True, True, True, True, True)
    receipt = tmp_path / "receipt.json"
    write_container_gate_receipt(receipt, result)
    assert load_container_gate_receipt(receipt, PINNED_NODE_IMAGE).passed is True
    with pytest.raises(ValueError, match="authorize"):
        load_container_gate_receipt(receipt, "node@sha256:" + "0" * 64)


@pytest.mark.skipif(os.getenv("LOOPGRAPH_DOCKER_E2E") != "1", reason="set LOOPGRAPH_DOCKER_E2E=1 for live Docker isolation probe")
def test_live_builder_container_security_gate(tmp_path):
    result = DockerBuilderSecurityGate(PINNED_BUILDER_IMAGE).probe(tmp_path)
    assert result.passed is True
    assert result.holdout_absent is True
    assert result.sibling_absent is True
    assert result.network_denied is True
