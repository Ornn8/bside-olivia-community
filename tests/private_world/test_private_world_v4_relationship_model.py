from datetime import datetime, timedelta, timezone

import pytest

from private_world_port import (
    AcknowledgedAffection,
    ActiveBoundary,
    AffectionIntensity,
    AffectionScope,
    PrivateWorldSnapshot,
)
from private_world_reducer import (
    ReducerEvent,
    ReducerEventKind,
    ReducerInputError,
    reduce_private_world,
)
from runtime.memory.private_world_projection import project_private_world


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _event(kind: ReducerEventKind, **payload: object) -> ReducerEvent:
    return ReducerEvent(
        kind=kind,
        occurred_at=NOW,
        semantic_key=f"semantic.{kind.value}",
        canonical_reply_id="reply.canonical.1",
        **payload,
    )


def test_only_canonical_character_events_set_and_withdraw_boundaries() -> None:
    boundary = ActiveBoundary(
        boundary_id="boundary.no_offline_meeting",
        set_at=NOW.isoformat(),
        scope="offline_meeting",
    )
    established = reduce_private_world(
        PrivateWorldSnapshot(),
        _event(ReducerEventKind.CHARACTER_BOUNDARY_SET, boundary=boundary),
    )

    assert established.snapshot.active_boundaries == (boundary,)
    assert reduce_private_world(
        established.snapshot,
        ReducerEvent(
            kind=ReducerEventKind.CONFESSION,
            occurred_at=NOW + timedelta(days=1),
            semantic_key="user.claimed.boundary",
        ),
    ).snapshot.active_boundaries == (boundary,)

    withdrawn = reduce_private_world(
        established.snapshot,
        _event(
            ReducerEventKind.CHARACTER_BOUNDARY_WITHDRAWN,
            boundary_id=boundary.boundary_id,
        ),
    )
    assert withdrawn.snapshot.active_boundaries == ()


def test_affection_only_increases_on_a_stronger_character_statement() -> None:
    warmth = AcknowledgedAffection(
        intensity=AffectionIntensity.WARMTH,
        statement_ref_id="reply.canonical.1.line.2",
        scope=AffectionScope.ONGOING_CORRESPONDENCE,
    )
    established = reduce_private_world(
        PrivateWorldSnapshot(),
        _event(
            ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED,
            acknowledged_affection=warmth,
            asserted_affection_scope=AffectionScope.ONGOING_CORRESPONDENCE,
        ),
    )
    weaker = AcknowledgedAffection(
        intensity=AffectionIntensity.WARMTH,
        statement_ref_id="reply.canonical.2.line.1",
        scope=AffectionScope.THIS_REPLY,
    )
    unchanged = reduce_private_world(
        established.snapshot,
        ReducerEvent(
            kind=ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED,
            occurred_at=NOW + timedelta(days=1),
            semantic_key="affection.weaker",
            canonical_reply_id="reply.canonical.2",
            acknowledged_affection=weaker,
            asserted_affection_scope=AffectionScope.THIS_REPLY,
        ),
    )

    assert unchanged.snapshot.acknowledged_affection == warmth
    assert unchanged.delta.reason_code == "AFFECTION_NOT_STRONGER"


def test_affection_scope_cannot_exceed_the_character_statement() -> None:
    with pytest.raises(ReducerInputError, match="scope"):
        _event(
            ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED,
            acknowledged_affection=AcknowledgedAffection(
                intensity=AffectionIntensity.CARE,
                statement_ref_id="reply.canonical.1.line.3",
                scope=AffectionScope.RELATIONSHIP,
            ),
            asserted_affection_scope=AffectionScope.THIS_REPLY,
        )


def test_v4_relationship_facts_project_without_hidden_control_state() -> None:
    boundary = ActiveBoundary(
        boundary_id="boundary.no_offline_meeting",
        set_at=NOW.isoformat(),
        scope="offline_meeting",
    )
    affection = AcknowledgedAffection(
        intensity=AffectionIntensity.CARE,
        statement_ref_id="reply.canonical.1.line.3",
        scope=AffectionScope.ONGOING_CORRESPONDENCE,
    )

    projected = project_private_world(
        PrivateWorldSnapshot(
            active_boundaries=(boundary,),
            acknowledged_affection=affection,
        )
    ).behavior.to_dict()

    assert projected["active_boundaries"] == [
        {
            "boundary_id": "boundary.no_offline_meeting",
            "scope": "offline_meeting",
        }
    ]
    assert projected["acknowledged_affection"] == {
        "intensity": "care",
        "statement_ref_id": "reply.canonical.1.line.3",
        "scope": "ongoing_correspondence",
    }


def test_old_snapshot_defaults_v4_relationship_facts_to_empty() -> None:
    snapshot = PrivateWorldSnapshot(version=9, trust=30)

    assert snapshot.active_boundaries == ()
    assert snapshot.acknowledged_affection is None
