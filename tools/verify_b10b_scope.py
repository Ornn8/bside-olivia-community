"""Independent fail-closed verifier for the B10B current-main composition."""

from __future__ import annotations

import argparse
from functools import lru_cache
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

B10B_BASELINE = "44b88e9fd9d2f5ef1ee2060bca89b4427f829d0b"
B10B_EXACT = frozenset(
    {
        ".github/workflows/required-ci.yml",
        "docs/B10B_MODULE_LIFECYCLE.md",
        "runtime/packaging/manifests/b10b.modules.json",
        "runtime/packaging/schemas/b10b.modules.schema.json",
        "runtime/packaging/b10b/__init__.py",
        "runtime/packaging/b10b/__main__.py",
        "runtime/packaging/b10b/cli.py",
        "runtime/packaging/b10b/errors.py",
        "runtime/packaging/b10b/extensions.py",
        "runtime/packaging/b10b/live_bridge.py",
        "runtime/packaging/b10b/manager.py",
        "runtime/packaging/b10b/manifest.py",
        "runtime/packaging/b10b/profiles.py",
        "runtime/packaging/b10b/schemas/b10b.state.schema.json",
        "runtime/packaging/b10b/security.py",
        "tests/governance/test_gov_scope.py",
        "tests/packaging/test_B10B_cli.py",
        "tests/packaging/test_B10B_lifecycle.py",
        "tests/packaging/test_B10B_live_bridge.py",
        "tests/packaging/test_B10B_live_memory_bridge.py",
        "tests/packaging/test_B10B_live_launcher.py",
        "tests/packaging/test_B10B_scope.py",
        "tools/B10B_cli.py",
        "tools/verify_B07_scope.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_b04_scope.py",
        "tools/verify_b05_scope.py",
        "tools/verify_b06_scope.py",
        "tools/verify_p01_scope.py",
        "tools/b10b_lifecycle_evidence.py",
        "tools/live_b10b.py",
        "tools/verify_b10b_scope.py",
        "tools/verify_current_main_scope.py",
    }
)
B10B_PREFIXES: tuple[str, ...] = ()


def is_b10b_path(path: str) -> bool:
    return path in B10B_EXACT


