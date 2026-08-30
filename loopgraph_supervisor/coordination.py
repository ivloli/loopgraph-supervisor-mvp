from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from .store import SQLiteStore

CandidateAction = Literal["add", "remove", "retain", "change"]


@dataclass(frozen=True)
class CandidateClaim:
    subject: str
    action: CandidateAction

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("candidate claim requires a subject")

    @property
    def normalized_subject(self) -> str:
        return " ".join(self.subject.casefold().split())


@dataclass(frozen=True)
class CoordinatedCandidate:
    candidate_id: str
    parent_commit: str
    candidate_commit: str
    changed_paths: tuple[str, ...]
    claims: tuple[CandidateClaim, ...]

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.parent_commit or not self.candidate_commit:
            raise ValueError("candidate requires an id, parent commit, and candidate commit")
        if not self.changed_paths:
            raise ValueError("candidate requires changed paths")


@dataclass(frozen=True)
class CoordinationResult:
    parent_commit: str
    candidate_ids: tuple[str, ...]
    conflicting_candidate_ids: tuple[str, ...]
    conflict_subjects: tuple[str, ...]
    selected_candidate_id: str | None = None

    @property
    def can_open_pr(self) -> bool:
        return len(self.candidate_ids) == 1 and self.selected_candidate_id is not None and not self.conflicting_candidate_ids


@dataclass(frozen=True)
class CandidateApproval:
    candidate_id: str
    parent_commit: str
    candidate_commit: str
    reviewer: str


def _authorize_current_result(result: CoordinationResult, candidate: CoordinatedCandidate, approval: CandidateApproval) -> None:
    if result.parent_commit != candidate.parent_commit or not result.can_open_pr or result.selected_candidate_id != candidate.candidate_id:
        raise ValueError("candidate was not selected as the coordinated PR")
    if (approval.candidate_id, approval.parent_commit, approval.candidate_commit) != (candidate.candidate_id, candidate.parent_commit, candidate.candidate_commit):
        raise ValueError("approval is stale or does not bind the selected candidate head")
    if not approval.reviewer.strip():
        raise ValueError("approval requires a reviewer")


class CandidateCoordinator:
    """Host authority that serializes AI proposals into one coherent PR candidate."""

    def __init__(self, parent_commit: str):
        if not parent_commit:
            raise ValueError("canonical baseline requires a commit")
        self.parent_commit = parent_commit
        self._candidates: dict[str, CoordinatedCandidate] = {}

    def register(self, candidate: CoordinatedCandidate) -> CoordinationResult:
        if candidate.parent_commit != self.parent_commit:
            raise ValueError("candidate is based on a stale or different canonical baseline")
        existing = self._candidates.get(candidate.candidate_id)
        if existing is not None and existing != candidate:
            raise ValueError("candidate id is immutable")
        self._candidates[candidate.candidate_id] = candidate
        return self.result()

    def select(self, candidate_id: str) -> CoordinationResult:
        result = self.result()
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if candidate_id in result.conflicting_candidate_ids:
            raise ValueError("conflicting candidate cannot be selected without resolving the conflict")
        return CoordinationResult(self.parent_commit, result.candidate_ids, result.conflicting_candidate_ids, result.conflict_subjects, candidate_id)

    def result(self) -> CoordinationResult:
        by_subject: dict[str, dict[CandidateAction, set[str]]] = {}
        for candidate in self._candidates.values():
            for claim in candidate.claims:
                by_subject.setdefault(claim.normalized_subject, {}).setdefault(claim.action, set()).add(candidate.candidate_id)
        conflict_subjects = tuple(sorted(subject for subject, actions in by_subject.items() if len(actions) > 1))
        conflicting = {
            candidate_id
            for actions in by_subject.values()
            if len(actions) > 1
            for candidate_ids in actions.values()
            for candidate_id in candidate_ids
        }
        return CoordinationResult(self.parent_commit, tuple(sorted(self._candidates)), tuple(sorted(conflicting)), conflict_subjects)


