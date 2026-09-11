"""Shared pytest setup that is safe on a clean checkout."""

from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def immediate_reply_rhythm(monkeypatch):
    # Product rhythm has dedicated deterministic tests. Other synthetic sends
    # must not inherit the machine's current 23:30-00:00 bathing window.
    import local_server
    monkeypatch.setattr(local_server, "_current_life_rhythm", lambda: {})


def pytest_configure(config) -> None:
    """Create the parent of pytest's configured basetemp before fixtures run.

    ``pytest.ini`` intentionally keeps evidence under ``.evidence/pytest``.
    Pytest creates the final basetemp itself, but does not create a missing
    parent directory on all supported Windows runners.
    """

    configured = config.getoption("basetemp")
    if configured:
        Path(configured).parent.mkdir(parents=True, exist_ok=True)
