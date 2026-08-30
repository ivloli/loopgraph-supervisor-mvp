from __future__ import annotations

from collections import defaultdict, deque

from .loopspec import LoopSpec, NodeKind, Outcome

REQUIRED_OUTCOMES: dict[NodeKind, frozenset[Outcome]] = {
    "dsh_execute": frozenset({"pass"}),
    "verifier": frozenset(),
    "human_gate": frozenset({"approve", "retry", "reject"}),
    "promotion": frozenset({"pass"}),
    "terminal": frozenset(),
}


def validate_loopgraph(spec: LoopSpec, *, require_coding_supervisor: bool = False) -> None:
    """Validate that a LoopSpec is structurally complete and executable."""
    nodes = {node.id: node for node in spec.nodes}
    outgoing: dict[str, set[str]] = defaultdict(set)
    outcomes: dict[str, set[Outcome]] = defaultdict(set)
    for edge in spec.edges:
        outgoing[edge.source].add(edge.target)
        outcomes[edge.source].update(edge.outcomes)

    reachable = {spec.entrypoint}
    queue = deque([spec.entrypoint])
    while queue:
        source = queue.popleft()
        for target in outgoing[source]:
            if target not in reachable:
                reachable.add(target)
                queue.append(target)
    unreachable = sorted(set(nodes) - reachable)
    if unreachable:
        raise ValueError(f"LoopSpec contains unreachable nodes: {unreachable}")

    terminals = {node.id for node in spec.nodes if node.kind == "terminal"}
    if not terminals:
        raise ValueError("LoopSpec requires at least one terminal node")
    for node in spec.nodes:
        if node.kind == "terminal" and outgoing[node.id]:
            raise ValueError(f"terminal node has outgoing edges: {node.id}")
        if node.kind != "terminal" and not outgoing[node.id]:
            raise ValueError(f"non-terminal node has no outgoing edge: {node.id}")
        missing = REQUIRED_OUTCOMES[node.kind] - outcomes[node.id]
        if missing:
            raise ValueError(f"node {node.id} is missing required outcomes: {sorted(missing)}")

    can_reach_terminal = set(terminals)
    changed = True
    while changed:
        changed = False
        for source, targets in outgoing.items():
            if source not in can_reach_terminal and targets & can_reach_terminal:
                can_reach_terminal.add(source)
                changed = True
    stranded = sorted(set(nodes) - can_reach_terminal)
    if stranded:
        raise ValueError(f"LoopSpec contains nodes with no terminating path: {stranded}")

    if require_coding_supervisor:
        kinds = {node.kind for node in spec.nodes}
        required = {"dsh_execute", "verifier", "human_gate", "promotion", "terminal"}
        if missing_kinds := sorted(required - kinds):
            raise ValueError(f"coding-supervisor graph is missing required node kinds: {missing_kinds}")
