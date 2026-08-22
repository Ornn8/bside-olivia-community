"""Verify that Issue #4 changes stay inside the P01 allow-list."""

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
CANONICAL_BASE = "d3ea613a61afa83cafb53088da8122ae7941fb4a"
P02_01_EXACT = frozenset(
    {
        "contracts/persona_v2.schema.json",
        "tests/persona/test_persona_v2_schema.py",
    }
)
ALLOWED = frozenset(
    {
        ".gitignore",
        "contracts/llm_config.schema.json",
        "contracts/persona_config.schema.json",
        "contracts/persona_provenance.schema.json",
        "contracts/persona_style_features.schema.json",
        "docs/P01_PERSONA_EVIDENCE.md",
        "docs/B03_LLM_GATEWAY.md",
        "docs/STATUS.md",
        "llm_config.example.json",
        "llm_gateway.py",
        "local_server.py",
        "linli_character/persona_config.json",
        "linli_character/persona_template.md",
        "linli_character/provenance.json",
        "linli_character/style_features.json",
        "persona_provider.py",
        "tests/persona/__init__.py",
        "tests/persona/fixtures/dev_cases.json",
        "tests/persona/fixtures/holdout_cases.json",
        "tests/persona/fixtures/review_cases.json",
        "tests/persona/test_persona_evaluator.py",
        "tests/persona/test_persona_provider.py",
        "tools/persona_evaluator.py",
        "tools/verify_p01_scan.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_b04_scope.py",
        "tools/verify_p01_scope.py",
    }
) | P02_01_EXACT


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


def current_p01_paths() -> frozenset[str]:
    """Return only current paths owned by the P01 exact boundary."""

    from tools.scope_compat import effective_scope_base

    changed = _git("diff", "--name-only", effective_scope_base(CANONICAL_BASE), "--")
    status = _git("status", "--short", "--untracked-files=all")
    status_paths = [line[3:] for line in status if len(line) >= 4]
    return frozenset(path for path in [*changed, *status_paths] if path in ALLOWED)


