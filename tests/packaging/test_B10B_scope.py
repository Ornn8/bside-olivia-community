"""B10B scope verifier and current-main fail-closed composition tests."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any

import tools.verify_b10b_scope as b10b_scope

P02_01_PERSONA_PATHS = frozenset(
    {"contracts/persona_v2.schema.json", "tests/persona/test_persona_v2_schema.py"}
)
P02_01_SHARED_DEPENDENCIES = frozenset(
    {"pyproject.toml", "requirements-ci.txt", "requirements-dev.txt"}
)
P02_02_PERSONA_PATHS = frozenset(
    {
        "contracts/persona_v2_provenance.schema.json",
        "linli_character/persona_v2.json",
        "linli_character/provenance_v2.json",
        "tests/persona/test_persona_v2_assets.py",
        "tools/verify_p02_scope.py",
    }
)


def _forced_failure(**_kwargs: Any) -> dict[str, object]:
    return {"status": "FAIL", "scope_paths": []}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def test_verified_ancestor_rollback_accepts_exact_ancestor_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Scope Test")
    _git(repo, "config", "user.email", "scope@example.invalid")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "accidental.py").write_text("# pollution\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "accidental direct push")
    polluted = _git(repo, "rev-parse", "HEAD")
    (repo / "accidental.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "revert accidental direct push")
    repaired = _git(repo, "rev-parse", "HEAD")

    result = subprocess.run(
        [
            sys.executable,
            str(b10b_scope.ROOT / "tools" / "verify_b10b_scope.py"),
            "--verified-ancestor-rollback",
            "--repo-root",
            str(repo),
            "--base",
            polluted,
            "--head",
            repaired,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=PASS" in result.stdout
    assert f"matched_ancestor={baseline}" in result.stdout


def test_verified_ancestor_rollback_rejects_partial_deletion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Scope Test")
    _git(repo, "config", "user.email", "scope@example.invalid")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    (repo / "accidental-a.py").write_text("# pollution a\n", encoding="utf-8")
    (repo / "accidental-b.py").write_text("# pollution b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "accidental direct push")
    polluted = _git(repo, "rev-parse", "HEAD")
    (repo / "accidental-a.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "partial cleanup")
    partial = _git(repo, "rev-parse", "HEAD")

    result = subprocess.run(
        [
            sys.executable,
            str(b10b_scope.ROOT / "tools" / "verify_b10b_scope.py"),
            "--verified-ancestor-rollback",
            "--repo-root",
            str(repo),
            "--base",
            polluted,
            "--head",
            partial,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert "status=FAIL" in result.stdout
    assert "matched_ancestor=None" in result.stdout


def test_verified_ancestor_rollback_preserves_later_governance_changes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Scope Test")
    _git(repo, "config", "user.email", "scope@example.invalid")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "accidental.py").write_text("# pollution\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "accidental direct push")
    polluted = _git(repo, "rev-parse", "HEAD")
    (repo / "governance.txt").write_text("protected\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "protect main")
    protected_base = _git(repo, "rev-parse", "HEAD")
    (repo / "accidental.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "revert accidental direct push")
    repaired = _git(repo, "rev-parse", "HEAD")

    result = subprocess.run(
        [
            sys.executable,
            str(b10b_scope.ROOT / "tools" / "verify_b10b_scope.py"),
            "--verified-ancestor-rollback",
            "--repo-root",
            str(repo),
            "--base",
            protected_base,
            "--head",
            repaired,
            "--target-ancestor",
            baseline,
            "--polluted-commit",
            polluted,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"matched_ancestor={baseline}" in result.stdout
    assert (repo / "governance.txt").read_text(encoding="utf-8") == "protected\n"


def test_verified_ancestor_rollback_reports_git_verification_errors(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")

    result = subprocess.run(
        [
            sys.executable,
            str(b10b_scope.ROOT / "tools" / "verify_b10b_scope.py"),
            "--verified-ancestor-rollback",
            "--repo-root",
            str(repo),
            "--base",
            "missing-base",
            "--head",
            "missing-head",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 2
    assert "git verification failed" in result.stdout


def test_verified_ancestor_rollback_rejects_transient_unrelated_history(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Scope Test")
    _git(repo, "config", "user.email", "scope@example.invalid")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "accidental.py").write_text("# pollution\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "accidental direct push")
    polluted = _git(repo, "rev-parse", "HEAD")
    (repo / "transient-secret.txt").write_text("must not enter history\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "transient unrelated addition")
    (repo / "transient-secret.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "hide unrelated addition")
    (repo / "accidental.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "revert accidental direct push")
    repaired = _git(repo, "rev-parse", "HEAD")

    result = subprocess.run(
        [
            sys.executable,
            str(b10b_scope.ROOT / "tools" / "verify_b10b_scope.py"),
            "--verified-ancestor-rollback",
            "--repo-root",
            str(repo),
            "--base",
            polluted,
            "--head",
            repaired,
            "--target-ancestor",
            baseline,
            "--polluted-commit",
            polluted,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert "status=FAIL" in result.stdout
    assert "matched_ancestor=None" in result.stdout


def test_verified_ancestor_rollback_fails_closed_for_unrelated_approved_range(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Scope Test")
    _git(repo, "config", "user.email", "scope@example.invalid")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    (repo / "accidental.py").write_text("# pollution\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "accidental direct push")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "accidental.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "repair")
    head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "--orphan", "unrelated")
    (repo / "kept.txt").unlink(missing_ok=True)
    (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "unrelated baseline")
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "approved.py").write_text("# approved\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "unrelated addition")
    polluted = _git(repo, "rev-parse", "HEAD")

    result = subprocess.run(
        [
            sys.executable,
            str(b10b_scope.ROOT / "tools" / "verify_b10b_scope.py"),
            "--verified-ancestor-rollback",
            "--repo-root",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--target-ancestor",
            target,
            "--polluted-commit",
            polluted,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 2
    assert "git verification failed" in result.stdout


def test_b10b_scope_accepts_only_its_declared_paths() -> None:
    assert b10b_scope.is_b10b_path("runtime/packaging/b10b/manager.py")
    assert b10b_scope.is_b10b_path("runtime/packaging/b10b/live_bridge.py")
    assert b10b_scope.is_b10b_path("tests/packaging/test_B10B_live_memory_bridge.py")
    assert b10b_scope.is_b10b_path("tests/packaging/test_B10B_lifecycle.py")
    assert not b10b_scope.is_b10b_path("random/unrelated.txt")
    assert not b10b_scope.is_b10b_path("runtime/packaging/b10b/not-owned-random.py")
    assert not b10b_scope.is_b10b_path("tests/packaging/test_B10B_not_owned.py")


def test_p02_01_ownership_splits_persona_contracts_from_shared_dependencies() -> None:
    import tools.verify_b02_scope as b02_scope
    import tools.verify_p01_scope as p01_scope

    assert p01_scope.P02_01_EXACT == P02_01_PERSONA_PATHS
    assert P02_01_PERSONA_PATHS <= p01_scope.ALLOWED
    assert P02_01_SHARED_DEPENDENCIES <= b02_scope.B02_CONTRACT_PATHS
    assert P02_01_SHARED_DEPENDENCIES <= b02_scope.ALLOWED_MUTATIONS
    assert p01_scope.P02_01_EXACT.isdisjoint(P02_01_SHARED_DEPENDENCIES)


def test_p02_02_scope_has_a_disjoint_asset_boundary() -> None:
    import tools.verify_p01_scope as p01_scope
    import tools.verify_p02_scope as p02_scope

    assert p02_scope.P02_02_EXACT == P02_02_PERSONA_PATHS
    assert p02_scope.P02_02_SHARED == frozenset({".gitignore"})
    assert p02_scope.P02_02_EXACT.isdisjoint(p02_scope.P02_02_SHARED)
    assert p02_scope.P02_02_SHARED <= p01_scope.ALLOWED


def test_p02_02_child_uses_valid_ci_diff_without_revalidating_merged_siblings(
    monkeypatch: Any,
) -> None:
    import tools.verify_p02_scope as p02_scope

    ci_base = "ci-base"
    head = "head"
    seen_diffs: list[tuple[str, ...]] = []
    monkeypatch.setenv("DIFF_BASE", ci_base)

    def fake_git(*args: str) -> list[str]:
        if args[:1] == ("rev-parse",):
            return [args[1]]
        if args[:1] == ("merge-base",):
            return [args[1]]
        if args[:2] == ("diff", "--name-only"):
            seen_diffs.append(args)
            if args[2] == p02_scope.P02_02_BASELINE:
                return ["tests/tts/test_external_adapter.py"]
            return ["linli_character/persona_v2.json"]
        if args[:2] == ("status", "--short"):
            return []
        return []

    monkeypatch.setattr(p02_scope, "_git", fake_git)

    report = p02_scope.check_scope(head=head, child_mode=True)

    assert report["status"] == "PASS"
    assert report["comparison_base"] == ci_base
    assert ("diff", "--name-only", ci_base, head, "--") in seen_diffs
    assert not any(args[2] == p02_scope.P02_02_BASELINE for args in seen_diffs)


def test_b10b_scope_fails_closed_on_unrelated_dirty(monkeypatch: Any) -> None:
    def fake_git(*args: str) -> list[str]:
        if args[:2] == ("rev-parse", "HEAD"):
            return ["head"]
        if args[:2] == ("rev-parse", b10b_scope.B10B_BASELINE):
            return [b10b_scope.B10B_BASELINE]
        if args[:1] == ("merge-base",):
            return [b10b_scope.B10B_BASELINE]
        if args[:2] == ("diff", "--name-only"):
            return ["runtime/packaging/b10b/manager.py"]
        return []

    monkeypatch.setattr(b10b_scope, "_git", fake_git)
    monkeypatch.setattr(b10b_scope, "_status_paths", lambda: ["random/unrelated.txt"])
    report = b10b_scope.check_scope()
    assert report["status"] == "FAIL"
    assert report["unexpected_paths"] == ["random/unrelated.txt"]


def test_b10b_child_mode_does_not_reenter_b08(monkeypatch: Any) -> None:
    import tools.verify_b08_scope as b08_scope

    def fake_git(*args: str) -> list[str]:
        if args[:2] == ("rev-parse", "HEAD"):
            return ["head"]
        if args[:2] == ("rev-parse", b10b_scope.B10B_BASELINE):
            return [b10b_scope.B10B_BASELINE]
        if args[:1] == ("merge-base",):
            return [b10b_scope.B10B_BASELINE]
        if args[:2] == ("diff", "--name-only"):
            return ["runtime/packaging/b10b/manager.py"]
        return []

    monkeypatch.setattr(b10b_scope, "_git", fake_git)
    monkeypatch.setattr(b10b_scope, "_status_paths", lambda: [])
    monkeypatch.setattr(
        b08_scope,
        "check_scope",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("recursive B08 call")),
    )
    report = b10b_scope.check_scope(child_mode=True)

    assert report["status"] == "PASS", report
    assert report["b08_scope_pass"] is True


def _stub_b10b_later_children(monkeypatch: Any) -> None:
    import tools.verify_B07_scope as b07_scope
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_b05_scope as b05_scope
    import tools.verify_b06_scope as b06_scope
    import tools.verify_b08_scope as b08_scope
    import tools.verify_gov_scope as gov_scope
    import tools.verify_p01_scope as p01_scope
    import tools.verify_p02_scope as p02_scope
    import tools.scope_compat as compat

    for module, name in (
        (b05_scope, "current_b05_paths"),
        (b06_scope, "current_b06_paths"),
        (b07_scope, "current_b07_paths"),
        (b08_scope, "current_b08_paths"),
        (b10a_scope, "current_b10a_paths"),
        (gov_scope, "current_gov_paths"),
        (p01_scope, "current_p01_paths"),
        (p02_scope, "current_p02_paths"),
        (compat, "current_b11_paths"),
    ):
        monkeypatch.setattr(module, name, lambda: frozenset())
    monkeypatch.setattr(b10a_scope, "ALLOWED_EXACT", frozenset())
    for module in (
        b05_scope,
        b06_scope,
        b07_scope,
        b08_scope,
        gov_scope,
        b10a_scope,
        p01_scope,
        p02_scope,
    ):
        monkeypatch.setattr(module, "check_scope", lambda **_kwargs: {"status": "PASS"})


def test_b10b_donates_no_p02_01_paths_after_forced_p01_child_failure(
    monkeypatch: Any,
) -> None:
    import tools.scope_compat as compat
    import tools.verify_p01_scope as p01_scope

    _stub_b10b_later_children(monkeypatch)
    monkeypatch.setattr(p01_scope, "current_p01_paths", lambda: P02_01_PERSONA_PATHS)
    monkeypatch.setattr(
        p01_scope,
        "check_scope",
        lambda **_kwargs: {"status": "FAIL", "scope_paths": sorted(P02_01_PERSONA_PATHS)},
    )
    monkeypatch.setattr(
        compat,
        "verified_b02_paths",
        lambda _excluded: (P02_01_SHARED_DEPENDENCIES, False),
    )
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))

    donated, failed = b10b_scope._verified_later_paths(frozenset())

    assert failed is True
    assert donated == frozenset()


def test_b10b_later_paths_donate_verified_b02_and_p01_paths(monkeypatch: Any) -> None:
    import tools.scope_compat as compat
    import tools.verify_p01_scope as p01_scope

    _stub_b10b_later_children(monkeypatch)
    monkeypatch.setattr(p01_scope, "current_p01_paths", lambda: P02_01_PERSONA_PATHS)
    calls: list[str] = []

    def passing_b02(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
        calls.append("b02")
        assert P02_01_PERSONA_PATHS <= excluded
        return P02_01_SHARED_DEPENDENCIES, False

    def passing_p01(**kwargs: object) -> dict[str, object]:
        calls.append("p01")
        assert kwargs["child_mode"] is True
        return {"status": "PASS"}

    monkeypatch.setattr(compat, "verified_b02_paths", passing_b02)
    monkeypatch.setattr(p01_scope, "check_scope", passing_p01)
    seen_b11_exclusions: list[frozenset[str]] = []
    monkeypatch.setattr(
        compat,
        "verified_b11_paths",
        lambda excluded: (seen_b11_exclusions.append(excluded) or (frozenset(), False)),
    )

    donated, failed = b10b_scope._verified_later_paths(frozenset())

    assert failed is False
    assert P02_01_PERSONA_PATHS | P02_01_SHARED_DEPENDENCIES <= donated
    assert calls == ["b02", "p01"]
    assert P02_01_SHARED_DEPENDENCIES <= seen_b11_exclusions[0]


def test_b10b_later_paths_donate_verified_p02_02_paths(monkeypatch: Any) -> None:
    import tools.scope_compat as compat
    import tools.verify_p02_scope as p02_scope

    _stub_b10b_later_children(monkeypatch)
    monkeypatch.setattr(p02_scope, "current_p02_paths", lambda: P02_02_PERSONA_PATHS)
    monkeypatch.setattr(
        p02_scope,
        "check_scope",
        lambda **_kwargs: {"status": "PASS", "scope_paths": sorted(P02_02_PERSONA_PATHS)},
    )
    monkeypatch.setattr(
        compat,
        "verified_b02_paths",
        lambda _excluded: (P02_01_SHARED_DEPENDENCIES, False),
    )
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))

    donated, failed = b10b_scope._verified_later_paths(frozenset())

    assert failed is False
    assert P02_02_PERSONA_PATHS <= donated


def test_b10b_later_paths_donate_no_p02_02_paths_after_forced_p02_02_failure(
    monkeypatch: Any,
) -> None:
    import tools.scope_compat as compat
    import tools.verify_p02_scope as p02_scope

    _stub_b10b_later_children(monkeypatch)
    monkeypatch.setattr(p02_scope, "current_p02_paths", lambda: P02_02_PERSONA_PATHS)
    monkeypatch.setattr(
        p02_scope,
        "check_scope",
        lambda **_kwargs: {"status": "FAIL", "scope_paths": sorted(P02_02_PERSONA_PATHS)},
    )
    monkeypatch.setattr(
        compat,
        "verified_b02_paths",
        lambda _excluded: (P02_01_SHARED_DEPENDENCIES, False),
    )
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))

    donated, failed = b10b_scope._verified_later_paths(frozenset())

    assert failed is True
    assert donated == frozenset()


def test_b10b_later_path_donates_no_b02_paths_after_forced_failure(monkeypatch: Any) -> None:
    import tools.scope_compat as compat

    _stub_b10b_later_children(monkeypatch)
    candidate = frozenset({"installer/start_local.py"})
    monkeypatch.setattr(compat, "verified_b02_paths", lambda _excluded: (candidate, True))

    donated, failed = b10b_scope._verified_later_paths(frozenset())

    assert failed is True
    assert donated == frozenset()
    assert candidate.isdisjoint(donated)


def test_current_main_composition_does_not_exclude_b10b_when_child_fails(
    monkeypatch: Any,
) -> None:
    import tools.verify_current_main_scope as current_main

    failing = {"status": "FAIL", "unexpected_paths": ["runtime/packaging/b10b/manager.py"]}
    monkeypatch.setattr(current_main, "_b10b_report", lambda: failing)
    monkeypatch.setattr(current_main, "_historical_reports", lambda _excluded: {
        "b05": {"status": "PASS"},
    })
    report = current_main.check_scope()
    assert report["status"] == "FAIL"
    assert report["excluded_b10b_paths"] == []
    assert report["b10b"]["status"] == "FAIL"


def test_current_main_composition_propagates_historical_child_failure(
    monkeypatch: Any,
) -> None:
    import tools.verify_b08_scope as b08_scope
    import tools.verify_current_main_scope as current_main

    passing = {"status": "PASS", "scope_paths": ["runtime/packaging/b10b/manager.py"]}
    monkeypatch.setattr(current_main, "_b10b_report", lambda: passing)
    monkeypatch.setattr(current_main, "_b11_report", lambda: {"status": "PASS", "scope_paths": []})
    monkeypatch.setattr(current_main, "_gov_report", lambda _excluded: {"status": "PASS", "scope_paths": [], "b08_paths": []})
    seen: dict[str, object] = {}

    def historical_children(_paths: frozenset[str], **kwargs: object) -> dict[str, dict[str, object]]:
        seen.update(kwargs)
        return {"b05": {"status": "FAIL", "unexpected_paths": ["unrelated.txt"]}}

    monkeypatch.setattr(b08_scope, "_compose_children", historical_children)
    report = current_main.check_scope()

    assert report["status"] == "PASS"
    assert report["excluded_b10b_paths"] == ["runtime/packaging/b10b/manager.py"]
    assert report["historical_pass"] is False
    assert report["historical_advisory"] is True
    assert seen == {"historical_mutual": True, "include_current_main": False}


def test_current_main_child_mode_keeps_historical_advisory_but_blocks_current_failure(
    monkeypatch: Any,
) -> None:
    import tools.scope_compat as compat
    import tools.verify_B07_scope as b07_scope
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_b05_scope as b05_scope
    import tools.verify_b06_scope as b06_scope
    import tools.verify_b08_scope as b08_scope
    import tools.verify_gov_scope as gov_scope
    import tools.verify_current_main_scope as current_main
    import tools.verify_p01_scope as p01_scope
    import tools.verify_p02_scope as p02_scope

    for module, name in (
        (b05_scope, "current_b05_paths"),
        (b06_scope, "current_b06_paths"),
        (b07_scope, "current_b07_paths"),
        (b08_scope, "current_b08_paths"),
        (b10a_scope, "current_b10a_paths"),
        (b10b_scope, "current_b10b_paths"),
        (gov_scope, "current_gov_paths"),
        (p02_scope, "current_p02_paths"),
        (compat, "current_b11_paths"),
    ):
        monkeypatch.setattr(module, name, lambda: frozenset())
    candidate_p01 = frozenset({"p01.py"})
    monkeypatch.setattr(p01_scope, "current_p01_paths", lambda: candidate_p01)
    b02_seen: list[frozenset[str]] = []
    monkeypatch.setattr(
        compat,
        "verified_b02_paths",
        lambda excluded: (b02_seen.append(excluded) or (frozenset(), False)),
    )
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))
    monkeypatch.setattr(
        b10b_scope,
        "check_scope",
        lambda **_kwargs: {"status": "PASS", "scope_paths": []},
    )
    monkeypatch.setattr(
        gov_scope,
        "check_scope",
        lambda **_kwargs: {"status": "PASS", "scope_paths": []},
    )
    monkeypatch.setattr(
        b08_scope,
        "check_scope",
        lambda **_kwargs: {"status": "PASS", "scope_paths": []},
    )
    monkeypatch.setattr(p01_scope, "check_scope", lambda **_kwargs: {"status": "PASS"})
    monkeypatch.setattr(p02_scope, "check_scope", lambda **_kwargs: {"status": "PASS"})
    monkeypatch.setattr(
        current_main,
        "_historical_reports",
        lambda _excluded: {"b05": {"status": "FAIL"}},
    )

    report = current_main.check_scope(child_mode=True)
    assert report["status"] == "PASS"
    assert report["p01"] == {"status": "PASS", "scope_paths": ["p01.py"]}
    assert candidate_p01 <= b02_seen[0]
    assert report["historical_pass"] is False
    assert report["historical_advisory"] is True

    monkeypatch.setattr(p01_scope, "check_scope", _forced_failure)
    p01_blocked = current_main.check_scope(child_mode=True)
    assert p01_blocked["status"] == "FAIL"
    assert p01_blocked["p01"]["scope_paths"] == []

    monkeypatch.setattr(p01_scope, "check_scope", lambda **_kwargs: {"status": "PASS"})
    monkeypatch.setattr(
        b08_scope,
        "check_scope",
        lambda **_kwargs: {"status": "FAIL", "scope_paths": []},
    )
    blocked = current_main.check_scope(child_mode=True)
    assert blocked["status"] == "FAIL"
    assert blocked["b08"]["status"] == "FAIL"


def test_current_main_child_mode_donates_verified_p02_02_paths(
    monkeypatch: Any,
) -> None:
    import tools.scope_compat as compat
    import tools.verify_B07_scope as b07_scope
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_b05_scope as b05_scope
    import tools.verify_b06_scope as b06_scope
    import tools.verify_b08_scope as b08_scope
    import tools.verify_b10b_scope as b10b_scope
    import tools.verify_current_main_scope as current_main
    import tools.verify_gov_scope as gov_scope
    import tools.verify_p01_scope as p01_scope
    import tools.verify_p02_scope as p02_scope

    for module, name in (
        (b05_scope, "current_b05_paths"),
        (b06_scope, "current_b06_paths"),
        (b07_scope, "current_b07_paths"),
        (b08_scope, "current_b08_paths"),
        (b10a_scope, "current_b10a_paths"),
        (b10b_scope, "current_b10b_paths"),
        (gov_scope, "current_gov_paths"),
        (p01_scope, "current_p01_paths"),
        (compat, "current_b11_paths"),
    ):
        monkeypatch.setattr(module, name, lambda: frozenset())
    monkeypatch.setattr(current_main, "_current_p02_paths", lambda: P02_02_PERSONA_PATHS)
    monkeypatch.setattr(p02_scope, "check_scope", lambda **_kwargs: {"status": "PASS"})
    monkeypatch.setattr(compat, "verified_b02_paths", lambda _excluded: (frozenset(), False))
    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), False))
    monkeypatch.setattr(p01_scope, "check_scope", lambda **_kwargs: {"status": "PASS"})
    for module in (b08_scope, b10b_scope, gov_scope):
        monkeypatch.setattr(module, "check_scope", lambda **_kwargs: {"status": "PASS"})
    monkeypatch.setattr(current_main, "_historical_reports", lambda _excluded: {})

    report = current_main.check_scope(child_mode=True)

    assert report["status"] == "PASS"
    assert report["p02"]["scope_paths"] == sorted(P02_02_PERSONA_PATHS)


def test_current_main_donates_b11_paths_only_after_current_verified_child(
    monkeypatch: Any,
) -> None:
    import os

    import tools.scope_compat as compat
    import tools.verify_current_main_scope as current_main

    comparison_base = "91c2e715f6823dcf6dad912cca062afdee573f99"
    monkeypatch.setenv("DIFF_BASE", comparison_base)
    monkeypatch.setattr(
        current_main,
        "_b10b_report",
        lambda: {"status": "PASS", "scope_paths": []},
    )
    monkeypatch.setattr(
        current_main,
        "_gov_report",
        lambda _excluded: {"status": "PASS", "scope_paths": [], "b08_paths": []},
    )
    seen: list[frozenset[str]] = []
    monkeypatch.setattr(
        current_main,
        "_historical_reports",
        lambda excluded: (seen.append(excluded) or {"b05": {"status": "PASS"}}),
    )
    child_exclusions = frozenset({"tests/packaging/test_B10B_scope.py"})
    b02_paths = frozenset({"contracts/http_contract.schema.json"})
    monkeypatch.setattr(compat, "b11_child_exclusions", lambda: child_exclusions)
    monkeypatch.setattr(compat, "verified_b02_paths", lambda _excluded: (b02_paths, False))

    def passing_b11(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
        assert os.environ["DIFF_BASE"] == comparison_base
        assert excluded == child_exclusions | b02_paths | P02_02_PERSONA_PATHS
        return frozenset({"runtime/visual/livetalking.py"}), False

    monkeypatch.setattr(compat, "verified_b11_paths", passing_b11)
    passing = current_main.check_scope()
    assert passing["status"] == "PASS"
    assert "runtime/visual/livetalking.py" in seen[-1]

    monkeypatch.setattr(compat, "verified_b11_paths", lambda _excluded: (frozenset(), True))
    failing = current_main.check_scope()
    assert failing["status"] == "FAIL"
    assert failing["b11"]["scope_paths"] == []
    assert "runtime/visual/livetalking.py" not in seen[-1]

    monkeypatch.setattr(compat, "verified_b02_paths", lambda _excluded: (frozenset(), True))
    b02_failing = current_main.check_scope()
    assert b02_failing["status"] == "FAIL"
    assert b02_failing["b11"]["scope_paths"] == []


def test_current_main_b02_failure_donates_no_paths(monkeypatch: Any) -> None:
    import tools.scope_compat as compat
    import tools.verify_current_main_scope as current_main

    monkeypatch.setattr(compat, "b11_child_exclusions", lambda: frozenset())
    monkeypatch.setattr(compat, "verified_b02_paths", lambda _excluded: (frozenset(), True))

    report = current_main._b11_report()

    assert report["status"] == "FAIL"
    assert report["b02_scope_pass"] is False
    assert report["scope_paths"] == []


def test_current_main_b11_excludes_verified_p02_02_paths(monkeypatch: Any) -> None:
    import tools.scope_compat as compat
    import tools.verify_current_main_scope as current_main

    monkeypatch.setattr(compat, "b11_child_exclusions", lambda: frozenset())
    monkeypatch.setattr(current_main, "_current_p02_paths", lambda: P02_02_PERSONA_PATHS)
    monkeypatch.setattr(compat, "verified_b02_paths", lambda _excluded: (frozenset(), False))
    seen: list[frozenset[str]] = []
    monkeypatch.setattr(
        compat,
        "verified_b11_paths",
        lambda excluded: (seen.append(excluded) or (frozenset(), False)),
    )

    report = current_main._b11_report()

    assert report["status"] == "PASS"
    assert seen == [P02_02_PERSONA_PATHS]


def test_current_main_gov_wrapper_keeps_siblings_but_never_forwards_gov_owned_paths(
    monkeypatch: Any,
) -> None:
    import tools.verify_current_main_scope as current_main
    import tools.verify_gov_scope as gov_scope

    seen: list[frozenset[str]] = []
    monkeypatch.setattr(
        gov_scope,
        "check_scope",
        lambda *, excluded: (seen.append(excluded) or {"status": "PASS"}),
    )

    report = current_main._gov_report(
        frozenset({"runtime/visual/livetalking.py", *gov_scope.GOV_PATHS})
    )

    assert report["status"] == "PASS"
    assert seen == [frozenset({"runtime/visual/livetalking.py"})]


def test_current_main_child_mode_propagates_forced_b10b_failure(monkeypatch: Any) -> None:
    import tools.verify_current_main_scope as current_main

    monkeypatch.setattr(b10b_scope, "check_scope", _forced_failure)
    report = current_main.check_scope(child_mode=True)

    assert report["status"] == "FAIL"
    assert report["b10b"]["status"] == "FAIL"


def test_current_b10a_paths_are_derived_only_from_exact_owned_boundary() -> None:
    import tools.verify_B10A_scope as b10a_scope
    from tools.scope_compat import scope_ci_diff_mode

    with scope_ci_diff_mode(False):
        paths = b10a_scope.current_b10a_paths()
    assert "runtime/packaging/b10a/manager.py" in paths
    assert "asr/management.py" not in paths
    assert "docs/PROJECT_MANAGEMENT.md" not in paths


def test_b11_historical_b10a_child_uses_the_outer_comparison_base(
    monkeypatch: Any,
) -> None:
    import tools.check_b11_docs as b11_docs
    import tools.verify_B07_scope as b07_scope
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_b02_scope as b02_scope
    import tools.verify_b04_scope as b04_scope
    import tools.verify_b05_scope as b05_scope
    import tools.verify_b06_scope as b06_scope
    import tools.verify_b08_scope as b08_scope
    import tools.verify_b10b_scope as b10b_scope_module
    import tools.verify_b11_scope as b11_scope
    import tools.verify_gov_scope as gov_scope
    import tools.verify_p01_scope as p01_scope

    owned = "runtime/packaging/b10a/manager.py"
    observed: dict[str, object] = {}
    monkeypatch.setattr(b11_docs, "verified_b11_paths", lambda: (frozenset(), False))
    for module, current_name in (
        (b05_scope, "current_b05_paths"),
        (b06_scope, "current_b06_paths"),
        (b08_scope, "current_b08_paths"),
        (b07_scope, "current_b07_paths"),
        (b10b_scope_module, "current_b10b_paths"),
        (gov_scope, "current_gov_paths"),
        (p01_scope, "current_p01_paths"),
    ):
        monkeypatch.setattr(module, current_name, lambda: frozenset())
    for module in (b02_scope, b04_scope, b05_scope, b06_scope, b07_scope, b08_scope, b10b_scope_module, gov_scope, p01_scope):
        monkeypatch.setattr(module, "check_scope", lambda **_kwargs: {"status": "PASS"})

    def current_b10a_paths(*, comparison_base: str | None = None) -> frozenset[str]:
        observed["candidate_base"] = comparison_base
        return frozenset({owned})

    def check_b10a(**kwargs: object) -> dict[str, object]:
        observed["child_base"] = kwargs.get("comparison_base")
        return {"status": "PASS"}

    monkeypatch.setattr(b10a_scope, "current_b10a_paths", current_b10a_paths)
    monkeypatch.setattr(b10a_scope, "check_scope", check_b10a)
    trusted, failed, reports = b11_scope._compose_native_children(
        frozenset(), frozenset(), comparison_base="historical-base"
    )

    assert failed is False
    assert reports["b10a"]["status"] == "PASS"
    assert observed == {"candidate_base": "historical-base", "child_base": "historical-base"}
    assert owned in trusted


def test_b10a_does_not_exclude_b05_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_B10A_scope as b10a_scope
    import tools.verify_b05_scope as b05_scope

    monkeypatch.setattr(b05_scope, "check_scope", _forced_failure)
    from tools.scope_compat import scope_ci_diff_mode

    with scope_ci_diff_mode(False):
        report = b10a_scope.check_scope()
    assert report["status"] == "FAIL"
    assert "asr/management.py" in report["unexpected_paths"]


def test_b10a_does_not_donate_b07_paths_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_B07_scope as b07_scope
    import tools.verify_B10A_scope as b10a_scope

    monkeypatch.setattr(
        b07_scope,
        "check_scope",
        lambda **_kwargs: {
            "status": "FAIL",
            "scope_paths": ["visual_driver/contracts.py"],
        },
    )
    from tools.scope_compat import scope_ci_diff_mode

    with scope_ci_diff_mode(False):
        report = b10a_scope.check_scope()
    assert report["status"] == "FAIL"
    assert "visual_driver/contracts.py" in report["unexpected_paths"]


def test_b04_does_not_exclude_b05_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_b04_scope as b04_scope
    import tools.verify_b05_scope as b05_scope

    monkeypatch.setattr(b05_scope, "check_scope", _forced_failure)
    from tools.scope_compat import scope_ci_diff_mode

    with scope_ci_diff_mode(False):
        report = b04_scope.check_scope()
    assert report["status"] == "FAIL"
    assert "asr/management.py" in report["unexpected_paths"]


def test_b04_does_not_donate_b07_paths_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_B07_scope as b07_scope
    import tools.verify_b04_scope as b04_scope

    monkeypatch.setattr(
        b07_scope,
        "check_scope",
        lambda **_kwargs: {"status": "FAIL", "scope_paths": ["tools/visual_driver.py"]},
    )

    paths, failed = b04_scope._verified_b07_paths(frozenset())

    assert failed is True
    assert paths == frozenset()


def test_b04_historical_gate_uses_fixed_base_when_ci_diff_is_absent_or_invalid(
    monkeypatch: Any,
) -> None:
    import tools.verify_b04_scope as b04_scope

    for diff_base in (None, "HEAD", "not-a-revision"):
        if diff_base is None:
            monkeypatch.delenv("DIFF_BASE", raising=False)
        else:
            monkeypatch.setenv("DIFF_BASE", diff_base)
        seen: list[tuple[str, ...]] = []

        def fake_git(*args: str) -> tuple[str, ...]:
            if args[:2] == ("diff", "--name-only"):
                seen.append(args)
                return ("outside-b04.py",)
            if args[:2] == ("status", "--short"):
                return ()
            return ()

        monkeypatch.setattr(b04_scope, "_git", fake_git)
        report = b04_scope.check_scope(frozenset(), child_mode=True)

        assert report["status"] == "FAIL"
        assert report["unexpected_paths"] == ["outside-b04.py"]
        assert seen == [("diff", "--name-only", b04_scope.B04_BASELINE, "HEAD", "--")]


def test_b10a_historical_gate_uses_fixed_base_when_ci_diff_is_absent_or_invalid(
    monkeypatch: Any,
) -> None:
    import tools.verify_B10A_scope as b10a_scope

    for diff_base in (None, "HEAD", "not-a-revision"):
        if diff_base is None:
            monkeypatch.delenv("DIFF_BASE", raising=False)
        else:
            monkeypatch.setenv("DIFF_BASE", diff_base)
        seen: list[tuple[str, ...]] = []

        def fake_git(*args: str) -> str:
            if args[:2] == ("diff", "--name-only"):
                seen.append(args)
                return "outside-b10a.py\n"
            return ""

        monkeypatch.setattr(b10a_scope, "_git", fake_git)
        report = b10a_scope.check_scope(frozenset(), child_mode=True)

        assert report["status"] == "FAIL"
        assert report["unexpected_paths"] == ["outside-b10a.py"]
        assert seen == [("diff", "--name-only", b10a_scope.B10A_BASELINE, "HEAD", "--")]


def test_b06_does_not_exclude_b10b_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_b06_scope as b06_scope

    monkeypatch.setattr(b10b_scope, "check_scope", _forced_failure)
    report = b06_scope.check_scope()
    assert report["status"] == "FAIL"
    assert "runtime/packaging/b10b/manager.py" in report["unexpected_paths"]


def test_b02_does_not_exclude_b10b_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_b02_scope as b02_scope

    monkeypatch.setattr(b10b_scope, "check_scope", _forced_failure)
    report = b02_scope.check_scope()
    assert report["status"] == "FAIL"
    assert "runtime/packaging/b10b/manager.py" not in report["excluded_paths"]


def test_p01_does_not_exclude_b10b_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_p01_scope as p01_scope

    monkeypatch.setattr(b10b_scope, "check_scope", _forced_failure)
    report = p01_scope.check_scope()
    assert report["status"] == "FAIL"
    assert "runtime/packaging/b10b/manager.py" not in report["excluded_paths"]


def test_b10b_does_not_exclude_b10a_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.verify_B10A_scope as b10a_scope
    from tools.scope_compat import scope_ci_diff_mode

    monkeypatch.setattr(b10a_scope, "check_scope", _forced_failure)
    with scope_ci_diff_mode(False):
        report = b10b_scope.check_scope()

    assert report["status"] == "FAIL"
    assert "runtime/packaging/b10a/manager.py" not in report["excluded_paths"]
    assert "runtime/packaging/b10a/manager.py" in report["unexpected_paths"]


def test_optional_b11_legacy_signature_is_not_treated_as_a_pass(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    import tools.scope_compat as compat

    monkeypatch.setattr(
        compat,
        "_b11_module",
        lambda: SimpleNamespace(
            check_scope=lambda: {"status": "PASS", "scope_paths": ["visual/owned.py"]}
        ),
    )

    paths, failed = compat.verified_b11_paths(frozenset({"asr/owned.py"}))

    assert failed is True
    assert paths == frozenset()


def test_optional_b11_excludes_owned_paths_only_after_child_pass(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    import tools.scope_compat as compat

    monkeypatch.setenv("DIFF_BASE", "91c2e715f6823dcf6dad912cca062afdee573f99")

    def passing_child(*, base: str, excluded: frozenset[str], child_mode: bool) -> dict[str, object]:
        assert base == "91c2e715f6823dcf6dad912cca062afdee573f99"
        assert child_mode is True
        assert "asr/owned.py" in excluded
        return {"status": "PASS", "scope_paths": ["visual/owned.py"]}

    monkeypatch.setattr(
        compat,
        "_b11_module",
        lambda: SimpleNamespace(
            B11_BASELINE="8434545de00240dad9cdee2fb91fe57616c875af",
            check_scope=passing_child,
        ),
    )

    paths, failed = compat.verified_b11_paths(frozenset({"asr/owned.py"}))

    assert failed is False
    from tools.check_b11_docs import B11_OWNED_PATHS

    assert paths == frozenset({"visual/owned.py"}) | B11_OWNED_PATHS


def test_optional_b11_forced_failure_never_returns_candidate_paths(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    import tools.scope_compat as compat

    def failing_child(*, excluded: frozenset[str], child_mode: bool) -> dict[str, object]:
        return {
            "status": "FAIL",
            "scope_paths": ["visual/owned.py"],
            "unexpected_paths": ["random/unrelated.txt"],
        }

    monkeypatch.setattr(
        compat,
        "_b11_module",
        lambda: SimpleNamespace(check_scope=failing_child),
    )

    paths, failed = compat.verified_b11_paths()

    assert failed is True
    assert paths == frozenset()


def test_b10b_does_not_exclude_b11_after_forced_child_failure(monkeypatch: Any) -> None:
    import tools.scope_compat as compat

    monkeypatch.setattr(
        compat,
        "verified_b11_paths",
        lambda _excluded: (frozenset(), True),
    )

    with compat.scope_ci_diff_mode(False):
        report = b10b_scope.check_scope()

    assert report["status"] == "FAIL"
    assert "tools/verify_b11_scope.py" in report["unexpected_paths"]
