import pytest

from loopgraph_supervisor.graph_validator import validate_loopgraph
from loopgraph_supervisor.loopspec import LoopEdge, LoopNode, LoopSpec, default_coding_spec


def test_valid_coding_graph_passes_complete_graph_gate():
    validate_loopgraph(default_coding_spec(), require_coding_supervisor=True)


def test_unreachable_node_is_rejected():
    baseline = default_coding_spec()
    spec = LoopSpec(baseline.spec_id, 1, baseline.entrypoint, baseline.nodes + (LoopNode("orphan", "terminal"),), baseline.edges, baseline.max_iterations)

    with pytest.raises(ValueError, match="unreachable"):
        validate_loopgraph(spec)


def test_terminal_exit_is_rejected():
    baseline = default_coding_spec()
    edges = baseline.edges + (LoopEdge("complete", "failed", ("pass",)),)
    with pytest.raises(ValueError, match="terminal node"):
        validate_loopgraph(LoopSpec(baseline.spec_id, 1, baseline.entrypoint, baseline.nodes, edges, baseline.max_iterations))


def test_graph_without_terminal_is_rejected():
    baseline = default_coding_spec()
    nodes = (LoopNode("start", "dsh_execute"), LoopNode("loop", "verifier"))
    edges = (LoopEdge("start", "loop", ("pass",)), LoopEdge("loop", "start", ("retry",)))
    with pytest.raises(ValueError, match="terminal"):
        validate_loopgraph(LoopSpec("custom", 1, "start", nodes, edges, baseline.max_iterations))


def test_coding_profile_requires_governance_nodes():
    baseline = default_coding_spec()
    nodes = tuple(node for node in baseline.nodes if node.id in {"execute", "verify", "promote", "complete"})
    edges = (
        LoopEdge("execute", "verify", ("pass",)),
        LoopEdge("verify", "promote", ("auto_promote",)),
        LoopEdge("promote", "complete", ("pass",)),
    )
    spec = LoopSpec("coding-supervisor", 1, "execute", nodes, edges, 3)
    with pytest.raises(ValueError, match="required node kinds"):
        validate_loopgraph(spec, require_coding_supervisor=True)
