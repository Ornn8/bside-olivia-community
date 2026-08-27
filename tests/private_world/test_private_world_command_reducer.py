from __future__ import annotations

from datetime import datetime, timezone

import pytest

from private_world_commands import (
    ConfirmRelationshipStage,
    DeleteContinuationFact,
    GrantNickname,
    PrivateWorldActor,
    PrivateWorldCommandSource,
    RecordBoundaryRespected,
    RecordConflict,
    RecordRepair,
    RevokeNickname,
    SetContinuationAwareness,
    SetHomeAccess,
    UpsertContinuationFact,
)
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)
from private_world_reducer import (
    ReducerInputError,
    reduce_private_world_command,
)
from runtime.reply.reply_context import RelationshipStage


NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)


def _common(sequence: int = 1) -> dict[str, object]:
    return {
        "command_id": f"command.synthetic-{sequence}",
        "idempotency_key": f"idempotency.synthetic-{sequence}",
        "actor": PrivateWorldActor.LOCAL_USER,
        "source": PrivateWorldCommandSource.CONTROL_CENTER,
        "occurred_at": NOW,
        "reason": "synthetic confirmed change",
        "evidence_refs": (f"letter:synthetic-{sequence}",),
    }


def test_relationship_commands_reuse_slow_bounded_reducer_policy() -> None:
    before = PrivateWorldSnapshot(
        version=4,
        trust=99,
        comfort=100,
        tension=1,
    )

    boundary = reduce_private_world_command(
        before,
        RecordBoundaryRespected(**_common(1)),
    )
    assert before == PrivateWorldSnapshot(
        version=4,
        trust=99,
        comfort=100,
        tension=1,
    )
    assert boundary.snapshot.version == 5
    assert boundary.snapshot.trust == 100
    assert boundary.snapshot.comfort == 100
    assert boundary.delta.reason_code == "BOUNDARY_RESPECTED"
    assert tuple(
        change.field for change in boundary.delta.changes
    ) == ("trust",)

    conflict = reduce_private_world_command(
        boundary.snapshot,
        RecordConflict(**_common(2)),
    )
    assert conflict.snapshot.trust == 98
    assert conflict.snapshot.comfort == 98
    assert conflict.snapshot.tension == 4

    repair = reduce_private_world_command(
        conflict.snapshot,
        RecordRepair(**_common(3)),
    )
    assert repair.snapshot.trust == 99
    assert repair.snapshot.comfort == 99
    assert repair.snapshot.tension == 2


def test_stage_confirmation_uses_typed_bounded_stage() -> None:
    before = PrivateWorldSnapshot(
        version=2,
        relationship_stage="acquaintance",
    )
    command = ConfirmRelationshipStage(
        **_common(),
        target_stage=RelationshipStage.FAMILIAR,
        basis_event_ids=("event.synthetic-1",),
    )

    result = reduce_private_world_command(before, command)

    assert result.snapshot.version == 3
    assert result.snapshot.relationship_stage == "familiar"
    assert result.delta.reason_code == "STAGE_CONFIRMED"
    assert result.delta.changes[0].before == "acquaintance"
    assert result.delta.changes[0].after == "familiar"

    repeated = reduce_private_world_command(result.snapshot, command)
    assert repeated.snapshot == result.snapshot
    assert repeated.delta.applied is False
    assert repeated.delta.reason_code == "BOUNDED_NO_CHANGE"