def _verified_b07_paths(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Exclude a later tranche only after its own scope check passes."""

    try:
        from tools.verify_B07_scope import check_scope as check_b07_scope
    except ImportError:
        return frozenset(), True
    from tools.verify_b05_scope import current_b05_paths
    from tools.verify_b06_scope import current_b06_paths
    from tools.verify_b08_scope import current_b08_paths
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.verify_gov_scope import current_gov_paths
    from tools.scope_compat import current_b11_paths
    b10b_paths, b10b_failed = _verified_b10b_paths()
    if b10b_failed:
        return frozenset(), True

    report = check_b07_scope(
        excluded=(
            excluded
            | current_b05_paths()
            | current_b06_paths()
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


def _verified_b06_paths(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Exclude the later B06 tranche only after its own scope check passes."""

    try:
        from tools.verify_b05_scope import current_b05_paths
        from tools.verify_B07_scope import current_b07_paths
        from tools.verify_b06_scope import check_scope as check_b06_scope
        from tools.verify_b08_scope import current_b08_paths
        from tools.verify_B10A_scope import current_b10a_paths
        from tools.verify_gov_scope import current_gov_paths
        from tools.scope_compat import current_b11_paths
    except ImportError:
        return frozenset(), True
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
    excluded: frozenset[str] = frozenset(),
    *,
    child_mode: bool = False,
) -> dict[str, object]:
    base = _git("rev-parse", CANONICAL_BASE)[0]
    head = _git("rev-parse", "HEAD")[0]
    merge_base = _git("merge-base", CANONICAL_BASE, "HEAD")[0]
    from tools.scope_compat import effective_scope_base

    changed = _git("diff", "--name-only", effective_scope_base(CANONICAL_BASE), "--")
    status_lines = _git("status", "--short", "--untracked-files=all")
    status_paths = [line[3:] for line in status_lines if len(line) >= 4]
    if child_mode:
        b10b_paths, b10b_failed = frozenset(), False
        b02_paths, b02_failed = frozenset(), False
        safe_excluded = frozenset(excluded)
        gov_paths = frozenset()
        gov_failed = False
        b06_paths, b06_failed = frozenset(), False
        b07_paths, b07_failed = frozenset(), False
        from tools.verify_b05_scope import current_b05_paths

        b05_paths = frozenset()
        b11_paths = frozenset()
        b11_failed = False
    else:
        b10b_paths, b10b_failed = _verified_b10b_paths()
        from tools.scope_compat import verified_b02_paths

        b02_paths, b02_failed = verified_b02_paths(
            frozenset(excluded) | b10b_paths | current_p01_paths()
        )
        from tools.verify_B10A_scope import current_b10a_paths
        b10a_paths = current_b10a_paths()
        safe_excluded = frozenset(excluded) | b10b_paths | b10a_paths | b02_paths
        safe_excluded = safe_excluded - GOV_PATHS
        gov_paths, gov_failed = verified_gov_paths(safe_excluded)
        from tools.verify_b05_scope import current_b05_paths

        child_excluded = safe_excluded | gov_paths | b10b_paths
        b06_paths, b06_failed = _verified_b06_paths(child_excluded)
        b07_paths, b07_failed = _verified_b07_paths(child_excluded)
        b05_paths = current_b05_paths()
        from tools.scope_compat import b11_child_exclusions, verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths(
            b11_child_exclusions(frozenset(safe_excluded) | current_p01_paths())
        )
        if not b11_failed:
            safe_excluded = safe_excluded | b11_paths
    excluded_paths = safe_excluded | gov_paths | b10b_paths | b05_paths | b06_paths | b07_paths
    unexpected = sorted(
        {
            path
            for path in [*changed, *status_paths]
            if path not in ALLOWED and path not in excluded_paths
        }
    )
    return {
        "status": "PASS"
        if (
            not unexpected
            and merge_base == base
            and not gov_failed
            and not b06_failed
            and not b07_failed
            and not b10b_failed
            and not b02_failed
            and not b11_failed
        )
        else "FAIL",
        "changed_paths": sorted(set(changed)),
        "status_paths": sorted(set(status_paths)),
        "unexpected_paths": unexpected,
        "allowlist_size": len(ALLOWED),
        "canonical_base": base,
        "head": head,
        "base_is_ancestor": merge_base == base,
        "excluded_b07_paths": sorted(b07_paths),
        "composed_b07": not b07_failed,
        "excluded_paths": sorted(excluded_paths),
        "excluded_b06_paths": sorted(b06_paths),
        "composed_b06": not b06_failed,
        "child_mode": child_mode,
        "excluded_gov_paths": sorted(gov_paths),
        "composed_gov": not gov_failed,
        "composed_b10b": not b10b_failed,
        "excluded_b10b_paths": sorted(b10b_paths),
        "b02_scope_pass": not b02_failed,
        "excluded_b02_paths": sorted(b02_paths),
        "excluded_b11_paths": sorted(b11_paths),
        "composed_b11": not b11_failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the historical P01 file boundary.")
    parser.add_argument(
        "--composed-b05",
        action="store_true",
        help="explicitly exclude only paths owned by the independently checked B05 scope",
    )
    args = parser.parse_args()
    excluded = frozenset()
    if args.composed_b05:
        from tools.verify_b05_scope import current_b05_paths

        excluded = current_b05_paths()
    report = check_scope(excluded=excluded)
    print(
        "status={status} canonical_base={canonical_base} head={head} "
        "base_is_ancestor={base_is_ancestor} allowlist_size={allowlist_size} "
        "changed={changed_paths} status_paths={status_paths} "
        "unexpected={unexpected_paths} excluded={excluded_paths} "
        "composed_b07={composed_b07} composed_b06={composed_b06}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