@lru_cache(maxsize=None)
def _git(*args: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def _status_paths() -> list[str]:
    paths: list[str] = []
    for line in _git("status", "--short", "--untracked-files=all"):
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        paths.append(path)
    return paths


def _repo_git(repo_root: Path, *args: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def verify_ancestor_rollback(
    *,
    repo_root: Path,
    base: str,
    head: str,
    target_ancestor: str | None = None,
    polluted_commit: str | None = None,
) -> dict[str, Any]:
    """Verify a deletion-only repair against an exact approved addition range."""

    try:
        root = Path(repo_root).resolve(strict=True)
        base_commit = _repo_git(root, "rev-parse", f"{base}^{{commit}}")[0]
        head_commit = _repo_git(root, "rev-parse", f"{head}^{{commit}}")[0]
        merge_base = _repo_git(root, "merge-base", base_commit, head_commit)[0]
        changes = _repo_git(root, "diff", "--name-status", base_commit, head_commit, "--")
        head_tree = _repo_git(root, "rev-parse", f"{head_commit}^{{tree}}")[0]
        history = _repo_git(root, "log", "--format=%H%x09%T", base_commit)
        target_commit = (
            _repo_git(root, "rev-parse", f"{target_ancestor}^{{commit}}")[0]
            if target_ancestor is not None
            else None
        )
        polluted = (
            _repo_git(root, "rev-parse", f"{polluted_commit}^{{commit}}")[0]
            if polluted_commit is not None
            else None
        )
    except (IndexError, OSError, subprocess.CalledProcessError) as exc:
        return {
            "status": "FAIL",
            "error": f"git verification failed: {type(exc).__name__}",
            "base": None,
            "head": None,
            "base_is_ancestor": False,
            "matched_ancestor": None,
            "deleted_paths": [],
        }

    deleted_paths: list[str] = []
    deletion_only = bool(changes)
    for line in changes:
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] != "D" or not fields[1]:
            deletion_only = False
            break
        deleted_paths.append(fields[1])
    matched_ancestor = None
    verification_error = None
    history_is_deletion_only = False
    try:
        rollback_commits = _repo_git(
            root, "rev-list", "--reverse", f"{base_commit}..{head_commit}"
        )
        history_is_deletion_only = bool(rollback_commits)
        for commit in rollback_commits:
            parents = _repo_git(root, "show", "-s", "--format=%P", commit)[0].split()
            if not parents:
                history_is_deletion_only = False
                break
            commit_changes = _repo_git(
                root, "diff", "--name-status", parents[0], commit, "--"
            )
            if not commit_changes:
                history_is_deletion_only = False
                break
            for line in commit_changes:
                fields = line.split("\t")
                if (
                    len(fields) != 2
                    or fields[0] != "D"
                    or fields[1] not in deleted_paths
                ):
                    history_is_deletion_only = False
                    break
            if not history_is_deletion_only:
                break
    except (IndexError, OSError, subprocess.CalledProcessError) as exc:
        verification_error = f"git verification failed: {type(exc).__name__}"
    if target_commit is None and polluted is None:
        for line in history:
            fields = line.split("\t", 1)
            if len(fields) == 2 and fields[1] == head_tree:
                matched_ancestor = fields[0]
                break
    elif target_commit is not None and polluted is not None:
        try:
            target_to_polluted = _repo_git(
                root, "diff", "--name-status", target_commit, polluted, "--"
            )
            target_is_ancestor = (
                _repo_git(root, "merge-base", target_commit, polluted)[0] == target_commit
            )
            polluted_is_ancestor = (
                _repo_git(root, "merge-base", polluted, base_commit)[0] == polluted
            )
            added_paths: list[str] = []
            additions_only = bool(target_to_polluted)
            for line in target_to_polluted:
                fields = line.split("\t")
                if len(fields) != 2 or fields[0] != "A" or not fields[1]:
                    additions_only = False
                    break
                added_paths.append(fields[1])
            unchanged_since_pollution = not _repo_git(
                root,
                "diff",
                "--name-only",
                polluted,
                base_commit,
                "--",
                *deleted_paths,
            )
            if (
                target_is_ancestor
                and polluted_is_ancestor
                and additions_only
                and sorted(added_paths) == sorted(deleted_paths)
                and unchanged_since_pollution
                and history_is_deletion_only
            ):
                matched_ancestor = target_commit
        except (IndexError, OSError, subprocess.CalledProcessError) as exc:
            matched_ancestor = None
            verification_error = f"git verification failed: {type(exc).__name__}"
    passed = (
        merge_base == base_commit
        and deletion_only
        and history_is_deletion_only
        and matched_ancestor is not None
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "base": base_commit,
        "head": head_commit,
        "base_is_ancestor": merge_base == base_commit,
        "matched_ancestor": matched_ancestor,
        "deleted_paths": sorted(deleted_paths),
        "error": verification_error,
    }


def _current_b10b_paths() -> frozenset[str]:
    from tools.scope_compat import effective_scope_base

    committed = _git("diff", "--name-only", effective_scope_base(B10B_BASELINE), "--")
    working = _git("diff", "--name-only", "HEAD", "--")
    return frozenset(
        path for path in [*committed, *working, *_status_paths()] if is_b10b_path(path)
    )


def _verified_later_paths(
    excluded: frozenset[str] = frozenset(),
) -> tuple[frozenset[str], bool]:
    """Validate later tranche children once, without re-entering B10B."""

    from tools.verify_B07_scope import check_scope as check_b07_scope, current_b07_paths
    from tools.verify_b08_scope import check_scope as check_b08_scope, current_b08_paths
    from tools.verify_b05_scope import check_scope as check_b05_scope, current_b05_paths
    from tools.verify_b06_scope import check_scope as check_b06_scope, current_b06_paths
    from tools.verify_gov_scope import check_scope as check_gov_scope, current_gov_paths
    from tools.verify_p01_scope import check_scope as check_p01_scope, current_p01_paths
    from tools.verify_p02_scope import (
        P02_02_SHARED,
        check_scope as check_p02_scope,
        current_p02_paths,
    )
    from tools.verify_B10A_scope import ALLOWED_EXACT as B10A_ALLOWED_PATHS
    from tools.verify_B10A_scope import check_scope as check_b10a_scope
    from tools.scope_compat import current_b11_paths, verified_b02_paths, verified_b11_paths

    b05_paths = current_b05_paths()
    b06_paths = current_b06_paths()
    b07_paths = current_b07_paths()
    b08_paths = current_b08_paths()
    gov_paths = current_gov_paths()
    p01_paths = current_p01_paths()
    p02_paths = current_p02_paths()
    b10b_paths = _current_b10b_paths()
    from tools.scope_compat import effective_scope_base

    b10a_base = effective_scope_base(B10B_BASELINE)
    b10a_paths = frozenset(
        path
        for path in _git("diff", "--name-only", b10a_base, "HEAD", "--")
        if path in B10A_ALLOWED_PATHS
    )
    b11_paths = current_b11_paths()
    all_children = (
        frozenset(excluded)
        | b05_paths
        | b06_paths
        | b07_paths
        | b08_paths
        | gov_paths
        | p01_paths
        | p02_paths
        | P02_02_SHARED
        | b10b_paths
        | b10a_paths
        | b11_paths
    )
    b02_paths, b02_failed = verified_b02_paths(all_children)
    if b02_failed:
        return frozenset(), True
    all_children |= b02_paths
    b11_owned, b11_failed = verified_b11_paths(all_children - b11_paths)
    reports = (
        check_b05_scope(excluded=all_children - b05_paths, child_mode=True, compose_b07=False),
        check_b06_scope(excluded=all_children - b06_paths, child_mode=True),
        check_b07_scope(excluded=all_children - b07_paths, child_mode=True),
        check_gov_scope(excluded=all_children - gov_paths, child_mode=True),
        check_p01_scope(excluded=all_children - p01_paths, child_mode=True),
        check_p02_scope(excluded=all_children - p02_paths, child_mode=True),
        check_b08_scope(excluded=all_children - b08_paths, child_mode=True),
        check_b10a_scope(
            excluded=all_children - b10a_paths,
            child_mode=True,
            comparison_base=b10a_base,
        ),
        {"status": "FAIL"} if b11_failed else {"status": "PASS"},
    )
    if any(report.get("status") != "PASS" for report in reports):
        return frozenset(), True
    verified_b10a_paths = frozenset(reports[7].get("scope_paths", []))
    if verified_b10a_paths != b10a_paths:
        return frozenset(), True
    return (
        b05_paths
        | b06_paths
        | b07_paths
        | b08_paths
        | gov_paths
        | p01_paths
        | p02_paths
        | verified_b10a_paths
        | b02_paths
        | b11_owned
    ), False


def _failure(message: str) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "error": message,
        "base": B10B_BASELINE,
        "head": None,
        "base_is_ancestor": False,
        "changed_paths": [],
        "status_paths": [],
        "scope_paths": [],
        "unexpected_paths": [],
        "allowlist_exact": sorted(B10B_EXACT),
        "allowlist_prefixes": list(B10B_PREFIXES),
    }