def test_nickname_grant_revoke_preserve_order_and_are_idempotent() -> None:
    before = PrivateWorldSnapshot(
        version=1,
        nickname_permissions=("旅行者",),
    )
    grant = GrantNickname(**_common(1), nickname="小河豚")

    added = reduce_private_world_command(before, grant)
    assert added.snapshot.version == 2
    assert added.snapshot.nickname_permissions == (
        "旅行者",
        "小河豚",
    )
    assert added.delta.reason_code == "GRANT_NICKNAME"

    duplicate = reduce_private_world_command(added.snapshot, grant)
    assert duplicate.snapshot == added.snapshot
    assert duplicate.delta.reason_code == "NICKNAME_ALREADY_GRANTED"

    revoked = reduce_private_world_command(
        added.snapshot,
        RevokeNickname(**_common(2), nickname="旅行者"),
    )
    assert revoked.snapshot.nickname_permissions == ("小河豚",)
    assert revoked.delta.reason_code == "REVOKE_NICKNAME"

    absent = reduce_private_world_command(
        revoked.snapshot,
        RevokeNickname(**_common(3), nickname="旅行者"),
    )
    assert absent.snapshot == revoked.snapshot
    assert absent.delta.reason_code == "NICKNAME_NOT_GRANTED"


def test_nickname_limit_is_a_noop_without_version_change() -> None:
    before = PrivateWorldSnapshot(
        version=9,
        nickname_permissions=tuple(
            f"称呼{index}" for index in range(16)
        ),
    )

    result = reduce_private_world_command(
        before,
        GrantNickname(**_common(), nickname="新称呼"),
    )

    assert result.snapshot == before
    assert result.delta.reason_code == "NICKNAME_LIMIT_REACHED"


def test_home_access_uses_enum_and_noop_does_not_bump_version() -> None:
    before = PrivateWorldSnapshot(
        version=5,
        home_access=HomeAccess.NO_ACCESS,
    )
    command = SetHomeAccess(
        **_common(),
        home_access=HomeAccess.VISIT_ACCESS,
    )

    changed = reduce_private_world_command(before, command)
    assert changed.snapshot.version == 6
    assert changed.snapshot.home_access is HomeAccess.VISIT_ACCESS
    assert changed.delta.reason_code == "SET_HOME_ACCESS"

    unchanged = reduce_private_world_command(changed.snapshot, command)
    assert unchanged.snapshot == changed.snapshot
    assert unchanged.delta.reason_code == "HOME_ACCESS_UNCHANGED"


def test_continuation_upsert_updates_in_place_and_preserves_order() -> None:
    before = PrivateWorldSnapshot(
        version=1,
        continuation_facts=(
            LocalContinuationFact(
                "continuation.first",
                "第一条合成测试事实。",
                ContinuationAwareness.CONTROL_ONLY,
            ),
            LocalContinuationFact(
                "continuation.second",
                "第二条合成测试事实。",
                ContinuationAwareness.PENDING,
            ),
        ),
    )

    updated = reduce_private_world_command(
        before,
        UpsertContinuationFact(
            **_common(1),
            fact_id="continuation.first",
            statement="第一条合成测试事实已经更新。",
            awareness=ContinuationAwareness.CHARACTER_KNOWN,
        ),
    )
    assert updated.snapshot.version == 2
    assert tuple(
        fact.fact_id for fact in updated.snapshot.continuation_facts
    ) == ("continuation.first", "continuation.second")
    assert updated.snapshot.continuation_facts[0].statement.endswith(
        "已经更新。"
    )
    assert (
        updated.snapshot.continuation_facts[0].awareness
        is ContinuationAwareness.CHARACTER_KNOWN
    )

    added = reduce_private_world_command(
        updated.snapshot,
        UpsertContinuationFact(
            **_common(2),
            fact_id="continuation.third",
            statement="第三条合成测试事实。",
            awareness=ContinuationAwareness.CONTROL_ONLY,
        ),
    )
    assert tuple(
        fact.fact_id for fact in added.snapshot.continuation_facts
    ) == (
        "continuation.first",
        "continuation.second",
        "continuation.third",
    )

    duplicate = reduce_private_world_command(
        added.snapshot,
        UpsertContinuationFact(
            **_common(3),
            fact_id="continuation.third",
            statement="第三条合成测试事实。",
            awareness=ContinuationAwareness.CONTROL_ONLY,
        ),
    )
    assert duplicate.snapshot == added.snapshot
    assert duplicate.delta.reason_code == "CONTINUATION_UNCHANGED"


