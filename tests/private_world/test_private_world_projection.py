from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    PrivateWorldSnapshot,
)
from private_world_projection import project_private_world
from reply_context import BehaviorLevel, RelationshipStage


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


def test_only_current_authorized_nicknames_enter_projection() -> None:
    projected = project_private_world(
        PrivateWorldSnapshot(nickname_permissions=("linli", "friend"))
    )

    assert projected.authorized_nicknames == ("linli", "friend")
    assert projected.to_dict()["authorized_nicknames"] == ["linli", "friend"]


def test_control_only_and_pending_continuation_never_enter_character_payload() -> None:
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


def test_home_access_only_grants_history_acknowledgement() -> None:
    no_access = project_private_world(PrivateWorldSnapshot())
    visit_access = project_private_world(
        PrivateWorldSnapshot(home_access=HomeAccess.VISIT_ACCESS)
    )

    assert no_access.may_acknowledge_home_history is False
    assert visit_access.may_acknowledge_home_history is True
    assert "may_acknowledge_home_history" not in visit_access.to_dict()
    assert visit_access.behavior.home_access.value == "no_access"
    assert "mention" not in repr(visit_access.to_dict()).lower()
    assert "describe" not in repr(visit_access.to_dict()).lower()


def test_unknown_relationship_stage_fails_closed_to_unknown() -> None:
    projected = project_private_world(
        PrivateWorldSnapshot(relationship_stage="custom_future_stage")
    )

    assert projected.behavior.relationship_stage is RelationshipStage.UNKNOWN
