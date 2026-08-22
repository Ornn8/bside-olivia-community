from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.verify_b08_scope as b08_scope
from tools.scope_compat import scope_ci_diff_mode
from tools.verify_b08_scope import B08_BASELINE, check_scope, is_b08_path


ROOT = Path(__file__).resolve().parents[2]


# Experimental advisory: standalone B08 fixed-base ownership remains a local audit;
# current-main composition is the blocking boundary.
@pytest.mark.experimental
def test_b08_scope_is_fixed_base_aware_and_current_paths_are_owned() -> None:
    report = check_scope()

    assert report["baseline"] == B08_BASELINE
    assert report["base_is_ancestor"] is True
    assert report["status"] == "PASS", report
    assert report["unexpected_paths"] == []
    assert is_b08_path("live/session.py")
    assert is_b08_path("tools/live_e2e_acceptance.py")
    assert is_b08_path("tests/live/test_e2e_acceptance.py")
    assert is_b08_path("tests/live/test_scope.py")
    assert not is_b08_path("tts/service.py")
    assert not is_b08_path("live/private.key")
    assert not is_b08_path("tests/live/fixture.bin")


def test_b08_scope_rejects_unrelated_dirty_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b08_scope, "_status_paths", lambda: ["tts/service.py"])

    report = b08_scope.check_scope()

    assert report["status"] == "FAIL"
    assert "tts/service.py" in report["unexpected_paths"]


def test_b08_non_recursive_child_rejects_unrelated_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git(*args: str) -> list[str]:
        if args[:1] == ("rev-parse",):
            return ["base"]
        if args[:1] == ("merge-base",):
            return ["base"]
        return []

    monkeypatch.setattr(b08_scope, "_git", fake_git)
    monkeypatch.setattr(b08_scope, "_status_paths", lambda: ["unrelated.py"])

    report = b08_scope.check_scope(
        base="base",
        head="HEAD",
        compose_b10b=False,
        excluded=frozenset({"unrelated.py"}),
    )

    assert report["status"] == "FAIL"
    assert report["rejected_exclusions"] == ["unrelated.py"]
    assert report["unexpected_paths"] == ["unrelated.py"]


def test_b08_default_composition_rejects_unrelated_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git(*args: str) -> list[str]:
        if args[:1] == ("rev-parse",):
            return ["base"]
        if args[:1] == ("merge-base",):
            return ["base"]
        return []

    monkeypatch.setattr(b08_scope, "_git", fake_git)
    monkeypatch.setattr(b08_scope, "_status_paths", lambda: [])
    monkeypatch.setattr(
        b08_scope,
        "_verified_b10b_paths",
        lambda _b08_paths: (frozenset(), False),
    )
    monkeypatch.setattr(
        b08_scope,
        "_compose_children",
        lambda *_args, **_kwargs: {"b11": {"status": "PASS", "scope_paths": []}},
    )

    report = b08_scope.check_scope(
        base="base",
        head="HEAD",
        excluded=frozenset({"unrelated.py"}),
    )

    assert report["status"] == "FAIL"
    assert report["rejected_exclusions"] == ["unrelated.py"]


def test_b08_composed_scope_propagates_child_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.verify_b05_scope as b05_scope

    monkeypatch.setattr(b05_scope, "check_scope", lambda **_kwargs: {"status": "FAIL"})

    with scope_ci_diff_mode(False):
        report = b08_scope.check_scope(composed=True)

    assert report["status"] == "FAIL"
    assert report["composition_pass"] is False
    assert report["child_reports"]["b05"]["status"] == "FAIL"
    # A failed child cannot donate its owned paths to the parent composition;
    # otherwise the current B05 files would be silently allow-listed.
    assert "asr/management.py" in report["unexpected_paths"]


def test_b08_composed_scope_propagates_gov_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.verify_gov_scope as gov_scope

    monkeypatch.setattr(gov_scope, "check_scope", lambda **_kwargs: {"status": "FAIL"})

    report = b08_scope.check_scope(composed=True)

    assert report["status"] == "FAIL"
    assert report["composition_pass"] is False
    assert report["child_reports"]["gov"]["status"] == "FAIL"


def test_b08_composed_scope_does_not_accept_b02_paths_after_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.verify_b02_scope as b02_scope

    monkeypatch.setattr(b02_scope, "check_scope", lambda **_kwargs: {"status": "FAIL"})

    with scope_ci_diff_mode(False):
        report = b08_scope.check_scope(composed=True)

    assert report["status"] == "FAIL"
    assert report["child_reports"]["b02"]["status"] == "FAIL"
    assert "contracts/http_contract.schema.json" in report["unexpected_paths"]


