from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import tools.verify_b06_scope as b06_scope
import tools.verify_gov_scope as gov_scope
import tools.scope_compat as scope_compat
from tools.scope_compat import scope_ci_diff_mode
from tools.verify_gov_scope import GOV_PATHS, GOV_SUPPORT_PATHS


ROOT = Path(__file__).resolve().parents[2]


# Experimental advisory: this standalone fixed-baseline composition audit is
# retained for diagnostics; current composed scope is the blocking gate.
@pytest.mark.experimental
def test_gov_scope_accepts_exactly_the_three_governance_documents() -> None:
    import tools.verify_b10b_scope as b10b_scope

    b10b_report = b10b_scope.check_scope()
    assert b10b_report["status"] == "PASS", b10b_report
    report = gov_scope.check_scope(excluded=frozenset(b10b_report["scope_paths"]))

    assert report["status"] == "PASS", report
    assert set(report["scope_paths"]) == set(GOV_PATHS)
    assert report["unexpected_paths"] == []


def test_gov_support_accepts_exact_baseline_scanner_only() -> None:
    assert "baseline_hardening_scan.py" in GOV_SUPPORT_PATHS
    assert "baseline_hardening_scan.py.py" not in GOV_SUPPORT_PATHS
    assert "tests/conftest.py" in GOV_SUPPORT_PATHS
    assert "tests/conftest.py.bak" not in GOV_SUPPORT_PATHS


def test_current_gov_child_donates_only_exact_owned_diff_paths(monkeypatch) -> None:
    def fake_git(*args: str) -> list[str]:
        if args[:1] == ("rev-parse",) or args[:1] == ("merge-base",):
            return ["base"]
        if args[:2] == ("diff", "--name-only"):
            return [
                "baseline_hardening_scan.py",
                "tests/conftest.py",
                "requirements-ci.txt",
                "random.py",
            ]
        if args[:2] == ("status", "--short"):
            return []
        return []

    monkeypatch.setattr(gov_scope, "_git", fake_git)
    monkeypatch.setattr(
        scope_compat,
        "effective_scope_base",
        lambda _fallback, _head="HEAD": "base",
    )
    monkeypatch.setattr(
        scope_compat,
        "verified_b02_paths",
        lambda _excluded: (frozenset(), False),
    )

    report = gov_scope.check_scope(child_mode=True)

    assert report["status"] == "FAIL"
    assert report["scope_paths"] == ["baseline_hardening_scan.py", "tests/conftest.py"]
    assert "requirements-ci.txt" not in report["scope_paths"]
    assert "random.py" in report["unexpected_paths"]


def test_gov_scope_rejects_an_unrelated_dirty_path(monkeypatch) -> None:
    monkeypatch.setattr(gov_scope, "_status_paths", lambda: ["unrelated.py"])

    report = gov_scope.check_scope()

    assert report["status"] == "FAIL", report
    assert "unrelated.py" in report["unexpected_paths"]


def test_gov_scope_rejects_direct_governance_exclusions() -> None:
    report = gov_scope.check_scope(excluded=GOV_PATHS)

    assert report["status"] == "FAIL", report
    assert sorted(GOV_PATHS) == report["rejected_exclusions"]


# Experimental advisory: standalone GOV/B08 exclusion audit is non-blocking;
# current-main composition remains the blocking gate.
@pytest.mark.experimental
def test_gov_does_not_reject_unmodified_b08_exclusion_but_rejects_actual_path(monkeypatch) -> None:
    # .gitignore is B08-owned but absent from this actual change-set, so it is
    # not a rejected direct B08 exclusion.  A real changed B08 path remains
    # fail-closed when the B08 child did not verify it.
    with scope_ci_diff_mode(False):
        clean = gov_scope.check_scope(excluded=frozenset({".gitignore"}))
        assert ".gitignore" not in clean["rejected_b08_exclusions"]
        monkeypatch.setattr(gov_scope, "_verified_b08_paths", lambda _excluded: (frozenset(), False))
        report = gov_scope.check_scope(excluded=frozenset({"live/session.py"}))
    assert report["status"] == "FAIL"
    assert "live/session.py" in report["rejected_b08_exclusions"]


