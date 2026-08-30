from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import pytest

from private_world_commands import (
    ApplyHistoricalRelationshipEvidence,
    ConfirmRelationshipStage,
    GrantIntimacy,
    GrantNickname,
    InitializeHistoricalRelationship,
    PrivateWorldActor,
    PrivateWorldCommandSource,
    RecordBoundaryRespected,
    RecordConflict,
    SetHomeAccess,
    UpsertContinuationFact,
)
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    PrivateWorldSnapshot,
)
from private_world_reducer import reduce_private_world_command
from private_world_ledger import (
    LedgerEvent,
    LedgerVersionConflictError,
    LedgerWriteError,
    SQLitePrivateWorldLedger,
)
from private_world_service import (
    CommandExecutionStatus,
    PrivateWorldCommandService,
    PrivateWorldCommandServiceError,
)
from runtime.reply.reply_context import IntimacyTier, RelationshipStage


class _InitialSnapshotBarrierLedger(SQLitePrivateWorldLedger):
    def __init__(self, database: Path, barrier: Barrier) -> None:
        super().__init__(database)
        self._initial_snapshot_barrier = barrier
        self.snapshot_calls = 0

    def snapshot(self) -> PrivateWorldSnapshot:
        snapshot = super().snapshot()
        self.snapshot_calls += 1
        if self.snapshot_calls == 1:
            self._initial_snapshot_barrier.wait(timeout=5)
        return snapshot


class _GenericWriteFailureLedger(SQLitePrivateWorldLedger):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.apply_calls = 0

    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
    ) -> bool:
        self.apply_calls += 1
        raise LedgerWriteError("synthetic generic storage failure")


class _VersionConflictFailureLedger(_GenericWriteFailureLedger):
    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
    ) -> bool:
        self.apply_calls += 1
        raise LedgerVersionConflictError("synthetic stale snapshot")


def _command() -> InitializeHistoricalRelationship:
    return InitializeHistoricalRelationship(
        command_id="history.init.fixture",
        idempotency_key="history.init.fixture",
        actor=PrivateWorldActor.MIGRATION,
        source=PrivateWorldCommandSource.IMPORT,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reason="one-time ordered historical import",
        evidence_refs=("history.letter.1", "history.letter.2"),
        relationship_stage=RelationshipStage.FAMILIAR,
        familiarity=48,
        trust=44,
        comfort=42,
        closeness=36,
        tension=9,
    )


def _default_command() -> InitializeHistoricalRelationship:
    return InitializeHistoricalRelationship(
        command_id="history.init.default",
        idempotency_key="history.init.default",
        actor=PrivateWorldActor.MIGRATION,
        source=PrivateWorldCommandSource.IMPORT,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reason="one-time ordered imported exchanges",
        evidence_refs=("history.letter.1",),
        relationship_stage=RelationshipStage.UNKNOWN,
        familiarity=0,
        trust=0,
        comfort=0,
        closeness=0,
        tension=0,
    )


def _incremental_command(
    sequence: int = 1,
    *,
    familiarity: int = 48,
    closeness: int = 36,
) -> ApplyHistoricalRelationshipEvidence:
    corpus_id = f"history.increment.fixture-{sequence}"
    return ApplyHistoricalRelationshipEvidence(
        command_id=corpus_id,
        idempotency_key=corpus_id,
        actor=PrivateWorldActor.MIGRATION,
        source=PrivateWorldCommandSource.IMPORT,
        occurred_at=datetime(2026, 2, sequence, tzinfo=timezone.utc),
        reason="ordered historical intimacy evidence",
        evidence_refs=(f"history.letter.{sequence}",),
        familiarity=familiarity,
        closeness=closeness,
    )


def _control_common(sequence: int) -> dict[str, object]:
    return {
        "command_id": f"control.history-fixture-{sequence}",
        "idempotency_key": f"control.history-fixture-{sequence}",
        "actor": PrivateWorldActor.LOCAL_USER,
        "source": PrivateWorldCommandSource.CONTROL_CENTER,
        "occurred_at": datetime(2026, 3, sequence, tzinfo=timezone.utc),
        "reason": "synthetic existing relationship setup",
        "evidence_refs": (f"event.history-fixture-{sequence}",),
    }


