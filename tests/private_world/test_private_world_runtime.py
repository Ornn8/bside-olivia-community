from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest
from jsonschema import Draft202012Validator

import private_world_runtime
from private_world_delivery import (
    DeliveryEvent,
    DeliveryStatus,
)
from private_world_ledger import (
    PRIVATE_WORLD_LEDGER_SCHEMA_VERSION,
    LedgerEvent,
    LedgerWriteError,
    SQLitePrivateWorldLedger,
)


ROOT = Path(__file__).resolve().parents[2]
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    LocalContinuationFact,
    NullPrivateWorldPort,
    PrivateWorldSnapshot,
)
from private_world_runtime import (
    PRIVATE_WORLD_DEFAULT_RELATIVE_PATH,
    create_private_world_runtime,
    resolve_private_world_database,
)
from private_world_reducer import ReducerEventKind


def _legacy_v1_database(
    path: Path,
    *,
    includes_continuation_facts: bool = False,
) -> None:
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
        # This is the historical v1 wire payload.  It intentionally does not
        # use the current snapshot serializer: v1 had no continuation facts.
        snapshot = {
            "version": 1,
            "view": "snapshot",
            "familiarity": 0,
            "trust": 3,
            "comfort": 0,
            "closeness": 0,
            "tension": 0,
            "relationship_stage": "acquaintance",
            "nickname_permissions": [],
            "home_access": "no_access",
            "continuation_awareness": "control_only",
        }
        if includes_continuation_facts:
            snapshot["continuation_facts"] = []
        connection.execute(
            """INSERT INTO private_world_snapshots
               (version, payload_json, event_id) VALUES (?, ?, ?)""",
            (
                1,
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "legacy-event",
            ),
        )


def test_legacy_ledger_is_backed_up_and_migrated_once(tmp_path: Path) -> None:
    database = tmp_path / "private_world.sqlite3"
    _legacy_v1_database(database)

    ledger = SQLitePrivateWorldLedger(database)

    assert ledger.schema_version == PRIVATE_WORLD_LEDGER_SCHEMA_VERSION == 2
    assert ledger.migration_status == "migrated_v1_to_v2"
    assert ledger.snapshot() == PrivateWorldSnapshot(
        version=1,
        trust=3,
        relationship_stage="acquaintance",
    )
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


def test_later_metadata_less_v1_payload_with_continuation_facts_migrates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private_world.sqlite3"
    _legacy_v1_database(database, includes_continuation_facts=True)

    ledger = SQLitePrivateWorldLedger(database)

    assert ledger.migration_status == "migrated_v1_to_v2"
    assert ledger.snapshot() == PrivateWorldSnapshot(
        version=1,
        trust=3,
        relationship_stage="acquaintance",
    )
    with sqlite3.connect(database) as connection:
        stored_payload = connection.execute(
            "SELECT payload_json FROM private_world_snapshots WHERE version = 1"
        ).fetchone()[0]
    assert json.loads(stored_payload)["continuation_facts"] == []


def test_v1_migration_locks_the_validated_source_epoch_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "private_world.sqlite3"
    _legacy_v1_database(database)
    original_backup = SQLitePrivateWorldLedger._backup_legacy_database
    competing_write: dict[str, str] = {}

    def attempt_legacy_write_during_backup(
        ledger: SQLitePrivateWorldLedger,
        *args: object,
    ) -> None:
        try:
            with sqlite3.connect(database, timeout=0) as writer:
                row = writer.execute(
                    "SELECT payload_json FROM private_world_snapshots WHERE version = 1"
                ).fetchone()
                payload = json.loads(row[0])
                payload["trust"] = 99
                writer.execute(
                    "UPDATE private_world_snapshots SET payload_json = ? WHERE version = 1",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
                )
            competing_write["result"] = "wrote"
        except sqlite3.OperationalError:
            competing_write["result"] = "locked"
        original_backup(ledger, *args)

    monkeypatch.setattr(
        SQLitePrivateWorldLedger,
        "_backup_legacy_database",
        attempt_legacy_write_during_backup,
    )

    ledger = SQLitePrivateWorldLedger(database)

    assert competing_write == {"result": "locked"}
    assert ledger.snapshot().trust == 3
    backup = next(tmp_path.glob("private_world.sqlite3.pre-v2-*.bak"))
    with sqlite3.connect(backup) as connection:
        backup_payload = connection.execute(
            "SELECT payload_json FROM private_world_snapshots WHERE version = 1"
        ).fetchone()[0]
    assert json.loads(backup_payload)["trust"] == 3


