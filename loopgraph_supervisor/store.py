import json
import sqlite3
from typing import Any

from .domain import DecisionRecord, ImprovementProposal, Node, Version, Workflow, WorkflowStatus, utc_now


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def decode(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class SQLiteStore:
    """Durable facts and append-only explanation history for a workflow."""

    def __init__(self, path: str = "supervisor.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                current_node TEXT NOT NULL, attempt INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL, active_version TEXT NOT NULL,
                spec_id TEXT NOT NULL DEFAULT 'coding-supervisor', spec_revision INTEGER NOT NULL DEFAULT 1,
                spec_hash TEXT NOT NULL DEFAULT '',
                pause_requested INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_contracts (
                workflow_id TEXT PRIMARY KEY, contract TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL,
                type TEXT NOT NULL, from_node TEXT, to_node TEXT,
                payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, number INTEGER NOT NULL,
                execution_token TEXT UNIQUE NOT NULL, input TEXT NOT NULL,
                output TEXT NOT NULL, error TEXT NOT NULL, session_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_intents (
                token TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, request TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, passed INTEGER NOT NULL,
                feedback TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                decision_type TEXT NOT NULL, question TEXT NOT NULL, decision TEXT NOT NULL,
                rationale TEXT NOT NULL, evidence TEXT NOT NULL, alternatives TEXT NOT NULL,
                risk TEXT NOT NULL, expected_effect TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, based_on_attempt INTEGER NOT NULL,
                problem TEXT NOT NULL, hypothesis TEXT NOT NULL, changes TEXT NOT NULL,
                expected_evidence TEXT NOT NULL, risk_level TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hitl_requests (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, reason TEXT NOT NULL,
                context TEXT NOT NULL, decision TEXT, created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS versions (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, parent_id TEXT NOT NULL,
                artifact TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        self._ensure_workflow_columns()
        self.db.commit()

    def _ensure_workflow_columns(self) -> None:
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(workflows)")}
        for name, definition in (("spec_id", "TEXT NOT NULL DEFAULT 'coding-supervisor'"), ("spec_revision", "INTEGER NOT NULL DEFAULT 1"), ("spec_hash", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE workflows ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self.db.close()

    def create_workflow(self, workflow: Workflow) -> None:
        self.db.execute(
            "INSERT INTO workflows(id,goal,status,current_node,attempt,max_attempts,active_version,spec_id,spec_revision,spec_hash,pause_requested,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (workflow.id, workflow.goal, workflow.status.value, workflow.current_node.value,
             workflow.attempt, workflow.max_attempts, workflow.active_version, workflow.spec_id, workflow.spec_revision, workflow.spec_hash,
             int(workflow.pause_requested), workflow.created_at, workflow.updated_at),
        )
        self.db.commit()

    def get_workflow(self, workflow_id: str) -> Workflow:
        row = self.db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        contract = self.db.execute("SELECT contract FROM workflow_contracts WHERE workflow_id=?", (workflow_id,)).fetchone()
        return Workflow(id=row["id"], goal=row["goal"], status=WorkflowStatus(row["status"]), current_node=Node(row["current_node"]), attempt=row["attempt"], max_attempts=row["max_attempts"], active_version=row["active_version"], spec_id=row["spec_id"], spec_revision=row["spec_revision"], spec_hash=row["spec_hash"], pause_requested=bool(row["pause_requested"]), acceptance=decode(contract["contract"], {}) if contract else {}, created_at=row["created_at"], updated_at=row["updated_at"])

    def list_workflows(self) -> list[Workflow]:
        rows = self.db.execute("SELECT id FROM workflows ORDER BY updated_at DESC").fetchall()
        return [self.get_workflow(row["id"]) for row in rows]

    def save_contract(self, workflow_id: str, contract: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO workflow_contracts(workflow_id,contract) VALUES(?,?) ON CONFLICT(workflow_id) DO UPDATE SET contract=excluded.contract", (workflow_id, encode(contract)))
        self.db.commit()

    def save_workflow(self, workflow: Workflow) -> None:
        workflow.updated_at = utc_now()
        self.db.execute(
            "UPDATE workflows SET status=?, current_node=?, attempt=?, active_version=?, spec_id=?, spec_revision=?, spec_hash=?, pause_requested=?, updated_at=? WHERE id=?",
            (workflow.status.value, workflow.current_node.value, workflow.attempt, workflow.active_version, workflow.spec_id, workflow.spec_revision, workflow.spec_hash, int(workflow.pause_requested), workflow.updated_at, workflow.id),
        )
        self.db.commit()

    def append_event(self, workflow_id: str, event_type: str, from_node: str = "", to_node: str = "", payload: dict[str, Any] | None = None) -> None:
        self.db.execute("INSERT INTO events(workflow_id,type,from_node,to_node,payload,created_at) VALUES(?,?,?,?,?,?)", (workflow_id, event_type, from_node, to_node, encode(payload or {}), utc_now()))
        self.db.commit()

    def get_attempt(self, token: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM attempts WHERE execution_token=?", (token,)).fetchone()

    def save_attempt(self, workflow_id: str, number: int, token: str, request: dict[str, Any], output: dict[str, Any], session_id: str, error: str = "") -> None:
        self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET output=excluded.output,error=excluded.error", (token, workflow_id, number, token, encode(request), encode(output), error, session_id, utc_now()))
        self.db.commit()

    def get_open_execution(self, workflow_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM execution_intents WHERE workflow_id=? AND status IN ('STARTED','RETRY_APPROVED') ORDER BY attempt DESC LIMIT 1", (workflow_id,)).fetchone()

    def start_execution(self, workflow_id: str, attempt: int, token: str, request: dict[str, Any]) -> None:
        timestamp = utc_now()
        self.db.execute("INSERT INTO execution_intents VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(token) DO NOTHING", (token, workflow_id, attempt, encode(request), "STARTED", "", timestamp, timestamp))
        self.db.commit()

    def finish_execution(self, token: str, status: str, error: str = "") -> None:
        self.db.execute("UPDATE execution_intents SET status=?, error=?, updated_at=? WHERE token=?", (status, error, utc_now(), token))
        self.db.commit()

    def set_execution_status(self, token: str, status: str) -> None:
        self.db.execute("UPDATE execution_intents SET status=?, updated_at=? WHERE token=?", (status, utc_now(), token))
        self.db.commit()

    def save_verification(self, workflow_id: str, attempt: int, passed: bool, feedback: str, evidence: list[dict[str, Any]]) -> None:
        self.db.execute("INSERT INTO verifications(workflow_id,attempt,passed,feedback,evidence,created_at) VALUES(?,?,?,?,?,?)", (workflow_id, attempt, int(passed), feedback, encode(evidence), utc_now()))
        self.db.commit()

    def latest_verification(self, workflow_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM verifications WHERE workflow_id=? ORDER BY id DESC LIMIT 1", (workflow_id,)).fetchone()

    def latest_proposal(self, workflow_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM proposals WHERE workflow_id=? ORDER BY created_at DESC LIMIT 1", (workflow_id,)).fetchone()

    def save_decision(self, decision: DecisionRecord) -> None:
        self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (decision.id, decision.workflow_id, decision.attempt, decision.decision_type, decision.question, decision.decision, encode(decision.rationale), encode(decision.evidence), encode(decision.alternatives), decision.risk, decision.expected_effect, decision.created_by, decision.created_at))
        self.db.commit()

    def save_proposal(self, proposal: ImprovementProposal) -> None:
        self.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?)", (proposal.id, proposal.workflow_id, proposal.based_on_attempt, proposal.problem, proposal.hypothesis, encode(proposal.changes), encode(proposal.expected_evidence), proposal.risk_level, proposal.status, proposal.created_at))
        self.db.commit()

    def save_hitl(self, request_id: str, workflow_id: str, reason: str, context: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO hitl_requests VALUES(?,?,?,?,?,?,?)", (request_id, workflow_id, reason, encode(context), None, utc_now(), None))
        self.db.commit()

    def open_hitl(self, workflow_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM hitl_requests WHERE workflow_id=? AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1", (workflow_id,)).fetchone()

    def resolve_hitl(self, request_id: str, decision: str) -> None:
        self.db.execute("UPDATE hitl_requests SET decision=?, resolved_at=? WHERE id=? AND resolved_at IS NULL", (decision, utc_now(), request_id))
        self.db.commit()

    def save_version(self, version: Version) -> None:
        self.db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, artifact=excluded.artifact, status=excluded.status", (version.id, version.workflow_id, version.parent_id, encode(version.artifact), version.status, version.created_at))
        self.db.commit()

    def has_version(self, workflow_id: str, version_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM versions WHERE workflow_id=? AND id=?", (workflow_id, version_id)).fetchone() is not None

    def get_version(self, workflow_id: str, version_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM versions WHERE workflow_id=? AND id=?", (workflow_id, version_id)).fetchone()

    def explain(self, workflow_id: str) -> dict[str, list[dict[str, Any]]]:
        decisions = [dict(row) for row in self.db.execute("SELECT * FROM decisions WHERE workflow_id=? ORDER BY created_at", (workflow_id,))]
        for item in decisions:
            for key in ("rationale", "evidence", "alternatives"):
                item[key] = decode(item[key], [])
        proposals = [dict(row) for row in self.db.execute("SELECT * FROM proposals WHERE workflow_id=? ORDER BY created_at", (workflow_id,))]
        for item in proposals:
            for key in ("changes", "expected_evidence"):
                item[key] = decode(item[key], [])
        events = [dict(row) for row in self.db.execute("SELECT * FROM events WHERE workflow_id=? ORDER BY id", (workflow_id,))]
        for item in events:
            item["payload"] = decode(item["payload"], {})
        versions = [dict(row) for row in self.db.execute("SELECT * FROM versions WHERE workflow_id=? ORDER BY created_at", (workflow_id,))]
        for item in versions:
            item["artifact"] = decode(item["artifact"], {})
        verifications = [dict(row) for row in self.db.execute("SELECT * FROM verifications WHERE workflow_id=? ORDER BY id", (workflow_id,))]
        for item in verifications:
            item["passed"] = bool(item["passed"])
            item["evidence"] = decode(item["evidence"], [])
        hitl = [dict(row) for row in self.db.execute("SELECT * FROM hitl_requests WHERE workflow_id=? ORDER BY created_at", (workflow_id,))]
        for item in hitl:
            item["context"] = decode(item["context"], {})
        attempts = [dict(row) for row in self.db.execute("SELECT id,number,execution_token,error,session_id,created_at FROM attempts WHERE workflow_id=? ORDER BY number", (workflow_id,))]
        return {"decisions": decisions, "proposals": proposals, "events": events, "versions": versions, "verifications": verifications, "hitl": hitl, "attempts": attempts}