class CoordinationStore:
    """Durable candidate set and Host selection for one canonical Git baseline."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS candidate_coordination (candidate_id TEXT PRIMARY KEY, parent_commit TEXT NOT NULL, candidate_commit TEXT NOT NULL, document TEXT NOT NULL)"
        )
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS candidate_selections (parent_commit TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, candidate_commit TEXT NOT NULL)"
        )
        self.store.db.commit()

    def register(self, candidate: CoordinatedCandidate) -> CoordinationResult:
        coordinator = self.coordinator(candidate.parent_commit)
        result = coordinator.register(candidate)
        document = json.dumps(
            {
                "changed_paths": list(candidate.changed_paths),
                "claims": [{"subject": claim.subject, "action": claim.action} for claim in candidate.claims],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        existing = self.store.db.execute("SELECT parent_commit,candidate_commit,document FROM candidate_coordination WHERE candidate_id=?", (candidate.candidate_id,)).fetchone()
        if existing is not None and (existing["parent_commit"], existing["candidate_commit"], existing["document"]) != (candidate.parent_commit, candidate.candidate_commit, document):
            raise ValueError("candidate id is immutable")
        self.store.db.execute(
            "INSERT OR IGNORE INTO candidate_coordination VALUES(?,?,?,?)",
            (candidate.candidate_id, candidate.parent_commit, candidate.candidate_commit, document),
        )
        self.store.db.commit()
        return result

    def select(self, parent_commit: str, candidate_id: str) -> CoordinationResult:
        coordinator = self.coordinator(parent_commit)
        result = coordinator.select(candidate_id)
        candidate = next(item for item in self.candidates(parent_commit) if item.candidate_id == candidate_id)
        self.store.db.execute(
            "INSERT INTO candidate_selections VALUES(?,?,?) ON CONFLICT(parent_commit) DO UPDATE SET candidate_id=excluded.candidate_id,candidate_commit=excluded.candidate_commit",
            (parent_commit, candidate_id, candidate.candidate_commit),
        )
        self.store.db.commit()
        return result

    def result(self, parent_commit: str) -> CoordinationResult:
        result = self.coordinator(parent_commit).result()
        selected = self.store.db.execute("SELECT candidate_id,candidate_commit FROM candidate_selections WHERE parent_commit=?", (parent_commit,)).fetchone()
        if selected is None:
            return result
        candidate = next((item for item in self.candidates(parent_commit) if item.candidate_id == selected["candidate_id"]), None)
        if candidate is None or candidate.candidate_commit != selected["candidate_commit"]:
            raise RuntimeError("stored selection no longer binds a candidate head")
        return CoordinationResult(result.parent_commit, result.candidate_ids, result.conflicting_candidate_ids, result.conflict_subjects, candidate.candidate_id)

    def authorize_pr(self, candidate_id: str, approval: CandidateApproval) -> None:
        """Authorize against freshly reconstructed durable state."""
        candidate = next((item for item in self.candidates(approval.parent_commit) if item.candidate_id == candidate_id), None)
        if candidate is None:
            raise ValueError("approval candidate is not registered for its parent")
        _authorize_current_result(self.result(approval.parent_commit), candidate, approval)

    def coordinator(self, parent_commit: str) -> CandidateCoordinator:
        coordinator = CandidateCoordinator(parent_commit)
        for candidate in self.candidates(parent_commit):
            coordinator.register(candidate)
        return coordinator

    def candidates(self, parent_commit: str) -> tuple[CoordinatedCandidate, ...]:
        rows = self.store.db.execute("SELECT * FROM candidate_coordination WHERE parent_commit=? ORDER BY candidate_id", (parent_commit,)).fetchall()
        candidates = []
        for row in rows:
            document = json.loads(row["document"])
            claims = tuple(CandidateClaim(item["subject"], item["action"]) for item in document["claims"])
            candidates.append(CoordinatedCandidate(row["candidate_id"], row["parent_commit"], row["candidate_commit"], tuple(document["changed_paths"]), claims))
        return tuple(candidates)

    def candidate(self, parent_commit: str, candidate_id: str) -> CoordinatedCandidate:
        candidate = next((item for item in self.candidates(parent_commit) if item.candidate_id == candidate_id), None)
        if candidate is None:
            raise KeyError(candidate_id)
        return candidate
