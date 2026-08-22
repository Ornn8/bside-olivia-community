from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from private_world_commands import (
    GrantNickname,
    PrivateWorldActor,
    PrivateWorldCommandSource,
    RecordConflict,
    SetHomeAccess,
)
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_port import HomeAccess
from private_world_service import (
    CommandExecutionStatus,
    PrivateWorldCommandService,
)


NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)


def _common(sequence: int = 1) -> dict[str, object]:
    return {
        "command_id": f"command.sqlite-{sequence}",
        "idempotency_key": f"idempotency.sqlite-{sequence}",
        "actor": PrivateWorldActor.LOCAL_USER,
        "source": PrivateWorldCommandSource.CONTROL_CENTER,
        "occurred_at": NOW,
        "reason": "synthetic SQLite integration",
        "evidence_refs": (f"letter:sqlite-{sequence}",),
    }


def test_sqlite_service_persists_and_deduplicates_across_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    command = RecordConflict(**_common(1))

    first_ledger = SQLitePrivateWorldLedger(database)
    first = PrivateWorldCommandService(first_ledger).execute(command)

    assert first.status is CommandExecutionStatus.APPLIED
    assert first_ledger.snapshot().tension == 3
    assert first_ledger.health()["event_count"] == 1
    assert first_ledger.health()["snapshot_count"] == 1

    reopened = SQLitePrivateWorldLedger(database)
    service = PrivateWorldCommandService(reopened)
    duplicate = service.execute(command)

    assert duplicate.status is CommandExecutionStatus.DUPLICATE
    assert duplicate.event_id == first.event_id
    assert reopened.health()["event_count"] == 1
    assert reopened.snapshot().version == 2

    nickname = GrantNickname(
        **_common(2),
        nickname="合成称呼",
    )
    applied = service.execute(nickname)
    assert applied.status is CommandExecutionStatus.APPLIED
    assert reopened.snapshot().nickname_permissions == ("合成称呼",)
    assert reopened.snapshot().version == 3
    assert reopened.health()["event_count"] == 2
    assert reopened.health()["snapshot_count"] == 2


def test_first_noop_command_is_audited_at_initial_snapshot_version(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(
        tmp_path / "private-world-noop.sqlite3"
    )
    service = PrivateWorldCommandService(ledger)
    noop = SetHomeAccess(
        **_common(1),
        home_access=HomeAccess.NO_ACCESS,
    )

    result = service.execute(noop)

    assert result.status is CommandExecutionStatus.NOOP
    assert result.snapshot_version == 1
    assert ledger.snapshot().version == 1
    assert ledger.health()["event_count"] == 1
    assert ledger.health()["snapshot_count"] == 1
