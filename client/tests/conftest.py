"""Shared pytest setup that is safe on a clean checkout."""

import os
from pathlib import Path
import pytest

# Node harnesses that run a slice of an injected script need the Chinese-mode t() helper (tests/js_shim).
_SHIM = Path(__file__).resolve().parent / "js_shim" / "olivia_i18n_shim.js"


@pytest.fixture(autouse=True)
def immediate_reply_rhythm(monkeypatch):
    # Product rhythm has dedicated deterministic tests. Other synthetic sends
    # must not inherit the machine's current evening bathing window.
    import local_server
    monkeypatch.setattr(local_server, "_current_life_rhythm", lambda: {})


@pytest.fixture(autouse=True)
def chinese_browser_language(monkeypatch):
    # The injected UI follows the browser language when no choice is stored; the browser tests assert the Chinese
    # text, so their pages default to a Chinese locale (a test can still pass its own locale). Tests that run a
    # slice of a script also get the Chinese-mode t() helper as a page global (tests/js_shim).
    try:
        from playwright.sync_api import Browser
    except ImportError:
        return

    for name in ("new_page", "new_context"):
        original = getattr(Browser, name)

        def patched(self, *args, _original=original, **kwargs):
            kwargs.setdefault("locale", "zh-CN")
            result = _original(self, *args, **kwargs)
            result.add_init_script(path=str(_SHIM))
            if hasattr(result, "evaluate"):
                # set_content() keeps the first document, so define the helper there as well
                result.evaluate(_SHIM.read_text(encoding="utf-8"))
            return result

        monkeypatch.setattr(Browser, name, patched)


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

    option = f'--require "{_SHIM.as_posix()}"'
    current = os.environ.get("NODE_OPTIONS", "")
    if str(_SHIM.as_posix()) not in current:
        os.environ["NODE_OPTIONS"] = (current + " " + option).strip()

    config.addinivalue_line("markers", "real_user_idle: use the real absent-user check for life cadence")
    configured = config.getoption("basetemp")
    if configured:
        Path(configured).parent.mkdir(parents=True, exist_ok=True)
