from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .store import SQLiteStore, decode, encode


@dataclass(frozen=True)
class TaskFeedback:
    """Durable task evidence that may justify an evolution proposal."""

    workflow_id: str
    attempt: int
    passed: bool
    feedback: str


@dataclass(frozen=True)
class HumanFeedback:
    """Explicit operator request to review or improve the Supervisor."""

    reviewer: str
    comment: str

    def __post_init__(self) -> None:
        if not self.reviewer.strip() or not self.comment.strip():
            raise ValueError("human feedback requires reviewer and comment")


@dataclass(frozen=True)
class EvolutionTrigger:
    """Host-owned trigger derived from executed task feedback, never spontaneous."""

    target_id: str
    reason: str
    evidence: tuple[TaskFeedback | HumanFeedback, ...]
    source: str = "task_feedback"

    def __post_init__(self) -> None:
        if not self.target_id or not self.reason.strip() or not self.evidence or self.source not in {"task_feedback", "human_feedback"}:
            raise ValueError("evolution trigger requires target, reason, and task evidence")


def trigger_from_feedback(target_id: str, feedback: tuple[TaskFeedback, ...], *, minimum_failures: int = 1) -> EvolutionTrigger | None:
    """Create an evolution trigger only when observed task failures cross a host threshold."""
    failures = tuple(item for item in feedback if not item.passed)
    if len(failures) < minimum_failures:
        return None
    return EvolutionTrigger(target_id, f"{len(failures)} observed task failure(s) require a bounded policy review", failures)


def trigger_from_human(target_id: str, reviewer: str, comment: str) -> EvolutionTrigger:
    """Create an immediate RSI request from an explicit human instruction."""
    feedback = HumanFeedback(reviewer, comment)
    return EvolutionTrigger(target_id, "human explicitly requested a bounded policy review", (feedback,), "human_feedback")


class EvolutionTriggerStore:
    """Durable trigger inbox shared by task feedback and human requests."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        self.store.db.execute("CREATE TABLE IF NOT EXISTS evolution_triggers (id TEXT PRIMARY KEY, target_id TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING')")
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(evolution_triggers)")}
        if "status" not in columns:
            self.store.db.execute("ALTER TABLE evolution_triggers ADD COLUMN status TEXT NOT NULL DEFAULT 'PENDING'")
        self.store.db.commit()

    def save(self, trigger_id: str, trigger: EvolutionTrigger) -> None:
        if not trigger_id.strip():
            raise ValueError("evolution trigger requires an id")
        evidence = []
        for item in trigger.evidence:
            evidence.append({"type": "task", **item.__dict__} if isinstance(item, TaskFeedback) else {"type": "human", **item.__dict__})
        self.store.db.execute("INSERT INTO evolution_triggers VALUES(?,?,?,?,?,?)", (trigger_id, trigger.target_id, trigger.source, trigger.reason, encode(evidence), "PENDING"))
        self.store.db.commit()

    def get(self, trigger_id: str) -> EvolutionTrigger | None:
        row = self.store.db.execute("SELECT * FROM evolution_triggers WHERE id=?", (trigger_id,)).fetchone()
        if row is None:
            return None
        evidence = []
        for item in decode(row["evidence"], []):
            evidence.append(TaskFeedback(item["workflow_id"], item["attempt"], item["passed"], item["feedback"]) if item["type"] == "task" else HumanFeedback(item["reviewer"], item["comment"]))
        return EvolutionTrigger(row["target_id"], row["reason"], tuple(evidence), row["source"])

    def status(self, trigger_id: str) -> str:
        row = self.store.db.execute("SELECT status FROM evolution_triggers WHERE id=?", (trigger_id,)).fetchone()
        if row is None:
            raise KeyError(trigger_id)
        return str(row["status"])

    def claim(self, trigger_id: str) -> EvolutionTrigger:
        trigger = self.get(trigger_id)
        if trigger is None:
            raise KeyError(trigger_id)
        cursor = self.store.db.execute("UPDATE evolution_triggers SET status='PROCESSING' WHERE id=? AND status='PENDING'", (trigger_id,))
        if cursor.rowcount != 1:
            raise ValueError(f"evolution trigger is not pending: {trigger_id}")
        self.store.db.commit()
        return trigger

    def finish(self, trigger_id: str, status: str) -> None:
        if status not in {"CONSUMED", "FAILED"}:
            raise ValueError("unsupported evolution trigger terminal status")
        cursor = self.store.db.execute("UPDATE evolution_triggers SET status=? WHERE id=? AND status='PROCESSING'", (status, trigger_id))
        if cursor.rowcount != 1:
            raise ValueError(f"evolution trigger is not processing: {trigger_id}")
        self.store.db.commit()

    def requeue(self, trigger_id: str) -> None:
        cursor = self.store.db.execute("UPDATE evolution_triggers SET status='PENDING' WHERE id=? AND status='PROCESSING'", (trigger_id,))
        if cursor.rowcount != 1:
            raise ValueError(f"evolution trigger is not processing: {trigger_id}")
        self.store.db.commit()

    def create_human_request(self, target_id: str, reviewer: str, comment: str) -> tuple[str, EvolutionTrigger]:
        trigger_id = f"human:{uuid4()}"
        trigger = trigger_from_human(target_id, reviewer, comment)
        self.save(trigger_id, trigger)
        return trigger_id, trigger
