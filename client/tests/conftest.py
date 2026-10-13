"""Shared pytest setup that is safe on a clean checkout."""

from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def immediate_reply_rhythm(monkeypatch):
    # Product rhythm has dedicated deterministic tests. Other synthetic sends
    # must not inherit the machine's current evening bathing window.
    import local_server
    monkeypatch.setattr(local_server, "_current_life_rhythm", lambda: {})


@pytest.fixture(autouse=True)
def present_user(request, monkeypatch):
    # Life slows to 3-5 hours while the user has not written. Synthetic worlds
    # rarely record exchanges, so tests see an active user unless they opt in.
    if request.node.get_closest_marker("real_user_idle") is None:
        from runtime.private_world import daily_life
        monkeypatch.setattr(daily_life, "_user_idle", lambda exchanges, now: False)


def pytest_configure(config) -> None:
    """Create the parent of pytest's configured basetemp before fixtures run.

    ``pytest.ini`` intentionally keeps evidence under ``.evidence/pytest``.
    Pytest creates the final basetemp itself, but does not create a missing
    parent directory on all supported Windows runners.
    """

    config.addinivalue_line("markers", "real_user_idle: use the real absent-user check for life cadence")
    configured = config.getoption("basetemp")
    if configured:
        Path(configured).parent.mkdir(parents=True, exist_ok=True)
