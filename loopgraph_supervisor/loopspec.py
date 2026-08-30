from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

NodeKind = Literal["dsh_execute", "verifier", "human_gate", "promotion", "terminal"]
Outcome = Literal["pass", "fail", "retry", "approve", "auto_promote", "reject", "exhausted"]


@dataclass(frozen=True)
class LoopNode:
    id: str
    kind: NodeKind
    role: str | None = None


@dataclass(frozen=True)
class LoopEdge:
    source: str
    target: str
    outcomes: tuple[Outcome, ...] = ()


@dataclass(frozen=True)
class LoopSpec:
    spec_id: str
    revision: int
    entrypoint: str
    nodes: tuple[LoopNode, ...]
    edges: tuple[LoopEdge, ...]
    max_iterations: int
    predecessor_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.spec_id or self.revision < 1 or self.max_iterations < 1:
            raise ValueError("LoopSpec requires a non-empty id, positive revision, and max_iterations")
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("LoopSpec node ids must be unique")
        if self.entrypoint not in set(node_ids):
            raise ValueError(f"LoopSpec entrypoint is not defined: {self.entrypoint}")
        for edge in self.edges:
            if edge.source not in set(node_ids) or edge.target not in set(node_ids):
                raise ValueError(f"LoopSpec edge references an unknown node: {edge.source}->{edge.target}")
            if len(edge.outcomes) != len(set(edge.outcomes)):
                raise ValueError(f"LoopSpec edge outcomes must be unique: {edge.source}->{edge.target}")
        routes = [(edge.source, outcome) for edge in self.edges for outcome in edge.outcomes]
        if len(routes) != len(set(routes)):
            raise ValueError("LoopSpec source/outcome routes must be unique")

    def next_node(self, source: str, outcome: Outcome) -> str:
        matches = [edge.target for edge in self.edges if edge.source == source and outcome in edge.outcomes]
        if len(matches) != 1:
            raise ValueError(f"LoopSpec has no unique edge for {source} on {outcome}")
        return matches[0]

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "spec_id": self.spec_id,
            "revision": self.revision,
            "predecessor_hash": self.predecessor_hash,
            "entrypoint": self.entrypoint,
            "max_iterations": self.max_iterations,
            "nodes": [{key: value for key, value in {"id": node.id, "kind": node.kind, "role": node.role}.items() if value is not None} for node in self.nodes],
            "edges": [{"source": edge.source, "target": edge.target, "outcomes": list(edge.outcomes)} for edge in self.edges],
        }

    def content_hash(self) -> str:
        encoded = json.dumps(self.document(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def load_loopspec(path: str | Path) -> LoopSpec:
    """Load one versioned LoopGraph artifact and validate it before use."""
    document = json.loads(Path(path).read_text())
    if not isinstance(document, dict):
        raise ValueError("LoopSpec artifact must contain one JSON object")
    spec = LoopSpec(
        spec_id=document["spec_id"],
        revision=document["revision"],
        predecessor_hash=document.get("predecessor_hash"),
        entrypoint=document["entrypoint"],
        max_iterations=document["max_iterations"],
        nodes=tuple(LoopNode(item["id"], item["kind"], item.get("role")) for item in document["nodes"]),
        edges=tuple(LoopEdge(item["source"], item["target"], tuple(item.get("outcomes", []))) for item in document["edges"]),
    )
    from .graph_validator import validate_loopgraph

    validate_loopgraph(spec, require_coding_supervisor=spec.spec_id == "coding-supervisor")
    return spec


def default_coding_spec() -> LoopSpec:
    artifact = Path(__file__).resolve().parents[1] / "configs" / "loopspecs" / "coding-supervisor" / "v1.json"
    return load_loopspec(artifact)