def test_assessed_corpus_initializes_only_intimacy_axes_through_sqlite_service(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    before = ledger.snapshot()

    result = PrivateWorldCommandService(ledger).execute(
        _incremental_command()
    )

    assert result.status is CommandExecutionStatus.APPLIED
    assert result.change_fields == ("familiarity", "closeness")
    assert ledger.snapshot() == replace(
        before,
        version=before.version + 1,
        familiarity=48,
        closeness=36,
    )
    assert ledger.events()[0].payload["change_fields"] == [
        "familiarity",
        "closeness",
    ]


def test_new_corpus_max_merges_only_existing_intimacy_axes(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    service = PrivateWorldCommandService(ledger)
    service.execute(RecordBoundaryRespected(**_control_common(1)))
    conflict = service.execute(RecordConflict(**_control_common(2)))
    service.execute(
        ConfirmRelationshipStage(
            **_control_common(3),
            target_stage=RelationshipStage.CLOSE,
            basis_event_ids=(conflict.event_id,),
        )
    )
    service.execute(
        GrantNickname(**_control_common(4), nickname="合成称呼")
    )
    service.execute(
        SetHomeAccess(
            **_control_common(5),
            home_access=HomeAccess.ERRAND_ACCESS,
        )
    )
    service.execute(
        UpsertContinuationFact(
            **_control_common(6),
            fact_id="continuation.history-fixture",
            statement="一条仅用于测试的私有事实。",
            awareness=ContinuationAwareness.CONTROL_ONLY,
        )
    )
    service.execute(
        GrantIntimacy(
            **_control_common(7),
            grant_id="intimacy.history-fixture",
            tier=IntimacyTier.LIGHT_CONTACT,
            statement="A synthetic granted interaction.",
        )
    )
    service.execute(
        _incremental_command(1, familiarity=60, closeness=10)
    )
    before = ledger.snapshot()

    result = service.execute(
        _incremental_command(2, familiarity=48, closeness=36)
    )

    assert result.status is CommandExecutionStatus.APPLIED
    assert result.change_fields == ("closeness",)
    assert ledger.snapshot() == replace(
        before,
        version=before.version + 1,
        closeness=36,
    )
    assert ledger.events()[-1].payload["change_fields"] == ["closeness"]


def test_lower_assessment_is_audited_noop_without_version_change(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    service = PrivateWorldCommandService(ledger)
    service.execute(
        _incremental_command(1, familiarity=60, closeness=36)
    )
    before = ledger.snapshot()

    result = service.execute(
        _incremental_command(2, familiarity=48, closeness=10)
    )

    assert result.status is CommandExecutionStatus.NOOP
    assert result.reason_code == "HISTORICAL_RELATIONSHIP_EVIDENCE_NO_CHANGE"
    assert result.snapshot_version == before.version
    assert result.change_fields == ()
    assert ledger.snapshot() == before
    assert ledger.health() == {
        "status": "READY",
        "event_count": 2,
        "snapshot_count": 1,
    }
    assert ledger.events()[-1].payload["applied"] is False
    assert ledger.events()[-1].payload["change_fields"] == []


def test_command_lookup_is_read_only_and_returns_no_audit_body(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    service = PrivateWorldCommandService(ledger)
    command = _incremental_command()
    pristine = (
        ledger.health(),
        ledger.snapshot(),
        ledger.events(),
    )

    assert service.lookup_command(command.command_id) is None
    assert (ledger.health(), ledger.snapshot(), ledger.events()) == pristine
    applied = service.execute(command)
    before_health = ledger.health()
    before_snapshot = ledger.snapshot()
    before_events = ledger.events()

    found = service.lookup_command(command.command_id)

    assert found == applied
    assert found is not None
    assert set(found.to_dict()) == {
        "status",
        "command_id",
        "event_id",
        "reason_code",
        "snapshot_version",
        "change_fields",
    }
    assert service.lookup_command("history.increment.missing") is None
    assert ledger.health() == before_health
    assert ledger.snapshot() == before_snapshot
    assert ledger.events() == before_events


@pytest.mark.parametrize(
    ("actor", "source"),
    [
        (
            PrivateWorldActor.LOCAL_USER,
            PrivateWorldCommandSource.CONTROL_CENTER,
        ),
        (
            PrivateWorldActor.LOCAL_USER,
            PrivateWorldCommandSource.IMPORT,
        ),
        (
            PrivateWorldActor.MIGRATION,
            PrivateWorldCommandSource.MIGRATION,
        ),
    ],
)
def test_historical_evidence_requires_import_migration_authority(
    tmp_path: Path,
    actor: PrivateWorldActor,
    source: PrivateWorldCommandSource,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    command = replace(_incremental_command(), actor=actor, source=source)

    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN",
    ):
        PrivateWorldCommandService(ledger).execute(command)

    assert ledger.health()["event_count"] == 0
    assert ledger.snapshot() == PrivateWorldSnapshot()


def test_identical_corpus_replay_is_duplicate_without_audit_or_version_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    command = _incremental_command()
    first_ledger = SQLitePrivateWorldLedger(database)
    first = PrivateWorldCommandService(first_ledger).execute(command)
    before_snapshot = first_ledger.snapshot()
    before_events = first_ledger.events()
    before_health = first_ledger.health()

    reopened = SQLitePrivateWorldLedger(database)
    duplicate = PrivateWorldCommandService(reopened).execute(command)

    assert duplicate.status is CommandExecutionStatus.DUPLICATE
    assert duplicate.event_id == first.event_id
    assert duplicate.snapshot_version == first.snapshot_version
    assert reopened.snapshot() == before_snapshot
    assert reopened.events() == before_events
    assert reopened.health() == before_health


def test_concurrent_corpora_choose_current_state_inside_command_service(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    service = PrivateWorldCommandService(ledger)
    commands = (
        _incremental_command(1, familiarity=60, closeness=10),
        _incremental_command(2, familiarity=48, closeness=36),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(service.execute, commands))

    assert all(
        result.status is CommandExecutionStatus.APPLIED
        for result in results
    )
    assert ledger.snapshot() == replace(
        PrivateWorldSnapshot(),
        version=3,
        familiarity=60,
        closeness=36,
    )
    assert ledger.health() == {
        "status": "READY",
        "event_count": 2,
        "snapshot_count": 2,
    }


def test_independent_services_retry_stale_historical_join(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    barrier = Barrier(2)
    ledgers = (
        _InitialSnapshotBarrierLedger(database, barrier),
        _InitialSnapshotBarrierLedger(database, barrier),
    )
    services = tuple(
        PrivateWorldCommandService(ledger) for ledger in ledgers
    )
    commands = (
        _incremental_command(1, familiarity=60, closeness=10),
        _incremental_command(2, familiarity=48, closeness=36),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(service.execute, command)
            for service, command in zip(services, commands, strict=True)
        )
        results = tuple(future.result(timeout=10) for future in futures)

    assert all(
        result.status is CommandExecutionStatus.APPLIED
        for result in results
    )
    assert {result.snapshot_version for result in results} == {2, 3}
    assert sorted(ledger.snapshot_calls for ledger in ledgers) == [1, 2]
    stored = SQLitePrivateWorldLedger(database)
    assert stored.snapshot() == replace(
        PrivateWorldSnapshot(),
        version=3,
        familiarity=60,
        closeness=36,
    )
    audits = {
        event.payload["snapshot_version"]: event.payload
        for event in stored.events()
    }
    assert audits[2]["change_fields"] == ["familiarity", "closeness"]
    assert audits[3]["change_fields"] in (["familiarity"], ["closeness"])
    assert {
        event.payload["command_id"] for event in stored.events()
    } == {command.command_id for command in commands}
    assert stored.health() == {
        "status": "READY",
        "event_count": 2,
        "snapshot_count": 2,
    }


def test_independent_services_resolve_concurrent_identical_corpus_duplicate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    barrier = Barrier(2)
    services = tuple(
        PrivateWorldCommandService(
            _InitialSnapshotBarrierLedger(database, barrier)
        )
        for _ in range(2)
    )
    command = _incremental_command()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(service.execute, command)
            for service in services
        )
        results = tuple(future.result(timeout=10) for future in futures)

    assert {result.status for result in results} == {
        CommandExecutionStatus.APPLIED,
        CommandExecutionStatus.DUPLICATE,
    }
    stored = SQLitePrivateWorldLedger(database)
    assert len(stored.events()) == 1
    assert stored.snapshot().version == 2


def test_historical_command_does_not_retry_generic_storage_failure(
    tmp_path: Path,
) -> None:
    ledger = _GenericWriteFailureLedger(
        tmp_path / "private-world.sqlite3"
    )

    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE",
    ):
        PrivateWorldCommandService(ledger).execute(_incremental_command())

    assert ledger.apply_calls == 1
    assert ledger.health()["event_count"] == 0


def test_nonhistorical_command_does_not_retry_version_conflict(
    tmp_path: Path,
) -> None:
    ledger = _VersionConflictFailureLedger(
        tmp_path / "private-world.sqlite3"
    )
    command = GrantNickname(
        **_control_common(1),
        nickname="合成称呼",
    )

    with pytest.raises(
        PrivateWorldCommandServiceError,
        match="PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE",
    ):
        PrivateWorldCommandService(ledger).execute(command)

    assert ledger.apply_calls == 1
    assert ledger.health()["event_count"] == 0


def test_historical_relationship_initializes_one_pristine_snapshot() -> None:
    result = reduce_private_world_command(PrivateWorldSnapshot(), _command())

    assert result.delta.applied is True
    assert result.delta.reason_code == "INITIALIZE_HISTORICAL_RELATIONSHIP"
    assert result.snapshot.relationship_stage == "familiar"
    assert result.snapshot.familiarity == 48
    assert result.snapshot.trust == 44
    assert result.snapshot.comfort == 42
    assert result.snapshot.closeness == 36
    assert result.snapshot.tension == 9


def test_historical_relationship_never_overwrites_an_existing_private_world() -> None:
    existing = PrivateWorldSnapshot(version=2, trust=1)

    result = reduce_private_world_command(existing, _command())

    assert result.snapshot == existing
    assert result.delta.applied is False
    assert result.delta.reason_code == "HISTORY_ALREADY_INITIALIZED"


def test_default_historical_relationship_still_records_one_initialization() -> None:
    first = reduce_private_world_command(PrivateWorldSnapshot(), _default_command())
    second = reduce_private_world_command(first.snapshot, _default_command())

    assert first.delta.applied is True
    assert first.delta.changes == ()
    assert first.snapshot == PrivateWorldSnapshot(version=2)
    assert second.delta.applied is False
    assert second.delta.reason_code == "HISTORY_ALREADY_INITIALIZED"


def test_migration_actor_can_atomically_initialize_once(tmp_path) -> None:
    service = PrivateWorldCommandService(
        SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    )

    first = service.execute(_command())
    duplicate = service.execute(_command())

    assert first.status is CommandExecutionStatus.APPLIED
    assert duplicate.status is CommandExecutionStatus.DUPLICATE