def test_b08_historical_b10b_failure_does_not_donate_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.verify_b10b_scope as b10b_scope

    captured: dict[str, object] = {}
    monkeypatch.setattr(b10b_scope, "check_scope", lambda: {"status": "FAIL", "scope_paths": ["docs/B10B_MODULE_LIFECYCLE.md"]})
    monkeypatch.setattr(b08_scope, "_verified_governance_paths", lambda _paths: (frozenset(), False))
    def historical_children(
        _paths: frozenset[str],
        *,
        historical_mutual: bool = False,
        include_current_main: bool = True,
    ) -> dict[str, object]:
        captured["historical_mutual"] = historical_mutual
        captured["include_current_main"] = include_current_main
        return {}

    monkeypatch.setattr(b08_scope, "_compose_children", historical_children)

    def capture_check_scope(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "status": "PASS",
            "baseline": "base",
            "head": "head",
            "base_is_ancestor": True,
            "changed_paths": ["docs/B10B_MODULE_LIFECYCLE.md"],
            "status_paths": [],
            "unexpected_paths": ["docs/B10B_MODULE_LIFECYCLE.md"],
            "media_paths": [],
            "composed": False,
            "composition_pass": "not-run",
        }

    monkeypatch.setattr(b08_scope, "check_scope", capture_check_scope)

    assert b08_scope.main(["--historical-only"]) == 1
    assert captured["historical_mutual"] is True
    assert "docs/B10B_MODULE_LIFECYCLE.md" not in captured["excluded"]


# Experimental advisory: fixed historical B08 CI-base audit is non-blocking.
@pytest.mark.experimental
def test_b08_historical_cli_uses_valid_ci_diff_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIFF_BASE", "91c2e715f6823dcf6dad912cca062afdee573f99")

    assert b08_scope.main(["--historical-only"]) == 0


@pytest.mark.parametrize("historical_mutual", [True, False])
def test_b08_mutual_p01_b10a_donates_only_after_both_pass(
    monkeypatch: pytest.MonkeyPatch, historical_mutual: bool,
) -> None:
    import tools.scope_compat as compat
    import tools.verify_B07_scope as b07_scope
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_b02_scope as b02_scope
    import tools.verify_b04_scope as b04_scope
    import tools.verify_b05_scope as b05_scope
    import tools.verify_b06_scope as b06_scope
    import tools.verify_b10b_scope as b10b_scope
    import tools.verify_gov_scope as gov_scope
    import tools.verify_p01_scope as p01_scope

    for module, name in (
        (b05_scope, "current_b05_paths"),
        (b06_scope, "current_b06_paths"),
        (b07_scope, "current_b07_paths"),
        (gov_scope, "current_gov_paths"),
        (b10b_scope, "current_b10b_paths"),
    ):
        monkeypatch.setattr(module, name, lambda: frozenset())
    monkeypatch.setattr(b10a_scope, "current_b10a_paths", lambda: frozenset({"b10a.py"}))
    monkeypatch.setattr(p01_scope, "current_p01_paths", lambda: frozenset({"p01.py"}))
    monkeypatch.setattr(compat, "current_b11_paths", lambda: frozenset())
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))
    provisional_b02: list[frozenset[str]] = []
    monkeypatch.setattr(
        compat,
        "verified_b02_paths",
        lambda excluded: (provisional_b02.append(excluded) or (frozenset(), False)),
    )
    seen: dict[str, frozenset[str]] = {}

    def child(name: str, _checker: object, *, excluded: frozenset[str] = frozenset(), **_kwargs: object) -> dict[str, object]:
        seen[name] = excluded
        return {"status": "PASS", "scope_paths": [], "changed_paths": [], "status_paths": []}

    monkeypatch.setattr(b08_scope, "_safe_child", child)
    reports = b08_scope._compose_children(frozenset(), historical_mutual=historical_mutual)

    assert "b10a.py" in seen["p01"]
    assert "p01.py" in seen["b10a"]
    assert "p01.py" in provisional_b02[0]
    assert {"b10a.py", "p01.py"} <= seen["b02"]
    assert reports["p01"]["scope_paths"] == ["p01.py"]
    assert reports["b10a"]["scope_paths"] == ["b10a.py"]