def test_invalid_v1_payload_does_not_modify_database_or_create_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private_world.sqlite3"
    _legacy_v1_database(database)
    with sqlite3.connect(database) as connection:
        payload_json = connection.execute(
            "SELECT payload_json FROM private_world_snapshots WHERE version = 1"
        ).fetchone()[0]
        payload = json.loads(payload_json)
        payload["not_a_v1_field"] = True
        connection.execute(
            "UPDATE private_world_snapshots SET payload_json = ? WHERE version = 1",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
        )
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    with pytest.raises(LedgerWriteError, match="stored v1 state is invalid"):
        SQLitePrivateWorldLedger(database)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash
    assert not tuple(tmp_path.glob("private_world.sqlite3.pre-v2-*.bak"))
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'private_world_metadata'"
        ).fetchone() is None


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


def test_public_private_world_health_matches_the_sanitized_schema(
    tmp_path: Path,
) -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world_runtime_health.schema.json").read_text(
            encoding="utf-8"
        )
    )
    status = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    ).public_status()

    assert list(Draft202012Validator(schema).iter_errors(status)) == []
    serialized = json.dumps(status, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "trust" not in serialized
    assert "reply" not in serialized


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "available",
            "provider": "none",
            "reason_code": None,
            "enabled": True,
            "schema_version": 2,
            "migration_status": "current_v2",
            "event_count": 0,
            "snapshot_count": 0,
            "probe": "in-process",
            "network_called": False,
        },
        {
            "status": "unavailable",
            "provider": "sqlite",
            "reason_code": "PRIVATE_WORLD_STORAGE_UNAVAILABLE",
            "enabled": True,
            "schema_version": None,
            "migration_status": None,
            "event_count": 0,
            "snapshot_count": 0,
            "probe": "not-run",
            "network_called": False,
        },
        {
            "status": "disabled",
            "provider": "none",
            "reason_code": "PRIVATE_WORLD_DISABLED",
            "enabled": True,
            "schema_version": None,
            "migration_status": None,
            "event_count": 0,
            "snapshot_count": 0,
            "probe": "not-run",
            "network_called": False,
        },
        {
            "status": "available",
            "provider": "sqlite",
            "reason_code": "PRIVATE_WORLD_STORAGE_UNAVAILABLE",
            "enabled": True,
            "schema_version": 2,
            "migration_status": "current_v2",
            "event_count": 0,
            "snapshot_count": 0,
            "probe": "in-process",
            "network_called": False,
        },
        {
            "status": "unavailable",
            "provider": "none",
            "reason_code": None,
            "enabled": True,
            "schema_version": None,
            "migration_status": None,
            "event_count": 0,
            "snapshot_count": 0,
            "probe": "not-run",
            "network_called": False,
        },
    ],
)
def test_private_world_health_schema_rejects_contradictory_combinations(
    payload: dict[str, object],
) -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world_runtime_health.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert list(Draft202012Validator(schema).iter_errors(payload))


def test_private_world_health_schema_accepts_every_public_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world_runtime_health.schema.json").read_text(
            encoding="utf-8"
        )
    )
    available = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path / "available")}
    )
    degraded = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path / "degraded")}
    )
    assert isinstance(degraded.port, SQLitePrivateWorldLedger)

    def unavailable_health() -> dict[str, int | str]:
        raise sqlite3.DatabaseError("synthetic sqlite failure")

    monkeypatch.setattr(degraded.port, "health", unavailable_health)
    statuses = [
        available.public_status(),
        create_private_world_runtime(
            {"OLIVIA_PRIVATE_WORLD_ENABLED": "0"}
        ).public_status(),
        create_private_world_runtime(
            {"OLIVIA_PRIVATE_WORLD_ENABLED": "maybe"}
        ).public_status(),
        create_private_world_runtime({}).public_status(),
        create_private_world_runtime(
            {"OLIVIA_PRIVATE_WORLD_DB": "relative.sqlite3"}
        ).public_status(),
        degraded.public_status(),
    ]

    validator = Draft202012Validator(schema)
    assert all(list(validator.iter_errors(status)) == [] for status in statuses)


def test_runtime_health_fails_closed_when_sqlite_becomes_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )
    assert isinstance(runtime.port, SQLitePrivateWorldLedger)

    def unavailable_health() -> dict[str, int | str]:
        raise sqlite3.DatabaseError("synthetic sqlite failure")

    monkeypatch.setattr(runtime.port, "health", unavailable_health)

    assert runtime.public_status() == {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "PRIVATE_WORLD_STORAGE_UNAVAILABLE",
        "enabled": True,
        "schema_version": 2,
        "migration_status": "created_v2",
        "event_count": 0,
        "snapshot_count": 0,
        "probe": "not-run",
        "network_called": False,
    }


