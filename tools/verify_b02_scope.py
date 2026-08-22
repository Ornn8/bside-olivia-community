"""Verify that B02 does not mutate tracked files outside its allow-list."""

from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.verify_gov_scope import GOV_PATHS, verified_gov_paths

B02_BASELINE = "70247b03d074d7b9e4e259c22c2d270cbd5b5c53"
B02_CONTRACT_PATHS = frozenset(
    {
        "INSTALL.cmd",
        "START.cmd",
        "UNINSTALL.cmd",
        "contracts/http_contract.example.json",
        "contracts/http_contract.schema.json",
        "docs/B02_ERROR_CODES.md",
        "docs/B02_HTTP_CONTRACT.md",
        "docs/WINDOWS_FULL_PATCH.md",
        "http_contract.py",
        "installer/Install.ps1",
        "installer/__init__.py",
        "installer/__main__.py",
        "installer/configure.py",
        "installer/full-patch-manifest.json",
        "installer/full_patch.py",
        "installer/runtime-requirements.txt",
        "installer/start_local.py",
        "installer/uninstall.py",
        "latentsync_reply.py",
        "letter_triage.py",
        "local_server.py",
        "music_duration.py",
        "music_renderer.py",
        "music_reply.py",
        "patch_feapp.py",
        "pyproject.toml",
        "reply_delivery.py",
        "reply_media.py",
        "song_content.py",
        "tests/http/test_contract.py",
        "tests/http/test_letter_triage_portable.py",
        "tests/http/test_reply_delay_and_media.py",
        "tests/installer/test_windows_full_patch.py",
        "tests/media/test_portable_media_boundaries.py",
        "tests/test_baseline_hardening.py",
        "requirements-ci.txt",
        "requirements-dev.txt",
        "tools/minimax_music3_worker.py",
        "tools/Install-ThirdParty.ps1",
        "tools/verify_b02_scope.py",
        "tts/delivery.py",
    }
)

# New B02 files are allowed to be absent from the starting commit.  The small
# shared OPS allowlist lets this scope check run before the OPS commit exists;
# existing files outside this set must remain byte-identical to HEAD.
ALLOWED_MUTATIONS = frozenset(
    {
        ".gitignore",
        ".github/CODEOWNERS",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/workflows/required-ci.yml",
        "INSTALL.cmd",
        "START.cmd",
        "UNINSTALL.cmd",
        "README.md",
        "local_server.py",
        "http_contract.py",
        "tools/healthcheck.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b02_scope.py",
        "tools/verify_B10A_scope.py",
        "tools/verify_b04_scope.py",
        "contracts/http_contract.example.json",
        "contracts/http_contract.schema.json",
        "contracts/legacy_letter_import.example.json",
        "contracts/legacy_letter_import.schema.json",
        "contracts/music_fixture.json",
        "contracts/llm_config.schema.json",
        "docs/B02_COVERAGE.md",
        "docs/B02_ERROR_CODES.md",
        "docs/B02_HTTP_CONTRACT.md",
        "docs/WINDOWS_FULL_PATCH.md",
        "docs/B03_LLM_GATEWAY.md",
        "docs/B04_LOCAL_MEMORY.md",
        "docs/GITHUB_MIGRATION.md",
        "docs/MASTER_PLAN.md",
        "docs/OPS_REQUIRED_CI.md",
        "docs/STATUS.md",
        "llm_config.example.json",
        "requirements-ci.txt",
        "requirements-dev.txt",
        "llm_gateway.py",
        "local_memory.py",
        "memory_config.example.json",
        "memory_import.py",
        "memory_port.py",
        "memory_prompt.py",
        "persona_provider.py",
        "installer/Install.ps1",
        "installer/__init__.py",
        "installer/__main__.py",
        "installer/configure.py",
        "installer/full-patch-manifest.json",
        "installer/full_patch.py",
        "installer/runtime-requirements.txt",
        "installer/start_local.py",
        "installer/uninstall.py",
        "latentsync_reply.py",
        "letter_triage.py",
        "music_duration.py",
        "music_renderer.py",
        "music_reply.py",
        "patch_feapp.py",
        "pyproject.toml",
        "reply_delivery.py",
        "reply_media.py",
        "song_content.py",
        "reply_orchestrator.py",
        "contracts/memory_config.schema.json",
        "tests/http/test_contract.py",
        "tests/http/test_letter_triage_portable.py",
        "tests/http/test_reply_delay_and_media.py",
        "tests/installer/test_windows_full_patch.py",
        "tests/media/test_portable_media_boundaries.py",
        "tests/test_baseline_hardening.py",
        "tests/llm/test_b03_integration.py",
        "tests/llm/test_b04_memory_integration.py",
        "tests/llm/test_gateway.py",
        "tests/llm/test_orchestrator.py",
        "tests/memory/test_local_memory.py",
        "tests/governance/test_project_status.py",
        "tests/packaging/test_B10A_runtime.py",
        "runtime/packaging/b10a/manager.py",
        "tools/minimax_music3_worker.py",
        "tools/Install-ThirdParty.ps1",
        "tools/verify_project_status.py",
        "tts/delivery.py",
    }
)
BASELINE_PATH = "tests/test_baseline_hardening.py"


