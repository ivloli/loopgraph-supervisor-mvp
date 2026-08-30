from loopgraph_supervisor.activation import ActivationService


def test_failed_post_activation_canary_rolls_back():
    calls = []
    result = ActivationService().activate("v1", "v2", lambda: calls.append("activate"), lambda: False, lambda: calls.append("rollback"), lambda version: version == "v1")

    assert result.active_version == "v1"
    assert result.rolled_back is True
    assert calls == ["activate", "rollback"]


def test_successful_post_activation_canary_keeps_candidate():
    result = ActivationService().activate("v1", "v2", lambda: None, lambda: True, lambda: None, lambda version: version == "v2")

    assert result.active_version == "v2"
    assert result.rolled_back is False


def test_activation_exception_still_attempts_rollback():
    calls = []

    def activate():
        calls.append("activate")
        raise RuntimeError("partial activation")

    result = ActivationService().activate("v1", "v2", activate, lambda: True, lambda: calls.append("rollback"), lambda version: version == "v1")

    assert result.rolled_back is True
    assert calls == ["activate", "rollback"]


def test_failed_active_state_verification_still_rolls_back():
    calls = []
    result = ActivationService().activate("v1", "v2", lambda: calls.append("activate"), lambda: True, lambda: calls.append("rollback"), lambda version: version == "v1")

    assert result.rolled_back is True
    assert calls == ["activate", "rollback"]