def test_invalid_or_nonancestor_diff_base_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("DIFF_BASE", "not-a-revision")
    assert scope_compat.effective_scope_base("fixed") == "fixed"

    class Result:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(args, **_kwargs):
        return Result("candidate\n" if args[1] == "rev-parse" else "different\n")

    monkeypatch.setattr(scope_compat.subprocess, "run", fake_run)
    monkeypatch.setenv("DIFF_BASE", "candidate")
    assert scope_compat.effective_scope_base("fixed") == "fixed"


def test_head_diff_base_falls_back_instead_of_claiming_an_empty_pr(monkeypatch) -> None:
    monkeypatch.setenv("DIFF_BASE", "HEAD")
    assert scope_compat.effective_scope_base("fixed") == "fixed"


def test_historical_scope_does_not_exclude_gov_after_child_failure(monkeypatch) -> None:
    monkeypatch.setattr(gov_scope, "check_scope", lambda **_kwargs: {"status": "FAIL"})

    report = b06_scope.check_scope(excluded=frozenset(GOV_PATHS))

    assert report["status"] == "FAIL", report
    assert not set(report["excluded_paths"]) & set(GOV_PATHS)
    assert set(GOV_PATHS) <= set(report["unexpected_paths"])


def test_gov_does_not_accept_b02_paths_after_forced_b02_child_failure(monkeypatch) -> None:
    import tools.verify_b02_scope as b02_scope

    monkeypatch.setattr(b02_scope, "check_scope", lambda **_kwargs: {"status": "FAIL"})

    with scope_ci_diff_mode(False):
        report = gov_scope.check_scope()

    assert report["status"] == "FAIL"
    assert report["b02_scope_pass"] is False
    assert "contracts/http_contract.schema.json" in report["unexpected_paths"]


def test_verified_gov_paths_donate_nothing_after_forced_gov_failure(monkeypatch) -> None:
    monkeypatch.setattr(gov_scope, "check_scope", lambda **_kwargs: {"status": "FAIL"})

    paths, failed = gov_scope.verified_gov_paths()

    assert failed is True
    assert paths == frozenset()


def test_required_ci_runs_gov_before_historical_scope() -> None:
    workflow = (ROOT / ".github" / "workflows" / "required-ci.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.index("tools/verify_b10b_scope.py") < workflow.index(
        "tools/verify_current_main_scope.py"
    ) < workflow.index("tools/verify_b08_scope.py --current") < workflow.index(
        "tools/verify_b08_scope.py --composed"
    ) < workflow.index("tools/verify_project_status.py")


