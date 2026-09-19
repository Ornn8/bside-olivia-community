"""Fail-closed B11 visual scope verifier with an explicit child protocol.

The visual tranche is optional in the native-ASR checkout.  When present,
this verifier is the later sibling for the existing tranche graph.  Its
parent callers may exclude B11 paths only after this module has returned a
child-mode PASS.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
B11_BASELINE = "8434545de00240dad9cdee2fb91fe57616c875af"
B11_ALLOWED_EXACT = frozenset(
    {
        ".gitignore",
        "docs/B11_VISUAL_RUNTIME.md",
        "runtime/visual/__init__.py",
        "runtime/visual/livetalking.py",
        "runtime/visual/livetalking_backend.py",
        "tests/live_driver/test_livetalking_runtime.py",
        "tests/live_driver/test_livetalking_backend.py",
        "tests/packaging/test_B11_lifecycle.py",
        "tests/packaging/test_B11_scope.py",
        "tools/livetalking_runtime.py",
        "tools/livetalking_worker.py",
        "tools/scope_compat.py",
        "tools/verify_b08_scope.py",
        "tools/verify_b06_scope.py",
        "tools/verify_B07_scope.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b04_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_b05_scope.py",
        "tools/verify_p01_scope.py",
        "tools/verify_b10b_scope.py",
        "tools/verify_b11_scope.py",
        "tools/verify_current_main_scope.py",
        "tools/verify_gov_scope.py",
    }
)
B11_SHARED_B10B = frozenset(
    {
        "docs/B10B_MODULE_LIFECYCLE.md",
        "runtime/packaging/b10b/manager.py",
        "runtime/packaging/manifests/b10b.modules.json",
    }
)
B11_SHARED_B10A = frozenset({"runtime/packaging/b10a/manager.py"})


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/")


def is_b11_path(path: str) -> bool:
    return _normalize(path) in B11_ALLOWED_EXACT


def _git(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [_normalize(line) for line in result.stdout.splitlines() if line]


def _status_paths() -> list[str]:
    paths: list[str] = []
    for line in _git("status", "--short", "--untracked-files=all"):
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        paths.append(_normalize(value))
    return paths


def _all_paths(base: str, head: str) -> tuple[str, str, str, list[str], list[str], list[str]]:
    base_commit = _git("rev-parse", base)[0]
    head_commit = _git("rev-parse", head)[0]
    merge_base = _git("merge-base", base_commit, head_commit)[0]
    committed = _git("diff", "--name-only", base_commit, head, "--")
    working = _git("diff", "--name-only", head, "--")
    status_paths = _status_paths()
    return base_commit, head_commit, merge_base, committed, working, status_paths


def current_b11_paths() -> frozenset[str]:
    """Return only exact B11-owned paths from the current checkout."""

    try:
        from tools.scope_compat import effective_scope_base

        comparison_base = effective_scope_base(B11_BASELINE, resolver=_git)
        _, _, _, committed, working, status_paths = _all_paths(comparison_base, "HEAD")
    except (IndexError, OSError, subprocess.CalledProcessError):
        return frozenset()
    return frozenset(
        path
        for path in [*committed, *working, *status_paths]
        if is_b11_path(path)
    )


def _child(
    name: str,
    checker: Callable[..., dict[str, Any]],
    *,
    excluded: frozenset[str],
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        report = checker(excluded=excluded, child_mode=True, **kwargs)
    except Exception as exc:
        return {
            "status": "FAIL",
            "checker": name,
            "error_code": "SCOPE_CHILD_ERROR",
            "error_type": type(exc).__name__,
        }
    if not isinstance(report, dict) or report.get("status") != "PASS":
        return {"status": "FAIL", "checker": name, "child": report}
    return report


def _compose_native_children(
    b11_paths: frozenset[str],
    excluded: frozenset[str],
    *,
    comparison_base: str,
) -> tuple[frozenset[str], bool, dict[str, dict[str, Any]]]:
    """Independently check native siblings before excluding their paths."""

    from tools.verify_B07_scope import check_scope as check_b07, current_b07_paths
    from tools.verify_B10A_scope import check_scope as check_b10a, current_b10a_paths
    from tools.verify_b02_scope import check_scope as check_b02
    from tools.verify_b04_scope import check_scope as check_b04
    from tools.verify_b05_scope import check_scope as check_b05, current_b05_paths
    from tools.verify_b06_scope import check_scope as check_b06, current_b06_paths
    from tools.verify_b08_scope import check_scope as check_b08, current_b08_paths
    from tools.verify_b10b_scope import check_scope as check_b10b, current_b10b_paths
    from tools.verify_gov_scope import check_scope as check_gov, current_gov_paths
    from tools.verify_p01_scope import check_scope as check_p01, current_p01_paths
    from tools.check_b11_docs import verified_b11_paths as verified_b11_docs_paths

    docs_paths, docs_failed = verified_b11_docs_paths()
    if docs_failed:
        return frozenset(excluded), True, {
            "b11_docs": {"status": "FAIL", "scope_paths": []}
        }

    candidates = {
        "b02": frozenset(),
        "b04": frozenset(),
        "b05": current_b05_paths(),
        "b06": current_b06_paths(),
        "b08": current_b08_paths(),
        "b07": current_b07_paths(),
        "b10a": current_b10a_paths(comparison_base=comparison_base),
        "b10b": current_b10b_paths(),
        "gov": current_gov_paths(),
        "p01": current_p01_paths(),
    }
    all_candidates = frozenset(excluded) | b11_paths | docs_paths
    all_candidates |= frozenset().union(*candidates.values())
    reports = {
        "b02": _child("b02", check_b02, excluded=all_candidates - candidates["b02"]),
        "b04": _child("b04", check_b04, excluded=all_candidates - candidates["b04"]),
        "b05": _child(
            "b05",
            check_b05,
            excluded=all_candidates - candidates["b05"],
            compose_b07=False,
        ),
        "b06": _child("b06", check_b06, excluded=all_candidates - candidates["b06"]),
        "b07": _child("b07", check_b07, excluded=all_candidates - candidates["b07"]),
        "b08": _child(
            "b08",
            check_b08,
            excluded=all_candidates - candidates["b08"],
        ),
        "b10a": _child(
            "b10a",
            check_b10a,
            excluded=all_candidates - candidates["b10a"],
            comparison_base=comparison_base,
        ),
        "b10b": _child("b10b", check_b10b, excluded=all_candidates - candidates["b10b"]),
        "gov": _child("gov", check_gov, excluded=all_candidates - candidates["gov"]),
        "p01": _child("p01", check_p01, excluded=all_candidates - candidates["p01"]),
    }
    trusted = frozenset(excluded) | docs_paths
    for name, report in reports.items():
        if report.get("status") == "PASS":
            trusted |= candidates[name]
    failed = any(report.get("status") != "PASS" for report in reports.values())
    return trusted, failed, reports


def check_scope(
    *,
    base: str = B11_BASELINE,
    head: str = "HEAD",
    excluded: frozenset[str] = frozenset(),
    child_mode: bool = False,
) -> dict[str, Any]:
    try:
        base_commit, head_commit, merge_base, committed, working, status_paths = _all_paths(
            base, head
        )
    except (IndexError, OSError, subprocess.CalledProcessError) as exc:
        return {
            "status": "FAIL",
            "error": f"git verification failed: {type(exc).__name__}",
            "unexpected_paths": [],
            "scope_paths": [],
            "child_mode": child_mode,
        }

    all_paths = sorted(set([*committed, *working, *status_paths]))
    b11_paths = frozenset(path for path in all_paths if is_b11_path(path))
    excluded_paths = frozenset(_normalize(path) for path in excluded)
    composition_failed = False
    child_reports: dict[str, dict[str, Any]] = {}
    try:
        from tools.check_b11_docs import verified_b11_paths as verified_b11_docs_paths

        docs_paths, docs_failed = verified_b11_docs_paths()
    except Exception:
        docs_paths, docs_failed = frozenset(), True
    excluded_paths |= docs_paths
    composition_failed = docs_failed
    if not child_mode:
        from tools.scope_compat import effective_scope_base

        excluded_paths, native_failed, child_reports = _compose_native_children(
            b11_paths,
            excluded_paths,
            comparison_base=effective_scope_base(base, head),
        )
        composition_failed = composition_failed or native_failed
    shared = sorted(path for path in all_paths if path in B11_SHARED_B10B)
    unexpected = sorted(
        path
        for path in all_paths
        if path not in B11_ALLOWED_EXACT
        and path not in B11_SHARED_B10B
        and path not in excluded_paths
    )
    return {
        "status": "PASS"
        if merge_base == base_commit and not unexpected and not composition_failed
        else "FAIL",
        "base": base_commit,
        "head": head_commit,
        "base_is_ancestor": merge_base == base_commit,
        "changed_paths": sorted(set([*committed, *working])),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": sorted(b11_paths),
        "shared_b10b_paths": shared,
        "unexpected_paths": unexpected,
        "excluded_paths": sorted(excluded_paths),
        "child_reports": child_reports,
        "composition_pass": not composition_failed if not child_mode else "not-run",
        "child_mode": child_mode,
        "allowlist_exact": sorted(B11_ALLOWED_EXACT),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the B11 visual assembly boundary.")
    parser.add_argument("--base", default=B11_BASELINE)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    report = check_scope(base=args.base, head=args.head)
    print(
        "status={status} base={base} head={head} base_is_ancestor={base_is_ancestor} "
        "scope_paths={scope_paths} shared_b10b={shared_b10b_paths} "
        "unexpected={unexpected_paths} composition_pass={composition_pass}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
