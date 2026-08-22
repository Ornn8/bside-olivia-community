from __future__ import annotations

import pytest

from control_center.auth import (
    ControlAuthError,
    ControlSessionManager,
)


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_bootstrap_is_one_time_and_session_repr_hides_tokens() -> None:
    manager = ControlSessionManager()
    bootstrap = manager.issue_bootstrap_token()

    credentials = manager.bootstrap(bootstrap)

    assert credentials.session_token
    assert credentials.csrf_token
    assert credentials.session_token not in repr(credentials)
    assert credentials.csrf_token not in repr(credentials)
    with pytest.raises(
        ControlAuthError,
        match="CONTROL_BOOTSTRAP_INVALID",
    ):
        manager.bootstrap(bootstrap)


def test_authentication_slides_expiry_and_requires_matching_csrf() -> None:
    clock = Clock()
    manager = ControlSessionManager(
        idle_timeout_seconds=30,
        clock=clock,
    )
    credentials = manager.bootstrap(manager.issue_bootstrap_token())

    first_expiry = manager.authenticate(credentials.session_token)
    manager.validate_csrf(
        credentials.session_token,
        credentials.csrf_token,
    )
    clock.advance(10)
    second_expiry = manager.authenticate(credentials.session_token)

    assert first_expiry == 130
    assert second_expiry == 140
    with pytest.raises(ControlAuthError, match="CONTROL_CSRF_INVALID"):
        manager.validate_csrf(
            credentials.session_token,
            "wrong-token",
        )
    with pytest.raises(ControlAuthError, match="CONTROL_CSRF_REQUIRED"):
        manager.validate_csrf(credentials.session_token, None)


def test_expired_bootstrap_and_session_are_pruned() -> None:
    clock = Clock()
    manager = ControlSessionManager(
        bootstrap_ttl_seconds=5,
        idle_timeout_seconds=10,
        clock=clock,
    )
    expired_bootstrap = manager.issue_bootstrap_token()
    clock.advance(6)
    with pytest.raises(
        ControlAuthError,
        match="CONTROL_BOOTSTRAP_INVALID",
    ):
        manager.bootstrap(expired_bootstrap)

    credentials = manager.bootstrap(manager.issue_bootstrap_token())
    assert manager.status()["active_sessions"] == 1
    clock.advance(11)
    with pytest.raises(
        ControlAuthError,
        match="CONTROL_SESSION_INVALID",
    ):
        manager.authenticate(credentials.session_token)
    assert manager.status()["active_sessions"] == 0


def test_logout_is_idempotent() -> None:
    manager = ControlSessionManager()
    credentials = manager.bootstrap(manager.issue_bootstrap_token())

    manager.logout(credentials.session_token)
    manager.logout(credentials.session_token)
    manager.logout(None)

    with pytest.raises(
        ControlAuthError,
        match="CONTROL_SESSION_INVALID",
    ):
        manager.authenticate(credentials.session_token)
