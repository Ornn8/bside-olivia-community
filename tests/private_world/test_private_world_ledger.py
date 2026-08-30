import json
from pathlib import Path
import sqlite3

import pytest

from private_world_ledger import (
    PRIVATE_WORLD_LEDGER_SCHEMA_VERSION,
    LedgerEvent,
    LedgerVersionConflictError,
    LedgerWriteError,
    SQLitePrivateWorldLedger,
)
from private_world_port import (
    HomeAccess,
    IntimacyGrant,
    PrivateWorldSnapshot,
)
from runtime.reply.reply_context import IntimacyTier


def _event(number: int, *, delivery_id: str | None = None) -> LedgerEvent:
    return LedgerEvent(
        event_id=f"event-{number}",
        delivery_id=delivery_id or f"delivery-{number}",
        event_type="reply_accepted",
        payload={"trust_delta": number},
        occurred_at="2026-08-22T00:00:00+00:00",
    )


def _legacy_v2_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE private_world_events (
                event_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE private_world_snapshots (
                version INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                FOREIGN KEY(event_id) REFERENCES private_world_events(event_id)
            );
            CREATE TABLE private_world_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO private_world_metadata (key, value)
            VALUES ('schema_version', '2');
            """
        )
        for version in (1, 2):
            event = _event(version)
            connection.execute(
                """INSERT INTO private_world_events
                   (event_id, delivery_id, event_type, payload_json, occurred_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.delivery_id,
                    event.event_type,
                    event._payload_json(),
                    event.occurred_at,
                ),
            )
            payload = PrivateWorldSnapshot(
                version=version,
                trust=version,
            ).to_dict()
            payload.pop("intimacy_grants")
            payload.pop("growth_window_start")
            payload.pop("growth_used")
            connection.execute(
                """INSERT INTO private_world_snapshots
                   (version, payload_json, event_id) VALUES (?, ?, ?)""",
                (
                    version,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    event.event_id,
                ),
            )


def _legacy_v1_database(path: Path) -> None:
    _legacy_v2_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE private_world_metadata")
        rows = connection.execute(
            "SELECT version, payload_json FROM private_world_snapshots"
        ).fetchall()
        for version, payload_json in rows:
            payload = json.loads(payload_json)
            payload.pop("continuation_facts")
            connection.execute(
                """UPDATE private_world_snapshots
                   SET payload_json = ? WHERE version = ?""",
                (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    version,
                ),
            )


def test_v2_ledger_migrates_to_v3_without_losing_events_or_snapshots(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    _legacy_v2_database(database)

    ledger = SQLitePrivateWorldLedger(database)

    assert ledger.schema_version == PRIVATE_WORLD_LEDGER_SCHEMA_VERSION == 3
    assert ledger.migration_status == "migrated_v2_to_v3"
    assert ledger.events() == (_event(1), _event(2))
    assert ledger.snapshot() == PrivateWorldSnapshot(version=2, trust=2)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """SELECT version, payload_json FROM private_world_snapshots
               ORDER BY version"""
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2]
        assert all(
            json.loads(row[1])["intimacy_grants"] == []
            and json.loads(row[1])["growth_window_start"] == ""
            and json.loads(row[1])["growth_used"] == 0
            for row in rows
        )
    backups = tuple(tmp_path.glob("private-world.sqlite3.pre-v3-*.bak"))
    assert len(backups) == 1
    assert backups[0].is_file() and backups[0].stat().st_size > 0


def test_v1_ledger_uses_the_validated_v2_to_v3_migration_chain(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    _legacy_v1_database(database)

    ledger = SQLitePrivateWorldLedger(database)

    assert ledger.schema_version == 3
    assert ledger.migration_status == "migrated_v2_to_v3"
    assert ledger.events() == (_event(1), _event(2))
    assert ledger.snapshot() == PrivateWorldSnapshot(version=2, trust=2)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM private_world_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("3",)
        payloads = connection.execute(
            "SELECT payload_json FROM private_world_snapshots ORDER BY version"
        ).fetchall()
    assert all(set(json.loads(row[0])) >= {
        "continuation_facts",
        "intimacy_grants",
        "growth_window_start",
        "growth_used",
    } for row in payloads)
    assert len(tuple(tmp_path.glob("private-world.sqlite3.pre-v3-*.bak"))) == 1


def test_v3_ledger_reopens_without_another_migration_or_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-world.sqlite3"
    created = SQLitePrivateWorldLedger(database)

    assert created.migration_status == "created_v3"
    assert not tuple(tmp_path.glob("*.pre-v3-*.bak"))

    reopened = SQLitePrivateWorldLedger(database)

    assert reopened.migration_status == "current_v3"
    assert not tuple(tmp_path.glob("*.pre-v3-*.bak"))


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


def test_ledger_round_trips_intimacy_grants_and_growth_window_state(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    snapshot = PrivateWorldSnapshot(
        version=1,
        intimacy_grants=(
            IntimacyGrant(
                "grant.synthetic",
                IntimacyTier.LIGHT_CONTACT,
                "A synthetic granted interaction.",
            ),
        ),
        growth_window_start="2026-08-29T00:00:00+00:00",
        growth_used=2,
    )

    assert ledger.apply_once(_event(1), snapshot) is True
    assert ledger.snapshot() == snapshot
    assert ledger.control_view().intimacy_grants == snapshot.intimacy_grants
    assert ledger.character_view().granted_intimacy is IntimacyTier.LIGHT_CONTACT


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

    with pytest.raises(LedgerWriteError) as raised:
        ledger.apply_once(_event(2), PrivateWorldSnapshot(version=3))

    assert not isinstance(raised.value, LedgerVersionConflictError)
    assert ledger.events() == (_event(1),)
    assert ledger.health()["event_count"] == 1


def test_stale_divergent_snapshot_raises_typed_version_conflict(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    ledger.apply_once(
        _event(1),
        PrivateWorldSnapshot(version=2, trust=1),
    )

    with pytest.raises(LedgerVersionConflictError) as raised:
        ledger.apply_once(
            _event(2),
            PrivateWorldSnapshot(version=2, trust=2),
        )

    assert raised.value.code == "PRIVATE_WORLD_VERSION_CONFLICT"
    assert ledger.events() == (_event(1),)
    assert ledger.snapshot() == PrivateWorldSnapshot(version=2, trust=1)


def test_older_snapshot_raises_typed_version_conflict(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private-world.sqlite3")
    ledger.apply_once(_event(1), PrivateWorldSnapshot(version=2))
    ledger.apply_once(_event(2), PrivateWorldSnapshot(version=3))

    with pytest.raises(LedgerVersionConflictError):
        ledger.apply_once(_event(3), PrivateWorldSnapshot(version=2))

    assert ledger.events() == (_event(1), _event(2))
    assert ledger.snapshot() == PrivateWorldSnapshot(version=3)


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
