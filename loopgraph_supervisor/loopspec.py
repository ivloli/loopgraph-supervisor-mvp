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
        if not self.spec_id or not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1 or not isinstance(self.max_iterations, int) or isinstance(self.max_iterations, bool) or self.max_iterations < 1:
            raise ValueError("LoopSpec requires a non-empty id, positive revision, and max_iterations")
        valid_kinds = {"dsh_execute", "verifier", "human_gate", "promotion", "terminal"}
        valid_outcomes = {"pass", "fail", "retry", "approve", "auto_promote", "reject", "exhausted"}
        if any(node.kind not in valid_kinds for node in self.nodes) or any(outcome not in valid_outcomes for edge in self.edges for outcome in edge.outcomes):
            raise ValueError("LoopSpec contains an unsupported node kind or outcome")
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
    if set(document) != {"schema_version", "spec_id", "revision", "predecessor_hash", "entrypoint", "max_iterations", "nodes", "edges"}:
        raise ValueError("LoopSpec artifact has an unexpected shape")
    if document.get("schema_version") != 1:
        raise ValueError("unsupported LoopSpec schema version")
    if any(set(item) - {"id", "kind", "role"} for item in document["nodes"]) or any(set(item) - {"source", "target", "outcomes"} for item in document["edges"]):
        raise ValueError("LoopSpec nodes or edges contain unknown fields")
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


def load_active_loopspec(path: str | Path) -> LoopSpec:
    manifest_path = Path(path).resolve()
    document = json.loads(manifest_path.read_text())
    if not isinstance(document, dict) or set(document) != {"schema_version", "spec_id", "active_revision", "artifact", "content_hash"} or document.get("schema_version") != 1:
        raise ValueError("active LoopSpec manifest has an unexpected shape")
    artifact_name = document["artifact"]
    if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name or not artifact_name.endswith(".json"):
        raise ValueError("active LoopSpec artifact must be a safe sibling JSON file")
    artifact_path = (manifest_path.parent / artifact_name).resolve()
    try:
        artifact_path.relative_to(manifest_path.parent)
    except ValueError as error:
        raise ValueError("active LoopSpec artifact escapes its manifest directory") from error
    spec = load_loopspec(artifact_path)
    if spec.spec_id != document["spec_id"] or spec.revision != document["active_revision"] or spec.content_hash() != document["content_hash"]:
        raise ValueError("active LoopSpec manifest does not bind its artifact")
    return spec


def coding_spec_revision(revision: int) -> LoopSpec:
    artifact = Path(__file__).resolve().parents[1] / "configs" / "loopspecs" / "coding-supervisor" / f"v{revision}.json"
    return load_loopspec(artifact)


def coding_spec_chain() -> tuple[LoopSpec, ...]:
    active = default_coding_spec()
    specs = tuple(coding_spec_revision(revision) for revision in range(1, active.revision + 1))
    for predecessor, candidate in zip(specs, specs[1:]):
        if candidate.predecessor_hash != predecessor.content_hash():
            raise ValueError("repository LoopSpec predecessor chain is invalid")
    return specs


def default_coding_spec() -> LoopSpec:
    manifest = Path(__file__).resolve().parents[1] / "configs" / "loopspecs" / "coding-supervisor" / "active.json"
    return load_active_loopspec(manifest)
