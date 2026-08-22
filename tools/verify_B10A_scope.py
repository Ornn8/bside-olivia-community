"""Verify B10A and explicitly shared OPS changes stay allow-listed."""

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
B10A_BASELINE = "7998028aed20477082ee5620b7bbf3fb33a9c295"
ALLOWED_EXACT = frozenset(
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
        "memory_config.example.json",
        "memory_import.py",
        "memory_port.py",
        "memory_prompt.py",
        "local_server.py",
        "tests/llm/test_b04_memory_integration.py",
        "tests/memory/test_local_memory.py",
        "tools/healthcheck.py",
        "tools/verify_b02_scope.py",
        ".github/CODEOWNERS",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/workflows/required-ci.yml",
        "docs/B10A_MODULE_RUNTIME.md",
        "docs/INTEGRATION_B09_B10A.md",
        "docs/GITHUB_MIGRATION.md",
        "docs/OPS_REQUIRED_CI.md",
        "docs/STATUS.md",
        "runtime/__init__.py",
        "runtime/packaging/__init__.py",
        "runtime/packaging/b10a/__init__.py",
        "runtime/packaging/b10a/__main__.py",
        "runtime/packaging/b10a/cli.py",
        "runtime/packaging/b10a/config.py",
        "runtime/packaging/b10a/errors.py",
        "runtime/packaging/b10a/manager.py",
        "runtime/packaging/b10a/manifest.py",
        "runtime/packaging/b10a/mock_service.py",
        "runtime/packaging/b10a/security.py",
        "runtime/packaging/config/b10a.config.example.json",
        "runtime/packaging/manifests/b10a.modules.json",
        "runtime/packaging/schemas/b10a.config.schema.json",
        "runtime/packaging/schemas/b10a.modules.schema.json",
        "runtime/packaging/schemas/b10a.state.schema.json",
        "tests/packaging/__init__.py",
        "tests/packaging/test_B10A_runtime.py",
        "tests/packaging/test_B10A_scope.py",
        "tests/governance/test_project_status.py",
        "tools/B10A_cli.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b04_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_project_status.py",
        "requirements-ci.txt",
    }
)


@lru_cache(maxsize=None)
def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout


def _allowed(path: str) -> bool:
    return path in ALLOWED_EXACT


def current_b10a_paths(
    *,
    comparison_base: str | None = None,
) -> frozenset[str]:
    """Return only current paths owned by the B10A exact boundary."""

    from tools.scope_compat import effective_scope_base

    if comparison_base is None:
        comparison_base = effective_scope_base(B10A_BASELINE)
    changed = _git("diff", "--name-only", comparison_base, "HEAD", "--").splitlines()
    status = _git("status", "--short", "--untracked-files=all").splitlines()
    status_paths = [line[3:] for line in status if len(line) >= 4]
    return frozenset(path for path in [*changed, *status_paths] if _allowed(path))


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
    """Trust B05/B07 paths only after both child boundaries independently pass."""

    from tools.verify_b05_scope import check_scope as check_b05_scope, current_b05_paths
    from tools.verify_b06_scope import current_b06_paths
    from tools.verify_B07_scope import check_scope as check_b07_scope, current_b07_paths
    from tools.verify_b08_scope import current_b08_paths
    from tools.verify_b10b_scope import current_b10b_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.scope_compat import current_b11_paths, verified_b02_paths

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
    b02_paths, b02_failed = verified_b02_paths(siblings)
    if b02_failed:
        return frozenset(), True
    siblings |= b02_paths
    report = check_b05_scope(
        excluded=siblings,
        compose_b07=False,
        child_mode=True,
    )
    if report.get("status") != "PASS":
        return frozenset(), True
    b07_report = check_b07_scope(
        excluded=(siblings - current_b07_paths()) | candidate,
        child_mode=True,
    )
    if b07_report.get("status") != "PASS":
        return frozenset(), True
    return candidate | frozenset(b07_report.get("scope_paths", [])), False