def _verified_b08_paths(
    excluded: frozenset[str] = frozenset(),
) -> tuple[frozenset[str], bool]:
    """Trust B08 paths only after its independent current boundary passes."""

    try:
        from tools.verify_b08_scope import B08_BASELINE, check_scope
        from tools.verify_B07_scope import current_b07_paths
        from tools.verify_b05_scope import current_b05_paths
        from tools.verify_b06_scope import current_b06_paths

        report = check_scope(
            base=B08_BASELINE,
            head="HEAD",
            composed=False,
            child_mode=True,
            excluded=(
                frozenset(excluded)
                | current_b05_paths()
                | current_b06_paths()
                | current_b07_paths()
            ),
        )
    except Exception:
        return frozenset(), True
    if report.get("status") != "PASS":
        return frozenset(), True
    return frozenset(report.get("scope_paths", [])), False


def check_scope(
    *,
    base: str = B10B_BASELINE,
    head: str = "HEAD",
    excluded: frozenset[str] = frozenset(),
    child_mode: bool = False,
) -> dict[str, Any]:
    """Verify that all changes since the B10B baseline are B10B-owned.

    This checker deliberately has no imports from prior tranche checkers.
    A caller may therefore trust its PASS result before composing any earlier
    scope, while a dirty unrelated path remains a hard failure.
    """

    try:
        base_commit = _git("rev-parse", base)[0]
        head_commit = _git("rev-parse", head)[0]
        merge_base = _git("merge-base", base_commit, head_commit)[0]
        from tools.scope_compat import effective_scope_base

        comparison_base = _git("rev-parse", effective_scope_base(base, head, resolver=_git))[0]
        committed = _git("diff", "--name-only", comparison_base, head_commit, "--")
        working = _git("diff", "--name-only", head_commit, "--")
        status_paths = _status_paths()
    except (IndexError, OSError, subprocess.CalledProcessError) as exc:
        return _failure(f"git verification failed: {type(exc).__name__}")

    all_paths = sorted(set([*committed, *working, *status_paths]))
    if child_mode:
        later_paths, later_failed = frozenset(), False
    else:
        later_paths, later_failed = _verified_later_paths(excluded)
    excluded = frozenset(excluded) | later_paths
    scope_paths = sorted(path for path in all_paths if is_b10b_path(path))
    unexpected = sorted(
        path for path in all_paths if not is_b10b_path(path) and path not in excluded
    )
    return {
        "status": "PASS"
        if merge_base == base_commit and not unexpected and not later_failed
        else "FAIL",
        "base": base_commit,
        "head": head_commit,
        "base_is_ancestor": merge_base == base_commit,
        "changed_paths": sorted(set([*committed, *working])),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": scope_paths,
        "composed_paths": sorted(frozenset(scope_paths) | later_paths),
        "b08_paths": sorted(later_paths),
        "b08_scope_pass": not later_failed,
        "excluded_paths": sorted(later_paths),
        "child_mode": child_mode,
        "unexpected_paths": unexpected,
        "allowlist_exact": sorted(B10B_EXACT),
        "allowlist_prefixes": list(B10B_PREFIXES),
    }


def current_b10b_paths() -> frozenset[str]:
    return _current_b10b_paths()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the independent B10B scope boundary.")
    parser.add_argument("--base", default=B10B_BASELINE)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--verified-ancestor-rollback", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--target-ancestor")
    parser.add_argument("--polluted-commit")
    args = parser.parse_args(argv)
    if args.verified_ancestor_rollback:
        report = verify_ancestor_rollback(
            repo_root=args.repo_root,
            base=args.base,
            head=args.head,
            target_ancestor=args.target_ancestor,
            polluted_commit=args.polluted_commit,
        )
        display = {**report, "error": report.get("error")}
        print(
            "status={status} base={base} head={head} base_is_ancestor={base_is_ancestor} "
            "matched_ancestor={matched_ancestor} deleted_paths={deleted_paths} error={error}".format(
                **display
            )
        )
        if report.get("error") is not None:
            return 2
        return 0 if report["status"] == "PASS" else 1
    report = check_scope(base=args.base, head=args.head)
    print(
        "status={status} base={base} head={head} base_is_ancestor={base_is_ancestor} "
        "scope_paths={scope_paths} unexpected={unexpected_paths}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
