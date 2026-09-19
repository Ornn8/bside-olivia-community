"""Base-aware Issue #5 scope and worktree scanner.

The scanner is intentionally separate from the prior B02/B04/B10A/P01
allow-lists.  ``--composed`` is the only mode that excludes the current B05
paths while re-running those prior checks; no prior allow-list is
silently widened.
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

from tools.verify_gov_scope import GOV_PATHS, verified_gov_paths
CANONICAL_BASE = "20fb833b77ad4aa1bf6641ee219dfe66be19aff9"
HISTORICAL_BASE = "0ddfa2816b85df57561bb1ad661d0f3c61e0e98c"

B05_EXACT = frozenset(
    {
        ".gitignore",
        ".github/workflows/required-ci.yml",
        "README.md",
        "asr/__init__.py",
        "asr/config.py",
        "asr/contracts.py",
        "asr/cuda_toolchain.py",
        "asr/errors.py",
        "asr/fallback.py",
        "asr/management.py",
        "asr/metrics.py",
        "asr/protocol.py",
        "asr/provider.py",
        "contracts/asr_config.schema.json",
        "contracts/asr_events.schema.json",
        "docs/B05_STREAMING_ASR.md",
        "docs/OPS_REQUIRED_CI.md",
        "http_contract.py",
        "local_server.py",
        "pytest.ini",
        "tests/http/test_b05_health.py",
        "tests/http/test_contract.py",
        "tests/asr/test_asr_contract.py",
        "tests/asr/test_config.py",
        "tests/asr/test_cuda_toolchain.py",
        "tests/asr/test_evidence.py",
        "tests/asr/test_failure_contract.py",
        "tests/asr/test_fallback.py",
        "tests/asr/test_management.py",
        "tests/asr/test_metrics.py",
        "tests/asr/test_protocol.py",
        "tests/asr/test_provider_health.py",
        "tests/asr/test_schemas.py",
        "tests/asr/test_scope.py",
        "tests/asr/test_tools.py",
        "tests/packaging/test_B10A_scope.py",
        "tools/asr_healthcheck.py",
        "tools/asr_manage.py",
        "tools/build_b05_evidence.py",
        "tools/b05_native_http_snapshot.patch",
        "tools/run_b05_native_probe.py",
        "tools/healthcheck.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_b04_scope.py",
        "tools/verify_b05_scope.py",
        "tools/verify_p01_scope.py",
    }
)
B05_PREFIXES: tuple[str, ...] = ()


def is_b05_path(path: str) -> bool:
    return path in B05_EXACT


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
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def _paths_for_base(base: str, head: str = "HEAD") -> frozenset[str]:
    # _git is a process-local result cache; copy the list before extending
    # it so one composition cannot mutate the cached subprocess result.
    from tools.scope_compat import effective_scope_base

    paths = list(_git("diff", "--name-only", effective_scope_base(base, head), head, "--"))
    paths += list(_git("diff", "--name-only", "HEAD", "--")) + _status_paths()
    return frozenset(path for path in paths if is_b05_path(path))


def current_b05_paths() -> frozenset[str]:
    """Return current-main committed and working-tree B05 paths."""

    return _paths_for_base(CANONICAL_BASE)


def _verified_b06_paths(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Return B06-owned paths only after the B06 child verifier passes."""

    from tools.verify_B07_scope import current_b07_paths
    from tools.verify_b06_scope import check_scope as check_b06_scope
    from tools.verify_b10b_scope import check_scope as check_b10b_scope
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.scope_compat import current_b11_paths

    from tools.scope_compat import b11_child_exclusions
    from tools.scope_compat import current_b11_paths

    b10b_report = check_b10b_scope(
        child_mode=True,
        excluded=b11_child_exclusions(frozenset(excluded)) | current_b11_paths(),
    )
    if b10b_report.get("status") != "PASS":
        return frozenset(), True
    b10b_paths = frozenset(b10b_report.get("scope_paths", []))
    report = check_b06_scope(
        excluded=(
            excluded
            | b10b_paths
            | current_b05_paths()
            | current_b07_paths()
            | current_b10a_paths()
            | current_gov_paths()
            | current_b11_paths()
        ),
        child_mode=True,
    )
    if report["status"] != "PASS":
        return frozenset(), True
    return frozenset(report["scope_paths"]), False