def _verified_b06_paths(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Exclude B06 only after its baseline-aware child check passes."""

    from tools.verify_B07_scope import current_b07_paths
    from tools.verify_b05_scope import current_b05_paths
    from tools.verify_b06_scope import check_scope as check_b06_scope
    from tools.verify_b08_scope import current_b08_paths
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


def check_scope(
    excluded: frozenset[str] | None = None,
    *,
    child_mode: bool = False,
    comparison_base: str | None = None,
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
            b05_paths, b05_failed = _verified_b05_paths()
            composition_failed = composition_failed or b05_failed
            excluded = excluded | b05_paths
            if b05_failed:
                from tools.verify_b05_scope import current_b05_paths

                untrusted_b05_paths = current_b05_paths()
        else:
            excluded = frozenset(excluded)
        p01_paths, p01_failed = _verified_p01_paths()
        composition_failed = composition_failed or p01_failed
        if not p01_failed:
            excluded = excluded | p01_paths
        excluded = excluded | b10b_paths
        excluded = excluded | current_b10a_paths()
        safe_excluded = (frozenset(excluded) | b10b_paths) - GOV_PATHS
        gov_paths, gov_failed = verified_gov_paths(safe_excluded)
        composition_failed = composition_failed or gov_failed
        excluded = safe_excluded | gov_paths
        # B10A's direct boundary sits after B06/B07.  Those sibling paths are
        # excluded only after their child checks pass, irrespective of whether
        # the caller already supplied an independently verified B05 boundary.
        excluded = excluded | b10b_paths
        b06_paths, b06_failed = _verified_b06_paths(excluded)
        composition_failed = composition_failed or b06_failed
        from tools.scope_compat import current_b11_paths

        excluded = excluded | b06_paths | b10b_paths | current_b11_paths()
        from tools.verify_B07_scope import check_scope as check_b07_scope

        b07_report = check_b07_scope(excluded=excluded, child_mode=True)
        if b07_report["status"] != "PASS":
            composition_failed = True
        else:
            excluded = excluded | frozenset(b07_report.get("scope_paths", []))
        excluded = excluded | b10b_paths
        from tools.scope_compat import b11_child_exclusions, verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths(
            b11_child_exclusions(frozenset(excluded) | current_b10a_paths())
        )
        if not b11_failed:
            excluded = frozenset(excluded) | b11_paths
        composition_failed = composition_failed or b11_failed
    if comparison_base is None:
        from tools.scope_compat import effective_scope_base

        comparison_base = effective_scope_base(B10A_BASELINE)
    changed = [path for path in _git("diff", "--name-only", comparison_base, "HEAD", "--").splitlines() if path]
    status = _git("status", "--short", "--untracked-files=all").splitlines()
    status_paths: list[str] = []
    for line in status:
        if len(line) >= 4:
            status_paths.append(line[3:])
    unexpected = sorted(
        {
            path
            for path in [*changed, *status_paths, *untrusted_b05_paths]
            if not _allowed(path) and path not in excluded
        }
    )
    tracked_b10a = [
        path
        for path in _git("ls-files").splitlines()
        if path.startswith(("runtime/packaging/", "tests/packaging/", "tools/B10A", "tools/verify_B10A"))
    ]
    return {
        "status": "PASS" if not unexpected and not composition_failed else "FAIL",
        "changed_paths": sorted(set(changed)),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": sorted(
            path for path in [*changed, *status_paths] if _allowed(path) and path not in excluded
        ),
        "unexpected_paths": unexpected,
        "tracked_b10a_paths": sorted(tracked_b10a),
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
    parser = argparse.ArgumentParser(description="Check the historical B10A file boundary.")
    parser.add_argument(
        "--composed-p01",
        action="store_true",
        help="exclude only paths that independently pass the P01 scope check",
    )
    parser.add_argument(
        "--historical-only",
        action="store_true",
        help="run only the historical B10A allow-list without composition",
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
        "composed_p01={composed_p01} composed_b05={composed_b05} "
        "tracked_b10a={tracked_b10a_paths}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
