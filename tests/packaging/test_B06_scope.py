"""B06 scope must remain baseline-aware in both dirty and committed states."""

from __future__ import annotations

import pytest

import tools.verify_B10A_scope as b10a_scope
import tools.verify_b06_scope as b06_scope
import tools.verify_b02_scope as b02_scope
import tools.verify_p01_scope as p01_scope
from tools.verify_b06_scope import B06_BASELINE, check_scope


# Experimental advisory: standalone B06 fixed-base cleanliness is a duplicate audit.
@pytest.mark.experimental
def test_b06_scope_is_clean() -> None:
    report = check_scope()
    assert report["status"] == "PASS", report
    assert report["unexpected_paths"] == []
    assert report["media_paths"] == []


def test_b06_scope_validates_tranche_from_accepted_baseline() -> None:
    report = check_scope()
    assert report["baseline"] == B06_BASELINE
    assert "tts/service.py" in report["changed_paths"] or "tts/service.py" in report["status_paths"]


def test_b06_scope_rejects_unrelated_dirty_path(monkeypatch) -> None:
    def fake_git(*args: str) -> str:
        if args[:2] == ("diff", "--name-only"):
            return "tts/service.py\n"
        if args[:2] == ("status", "--short"):
            return "?? unrelated.py\n"
        return ""

    monkeypatch.setattr(b06_scope, "_git", fake_git)
    report = b06_scope.check_scope()
    assert report["status"] == "FAIL", report
    assert report["unexpected_paths"] == ["unrelated.py"]


def test_b06_child_validates_exact_bridge_support_without_widening_historical_allowlist(
    monkeypatch,
) -> None:
    bridge_paths = {
        "tests/tts/test_external_adapter.py",
        "tts/external_cosyvoice_worker.py",
    }
    assert bridge_paths.isdisjoint(b06_scope.ALLOWED_PATHS)

    def fake_git(*args: str) -> str:
        if args[:2] == ("diff", "--name-only"):
            return "\n".join(sorted(bridge_paths)) + "\n"
        return ""

    monkeypatch.setattr(b06_scope, "_git", fake_git)
    monkeypatch.setattr(
        b06_scope,
        "_verified_gov_exclusions",
        lambda _excluded: (frozenset(), False),
    )

    report = b06_scope.check_scope(excluded=frozenset(), child_mode=True)

    assert report["status"] == "PASS", report
    assert set(report["scope_paths"]) == bridge_paths


# Experimental advisory: this standalone fixed-baseline composition audit is
# retained for diagnostics; B08 composed scope is the blocking gate.
@pytest.mark.experimental
def test_b06_scope_composes_through_p01_and_b10a() -> None:
    p01 = p01_scope.check_scope()
    b10a = b10a_scope.check_scope()
    b02 = b02_scope.check_scope()
    assert p01["status"] == "PASS", p01
    assert p01["composed_b06"] is True, p01
    assert b10a["status"] == "PASS", b10a
    assert b10a["composed_p01"] is True, b10a
    assert b02["status"] == "PASS", b02
    assert b02["composed_b06"] is True, b02
