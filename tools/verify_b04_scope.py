"""Verify that the Issue #3 change stays inside the B04 file boundary."""

from __future__ import annotations

import argparse
from functools import lru_cache
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.verify_gov_scope import GOV_PATHS, verified_gov_paths
B04_BASELINE = "7d744fd09cf13f69b4d92e8e971b3f81e6f2d1d0"
ALLOWED = frozenset(
    {
        ".gitignore",
        "README.md",
        "contracts/memory_config.schema.json",
        "docs/B03_LLM_GATEWAY.md",
        "docs/B04_LOCAL_MEMORY.md",
        "docs/MASTER_PLAN.md",
        "docs/STATUS.md",
        "http_contract.py",
        "local_memory.py",
        "local_server.py",
        "contracts/memory_config.example.json",
        "memory_import.py",
        "memory_port.py",
        "memory_prompt.py",
        "tests/llm/test_b04_memory_integration.py",
        "tests/memory/test_local_memory.py",
        "tools/healthcheck.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_b04_scope.py",
    }
)


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


def _verified_b10b_paths() -> tuple[frozenset[str], bool]:
    from tools.verify_b10b_scope import check_scope as check_b10b_scope

    report = check_b10b_scope()
    if report["status"] != "PASS":
        return frozenset(), True
    return frozenset(report.get("composed_paths", report["scope_paths"])), False


def _verified_p01_paths() -> tuple[frozenset[str], bool]:
    from tools.verify_p01_scope import check_scope as check_p01_scope

    report = check_p01_scope()
    if report["status"] != "PASS":
        return frozenset(), True
    from tools.verify_p01_scope import current_p01_paths
    return current_p01_paths(), False


def _verified_b05_paths() -> tuple[frozenset[str], bool]:
    from tools.verify_b05_scope import check_scope as check_b05_scope, current_b05_paths
    from tools.verify_b06_scope import current_b06_paths
    from tools.verify_B07_scope import current_b07_paths
    from tools.verify_b08_scope import current_b08_paths
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.verify_b10b_scope import current_b10b_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.scope_compat import current_b11_paths

    candidate = current_b05_paths()
    siblings = (
        current_b06_paths()
        | current_b07_paths()
        | current_b08_paths()
        | current_b10a_paths()
        | current_b10b_paths()
        | current_gov_paths()
        | current_b11_paths()
    )
    report = check_b05_scope(excluded=siblings, compose_b07=False, child_mode=True)
    return (candidate, False) if report.get("status") == "PASS" else (frozenset(), True)


