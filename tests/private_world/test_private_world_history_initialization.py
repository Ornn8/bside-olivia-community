from __future__ import annotations

from datetime import datetime, timezone

from private_world_commands import (
    InitializeHistoricalRelationship,
    PrivateWorldActor,
    PrivateWorldCommandSource,
)
from private_world_port import PrivateWorldSnapshot
from private_world_reducer import reduce_private_world_command
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import CommandExecutionStatus, PrivateWorldCommandService
from runtime.reply.reply_context import RelationshipStage


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


def test_migration_actor_can_atomically_initialize_once(tmp_path) -> None:
    service = PrivateWorldCommandService(
        SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    )

    first = service.execute(_command())
    duplicate = service.execute(_command())

    assert first.status is CommandExecutionStatus.APPLIED
    assert duplicate.status is CommandExecutionStatus.DUPLICATE
