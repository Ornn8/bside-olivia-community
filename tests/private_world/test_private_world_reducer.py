from datetime import datetime, timedelta, timezone

import pytest

from private_world_port import PrivateWorldSnapshot
from private_world_reducer import (
    ReducerEvent,
    ReducerEventKind,
    ReducerInputError,
    reduce_private_world,
)


NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


def _event(kind: ReducerEventKind, **changes: object) -> ReducerEvent:
    values: dict[str, object] = {
        "kind": kind,
        "occurred_at": NOW,
        "semantic_key": f"semantic.{kind.value}",
    }
    values.update(changes)
    return ReducerEvent(**values)  # type: ignore[arg-type]


def test_boundary_respect_changes_state_slowly_and_explains_delta() -> None:
    before = PrivateWorldSnapshot(version=3, trust=10, comfort=20)

    result = reduce_private_world(
        before, _event(ReducerEventKind.BOUNDARY_RESPECTED)
    )

    assert before == PrivateWorldSnapshot(version=3, trust=10, comfort=20)
    assert result.snapshot.version == 4
    assert result.snapshot.trust == 11
    assert result.snapshot.comfort == 21
    assert result.delta.applied is True
    assert result.delta.reason_code == "BOUNDARY_RESPECTED"
    assert {change.field for change in result.delta.changes} == {"trust", "comfort"}


@pytest.mark.parametrize(
    "kind",
    [
        ReducerEventKind.HIGH_FREQUENCY_MESSAGE,
        ReducerEventKind.GIFT,
        ReducerEventKind.REPEATED_PHRASE,
        ReducerEventKind.CONFESSION,
        ReducerEventKind.INACTIVITY,
    ],
)
def test_spam_gifts_confession_and_inactivity_do_not_upgrade_state(
    kind: ReducerEventKind,
) -> None:
    before = PrivateWorldSnapshot(version=7, trust=30, closeness=30)

    result = reduce_private_world(before, _event(kind))

    assert result.snapshot == before
    assert result.delta.applied is False
    assert result.delta.changes == ()


def test_conflict_and_repair_apply_bounded_explainable_changes() -> None:
    before = PrivateWorldSnapshot(version=1, trust=1, comfort=1, tension=99)
    conflict = reduce_private_world(before, _event(ReducerEventKind.CONFLICT))

    assert conflict.snapshot.trust == 0
    assert conflict.snapshot.comfort == 0
    assert conflict.snapshot.tension == 100

    repair = reduce_private_world(
        conflict.snapshot, _event(ReducerEventKind.REPAIR)
    )
    assert repair.snapshot.trust == 1
    assert repair.snapshot.comfort == 1
    assert repair.snapshot.tension == 98


def test_stage_never_changes_from_score_threshold_alone() -> None:
    high_scores = PrivateWorldSnapshot(
        version=1,
        familiarity=100,
        trust=100,
        comfort=100,
        closeness=100,
        relationship_stage="acquaintance",
    )

    ordinary = reduce_private_world(
        high_scores, _event(ReducerEventKind.BOUNDARY_RESPECTED)
    )
    assert ordinary.snapshot.relationship_stage == "acquaintance"

    confirmed = reduce_private_world(
        ordinary.snapshot,
        _event(
            ReducerEventKind.STAGE_CONFIRMED,
            target_stage="familiar",
            basis_event_ids=("basis-1",),
        ),
    )
    assert confirmed.snapshot.relationship_stage == "familiar"

    with pytest.raises(ReducerInputError):
        reduce_private_world(
            ordinary.snapshot,
            _event(ReducerEventKind.STAGE_CONFIRMED, target_stage="close"),
        )


def test_same_semantic_event_is_deduplicated_inside_fixed_window() -> None:
    before = PrivateWorldSnapshot(version=1, trust=10)
    duplicate = _event(
        ReducerEventKind.BOUNDARY_RESPECTED,
        last_equivalent_at=NOW - timedelta(hours=2),
    )

    blocked = reduce_private_world(before, duplicate)
    assert blocked.snapshot == before
    assert blocked.delta.reason_code == "SEMANTIC_DUPLICATE"

    outside_window = _event(
        ReducerEventKind.BOUNDARY_RESPECTED,
        last_equivalent_at=NOW - timedelta(hours=25),
    )
    assert reduce_private_world(before, outside_window).delta.applied is True
