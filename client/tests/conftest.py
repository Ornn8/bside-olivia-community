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


class _MemoryGpuPreferences:
    """Stands in for HKCU UserGpuPreferences."""

    def __init__(self):
        self.data = {}

    def values(self):
        return dict(self.data)

    def write(self, name, data):
        self.data[name] = data

    def delete(self, name):
        self.data.pop(name, None)


@pytest.fixture(autouse=True)
def no_real_display_system(monkeypatch):
    # Display compat (installer/display_compat.py) reads and writes the Windows registry and inspects windows.
    # No test touches the real ones; tests/installer/test_display_compat.py passes its own fakes.
    from installer import display_compat
    monkeypatch.setattr(display_compat, "SYSTEM", display_compat.DisplaySystem(
        store=_MemoryGpuPreferences, device_ids=lambda: [], probe=lambda _pid: display_compat.WindowProbe(),
        watch=False))


@pytest.fixture(autouse=True)
def judgment_recall_unless_r3(request, monkeypatch):
    # Rerank recall (runtime/memory/r3_recall) is on by default in the product; other tests keep the former judgment and
    # tests/memory/test_r3_*.py switch it on with the `r3` marker, and no test depends on the machine's own r3-recall.json.
    value = "1" if request.node.get_closest_marker("r3") else "0"
    monkeypatch.setenv("OLIVIA_R3_RECALL", value)
    monkeypatch.setenv("OLIVIA_R3_FORMAT", value)
    monkeypatch.setenv("OLIVIA_R3_COMPACT", "1")


@pytest.fixture(autouse=True)
def chinese_mode_helper_in_browser_pages(monkeypatch):
    # Tests that run a slice of an injected script in a browser page need the Chinese-mode t() helper as a page
    # global (tests/js_shim); the full script defines its own.
    try:
        from playwright.sync_api import Browser
    except ImportError:
        return

    for name in ("new_page", "new_context"):
        original = getattr(Browser, name)

        def patched(self, *args, _original=original, **kwargs):
            result = _original(self, *args, **kwargs)
            result.add_init_script(path=str(_SHIM))
            if hasattr(result, "evaluate"):
                # set_content() keeps the first document, so define the helper there as well
                result.evaluate(_SHIM.read_text(encoding="utf-8"))
            return result

        monkeypatch.setattr(Browser, name, patched)


@pytest.fixture(autouse=True)
def interface_language_is_chinese():
    # The interface language is process-wide state; every test starts in Chinese, the default.
    from runtime import ui_language
    ui_language.reset()
    yield
    ui_language.reset()


@pytest.fixture(autouse=True)
def present_user(request, monkeypatch):
    # Life slows to 3-5 hours while the user has not written. Synthetic worlds
    # rarely record exchanges, so tests see an active user unless they opt in.
    if request.node.get_closest_marker("real_user_idle") is None:
        from runtime.private_world import daily_life
        monkeypatch.setattr(daily_life, "_user_idle", lambda exchanges, now: False)


@pytest.fixture(autouse=True)
def diary_told_by_her(request, monkeypatch):
    # Every diary entry is read once more to confirm it is told from her side. Stand-in
    # writers answer that call like any other, so tests skip it unless they opt in.
    if request.node.get_closest_marker("real_narrator_check") is None:
        from runtime.diary import diary

        async def told(*args, **kwargs):
            return True

        monkeypatch.setattr(diary, "told_by_her", told)


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
    config.addinivalue_line("markers", "real_narrator_check: read each diary entry once more for whose side it is told from")
    configured = config.getoption("basetemp")
    if configured:
        Path(configured).parent.mkdir(parents=True, exist_ok=True)
