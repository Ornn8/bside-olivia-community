"""Run reproducible, repository-only B00 hardening scans.

The scanner emits only relative paths, line numbers, pattern labels, counts,
and exit status. It never prints matched source text, configuration values,
request bodies, or file contents.

Exit codes:
    0: selected scans completed with no findings.
    1: selected scans completed and found one or more findings.
    2: invalid arguments or a scanner/environment error.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path


MAX_TRACKED_FILE_BYTES = 50_000_000
MODES = (
    "comments",
    "runtime-dependencies",
    "secrets",
    "sensitive-paths",
    "large-files",
    "evidence-ignore",
    "persona-release",
)

PERSONA_RELEASE_PATTERNS = (
    "private_reference_path",
    "private_state_path",
    "private_communication_path",
    "control_view_instance",
    "continuation_instance",
    "private_nickname_instance",
    "blocked_release_record",
    "long_source_copy",
    "invalid_release_asset",
)
PERSONA_RELEASE_ROOTS = {"linli_character", "persona", "personas", "content"}
PERSONA_RELEASE_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".md"}
PERSONA_RELEASE_TEXT_KEYS = {
    "content",
    "original_text",
    "source_text",
    "statement",
    "summary",
    "text",
}
MAX_PERSONA_RELEASE_TEXT_CHARS = 1_200


COMMENT_PATTERNS = (
    (
        "official_word",
        re.compile(
            r"(?:官方|(?<![A-Za-z])official(?:ly)?(?![A-Za-z])|"
            r"(?<![A-Za-z])offical(?:ly)?(?![A-Za-z]))",
            re.IGNORECASE,
        ),
    ),
    (
        "historical_snapshot_word",
        re.compile(
            r"(?:历史|快照|(?<![A-Za-z])histor(?:y|ical)(?![A-Za-z])|"
            r"(?<![A-Za-z])snap[-_ ]?shot(?![A-Za-z]))",
            re.IGNORECASE,
        ),
    ),
    (
        "online_dependency",
        re.compile(
            r"(?:在线|联网|外联|远程|(?<![A-Za-z])online(?![A-Za-z])|"
            r"(?<![A-Za-z])remote(?![A-Za-z])|(?<![A-Za-z])external(?![A-Za-z]))"
            r".{0,80}"
            r"(?:依赖|服务|接口|调用|请求|(?<![A-Za-z])dependency(?![A-Za-z])|"
            r"(?<![A-Za-z])service(?![A-Za-z])|(?<![A-Za-z])endpoint(?![A-Za-z])|"
            r"(?<![A-Za-z])api(?![A-Za-z])|(?<![A-Za-z])request(?![A-Za-z])|"
            r"(?<![A-Za-z])provider(?![A-Za-z]))"
            r"|"
            r"(?:依赖|服务|接口|调用|请求|(?<![A-Za-z])dependency(?![A-Za-z])|"
            r"(?<![A-Za-z])service(?![A-Za-z])|(?<![A-Za-z])endpoint(?![A-Za-z])|"
            r"(?<![A-Za-z])api(?![A-Za-z])|(?<![A-Za-z])request(?![A-Za-z])|"
            r"(?<![A-Za-z])provider(?![A-Za-z]))"
            r".{0,80}"
            r"(?:在线|联网|外联|远程|(?<![A-Za-z])online(?![A-Za-z])|"
            r"(?<![A-Za-z])remote(?![A-Za-z])|(?<![A-Za-z])external(?![A-Za-z]))",
            re.IGNORECASE,
        ),
    ),
    (
        "completed_persona_distillation",
        re.compile(
            r"(?:蒸馏|蒸餾|(?<![A-Za-z])distill(?:ed|ation)?(?![A-Za-z]))"
            r".{0,80}"
            r"(?:完成|最终|定稿|已完成|(?<![A-Za-z])complete(?:d)?(?![A-Za-z])|"
            r"(?<![A-Za-z])final(?![A-Za-z])|(?<![A-Za-z])done(?![A-Za-z])|"
            r"(?<![A-Za-z])finished(?![A-Za-z]))"
            r"|"
            r"(?:完成|最终|定稿|已完成|(?<![A-Za-z])complete(?:d)?(?![A-Za-z])|"
            r"(?<![A-Za-z])final(?![A-Za-z])|(?<![A-Za-z])done(?![A-Za-z])|"
            r"(?<![A-Za-z])finished(?![A-Za-z]))"
            r".{0,80}"
            r"(?:蒸馏|蒸餾|人格|人设|人設|(?<![A-Za-z])distill(?:ed|ation)?(?![A-Za-z])|"
            r"(?<![A-Za-z])persona(?![A-Za-z]))",
            re.IGNORECASE,
        ),
    ),
)

RUNTIME_DEPENDENCY_PATTERNS = (
    (
        "known_official_host",
        re.compile(
            r"(?:toy-cnbeta01\.olivia\.miyoushe\.com|"
            r"(?<![A-Za-z])(?:miyoushe|mihoyo)(?![A-Za-z])|米哈游|"
            r"(?<![A-Za-z])olivia[-_.]steam(?![A-Za-z]))",
            re.IGNORECASE,
        ),
    ),
    (
        "official_request_poll_download_token_marker",
        re.compile(
            r"(?:\bofficial[\s_-]*(?:base|request|reply|response|poll|download|token)\b|"
            r"\b(?:base|request|reply|response|poll|download|token)[\s_-]*official\b|"
            r"\bcapture[\s_-]*official(?:[\s_-]*(?:reply|response))?\b|"
            r"\bdownload[\s_-]*reply[\s_-]*video\b|\bx[\s_-]*token\b)",
            re.IGNORECASE,
        ),
    ),
)

SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]|github_pat|glpat)-[A-Za-z0-9_-]{20,}\b")),
    (
        "jwt_like",
        re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    ),
    ("bearer_secret", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_./+=-]{24,}")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|access[_-]?token)\b\s*[:=]\s*"
            r"[\"'][^\"']{16,}[\"']"
        ),
    ),
)

SENSITIVE_PATH_PATTERN = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\.|$)|[^/]*(?:secret|token|credential|password)[^/]*|"
    r"[^/]+\.(?:pem|key|p12|pfx|crt|sqlite|sqlite3|db)|"
    r"[^/]+\.(?:safetensors|pth|pt|ckpt|onnx|gguf|bin|wav|flac|mp3|mp4|zip|7z|rar))$"
)

EVIDENCE_PROBES = (
    ".evidence/baseline-hardening/run-id/pytest.log",
    ".evidence/baseline-hardening/run-id/pytest.junit.xml",
    ".evidence/baseline-hardening/run-id/pytest-collect.log",
    ".evidence/baseline-hardening/run-id/compile.txt",
    ".evidence/baseline-hardening/run-id/git-diff-check.txt",
    ".evidence/baseline-hardening/run-id/scan-commands.txt",
    ".evidence/baseline-hardening/run-id/manifest.sha256",
)


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    names = sorted(name for name in result.stdout.decode("utf-8").split("\0") if name)
    return [root / Path(name) for name in names]


def runtime_python_files(root: Path, files: list[Path]) -> list[Path]:
    scanner = root / Path(__file__).name
    return [
        path
        for path in files
        if path.suffix.lower() == ".py"
        and "tests" not in path.relative_to(root).parts
        and path != scanner
    ]


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _pattern_findings(name: str, text: str, start_line: int = 1) -> list[str]:
    findings: list[str] = []
    for label, pattern in COMMENT_PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(f"{name}:{start_line + line_number(text, match.start()) - 1}:{label}")
    return findings


def _docstrings(tree: ast.AST) -> list[tuple[int, str]]:
    nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    result: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, nodes) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            result.append((first.lineno, first.value.value))
    return result


def comment_findings(name: str, text: str) -> list[str]:
    return _pattern_findings(name, text)


def _is_pinned_cors_metadata(path: Path, root: Path, lines: list[str], line_no: int) -> bool:
    """Allow the fixed frontend origin and header name used only by local CORS."""

    if relative(path, root) != "local_server.py":
        return False
    line = lines[line_no - 1].strip()
    context = "".join(lines[max(0, line_no - 17): line_no - 1])
    return (
        line == "'https://toy-cnbeta01.olivia.miyoushe.com',"
        and "TRUSTED_FRONTEND_ORIGINS" in context
    ) or (
        line == "'X-Token',"
        and "ALLOWED_HEADERS" in context
    )


def _is_user_confirmed_letter_import(path: Path, root: Path) -> bool:
    return relative(path, root) in {
        "runtime/imports/historical_memory.py",
        "runtime/imports/official_letters.py",
    }


def scan_runtime_comments(root: Path, files: list[Path]) -> tuple[list[str], list[str], int]:
    findings: list[str] = []
    runtime = runtime_python_files(root, files)
    for path in runtime:
        if _is_user_confirmed_letter_import(path, root):
            continue
        with tokenize.open(path) as handle:
            source = handle.read()
        tree = ast.parse(source, filename=str(path))
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            findings.extend(
                _pattern_findings(relative(path, root), token.string, token.start[0])
            )
        for start_line, docstring in _docstrings(tree):
            findings.extend(
                _pattern_findings(relative(path, root), docstring, start_line)
            )
    return findings, [name for name, _ in COMMENT_PATTERNS], len(runtime)


def scan_runtime_dependencies(root: Path, files: list[Path]) -> tuple[list[str], list[str], int]:
    findings: list[str] = []
    runtime = runtime_python_files(root, files)
    for path in runtime:
        if _is_user_confirmed_letter_import(path, root):
            continue
        with tokenize.open(path) as handle:
            lines = handle.readlines()
        for line_no, line in enumerate(lines, 1):
            if _is_pinned_cors_metadata(path, root, lines, line_no):
                continue
            for label, pattern in RUNTIME_DEPENDENCY_PATTERNS:
                if pattern.search(line):
                    findings.append(f"{relative(path, root)}:{line_no}:{label}")
    return findings, [name for name, _ in RUNTIME_DEPENDENCY_PATTERNS], len(runtime)


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".wav", ".flac", ".zip"}:
        return True
    with path.open("rb") as handle:
        return b"\0" in handle.read(4096)


def scan_secrets(root: Path, files: list[Path]) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    for path in files:
        if not path.is_file() or _is_binary(path):
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, 1):
                for label, pattern in SECRET_PATTERNS:
                    if pattern.search(line):
                        findings.append(f"{relative(path, root)}:{line_no}:{label}")
    return findings, [name for name, _ in SECRET_PATTERNS]


def scan_sensitive_paths(root: Path, files: list[Path]) -> tuple[list[str], list[str]]:
    findings = [
        relative(path, root)
        for path in files
        if SENSITIVE_PATH_PATTERN.search(relative(path, root))
    ]
    return findings, [
        "secret_token_credential_paths",
        "private_key_paths",
        "model_media_archive_paths",
    ]


def scan_large_files(root: Path, files: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in files:
        if path.is_file() and path.stat().st_size > MAX_TRACKED_FILE_BYTES:
            findings.append(f"{relative(path, root)}:{path.stat().st_size}")
    return findings


def scan_evidence_ignore(root: Path) -> tuple[list[str], list[str]]:
    not_ignored: list[str] = []
    checked: list[str] = []
    for probe in EVIDENCE_PROBES:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--", probe],
            cwd=root,
            capture_output=True,
        )
        checked.append(probe)
        if result.returncode != 0:
            not_ignored.append(probe)
    return not_ignored, checked


def _persona_release_files(root: Path, files: list[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in files:
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if (
            not parts
            or parts[0] not in PERSONA_RELEASE_ROOTS
            or path.suffix.lower() not in PERSONA_RELEASE_SUFFIXES
            or "provenance" in path.stem.lower()
        ):
            continue
        selected.append(path)
    return selected


def _append_persona_finding(findings: list[str], name: str, label: str) -> None:
    finding = f"{name}:{label}"
    if finding not in findings:
        findings.append(finding)


def _scan_persona_json(
    value: object,
    name: str,
    findings: list[str],
    *,
    enforce_release_flags: bool,
) -> None:
    if isinstance(value, list):
        for item in value:
            _scan_persona_json(
                item,
                name,
                findings,
                enforce_release_flags=enforce_release_flags,
            )
        return
    if not isinstance(value, dict):
        return

    allowed = value.get("allowed_public_release")
    rights = value.get("rights_status")
    if enforce_release_flags and (
        allowed is False
        or (
            isinstance(rights, str)
            and any(marker in rights.upper() for marker in ("UNKNOWN", "BLOCK"))
        )
    ):
        _append_persona_finding(findings, name, "blocked_release_record")
    if value.get("view") == "control":
        _append_persona_finding(findings, name, "control_view_instance")
    if value.get("continuation_awareness") in {"control_only", "pending"}:
        _append_persona_finding(findings, name, "continuation_instance")
    if value.get("nickname_permissions"):
        _append_persona_finding(findings, name, "private_nickname_instance")

    for key, item in value.items():
        if (
            key in PERSONA_RELEASE_TEXT_KEYS
            and isinstance(item, str)
            and len(item) > MAX_PERSONA_RELEASE_TEXT_CHARS
        ):
            _append_persona_finding(findings, name, "long_source_copy")
        _scan_persona_json(
            item,
            name,
            findings,
            enforce_release_flags=enforce_release_flags,
        )


def _scan_persona_text(text: str, name: str, findings: list[str]) -> None:
    checks = (
        (r"(?i)allowed_public_release\s*[:=]\s*false", "blocked_release_record"),
        (r"(?i)rights_status\s*[:=]\s*[^\r\n]*(?:unknown|block)", "blocked_release_record"),
        (r"(?i)(?:^|[,{\s])view\s*[:=]\s*[\"']?control\b", "control_view_instance"),
        (
            r"(?i)continuation_awareness\s*[:=]\s*[\"']?(?:control_only|pending)\b",
            "continuation_instance",
        ),
        (r"(?i)nickname_permissions\s*[:=]\s*\[[^\]\s]", "private_nickname_instance"),
    )
    for pattern, label in checks:
        if re.search(pattern, text):
            _append_persona_finding(findings, name, label)
    if any(len(line) > MAX_PERSONA_RELEASE_TEXT_CHARS for line in text.splitlines()):
        _append_persona_finding(findings, name, "long_source_copy")


def scan_persona_release(
    root: Path, files: list[Path]
) -> tuple[list[str], list[str], int]:
    findings: list[str] = []
    selected = _persona_release_files(root, files)
    for path in selected:
        name = relative(path, root)
        lower_name = name.lower()
        if re.search(r"(?:^|/)(?:private|reference)[^/]*\.(?:md|txt|docx?|pdf)$", lower_name):
            _append_persona_finding(findings, name, "private_reference_path")
        if re.search(r"(?:^|/)(?:private_world|relationship|world)[^/]*state[^/]*\.", lower_name):
            _append_persona_finding(findings, name, "private_state_path")
        if re.search(
            r"(?:^|/)(?:chat|letter|message|transcript|communication)[^/]*\.",
            lower_name,
        ):
            _append_persona_finding(findings, name, "private_communication_path")

        try:
            text = path.read_text(encoding="utf-8")
            if path.suffix.lower() == ".json":
                _scan_persona_json(
                    json.loads(text),
                    name,
                    findings,
                    enforce_release_flags=path.name != "persona_v2.json",
                )
            else:
                _scan_persona_text(text, name, findings)
        except (OSError, UnicodeError, json.JSONDecodeError):
            _append_persona_finding(findings, name, "invalid_release_asset")
    return findings, list(PERSONA_RELEASE_PATTERNS), len(selected)


def emit_section(name: str, patterns: list[str], findings: list[str]) -> None:
    print(f"[{name}]")
    print(f"patterns={','.join(patterns)}")
    print(f"matches={len(findings)}")
    for finding in findings:
        print(f"finding={finding}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--mode",
        action="append",
        choices=("all", *MODES),
        help="select one or more scan modes; default is all modes",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    selected = MODES if not args.mode or "all" in args.mode else tuple(dict.fromkeys(args.mode))

    try:
        files = tracked_files(root)
        print("baseline_hardening_scan=1")
        print(f"selected_modes={','.join(selected)}")
        print(f"tracked_files={len(files)}")

        all_findings: list[str] = []
        if "comments" in selected:
            findings, patterns, checked = scan_runtime_comments(root, files)
            emit_section("misleading_runtime_comments", patterns, findings)
            print(f"runtime_python_files_checked={checked}")
            all_findings.extend(findings)
        if "runtime-dependencies" in selected:
            findings, patterns, checked = scan_runtime_dependencies(root, files)
            emit_section("official_runtime_dependency_markers", patterns, findings)
            print(f"runtime_python_files_checked={checked}")
            all_findings.extend(findings)
        if "secrets" in selected:
            findings, patterns = scan_secrets(root, files)
            emit_section("tracked_secret_markers", patterns, findings)
            all_findings.extend(findings)
        if "sensitive-paths" in selected:
            findings, patterns = scan_sensitive_paths(root, files)
            emit_section("tracked_sensitive_paths", patterns, findings)
            all_findings.extend(findings)
        if "large-files" in selected:
            findings = scan_large_files(root, files)
            print("[tracked_large_files]")
            print(f"threshold_bytes={MAX_TRACKED_FILE_BYTES}")
            print(f"matches={len(findings)}")
            for finding in findings:
                print(f"finding={finding}")
            all_findings.extend(findings)
        if "evidence-ignore" in selected:
            findings, probes = scan_evidence_ignore(root)
            emit_section("evidence_ignore_boundary", [".evidence/**"], findings)
            print(f"evidence_probe_count={len(probes)}")
            print(f"evidence_probes_checked={len(probes) - len(findings)}")
            all_findings.extend(findings)
        if "persona-release" in selected:
            findings, patterns, checked = scan_persona_release(root, files)
            emit_section("persona_public_release_boundary", patterns, findings)
            print(f"persona_release_files_checked={checked}")
            all_findings.extend(findings)
    except (OSError, UnicodeError, subprocess.SubprocessError, tokenize.TokenError, SyntaxError) as exc:
        print(f"status=ERROR:{type(exc).__name__}")
        return 2

    status = "PASS" if not all_findings else "FAIL"
    print(f"status={status}")
    print(f"exit_code={0 if not all_findings else 1}")
    return 0 if not all_findings else 1


if __name__ == "__main__":
    sys.exit(main())
