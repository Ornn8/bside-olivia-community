"""Verify that B07 changes stay inside the original-visual driver tranche."""

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
# B07 is a tranche on top of the accepted main commit.  Comparing only with
# HEAD makes a clean committed checkout look unchanged and breaks downstream
# scope composition (P01 -> B10A).
B07_BASELINE = "0ddfa2816b85df57561bb1ad661d0f3c61e0e98c"
ALLOWED_PATHS = frozenset(
    {
        ".gitignore",
        "contracts/visual_driver.example.json",
        "contracts/visual_driver.schema.json",
        "docs/B07_VISUAL_DRIVER.md",
        "tests/live_driver/__init__.py",
        "tests/live_driver/test_B07_scope.py",
        "tests/live_driver/test_visual_driver.py",
        "tools/verify_p01_scope.py",
        "tools/verify_B07_scope.py",
        "tools/verify_b02_scope.py",
        "tools/visual_compare.py",
        "tools/visual_driver.py",
        "visual_driver/__init__.py",
        "visual_driver/contracts.py",
        "visual_driver/engine.py",
    }
)
MEDIA_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".wav", ".mp3", ".flac"}
)


@lru_cache(maxsize=None)
def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _status_paths() -> list[str]:
    paths: list[str] = []
    for line in _git("status", "--short", "--untracked-files=all").splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        # Git uses ``old -> new`` for a rename.  Neither side is expected in
        # this tranche, but report the destination without exposing contents.
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        paths.append(value)
    return paths


def current_b07_paths() -> frozenset[str]:
    from tools.scope_compat import effective_scope_base

    paths = [
        path for path in _git("diff", "--name-only", effective_scope_base(B07_BASELINE), "--").splitlines() if path
    ]
    paths.extend(_status_paths())
    return frozenset(path for path in paths if path in ALLOWED_PATHS)


def _verified_b05_paths() -> tuple[frozenset[str], bool]:
    """Return B05-owned paths only after the B05 child verifier passes."""

    from tools.verify_b05_scope import check_scope as check_b05_scope, current_b05_paths
    from tools.verify_b06_scope import current_b06_paths
    from tools.verify_b10b_scope import check_scope as check_b10b_scope
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.scope_compat import current_b11_paths

    b10b_report = check_b10b_scope()
    if b10b_report["status"] != "PASS":
        return frozenset(), True
    b10b_paths = frozenset(b10b_report["scope_paths"])
    from tools.verify_b08_scope import is_b08_path

    verified_b08_paths = frozenset(
        path for path in b10b_report.get("b08_paths", []) if is_b08_path(path)
    )
    report = check_b05_scope(
        excluded=(
            current_b07_paths()
            | current_b06_paths()
            | verified_b08_paths
            | b10b_paths
            | current_b10a_paths()
            | current_b11_paths()
        ),
        compose_b07=False,
    )
    if report["status"] != "PASS":
        return frozenset(), True
    return current_b05_paths(), False


