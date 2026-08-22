"""SQLite event ledger for PrivateWorld state; contains no reduction policy."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    LocalContinuationFact,
    PrivateWorldCharacterView,
    PrivateWorldControlView,
    PrivateWorldSnapshot,
)


PRIVATE_WORLD_LEDGER_SCHEMA_VERSION = 2
_METADATA_KEY = "schema_version"
_LEGACY_TABLES = frozenset(
    {"private_world_events", "private_world_snapshots"}
)


class LedgerWriteError(RuntimeError):
    code = "PRIVATE_WORLD_WRITE_FAILED"


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    delivery_id: str
    event_type: str
    payload: dict[str, object]
    occurred_at: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str)
            or not _ID_RE.fullmatch(value)
            for value in (
                self.event_id,
                self.delivery_id,
                self.event_type,
            )
        ):
            raise ValueError("event identifiers are invalid")
        if not isinstance(self.payload, dict):
            raise ValueError("event payload must be an object")
        try:
            encoded = json.dumps(
                self.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "event payload must be JSON serializable"
            ) from exc
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("event payload is too large")
        try:
            timestamp = datetime.fromisoformat(
                self.occurred_at.replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("event timestamp is invalid") from exc
        if (
            timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise ValueError(
                "event timestamp must be timezone-aware"
            )

    def _payload_json(self) -> str:
        return json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class SQLitePrivateWorldLedger:
    def __init__(self, database_path: Path) -> None:
        path = Path(database_path)
        if (
            str(path) in {"", "."}
            or path.exists()
            and path.is_dir()
        ):
            raise ValueError(
                "an explicit database file path is required"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._database_path = path
        self._migration_status = "unknown"
        self._initialize()

    @property
    def schema_version(self) -> int:
        return PRIVATE_WORLD_LEDGER_SCHEMA_VERSION

    @property
    def migration_status(self) -> str:
        return self._migration_status

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._database_path,
            timeout=5,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _existing_schema_version(self) -> int:
        if (
            not self._database_path.is_file()
            or self._database_path.stat().st_size == 0
        ):
            return 0
        try:
            with closing(
                sqlite3.connect(
                    self._database_path,
                    timeout=5,
                )
            ) as connection:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                tables = {str(row[0]) for row in rows}
                if not tables:
                    return 0
                if "private_world_metadata" in tables:
                    row = connection.execute(
                        "SELECT value FROM private_world_metadata WHERE key = ?",
                        (_METADATA_KEY,),
                    ).fetchone()
                    if row is None:
                        raise LedgerWriteError(
                            "private world schema metadata is incomplete"
                        )
                    try:
                        return int(row[0])
                    except (TypeError, ValueError) as exc:
                        raise LedgerWriteError(
                            "private world schema metadata is invalid"
                        ) from exc
                if _LEGACY_TABLES.issubset(tables):
                    return 1
                raise LedgerWriteError(
                    "private world schema is unrecognized"
                )
        except sqlite3.Error as exc:
            raise LedgerWriteError(
                "private world schema inspection failed"
            ) from exc

    def _backup_legacy_database(self) -> None:
        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        backup = self._database_path.with_name(
            f"{self._database_path.name}.pre-v2-{stamp}.bak"
        )
        try:
            with closing(
                sqlite3.connect(
                    self._database_path,
                    timeout=5,
                )
            ) as source, closing(
                sqlite3.connect(
                    backup,
                    timeout=5,
                )
            ) as destination:
                source.backup(destination)
                destination.commit()
        except sqlite3.Error as exc:
            backup.unlink(missing_ok=True)
            raise LedgerWriteError(
                "private world migration backup failed"
            ) from exc

    def _initialize(self) -> None:
        previous = self._existing_schema_version()
        if previous > PRIVATE_WORLD_LEDGER_SCHEMA_VERSION:
            raise LedgerWriteError(
                "private world schema is newer than this runtime"
            )
        if previous == 1:
            self._backup_legacy_database()
            self._migration_status = "migrated_v1_to_v2"
        elif previous == 0:
            self._migration_status = "created_v2"
        else:
            self._migration_status = "current_v2"

        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS private_world_events (
                        event_id TEXT PRIMARY KEY,
                        delivery_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        occurred_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS private_world_snapshots (
                        version INTEGER PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        event_id TEXT NOT NULL UNIQUE,
                        FOREIGN KEY(event_id) REFERENCES private_world_events(event_id)
                    );
                    CREATE TABLE IF NOT EXISTS private_world_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    """INSERT INTO private_world_metadata (key, value)
                       VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                    (
                        _METADATA_KEY,
                        str(PRIVATE_WORLD_LEDGER_SCHEMA_VERSION),
                    ),
                )
        except sqlite3.Error as exc:
            raise LedgerWriteError(
                "private world schema initialization failed"
            ) from exc

    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
    ) -> bool:
        if not isinstance(event, LedgerEvent) or not isinstance(
            snapshot,
            PrivateWorldSnapshot,
        ):
            raise TypeError(
                "apply_once requires a typed event and snapshot"
            )
        try:
            with self._connection() as connection:
                duplicate = connection.execute(
                    """SELECT 1 FROM private_world_events
                       WHERE event_id = ? OR delivery_id = ? LIMIT 1""",
                    (
                        event.event_id,
                        event.delivery_id,
                    ),
                ).fetchone()
                if duplicate:
                    return False
                latest = connection.execute(
                    """SELECT version, payload_json FROM private_world_snapshots
                       ORDER BY version DESC LIMIT 1"""
                ).fetchone()
                snapshot_json = json.dumps(
                    snapshot.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if latest is None:
                    write_snapshot = snapshot.version in {1, 2}
                elif snapshot.version == latest[0]:
                    if snapshot_json != latest[1]:
                        raise LedgerWriteError(
                            "unchanged snapshot version must preserve state"
                        )
                    write_snapshot = False
                else:
                    write_snapshot = (
                        snapshot.version == latest[0] + 1
                    )
                if not write_snapshot and (
                    latest is None
                    or snapshot.version != latest[0]
                ):
                    raise LedgerWriteError(
                        "snapshot version is not contiguous"
                    )
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
                if write_snapshot:
                    connection.execute(
                        """INSERT INTO private_world_snapshots
                           (version, payload_json, event_id) VALUES (?, ?, ?)""",
                        (
                            snapshot.version,
                            snapshot_json,
                            event.event_id,
                        ),
                    )
        except sqlite3.Error as exc:
            raise LedgerWriteError(
                "private world transaction failed"
            ) from exc
        return True

    def events(self) -> tuple[LedgerEvent, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event_id, delivery_id, event_type, payload_json, occurred_at
                   FROM private_world_events ORDER BY rowid"""
            ).fetchall()
        return tuple(
            LedgerEvent(
                row[0],
                row[1],
                row[2],
                json.loads(row[3]),
                row[4],
            )
            for row in rows
        )

    def snapshot(self) -> PrivateWorldSnapshot:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT payload_json FROM private_world_snapshots
                   ORDER BY version DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return PrivateWorldSnapshot()
        payload = json.loads(row[0])
        continuation_facts = tuple(
            LocalContinuationFact(
                fact_id=item["fact_id"],
                statement=item["statement"],
                awareness=ContinuationAwareness(
                    item["awareness"]
                ),
            )
            for item in payload.get(
                "continuation_facts",
                (),
            )
        )
        return PrivateWorldSnapshot(
            version=payload["version"],
            familiarity=payload["familiarity"],
            trust=payload["trust"],
            comfort=payload["comfort"],
            closeness=payload["closeness"],
            tension=payload["tension"],
            relationship_stage=payload[
                "relationship_stage"
            ],
            nickname_permissions=tuple(
                payload["nickname_permissions"]
            ),
            home_access=HomeAccess(
                payload["home_access"]
            ),
            continuation_awareness=ContinuationAwareness(
                payload["continuation_awareness"]
            ),
            continuation_facts=continuation_facts,
        )

    def control_view(self) -> PrivateWorldControlView:
        return self.snapshot().control_view()

    def character_view(self) -> PrivateWorldCharacterView:
        return self.snapshot().character_view()

    def health(self) -> dict[str, int | str]:
        with self._connection() as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM private_world_events"
            ).fetchone()[0]
            snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM private_world_snapshots"
            ).fetchone()[0]
        return {
            "status": "READY",
            "event_count": event_count,
            "snapshot_count": snapshot_count,
        }