def _verified_b07_paths(
    b05_paths: frozenset[str], b06_paths: frozenset[str], excluded: frozenset[str] = frozenset()
) -> tuple[frozenset[str], bool]:
    """Return B07-owned paths only after the B07 child verifier passes."""

    from tools.verify_B07_scope import check_scope as check_b07_scope
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.scope_compat import current_b11_paths

    report = check_b07_scope(
        excluded=(
            excluded
            | b05_paths
            | b06_paths
            | current_b10a_paths()
            | current_gov_paths()
            | current_b11_paths()
        ),
        child_mode=True,
    )
    if report["status"] != "PASS":
        return frozenset(), True
    return frozenset(report["scope_paths"]), False


def _check_scope(
    *,
    base: str = CANONICAL_BASE,
    head: str = "HEAD",
    composed: bool = False,
    excluded: frozenset[str] = frozenset(),
    compose_b07: bool = True,
    child_mode: bool = False,
) -> dict[str, Any]:
    if child_mode:
        b10b_failed = False
        b10b_paths = frozenset()
        verified_later_paths = frozenset()
    else:
        from tools.verify_b10b_scope import check_scope as check_b10b_scope

        b10b_report = check_b10b_scope()
        b10b_failed = b10b_report["status"] != "PASS"
        b10b_paths = frozenset(b10b_report["scope_paths"]) if not b10b_failed else frozenset()
        verified_later_paths = (
            frozenset(b10b_report.get("b08_paths", [])) if not b10b_failed else frozenset()
        )
        from tools.verify_b08_scope import is_b08_path

        verified_later_paths = frozenset(
            path for path in verified_later_paths if not is_b08_path(path)
        )
        from tools.verify_B10A_scope import current_b10a_paths
        excluded = frozenset(excluded) | current_b10a_paths()
    excluded = frozenset(excluded) | b10b_paths | verified_later_paths
    base_commit = _git("rev-parse", base)[0]
    head_commit = _git("rev-parse", head)[0]
    merge_base = _git("merge-base", base_commit, head_commit)[0]
    from tools.scope_compat import effective_scope_base

    comparison_base = _git("rev-parse", effective_scope_base(base, head))[0]
    committed = _git("diff", "--name-only", comparison_base, head_commit, "--")
    working = _git("diff", "--name-only", head_commit, "--")
    status_paths = _status_paths()
    all_paths = sorted(set([*committed, *working, *status_paths]))
    safe_excluded = frozenset(excluded) if child_mode else frozenset(excluded) - GOV_PATHS
    if child_mode:
        gov_paths, gov_failed = frozenset(), False
    else:
        gov_paths, gov_failed = verified_gov_paths(safe_excluded)
    b05_paths = _paths_for_base(base, head)
    child_excluded = safe_excluded | gov_paths | b10b_paths
    b08_paths: frozenset[str] = frozenset()
    b08_failed = False
    if not child_mode:
        from tools.verify_b08_scope import check_scope as check_b08_scope

        b08_report = check_b08_scope(
            child_mode=True,
            excluded=child_excluded | b05_paths,
        )
        b08_failed = b08_report.get("status") != "PASS"
        if not b08_failed:
            b08_paths = frozenset(b08_report.get("scope_paths", []))
            child_excluded |= b08_paths
    if child_mode:
        b06_paths, b06_failed = frozenset(), False
    else:
        b06_paths, b06_failed = _verified_b06_paths(child_excluded)
    b07_paths: frozenset[str] = frozenset()
    b07_failed = False
    b11_paths = frozenset()
    b11_failed = False
    if compose_b07 and not child_mode:
        b07_paths, b07_failed = _verified_b07_paths(b05_paths, b06_paths, child_excluded)
    excluded_paths = child_excluded | b06_paths | b07_paths | b08_paths
    if not child_mode:
        from tools.scope_compat import b11_child_exclusions, verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths(
            b11_child_exclusions(frozenset(excluded_paths) | current_b05_paths())
        )
        if not b11_failed:
            excluded_paths |= b11_paths
    unexpected = sorted(
        path
        for path in all_paths
        if not is_b05_path(path) and path not in excluded_paths
    )
    report: dict[str, Any] = {
        "status": "PASS"
        if (
            merge_base == base_commit
            and not unexpected
            and not gov_failed
            and not b06_failed
            and not b07_failed
            and not b08_failed
            and not b10b_failed
            and not b11_failed
        )
        else "FAIL",
        "canonical_base": base_commit,
        "head": head_commit,
        "base_is_ancestor": merge_base == base_commit,
        "changed_paths": sorted(set([*committed, *working])),
        "status_paths": sorted(set(status_paths)),
        "unexpected_paths": unexpected,
        "allowlist_exact": sorted(B05_EXACT),
        "allowlist_prefixes": list(B05_PREFIXES),
        "composed": composed,
        "composed_b07": compose_b07 and not b07_failed,
        "composed_gov": not gov_failed,
        "composed_b06": not b06_failed,
        "composed_b10b": not b10b_failed,
        "excluded_b10b_paths": sorted(b10b_paths),
        "excluded_b06_paths": sorted(b06_paths),
        "excluded_b07_paths": sorted(b07_paths),
        "excluded_paths": sorted(excluded_paths),
        "excluded_b11_paths": sorted(b11_paths),
        "composed_b11": not b11_failed,
        "child_mode": child_mode,
    }
    if composed and not child_mode:
        exclusions = (
            b10b_paths
            | current_b05_paths()
            | frozenset(report["excluded_b06_paths"])
            | frozenset(report["excluded_b07_paths"])
            | gov_paths
        )
        reports: dict[str, Any] = {}
        from tools.verify_B10A_scope import check_scope as check_b10a
        from tools.verify_b04_scope import check_scope as check_b04
        from tools.verify_b02_scope import check_scope as check_b02
        from tools.verify_p01_scope import check_scope as check_p01

        for name, checker in (
            ("b02", check_b02),
            ("b04", check_b04),
            ("b10a", check_b10a),
            ("p01", check_p01),
        ):
            reports[name] = checker(excluded=exclusions)
        report["historical_reports"] = reports
        report["historical_composed_pass"] = all(item.get("status") == "PASS" for item in reports.values())
        if not report["historical_composed_pass"]:
            report["status"] = "FAIL"
    return report


