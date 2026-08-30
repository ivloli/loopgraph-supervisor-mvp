from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ActivationResult:
    candidate_version: str
    previous_version: str
    canary_passed: bool
    active_version: str
    rolled_back: bool


class ActivationService:
    """Runs post-activation canary and restores the previous version on failure."""

    def activate(self, previous_version: str, candidate_version: str, activate: Callable[[], None], canary: Callable[[], bool], rollback: Callable[[], None], verify_active: Callable[[str], bool] | None = None) -> ActivationResult:
        if verify_active is None:
            raise ValueError("activation requires an active-version verifier")
        try:
            activate()
            if not verify_active(candidate_version):
                raise RuntimeError("candidate activation state could not be verified")
            passed = canary()
        except Exception:
            return self._rollback(candidate_version, previous_version, rollback, verify_active)
        if passed and verify_active(candidate_version):
            return ActivationResult(candidate_version, previous_version, True, candidate_version, False)
        if passed:
            return self._rollback(candidate_version, previous_version, rollback, verify_active)
        return self._rollback(candidate_version, previous_version, rollback, verify_active)

    @staticmethod
    def _rollback(candidate_version: str, previous_version: str, rollback: Callable[[], None], verify_active: Callable[[str], bool]) -> ActivationResult:
        try:
            rollback()
        except Exception as rollback_error:
            raise RuntimeError("activation failed and rollback failed") from rollback_error
        if not verify_active(previous_version):
            raise RuntimeError("activation failed and rollback state could not be verified")
        return ActivationResult(candidate_version, previous_version, False, previous_version, True)
