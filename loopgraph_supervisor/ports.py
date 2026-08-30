from typing import Protocol

from .domain import AgentInput, AgentOutput, Verification


class AgentExecutor(Protocol):
    def execute(self, request: AgentInput) -> AgentOutput: ...


class Verifier(Protocol):
    def verify(self, output: AgentOutput, acceptance: dict) -> Verification: ...