def test_required_ci_blocks_on_persona_v2_asset_contracts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "required-ci.yml").read_text(
        encoding="utf-8"
    )
    pytest_config = (ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "tests/persona" in pytest_config
    assert '"tests/persona"' in workflow


def test_required_ci_keeps_current_composition_blocking_and_legacy_duplicates_advisory() -> None:
    workflow = (ROOT / ".github" / "workflows" / "required-ci.yml").read_text(
        encoding="utf-8"
    )

    retained = (
        "tools/verify_b10b_scope.py",
        "tools/verify_current_main_scope.py",
        "tools/verify_b08_scope.py --current",
        "tools/verify_b08_scope.py --composed",
        "tools/verify_project_status.py",
    )
    for command in retained:
        assert command in workflow

    removed = (
        "tools/verify_b08_scope.py --historical-only",
        "tools/verify_b05_scope.py --historical-only",
        "tools/verify_b06_scope.py",
        "tools/verify_B07_scope.py --composed-b05",
        "tools/verify_b05_scope.py --composed",
        "tools/verify_b02_scope.py --composed-b05",
        "tools/verify_b04_scope.py --composed-b05",
        "tools/verify_B10A_scope.py --composed-b05",
        "tools/verify_p01_scope.py --composed-b05",
    )
    for command in removed:
        assert command not in workflow

    assert "Experimental advisory audits" in workflow
    assert "current-main and composed scope above stay blocking" in workflow


def test_required_ci_installer_only_scope_is_exact_and_fail_closed() -> None:
    workflow = (ROOT / ".github" / "workflows" / "required-ci.yml").read_text(
        encoding="utf-8"
    )

    exact = {"INSTALL.cmd", "START.cmd", "UNINSTALL.cmd", "docs/WINDOWS_FULL_PATCH.md"}

    def installer_only(changed: list[str]) -> bool:
        return bool(changed) and all(
            path in exact or path.startswith("installer/") or path.startswith("tests/installer/")
            for path in changed
        )

    assert installer_only(["INSTALL.cmd", "installer/Install.ps1", "tests/installer/test_windows_full_patch.py"])
    assert installer_only(["docs/WINDOWS_FULL_PATCH.md"])
    assert not installer_only(["INSTALL.cmd", "local_server.py"])
    assert not installer_only(["installer/Install.ps1", "tests/test_baseline_hardening.py"])

    for path in exact:
        assert f'"{path}"' in workflow
    assert '$relativePath -like "installer/*"' in workflow
    assert '$relativePath -like "tests/installer/*"' in workflow
    assert '"INSTALLER_ONLY_CHANGE=$($installerOnly.ToString().ToLowerInvariant())"' in workflow
    assert "if: env.INSTALLER_ONLY_CHANGE == 'true'" in workflow
    assert "if: env.PUBLIC_ONLY_CHANGE != 'true' && env.INSTALLER_ONLY_CHANGE != 'true'" in workflow
    assert "tests/installer" in workflow
    assert "baseline_hardening_scan.py --root . --mode all" in workflow


def test_required_ci_accepts_only_a_verified_exact_ancestor_rollback() -> None:
    workflow = (ROOT / ".github" / "workflows" / "required-ci.yml").read_text(
        encoding="utf-8"
    )

    command = (
        "tools/verify_b10b_scope.py --verified-ancestor-rollback "
        "--base $env:DIFF_BASE --head $env:GITHUB_SHA "
        "--target-ancestor 27d001ccd6ed17e8a39c776e03d8946631858133 "
        "--polluted-commit e4281e9068d73ec14934c208b85775337a5a751b"
    )
    assert workflow.count(command) == 2
    assert "env.VERIFIED_ANCESTOR_ROLLBACK != 'true'" in workflow
    assert "env.VERIFIED_ANCESTOR_ROLLBACK == 'true'" in workflow
    classification = workflow[: workflow.index("- name: Public-only baseline gate")]
    assert "VERIFIED_ANCESTOR_ROLLBACK=$($verifiedRollback.ToString().ToLowerInvariant())" in classification
    assert classification.rstrip().endswith("exit 0")
    rollback_step = workflow[workflow.index("- name: Verified ancestor rollback scope") :]
    assert command in rollback_step
    assert "git diff --check $env:DIFF_BASE $env:GITHUB_SHA --" in rollback_step
    assert "continue-on-error" not in rollback_step


def test_b08_composed_child_collection_keeps_all_scope_gates() -> None:
    import tools.verify_b08_scope as b08_scope

    source = inspect.getsource(b08_scope._compose_children)
    expected = {
        "b05",
        "b06",
        "b07",
        "b10a",
        "p01",
        "b10b",
        "b11",
        "gov",
        "current_main",
    }
    assert all(f'reports["{name}"]' in source for name in expected)
    assert '"b02", check_b02_scope' in source
    assert '"b04", check_b04_scope' in source


def test_required_ci_pytest_steps_are_verbose_and_bounded() -> None:
    workflow = (ROOT / ".github" / "workflows" / "required-ci.yml").read_text(
        encoding="utf-8"
    )
    requirements = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")

    assert "pytest-timeout==2.4.0 --hash=sha256:c42667e5cdadb151aeb5b26d114aff6bdf5a907f176a007a30b940d3d865b5c2" in requirements

    targeted_start = workflow.index("function Invoke-PytestNoSkip")
    full_start = workflow.index("- name: Full pytest (zero skips)")
    targeted = workflow[targeted_start:full_start]
    full = workflow[full_start:]

    for option in ("-vv", "--durations=20", "--timeout=300", "--timeout-method=thread"):
        assert option in targeted
        assert option in full
    assert '"-m", "pytest"' in targeted
    assert '"--timeout=300"' in targeted
    assert "& python -m pytest -vv --durations=20 --timeout=300 --timeout-method=thread" in full


def test_architecture_boundary_is_documented_as_blocking() -> None:
    documents = [
        (ROOT / "docs" / "ACCEPTANCE.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "DELEGATION_BOARD.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "PROJECT_MANAGEMENT.md").read_text(encoding="utf-8"),
    ]

    assert all("只组装，不自己造" in text for text in documents)
    assert "ARCH-01" in documents[0]
    assert "blocking" in documents[1]