def test_runtime_health_fails_closed_when_current_snapshot_is_semantically_corrupt(
    tmp_path: Path,
) -> None:
    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )
    assert isinstance(runtime.port, SQLitePrivateWorldLedger)
    runtime.port.apply_once(
        LedgerEvent(
            event_id="corrupt-health-event",
            delivery_id="corrupt-health-delivery",
            event_type="canonical_reply_delivered",
            payload={"applied": False},
            occurred_at="2026-08-22T00:00:00+00:00",
        ),
        PrivateWorldSnapshot(version=1),
    )
    database = tmp_path / PRIVATE_WORLD_DEFAULT_RELATIVE_PATH
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE private_world_snapshots SET payload_json = ?",
            ("[]",),
        )

    assert runtime.public_status() == {
        "status": "unavailable",
        "provider": "none",
        "reason_code": "PRIVATE_WORLD_STORAGE_UNAVAILABLE",
        "enabled": True,
        "schema_version": 2,
        "migration_status": "created_v2",
        "event_count": 0,
        "snapshot_count": 0,
        "probe": "not-run",
        "network_called": False,
    }


def test_unknown_stored_snapshot_field_degrades_health_and_canonical_commit(
    tmp_path: Path,
) -> None:
    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )
    assert isinstance(runtime.port, SQLitePrivateWorldLedger)
    assert runtime.committer is not None
    runtime.port.apply_once(
        LedgerEvent(
            event_id="unknown-field-event",
            delivery_id="unknown-field-delivery",
            event_type="canonical_reply_delivered",
            payload={"applied": False},
            occurred_at="2026-08-22T00:00:00+00:00",
        ),
        PrivateWorldSnapshot(version=1),
    )
    payload = PrivateWorldSnapshot(version=1).to_dict()
    payload["unrecognized_control_state"] = "must-not-be-ignored"
    database = tmp_path / PRIVATE_WORLD_DEFAULT_RELATIVE_PATH
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE private_world_snapshots SET payload_json = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
        )

    assert runtime.public_status()["status"] == "unavailable"
    assert runtime.committer.commit(
        DeliveryEvent(
            delivery_id="unknown-field-commit:1",
            kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
            occurred_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
            semantic_key="canonical.unknown-field",
        )
    ) is DeliveryStatus.UNAVAILABLE


@pytest.mark.parametrize(
    "corruption",
    [
        "unknown_nested_field",
        "invalid_bounded_value",
        "row_payload_version_mismatch",
        "noncanonical_roundtrip",
    ],
)
def test_strict_stored_snapshot_validation_rejects_every_noncanonical_row(
    tmp_path: Path,
    corruption: str,
) -> None:
    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )
    assert isinstance(runtime.port, SQLitePrivateWorldLedger)
    assert runtime.committer is not None
    runtime.port.apply_once(
        LedgerEvent(
            event_id=f"{corruption}-event",
            delivery_id=f"{corruption}-delivery",
            event_type="canonical_reply_delivered",
            payload={"applied": False},
            occurred_at="2026-08-22T00:00:00+00:00",
        ),
        PrivateWorldSnapshot(version=1),
    )
    payload = PrivateWorldSnapshot(version=1).to_dict()
    if corruption == "unknown_nested_field":
        payload["continuation_facts"] = [
            {
                "fact_id": "known.synthetic",
                "statement": "合成已知事实。",
                "awareness": "character_known",
                "unrecognized_control_state": "must-not-be-ignored",
            }
        ]
    elif corruption == "invalid_bounded_value":
        payload["trust"] = 101
    elif corruption == "row_payload_version_mismatch":
        payload["version"] = 2
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=corruption != "noncanonical_roundtrip",
        separators=(",", ":") if corruption != "noncanonical_roundtrip" else None,
    )
    database = tmp_path / PRIVATE_WORLD_DEFAULT_RELATIVE_PATH
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE private_world_snapshots SET payload_json = ?",
            (encoded,),
        )

    assert runtime.public_status()["status"] == "unavailable"
    assert runtime.committer.commit(
        DeliveryEvent(
            delivery_id=f"{corruption}-commit:1",
            kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
            occurred_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
            semantic_key=f"canonical.{corruption}",
        )
    ) is DeliveryStatus.UNAVAILABLE


