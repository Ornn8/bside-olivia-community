"""Compose current-main scope only after the independent B10B gate passes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _b10b_report() -> dict[str, Any]:
    from tools.verify_b10b_scope import check_scope

    return check_scope()


def _current_p02_paths() -> frozenset[str]:
    from tools.verify_p02_scope import current_p02_paths

    return current_p02_paths()


def _b11_report() -> dict[str, Any]:
    from tools.scope_compat import (
        b11_child_exclusions,
        verified_b02_paths,
        verified_b11_paths,
    )

    exclusions = b11_child_exclusions() | _current_p02_paths()
    b02_paths, b02_failed = verified_b02_paths(exclusions)
    if b02_failed:
        return {"status": "FAIL", "scope_paths": [], "b02_scope_pass": False}
    paths, failed = verified_b11_paths(exclusions | b02_paths)
    return {
        "status": "FAIL" if failed else "PASS",
        "scope_paths": sorted(paths),
        "b02_scope_pass": True,
    }


def _gov_report(excluded: frozenset[str]) -> dict[str, Any]:
    from tools.verify_gov_scope import GOV_PATHS, check_scope

    return check_scope(excluded=frozenset(excluded) - GOV_PATHS)


def _historical_reports(_excluded: frozenset[str]) -> dict[str, dict[str, Any]]:
    from tools.verify_b08_scope import historical_child_reports

    return historical_child_reports()


def check_scope(*, child_mode: bool = False) -> dict[str, Any]:
    if child_mode:
        from tools.verify_B10A_scope import current_b10a_paths
        from tools.verify_B07_scope import current_b07_paths
        from tools.verify_b05_scope import current_b05_paths
        from tools.verify_b06_scope import current_b06_paths
        from tools.verify_b08_scope import check_scope as check_b08
        from tools.verify_b08_scope import current_b08_paths
        from tools.verify_b10b_scope import check_scope as check_b10b
        from tools.verify_b10b_scope import current_b10b_paths
        from tools.verify_gov_scope import check_scope as check_gov
        from tools.verify_gov_scope import current_gov_paths
        from tools.verify_p01_scope import check_scope as check_p01
        from tools.verify_p01_scope import current_p01_paths
        from tools.verify_p02_scope import check_scope as check_p02
        from tools.scope_compat import current_b11_paths, verified_b02_paths, verified_b11_paths

        candidate_b05 = current_b05_paths()
        candidate_b06 = current_b06_paths()
        candidate_b07 = current_b07_paths()
        candidate_b08 = current_b08_paths()
        candidate_b10a = current_b10a_paths()
        candidate_b10b = current_b10b_paths()
        candidate_gov = current_gov_paths()
        candidate_b11 = current_b11_paths()
        candidate_p01 = current_p01_paths()
        candidate_p02 = _current_p02_paths()
        candidates = (
            candidate_b05
            | candidate_b06
            | candidate_b07
            | candidate_b08
            | candidate_b10a
            | candidate_b10b
            | candidate_gov
            | candidate_b11
            | candidate_p01
            | candidate_p02
        )
        candidate_b02, b02_failed = verified_b02_paths(candidates)
        if not b02_failed:
            candidates |= candidate_b02
        p02 = check_p02(excluded=candidates - candidate_p02, child_mode=True)
        p02_pass = p02.get("status") == "PASS"
        p02_paths = candidate_p02 if p02_pass else frozenset()
        p02["scope_paths"] = sorted(p02_paths)
        p01 = check_p01(excluded=candidates - candidate_p01, child_mode=True)
        p01_pass = p01.get("status") == "PASS"
        p01_paths = candidate_p01 if p01_pass else frozenset()
        p01["scope_paths"] = sorted(p01_paths)
        b10b = check_b10b(
            excluded=candidates - candidate_b10b,
            child_mode=True,
        )
        gov = check_gov(
            excluded=candidates - candidate_gov,
            child_mode=True,
        )
        b08 = check_b08(
            excluded=candidates - candidate_b08,
            child_mode=True,
        )
        b11_paths, b11_failed = verified_b11_paths(candidates - candidate_b11)
        trusted = candidate_b02 if not b02_failed else frozenset()
        trusted |= p02_paths
        trusted |= p01_paths
        if b10b.get("status") == "PASS":
            trusted |= candidate_b10b
        if gov.get("status") == "PASS":
            trusted |= candidate_gov
        if b08.get("status") == "PASS":
            trusted |= candidate_b08
        if not b11_failed:
            trusted |= b11_paths
        historical = _historical_reports(trusted)
        historical_pass = all(
            report.get("status") == "PASS" for report in historical.values()
        )
        return {
            "status": "PASS"
            if b10b.get("status") == "PASS"
            and gov.get("status") == "PASS"
            and b08.get("status") == "PASS"
            and p02_pass
            and p01_pass
            and not b02_failed
            and not b11_failed
            else "FAIL",
            "child_mode": True,
            "b10b": b10b,
            "gov": gov,
            "b08": b08,
            "p01": p01,
            "p02": p02,
            "b02": {
                "status": "FAIL" if b02_failed else "PASS",
                "scope_paths": sorted(candidate_b02),
            },
            "b11": {"status": "FAIL" if b11_failed else "PASS", "scope_paths": sorted(b11_paths)},
            "historical": historical,
            "historical_pass": historical_pass,
            "historical_advisory": True,
            "excluded_b10b_paths": sorted(
                candidate_b10b if b10b.get("status") == "PASS" else frozenset()
            ),
            "excluded_gov_paths": sorted(
                candidate_gov if gov.get("status") == "PASS" else frozenset()
            ),
            "excluded_b08_paths": sorted(
                candidate_b08 if b08.get("status") == "PASS" else frozenset()
            ),
        }
    b10b = _b10b_report()
    b10b_pass = b10b.get("status") == "PASS"
    b10b_paths = frozenset(b10b.get("scope_paths", [])) if b10b_pass else frozenset()
    p02_paths = _current_p02_paths()
    b11 = _b11_report()
    b11_pass = b11.get("status") == "PASS"
    b11_paths = frozenset(b11.get("scope_paths", [])) if b11_pass else frozenset()
    gov = _gov_report(b10b_paths | b11_paths | p02_paths)
    gov_pass = gov.get("status") == "PASS"
    gov_paths = frozenset(gov.get("scope_paths", [])) if gov_pass else frozenset()
    b08_paths = frozenset(gov.get("b08_paths", [])) if gov_pass else frozenset()
    excluded = b10b_paths | b11_paths | p02_paths | gov_paths | b08_paths
    historical = _historical_reports(excluded)
    historical_pass = all(report.get("status") == "PASS" for report in historical.values())
    return {
        "status": "PASS" if b10b_pass and b11_pass and gov_pass else "FAIL",
        "b10b": b10b,
        "b11": b11,
        "gov": gov,
        "historical": historical,
        "excluded_b10b_paths": sorted(b10b_paths),
        "excluded_b11_paths": sorted(b11_paths),
        "excluded_gov_paths": sorted(gov_paths),
        "excluded_b08_paths": sorted(b08_paths),
        "excluded_p02_paths": sorted(p02_paths),
        "historical_pass": historical_pass,
        "historical_advisory": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run fail-closed current-main scope composition.")
    parser.add_argument("--json", action="store_true", help="emit the complete report as JSON")
    args = parser.parse_args(argv)
    report = check_scope()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "status={status} b10b={b10b_status} gov={gov_status} "
            "excluded_b10b={excluded_b10b_paths} excluded_gov={excluded_gov_paths} "
            "historical_pass={historical_pass} historical_advisory={historical_advisory}".format(
                status=report["status"],
                b10b_status=report["b10b"].get("status"),
                gov_status=report["gov"].get("status"),
                excluded_b10b_paths=report["excluded_b10b_paths"],
                excluded_gov_paths=report["excluded_gov_paths"],
                historical_pass=report["historical_pass"],
                historical_advisory=report["historical_advisory"],
            )
        )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