def check_scope(
    *,
    base: str = CANONICAL_BASE,
    head: str = "HEAD",
    composed: bool = False,
    excluded: frozenset[str] = frozenset(),
    compose_b07: bool = True,
    child_mode: bool = False,
) -> dict[str, Any]:
    return _check_scope(
        base=base,
        head=head,
        composed=composed,
        excluded=excluded,
        compose_b07=compose_b07,
        child_mode=child_mode,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the fixed-base B05 scope and worktree boundary.")
    parser.add_argument("--base", default=CANONICAL_BASE)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--historical-only",
        action="store_true",
        help="check only the B05 boundary; do not compose historical gates",
    )
    parser.add_argument(
        "--composed",
        action="store_true",
        help="explicitly exclude B05 paths while running B02/B04/B10A/P01 checks",
    )
    args = parser.parse_args(argv)
    if args.historical_only and args.composed:
        parser.error("--historical-only and --composed are mutually exclusive")
    if args.historical_only:
        from tools.verify_b10b_scope import check_scope as check_b10b_scope

        b10b_report = check_b10b_scope()
        b10b_paths = frozenset(b10b_report["scope_paths"]) if b10b_report["status"] == "PASS" else frozenset()
        report = check_scope(
            base=HISTORICAL_BASE,
            head=args.head,
            excluded=b10b_paths,
            compose_b07=True,
        )
        report["composed_b10b"] = b10b_report["status"] == "PASS"
        report["excluded_b10b_paths"] = sorted(b10b_paths)
    else:
        report = check_scope(base=args.base, head=args.head, composed=args.composed)
    report.setdefault("historical_composed_pass", "not-run")
    print(
        "status={status} base={canonical_base} head={head} base_is_ancestor={base_is_ancestor} "
        "changed={changed_paths} status_paths={status_paths} unexpected={unexpected_paths} "
        "composed={composed} composed_b07={composed_b07} "
        "historical_composed_pass={historical_composed_pass}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