def _verified_b06_paths(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Exclude B06 only after its baseline-aware child check passes."""

    from tools.verify_B07_scope import current_b07_paths
    from tools.verify_b05_scope import current_b05_paths
    from tools.verify_b06_scope import check_scope as check_b06_scope
    from tools.verify_b08_scope import current_b08_paths
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.scope_compat import current_b11_paths

    b10b_paths, b10b_failed = _verified_b10b_paths()
    if b10b_failed:
        return frozenset(), True
    report = check_b06_scope(
        excluded=(
            excluded
            | current_b05_paths()
            | current_b07_paths()
            | current_b08_paths()
            | current_b10a_paths()
            | current_gov_paths()
            | current_b11_paths()
            | b10b_paths
        ),
        child_mode=True,
    )
    if report["status"] != "PASS":
        return frozenset(), True
    return frozenset(report["scope_paths"]), False


def _verified_b07_paths(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Exclude B07 only after its child verifier has passed."""

    from tools.verify_B07_scope import check_scope as check_b07_scope

    report = check_b07_scope(excluded=excluded, child_mode=True)
    if report["status"] != "PASS":
        return frozenset(), True
    return frozenset(report["scope_paths"]), False


def check_scope(
    excluded: frozenset[str] | None = None,
    *,
    child_mode: bool = False,
) -> dict[str, object]:
    composed = excluded is None and not child_mode
    composition_failed = False
    b10b_paths = frozenset()
    b10b_failed = False
    gov_paths = frozenset()
    gov_failed = False
    b11_paths = frozenset()
    b11_failed = False
    b05_failed = False
    untrusted_b05_paths = frozenset()
    if child_mode:
        excluded = frozenset(excluded or ())
    else:
        b10b_paths, b10b_failed = _verified_b10b_paths()
        composition_failed = composition_failed or b10b_failed
        if excluded is None:
            excluded = frozenset()
        else:
            excluded = frozenset(excluded)
        p01_paths, p01_failed = _verified_p01_paths()
        composition_failed = composition_failed or p01_failed
        if not p01_failed:
            excluded = frozenset(excluded) | p01_paths
        excluded = frozenset(excluded) | b10b_paths
        if composed:
            b05_paths, b05_failed = _verified_b05_paths()
            composition_failed = composition_failed or b05_failed
            excluded = excluded | b05_paths
            if b05_failed:
                from tools.verify_b05_scope import current_b05_paths

                untrusted_b05_paths = current_b05_paths()
        safe_excluded = (frozenset(excluded) | b10b_paths) - GOV_PATHS
        gov_paths, gov_failed = verified_gov_paths(safe_excluded)
        composition_failed = composition_failed or gov_failed
        excluded = safe_excluded | gov_paths
    from tools.verify_B10A_scope import current_b10a_paths

    # B04 predates B10A/B06/B07.  These later tranches must be independently
    # checked before their paths can be excluded, including when B04 is itself
    # running as a B11 child.
    excluded = frozenset(excluded) | current_b10a_paths()
    if not child_mode:
        # The B11 child verifier itself calls B04.  Its owned paths are only a
        # provisional sibling input for the B06/B07 child checks here; they are
        # still accepted below only after the independent B11 verification.
        from tools.scope_compat import current_b11_paths

        excluded = frozenset(excluded) | current_b11_paths()
    b06_paths, b06_failed = _verified_b06_paths(frozenset(excluded))
    composition_failed = composition_failed or b06_failed
    if not b06_failed:
        excluded = frozenset(excluded) | b06_paths
    b07_paths, b07_failed = _verified_b07_paths(frozenset(excluded))
    composition_failed = composition_failed or b07_failed
    if not b07_failed:
        excluded = frozenset(excluded) | b07_paths

    if not child_mode:
        from tools.scope_compat import b11_child_exclusions, verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths(
            b11_child_exclusions(frozenset(excluded))
        )
        if not b11_failed:
            excluded = frozenset(excluded) | b11_paths
        composition_failed = composition_failed or b11_failed
    from tools.scope_compat import effective_scope_base

    changed = _git("diff", "--name-only", effective_scope_base(B04_BASELINE), "HEAD", "--")
    status_paths = [
        line[3:]
        for line in _git("status", "--short", "--untracked-files=all")
        if len(line) >= 4
    ]
    unexpected = sorted(
        {
            path
            for path in [*changed, *status_paths, *untrusted_b05_paths]
            if path not in ALLOWED and path not in excluded
        }
    )
    return {
        "status": "PASS" if not unexpected and not composition_failed else "FAIL",
        "changed_paths": sorted(set(changed)),
        "status_paths": sorted(set(status_paths)),
        "unexpected_paths": unexpected,
        "excluded_paths": sorted(excluded),
        "composed_p01": composed and not composition_failed,
        "composed_gov": not gov_failed,
        "composed_b10b": not b10b_failed,
        "excluded_b10b_paths": sorted(b10b_paths),
        "excluded_b11_paths": sorted(b11_paths),
        "composed_b11": not b11_failed,
        "child_mode": child_mode,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the historical B04 file boundary.")
    parser.add_argument(
        "--composed-p01",
        action="store_true",
        help="exclude only paths that independently pass the P01 scope check",
    )
    parser.add_argument(
        "--historical-only",
        action="store_true",
        help="run only the historical B04 allow-list without composition",
    )
    parser.add_argument(
        "--composed-b05",
        action="store_true",
        help="explicitly exclude only paths owned by the independently checked B05 scope",
    )
    args = parser.parse_args()
    if args.historical_only and args.composed_b05:
        parser.error("--historical-only and --composed-b05 are mutually exclusive")
    if args.composed_b05:
        from tools.verify_b05_scope import current_b05_paths

        report = check_scope(current_b05_paths())
        report["composed_b05"] = True
    else:
        report = check_scope(frozenset()) if args.historical_only else check_scope()
        report["composed_b05"] = False
    print(
        "status={status} changed={changed_paths} status_paths={status_paths} "
        "unexpected={unexpected_paths} excluded={excluded_paths} "
        "composed_p01={composed_p01} composed_b05={composed_b05}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
