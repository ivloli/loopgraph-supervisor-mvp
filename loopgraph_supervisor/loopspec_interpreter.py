from __future__ import annotations

from dataclasses import dataclass

from .loopspec import LoopSpec, Outcome


@dataclass(frozen=True)
class LoopTransition:
    source: str
    outcome: Outcome
    target: str
    iteration: int
    spec_hash: str


class LoopSpecInterpreter:
    """Deterministic interpreter for one immutable LoopSpec revision."""

    def __init__(self, spec: LoopSpec):
        self.spec = spec

    def transition(self, source: str, outcome: Outcome, iteration: int) -> LoopTransition:
        if iteration < 0:
            raise ValueError("LoopSpec iteration cannot be negative")
        if iteration >= self.spec.max_iterations and outcome in {"fail", "retry"}:
            raise RuntimeError(f"LoopSpec iteration limit exceeded: {self.spec.max_iterations}")
        return LoopTransition(source, outcome, self.spec.next_node(source, outcome), iteration, self.spec.content_hash())
