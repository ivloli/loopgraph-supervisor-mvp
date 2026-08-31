import hashlib
import os

import pytest

from loopgraph_supervisor.container_gate import PINNED_NODE_IMAGE
from loopgraph_supervisor.egress_relay import RELAY_SCRIPT, DockerEgressRelay, EgressProbeResult, load_egress_receipt, write_egress_receipt


@pytest.mark.skipif(os.getenv("LOOPGRAPH_DOCKER_EGRESS_E2E") != "1", reason="requires Docker and network access")
def test_internal_builder_network_uses_fixed_deepseek_relay():
    with DockerEgressRelay() as relay:
        result = relay.probe()
    assert result.passed is True
    assert result.direct_network_denied is True
    assert result.disallowed_path_denied is True
    assert result.deepseek_path_reached_upstream is True
    assert result.bridge_peer_denied is True
    assert result.relay_non_root is True
    assert result.out_of_budget_request_denied is True


def test_egress_receipt_is_machine_readable(tmp_path):
    result = EgressProbeResult(PINNED_NODE_IMAGE, hashlib.sha256(RELAY_SCRIPT.encode()).hexdigest(), True, True, True, True, True, True, True, True, True)
    report = tmp_path / "egress.json"
    write_egress_receipt(str(report), result)
    assert '"passed": true' in report.read_text()
    assert load_egress_receipt(str(report)).passed is True


def test_relay_owns_authorization_instead_of_forwarding_builder_header():
    assert "/run/secrets/deepseek_api_key" in RELAY_SCRIPT
    assert "req.headers.authorization" not in RELAY_SCRIPT


@pytest.mark.skipif(os.getenv("LOOPGRAPH_DOCKER_EGRESS_E2E") != "1", reason="requires Docker and network access")
def test_relay_secret_is_not_exposed_in_docker_metadata():
    with DockerEgressRelay(api_key="loopgraph-test-secret-marker") as relay:
        result = relay.probe()
    assert result.credential_not_in_metadata is True
