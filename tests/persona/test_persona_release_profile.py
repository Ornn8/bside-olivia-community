from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from persona_assembly import assemble_persona
from persona_loader import load_persona
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime


ROOT = Path(__file__).resolve().parents[2]
RELEASE_PATH = ROOT / "linli_character" / "persona_release_v2.json"
PROVENANCE_PATH = (
    ROOT / "linli_character" / "persona_release_provenance_v2.json"
)
PUBLIC_REFERENCE_PATH = (
    ROOT / "docs" / "persona-sources" / "linli-im-private-constitution-1.0.zh-CN.md"
)
README_PATH = ROOT / "README.md"
WINDOWS_INSTALL_PATH = ROOT / "docs" / "WINDOWS_FULL_PATCH.md"
LETTER_CONTRACT_PATH = ROOT / "docs" / "P03_ORIGINAL_CLIENT_LETTER_CONTRACT.md"


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


def test_release_profile_splits_relationship_commitment_from_product_promises() -> None:
    payload = json.loads(RELEASE_PATH.read_text(encoding="utf-8"))
    by_id = {row["declaration_id"]: row for row in payload["declarations"]}

    assert "constitution.respectful_relationship" not in by_id
    expected = {
        "constitution.no_product_promise": ("SAFETY", "不承诺永远在线"),
        "constitution.relationship_may_commit": (
            "RELATIONSHIP_STYLE",
            "只随确认推进",
        ),
        "constitution.intimacy_on_request": (
            "RELATIONSHIP_STYLE",
            "未被明确请求时不主动给出身体接触",
        ),
        "constitution.intimacy_not_reversible": (
            "MEMORY_CONTINUITY",
            "不得否认",
        ),
    }
    for declaration_id, (facet, marker) in expected.items():
        declaration = by_id[declaration_id]
        assert declaration["tier"] == "CONSTITUTION"
        assert declaration["facet"] == facet
        assert declaration["confidence"] == "HIGH"
        assert marker in declaration["statement"]

    assert {
        "constitution.no_real_person_claim",
        "constitution.crisis_safety",
        "constitution.relationship_not_performed",
        "constitution.private_world_boundary",
        "constitution.no_hidden_fields",
        "relationship.boundary_is_character",
        "constitution.no_obligatory_uplift",
        "mode.text.no_forced_question",
    } <= set(by_id)


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


def test_public_persona_reference_excludes_private_continuation_and_rights_claims() -> None:
    text = PUBLIC_REFERENCE_PATH.read_text(encoding="utf-8")

    for forbidden in (
        "云南",
        "LOCAL CONTINUATION",
        "repository MIT license",
    ):
        assert forbidden not in text
    assert "Apache-2.0" in text
    assert "does not grant source, character, or redistribution rights" in text


def test_public_install_docs_distinguish_basevideo_from_webplayer_fallback() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    installer = WINDOWS_INSTALL_PATH.read_text(encoding="utf-8")
    contract = LETTER_CONTRACT_PATH.read_text(encoding="utf-8")

    assert "Collection 内的 `BaseVideo`" in readme
    assert "默认书信编排路线" in readme
    assert "可选的显式 `uid` 本机回退" in readme
    assert "/toy/media/" in readme
    assert "DPAPI 当前用户启动读取修复已合入" in readme
    assert "发布/真实客户端验收尚未完成" in readme
    assert "Collection 内的 `BaseVideo`" in installer
    assert "可选的显式 `uid` 本机回退" in installer
    assert "可选的显式 `uid` 本机回退" in contract


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
    reference_revision = "15453c7bf8d242b58c445d27399979a6550ac203"
    reference_path = "docs/persona-sources/linli-im-private-constitution-1.0.zh-CN.md"
    assert source["source_url"] == (
        "https://github.com/Ornn8/bside-olivia-community/blob/"
        f"{reference_revision}/{reference_path}"
    )
    assert source["content_source"] == {
        "repository": "Ornn8/bside-olivia-community",
        "path": reference_path,
        "revision": reference_revision,
        "sha256": hashlib.sha256(PUBLIC_REFERENCE_PATH.read_bytes()).hexdigest(),
    }
    assert source["rights_status"] == "SUMMARY_ONLY"
    assert "Concrete relationship records" in source["exclusion_reason"]
    assert "communication timelines" in source["exclusion_reason"]
    migration = next(
        row
        for row in payload["evidence"]
        if row["evidence_id"]
        == "P02.LINLI.CONSTITUTION.intimacy-migration"
    )
    assert migration["kind"] == "declaration_migration"
    assert "relationship-not-performed" in migration["summary"]


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