def _check_scope(
    excluded: frozenset[str] | None = None,
    *,
    child_mode: bool = False,
) -> dict[str, object]:
    composition_failed = False
    b06_paths = frozenset()
    b10b_paths = frozenset()
    verified_later_paths = frozenset()
    b11_paths = frozenset()
    b11_failed = False
    if child_mode:
        excluded = frozenset(excluded or ())
    elif excluded is None:
        excluded, composition_failed = _verified_b05_paths()
        if not composition_failed:
            from tools.verify_b06_scope import current_b06_paths
            from tools.verify_b10b_scope import check_scope as check_b10b_scope
            from tools.verify_B10A_scope import current_b10a_paths

            b06_paths = current_b06_paths()
            b10b_report = check_b10b_scope()
            if b10b_report["status"] != "PASS":
                composition_failed = True
            else:
                b10b_paths = frozenset(b10b_report["scope_paths"])
                verified_later_paths = frozenset(b10b_report.get("b08_paths", []))
            excluded = excluded | b06_paths | b10b_paths | verified_later_paths | current_b10a_paths()
    else:
        from tools.verify_b06_scope import current_b06_paths
        from tools.verify_b10b_scope import check_scope as check_b10b_scope
        from tools.verify_B10A_scope import current_b10a_paths

        b06_paths = current_b06_paths()
        b10b_report = check_b10b_scope()
        if b10b_report["status"] != "PASS":
            composition_failed = True
        else:
            b10b_paths = frozenset(b10b_report["scope_paths"])
            verified_later_paths = frozenset(b10b_report.get("b08_paths", []))
        excluded = excluded | b06_paths | b10b_paths | verified_later_paths | current_b10a_paths()
    safe_excluded = frozenset(excluded) if child_mode else frozenset(excluded) - GOV_PATHS
    if child_mode:
        from tools.verify_gov_scope import current_gov_paths, check_scope as check_gov_scope

        gov_paths = current_gov_paths()
        gov_report = check_gov_scope(
            excluded=safe_excluded | gov_paths | current_b07_paths(),
            child_mode=True,
        )
        gov_failed = gov_report["status"] != "PASS"
    else:
        gov_paths, gov_failed = verified_gov_paths(safe_excluded)
    composition_failed = composition_failed or gov_failed
    excluded = safe_excluded | gov_paths
    if not child_mode:
        from tools.scope_compat import b11_child_exclusions, verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths(
            b11_child_exclusions(frozenset(excluded) | current_b07_paths())
        )
        if not b11_failed:
            excluded = frozenset(excluded) | b11_paths
        composition_failed = composition_failed or b11_failed
    from tools.scope_compat import effective_scope_base

    changed = [
        path
        for path in _git(
            "diff", "--name-only", effective_scope_base(B07_BASELINE, use_ci_diff=child_mode), "--"
        ).splitlines()
        if path
    ]
    status_paths = _status_paths()
    all_paths = sorted(set(changed) | set(status_paths))
    unexpected = sorted(
        path for path in all_paths if path not in ALLOWED_PATHS and path not in excluded
    )
    media_paths = sorted(
        path
        for path in all_paths
        if path not in excluded
        and (Path(path).suffix.casefold() in MEDIA_SUFFIXES or path.casefold().startswith(".evidence/"))
    )
    scope_paths = sorted(path for path in all_paths if path in ALLOWED_PATHS)
    return {
        "status": "PASS" if not unexpected and not media_paths and not composition_failed else "FAIL",
        "changed_paths": sorted(set(changed)),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": scope_paths,
        "unexpected_paths": unexpected,
        "media_paths": media_paths,
        "allowlist_size": len(ALLOWED_PATHS),
        "excluded_paths": sorted(excluded),
        "composed_b05": not composition_failed,
        "excluded_b06_paths": sorted(b06_paths),
        "composed_b06": not composition_failed,
        "composed_gov": not gov_failed,
        "composed_b10b": not composition_failed or bool(b10b_paths),
        "excluded_b10b_paths": sorted(b10b_paths),
        "excluded_b11_paths": sorted(b11_paths),
        "composed_b11": not b11_failed,
        "child_mode": child_mode,
    }


def check_scope(
    excluded: frozenset[str] | None = None,
    *,
    child_mode: bool = False,
) -> dict[str, object]:
    """Check the fixed accepted tranche or a current-PR child tranche."""

    from tools.scope_compat import scope_ci_diff_mode

    with scope_ci_diff_mode(child_mode):
        return _check_scope(excluded=excluded, child_mode=child_mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the B07 file boundary.")
    parser.add_argument(
        "--composed-b05",
        action="store_true",
        help="exclude only paths returned by an independently passing B05 scope check",
    )
    args = parser.parse_args(argv)
    if args.composed_b05:
        excluded, child_failed = _verified_b05_paths()
        report = check_scope(excluded=excluded if not child_failed else frozenset())
        if child_failed:
            report["status"] = "FAIL"
            report["composed_b05"] = False
    else:
        report = check_scope()
    print(
        "status={status} changed={changed_paths} status_paths={status_paths} "
        "scope_paths={scope_paths} unexpected={unexpected_paths} media={media_paths} "
        "excluded={excluded_paths} composed_b05={composed_b05} allowlist_size={allowlist_size}".format(
            **report
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