@lru_cache(maxsize=None)
def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_b02_paths(
    base: str = B02_BASELINE,
    head: str = "HEAD",
) -> frozenset[str]:
    """Return only the current B02 public-contract ownership candidates."""

    from tools.scope_compat import effective_scope_base

    comparison_base = effective_scope_base(base, head)
    committed = _git("diff", "--name-only", comparison_base, head, "--").splitlines()
    working = _git("diff", "--name-only", head, "--").splitlines()
    status = [
        line[3:]
        for line in _git("status", "--short", "--untracked-files=all").splitlines()
        if len(line) >= 4
    ]
    return frozenset(
        path for path in [*committed, *working, *status] if path in B02_CONTRACT_PATHS
    )


def _verified_b10b_paths() -> tuple[frozenset[str], bool]:
    from tools.verify_b10b_scope import check_scope as check_b10b_scope

    report = check_b10b_scope()
    if report["status"] != "PASS":
        return frozenset(), True
    return frozenset(report.get("composed_paths", report["scope_paths"])), False


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


def _verified_b05_paths(excluded: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Exclude the B05 tranche only after its own scope check passes."""

    try:
        from tools.verify_b05_scope import check_scope as check_b05_scope, current_b05_paths
        from tools.verify_B07_scope import current_b07_paths
        from tools.verify_b06_scope import current_b06_paths
        from tools.verify_b08_scope import current_b08_paths
        from tools.verify_B10A_scope import current_b10a_paths
        from tools.verify_gov_scope import current_gov_paths
        from tools.scope_compat import current_b11_paths
        from tools.scope_compat import current_b11_paths
    except ImportError:
        return frozenset(), True
    b10b_paths, b10b_failed = _verified_b10b_paths()
    if b10b_failed:
        return frozenset(), True
    report = check_b05_scope(
        excluded=(
            excluded
            | current_b06_paths()
            | current_b07_paths()
            | current_b08_paths()
            | current_b10a_paths()
            | current_gov_paths()
            | current_b11_paths()
            | b10b_paths
        ),
        compose_b07=False,
        child_mode=True,
    )
    if report["status"] != "PASS":
        return frozenset(), True
    return current_b05_paths(), False


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
    root: Path = ROOT,
    excluded: frozenset[str] = frozenset(),
    *,
    child_mode: bool = False,
) -> dict[str, object]:
    if child_mode:
        b10b_paths, b10b_failed = frozenset(), False
        safe_excluded = frozenset(excluded)
        # Explicit child mode is a direct boundary check.  Parent composition
        # has already independently verified sibling GOV paths; re-entering
        # GOV here would make the child validate its own B02 edits as GOV.
        gov_paths = frozenset()
        gov_failed = False
        b05_paths, b05_failed = frozenset(), False
        b06_paths, b06_failed = frozenset(), False
        b07_paths, b07_failed = frozenset(), False
        b11_paths, b11_failed = frozenset(), False
    else:
        b10b_paths, b10b_failed = _verified_b10b_paths()
        safe_excluded = (frozenset(excluded) | b10b_paths) - GOV_PATHS
        gov_paths, gov_failed = verified_gov_paths(safe_excluded)
        b05_paths, b05_failed = _verified_b05_paths(safe_excluded)
        b06_paths, b06_failed = _verified_b06_paths(safe_excluded)
        b07_paths, b07_failed = _verified_b07_paths(safe_excluded)
        from tools.scope_compat import b11_child_exclusions, verified_b11_paths

        b11_paths, b11_failed = verified_b11_paths(
            b11_child_exclusions(frozenset(safe_excluded))
        )
        if not b11_failed:
            safe_excluded = safe_excluded | b11_paths
    excluded_paths = safe_excluded | gov_paths | b10b_paths | b05_paths | b06_paths | b07_paths
    current = [path for path in _git("ls-files").splitlines() if path]
    head_tracked = [path for path in _git("ls-tree", "-r", "--name-only", "HEAD").splitlines() if path]
    # During a cherry-pick, ``ls-files`` includes staged additions that are not
    # addressable as ``HEAD:path`` yet.  They remain checked below through the
    # HEAD diff/status paths; only HEAD-tracked paths belong in the byte-equality
    # loop.
    outside = [
        path
        for path in current
        if path in head_tracked and path not in ALLOWED_MUTATIONS and path not in excluded_paths
    ]
    current = [path for path in _git("ls-files").splitlines() if path]
    head_tracked = [path for path in _git("ls-tree", "-r", "--name-only", "HEAD").splitlines() if path]
    outside = [
        path
        for path in current
        if path in head_tracked and path not in ALLOWED_MUTATIONS and path not in excluded_paths
    ]
    mismatches: list[str] = []
    deleted = [
        path
        for path in head_tracked
        if path not in ALLOWED_MUTATIONS and path not in excluded_paths and not (root / path).exists()
    ]
    mismatches.extend(deleted)
    checked = 0
    for relative in outside:
        worktree_path = root / relative
        if not worktree_path.exists():
            mismatches.append(relative)
            continue
        head_hash = _git("rev-parse", f"HEAD:{relative}")
        worktree_hash = _git("hash-object", "--", relative)
        checked += 1
        if head_hash != worktree_hash:
            mismatches.append(relative)

    baseline_exact = False
    if BASELINE_PATH in current and (root / BASELINE_PATH).exists():
        baseline_exact = (
            _git("rev-parse", f"HEAD:{BASELINE_PATH}")
            == _git("hash-object", "--", BASELINE_PATH)
        )
        if not baseline_exact and BASELINE_PATH not in mismatches:
            mismatches.append(BASELINE_PATH)

    changed_paths = [path for path in _git("diff", "--name-only", "HEAD", "--").splitlines() if path]
    unexpected_diff_paths = [
        path for path in changed_paths if path not in ALLOWED_MUTATIONS and path not in excluded_paths
    ]
    mismatches.extend(path for path in unexpected_diff_paths if path not in mismatches)

    status_lines = [
        line
        for line in _git("status", "--short", "--untracked-files=no").splitlines()
        if line
    ]
    status_paths = [
        line.strip().split(maxsplit=1)[1]
        for line in status_lines
        if len(line.strip().split(maxsplit=1)) == 2
    ]
    unexpected_status_paths = [
        path for path in status_paths if path not in ALLOWED_MUTATIONS and path not in excluded_paths
    ]
    mismatches.extend(path for path in unexpected_status_paths if path not in mismatches)

    return {
        "status": "PASS"
        if (
            not mismatches
            and baseline_exact
            and not gov_failed
            and not b05_failed
            and not b06_failed
            and not b07_failed
            and not b10b_failed
            and not b11_failed
        )
        else "FAIL",
        "checked_outside_allowlist": checked,
        "allowlist_size": len(ALLOWED_MUTATIONS),
        "mismatches": mismatches,
        "unexpected_diff_paths": unexpected_diff_paths,
        "unexpected_status_paths": unexpected_status_paths,
        "baseline_exact": baseline_exact,
        "baseline_sha256": _sha256(root / BASELINE_PATH) if baseline_exact else None,
        "excluded_b07_paths": sorted(b07_paths),
        "composed_b07": not b07_failed,
        "excluded_gov_paths": sorted(gov_paths),
        "composed_gov": not gov_failed,
        "excluded_b05_paths": sorted(b05_paths),
        "composed_b05": not b05_failed,
        "excluded_b06_paths": sorted(b06_paths),
        "composed_b06": not b06_failed,
        "excluded_paths": sorted(excluded_paths),
        "excluded_b10b_paths": sorted(b10b_paths),
        "composed_b10b": not b10b_failed,
        "excluded_b11_paths": sorted(b11_paths),
        "composed_b11": not b11_failed,
        "child_mode": child_mode,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check B02 file-scope integrity.")
    parser.add_argument(
        "--composed-b05",
        action="store_true",
        help="explicitly exclude only paths owned by the independently checked B05 scope",
    )
    args = parser.parse_args(argv)
    excluded = frozenset()
    if args.composed_b05:
        from tools.verify_b05_scope import current_b05_paths

        excluded = current_b05_paths()
    report = check_scope(excluded=excluded)
    print(
        "status={status} checked_outside_allowlist={checked_outside_allowlist} "
        "baseline_exact={baseline_exact} baseline_sha256={baseline_sha256} "
        "unexpected_status_paths={unexpected_status_paths} mismatches={mismatches}".format(**report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