def test_runtime_degrades_when_path_resolution_raises_sqlite_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_resolution(
        environ: dict[str, str] | None = None,
    ) -> tuple[Path | None, str | None, bool]:
        raise sqlite3.DatabaseError("synthetic sqlite failure")

    monkeypatch.setattr(
        private_world_runtime,
        "resolve_private_world_database",
        unavailable_resolution,
    )

    runtime = create_private_world_runtime({"OLIVIA_LOCAL_DATA_ROOT": "unused"})

    assert isinstance(runtime.port, NullPrivateWorldPort)
    assert runtime.committer is None
    assert runtime.public_status()["status"] == "unavailable"
    assert runtime.public_status()["reason_code"] == "PRIVATE_WORLD_STORAGE_UNAVAILABLE"


def test_runtime_degrades_when_ledger_construction_raises_sqlite_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_ledger(_path: Path) -> SQLitePrivateWorldLedger:
        raise sqlite3.DatabaseError("synthetic sqlite failure")

    monkeypatch.setattr(
        private_world_runtime,
        "SQLitePrivateWorldLedger",
        unavailable_ledger,
    )

    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )

    assert isinstance(runtime.port, NullPrivateWorldPort)
    assert runtime.committer is None
    assert runtime.public_status()["status"] == "unavailable"
    assert runtime.public_status()["reason_code"] == "PRIVATE_WORLD_STORAGE_UNAVAILABLE"


def test_runtime_degrades_when_startup_health_raises_sqlite_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyLedger:
        def __init__(self, _path: Path) -> None:
            pass

        def health(self) -> dict[str, int | str]:
            raise sqlite3.DatabaseError("synthetic sqlite failure")

    monkeypatch.setattr(
        private_world_runtime,
        "SQLitePrivateWorldLedger",
        UnhealthyLedger,
    )

    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )

    assert isinstance(runtime.port, NullPrivateWorldPort)
    assert runtime.committer is None
    assert runtime.public_status()["status"] == "unavailable"
    assert runtime.public_status()["reason_code"] == "PRIVATE_WORLD_STORAGE_UNAVAILABLE"


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


def test_available_sqlite_projects_only_character_view_into_reply_context(
    tmp_path: Path,
) -> None:
    from local_server import LetterAdapter
    from reply_context import ReplyMode

    runtime = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )
    assert isinstance(runtime.port, SQLitePrivateWorldLedger)
    runtime.port.apply_once(
        LedgerEvent(
            event_id="projection-event",
            delivery_id="projection-delivery",
            event_type="canonical_reply_delivered",
            payload={"applied": False},
            occurred_at="2026-08-22T00:00:00+00:00",
        ),
        PrivateWorldSnapshot(
            version=1,
            familiarity=88,
            trust=91,
            comfort=77,
            closeness=74,
            tension=12,
            relationship_stage="close",
            nickname_permissions=("合成称呼",),
            home_access=HomeAccess.DOMESTIC_ACCESS,
            continuation_facts=(
                LocalContinuationFact(
                    "known.class",
                    "角色已知的合成课程调整。",
                    ContinuationAwareness.CHARACTER_KNOWN,
                ),
                LocalContinuationFact(
                    "pending.trip",
                    "角色未知的合成旅行安排。",
                    ContinuationAwareness.PENDING,
                ),
                LocalContinuationFact(
                    "control.plan",
                    "仅控制层可见的合成计划。",
                    ContinuationAwareness.CONTROL_ONLY,
                ),
            ),
        ),
    )

    context = LetterAdapter(private_world_port=runtime.port).build_reply_context(
        ReplyMode.TEXT_LETTER
    )
    serialized_model_private_world = json.dumps(
        {
            "private_behavior": context.private_behavior.to_dict(),
            "world_facts": [fact.to_dict() for fact in context.world_facts],
        },
        ensure_ascii=False,
    )
    serialized_health = json.dumps(runtime.public_status(), ensure_ascii=False)

    assert context.private_behavior.to_dict() == {
        "familiarity": "high",
        "trust": "high",
        "comfort": "high",
        "closeness": "high",
        "tension": "low",
        "relationship_stage": "close",
        "nickname_permission": "allowed",
        "home_history_allowed": True,
        "known_continuations": [
            {
                "fact_id": "known.class",
                "statement": "角色已知的合成课程调整。",
            }
        ],
    }
    assert "合成称呼" in serialized_model_private_world
    assert "角色已知的合成课程调整。" in serialized_model_private_world
    for hidden in (
        "88",
        "91",
        "77",
        "74",
        "12",
        "pending.trip",
        "control.plan",
        "角色未知的合成旅行安排。",
        "仅控制层可见的合成计划。",
        "pending",
        "control_only",
        "no_access",
        "visit_access",
        "errand_access",
        "domestic_access",
    ):
        assert hidden not in serialized_model_private_world
        assert hidden not in serialized_health


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
