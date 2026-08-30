from __future__ import annotations

from typing import Any

from .domain import utc_now
from .graph_validator import validate_loopgraph
from .loopspec import LoopEdge, LoopNode, LoopSpec
from .store import SQLiteStore, decode, encode


class LoopSpecStore:
    """SQLite registry for immutable LoopSpec revisions."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS loop_specs (spec_id TEXT NOT NULL, revision INTEGER NOT NULL, content_hash TEXT NOT NULL, predecessor_hash TEXT, document TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(spec_id, revision))"
        )
        self.store.db.commit()

    def save(self, spec: LoopSpec, status: str, commit: bool = True, allow_human_activation: bool = False) -> None:
        validate_loopgraph(spec, require_coding_supervisor=spec.spec_id == "coding-supervisor")
        if status not in {"ACTIVE", "CANDIDATE", "SUPERSEDED", "REJECTED"}:
            raise ValueError(f"unsupported LoopSpec status: {status}")
        existing = self.store.db.execute("SELECT content_hash FROM loop_specs WHERE spec_id=? AND revision=?", (spec.spec_id, spec.revision)).fetchone()
        if existing is not None and existing["content_hash"] != spec.content_hash():
            raise ValueError("LoopSpec revisions are immutable")
        if status == "ACTIVE" and spec.revision > 1 and not allow_human_activation:
            raise ValueError("LoopSpec revisions above v1 require the proof-bound human activation path")
        if spec.revision == 1 and spec.predecessor_hash is not None:
            raise ValueError("LoopSpec revision 1 cannot have a predecessor")
        if spec.revision > 1:
            predecessor = self.store.db.execute("SELECT content_hash FROM loop_specs WHERE spec_id=? AND revision=?", (spec.spec_id, spec.revision - 1)).fetchone()
            if predecessor is None or predecessor["content_hash"] != spec.predecessor_hash:
                raise ValueError("LoopSpec predecessor binding does not match the prior revision")
        if status == "ACTIVE":
            self.store.db.execute("UPDATE loop_specs SET status='SUPERSEDED' WHERE spec_id=? AND status='ACTIVE'", (spec.spec_id,))
        self.store.db.execute(
            "INSERT INTO loop_specs VALUES(?,?,?,?,?,?,?) ON CONFLICT(spec_id,revision) DO UPDATE SET content_hash=excluded.content_hash, predecessor_hash=excluded.predecessor_hash, document=excluded.document, status=excluded.status",
            (spec.spec_id, spec.revision, spec.content_hash(), spec.predecessor_hash, encode(spec.document()), status, utc_now()),
        )
        if commit:
            self.store.db.commit()

    def active(self, spec_id: str) -> LoopSpec | None:
        row = self.store.db.execute("SELECT document FROM loop_specs WHERE spec_id=? AND status='ACTIVE' ORDER BY revision DESC LIMIT 1", (spec_id,)).fetchone()
        return None if row is None else from_document(decode(row["document"], {}))

    def revision(self, spec_id: str, revision: int) -> LoopSpec | None:
        row = self.store.db.execute("SELECT document FROM loop_specs WHERE spec_id=? AND revision=?", (spec_id, revision)).fetchone()
        return None if row is None else from_document(decode(row["document"], {}))



def from_document(document: dict[str, Any]) -> LoopSpec:
    if document.get("schema_version") != 1:
        raise ValueError("unsupported LoopSpec schema version")
    return LoopSpec(
        spec_id=document["spec_id"],
        revision=document["revision"],
        predecessor_hash=document.get("predecessor_hash"),
        entrypoint=document["entrypoint"],
        max_iterations=document["max_iterations"],
        nodes=tuple(LoopNode(item["id"], item["kind"], item.get("role")) for item in document["nodes"]),
        edges=tuple(LoopEdge(item["source"], item["target"], tuple(item.get("outcomes", []))) for item in document["edges"]),
    )
