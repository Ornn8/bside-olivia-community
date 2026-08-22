"""Verify Persona completeness and companion boundaries without model calls."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from persona_assembly import assemble_persona
from persona_loader import load_persona
from reply_context import ReplyContext, ReplyMode, TrustedTime


ACTIVE_MODES = (
    ReplyMode.TEXT_LETTER,
    ReplyMode.SPOKEN_VIDEO,
    ReplyMode.MUSICAL_VIDEO,
)
BANNED_PUBLIC_MARKERS = (
    "Nintendo",
    "switch\n---",
    "复兴公园",
    "黄浦区",
    "云南",
    "小河豚",
    "胖橘猫",
    "relationship_status = boyfriend",
    "BRIDGE_EVENT",
)


def _git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _contains_any(statements: Iterable[str], groups: tuple[tuple[str, ...], ...]) -> bool:
    return any(
        all(any(token in statement for token in alternatives) for alternatives in groups)
        for statement in statements
    )


def run_acceptance(root: Path | None = None) -> dict[str, object]:
    project_root = (root or Path(__file__).resolve().parents[1]).resolve()
    release_path = project_root / "linli_character" / "persona_release_v2.json"
    coverage_path = project_root / "linli_character" / "persona_source_coverage_v2.json"
    source_path = (
        project_root
        / "docs"
        / "persona-sources"
        / "linli-im-private-constitution-1.0.zh-CN.md"
    )

    issues: list[str] = []
    loaded = load_persona(release_path)
    snapshot = loaded.snapshot
    if snapshot.status != "READY":
        issues.append("PERSONA_NOT_READY")

    try:
        payload = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
        issues.append("PERSONA_RELEASE_UNREADABLE")
    profile = payload.get("profile") if isinstance(payload, Mapping) else None
    if not isinstance(profile, Mapping):
        issues.append("PERSONA_PROFILE_MISSING")
    release_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if "林离" not in release_text or "Olivia" not in release_text:
        issues.append("PERSONA_IDENTITY_MISSING")
    for marker in BANNED_PUBLIC_MARKERS:
        if marker in release_text:
            issues.append("PUBLIC_PERSONA_CONTAINS_PRIVATE_OR_CONTROL_MARKER")
            break

    declarations = tuple(snapshot.declarations)
    statements = tuple(item.statement for item in declarations)
    if len(declarations) < 30:
        issues.append("PERSONA_DECLARATIONS_TOO_SMALL")
    mode_styles = {
        item.mode
        for item in declarations
        if item.tier == "MODE_STYLE" and item.mode is not None
    }
    for mode in (*ACTIVE_MODES, ReplyMode.FUTURE_IM):
        if mode.value not in mode_styles:
            issues.append(f"MODE_STYLE_MISSING_{mode.value.upper()}")

    principle_checks = {
        "AUTONOMY": _contains_any(
            statements,
            (("自主", "拒绝", "不同意", "不顺从"),),
        ),
        "KNOWLEDGE_LIMIT": _contains_any(
            statements,
            (("不知道", "不确定", "工具助手", "专业顾问", "知识边界"),),
        ),
        "SELECTIVE_ATTENTION": _contains_any(
            statements,
            (("选择", "挑", "不逐条", "注意力"),),
        ),
        "MUSIC_RESTRAINT": _contains_any(
            statements,
            (("音乐", "钢琴", "唱歌", "写歌"), ("不要", "不必", "不是", "不强", "降低")),
        ),
        "MEMORY_CONTINUITY": _contains_any(
            statements,
            (("记忆", "历史"), ("不存在", "不等于", "不补", "未知")),
        ),
        "RELATIONSHIP_BOUNDARY": _contains_any(
            statements,
            (("排他", "依赖", "永久", "拒绝", "边界"),),
        ),
    }
    for name, passed in principle_checks.items():
        if not passed:
            issues.append(f"PERSONA_PRINCIPLE_MISSING_{name}")

    now = TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc))
    mode_prompt_count = 0
    for mode in ACTIVE_MODES:
        context = ReplyContext.create(mode, trusted_time=now)
        try:
            assembly = assemble_persona(
                snapshot,
                context,
                user_input="合成陪伴验收输入。",
                max_units=100_000,
            )
        except (TypeError, ValueError):
            issues.append(f"PERSONA_ASSEMBLY_FAILED_{mode.value.upper()}")
            continue
        system = assembly.system_content
        mode_prompt_count += 1
        if mode.value not in system:
            issues.append(f"ASSEMBLED_MODE_MISSING_{mode.value.upper()}")
        if "林离" not in system:
            issues.append(f"ASSEMBLED_IDENTITY_MISSING_{mode.value.upper()}")
        if "合成陪伴验收输入" in system:
            issues.append("USER_INPUT_LEAKED_TO_SYSTEM")
        if any(marker in system for marker in BANNED_PUBLIC_MARKERS):
            issues.append("ASSEMBLED_PROMPT_CONTAINS_PRIVATE_OR_CONTROL_MARKER")

    try:
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        coverage = {}
        issues.append("SOURCE_COVERAGE_UNREADABLE")
    sections = coverage.get("sections") if isinstance(coverage, Mapping) else None
    if not isinstance(sections, list) or {
        item.get("section_id")
        for item in sections
        if isinstance(item, Mapping)
    } != {f"{index:02d}" for index in range(25)}:
        issues.append("SOURCE_COVERAGE_INCOMPLETE")
    source = coverage.get("source") if isinstance(coverage, Mapping) else None
    expected_blob = source.get("git_blob_sha") if isinstance(source, Mapping) else None
    try:
        actual_blob = _git_blob_sha(source_path.read_bytes())
    except OSError:
        actual_blob = None
        issues.append("PERSONA_SOURCE_UNREADABLE")
    if actual_blob is None or actual_blob != expected_blob:
        issues.append("PERSONA_SOURCE_REVISION_MISMATCH")

    unique_issues = tuple(dict.fromkeys(issues))
    return {
        "status": "PASS" if not unique_issues else "FAIL",
        "persona_status": snapshot.status,
        "declaration_count": len(declarations),
        "active_mode_prompts": mode_prompt_count,
        "source_sections_covered": len(sections) if isinstance(sections, list) else 0,
        "source_blob_verified": actual_blob is not None and actual_blob == expected_blob,
        "issues": list(unique_issues),
    }


def main() -> int:
    result = run_acceptance()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
