"""Read-only contract check for the B11 acceptance and process documents."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
B11_OWNED_PATHS = frozenset(
    {
        ".gitignore",
        "docs/ACCEPTANCE.md",
        "docs/B11_ACCEPTANCE_STANDARD.md",
        "docs/DELEGATION_BOARD.md",
        "docs/MASTER_PLAN.md",
        "docs/PROJECT_MANAGEMENT.md",
        "tools/check_b11_docs.py",
    }
)
DOCS = {
    "docs/B11_ACCEPTANCE_STANDARD.md": (
        "collect > 0",
        "failed = 0",
        "errors = 0",
        "skipped = 0",
        "compile",
        "scanners",
        "scopes",
        "health",
        "lifecycle",
        "legacy_letters",
        "gpt-5.6-luna/xhigh",
        "GitHub/Hugging Face",
        "source_commit",
        "SHA-256",
        "许可证",
        "卸载路径",
    ),
    "docs/ACCEPTANCE.md": (
        "B11_ACCEPTANCE_STANDARD.md",
        "collect > 0",
        "gpt-5.6-luna/xhigh",
    ),
    "docs/PROJECT_MANAGEMENT.md": (
        "B11_ACCEPTANCE_STANDARD.md",
        "gpt-5.6-luna/xhigh",
        "UNAVAILABLE",
        "collect > 0",
    ),
    "docs/MASTER_PLAN.md": ("B11_ACCEPTANCE_STANDARD.md", "gpt-5.6-luna/xhigh"),
    "docs/DELEGATION_BOARD.md": (
        "B11_ACCEPTANCE_STANDARD.md",
        "gpt-5.6-luna/xhigh",
        "collect > 0",
    ),
}


def read_utf8(relative: str) -> str:
    path = ROOT / relative
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if text.encode("utf-8") != raw:
        raise AssertionError(f"not canonical UTF-8: {relative}")
    return text


def check_documents() -> dict[str, object]:
    failures: list[str] = []
    texts: dict[str, str] = {}
    for relative, required in DOCS.items():
        try:
            text = read_utf8(relative)
        except (AssertionError, OSError, UnicodeError) as exc:
            failures.append(str(exc))
            continue
        texts[relative] = text
        for term in required:
            if term not in text:
                failures.append(f"missing {term!r} in {relative}")

    maintained = tuple(DOCS)
    for relative in maintained:
        if "gpt-5.6-luna/max" in texts.get(relative, ""):
            failures.append(f"stale reviewer model in {relative}")

    return {
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
        "scope_paths": sorted(B11_OWNED_PATHS),
        "files_checked": len(texts),
    }


def verified_b11_paths() -> tuple[frozenset[str], bool]:
    """Return the exact B11 docs tranche only after the content check passes."""

    try:
        report = check_documents()
    except Exception:
        return frozenset(), True
    paths = frozenset(report.get("scope_paths", []))
    if report.get("status") != "PASS" or paths != B11_OWNED_PATHS:
        return frozenset(), True
    return paths, False


def main() -> int:
    report = check_documents()
    failures = report["failures"]
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print(
        f"B11_DOC_SELF_CHECK PASS files={report['files_checked']} "
        "read_only=true encoding=utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
