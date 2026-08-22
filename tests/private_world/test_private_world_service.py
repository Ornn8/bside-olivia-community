from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

import pytest

from private_world_commands import (
    ConfirmRelationshipStage,
    GrantNickname,
    PrivateWorldActor,
    PrivateWorldCommandSource,
    RecordConflict,
    RecordRepair,
    SetHomeAccess,
    UpsertContinuationFact,
)
from private_world_ledger import LedgerEvent, LedgerWriteError
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    PrivateWorldSnapshot,
)
from private_world_service import (
    CommandExecutionStatus,
    PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA,
    PrivateWorldCommandService,
    PrivateWorldCommandServiceError,
)
from reply_context import RelationshipStage


NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)


class FakeLedger:
    def __init__(self) -> None:
        self.current = PrivateWorldSnapshot()
        self.items: list[LedgerEvent] = []
        self.fail_writes = False
        self.fail_reads = False
        self._lock = Lock()

    def snapshot(self) -> PrivateWorldSnapshot:
        if self.fail_reads:
            raise LedgerWriteError("synthetic read failure")
        return self.current

    def events(self) -> tuple[LedgerEvent, ...]:
        if self.fail_reads:
            raise LedgerWriteError("synthetic read failure")
        return tuple(self.items)

    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
    ) -> bool:
        if self.fail_writes:
            raise LedgerWriteError("synthetic write failure")
        with self._lock:
            if any(
                item.event_id == event.event_id
                or item.delivery_id == event.delivery_id
                for item in self.items
            ):
                return False
            self.items.append(event)
            self.current = snapshot
            return True


def _common(
    sequence: int = 1,
    **changes: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "command_id": f"command.synthetic-{sequence}",
        "idempotency_key": f"idempotency.synthetic-{sequence}",
        "actor": PrivateWorldActor.LOCAL_USER,
        "source": PrivateWorldCommandSource.CONTROL_CENTER,
        "occurred_at": NOW,
        "reason": "synthetic confirmed change",
        "evidence_refs": (f"letter:synthetic-{sequence}",),
    }
    values.update(changes)
    return values


def test_service_applies_and_audits_without_payload_values() -> None:
    ledger = FakeLedger()
    service = PrivateWorldCommandService(ledger)
    command = UpsertContinuationFact(
        **_common(),
        fact_id="continuation.synthetic",
        statement="一条只用于合成测试的私人世界事实。",
        awareness=ContinuationAwareness.CONTROL_ONLY,
    )

    result = service.execute(command)

    assert result.status is CommandExecutionStatus.APPLIED
    assert result.snapshot_version == 2
    assert result.change_fields == ("continuation_facts",)
    assert ledger.current.continuation_facts[0].statement.endswith("事实。")
    assert len(ledger.items) == 1
    audit = ledger.items[0].payload
    assert audit["schema_version"] == PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA
    assert audit["command_kind"] == "upsert_continuation_fact"
    assert audit["actor"] == "local_user"
    assert audit["source"] == "control_center"
    assert audit["reason"] == "synthetic confirmed change"
    assert audit["evidence_refs"] == ["letter:synthetic-1"]
    assert audit["payload_fields"] == [
        "awareness",
        "fact_id",
        "statement",
    ]
    serialized = repr(audit)
    assert command.statement not in serialized
    assert command.fact_id not in serialized
    assert "control_only" not in serialized
    for value in (
        "familiarity",
        "trust",
        "comfort",
        "closeness",
        "tension",
    ):
        assert f"'{value}':" not in serialized


def test_exact_retry_returns_duplicate_without_second_write() -> None:
    ledger = FakeLedger()
    service = PrivateWorldCommandService(ledger)
    command = SetHomeAccess(
        **_common(),
        home_access=HomeAccess.VISIT_ACCESS,
    )

    first = service.execute(command)
    duplicate = service.execute(command)

    assert first.status is CommandExecutionStatus.APPLIED
    assert duplicate.status is CommandExecutionStatus.DUPLICATE
    assert duplicate.event_id == first.event_id
    assert duplicate.snapshot_version == first.snapshot_version
    assert len(ledger.items) == 1
    assert ledger.current.version == 2


@pytest.mark.parametrize("reuse", ["command_id", "idempotency_key"])
def test_identity_reuse_with_different_command_is_rejected(
    reuse: str,
) -> None:
    ledger = FakeLedger()
    service = PrivateWorldCommandService(ledger)
    original = SetHomeAccess(
        **_common(1),
        home_access=HomeAccess.VISIT_ACCESS,
    )
    service.execute(original)
    changes: dict[str, object] = {}
    if reuse == "command_id":
        changes["command_id"] = original.command_id
    else:
        changes["idempotency_key"] = original.idempotency_key
    conflicting = GrantNickname(
        **_common(2, **changes),
        nickname="合成称呼",
    )

    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_IDENTITY_CONFLICT",
    ):
        service.execute(conflicting)
    assert len(ledger.items) == 1


def test_noop_is_audited_without_incrementing_snapshot() -> None:
    ledger = FakeLedger()
    service = PrivateWorldCommandService(ledger)
    first = SetHomeAccess(
        **_common(1),
        home_access=HomeAccess.VISIT_ACCESS,
    )
    repeated_with_new_identity = SetHomeAccess(
        **_common(2),
        home_access=HomeAccess.VISIT_ACCESS,
    )

    applied = service.execute(first)
    noop = service.execute(repeated_with_new_identity)

    assert applied.snapshot_version == 2
    assert noop.status is CommandExecutionStatus.NOOP
    assert noop.reason_code == "HOME_ACCESS_UNCHANGED"
    assert noop.snapshot_version == 2
    assert len(ledger.items) == 2
    assert ledger.items[1].payload["applied"] is False


