from __future__ import annotations

from datetime import datetime, timezone

import pytest

from private_world_commands import (
    ConfirmRelationshipStage,
    DeleteContinuationFact,
    GrantNickname,
    PrivateWorldActor,
    PrivateWorldCommand,
    PrivateWorldCommandError,
    PrivateWorldCommandSource,
    RecordBoundaryRespected,
    RecordConflict,
    RecordRepair,
    RevokeNickname,
    SetContinuationAwareness,
    SetHomeAccess,
    UpsertContinuationFact,
)
from private_world_port import ContinuationAwareness, HomeAccess
from runtime.reply.reply_context import RelationshipStage


NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)


def _common(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "command_id": "command.synthetic-1",
        "idempotency_key": "idempotency.synthetic-1",
        "actor": PrivateWorldActor.LOCAL_USER,
        "source": PrivateWorldCommandSource.CONTROL_CENTER,
        "occurred_at": NOW,
        "reason": "synthetic confirmed change",
        "evidence_refs": (
            "letter:synthetic-1",
            "reply:synthetic-1:1",
        ),
    }
    values.update(changes)
    return values


@pytest.mark.parametrize(
    "command",
    [
        RecordBoundaryRespected(**_common()),
        RecordConflict(**_common()),
        RecordRepair(**_common()),
        ConfirmRelationshipStage(
            **_common(),
            target_stage=RelationshipStage.FAMILIAR,
            basis_event_ids=("event.synthetic-1",),
        ),
        GrantNickname(**_common(), nickname="小河豚"),
        RevokeNickname(**_common(), nickname="小河豚"),
        SetHomeAccess(
            **_common(),
            home_access=HomeAccess.VISIT_ACCESS,
        ),
        UpsertContinuationFact(
            **_common(),
            fact_id="continuation.synthetic-1",
            statement="一项只用于合成测试的未来安排。",
            awareness=ContinuationAwareness.CONTROL_ONLY,
        ),
        SetContinuationAwareness(
            **_common(),
            fact_id="continuation.synthetic-1",
            awareness=ContinuationAwareness.CHARACTER_KNOWN,
        ),
        DeleteContinuationFact(
            **_common(),
            fact_id="continuation.synthetic-1",
        ),
    ],
)
def test_commands_are_typed_serializable_and_hide_relationship_scores(
    command: PrivateWorldCommand,
) -> None:
    payload = command.to_dict()

    assert payload["command_id"] == "command.synthetic-1"
    assert payload["idempotency_key"] == "idempotency.synthetic-1"
    assert payload["actor"] == "local_user"
    assert payload["source"] == "control_center"
    assert payload["occurred_at"] == NOW.isoformat()
    assert payload["evidence_refs"] == [
        "letter:synthetic-1",
        "reply:synthetic-1:1",
    ]
    serialized = repr(payload)
    for hidden in (
        "familiarity",
        "trust",
        "comfort",
        "closeness",
        "tension",
    ):
        assert hidden not in serialized


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"command_id": ""}, "command id is invalid"),
        (
            {"idempotency_key": "contains whitespace"},
            "idempotency key is invalid",
        ),
        ({"actor": "local_user"}, "actor is invalid"),
        ({"source": "control_center"}, "source is invalid"),
        (
            {"occurred_at": datetime(2026, 8, 22, 20, 0)},
            "occurred_at must be timezone-aware",
        ),
        ({"reason": ""}, "reason is invalid"),
        ({"reason": "x" * 281}, "reason is invalid"),
        ({"reason": "bad\nreason"}, "reason is invalid"),
        (
            {"evidence_refs": ("same", "same")},
            "evidence refs are invalid",
        ),
        (
            {
                "evidence_refs": tuple(
                    f"e:{index}" for index in range(9)
                )
            },
            "evidence refs are invalid",
        ),
    ],
)
def test_common_command_metadata_is_strict(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(PrivateWorldCommandError, match=message):
        RecordConflict(**_common(**changes))  # type: ignore[arg-type]


def test_base_command_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError, match="concrete"):
        PrivateWorldCommand(**_common())  # type: ignore[abstract]


@pytest.mark.parametrize(
    "changes",
    [
        {"target_stage": "familiar"},
        {"basis_event_ids": ()},
        {"basis_event_ids": ("same", "same")},
    ],
)
def test_stage_confirmation_requires_valid_explicit_basis(
    changes: dict[str, object],
) -> None:
    values = {
        **_common(),
        "target_stage": RelationshipStage.FAMILIAR,
        "basis_event_ids": ("event.synthetic-1",),
        **changes,
    }
    with pytest.raises(PrivateWorldCommandError):
        ConfirmRelationshipStage(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "nickname",
    ["", "two words", "x" * 33, "bad\tname"],
)
def test_nickname_commands_reject_unbounded_or_spaced_values(
    nickname: str,
) -> None:
    with pytest.raises(PrivateWorldCommandError):
        GrantNickname(**_common(), nickname=nickname)


def test_home_access_requires_typed_enum() -> None:
    with pytest.raises(PrivateWorldCommandError, match="home access"):
        SetHomeAccess(
            **_common(),
            home_access="visit_access",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("fact_id", "statement", "awareness"),
    [
        (
            "invalid id",
            "synthetic",
            ContinuationAwareness.PENDING,
        ),
        ("valid", "", ContinuationAwareness.PENDING),
        ("valid", "synthetic", "pending"),
    ],
)
def test_continuation_upsert_reuses_port_validation(
    fact_id: str,
    statement: str,
    awareness: object,
) -> None:
    with pytest.raises(PrivateWorldCommandError):
        UpsertContinuationFact(
            **_common(),
            fact_id=fact_id,
            statement=statement,
            awareness=awareness,  # type: ignore[arg-type]
        )
