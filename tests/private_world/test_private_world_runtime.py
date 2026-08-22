from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from private_world_ledger import (
    PRIVATE_WORLD_LEDGER_SCHEMA_VERSION,
    LedgerEvent,
    LedgerWriteError,
    SQLitePrivateWorldLedger,
)
from private_world_port import NullPrivateWorldPort, PrivateWorldSnapshot
from private_world_runtime import (
    PRIVATE_WORLD_DEFAULT_RELATIVE_PATH,
    create_private_world_runtime,
    resolve_private_world_database,
)


def _legacy_v1_database(path: Path) -> None:
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
            """
        )
        connection.execute(
            """INSERT INTO private_world_events
               (event_id, delivery_id, event_type, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "legacy-event",
                "legacy-delivery",
                "canonical_reply_delivered",
                json.dumps({"applied": False}),
                "2026-08-22T00:00:00+00:00",
            ),
        )
        snapshot = PrivateWorldSnapshot(version=1).to_dict()
        connection.execute(
            """INSERT INTO private_world_snapshots
               (version, payload_json, event_id) VALUES (?, ?, ?)""",
            (1, json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")), "legacy-event"),
        )


def test_legacy_ledger_is_backed_up_and_migrated_once(tmp_path: Path) -> None:
    database = tmp_path / "private_world.sqlite3"
    _legacy_v1_database(database)

    ledger = SQLitePrivateWorldLedger(database)

    assert ledger.schema_version == PRIVATE_WORLD_LEDGER_SCHEMA_VERSION == 2
    assert ledger.migration_status == "migrated_v1_to_v2"
    assert ledger.snapshot() == PrivateWorldSnapshot(version=1)
    backups = tuple(tmp_path.glob("private_world.sqlite3.pre-v2-*.bak"))
    assert len(backups) == 1
    assert backups[0].is_file() and backups[0].stat().st_size > 0
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM private_world_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("2",)

    reopened = SQLitePrivateWorldLedger(database)
    assert reopened.migration_status == "current_v2"
    assert tuple(tmp_path.glob("private_world.sqlite3.pre-v2-*.bak")) == backups


def test_new_ledger_records_v2_metadata_without_backup(tmp_path: Path) -> None:
    database = tmp_path / "private_world.sqlite3"
    ledger = SQLitePrivateWorldLedger(database)

    assert ledger.migration_status == "created_v2"
    assert not tuple(tmp_path.glob("*.pre-v2-*.bak"))
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM private_world_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("2",)


def test_newer_schema_is_rejected_without_modification(tmp_path: Path) -> None:
    database = tmp_path / "private_world.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE private_world_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO private_world_metadata (key, value) VALUES ('schema_version', '99')"
        )

    with pytest.raises(LedgerWriteError, match="newer than this runtime"):
        SQLitePrivateWorldLedger(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM private_world_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("99",)


def test_default_runtime_uses_local_data_root_and_reports_sanitized_health(
    tmp_path: Path,
) -> None:
    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )
    database = tmp_path / PRIVATE_WORLD_DEFAULT_RELATIVE_PATH

    assert database.is_file()
    assert isinstance(runtime.port, SQLitePrivateWorldLedger)
    assert runtime.committer is not None
    assert runtime.public_status() == {
        "status": "available",
        "provider": "sqlite",
        "reason_code": None,
        "enabled": True,
        "schema_version": 2,
        "migration_status": "created_v2",
        "event_count": 0,
        "snapshot_count": 0,
        "probe": "in-process",
        "network_called": False,
    }

    runtime.port.apply_once(
        LedgerEvent(
            event_id="event-1",
            delivery_id="delivery-1",
            event_type="canonical_reply_delivered",
            payload={"applied": False},
            occurred_at="2026-08-22T00:00:00+00:00",
        ),
        PrivateWorldSnapshot(version=1, trust=88, nickname_permissions=("secret_name",)),
    )
    status = runtime.public_status()
    serialized = json.dumps(status, ensure_ascii=False)
    assert status["event_count"] == 1
    assert status["snapshot_count"] == 1
    assert str(tmp_path) not in serialized
    assert "secret_name" not in serialized
    assert "88" not in serialized


def test_explicit_absolute_database_overrides_default(tmp_path: Path) -> None:
    explicit = tmp_path / "custom" / "world.sqlite3"
    path, reason, enabled = resolve_private_world_database(
        {
            "OLIVIA_LOCAL_DATA_ROOT": str(tmp_path / "data"),
            "OLIVIA_PRIVATE_WORLD_DB": str(explicit),
        }
    )
    assert (path, reason, enabled) == (explicit.resolve(), None, True)

    runtime = create_private_world_runtime(
        {"OLIVIA_PRIVATE_WORLD_DB": str(explicit)}
    )
    assert runtime.status == "available"
    assert explicit.is_file()


@pytest.mark.parametrize(
    ("environ", "status", "reason", "enabled"),
    [
        (
            {"OLIVIA_PRIVATE_WORLD_ENABLED": "0"},
            "disabled",
            "PRIVATE_WORLD_DISABLED",
            False,
        ),
        (
            {},
            "unavailable",
            "PRIVATE_WORLD_DATA_ROOT_NOT_CONFIGURED",
            True,
        ),
        (
            {"OLIVIA_PRIVATE_WORLD_ENABLED": "maybe"},
            "disabled",
            "PRIVATE_WORLD_ENABLED_INVALID",
            False,
        ),
        (
            {"OLIVIA_PRIVATE_WORLD_DB": "relative.sqlite3"},
            "unavailable",
            "PRIVATE_WORLD_DB_MUST_BE_ABSOLUTE",
            True,
        ),
    ],
)
def test_disabled_and_invalid_runtime_states_are_explicit(
    environ: dict[str, str],
    status: str,
    reason: str,
    enabled: bool,
) -> None:
    runtime = create_private_world_runtime(environ)
    assert isinstance(runtime.port, NullPrivateWorldPort)
    assert runtime.committer is None
    public = runtime.public_status()
    assert public["status"] == status
    assert public["reason_code"] == reason
    assert public["enabled"] is enabled
    assert public["network_called"] is False