@pytest.mark.parametrize(
    "command",
    [
        RecordConflict(
            **_common(
                actor=PrivateWorldActor.SYSTEM_CANDIDATE,
                source=PrivateWorldCommandSource.APPROVED_CANDIDATE,
            )
        ),
        SetHomeAccess(
            **_common(
                source=PrivateWorldCommandSource.APPROVED_CANDIDATE,
            ),
            home_access=HomeAccess.DOMESTIC_ACCESS,
        ),
        GrantNickname(
            **_common(source=PrivateWorldCommandSource.IMPORT),
            nickname="合成称呼",
        ),
        RecordRepair(
            **_common(
                actor=PrivateWorldActor.MIGRATION,
                source=PrivateWorldCommandSource.CONTROL_CENTER,
            )
        ),
    ],
)
def test_service_rejects_unapproved_or_mismatched_authority(
    command,
) -> None:
    with pytest.raises(PrivateWorldCommandServiceError):
        PrivateWorldCommandService(FakeLedger()).execute(command)


def test_approved_candidate_requires_relationship_command_and_evidence() -> None:
    service = PrivateWorldCommandService(FakeLedger())
    missing_evidence = RecordConflict(
        **_common(
            source=PrivateWorldCommandSource.APPROVED_CANDIDATE,
            evidence_refs=(),
        )
    )
    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_EVIDENCE_REQUIRED",
    ):
        service.execute(missing_evidence)

    approved = RecordConflict(
        **_common(
            source=PrivateWorldCommandSource.APPROVED_CANDIDATE,
        )
    )
    assert (
        service.execute(approved).status
        is CommandExecutionStatus.APPLIED
    )


def test_stage_confirmation_requires_existing_relationship_evidence() -> None:
    ledger = FakeLedger()
    service = PrivateWorldCommandService(ledger)
    conflict = RecordConflict(**_common(1))
    conflict_result = service.execute(conflict)
    stage = ConfirmRelationshipStage(
        **_common(2),
        target_stage=RelationshipStage.FAMILIAR,
        basis_event_ids=(conflict_result.event_id,),
    )

    confirmed = service.execute(stage)

    assert confirmed.status is CommandExecutionStatus.APPLIED
    assert ledger.current.relationship_stage == "familiar"

    missing = ConfirmRelationshipStage(
        **_common(3),
        target_stage=RelationshipStage.CLOSE,
        basis_event_ids=("event.missing",),
    )
    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_EVIDENCE_INVALID",
    ):
        service.execute(missing)

    canonical = LedgerEvent(
        event_id="delivery.synthetic",
        delivery_id="delivery.synthetic",
        event_type="canonical_reply_delivered",
        payload={},
        occurred_at=NOW.isoformat(),
    )
    ledger.items.append(canonical)
    invalid = ConfirmRelationshipStage(
        **_common(4),
        target_stage=RelationshipStage.CLOSE,
        basis_event_ids=(canonical.event_id,),
    )
    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_EVIDENCE_INVALID",
    ):
        service.execute(invalid)


def test_migration_actor_can_use_only_migration_or_import_sources() -> None:
    service = PrivateWorldCommandService(FakeLedger())
    imported = GrantNickname(
        **_common(
            actor=PrivateWorldActor.MIGRATION,
            source=PrivateWorldCommandSource.IMPORT,
        ),
        nickname="迁移称呼",
    )
    assert (
        service.execute(imported).status
        is CommandExecutionStatus.APPLIED
    )


@pytest.mark.parametrize("failure", ["read", "write"])
def test_storage_failure_is_wrapped_in_stable_error(
    failure: str,
) -> None:
    ledger = FakeLedger()
    if failure == "read":
        ledger.fail_reads = True
    else:
        ledger.fail_writes = True
    service = PrivateWorldCommandService(ledger)

    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE",
    ):
        service.execute(RecordConflict(**_common()))


def test_service_serializes_concurrent_commands() -> None:
    ledger = FakeLedger()
    service = PrivateWorldCommandService(ledger)
    commands = tuple(
        GrantNickname(
            **_common(index),
            nickname=f"称呼{index}",
        )
        for index in range(1, 11)
    )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = tuple(executor.map(service.execute, commands))

    assert all(
        result.status is CommandExecutionStatus.APPLIED
        for result in results
    )
    assert ledger.current.version == 11
    assert len(ledger.items) == 10
    assert len(ledger.current.nickname_permissions) == 10


def test_execution_result_is_bounded_and_serializable() -> None:
    service = PrivateWorldCommandService(FakeLedger())
    result = service.execute(RecordConflict(**_common()))

    assert result.to_dict() == {
        "status": "APPLIED",
        "command_id": "command.synthetic-1",
        "event_id": result.event_id,
        "reason_code": "CONFLICT",
        "snapshot_version": 2,
        "change_fields": ["tension"],
    }


def test_service_requires_typed_ledger_and_command() -> None:
    with pytest.raises(TypeError, match="command ledger"):
        PrivateWorldCommandService(object())  # type: ignore[arg-type]
    service = PrivateWorldCommandService(FakeLedger())
    with pytest.raises(TypeError, match="typed PrivateWorld command"):
        service.execute(object())  # type: ignore[arg-type]