def test_continuation_limit_is_a_noop() -> None:
    facts = tuple(
        LocalContinuationFact(
            f"continuation.synthetic-{index}",
            f"第{index}条合成事实。",
            ContinuationAwareness.CONTROL_ONLY,
        )
        for index in range(32)
    )
    before = PrivateWorldSnapshot(
        version=7,
        continuation_facts=facts,
    )

    result = reduce_private_world_command(
        before,
        UpsertContinuationFact(
            **_common(),
            fact_id="continuation.overflow",
            statement="超出上限的合成事实。",
            awareness=ContinuationAwareness.PENDING,
        ),
    )

    assert result.snapshot == before
    assert result.delta.reason_code == "CONTINUATION_LIMIT_REACHED"


def test_continuation_awareness_and_delete_require_existing_fact() -> None:
    fact = LocalContinuationFact(
        "continuation.synthetic",
        "一条合成测试事实。",
        ContinuationAwareness.CONTROL_ONLY,
    )
    before = PrivateWorldSnapshot(
        version=3,
        continuation_facts=(fact,),
    )

    known = reduce_private_world_command(
        before,
        SetContinuationAwareness(
            **_common(1),
            fact_id=fact.fact_id,
            awareness=ContinuationAwareness.CHARACTER_KNOWN,
        ),
    )
    assert known.snapshot.version == 4
    assert (
        known.snapshot.continuation_facts[0].awareness
        is ContinuationAwareness.CHARACTER_KNOWN
    )

    missing_awareness = reduce_private_world_command(
        known.snapshot,
        SetContinuationAwareness(
            **_common(2),
            fact_id="continuation.missing",
            awareness=ContinuationAwareness.PENDING,
        ),
    )
    assert missing_awareness.snapshot == known.snapshot
    assert (
        missing_awareness.delta.reason_code
        == "CONTINUATION_NOT_FOUND"
    )

    deleted = reduce_private_world_command(
        known.snapshot,
        DeleteContinuationFact(
            **_common(3),
            fact_id=fact.fact_id,
        ),
    )
    assert deleted.snapshot.version == 5
    assert deleted.snapshot.continuation_facts == ()

    missing_delete = reduce_private_world_command(
        deleted.snapshot,
        DeleteContinuationFact(
            **_common(4),
            fact_id=fact.fact_id,
        ),
    )
    assert missing_delete.snapshot == deleted.snapshot
    assert missing_delete.delta.reason_code == "CONTINUATION_NOT_FOUND"


@pytest.mark.parametrize(
    "command",
    [
        GrantNickname(**_common(1), nickname="合成称呼"),
        SetHomeAccess(
            **_common(2),
            home_access=HomeAccess.ERRAND_ACCESS,
        ),
        UpsertContinuationFact(
            **_common(3),
            fact_id="continuation.synthetic",
            statement="一条合成测试事实。",
            awareness=ContinuationAwareness.CONTROL_ONLY,
        ),
    ],
)
def test_nonrelationship_commands_do_not_change_hidden_scores(
    command,
) -> None:
    before = PrivateWorldSnapshot(
        version=11,
        familiarity=12,
        trust=23,
        comfort=34,
        closeness=45,
        tension=56,
    )

    result = reduce_private_world_command(before, command)

    assert (
        result.snapshot.familiarity,
        result.snapshot.trust,
        result.snapshot.comfort,
        result.snapshot.closeness,
        result.snapshot.tension,
    ) == (12, 23, 34, 45, 56)


def test_command_reducer_requires_typed_inputs() -> None:
    with pytest.raises(
        ReducerInputError,
        match="typed snapshot and command",
    ):
        reduce_private_world_command(  # type: ignore[arg-type]
            {},
            RecordConflict(**_common()),
        )
    with pytest.raises(
        ReducerInputError,
        match="typed snapshot and command",
    ):
        reduce_private_world_command(  # type: ignore[arg-type]
            PrivateWorldSnapshot(),
            object(),
        )
