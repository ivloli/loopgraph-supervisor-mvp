from __future__ import annotations

from dataclasses import dataclass

from .store import SQLiteStore


@dataclass(frozen=True)
class EvolutionRun:
    run_id: str
    trigger_id: str
    target_id: str
    baseline_spec_revision: int
    baseline_spec_hash: str
    candidate_id: str | None = None
    candidate_hash: str | None = None
    proof_hash: str | None = None
    status: str = "REQUESTED"
    pr_number: int | None = None
    pr_head: str | None = None
    merge_commit: str | None = None
    active_version: str | None = None


class EvolutionRunStore:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.store.db.execute("CREATE TABLE IF NOT EXISTS evolution_runs (run_id TEXT PRIMARY KEY, trigger_id TEXT NOT NULL UNIQUE, target_id TEXT NOT NULL, baseline_spec_revision INTEGER NOT NULL, baseline_spec_hash TEXT NOT NULL, candidate_id TEXT, candidate_hash TEXT, proof_hash TEXT, status TEXT NOT NULL, pr_number INTEGER, pr_head TEXT, merge_commit TEXT, active_version TEXT)")
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(evolution_runs)")}
        for name, definition in (("pr_number", "INTEGER"), ("pr_head", "TEXT"), ("merge_commit", "TEXT"), ("active_version", "TEXT")):
            if name not in columns:
                self.store.db.execute(f"ALTER TABLE evolution_runs ADD COLUMN {name} {definition}")
        self.store.db.commit()

    def save(self, run: EvolutionRun) -> None:
        self.store.db.execute("INSERT INTO evolution_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET candidate_id=excluded.candidate_id,candidate_hash=excluded.candidate_hash,proof_hash=excluded.proof_hash,status=excluded.status,pr_number=excluded.pr_number,pr_head=excluded.pr_head,merge_commit=excluded.merge_commit,active_version=excluded.active_version", (run.run_id, run.trigger_id, run.target_id, run.baseline_spec_revision, run.baseline_spec_hash, run.candidate_id, run.candidate_hash, run.proof_hash, run.status, run.pr_number, run.pr_head, run.merge_commit, run.active_version))
        self.store.db.commit()

    def get(self, run_id: str) -> EvolutionRun | None:
        row = self.store.db.execute("SELECT * FROM evolution_runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else EvolutionRun(**dict(row))

    def by_trigger(self, trigger_id: str) -> EvolutionRun | None:
        row = self.store.db.execute("SELECT * FROM evolution_runs WHERE trigger_id=?", (trigger_id,)).fetchone()
        return None if row is None else EvolutionRun(**dict(row))

    def attach_pr(self, run_id: str, pr_number: int, pr_head: str) -> None:
        cursor = self.store.db.execute("UPDATE evolution_runs SET pr_number=?,pr_head=?,status='PR_OPEN' WHERE run_id=? AND status='PROMOTION_REVIEW'", (pr_number, pr_head, run_id))
        if cursor.rowcount != 1:
            raise ValueError("evolution run is not ready for PR attachment")
        self.store.db.commit()

    def record_merge(self, run_id: str, merge_commit: str) -> None:
        cursor = self.store.db.execute("UPDATE evolution_runs SET merge_commit=?,status='MERGED' WHERE run_id=? AND status='PR_OPEN'", (merge_commit, run_id))
        if cursor.rowcount != 1:
            raise ValueError("evolution run is not open for merge recording")
        self.store.db.commit()

    def record_activation(self, run_id: str, active_version: str, passed: bool) -> None:
        status = "ACTIVE" if passed else "ROLLED_BACK"
        cursor = self.store.db.execute("UPDATE evolution_runs SET active_version=?,status=? WHERE run_id=? AND status='MERGED'", (active_version, status, run_id))
        if cursor.rowcount != 1:
            raise ValueError("evolution run is not ready for activation recording")
        self.store.db.commit()
