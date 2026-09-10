import json
import re
import pytest
from datetime import datetime, timezone

from persona_assembly import assemble_persona
from persona_loader import PersonaDeclaration, PersonaProfile, PersonaSnapshot
from runtime.persona.relationship_expression import render_relationship_expression
from runtime.reply.reply_context import (
    BehaviorLevel,
    IntimacyTier,
    KnownContinuationFact,
    NicknamePermission,
    PrivateBehaviorView,
    RelationshipStage,
    ReplyContext,
    ReplyMode,
    TrustedTime,
)


def _snapshot() -> PersonaSnapshot:
    return PersonaSnapshot(
        schema_version="p02.persona.v2",
        persona_id="synthetic.persona",
        declarations=(
            PersonaDeclaration(
                declaration_id="constitution.synthetic",
                source_id="source.synthetic",
                tier="CONSTITUTION",
                confidence="HIGH",
                rights_status="SUMMARY_ONLY",
                allowed_public_release=True,
                statement="Do not invent shared history.",
                mode=None,
                facet="MEMORY_CONTINUITY",
            ),
            PersonaDeclaration(
                declaration_id="mode.synthetic",
                source_id="source.synthetic",
                tier="MODE_STYLE",
                confidence="HIGH",
                rights_status="SUMMARY_ONLY",
                allowed_public_release=True,
                statement="Use a selective letter voice.",
                mode="text_letter",
                facet="MODE_STYLE",
            ),
        ),
        status="READY",
        source="persona_v2",
        profile=PersonaProfile(
            display_name="林离 Olivia",
            locale="zh-CN",
            summary="Synthetic bounded persona.",
            required_facets=("IDENTITY",),
            required_modes=("text_letter",),
        ),
    )


def _context(behavior: PrivateBehaviorView) -> ReplyContext:
    return ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 9, 10, tzinfo=timezone.utc)),
        private_behavior=behavior,
    )


