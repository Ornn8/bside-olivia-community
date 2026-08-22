"""Run hardening scans over the fixed-base diff and current worktree files."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_BASE = "d3ea613a61afa83cafb53088da8122ae7941fb4a"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_hardening_scan as base_scan  # noqa: E402


def _git(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [line for line in result.stdout.splitlines() if line]


def _status_paths() -> list[str]:
    paths: list[str] = []
    for line in _git("status", "--short", "--untracked-files=all"):
        if len(line) >= 4:
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[-1]
            paths.append(path)
    return paths


def worktree_files(root: Path = ROOT) -> list[Path]:
    names = set(_git("ls-files"))
    names.update(_git("diff", "--name-only", CANONICAL_BASE, "--"))
    names.update(_status_paths())
    return sorted(
        (root / Path(name) for name in names if (root / Path(name)).is_file()),
        key=lambda path: path.as_posix(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--mode",
        action="append",
        choices=("all", *base_scan.MODES),
        help="select one or more scan modes; default is all modes",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    selected = (
        base_scan.MODES
        if not args.mode or "all" in args.mode
        else tuple(dict.fromkeys(args.mode))
    )

    try:
        files = worktree_files(root)
        print("p01_hardening_scan=1")
        print(f"selected_modes={','.join(selected)}")
        print(f"worktree_files={len(files)}")
        all_findings: list[str] = []
        if "comments" in selected:
            findings, patterns, checked = base_scan.scan_runtime_comments(root, files)
            base_scan.emit_section("worktree_runtime_comments", patterns, findings)
            print(f"runtime_python_files_checked={checked}")
            all_findings.extend(findings)
        if "runtime-dependencies" in selected:
            findings, patterns, checked = base_scan.scan_runtime_dependencies(root, files)
            base_scan.emit_section("worktree_runtime_dependencies", patterns, findings)
            print(f"runtime_python_files_checked={checked}")
            all_findings.extend(findings)
        if "secrets" in selected:
            findings, patterns = base_scan.scan_secrets(root, files)
            base_scan.emit_section("worktree_secret_markers", patterns, findings)
            all_findings.extend(findings)
        if "sensitive-paths" in selected:
            findings, patterns = base_scan.scan_sensitive_paths(root, files)
            base_scan.emit_section("worktree_sensitive_paths", patterns, findings)
            all_findings.extend(findings)
        if "large-files" in selected:
            findings = base_scan.scan_large_files(root, files)
            print("[worktree_large_files]")
            print(f"threshold_bytes={base_scan.MAX_TRACKED_FILE_BYTES}")
            print(f"matches={len(findings)}")
            for finding in findings:
                print(f"finding={finding}")
            all_findings.extend(findings)
        if "evidence-ignore" in selected:
            findings, probes = base_scan.scan_evidence_ignore(root)
            base_scan.emit_section("evidence_ignore_boundary", [".evidence/**"], findings)
            print(f"evidence_probe_count={len(probes)}")
            print(f"evidence_probes_checked={len(probes) - len(findings)}")
            all_findings.extend(findings)
    except (OSError, UnicodeError, subprocess.SubprocessError, SyntaxError) as exc:
        print(f"status=ERROR:{type(exc).__name__}")
        return 2

    status = "PASS" if not all_findings else "FAIL"
    print(f"status={status}")
    print(f"exit_code={0 if not all_findings else 1}")
    return 0 if not all_findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
