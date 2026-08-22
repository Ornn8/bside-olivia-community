from __future__ import annotations

import pytest

import tools.verify_B07_scope as b07_scope
import tools.verify_b05_scope as b05_scope
import tools.verify_b08_scope as b08_scope
from tools.scope_compat import scope_ci_diff_mode
from tools.verify_b05_scope import CANONICAL_BASE, HISTORICAL_BASE, check_scope, is_b05_path


# Experimental advisory: the standalone fixed-baseline audit is retained for
# diagnostics; current composed scope is the blocking release gate.
@pytest.mark.experimental
def test_b05_scope_is_fixed_base_aware_and_current_paths_are_owned() -> None:
    report = check_scope()
    assert report["canonical_base"] == CANONICAL_BASE
    assert report["base_is_ancestor"] is True
    assert report["status"] == "PASS", report
    assert report["unexpected_paths"] == []


def test_b05_scope_does_not_accept_unrelated_paths() -> None:
    assert is_b05_path("asr/provider.py")
    assert is_b05_path("tests/asr/test_scope.py")
    assert is_b05_path("docs/B05_STREAMING_ASR.md")
    assert not is_b05_path("TTS/production.py")
    assert not is_b05_path("asr/not-owned-random.py")
    assert not is_b05_path("tests/asr/test_not_owned.py")


# Experimental advisory: fixed historical B05/B07 composition is retained for local audit.
@pytest.mark.experimental
def test_historical_b05_scope_composes_only_a_passing_b07_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DIFF_BASE", raising=False)

    report = check_scope(base=HISTORICAL_BASE, compose_b07=True)

    assert report["status"] == "PASS", report
    assert report["composed_b07"] is True
    assert "docs/B07_VISUAL_DRIVER.md" in report["excluded_b07_paths"]


# Experimental advisory: fixed historical B05/B07 CI-base audit is non-blocking.
@pytest.mark.experimental
def test_historical_b05_uses_valid_ci_diff_for_b07_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIFF_BASE", "b47beaf8c236d9f8e6024fb44af88286ebc93792")
    child_reports: list[dict[str, object]] = []
    original = b07_scope.check_scope

    def record_b07_child(**kwargs: object) -> dict[str, object]:
        report = original(**kwargs)
        if kwargs.get("child_mode"):
            child_reports.append(report)
        return report

    monkeypatch.setattr(b07_scope, "check_scope", record_b07_child)

    report = check_scope(base=HISTORICAL_BASE, compose_b07=True)

    assert report["status"] == "PASS", report
    assert child_reports
    assert all("docs/B07_VISUAL_DRIVER.md" not in item["changed_paths"] for item in child_reports)
    assert "docs/B07_VISUAL_DRIVER.md" not in report["excluded_b07_paths"]


def test_b05_scope_rejects_an_unrelated_dirty_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b05_scope, "_status_paths", lambda: ["TTS/production.py"])

    report = b05_scope.check_scope()

    assert report["status"] == "FAIL"
    assert "TTS/production.py" in report["unexpected_paths"]


def test_b05_scope_propagates_forced_b07_child_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        b07_scope,
        "check_scope",
        lambda **_kwargs: {"status": "FAIL", "scope_paths": []},
    )

    report = b05_scope.check_scope(base=HISTORICAL_BASE, compose_b07=True)

    assert report["status"] == "FAIL"
    assert report["composed_b07"] is False


def test_b05_does_not_exclude_b08_paths_after_forced_b08_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(b08_scope, "check_scope", lambda **_kwargs: {"status": "FAIL"})

    with scope_ci_diff_mode(False):
        report = b05_scope.check_scope()

    assert report["status"] == "FAIL"
    assert "live/session.py" in report["unexpected_paths"]


# Experimental advisory: standalone B07/B05 composition is a duplicate audit.
@pytest.mark.experimental
def test_b07_scope_composes_only_a_passing_b05_child() -> None:
    report = b07_scope.check_scope()

    assert report["status"] == "PASS", report
    assert report["composed_b05"] is True
    assert "tools/visual_driver.py" in report["scope_paths"]


# Experimental advisory: standalone B07 composed CLI audit is non-blocking.
@pytest.mark.experimental
def test_b07_composed_b05_cli_uses_valid_ci_diff_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIFF_BASE", "b47beaf8c236d9f8e6024fb44af88286ebc93792")

    assert b07_scope.main(["--composed-b05"]) == 0


def test_b07_scope_rejects_an_unrelated_dirty_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b07_scope, "_status_paths", lambda: ["TTS/production.py"])

    report = b07_scope.check_scope()

    assert report["status"] == "FAIL"
    assert "TTS/production.py" in report["unexpected_paths"]


def test_b07_scope_propagates_forced_b05_child_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        b05_scope,
        "check_scope",
        lambda **_kwargs: {"status": "FAIL"},
    )

    report = b07_scope.check_scope()

    assert report["status"] == "FAIL"
    assert report["composed_b05"] is False
