"""Verify the standalone governance-document tranche.

Prior tranche verifiers may exclude these paths only after this independent
check passes.  The governance paths are deliberately not prior allowlist
entries.
"""

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

GOV_BASELINE = "49fe48dbf85cb4a79712836f72544f10bb636468"
GOV_PATHS = frozenset(
    {
        "docs/ACCEPTANCE.md",
        "docs/DELEGATION_BOARD.md",
        "docs/PROJECT_MANAGEMENT.md",
    }
)
GOV_SUPPORT_PATHS = frozenset(
    {
        ".github/workflows/required-ci.yml",
        "baseline_hardening_scan.py",
        "docs/OPS_REQUIRED_CI.md",
        "tests/conftest.py",
        "tests/governance/test_gov_scope.py",
        "tools/verify_B07_scope.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_b04_scope.py",
        "tools/verify_b05_scope.py",
        "tools/verify_b06_scope.py",
        "tools/verify_p01_scope.py",
        "tools/verify_gov_scope.py",
    }
)
GOV_OWNED_PATHS = GOV_PATHS | GOV_SUPPORT_PATHS


def _is_b08_path(path: str) -> bool:
    try:
        from tools.verify_b08_scope import is_b08_path
    except Exception:
        return False
    return is_b08_path(path)


def _verified_b08_paths(excluded: frozenset[str] = frozenset()) -> tuple[frozenset[str], bool]:
    """Trust B08 paths only after its non-composed boundary passes."""

    try:
        from tools.verify_b08_scope import B08_BASELINE, check_scope
        from tools.verify_B07_scope import current_b07_paths
        from tools.verify_b05_scope import current_b05_paths
        from tools.verify_b06_scope import current_b06_paths
        gov_paths = current_gov_paths()

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
                | gov_paths
            ),
        )
    except Exception:
        return frozenset(), True
    if report.get("status") != "PASS":
        return frozenset(), True
    return frozenset(report.get("scope_paths", [])), False


def _verified_b10b_paths(excluded: frozenset[str] = frozenset()) -> tuple[frozenset[str], bool]:
    """Trust B10B paths only after its verifier has validated B08 exclusions."""

    try:
        from tools.verify_b10b_scope import check_scope as check_b10b_scope
        from tools.verify_B07_scope import current_b07_paths
        from tools.verify_b08_scope import current_b08_paths
        from tools.verify_b05_scope import current_b05_paths
        from tools.verify_b06_scope import current_b06_paths

        report = check_b10b_scope(
            child_mode=True,
            excluded=(
                frozenset(excluded)
                | current_b05_paths()
                | current_b06_paths()
                | current_b07_paths()
                | current_b08_paths()
                | current_gov_paths()
            ),
        )
    except Exception:
        return frozenset(), True
    if report.get("status") != "PASS":
        return frozenset(), True
    return frozenset(report.get("composed_paths", report.get("scope_paths", []))), False


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
        value = line[3:]
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        paths.append(value)
    return paths


def current_gov_paths(
    base: str = GOV_BASELINE,
    head: str = "HEAD",
) -> frozenset[str]:
    from tools.scope_compat import effective_scope_base

    committed = _git("diff", "--name-only", effective_scope_base(base, head), head, "--")
    working = _git("diff", "--name-only", head, "--")
    return frozenset(
        path
        for path in [*committed, *working, *_status_paths()]
        if path in GOV_OWNED_PATHS
    )


def verified_gov_paths(
    excluded: frozenset[str] = frozenset(),
) -> tuple[frozenset[str], bool]:
    """Return GOV and independently verified B08 paths only after PASS."""

    safe_excluded = frozenset(excluded) - GOV_PATHS
    report = check_scope(excluded=safe_excluded)
    if report["status"] != "PASS":
        return frozenset(), True
    return (
        frozenset(current_gov_paths())
        | frozenset(report["b08_paths"])
        | frozenset(report["b10b_paths"])
        | frozenset(report["b02_paths"])
    ), False


