"""Shared pytest setup that is safe on a clean checkout."""

from pathlib import Path


def pytest_configure(config) -> None:
    """Create the parent of pytest's configured basetemp before fixtures run.

    ``pytest.ini`` intentionally keeps evidence under ``.evidence/pytest``.
    Pytest creates the final basetemp itself, but does not create a missing
    parent directory on all supported Windows runners.
    """

    configured = config.getoption("basetemp")
    if configured:
        Path(configured).parent.mkdir(parents=True, exist_ok=True)
