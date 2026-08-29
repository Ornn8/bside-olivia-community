from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    IntimacyGrant,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)
from runtime.memory.private_world_projection import project_private_world
from runtime.reply.reply_context import (
    BehaviorLevel,
    IntimacyTier,
    RelationshipStage,
)


def test_projection_converts_hidden_scores_to_finite_behavior_levels() -> None:
    projected = project_private_world(
        PrivateWorldSnapshot(
            familiarity=100,
            trust=70,
            comfort=69,
            closeness=35,
            tension=34,
            relationship_stage="close",
        )
    )

    assert projected.behavior.familiarity is BehaviorLevel.HIGH
    assert projected.behavior.trust is BehaviorLevel.HIGH
    assert projected.behavior.comfort is BehaviorLevel.MEDIUM
    assert projected.behavior.closeness is BehaviorLevel.MEDIUM
    assert projected.behavior.tension is BehaviorLevel.LOW
    assert projected.behavior.relationship_stage is RelationshipStage.CLOSE

    payload = projected.to_dict()
    serialized = repr(payload)
    for raw in ("100", "70", "69", "35", "34"):
        assert raw not in serialized


def test_current_unicode_nicknames_and_home_permission_enter_finite_projection() -> None:
    projected = project_private_world(
        PrivateWorldSnapshot(
            nickname_permissions=("小河豚", "旅行者"),
            home_access=HomeAccess.VISIT_ACCESS,
        )
    )

    assert projected.authorized_nicknames == ("小河豚", "旅行者")
    assert projected.to_dict()["authorized_nicknames"] == ["小河豚", "旅行者"]
    assert projected.behavior.nickname_permission.value == "allowed"
    assert projected.behavior.home_history_allowed is True
    assert projected.may_acknowledge_home_history is True


def test_control_only_and_pending_continuation_facts_never_enter_model_payload() -> None:
    projected = project_private_world(
        PrivateWorldSnapshot(
            continuation_facts=(
                LocalContinuationFact(
                    "known.class",
                    "她已经知道下周课程会调整。",
                    ContinuationAwareness.CHARACTER_KNOWN,
                ),
                LocalContinuationFact(
                    "pending.trip",
                    "角色尚未知道的旅行安排。",
                    ContinuationAwareness.PENDING,
                ),
                LocalContinuationFact(
                    "control.plan",
                    "仅控制层可见的未来计划。",
                    ContinuationAwareness.CONTROL_ONLY,
                ),
            )
        )
    )
    payload = projected.to_dict()

    assert projected.continuation_known is True
    assert payload["known_continuations"] == [
        {
            "fact_id": "known.class",
            "statement": "她已经知道下周课程会调整。",
        }
    ]
    assert payload["behavior"]["known_continuations"] == payload["known_continuations"]
    serialized = repr(payload)
    assert "pending.trip" not in serialized
    assert "control.plan" not in serialized
    assert "旅行安排" not in serialized
    assert "未来计划" not in serialized
    assert "pending" not in serialized
    assert "control_only" not in serialized


def test_legacy_global_awareness_projects_only_a_boolean_not_control_label() -> None:
    for awareness in (
        ContinuationAwareness.CONTROL_ONLY,
        ContinuationAwareness.PENDING,
    ):
        payload = project_private_world(
            PrivateWorldSnapshot(continuation_awareness=awareness)
        ).to_dict()

        assert payload["continuation_known"] is False
        assert awareness.value not in repr(payload)

    known = project_private_world(
        PrivateWorldSnapshot(
            continuation_awareness=ContinuationAwareness.CHARACTER_KNOWN
        )
    )
    assert known.continuation_known is True
    assert known.known_continuation_facts == ()


def test_unknown_relationship_stage_fails_closed_to_unknown() -> None:
    projected = project_private_world(
        PrivateWorldSnapshot(relationship_stage="custom_future_stage")
    )

    assert projected.behavior.relationship_stage is RelationshipStage.UNKNOWN


def test_projection_exposes_only_bounded_intimacy_tiers() -> None:
    projected = project_private_world(
        PrivateWorldSnapshot(
            familiarity=91,
            relationship_stage="committed",
            intimacy_grants=(
                IntimacyGrant(
                    "intimacy.synthetic-light",
                    IntimacyTier.LIGHT_CONTACT,
                    "Synthetic light-contact evidence.",
                ),
                IntimacyGrant(
                    "intimacy.synthetic-close",
                    IntimacyTier.CLOSE_CONTACT,
                    "Synthetic close-contact evidence.",
                ),
            ),
            growth_window_start="2026-08-29T00:00:00+00:00",
            growth_used=6,
        )
    )

    assert projected.intimacy_ceiling is IntimacyTier.CLOSE_CONTACT
    assert projected.granted_intimacy is IntimacyTier.CLOSE_CONTACT
    assert projected.behavior.intimacy_ceiling is IntimacyTier.CLOSE_CONTACT
    assert projected.behavior.granted_intimacy is IntimacyTier.CLOSE_CONTACT
    assert projected.to_dict()["intimacy_ceiling"] == "close_contact"
    assert projected.to_dict()["granted_intimacy"] == "close_contact"
    serialized = repr(projected.to_dict())
    assert "Synthetic" not in serialized
    assert "growth_" not in serialized
    assert "91" not in serialized
