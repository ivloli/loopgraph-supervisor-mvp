from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowStatus(str, Enum):
    RUNNING = "RUNNING"
    UNCERTAIN = "UNCERTAIN"
    PAUSED = "PAUSED"
    WAITING_HITL = "WAITING_HITL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Node(str, Enum):
    EXECUTE = "AGENT_EXECUTE"
    VERIFY = "VERIFY"
    PROMOTE = "VERSION_PROMOTE"
    COMPLETE = "COMPLETED"
    FAILED = "FAILED"
    HITL = "WAIT_HITL"


@dataclass
class Workflow:
    id: str
    goal: str
    status: WorkflowStatus = WorkflowStatus.RUNNING
    current_node: Node = Node.EXECUTE
    attempt: int = 0
    max_attempts: int = 3
    acceptance: dict[str, Any] = field(default_factory=dict)
    active_version: str = ""
    spec_id: str = "coding-supervisor"
    spec_revision: int = 1
    spec_hash: str = ""
    pause_requested: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass
class AgentInput:
    workflow_id: str
    goal: str
    attempt: int
    feedback: str = ""
    proposal: dict[str, Any] | None = None
    acceptance: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentOutput:
    artifact: dict[str, Any]
    summary: str
    session_id: str = ""


@dataclass
class Verification:
    passed: bool
    feedback: str
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DecisionRecord:
    id: str
    workflow_id: str
    attempt: int
    decision_type: str
    question: str
    decision: str
    rationale: list[str]
    evidence: list[dict[str, Any]]
    alternatives: list[dict[str, str]]
    risk: str
    expected_effect: str
    created_by: str = "supervisor-policy"
    created_at: str = field(default_factory=utc_now)


@dataclass
class ImprovementProposal:
    id: str
    workflow_id: str
    based_on_attempt: int
    problem: str
    hypothesis: str
    changes: list[str]
    expected_evidence: list[str]
    risk_level: str
    status: str = "PROPOSED"
    created_at: str = field(default_factory=utc_now)


@dataclass
class Version:
    id: str
    workflow_id: str
    parent_id: str
    artifact: dict[str, Any]
    status: str = "PROMOTED"
    created_at: str = field(default_factory=utc_now)
