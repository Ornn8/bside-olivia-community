"""Verify that B06 local-TTS changes stay inside the accepted tranche."""

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
# B06 is a tranche on top of the accepted main commit. Comparing only with
# HEAD makes a clean committed checkout look unchanged and breaks downstream
# scope composition (P01 -> B10A).
B06_BASELINE = "0ddfa2816b85df57561bb1ad661d0f3c61e0e98c"
ALLOWED_PATHS = frozenset(
    {
        ".gitignore",
        ".github/workflows/required-ci.yml",
        "docs/B06_LOCAL_TTS_ACCEPTANCE.md",
        "tests/packaging/test_B06_scope.py",
        "tests/tts/test_tts.py",
        "tools/tts_acceptance.py",
        "tools/tts_asr_probe.py",
        "tools/tts_cli.py",
        "tools/tts_constructor_probe.py",
        "tools/tts_hyper_probe.py",
        "tools/tts_load_phased_probe.py",
        "tools/tts_load_probe.py",
        "tools/verify_b02_scope.py",
        "tools/verify_b06_scope.py",
        "tools/verify_p01_scope.py",
        "tts/__init__.py",
        "tts/audio.py",
        "tts/contracts.py",
        "tts/profiles.py",
        "tts/providers.py",
        "tts/registry.py",
        "tts/sentence.py",
        "tts/service.py",
    }
)
B06_BRIDGE_SUPPORT_PATHS = frozenset(
    {
        "tests/tts/test_external_adapter.py",
        "tts/external_cosyvoice_worker.py",
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
        status_code = line[:2]
        value = line[3:]
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        # A failed managed-worktree index refresh can leave a stale stat entry.
        # Content that hashes exactly to HEAD is not a dirty B06 path; actual
        # content changes are covered by the baseline diff below.
        if status_code != "??" and (ROOT / value).is_file():
            try:
                head_hash = _git("rev-parse", f"HEAD:{value}").strip()
                worktree_hash = _git("hash-object", "--", value).strip()
            except subprocess.CalledProcessError:
                head_hash = ""
                worktree_hash = ""
            if head_hash and head_hash == worktree_hash:
                continue
        paths.append(value)
    return paths


def current_b06_paths() -> frozenset[str]:
    """Return current-main paths owned by the B06 allow-list."""

    from tools.scope_compat import effective_scope_base

    changed = [
        path for path in _git("diff", "--name-only", effective_scope_base(B06_BASELINE), "--").splitlines() if path
    ]
    owned = ALLOWED_PATHS | B06_BRIDGE_SUPPORT_PATHS
    return frozenset(path for path in [*changed, *_status_paths()] if path in owned)


def _default_exclusions() -> frozenset[str]:
    """Hide independently owned B05/B07 paths for a direct B06 scan.

    The child scopes remain responsible for validating their own boundaries;
    this only makes the baseline-aware B06 scanner usable on current main.
    Explicit callers provide the already-validated exclusions themselves.
    """

    from tools.verify_B07_scope import current_b07_paths
    from tools.verify_b05_scope import current_b05_paths
    from tools.verify_b10b_scope import check_scope as check_b10b_scope
    from tools.scope_compat import current_b11_paths

    b10b_report = check_b10b_scope()
    b10b_paths = frozenset(b10b_report["scope_paths"]) if b10b_report["status"] == "PASS" else frozenset()
    return current_b05_paths() | current_b07_paths() | b10b_paths | current_b11_paths()


def _verified_gov_exclusions(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    from tools.verify_gov_scope import current_gov_paths, check_scope as check_gov_scope
    from tools.verify_B10A_scope import current_b10a_paths
    from tools.scope_compat import current_b11_paths

    gov_paths = current_gov_paths()
    report = check_gov_scope(
        excluded=(
            frozenset(excluded)
            | gov_paths
            | current_b06_paths()
            | current_b10a_paths()
            | current_b11_paths()
        ),
        child_mode=True,
    )
    if report["status"] != "PASS":
        return frozenset(), True
    return gov_paths, False


def _check_scope(
    excluded: frozenset[str] | None = None,
    *,
    child_mode: bool = False,
) -> dict[str, object]:
    composed = excluded is None and not child_mode
    if excluded is None:
        excluded = frozenset() if child_mode else _default_exclusions()
    if child_mode:
        b10b_failed = False
        b10b_paths = frozenset()
        verified_later_paths = frozenset()
        gov_paths, gov_failed = _verified_gov_exclusions(excluded)
    else:
        from tools.verify_b10b_scope import check_scope as check_b10b_scope

        b10b_report = check_b10b_scope()
        b10b_failed = b10b_report["status"] != "PASS"
        b10b_paths = frozenset(b10b_report.get("scope_paths", [])) if not b10b_failed else frozenset()
        verified_later_paths = (
            frozenset(b10b_report.get("b08_paths", [])) if not b10b_failed else frozenset()
        )
        from tools.verify_B10A_scope import current_b10a_paths
        excluded = frozenset(excluded) | current_b10a_paths()
        excluded = (frozenset(excluded) | b10b_paths | verified_later_paths) - GOV_PATHS
        gov_paths, gov_failed = verified_gov_paths(excluded)
    b11_paths = frozenset()
    b11_failed = False
    excluded = (
        frozenset(excluded) | b10b_paths | verified_later_paths | gov_paths
        if child_mode
        else frozenset(excluded) | b10b_paths | verified_later_paths | gov_paths
    )
    if not child_mode:
        from tools.scope_compat import b11_child_exclusions, verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths(
            b11_child_exclusions(frozenset(excluded) | current_b06_paths())
        )
        if not b11_failed:
            # B11 documentation overlaps the governance tranche.  A failed
            # GOV child must never donate those paths through that overlap.
            trusted_b11_paths = b11_paths - GOV_PATHS if gov_failed else b11_paths
            excluded = frozenset(excluded) | trusted_b11_paths
    from tools.scope_compat import effective_scope_base

    changed = [
        path
        for path in _git(
            "diff", "--name-only", effective_scope_base(B06_BASELINE, use_ci_diff=child_mode), "--"
        ).splitlines()
        if path
    ]
    status_paths = _status_paths()
    all_paths = sorted(set(changed) | set(status_paths))
    allowed_paths = ALLOWED_PATHS | (B06_BRIDGE_SUPPORT_PATHS if child_mode else frozenset())
    unexpected = sorted(
        path for path in all_paths if path not in allowed_paths and path not in excluded
    )
    media_paths = sorted(
        path
        for path in all_paths
        if path not in excluded
        and (
            Path(path).suffix.casefold() in MEDIA_SUFFIXES
            or path.casefold().startswith(".evidence/")
        )
    )
    scope_paths = sorted(path for path in all_paths if path in allowed_paths and path not in excluded)
    return {
        "status": "PASS"
        if not unexpected and not media_paths and not gov_failed and not b10b_failed and not b11_failed
        else "FAIL",
        "changed_paths": sorted(set(changed)),
        "status_paths": sorted(set(status_paths)),
        "scope_paths": scope_paths,
        "unexpected_paths": unexpected,
        "media_paths": media_paths,
        "baseline": B06_BASELINE,
        "allowlist_size": len(ALLOWED_PATHS),
        "excluded_paths": sorted(excluded),
        "composed": composed,
        "composed_gov": not gov_failed,
        "composed_b10b": not b10b_failed,
        "child_mode": child_mode,
        "excluded_b11_paths": sorted(b11_paths),
        "composed_b11": not b11_failed,
    }


def check_scope(
    excluded: frozenset[str] | None = None,
    *,
    child_mode: bool = False,
) -> dict[str, object]:
    """Check the standalone accepted tranche or a current-PR child tranche."""

    from tools.scope_compat import scope_ci_diff_mode

    with scope_ci_diff_mode(child_mode):
        return _check_scope(excluded=excluded, child_mode=child_mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the B06 local-TTS file boundary.")
    parser.parse_args(argv)
    report = check_scope()
    print(
        "status={status} baseline={baseline} changed={changed_paths} "
        "status_paths={status_paths} unexpected={unexpected_paths} "
        "media={media_paths} allowlist_size={allowlist_size}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
