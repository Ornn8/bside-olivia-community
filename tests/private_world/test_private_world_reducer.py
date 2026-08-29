from datetime import datetime, timedelta, timezone

import pytest

from private_world_port import IntimacyGrant, PrivateWorldSnapshot
from private_world_reducer import (
    ReducerEvent,
    ReducerEventKind,
    ReducerInputError,
    reduce_private_world,
)
from runtime.reply.reply_context import IntimacyTier


NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


def _event(kind: ReducerEventKind, **changes: object) -> ReducerEvent:
    values: dict[str, object] = {
        "kind": kind,
        "occurred_at": NOW,
        "semantic_key": f"semantic.{kind.value}",
    }
    values.update(changes)
    return ReducerEvent(**values)  # type: ignore[arg-type]


def _grant(number: int, tier: IntimacyTier) -> IntimacyGrant:
    return IntimacyGrant(
        grant_id=f"intimacy.synthetic-{number}",
        tier=tier,
        statement=f"Synthetic intimacy grant {number}.",
    )


@pytest.mark.parametrize(
    ("stage", "tier", "applied", "reason_code"),
    [
        ("familiar", IntimacyTier.LIGHT_CONTACT, False, "INTIMACY_EXCEEDS_STAGE"),
        ("close", IntimacyTier.LIGHT_CONTACT, True, "INTIMACY_GRANTED"),
        ("close", IntimacyTier.CLOSE_CONTACT, False, "INTIMACY_EXCEEDS_STAGE"),
        ("committed", IntimacyTier.CLOSE_CONTACT, True, "INTIMACY_GRANTED"),
        ("custom_future_stage", IntimacyTier.LIGHT_CONTACT, False, "INTIMACY_EXCEEDS_STAGE"),
    ],
)
def test_intimacy_grant_respects_the_stage_ceiling(
    stage: str,
    tier: IntimacyTier,
    applied: bool,
    reason_code: str,
) -> None:
    before = PrivateWorldSnapshot(version=3, relationship_stage=stage)
    grant = _grant(1, tier)

    result = reduce_private_world(
        before,
        _event(ReducerEventKind.INTIMACY_GRANTED, intimacy_grant=grant),
    )

    assert result.delta.applied is applied
    assert result.delta.reason_code == reason_code
    assert result.snapshot.intimacy_grants == ((grant,) if applied else ())
    assert result.snapshot.closeness == (2 if applied else 0)


def test_boundary_respect_changes_state_slowly_and_explains_delta() -> None:
    before = PrivateWorldSnapshot(version=3, trust=10, comfort=20)

    result = reduce_private_world(
        before, _event(ReducerEventKind.BOUNDARY_RESPECTED)
    )

    assert before == PrivateWorldSnapshot(version=3, trust=10, comfort=20)
    assert result.snapshot.version == 4
    assert result.snapshot.trust == 11
    assert result.snapshot.comfort == 21
    assert result.snapshot.familiarity == 1
    assert result.delta.applied is True
    assert result.delta.reason_code == "BOUNDARY_RESPECTED"
    assert {change.field for change in result.delta.changes} == {
        "trust",
        "comfort",
        "familiarity",
        "growth_window_start",
        "growth_used",
    }


def test_weekly_growth_cap_blocks_only_growth_and_resets_at_seven_days() -> None:
    snapshot = PrivateWorldSnapshot(version=1)
    for index in range(6):
        snapshot = reduce_private_world(
            snapshot,
            _event(
                ReducerEventKind.BOUNDARY_RESPECTED,
                occurred_at=NOW + timedelta(hours=index),
                semantic_key=f"boundary.synthetic-{index}",
            ),
        ).snapshot

    assert snapshot.familiarity == 6
    assert snapshot.growth_used == 6
    assert snapshot.growth_window_start == NOW.isoformat()

    capped = reduce_private_world(
        snapshot,
        _event(
            ReducerEventKind.BOUNDARY_RESPECTED,
            occurred_at=NOW + timedelta(days=6),
            semantic_key="boundary.synthetic-capped",
        ),
    )
    assert capped.delta.applied is True
    assert capped.delta.reason_code == "GROWTH_CAP_REACHED"
    assert capped.snapshot.familiarity == 6
    assert capped.snapshot.trust == 7
    assert capped.snapshot.comfort == 7
    assert capped.snapshot.growth_used == 6

    reset = reduce_private_world(
        capped.snapshot,
        _event(
            ReducerEventKind.BOUNDARY_RESPECTED,
            occurred_at=NOW + timedelta(days=7),
            semantic_key="boundary.synthetic-reset",
        ),
    )
    assert reset.delta.reason_code == "BOUNDARY_RESPECTED"
    assert reset.snapshot.familiarity == 7
    assert reset.snapshot.growth_used == 1
    assert reset.snapshot.growth_window_start == (
        NOW + timedelta(days=7)
    ).isoformat()


