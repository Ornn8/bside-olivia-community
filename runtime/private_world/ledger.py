"""SQLite event ledger for PrivateWorld state; contains no reduction policy."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from .port import (
    ContinuationAwareness,
    HomeAccess,
    IntimacyGrant,
    LocalContinuationFact,
    PrivateWorldCharacterView,
    PrivateWorldControlView,
    PrivateWorldSnapshot,
)
from runtime.reply.reply_context import IntimacyTier


PRIVATE_WORLD_LEDGER_SCHEMA_VERSION = 3
_METADATA_KEY = "schema_version"
_LEGACY_TABLES = frozenset(
    {"private_world_events", "private_world_snapshots"}
)
_V1_PAYLOAD_FIELDS = frozenset(
    {
        "version",
        "view",
        "familiarity",
        "trust",
        "comfort",
        "closeness",
        "tension",
        "relationship_stage",
        "nickname_permissions",
        "home_access",
        "continuation_awareness",
    }
)
_V2_PAYLOAD_FIELDS = _V1_PAYLOAD_FIELDS | {"continuation_facts"}
_V3_PAYLOAD_FIELDS = _V2_PAYLOAD_FIELDS | {
    "intimacy_grants",
    "growth_window_start",
    "growth_used",
}


class LedgerWriteError(RuntimeError):
    code = "PRIVATE_WORLD_WRITE_FAILED"


class LedgerVersionConflictError(LedgerWriteError):
    code = "PRIVATE_WORLD_VERSION_CONFLICT"


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

    @staticmethod
    def _existing_schema_version(connection: sqlite3.Connection) -> int:
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
        raise LedgerWriteError("private world schema is unrecognized")

    def _backup_legacy_database(self, source: sqlite3.Connection) -> None:
        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        backup = self._database_path.with_name(
            f"{self._database_path.name}.pre-v3-{stamp}.bak"
        )
        created = False
        try:
            image = source.serialize()
            with backup.open("xb") as stream:
                created = True
                stream.write(image)
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, sqlite3.Error) as exc:
            if created:
                backup.unlink(missing_ok=True)
            raise LedgerWriteError(
                "private world migration backup failed"
            ) from exc

    def _initialize(self) -> None:
        try:
            with closing(
                sqlite3.connect(self._database_path, timeout=5)
            ) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    previous = self._existing_schema_version(connection)
                    if previous > PRIVATE_WORLD_LEDGER_SCHEMA_VERSION:
                        raise LedgerWriteError(
                            "private world schema is newer than this runtime"
                        )
                    migrated_rows: tuple[tuple[int, str], ...] = ()
                    if previous == 1:
                        v2_rows = self._validated_v1_rows(connection)
                        self._backup_legacy_database(connection)
                        connection.executemany(
                            """UPDATE private_world_snapshots
                               SET payload_json = ? WHERE version = ?""",
                            (
                                (payload_json, version)
                                for version, payload_json in v2_rows
                            ),
                        )
                        migrated_rows = self._validated_v2_rows(connection)
                    elif previous == 2:
                        migrated_rows = self._validated_v2_rows(connection)
                        self._backup_legacy_database(connection)
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS private_world_events (
                        event_id TEXT PRIMARY KEY,
                        delivery_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        occurred_at TEXT NOT NULL
                        )"""
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS private_world_snapshots (
                        version INTEGER PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        event_id TEXT NOT NULL UNIQUE,
                        FOREIGN KEY(event_id) REFERENCES private_world_events(event_id)
                        )"""
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS private_world_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                        )"""
                    )
                    if migrated_rows:
                        connection.executemany(
                            """UPDATE private_world_snapshots
                           SET payload_json = ? WHERE version = ?""",
                            (
                                (payload_json, version)
                                for version, payload_json in migrated_rows
                            ),
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
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except sqlite3.Error as exc:
            raise LedgerWriteError(
                "private world schema initialization failed"
            ) from exc
        if previous in {1, 2}:
            self._migration_status = "migrated_v2_to_v3"
        elif previous == 0:
            self._migration_status = "created_v3"
        else:
            self._migration_status = "current_v3"

    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
        expected_snapshot_version: int | None = None,
    ) -> bool:
        if not isinstance(event, LedgerEvent) or not isinstance(
            snapshot,
            PrivateWorldSnapshot,
        ):
            raise TypeError(
                "apply_once requires a typed event and snapshot"
            )
        if expected_snapshot_version is not None and (
            type(expected_snapshot_version) is not int
            or expected_snapshot_version < 1
        ):
            raise TypeError(
                "expected snapshot version must be a positive integer"
            )
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
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
                if latest is not None:
                    self._strict_stored_snapshot(latest[0], latest[1])
                current_version = (
                    latest[0]
                    if latest is not None
                    else PrivateWorldSnapshot().version
                )
                if (
                    expected_snapshot_version is not None
                    and current_version != expected_snapshot_version
                ):
                    raise LedgerVersionConflictError(
                        "snapshot base version is stale"
                    )
                snapshot_json = self._snapshot_json(snapshot)
                if latest is None:
                    if snapshot.version not in {1, 2}:
                        raise LedgerWriteError(
                            "snapshot version is not contiguous"
                        )
                    write_snapshot = True
                elif snapshot.version == latest[0]:
                    if snapshot_json != latest[1]:
                        raise LedgerVersionConflictError(
                            "unchanged snapshot version must preserve state"
                        )
                    write_snapshot = False
                elif snapshot.version == latest[0] + 1:
                    write_snapshot = True
                elif snapshot.version < latest[0]:
                    raise LedgerVersionConflictError(
                        "snapshot version is stale"
                    )
                else:
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

    @staticmethod
    def _snapshot_json(snapshot: PrivateWorldSnapshot) -> str:
        return json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _v1_snapshot_json(snapshot: PrivateWorldSnapshot) -> str:
        payload = json.loads(SQLitePrivateWorldLedger._v2_snapshot_json(snapshot))
        payload.pop("continuation_facts")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _v2_snapshot_json(snapshot: PrivateWorldSnapshot) -> str:
        payload = snapshot.to_dict()
        payload.pop("intimacy_grants")
        payload.pop("growth_window_start")
        payload.pop("growth_used")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _payload_object(payload_json: object) -> dict[str, object]:
        if not isinstance(payload_json, str):
            raise LedgerWriteError("stored state payload is invalid")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise LedgerWriteError("stored state payload is invalid") from exc
        if not isinstance(payload, dict):
            raise LedgerWriteError("stored state must be an object")
        return payload

    @staticmethod
    def _snapshot_from_payload(
        payload: dict[str, object],
        facts: tuple[LocalContinuationFact, ...],
        grants: tuple[IntimacyGrant, ...] = (),
    ) -> PrivateWorldSnapshot:
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
            continuation_facts=facts,
            intimacy_grants=grants,
            growth_window_start=payload.get("growth_window_start", ""),
            growth_used=payload.get("growth_used", 0),
        )

    def _strict_v1_stored_snapshot(
        self,
        stored_version: object,
        payload_json: object,
    ) -> PrivateWorldSnapshot:
        if type(stored_version) is not int or stored_version < 1:
            raise LedgerWriteError("stored v1 row version is invalid")
        try:
            payload = self._payload_object(payload_json)
            fields = set(payload)
            if fields not in {_V1_PAYLOAD_FIELDS, _V2_PAYLOAD_FIELDS}:
                raise ValueError("stored v1 fields are invalid")
            if payload["view"] != "snapshot":
                raise ValueError("stored v1 view is invalid")
            facts = payload.get("continuation_facts", [])
            if fields == _V2_PAYLOAD_FIELDS and not isinstance(facts, list):
                raise ValueError("stored v1 continuation facts must be a list")
            snapshot = self._snapshot_from_payload(
                payload,
                tuple(
                    LocalContinuationFact(
                        fact_id=item["fact_id"],
                        statement=item["statement"],
                        awareness=ContinuationAwareness(item["awareness"]),
                    )
                    for item in facts
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerWriteError("stored v1 state is invalid") from exc
        if snapshot.version != stored_version:
            raise LedgerWriteError("stored v1 version does not match row")
        expected_payload = (
            self._v2_snapshot_json(snapshot)
            if fields == _V2_PAYLOAD_FIELDS
            else self._v1_snapshot_json(snapshot)
        )
        if expected_payload != payload_json:
            raise LedgerWriteError("stored v1 state is not canonical")
        return snapshot

    def _validated_v1_rows(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[tuple[int, str], ...]:
        rows = connection.execute(
            """SELECT version, payload_json FROM private_world_snapshots
               ORDER BY version"""
        ).fetchall()
        migrated: list[tuple[int, str]] = []
        for stored_version, payload_json in rows:
            snapshot = self._strict_v1_stored_snapshot(stored_version, payload_json)
            migrated.append((stored_version, self._v2_snapshot_json(snapshot)))
        return tuple(migrated)

    def _validated_v2_rows(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[tuple[int, str], ...]:
        rows = connection.execute(
            """SELECT version, payload_json FROM private_world_snapshots
               ORDER BY version"""
        ).fetchall()
        migrated: list[tuple[int, str]] = []
        for stored_version, payload_json in rows:
            if type(stored_version) is not int or stored_version < 1:
                raise LedgerWriteError("stored v2 row version is invalid")
            try:
                payload = self._payload_object(payload_json)
                if set(payload) != _V2_PAYLOAD_FIELDS:
                    raise ValueError("stored v2 fields are invalid")
                if payload["view"] != "snapshot":
                    raise ValueError("stored v2 view is invalid")
                facts = payload["continuation_facts"]
                if not isinstance(facts, list):
                    raise ValueError("stored v2 continuation facts must be a list")
                snapshot = self._snapshot_from_payload(
                    payload,
                    tuple(
                        LocalContinuationFact(
                            fact_id=item["fact_id"],
                            statement=item["statement"],
                            awareness=ContinuationAwareness(item["awareness"]),
                        )
                        for item in facts
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise LedgerWriteError("stored v2 state is invalid") from exc
            if snapshot.version != stored_version:
                raise LedgerWriteError("stored v2 version does not match row")
            if self._v2_snapshot_json(snapshot) != payload_json:
                raise LedgerWriteError("stored v2 state is not canonical")
            migrated.append((stored_version, self._snapshot_json(snapshot)))
        return tuple(migrated)

    def _strict_stored_snapshot(
        self,
        stored_version: object,
        payload_json: object,
    ) -> PrivateWorldSnapshot:
        """Load only a canonical stored-state row that round-trips without loss."""

        if type(stored_version) is not int or stored_version < 1:
            raise LedgerWriteError("stored snapshot row version is invalid")
        try:
            payload = self._payload_object(payload_json)
            if set(payload) != _V3_PAYLOAD_FIELDS:
                raise ValueError("stored state fields are invalid")
            if payload["view"] != "snapshot":
                raise ValueError("stored state view is invalid")
            facts = payload["continuation_facts"]
            if not isinstance(facts, list):
                raise ValueError("stored continuation facts must be a list")
            grants = payload["intimacy_grants"]
            if not isinstance(grants, list):
                raise ValueError("stored intimacy grants must be a list")
            snapshot = self._snapshot_from_payload(
                payload,
                tuple(
                    LocalContinuationFact(
                        fact_id=item["fact_id"],
                        statement=item["statement"],
                        awareness=ContinuationAwareness(item["awareness"]),
                    )
                    for item in facts
                ),
                tuple(
                    IntimacyGrant(
                        grant_id=item["grant_id"],
                        tier=IntimacyTier(item["tier"]),
                        statement=item["statement"],
                    )
                    for item in grants
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerWriteError("stored snapshot is invalid") from exc
        if snapshot.version != stored_version:
            raise LedgerWriteError("stored snapshot version does not match row")
        if self._snapshot_json(snapshot) != payload_json:
            raise LedgerWriteError("stored snapshot is not canonical")
        return snapshot

    def snapshot(self) -> PrivateWorldSnapshot:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT version, payload_json FROM private_world_snapshots
                   ORDER BY version DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return PrivateWorldSnapshot()
        return self._strict_stored_snapshot(row[0], row[1])

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
            latest = connection.execute(
                """SELECT version, payload_json FROM private_world_snapshots
                   ORDER BY version DESC LIMIT 1"""
            ).fetchone()
        if latest is not None:
            self._strict_stored_snapshot(latest[0], latest[1])
        return {
            "status": "READY",
            "event_count": event_count,
            "snapshot_count": snapshot_count,
        }
