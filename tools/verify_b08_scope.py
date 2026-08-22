"""Base-aware B08 scope scanner with fail-closed child composition."""

from __future__ import annotations

import argparse
from functools import lru_cache
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# B08 is intentionally anchored to the accepted current-main merge, not to a
# moving branch head.  Keep the prior baseline named for regression evidence;
# current acceptance must be measured against the governance-accepted main.
HISTORICAL_B08_BASELINE = "49fe48dbf85cb4a79712836f72544f10bb636468"
CURRENT_MAIN_BASELINE = "5444781c673a1d77fc5835a79d3221e66d37061c"
B08_BASELINE = CURRENT_MAIN_BASELINE
B08_ALLOWED_EXACT = frozenset(
    {
        ".gitignore",
        ".github/workflows/required-ci.yml",
        "contracts/live_event.schema.json",
        "contracts/live_health.schema.json",
        "contracts/live_provenance.schema.json",
        "docs/B08_LIVE_ORCHESTRATION.md",
        "docs/OPS_REQUIRED_CI.md",
        "docs/reviews/b08-live-evidence.md",
        "live/__init__.py",
        "live/contracts.py",
        "live/environment.py",
        "live/provenance.json",
        "live/session.py",
        "llm_gateway.py",
        "tests/live/test_contract.py",
        "tests/live/test_e2e_acceptance.py",
        "tests/live/test_environment.py",
        "tests/live/test_scope.py",
        "tools/live_e2e_acceptance.py",
        "tools/live_healthcheck.py",
        "tools/verify_b10b_scope.py",
        "tools/verify_b08_scope.py",
        "tools/verify_current_main_scope.py",
        "tools/verify_gov_scope.py",
    }
)
B08_ALLOWED_PREFIXES: tuple[str, ...] = ()
B08_SHARED_B10B = frozenset(
    {
        "docs/B10B_MODULE_LIFECYCLE.md",
        "runtime/packaging/b10b/manager.py",
        "runtime/packaging/manifests/b10b.modules.json",
    }
)
MEDIA_SUFFIXES = frozenset(
    {".avi", ".flac", ".gif", ".jpeg", ".jpg", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".png", ".wav", ".webm"}
)


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/")


def is_b08_path(path: str) -> bool:
    normalized = _normalize(path)
    return normalized in B08_ALLOWED_EXACT or normalized.startswith(B08_ALLOWED_PREFIXES)


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
    return tuple(_normalize(line) for line in result.stdout.splitlines() if line)


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


def _paths_for_base(base: str, head: str = "HEAD") -> frozenset[str]:
    from tools.scope_compat import effective_scope_base

    changed = _git("diff", "--name-only", effective_scope_base(base, head, use_ci_diff=base == B08_BASELINE), head, "--")
    working = _git("diff", "--name-only", head, "--")
    return frozenset(
        path for path in [*changed, *working, *_status_paths()] if is_b08_path(path)
    )


def current_b08_paths() -> frozenset[str]:
    """Return only B08-owned paths changed from the fixed B08 baseline."""

    return _paths_for_base(B08_BASELINE)