def test_growth_cap_keeps_an_admitted_grant_without_free_growth() -> None:
    grant = _grant(2, IntimacyTier.LIGHT_CONTACT)
    before = PrivateWorldSnapshot(
        version=4,
        relationship_stage="close",
        closeness=12,
        growth_window_start=NOW.isoformat(),
        growth_used=6,
    )

    result = reduce_private_world(
        before,
        _event(ReducerEventKind.INTIMACY_GRANTED, intimacy_grant=grant),
    )

    assert result.delta.applied is True
    assert result.delta.reason_code == "GROWTH_CAP_REACHED"
    assert result.snapshot.intimacy_grants == (grant,)
    assert result.snapshot.closeness == 12
    assert result.snapshot.growth_used == 6


def test_growth_can_exactly_fill_the_remaining_weekly_quota() -> None:
    grant = _grant(3, IntimacyTier.LIGHT_CONTACT)
    before = PrivateWorldSnapshot(
        relationship_stage="close",
        closeness=8,
        growth_window_start=NOW.isoformat(),
        growth_used=4,
    )

    result = reduce_private_world(
        before,
        _event(ReducerEventKind.INTIMACY_GRANTED, intimacy_grant=grant),
    )

    assert result.delta.reason_code == "INTIMACY_GRANTED"
    assert result.snapshot.closeness == 10
    assert result.snapshot.growth_used == 6


def test_event_before_the_window_start_keeps_the_existing_window() -> None:
    before = PrivateWorldSnapshot(
        growth_window_start=NOW.isoformat(),
        growth_used=1,
    )

    result = reduce_private_world(
        before,
        _event(
            ReducerEventKind.BOUNDARY_RESPECTED,
            occurred_at=NOW - timedelta(days=1),
        ),
    )

    assert result.snapshot.growth_window_start == NOW.isoformat()
    assert result.snapshot.growth_used == 2
    assert result.snapshot.familiarity == 1


def test_exhausted_growth_with_bounded_non_growth_is_a_cap_noop() -> None:
    before = PrivateWorldSnapshot(
        familiarity=100,
        trust=100,
        comfort=100,
        growth_window_start=NOW.isoformat(),
        growth_used=6,
    )

    result = reduce_private_world(
        before,
        _event(ReducerEventKind.BOUNDARY_RESPECTED),
    )

    assert result.snapshot == before
    assert result.delta.applied is False
    assert result.delta.reason_code == "GROWTH_CAP_REACHED"


def test_grant_admission_order_precedes_capacity_and_growth_checks() -> None:
    existing = tuple(
        _grant(index, IntimacyTier.LIGHT_CONTACT)
        for index in range(1, 17)
    )
    full = PrivateWorldSnapshot(
        version=9,
        relationship_stage="close",
        intimacy_grants=existing,
        growth_window_start=NOW.isoformat(),
        growth_used=6,
    )

    duplicate = reduce_private_world(
        full,
        _event(
            ReducerEventKind.INTIMACY_GRANTED,
            intimacy_grant=existing[0],
        ),
    )
    assert duplicate.snapshot == full
    assert duplicate.delta.reason_code == "INTIMACY_ALREADY_GRANTED"

    at_capacity = reduce_private_world(
        full,
        _event(
            ReducerEventKind.INTIMACY_GRANTED,
            intimacy_grant=_grant(17, IntimacyTier.LIGHT_CONTACT),
        ),
    )
    assert at_capacity.snapshot == full
    assert at_capacity.delta.reason_code == "INTIMACY_LIMIT_REACHED"

    exceeds_stage = reduce_private_world(
        PrivateWorldSnapshot(
            version=9,
            relationship_stage="familiar",
            intimacy_grants=existing,
        ),
        _event(
            ReducerEventKind.INTIMACY_GRANTED,
            intimacy_grant=existing[0],
        ),
    )
    assert exceeds_stage.delta.reason_code == "INTIMACY_EXCEEDS_STAGE"


