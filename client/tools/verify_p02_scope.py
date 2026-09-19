"""Verify the P02-02 Constitution/provenance asset boundary."""

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


P02_02_BASELINE = "27d001ccd6ed17e8a39c776e03d8946631858133"
P02_02_EXACT = frozenset(
    {
        "contracts/persona_v2_provenance.schema.json",
        "linli_character/persona_v2.json",
        "linli_character/provenance_v2.json",
        "tests/persona/test_persona_v2_assets.py",
        "tools/verify_p02_scope.py",
    }
)
P02_02_SHARED = frozenset({".gitignore"})


def is_p02_path(path: str) -> bool:
    return path in P02_02_EXACT


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


def _status_paths() -> tuple[str, ...]:
    paths: list[str] = []
    for line in _git("status", "--short", "--untracked-files=all"):
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        paths.append(path)
    return tuple(paths)


def current_p02_paths() -> frozenset[str]:
    from tools.scope_compat import effective_scope_base

    comparison_base = effective_scope_base(P02_02_BASELINE, resolver=_git)
    committed = _git("diff", "--name-only", comparison_base, "HEAD", "--")
    working = _git("diff", "--name-only", "HEAD", "--")
    return frozenset(
        path
        for path in [*committed, *working, *_status_paths()]
        if is_p02_path(path)
    )


def check_scope(
    *,
    base: str = P02_02_BASELINE,
    head: str = "HEAD",
    excluded: frozenset[str] = frozenset(),
    child_mode: bool = False,
) -> dict[str, Any]:
    del child_mode
    try:
        from tools.scope_compat import effective_scope_base

        base_commit = _git("rev-parse", base)[0]
        head_commit = _git("rev-parse", head)[0]
        merge_base = _git("merge-base", base_commit, head_commit)[0]
        comparison_base = _git(
            "rev-parse",
            effective_scope_base(
                base,
                head,
                use_ci_diff=base == P02_02_BASELINE,
                resolver=_git,
            ),
        )[0]
        committed = _git("diff", "--name-only", comparison_base, head_commit, "--")
        working = _git("diff", "--name-only", head_commit, "HEAD", "--")
        status_paths = _status_paths()
    except (IndexError, OSError, subprocess.CalledProcessError) as exc:
        return {
            "status": "FAIL",
            "error": f"git verification failed: {type(exc).__name__}",
            "scope_paths": [],
            "unexpected_paths": [],
        }

    all_paths = sorted(set([*committed, *working, *status_paths]))
    excluded_paths = frozenset(excluded) | P02_02_SHARED
    scope_paths = sorted(path for path in all_paths if is_p02_path(path))
    unexpected = sorted(
        path
        for path in all_paths
        if not is_p02_path(path) and path not in excluded_paths
    )
    return {
        "status": "PASS"
        if merge_base == base_commit and not unexpected
        else "FAIL",
        "base": base_commit,
        "comparison_base": comparison_base,
        "head": head_commit,
        "base_is_ancestor": merge_base == base_commit,
        "changed_paths": sorted(set([*committed, *working])),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": scope_paths,
        "unexpected_paths": unexpected,
        "excluded_paths": sorted(excluded_paths),
        "allowlist_exact": sorted(P02_02_EXACT),
        "shared_paths": sorted(P02_02_SHARED),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the P02-02 asset boundary.")
    parser.add_argument("--base", default=P02_02_BASELINE)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    report = check_scope(base=args.base, head=args.head)
    print(
        "status={status} base={base} head={head} base_is_ancestor={base_is_ancestor} "
        "scope_paths={scope_paths} unexpected={unexpected_paths}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