def _payload(system_content: str) -> dict[str, object]:
    match = re.search(
        r"<private_behavior>\s*(.*?)\s*</private_behavior>",
        system_content,
        re.S,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_mixed_familiarity_and_closeness_do_not_imply_trust_or_comfort() -> None:
    rendered = render_relationship_expression(
        PrivateBehaviorView(
            familiarity=BehaviorLevel.HIGH,
            closeness=BehaviorLevel.HIGH,
            trust=BehaviorLevel.LOW,
            comfort=BehaviorLevel.LOW,
        )
    )

    assert rendered is not None
    assert "已有的来往可以自然接续" in rendered
    assert "对这段来往有亲近感" in rendered
    assert "自己的私事" in rendered
    assert "表达还有些拘谨" in rendered
    assert "彼此" not in rendered
    assert "恋爱" not in rendered
    assert "排他" not in rendered
    assert "背叛" not in rendered


def test_all_unknown_is_omitted_and_explicit_low_is_meaningful() -> None:
    assert render_relationship_expression(PrivateBehaviorView()) is None

    rendered = render_relationship_expression(
        PrivateBehaviorView(familiarity=BehaviorLevel.LOW)
    )
    assert rendered is not None
    assert "日常细节了解还有限" in rendered
    assert "unknown" not in rendered
    assert "初始" not in rendered


def test_tension_is_rendered_without_inventing_a_cause() -> None:
    rendered = render_relationship_expression(
        PrivateBehaviorView(tension=BehaviorLevel.HIGH)
    )

    assert rendered is not None
    assert "有所绷紧" in rendered
    assert "原因" not in rendered
    assert "冲突" not in rendered


@pytest.mark.parametrize("stage", [RelationshipStage.CLOSE, RelationshipStage.COMMITTED])
def test_low_familiarity_does_not_restart_confirmed_relationship(stage) -> None:
    behavior = PrivateBehaviorView(
        familiarity=BehaviorLevel.LOW, relationship_stage=stage,
        known_continuations=(KnownContinuationFact("relationship.confirmed", "双方已经确认这段关系。"),),
    )
    payload = _payload(assemble_persona(
        _snapshot(), _context(behavior), user_input="想听你聊聊自己。",
        max_units=8000, relationship_expression_enabled=True,
    ).system_content)
    assert payload["relationship_stage"] == stage.value
    assert payload["known_continuations"] == [fact.to_dict() for fact in behavior.known_continuations]
    assert "还在认识对方" not in payload["expression_context"]
    assert "日常细节了解还有限" in payload["expression_context"]


def test_trust_changes_self_disclosure_without_prescribing_suspicion() -> None:
    low = render_relationship_expression(PrivateBehaviorView(trust=BehaviorLevel.LOW))
    high = render_relationship_expression(PrivateBehaviorView(trust=BehaviorLevel.HIGH))
    assert "自己的私事" in low
    assert "自己的想法和小困扰" in high
    assert "核实" not in low and "怀疑" not in low
    assert "初识" not in low and "亲近感" not in high


def test_renderer_does_not_mutate_typed_permissions_or_known_facts() -> None:
    behavior = PrivateBehaviorView(
        familiarity=BehaviorLevel.MEDIUM,
        relationship_stage=RelationshipStage.CLOSE,
        intimacy_ceiling=IntimacyTier.LIGHT_CONTACT,
        granted_intimacy=IntimacyTier.LIGHT_CONTACT,
        nickname_permission=NicknamePermission.ALLOWED,
        home_history_allowed=True,
        known_continuations=(
            KnownContinuationFact("continuation.synthetic", "已确认的合成事实。"),
        ),
    )
    original = behavior.to_dict()

    assert render_relationship_expression(behavior) is not None
    assert behavior.to_dict() == original


def test_assembly_is_off_by_default_and_opt_in_replaces_only_writer_axes() -> None:
    behavior = PrivateBehaviorView(
        familiarity=BehaviorLevel.HIGH,
        trust=BehaviorLevel.LOW,
        closeness=BehaviorLevel.HIGH,
        relationship_stage=RelationshipStage.CLOSE,
        intimacy_ceiling=IntimacyTier.LIGHT_CONTACT,
        granted_intimacy=IntimacyTier.LIGHT_CONTACT,
        nickname_permission=NicknamePermission.ALLOWED,
        home_history_allowed=True,
        known_continuations=(
            KnownContinuationFact("continuation.synthetic", "已确认的合成事实。"),
        ),
    )
    context = _context(behavior)
    kwargs = {"user_input": "你好", "max_units": 8000}

    default_payload = _payload(assemble_persona(_snapshot(), context, **kwargs).system_content)
    opt_in_payload = _payload(
        assemble_persona(
            _snapshot(), context, relationship_expression_enabled=True, **kwargs
        ).system_content
    )

    assert "expression_context" not in default_payload
    assert default_payload["familiarity"] == "high"
    assert opt_in_payload["expression_context"]
    assert all(
        axis not in opt_in_payload
        for axis in {"familiarity", "trust", "comfort", "closeness", "tension"}
    )
    assert opt_in_payload["relationship_stage"] == "close"
    assert opt_in_payload["known_continuations"] == [
        {"fact_id": "continuation.synthetic", "statement": "已确认的合成事实。"}
    ]
    assert opt_in_payload["action_permissions"] == {
        "physical_contact": {"ceiling": "light_contact", "granted": "light_contact"},
        "nickname_use": {"permission": "allowed"},
        "claiming_home_history": {"allowed": True},
    }


def test_opt_in_all_unknown_keeps_original_private_behavior_payload() -> None:
    context = _context(PrivateBehaviorView())
    kwargs = {"user_input": "你好", "max_units": 8000}

    default_payload = _payload(assemble_persona(_snapshot(), context, **kwargs).system_content)
    opt_in_payload = _payload(
        assemble_persona(
            _snapshot(), context, relationship_expression_enabled=True, **kwargs
        ).system_content
    )

    assert opt_in_payload == default_payload


def test_expression_remains_atomic_with_existing_private_behavior_budget_block() -> None:
    context = _context(
        PrivateBehaviorView(
            familiarity=BehaviorLevel.HIGH,
            trust=BehaviorLevel.LOW,
            closeness=BehaviorLevel.HIGH,
        )
    )
    full = assemble_persona(
        _snapshot(),
        context,
        user_input="你好",
        max_units=8000,
        relationship_expression_enabled=True,
    )
    pressured = assemble_persona(
        _snapshot(),
        context,
        user_input="你好",
        max_units=full.budget_report.used_units - 1,
        relationship_expression_enabled=True,
    )

    assert "private_behavior" in full.budget_report.included_ids
    assert pressured.budget_report.dropped_ids == ("private_behavior",)
    assert "<private_behavior>" not in pressured.system_content
    assert "expression_context" not in pressured.system_content