def test_stage_growth_requires_an_actual_explicit_stage_change() -> None:
    before = PrivateWorldSnapshot(
        version=2,
        relationship_stage="familiar",
        familiarity=10,
        closeness=20,
    )

    repeated = reduce_private_world(
        before,
        _event(
            ReducerEventKind.STAGE_CONFIRMED,
            target_stage="familiar",
            basis_event_ids=("basis.repeat",),
        ),
    )
    assert repeated.snapshot == before
    assert repeated.delta.reason_code == "BOUNDED_NO_CHANGE"

    downgraded = reduce_private_world(
        before,
        _event(
            ReducerEventKind.STAGE_CONFIRMED,
            target_stage="acquaintance",
            basis_event_ids=("basis.downgrade",),
        ),
    )
    assert downgraded.snapshot.relationship_stage == "acquaintance"
    assert downgraded.snapshot.familiarity == 13
    assert downgraded.snapshot.closeness == 25
    assert downgraded.snapshot.growth_used == 0


@pytest.mark.parametrize(
    "kind",
    [
        ReducerEventKind.CANONICAL_REPLY_DELIVERED,
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
    assert result.delta.reason_code == "NO_RELATIONSHIP_EFFECT"
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


def test_event_sequences_never_downgrade_a_stage_implicitly() -> None:
    snapshot = PrivateWorldSnapshot(
        relationship_stage="committed",
        trust=1,
        comfort=1,
        tension=99,
    )
    for index, kind in enumerate(
        (
            ReducerEventKind.CONFLICT,
            ReducerEventKind.REPAIR,
            ReducerEventKind.BOUNDARY_RESPECTED,
            ReducerEventKind.INACTIVITY,
            ReducerEventKind.CONFESSION,
        )
    ):
        snapshot = reduce_private_world(
            snapshot,
            _event(kind, semantic_key=f"sequence.synthetic-{index}"),
        ).snapshot

    assert snapshot.relationship_stage == "committed"


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


def test_intimacy_grant_obeys_the_same_semantic_deduplication_window() -> None:
    before = PrivateWorldSnapshot(
        version=3,
        relationship_stage="close",
    )
    grant = _grant(30, IntimacyTier.LIGHT_CONTACT)

    blocked = reduce_private_world(
        before,
        _event(
            ReducerEventKind.INTIMACY_GRANTED,
            intimacy_grant=grant,
            last_equivalent_at=NOW - timedelta(hours=2),
        ),
    )
    assert blocked.snapshot == before
    assert blocked.delta.reason_code == "SEMANTIC_DUPLICATE"

    applied = reduce_private_world(
        before,
        _event(
            ReducerEventKind.INTIMACY_GRANTED,
            intimacy_grant=grant,
            last_equivalent_at=NOW - timedelta(hours=24),
        ),
    )
    assert applied.delta.reason_code == "INTIMACY_GRANTED"
    assert applied.snapshot.intimacy_grants == (grant,)


def test_growth_scores_remain_bounded_and_window_start_is_utc() -> None:
    offset_time = NOW.astimezone(timezone(timedelta(hours=9)))
    boundary = reduce_private_world(
        PrivateWorldSnapshot(
            familiarity=100,
            trust=100,
            comfort=100,
        ),
        _event(
            ReducerEventKind.BOUNDARY_RESPECTED,
            occurred_at=offset_time,
        ),
    )
    assert boundary.snapshot.familiarity == 100
    assert boundary.snapshot.trust == 100
    assert boundary.snapshot.comfort == 100
    assert boundary.snapshot.growth_window_start == NOW.isoformat()

    confirmed = reduce_private_world(
        PrivateWorldSnapshot(
            relationship_stage="familiar",
            familiarity=99,
            closeness=99,
        ),
        _event(
            ReducerEventKind.STAGE_CONFIRMED,
            target_stage="close",
            basis_event_ids=("basis.bounded",),
        ),
    )
    assert confirmed.snapshot.familiarity == 100
    assert confirmed.snapshot.closeness == 100


def test_intimacy_event_payload_is_strictly_exclusive_and_nonzero() -> None:
    grant = _grant(31, IntimacyTier.LIGHT_CONTACT)
    with pytest.raises(ReducerInputError):
        _event(
            ReducerEventKind.BOUNDARY_RESPECTED,
            intimacy_grant=grant,
        )
    with pytest.raises(ReducerInputError):
        _event(
            ReducerEventKind.STAGE_CONFIRMED,
            target_stage="close",
            basis_event_ids=("basis.invalid",),
            intimacy_grant=grant,
        )
    with pytest.raises(ReducerInputError):
        _event(
            ReducerEventKind.INTIMACY_GRANTED,
            intimacy_grant=IntimacyGrant(
                grant_id="intimacy.synthetic-none",
                tier=IntimacyTier.NONE,
                statement="A synthetic non-grant.",
            ),
        )
