"""SQLite event ledger for PrivateWorld state; contains no reduction policy."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    PrivateWorldCharacterView,
    PrivateWorldControlView,
    PrivateWorldSnapshot,
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
            not isinstance(value, str) or not _ID_RE.fullmatch(value)
            for value in (self.event_id, self.delivery_id, self.event_type)
        ):
            raise ValueError("event identifiers are invalid")
        if not isinstance(self.payload, dict):
            raise ValueError("event payload must be an object")
        try:
            encoded = json.dumps(
                self.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("event payload must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("event payload is too large")
        try:
            timestamp = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("event timestamp is invalid") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")

    def _payload_json(self) -> str:
        return json.dumps(
            self.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


class SQLitePrivateWorldLedger:
    def __init__(self, database_path: Path) -> None:
        path = Path(database_path)
        if str(path) in {"", "."} or path.exists() and path.is_dir():
            raise ValueError("an explicit database file path is required")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._database_path = path
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
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
                """
            )

    def apply_once(self, event: LedgerEvent, snapshot: PrivateWorldSnapshot) -> bool:
        if not isinstance(event, LedgerEvent) or not isinstance(
            snapshot, PrivateWorldSnapshot
        ):
            raise TypeError("apply_once requires a typed event and snapshot")
        try:
            with self._connection() as connection:
                duplicate = connection.execute(
                    """SELECT 1 FROM private_world_events
                       WHERE event_id = ? OR delivery_id = ? LIMIT 1""",
                    (event.event_id, event.delivery_id),
                ).fetchone()
                if duplicate:
                    return False
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
                connection.execute(
                    """INSERT INTO private_world_snapshots
                       (version, payload_json, event_id) VALUES (?, ?, ?)""",
                    (
                        snapshot.version,
                        json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":")),
                        event.event_id,
                    ),
                )
        except sqlite3.Error as exc:
            raise LedgerWriteError("private world transaction failed") from exc
        return True

    def events(self) -> tuple[LedgerEvent, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event_id, delivery_id, event_type, payload_json, occurred_at
                   FROM private_world_events ORDER BY rowid"""
            ).fetchall()
        return tuple(
            LedgerEvent(row[0], row[1], row[2], json.loads(row[3]), row[4])
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
        return PrivateWorldSnapshot(
            version=payload["version"],
            familiarity=payload["familiarity"],
            trust=payload["trust"],
            comfort=payload["comfort"],
            closeness=payload["closeness"],
            tension=payload["tension"],
            relationship_stage=payload["relationship_stage"],
            nickname_permissions=tuple(payload["nickname_permissions"]),
            home_access=HomeAccess(payload["home_access"]),
            continuation_awareness=ContinuationAwareness(
                payload["continuation_awareness"]
            ),
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
