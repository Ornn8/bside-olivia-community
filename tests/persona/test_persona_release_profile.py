from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from persona_assembly import assemble_persona
from persona_loader import load_persona
from reply_context import ReplyContext, ReplyMode, TrustedTime


ROOT = Path(__file__).resolve().parents[2]
RELEASE_PATH = ROOT / "linli_character" / "persona_release_v2.json"
PROVENANCE_PATH = (
    ROOT / "linli_character" / "persona_release_provenance_v2.json"
)


def test_release_profile_is_complete_linli_not_policy_only() -> None:
    loaded = load_persona(RELEASE_PATH)

    assert loaded.error_code is None
    assert loaded.snapshot.status == "READY"
    assert loaded.snapshot.profile is not None
    assert loaded.snapshot.profile.display_name == "林离 Olivia"
    assert "钢琴" in loaded.snapshot.profile.summary
    assert "不是通用助手" in loaded.snapshot.profile.summary
    assert not loaded.readiness_gaps

    facets = {row.facet for row in loaded.snapshot.declarations}
    assert set(loaded.snapshot.profile.required_facets) <= facets
    modes = {
        row.mode
        for row in loaded.snapshot.declarations
        if row.tier == "MODE_STYLE" and row.facet == "MODE_STYLE"
    }
    assert set(loaded.snapshot.profile.required_modes) <= modes


def test_release_profile_contains_character_behavior_not_only_safety_rules() -> None:
    payload = json.loads(RELEASE_PATH.read_text(encoding="utf-8"))
    by_id = {row["declaration_id"]: row for row in payload["declarations"]}

    assert {
        "identity.linli_name",
        "background.shanghai_music_student",
        "trait.autonomous_sensitive_aesthetic",
        "trait.not_generic_student",
        "knowledge.unknown_response",
        "style.corner_quotes",
        "style.no_mechanical_cuteness",
        "relationship.concrete_closeness",
        "memory.ask_for_reminder",
        "mode.text.no_forced_question",
        "mode.spoken.character_voice",
        "mode.musical.only_when_motivated",
    } <= set(by_id)
    assert all(row["allowed_public_release"] for row in by_id.values())
    assert all(row["rights_status"] == "SUMMARY_ONLY" for row in by_id.values())
    assert any(row["facet"] == "AUTONOMY" for row in by_id.values())
    assert any(row["facet"] == "EXPRESSION_STYLE" for row in by_id.values())
    assert any(row["facet"] == "RELATIONSHIP_STYLE" for row in by_id.values())


def test_release_profile_excludes_private_instances_and_control_protocol() -> None:
    text = RELEASE_PATH.read_text(encoding="utf-8")

    for forbidden in (
        "switch",
        "Nintendo",
        "云南",
        "男朋友",
        "小河豚",
        "胖橘猫",
        "复兴公园",
        "relationship_status",
        "control_only",
    ):
        assert forbidden not in text
    assert len(text) < 30_000
    payload = json.loads(text)
    assert max(len(row["statement"]) for row in payload["declarations"]) <= 240


def test_release_provenance_is_bidirectional_and_pinned_to_public_reference() -> None:
    schema = json.loads(
        (
            ROOT / "contracts" / "persona_v2_provenance.schema.json"
        ).read_text(encoding="utf-8")
    )
    payload = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    release = json.loads(RELEASE_PATH.read_text(encoding="utf-8"))

    Draft202012Validator(
        schema, format_checker=FormatChecker()
    ).validate(payload)
    declarations = {
        row["declaration_id"] for row in release["declarations"]
    }
    linked = {
        declaration_id
        for source in payload["sources"]
        for declaration_id in source["declaration_ids"]
    }
    assert linked == declarations
    source = payload["sources"][0]
    assert source["source_id"] == "P02.LINLI.CONSTITUTION"
    assert source["source_url"].endswith(
        "/blob/900d6d18eba458e86fba79c05608fe8671d9100e/"
        "docs/persona-sources/linli-im-private-constitution-1.0.zh-CN.md"
    )
    assert source["rights_status"] == "SUMMARY_ONLY"
    assert "Concrete relationship records" in source["exclusion_reason"]
    assert "communication timelines" in source["exclusion_reason"]


def test_assembled_release_keeps_identity_and_mode_style_under_budget() -> None:
    loaded = load_persona(RELEASE_PATH)
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(
            datetime(2026, 8, 22, tzinfo=timezone.utc)
        ),
    )
    assembly = assemble_persona(
        loaded.snapshot,
        context,
        user_input="今天只是普通地有点累。",
        max_units=10_000,
    )

    assert assembly.persona_status == "READY"
    assert "林离 Olivia" in assembly.system_content
    assert "不是通用助手" in assembly.system_content
    assert "不要求每次反问或升华" in assembly.system_content
    assert "<mode_style>" in assembly.system_content
    assert "<untrusted_history>" not in assembly.system_content
    assert assembly.budget_report.used_units <= 10_000
