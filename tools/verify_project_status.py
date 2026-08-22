"""Minimal static checks for the canonical project-governance documents."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = ROOT / "docs"
PROJECT_DOC = DOCS_ROOT / "PROJECT_MANAGEMENT.md"
STATUS_DOC = DOCS_ROOT / "STATUS.md"
ALLOWED_STATUSES = frozenset(
    {"BACKLOG", "IMPLEMENTING", "REVIEW", "READY", "MERGED", "BLOCKED"}
)

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")
WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s(`])(?:[A-Z]:[\\/]|\\\\[A-Za-z0-9._-]+[\\/])"
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+(?!TEST\b|<)[A-Za-z0-9._-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret)\s*[:=]\s*[^\s`<]{12,}"
    ),
)
COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
ACTIVE_HEADING_RE = re.compile(
    r"^#\s+(?:status|project management|项目治理|项目状态)(?:\s|/|$)", re.IGNORECASE
)


def _read(path: Path, findings: list[str]) -> str:
    if not path.is_file():
        findings.append(f"missing document: {path.relative_to(ROOT).as_posix()}")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        findings.append(f"document is not UTF-8: {path.relative_to(ROOT).as_posix()}")
        return ""


def _check_links(path: Path, text: str, findings: list[str]) -> None:
    for raw_target in MARKDOWN_LINK_RE.findall(text):
        target = raw_target.strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith("//") or target.startswith("#"):
            continue
        relative_target = target.split("#", 1)[0].split("?", 1)[0]
        if not relative_target:
            continue
        resolved = (path.parent / relative_target).resolve()
        if not resolved.is_file():
            findings.append(
                f"broken relative link in {path.relative_to(ROOT).as_posix()}: {relative_target}"
            )


def _check_sensitive_text(path: Path, text: str, findings: list[str]) -> None:
    label = path.relative_to(ROOT).as_posix()
    if WINDOWS_ABSOLUTE_PATH_RE.search(text):
        findings.append(f"absolute local path in {label}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(f"secret-like text in {label}: {pattern.pattern}")


def _status_rows(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0].startswith("-"):
            continue
        if cells[0].casefold() in {"work item", "项目", "工作项"}:
            continue
        rows.append((cells[0], cells[1]))
    return rows


def _check_active_documents(findings: list[str]) -> None:
    candidates = [
        path
        for path in DOCS_ROOT.rglob("*.md")
        if path.stem.casefold().replace("-", "_") in {"status", "project_management"}
    ]
    expected = {PROJECT_DOC.resolve(), STATUS_DOC.resolve()}
    if {path.resolve() for path in candidates} != expected:
        names = ", ".join(sorted(path.relative_to(ROOT).as_posix() for path in candidates))
        findings.append(f"duplicate active governance documents: {names}")

    for path in DOCS_ROOT.rglob("*.md"):
        if path.resolve() in expected:
            continue
        text = path.read_text(encoding="utf-8")
        if ACTIVE_HEADING_RE.search(text):
            findings.append(f"duplicate active governance heading: {path.relative_to(ROOT).as_posix()}")


def check_documents(root: Path = ROOT) -> list[str]:
    """Return sanitized findings; never include matched secret/path content."""

    global ROOT, DOCS_ROOT, PROJECT_DOC, STATUS_DOC
    old_values = ROOT, DOCS_ROOT, PROJECT_DOC, STATUS_DOC
    ROOT = root.resolve()
    DOCS_ROOT = ROOT / "docs"
    PROJECT_DOC = DOCS_ROOT / "PROJECT_MANAGEMENT.md"
    STATUS_DOC = DOCS_ROOT / "STATUS.md"
    findings: list[str] = []
    project_text = _read(PROJECT_DOC, findings)
    status_text = _read(STATUS_DOC, findings)

    for path, text in ((PROJECT_DOC, project_text), (STATUS_DOC, status_text)):
        _check_links(path, text, findings)
        _check_sensitive_text(path, text, findings)

    if status_text.count("唯一 source of truth") != 1:
        findings.append("STATUS.md must contain exactly one sole-source marker")
    if "STATUS.md" not in project_text:
        findings.append("PROJECT_MANAGEMENT.md must point to STATUS.md")
    if not re.search(
        r"GitHub Issues.*Milestones|Milestones.*GitHub Issues",
        project_text,
        re.IGNORECASE | re.DOTALL,
    ):
        findings.append(
            "PROJECT_MANAGEMENT.md must declare GitHub Issues and Milestones as the work queue"
        )
    if COMMIT_SHA_RE.search(status_text):
        findings.append("STATUS.md must not copy exact commit SHAs")

    rows = _status_rows(status_text)
    if rows:
        findings.append("STATUS.md must not contain per-work-item status table rows")
    for item, status in rows:
        if status not in ALLOWED_STATUSES:
            findings.append(f"invalid status for {item}: {status}")
    missing_statuses = [
        status for status in sorted(ALLOWED_STATUSES) if f"`{status}`" not in status_text
    ]
    if missing_statuses:
        findings.append("STATUS.md is missing state semantics")

    _check_active_documents(findings)
    ROOT, DOCS_ROOT, PROJECT_DOC, STATUS_DOC = old_values
    return sorted(set(findings))


def main() -> int:
    findings = check_documents()
    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
        return 1
    print(
        "PASS project-governance links=ok statuses=ok absolute-paths=0 "
        "secrets=0 duplicate-active=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
