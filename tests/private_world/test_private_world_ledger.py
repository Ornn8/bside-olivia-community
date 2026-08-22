from pathlib import Path

import pytest

from private_world_ledger import (
    LedgerEvent,
    LedgerWriteError,
    SQLitePrivateWorldLedger,
)
from private_world_port import HomeAccess, PrivateWorldSnapshot


def _event(number: int, *, delivery_id: str | None = None) -> LedgerEvent:
    return LedgerEvent(
        event_id=f"event-{number}",
        delivery_id=delivery_id or f"delivery-{number}",
        event_type="reply_accepted",
        payload={"trust_delta": number},
        occurred_at="2026-08-22T00:00:00+00:00",
    )


def test_apply_once_atomically_appends_event_and_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "private-world" / "ledger.sqlite3"
    ledger = SQLitePrivateWorldLedger(database)
    snapshot = PrivateWorldSnapshot(
        version=1,
        trust=4,
        relationship_stage="acquaintance",
        home_access=HomeAccess.VISIT_ACCESS,
    )

    assert ledger.apply_once(_event(4), snapshot) is True
    assert ledger.snapshot() == snapshot
    assert ledger.events() == (_event(4),)
    assert database.is_file()


def test_apply_once_deduplicates_event_and_delivery_ids(tmp_path: Path) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")

    assert ledger.apply_once(_event(1), PrivateWorldSnapshot(version=1)) is True
    assert ledger.apply_once(_event(1), PrivateWorldSnapshot(version=2)) is False
    assert (
        ledger.apply_once(
            _event(2, delivery_id="delivery-1"), PrivateWorldSnapshot(version=2)
        )
        is False
    )
    assert ledger.health() == {
        "status": "READY",
        "event_count": 1,
        "snapshot_count": 1,
    }


def test_event_insert_rolls_back_when_snapshot_version_skips_ahead(tmp_path: Path) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    ledger.apply_once(_event(1), PrivateWorldSnapshot(version=1))

    with pytest.raises(LedgerWriteError):
        ledger.apply_once(_event(2), PrivateWorldSnapshot(version=3))

    assert ledger.events() == (_event(1),)
    assert ledger.health()["event_count"] == 1


def test_no_effect_event_can_share_latest_snapshot_version(tmp_path: Path) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    ledger.apply_once(_event(1), PrivateWorldSnapshot(version=1))

    assert ledger.apply_once(_event(2), PrivateWorldSnapshot(version=1)) is True
    assert ledger.health() == {
        "status": "READY",
        "event_count": 2,
        "snapshot_count": 1,
    }


def test_health_never_exposes_database_path_or_private_values(tmp_path: Path) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "secret-name.sqlite3")
    ledger.apply_once(
        _event(1),
        PrivateWorldSnapshot(
            version=1,
            trust=88,
            nickname_permissions=("private_nickname",),
        ),
    )

    health = ledger.health()
    serialized = repr(health)
    assert set(health) == {"status", "event_count", "snapshot_count"}
    assert "secret-name" not in serialized
    assert "private_nickname" not in serialized
    assert "88" not in serialized


def test_ledger_requires_explicit_local_database_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SQLitePrivateWorldLedger(Path())
    with pytest.raises(ValueError):
        SQLitePrivateWorldLedger(tmp_path)