def check_scope(
    *,
    base: str = GOV_BASELINE,
    head: str = "HEAD",
    excluded: frozenset[str] = frozenset(),
    child_mode: bool = False,
) -> dict[str, Any]:
    base_commit = _git("rev-parse", base)[0]
    head_commit = _git("rev-parse", head)[0]
    merge_base = _git("merge-base", base_commit, head_commit)[0]
    from tools.scope_compat import effective_scope_base

    comparison_base = _git("rev-parse", effective_scope_base(base, head))[0]
    committed = _git("diff", "--name-only", comparison_base, head_commit, "--")
    working = _git("diff", "--name-only", head_commit, "--")
    status_paths = _status_paths()
    all_paths = sorted(set([*committed, *working, *status_paths]))
    requested_exclusions = frozenset(excluded)
    from tools.scope_compat import verified_b02_paths

    b02_paths, b02_failed = verified_b02_paths(requested_exclusions)
    if not b02_failed:
        requested_exclusions |= b02_paths
    later_validation_failed = False
    if not child_mode:
        try:
            from tools.verify_b10b_scope import _current_b10b_paths, _verified_later_paths

            later_paths, later_validation_failed = _verified_later_paths(requested_exclusions)
            if not later_validation_failed:
                requested_exclusions |= later_paths - GOV_PATHS | _current_b10b_paths()
        except Exception:
            later_validation_failed = True
    rejected_exclusions = [] if child_mode else sorted(requested_exclusions & GOV_PATHS)
    if child_mode:
        b08_paths, b08_failed = frozenset(), False
        b10b_paths, b10b_failed = frozenset(), False
    else:
        b08_paths, b08_failed = _verified_b08_paths(requested_exclusions)
        b10b_paths, b10b_failed = _verified_b10b_paths(requested_exclusions | b08_paths)
    rejected_b08_exclusions = (
        []
        if child_mode
        else sorted(
            path
            for path in requested_exclusions
            if _is_b08_path(path) and path in all_paths and path not in b08_paths
        )
    )
    safe_excluded = (
        frozenset(requested_exclusions)
        if child_mode
        else frozenset(
            path
            for path in requested_exclusions - GOV_PATHS
            if not _is_b08_path(path)
        ) | b08_paths | b10b_paths
    )
    unexpected = sorted(
        path
        for path in all_paths
        if path not in GOV_OWNED_PATHS and path not in safe_excluded
    )
    # GOV's fixed three-document ownership is independent of whether the
    # current PR happens to edit one of those already-accepted documents.
    owned_paths = sorted(path for path in all_paths if path in GOV_OWNED_PATHS)
    # A child donation must carry the exact current GOV-owned candidates (such
    # as baseline_hardening_scan.py), while the standalone GOV boundary keeps
    # its fixed three-document scope contract.
    scope_paths = owned_paths if child_mode else sorted(GOV_PATHS)
    return {
        "status": "PASS"
        if (
            merge_base == base_commit
            and not unexpected
            and not rejected_exclusions
            and not rejected_b08_exclusions
            and not b08_failed
            and not b10b_failed
            and not b02_failed
            and not later_validation_failed
        )
        else "FAIL",
        "baseline": base_commit,
        "head": head_commit,
        "base_is_ancestor": merge_base == base_commit,
        "changed_paths": sorted(set([*committed, *working])),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": scope_paths,
        "owned_paths": owned_paths,
        "unexpected_paths": unexpected,
        "excluded_paths": sorted(safe_excluded),
        "rejected_exclusions": rejected_exclusions,
        "rejected_b08_exclusions": rejected_b08_exclusions,
        "b08_paths": sorted(b08_paths),
        "b08_scope_pass": not b08_failed,
        "b10b_paths": sorted(b10b_paths),
        "b10b_scope_pass": not b10b_failed,
        "b02_paths": sorted(b02_paths),
        "b02_scope_pass": not b02_failed,
        "allowlist_size": len(GOV_PATHS),
        "child_mode": child_mode,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the standalone GOV document boundary.")
    parser.add_argument("--base", default=GOV_BASELINE)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    report = check_scope(base=args.base, head=args.head)
    print(
        "status={status} baseline={baseline} head={head} base_is_ancestor={base_is_ancestor} "
        "changed={changed_paths} status_paths={status_paths} scope_paths={scope_paths} "
        "unexpected={unexpected_paths} rejected_exclusions={rejected_exclusions}".format(
            **report
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