def _safe_child(name: str, checker: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    try:
        report = checker(**kwargs)
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


def _verified_governance_paths(
    b08_paths: frozenset[str],
) -> tuple[frozenset[str], bool]:
    """Return the prior GOV tranche only after its verifier passes."""

    try:
        from tools.verify_gov_scope import check_scope as check_gov_scope

        report = check_gov_scope(excluded=b08_paths)
    except Exception:
        return frozenset(), True
    if report.get("status") != "PASS":
        return frozenset(), True
    return (
        frozenset(report.get("owned_paths", []))
        | frozenset(report.get("b10b_paths", []))
        | frozenset(report.get("b02_paths", []))
    ), False


def _verified_b10b_paths(b08_paths: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Trust B10B only after its non-recursive scope verifier passes."""

    try:
        from tools.scope_compat import current_b11_paths, verified_b02_paths
        from tools.verify_B07_scope import current_b07_paths
        from tools.verify_B10A_scope import current_b10a_paths
        from tools.verify_b05_scope import current_b05_paths
        from tools.verify_b06_scope import current_b06_paths
        from tools.verify_b10b_scope import check_scope as check_b10b_scope
        from tools.verify_b10b_scope import current_b10b_paths
        from tools.verify_gov_scope import current_gov_paths

        b10b_paths = current_b10b_paths()
        sibling_paths = (
            b08_paths
            | current_b05_paths()
            | current_b06_paths()
            | current_b07_paths()
            | current_gov_paths()
            | current_b10a_paths()
            | current_b11_paths()
        )
        b02_paths, b02_failed = verified_b02_paths(sibling_paths)
        if b02_failed:
            return frozenset(), True
        sibling_paths |= b02_paths
        report = check_b10b_scope(
            excluded=sibling_paths - b10b_paths,
            child_mode=True,
        )
    except Exception:
        return frozenset(), True
    if report.get("status") != "PASS":
        return frozenset(), True
    return frozenset(report.get("composed_paths", report.get("scope_paths", []))), False


def _compose_children(
    b08_paths: frozenset[str], *, historical_mutual: bool = False, include_current_main: bool = True
) -> dict[str, dict[str, Any]]:
    """Run every relevant child scope and trust paths only after PASS."""

    from tools.verify_B07_scope import check_scope as check_b07_scope, current_b07_paths
    from tools.verify_B10A_scope import check_scope as check_b10a_scope
    from tools.verify_b10b_scope import check_scope as check_b10b_scope
    from tools.verify_b02_scope import check_scope as check_b02_scope
    from tools.verify_b04_scope import check_scope as check_b04_scope
    from tools.verify_b05_scope import check_scope as check_b05_scope, current_b05_paths, is_b05_path
    from tools.verify_b06_scope import check_scope as check_b06_scope, current_b06_paths
    from tools.verify_current_main_scope import check_scope as check_current_main_scope
    from tools.verify_gov_scope import check_scope as check_gov_scope
    from tools.verify_p01_scope import check_scope as check_p01_scope, current_p01_paths
    from tools.verify_p02_scope import check_scope as check_p02_scope, current_p02_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.verify_b10b_scope import current_b10b_paths
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.scope_compat import current_b11_paths, verified_b02_paths, verified_b11_paths

    reports: dict[str, dict[str, Any]] = {}
    candidate_b05 = current_b05_paths()
    candidate_b06 = current_b06_paths()
    candidate_b07 = current_b07_paths()
    candidate_gov = current_gov_paths()
    candidate_b10b = current_b10b_paths()
    candidate_b10a = current_b10a_paths()
    candidate_b11 = current_b11_paths()
    candidate_p01 = current_p01_paths()
    candidate_p02 = current_p02_paths()
    candidates = (
        b08_paths
        | candidate_b05
        | candidate_b06
        | candidate_b07
        | candidate_gov
        | candidate_b10b
        | candidate_b10a
        | candidate_b11
        | candidate_p01
        | candidate_p02
    )
    candidate_b02, b02_failed = verified_b02_paths(candidates)
    if not b02_failed:
        candidates |= candidate_b02

    p02 = _safe_child(
        "p02",
        check_p02_scope,
        excluded=candidates - candidate_p02,
        child_mode=True,
    )
    reports["p02"] = p02
    p02_paths = candidate_p02 if p02.get("status") == "PASS" else frozenset()
    if p02.get("status") == "PASS":
        p02["scope_paths"] = sorted(candidate_p02)

    b11_paths, b11_failed = verified_b11_paths(candidates - candidate_b11)
    reports["b11"] = {
        "status": "FAIL" if b11_failed else "PASS",
        "scope_paths": sorted(b11_paths),
    }

    b10b = _safe_child(
        "b10b",
        check_b10b_scope,
        child_mode=True,
        excluded=candidates - candidate_b10b,
    )
    reports["b10b"] = b10b
    b10b_paths = (
        frozenset(b10b.get("composed_paths", b10b.get("scope_paths", [])))
        if b10b.get("status") == "PASS"
        else frozenset()
    )
    gov = _safe_child(
        "gov",
        check_gov_scope,
        child_mode=True,
        excluded=candidates - candidate_gov,
    )
    reports["gov"] = gov
    gov_paths = (
        frozenset(gov.get("scope_paths", []))
        | frozenset(gov.get("b02_paths", []))
        if gov.get("status") == "PASS"
        else frozenset()
    )
    if include_current_main:
        current_main = _safe_child("current_main", check_current_main_scope, child_mode=True)
        reports["current_main"] = current_main
    # Each child is checked directly while sibling tranches are explicitly
    # excluded.  This avoids trusting a nested child result before its own
    # fixed-base checker has run, while still allowing the legacy scanners to
    # retain their independent allowlists.
    b05 = _safe_child(
        "b05",
        check_b05_scope,
        excluded=candidates - candidate_b05,
        compose_b07=False,
        child_mode=True,
    )
    reports["b05"] = b05
    # B05's child-mode report intentionally exposes its own boundary as
    # ``excluded_paths`` (the legacy verifier has no ``scope_paths`` field).
    # Only trust the precomputed B05 candidates after this independent child
    # returned PASS; never use them as an unconditional allow-list.
    b05_paths = (
        frozenset(
            path
            for path in [*b05.get("changed_paths", []), *b05.get("status_paths", [])]
            if is_b05_path(path)
        )
        if b05.get("status") == "PASS"
        else frozenset()
    )
    if b05.get("status") == "PASS":
        b05["scope_paths"] = sorted(b05_paths)

    b06 = _safe_child(
        "b06",
        check_b06_scope,
        excluded=candidates - candidate_b06,
        child_mode=True,
    )
    reports["b06"] = b06
    b06_paths = candidate_b06 if b06.get("status") == "PASS" else frozenset()

    b07 = _safe_child(
        "b07",
        check_b07_scope,
        excluded=candidates - candidate_b07,
        child_mode=True,
    )
    reports["b07"] = b07
    b07_paths = candidate_b07 if b07.get("status") == "PASS" else frozenset()

    # These local sets are the result of the independent child checks above.
    # In particular, B05 does not expose ``scope_paths`` in its legacy report,
    # so trusting only child report fields would leak a false unexpected-path
    # failure.  A child contributes owned paths only through its PASS branch.
    trusted = (
        b08_paths
        | b10b_paths
        | gov_paths
        | b05_paths
        | b06_paths
        | b07_paths
        | b11_paths
        | p02_paths
        | (candidate_b02 if not b02_failed else frozenset())
    )
    if historical_mutual:
        p01 = _safe_child(
            "p01",
            check_p01_scope,
            excluded=trusted | candidate_b10a,
            child_mode=True,
        )
        reports["p01"] = p01
        if p01.get("status") == "PASS":
            p01["scope_paths"] = sorted(candidate_p01)
            b10a = _safe_child(
                "b10a",
                check_b10a_scope,
                excluded=trusted | candidate_p01,
                child_mode=True,
            )
        else:
            b10a = {"status": "FAIL", "error_code": "P01_PREREQUISITE_FAILED"}
        reports["b10a"] = b10a
        if p01.get("status") == "PASS" and b10a.get("status") == "PASS":
            b10a["scope_paths"] = sorted(candidate_b10a)
            trusted |= candidate_p01 | candidate_b10a
        for name, checker in (("b02", check_b02_scope), ("b04", check_b04_scope)):
            reports[name] = _safe_child(
                name, checker, excluded=trusted, child_mode=True
            )
            if name == "b02" and reports[name].get("status") == "PASS":
                reports[name]["scope_paths"] = sorted(candidate_b02)
    else:
        p01 = _safe_child(
            "p01",
            check_p01_scope,
            excluded=trusted | candidate_b10a,
            child_mode=True,
        )
        reports["p01"] = p01
        if p01.get("status") == "PASS":
            p01["scope_paths"] = sorted(candidate_p01)
            b10a = _safe_child(
                "b10a",
                check_b10a_scope,
                excluded=trusted | candidate_p01,
                child_mode=True,
            )
        else:
            b10a = {"status": "FAIL", "error_code": "P01_PREREQUISITE_FAILED"}
        reports["b10a"] = b10a
        if p01.get("status") == "PASS" and b10a.get("status") == "PASS":
            b10a["scope_paths"] = sorted(candidate_b10a)
            trusted |= candidate_p01 | candidate_b10a
        for name, checker in (("b02", check_b02_scope), ("b04", check_b04_scope)):
            reports[name] = _safe_child(
                name, checker, excluded=trusted, child_mode=True
            )
            if name == "b02" and reports[name].get("status") == "PASS":
                reports[name]["scope_paths"] = sorted(candidate_b02)
    return reports


def historical_child_reports() -> dict[str, dict[str, Any]]:
    """Return the fixed-base B08 child graph without re-entering current-main."""

    from tools.scope_compat import scope_ci_diff_mode

    with scope_ci_diff_mode(False):
        return _compose_children(
            current_b08_paths(), historical_mutual=True, include_current_main=False
        )


def check_scope(
    *,
    base: str = B08_BASELINE,
    head: str = "HEAD",
    composed: bool = True,
    excluded: frozenset[str] = frozenset(),
    child_mode: bool = False,
    compose_b10b: bool = True,
) -> dict[str, Any]:
    try:
        from tools.check_b11_docs import verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths()
    except Exception:
        b11_paths, b11_failed = frozenset(), True
    base_commit = _git("rev-parse", base)[0]
    head_commit = _git("rev-parse", head)[0]
    merge_base = _git("merge-base", base_commit, head_commit)[0]
    from tools.scope_compat import effective_scope_base

    comparison_base = _git("rev-parse", effective_scope_base(base, head, use_ci_diff=base == B08_BASELINE))[0]
    committed = _git("diff", "--name-only", comparison_base, head_commit, "--")
    working = _git("diff", "--name-only", head_commit, "--")
    status_paths = _status_paths()
    all_paths = sorted(set([*committed, *working, *status_paths]))
    candidate_b08_paths = frozenset(path for path in all_paths if is_b08_path(path))
    if compose_b10b and not child_mode:
        b10b_paths, b10b_failed = _verified_b10b_paths(candidate_b08_paths)
    else:
        b10b_paths, b10b_failed = frozenset(), False
    requested_exclusions = frozenset(_normalize(path) for path in excluded)
    if child_mode:
        rejected_exclusions: list[str] = []
    elif compose_b10b:
        try:
            from tools.verify_gov_scope import GOV_OWNED_PATHS

            rejected_exclusions = sorted(
                path
                for path in requested_exclusions
                if path not in (b10b_paths | b11_paths | GOV_OWNED_PATHS)
            )
        except Exception:
            rejected_exclusions = sorted(requested_exclusions)
    else:
        try:
            from tools.verify_b10b_scope import is_b10b_path

            rejected_exclusions = sorted(
                path
                for path in requested_exclusions
                if path not in b11_paths and not is_b10b_path(path)
            )
        except Exception:
            rejected_exclusions = sorted(requested_exclusions)
    excluded = (
        (requested_exclusions - frozenset(rejected_exclusions))
        | b11_paths
        | b10b_paths
    )
    unexpected = sorted(
        path for path in all_paths if not is_b08_path(path) and path not in excluded
    )
    media = sorted(
        path
        for path in all_paths
        if path not in excluded
        and (Path(path).suffix.casefold() in MEDIA_SUFFIXES or path.startswith(".evidence/"))
    )
    base_is_ancestor = merge_base == base_commit
    report: dict[str, Any] = {
        "status": "PASS"
        if (
            base_is_ancestor
            and not unexpected
            and not media
            and not b11_failed
            and not b10b_failed
            and not rejected_exclusions
        )
        else "FAIL",
        "baseline": base_commit,
        "head": head_commit,
        "base_is_ancestor": base_is_ancestor,
        "changed_paths": sorted(set([*committed, *working])),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": sorted(path for path in all_paths if is_b08_path(path)),
        "unexpected_paths": unexpected,
        "media_paths": media,
        "allowlist_exact": sorted(B08_ALLOWED_EXACT),
        "allowlist_prefixes": list(B08_ALLOWED_PREFIXES),
        "composed": composed,
        "child_mode": child_mode,
        "excluded_paths": sorted(excluded),
        "b11_paths": sorted(b11_paths),
        "b11_scope_pass": not b11_failed,
        "b10b_paths": sorted(b10b_paths),
        "b10b_scope_pass": not b10b_failed,
        "compose_b10b": compose_b10b,
        "rejected_exclusions": rejected_exclusions,
    }
    if composed and not child_mode:
        children = _compose_children(current_b08_paths(), historical_mutual=True)
        report["child_reports"] = children
        report["composition_pass"] = all(item.get("status") == "PASS" for item in children.values())
        if not report["composition_pass"]:
            report["status"] = "FAIL"
        trusted = frozenset(report["excluded_paths"])
        for child in children.values():
            if child.get("status") == "PASS":
                trusted |= frozenset(child.get("scope_paths", []))
        independently_reported_dirty = frozenset(
            path
            for child in children.values()
            if child.get("status") == "PASS"
            for path in child.get("status_paths", [])
        )
        parent_only_dirty = set(status_paths) - set(independently_reported_dirty)
        trusted -= frozenset(parent_only_dirty)
        report["parent_only_dirty_paths"] = sorted(parent_only_dirty)
        report["excluded_paths"] = sorted(trusted)
        report["unexpected_paths"] = sorted(
            path for path in all_paths if not is_b08_path(path) and path not in trusted
        )
        report["media_paths"] = sorted(
            path
            for path in all_paths
            if path not in trusted
            and (Path(path).suffix.casefold() in MEDIA_SUFFIXES or path.startswith(".evidence/"))
        )
        report["status"] = (
            "PASS"
            if report["base_is_ancestor"]
            and report["composition_pass"]
            and not report["rejected_exclusions"]
            and not report["unexpected_paths"]
            and not report["media_paths"]
            else "FAIL"
        )
    else:
        report["composition_pass"] = "not-run"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the fixed-base B08 composition scope.")
    parser.add_argument("--base", default=B08_BASELINE)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--current", action="store_true", help="run the current B08 boundary explicitly")
    parser.add_argument("--historical-only", action="store_true", help="run only the fixed-base boundary")
    parser.add_argument("--composed", action="store_true", help="also run all child historical/current/composed scopes")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.current, args.historical_only, args.composed)) > 1:
        parser.error("--current, --historical-only, and --composed are mutually exclusive")
    base = args.base
    excluded: frozenset[str] = frozenset()
    historical_governance_failed = False
    if args.historical_only and args.base == B08_BASELINE:
        base = HISTORICAL_B08_BASELINE
        excluded, historical_governance_failed = _verified_governance_paths(
            current_b08_paths()
        )
        from tools.scope_compat import scope_ci_diff_mode
        from tools.verify_b10b_scope import check_scope as check_b10b_scope

        with scope_ci_diff_mode(False):
            historical_b10b = check_b10b_scope()
            historical_children = _compose_children(
                current_b08_paths(), historical_mutual=True
            )
        if historical_b10b.get("status") == "PASS":
            excluded |= frozenset(historical_b10b.get("scope_paths", []))
        else:
            historical_governance_failed = True
        # The fixed-base B08 boundary must exclude current sibling changes
        # only after each sibling's direct verifier passes.  Reuse the same
        # fail-closed composition graph as the current B08 check; this is
        # intentionally narrower than adding a tranche-wide allow-list.
        historical_governance_failed = historical_governance_failed or any(
            child.get("status") != "PASS" for child in historical_children.values()
        )
        for child in historical_children.values():
            if child.get("status") == "PASS":
                excluded |= frozenset(child.get("scope_paths", []))
    report = check_scope(
        base=base,
        head=args.head,
        composed=(args.composed or args.current or not args.historical_only),
        excluded=excluded,
        # The accepted-base mode has already independently verified every child
        # above.  Treat those resulting paths as internal child exclusions;
        # applying the public direct-exclusion contract here would otherwise
        # reject the very siblings the verified composition is meant to carry.
        child_mode=args.historical_only,
    )
    if historical_governance_failed:
        report["status"] = "FAIL"
        report["governance_status"] = "FAIL"
    else:
        report["governance_status"] = "PASS"
    print(
        "status={status} baseline={baseline} head={head} base_is_ancestor={base_is_ancestor} "
        "changed={changed_paths} status_paths={status_paths} unexpected={unexpected_paths} "
        "media={media_paths} composed={composed} composition_pass={composition_pass}".format(**report)
    )
    if args.composed:
        for name, child in report.get("child_reports", {}).items():
            print(f"child={name} status={child.get('status')} error={child.get('error_code', '')}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