def test_b08_composition_tracks_p02_02_as_an_independent_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.scope_compat as compat
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_B07_scope as b07_scope
    import tools.verify_b05_scope as b05_scope
    import tools.verify_b06_scope as b06_scope
    import tools.verify_b10b_scope as b10b_scope
    import tools.verify_gov_scope as gov_scope
    import tools.verify_p01_scope as p01_scope
    import tools.verify_p02_scope as p02_scope

    p02_paths = frozenset(p02_scope.P02_02_EXACT)
    for module, name in (
        (b05_scope, "current_b05_paths"),
        (b06_scope, "current_b06_paths"),
        (b07_scope, "current_b07_paths"),
        (gov_scope, "current_gov_paths"),
        (b10b_scope, "current_b10b_paths"),
        (b10a_scope, "current_b10a_paths"),
        (p01_scope, "current_p01_paths"),
        (compat, "current_b11_paths"),
    ):
        monkeypatch.setattr(module, name, lambda: frozenset())
    monkeypatch.setattr(p02_scope, "current_p02_paths", lambda: p02_paths)
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))
    monkeypatch.setattr(compat, "verified_b02_paths", lambda _excluded: (frozenset(), False))
    seen: dict[str, frozenset[str]] = {}

    def child(
        name: str,
        _checker: object,
        *,
        excluded: frozenset[str] = frozenset(),
        **_kwargs: object,
    ) -> dict[str, object]:
        seen[name] = excluded
        return {"status": "PASS", "scope_paths": [], "changed_paths": [], "status_paths": []}

    monkeypatch.setattr(b08_scope, "_safe_child", child)
    reports = b08_scope._compose_children(frozenset(), include_current_main=False)

    assert reports["p02"]["scope_paths"] == sorted(p02_paths)
    assert p02_paths <= seen["p01"]


@pytest.mark.parametrize("failed", ["p01", "b10a"])
def test_b08_historical_mutual_failure_donates_neither_sibling(
    monkeypatch: pytest.MonkeyPatch, failed: str
) -> None:
    import tools.scope_compat as compat
    import tools.verify_B07_scope as b07_scope
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_b02_scope as b02_scope
    import tools.verify_b04_scope as b04_scope
    import tools.verify_b05_scope as b05_scope
    import tools.verify_b06_scope as b06_scope
    import tools.verify_b10b_scope as b10b_scope
    import tools.verify_gov_scope as gov_scope
    import tools.verify_p01_scope as p01_scope

    for module, name in (
        (b05_scope, "current_b05_paths"), (b06_scope, "current_b06_paths"),
        (b07_scope, "current_b07_paths"), (gov_scope, "current_gov_paths"),
        (b10b_scope, "current_b10b_paths"),
    ):
        monkeypatch.setattr(module, name, lambda: frozenset())
    monkeypatch.setattr(b10a_scope, "current_b10a_paths", lambda: frozenset({"b10a.py"}))
    monkeypatch.setattr(p01_scope, "current_p01_paths", lambda: frozenset({"p01.py"}))
    monkeypatch.setattr(compat, "current_b11_paths", lambda: frozenset())
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))
    seen: dict[str, frozenset[str]] = {}

    def child(name: str, _checker: object, *, excluded: frozenset[str] = frozenset(), **_kwargs: object) -> dict[str, object]:
        seen[name] = excluded
        status = "FAIL" if name == failed else "PASS"
        return {"status": status, "scope_paths": [], "changed_paths": [], "status_paths": []}

    monkeypatch.setattr(b08_scope, "_safe_child", child)
    reports = b08_scope._compose_children(frozenset(), historical_mutual=True)

    assert reports[failed]["status"] == "FAIL"
    assert not all(report.get("status") == "PASS" for report in reports.values())
    assert "b10a.py" not in seen["b02"]
    assert "p01.py" not in seen["b02"]
    if failed == "p01":
        assert reports["p01"].get("scope_paths", []) == []


def test_b08_contracts_and_provenance_are_sanitized_and_complete() -> None:
    provenance = json.loads((ROOT / "live/provenance.json").read_text(encoding="utf-8"))
    event_schema = json.loads((ROOT / "contracts/live_event.schema.json").read_text(encoding="utf-8"))
    health_schema = json.loads((ROOT / "contracts/live_health.schema.json").read_text(encoding="utf-8"))
    provenance_schema = json.loads(
        (ROOT / "contracts/live_provenance.schema.json").read_text(encoding="utf-8")
    )

    assert event_schema["$id"] == "b08.live.event.v1"
    assert health_schema["$id"] == "b08.live.health.v1"
    assert provenance_schema["$id"] == "b08.live.provenance.v1"
    assert provenance["composition"] == "B08"
    assert {item["component"] for item in provenance["upstreams"]} == {
        "B03-llm",
        "B04-memory-persona",
        "B05-streaming-asr",
        "B06-tts",
        "B07-visual-driver",
    }
    for item in provenance["upstreams"]:
        assert item["source_repo"]
        assert item["fixed_ref"]
        assert item["license"]
        assert item["license_spdx"]
        assert item["license_evidence"]
        assert item["replacement_boundary"]
        assert item["uninstall_boundary"]
    encoded = repr(provenance)
    assert "D:/" not in encoded
    assert "F:/" not in encoded
    assert "api_key" not in encoded.casefold()


def test_required_ci_enforces_collection_count_and_b08_health() -> None:
    workflow = (ROOT / ".github/workflows/required-ci.yml").read_text(encoding="utf-8")

    assert "tests? collected" in workflow
    assert "$collected -le 0" in workflow
    assert "$failures" in workflow
    assert "$errors" in workflow
    assert "tools/live_healthcheck.py" in workflow
