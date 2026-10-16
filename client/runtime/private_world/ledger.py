"""SQLite event ledger for PrivateWorld state; contains no reduction policy."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from .port import (
    AcknowledgedAffection,
    ActiveBoundary,
    AffectionIntensity,
    AffectionScope,
    ContinuationAwareness,
    HomeAccess,
    IntimacyGrant,
    LocalContinuationFact,
    PrivateWorldCharacterView,
    PrivateWorldControlView,
    PrivateWorldSnapshot,
)
from runtime.reply.reply_context import IntimacyTier


PRIVATE_WORLD_LEDGER_SCHEMA_VERSION = 4
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
_V4_PAYLOAD_FIELDS = _V3_PAYLOAD_FIELDS | {
    "active_boundaries",
    "acknowledged_affection",
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
    reducer_input: dict | None = field(default=None, repr=False, compare=False)
    projection_result: dict | None = field(default=None, repr=False, compare=False)

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
            f"{self._database_path.name}.pre-v4-{stamp}.bak"
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
                    elif previous == 3:
                        migrated_rows = self._validated_v3_rows(connection)
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
        if previous in {1, 2, 3}:
            self._migration_status = f"migrated_v{previous}_to_v4"
        elif previous == 0:
            self._migration_status = "created_v4"
        else:
            self._migration_status = "current_v4"

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
        moment_kind = None
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
                current = self._strict_stored_snapshot(*latest) if latest else PrivateWorldSnapshot()
                if event.reducer_input is not None:
                    event, snapshot = self._project(connection, event, current)
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
                        json.dumps({key: value for key, value in event.payload.items() if key != 'relationship_moment'},
                                   ensure_ascii=False, sort_keys=True, separators=(',', ':')),
                        event.occurred_at,
                    ),
                )
                if event.reducer_input is not None:
                    connection.execute('INSERT INTO private_world_projection_inputs(event_id, payload_json, result_json) VALUES (?,?,?)',
                                       (event.event_id, json.dumps(event.reducer_input, ensure_ascii=False, sort_keys=True),
                                        json.dumps(event.projection_result, sort_keys=True)))
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
                moment = event.payload.get('relationship_moment')
                if moment and event.payload.get('reason_code') != 'SEMANTIC_DUPLICATE':
                    from runtime.relationship_moments import record_moment
                    moment_kind = record_moment(connection, event.payload['canonical_delivery_id'], event.occurred_at, moment)
                if snapshot.relationship_volume > current.relationship_volume:
                    connection.execute('CREATE TABLE IF NOT EXISTS relationship_volumes '
                                       '(volume INTEGER PRIMARY KEY, opened_at TEXT NOT NULL)')
                    connection.executemany('INSERT OR IGNORE INTO relationship_volumes VALUES (?,?)',
                        ((volume, event.occurred_at) for volume in range(current.relationship_volume + 1, snapshot.relationship_volume + 1)))
                initialized = (event.event_type == 'initialize_historical_relationship'
                               and event.payload.get('reason_code') == 'INITIALIZE_HISTORICAL_RELATIONSHIP')
                if initialized or (event.reducer_input is None and snapshot != current):
                    # An old writer/control path has no replayable input. Its
                    # committed state is an explicit compatibility checkpoint,
                    # never reverse-engineer commands from score differences.
                    self._save_projection_base(connection, snapshot)
        except sqlite3.Error as exc:
            raise LedgerWriteError(
                "private world transaction failed"
            ) from exc
        return True

    def _save_projection_base(self, connection, snapshot):
        cursor = connection.execute('SELECT COALESCE(MAX(rowid),0) FROM private_world_events').fetchone()[0]
        value = json.dumps({'cursor': cursor, 'snapshot': self._snapshot_json(snapshot)}, sort_keys=True)
        connection.execute('INSERT OR REPLACE INTO private_world_metadata(key,value) VALUES (?,?)',
                           ('ordered_projection_v1', value))

    def _project(self, connection, incoming, current):
        """Replay frozen inputs inside the same transaction as the new audit.

        No schema migration/rewrite of prior user records is required. Existing
        saved states supply the first baseline; subsequent typed inputs are ordered
        by UTC event time and stable event identity, independent of completion.
        """
        from .commands import ApplyHistoricalRelationshipEvidence, ConfirmRelationshipStage, command_from_record
        from .reducer import (_DEDUPLICATION_WINDOW, ReducerEventKind, reduce_private_world,
                              reduce_private_world_command, reducer_event_from_record)
        connection.execute('CREATE TABLE IF NOT EXISTS private_world_projection_inputs '
                           '(event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, result_json TEXT)')
        if 'result_json' not in {row[1] for row in connection.execute('PRAGMA table_info(private_world_projection_inputs)')}:
            connection.execute('ALTER TABLE private_world_projection_inputs ADD COLUMN result_json TEXT')
        row = connection.execute('SELECT value FROM private_world_metadata WHERE key=?',
                                 ('ordered_projection_v1',)).fetchone()
        if row is None:
            self._save_projection_base(connection, current)
            row = connection.execute('SELECT value FROM private_world_metadata WHERE key=?',
                                     ('ordered_projection_v1',)).fetchone()
        base = json.loads(row[0])
        opaque = connection.execute(
            'SELECT s.version, s.payload_json, e.rowid FROM private_world_events e '
            'JOIN private_world_snapshots s ON s.event_id=e.event_id '
            'LEFT JOIN private_world_projection_inputs p ON p.event_id=e.event_id '
            'WHERE e.rowid>? AND p.event_id IS NULL ORDER BY e.rowid DESC LIMIT 1',
            (base['cursor'],)).fetchone()
        if opaque is not None:
            # Also recognize writes made while rolled back to a pre-projection
            # binary: that writer cannot update our compatibility metadata.
            self._strict_stored_snapshot(opaque[0], opaque[1])
            base = {'cursor': opaque[2], 'snapshot': opaque[1]}
            connection.execute('UPDATE private_world_metadata SET value=? WHERE key=?',
                               (json.dumps(base, sort_keys=True), 'ordered_projection_v1'))
        base_payload = json.loads(base['snapshot'])
        projected = self._strict_stored_snapshot(base_payload['version'], base['snapshot'])
        rows = connection.execute('SELECT e.event_id, e.delivery_id, e.event_type, e.payload_json, e.occurred_at, p.payload_json '
                                  'FROM private_world_events e JOIN private_world_projection_inputs p ON e.event_id=p.event_id '
                                  'WHERE e.rowid>?', (base['cursor'],)).fetchall()
        records = [LedgerEvent(a, b, c, json.loads(d), e, json.loads(f)) for a, b, c, d, e, f in rows]
        records.append(incoming)
        records.sort(key=lambda item: (datetime.fromisoformat(item.occurred_at.replace('Z', '+00:00')).astimezone(timezone.utc),
            item.reducer_input['value']['command_id'] if item.reducer_input and item.reducer_input['kind'] == 'command'
            else item.event_id))
        legacy = connection.execute('SELECT e.payload_json, e.occurred_at, p.result_json FROM private_world_events e '
                                    'LEFT JOIN private_world_projection_inputs p ON e.event_id=p.event_id WHERE e.rowid<=?',
                                    (base['cursor'],)).fetchall()
        equivalents = []
        stage_watermark = None
        for raw, at, derived in legacy:
            audit = json.loads(raw)
            assessment = json.loads(derived) if derived else audit
            when = datetime.fromisoformat(at.replace('Z', '+00:00'))
            if assessment.get('applied') is True and audit.get('semantic_key'):
                equivalents.append((audit['semantic_key'], when))
            if 'relationship_stage' in audit.get('change_fields', ()):
                stage_watermark = max(stage_watermark, when) if stage_watermark else when
        live_stage = projected.relationship_stage != 'unknown'
        incoming_reason = None
        incoming_qualification = False
        outcomes = {}
        revision = connection.execute('SELECT COALESCE(MAX(rowid),0)+1 FROM private_world_events').fetchone()[0]
        # ponytail: replay the post-upgrade journal; checkpoint only after a
        # measured scale need and a defined late-event retention boundary.
        for item in records:
            frozen = item.reducer_input
            if frozen is None:
                continue  # Canonical delivery records do not alter state.
            if not isinstance(frozen, dict) or set(frozen) != {'schema', 'kind', 'value'} or frozen['schema'] != 1:
                raise LedgerWriteError('stored reducer input schema is invalid')
            if frozen['kind'] == 'event':
                candidate = reducer_event_from_record(frozen['value'])
                if candidate.kind.value != item.event_type or candidate.occurred_at != datetime.fromisoformat(item.occurred_at):
                    raise LedgerWriteError('stored reducer event time mismatch')
                if candidate.kind in {ReducerEventKind.SUPPORT_RECEIVED, ReducerEventKind.BOUNDARY_RESPECTED,
                                      ReducerEventKind.MEANINGFUL_EXCHANGE, ReducerEventKind.SHARED_EXPERIENCE,
                                      ReducerEventKind.CONFLICT, ReducerEventKind.REPAIR,
                                      ReducerEventKind.RELATIONSHIP_EVIDENCE}:
                    keys = {candidate.semantic_key, 'canonical-interaction:' + candidate.kind.value}
                    times = [at for key, at in equivalents if key in keys]
                    predecessor = max((at for at in times if at <= candidate.occurred_at), default=None)
                    if any(candidate.occurred_at < at < candidate.occurred_at + _DEDUPLICATION_WINDOW for at in times):
                        predecessor = candidate.occurred_at
                    candidate = replace(candidate, last_equivalent_at=predecessor)
                if candidate.kind is ReducerEventKind.STAGE_CONFIRMED and stage_watermark and candidate.occurred_at < stage_watermark:
                    candidate = replace(candidate, target_stage=projected.relationship_stage)
                result = reduce_private_world(projected, candidate)
                if result.delta.applied:
                    equivalents.append((candidate.semantic_key, candidate.occurred_at))
                live_stage = live_stage or candidate.kind is ReducerEventKind.STAGE_CONFIRMED
            elif frozen['kind'] == 'command':
                candidate = command_from_record(frozen['value'])
                if candidate.kind.value != item.event_type or candidate.occurred_at != datetime.fromisoformat(item.occurred_at):
                    raise LedgerWriteError('stored reducer command time mismatch')
                # A command denied at submission cannot acquire permission from
                # later arrival of an earlier stage confirmation.
                if item.payload.get('reason_code') == 'INTIMACY_EXCEEDS_STAGE':
                    outcomes[item.event_id] = {'applied': False, 'reason_code': 'INTIMACY_EXCEEDS_STAGE',
                                              'contact_qualification': False, 'revision': revision}
                    if item.event_id == incoming.event_id:
                        incoming_reason = 'INTIMACY_EXCEEDS_STAGE'
                    continue
                if isinstance(candidate, ApplyHistoricalRelationshipEvidence) and live_stage:
                    candidate = replace(candidate, relationship_stage=None)
                result = reduce_private_world_command(projected, candidate)
                live_stage = live_stage or isinstance(candidate, ConfirmRelationshipStage)
            else:
                raise LedgerWriteError('stored reducer input kind is invalid')
            projected = result.snapshot
            from runtime.personal_chat.initiative_profile import profile_from_snapshot
            profile = profile_from_snapshot(projected)
            outcomes[item.event_id] = {'applied': result.delta.applied, 'reason_code': result.delta.reason_code,
                'contact_qualification': sum(getattr(projected, key) >= 70
                    for key in ('familiarity', 'trust', 'comfort', 'closeness')) >= 3,
                'initiative_tier': profile.tier, 'initiative_caution': profile.caution,
                'revision': revision}
            if item.event_id == incoming.event_id:
                incoming_reason = result.delta.reason_code
                incoming_qualification = sum(getattr(projected, key) >= 70
                    for key in ('familiarity', 'trust', 'comfort', 'closeness')) >= 3
        if projected.relationship_volume < current.relationship_volume:
            floors = {'familiarity': 1} if current.relationship_volume >= 2 else {}
            if current.relationship_volume >= 3:
                floors.update(familiarity=35, comfort=35)
            if current.relationship_volume >= 4:
                floors['trust'] = 35
            projected = replace(projected, relationship_volume=current.relationship_volume,
                volume_started_days=min(current.volume_started_days, len(projected.relationship_days)),
                **{key: max(value, getattr(projected, key)) for key, value in floors.items()})
        changed = [field for field in current.__dataclass_fields__
                   if field != 'version' and getattr(current, field) != getattr(projected, field)]
        initialized = incoming_reason == 'INITIALIZE_HISTORICAL_RELATIONSHIP'
        applied = bool(changed) or initialized
        projected = replace(projected, version=current.version + applied)
        payload = {**incoming.payload, 'applied': applied,
                   'change_fields': changed, 'snapshot_version': projected.version,
                   'reason_code': incoming_reason,
                   'contact_qualification': incoming_qualification}
        for identity, outcome in outcomes.items():
            connection.execute('UPDATE private_world_projection_inputs SET result_json=? WHERE event_id=?',
                               (json.dumps(outcome, sort_keys=True), identity))
        return replace(incoming, payload=payload, projection_result=outcomes[incoming.event_id]), projected

    def projection_result(self, event_id: str) -> dict | None:
        """Read current derived eligibility; events() remains immutable audit."""
        with self._connection() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_world_projection_inputs'").fetchone()
            if not exists or 'result_json' not in {row[1] for row in connection.execute('PRAGMA table_info(private_world_projection_inputs)')}:
                return None
            row = connection.execute('SELECT result_json FROM private_world_projection_inputs WHERE event_id=?', (event_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def events(self) -> tuple[LedgerEvent, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event_id, delivery_id, event_type, payload_json, occurred_at
                   FROM private_world_events ORDER BY rowid"""
            ).fetchall()
            has_projection = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_world_projection_inputs'").fetchone()
            derived = {}
            if has_projection and 'result_json' in {row[1] for row in connection.execute('PRAGMA table_info(private_world_projection_inputs)')}:
                derived = {identity: json.loads(result) for identity, result in connection.execute(
                    'SELECT event_id,result_json FROM private_world_projection_inputs WHERE result_json IS NOT NULL')}
        return tuple(
            LedgerEvent(
                row[0],
                row[1],
                row[2],
                json.loads(row[3]),
                row[4],
                projection_result=derived.get(row[0]),
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
        payload = json.loads(SQLitePrivateWorldLedger._v3_snapshot_json(snapshot))
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
    def _v3_snapshot_json(snapshot: PrivateWorldSnapshot) -> str:
        payload = snapshot.to_dict()
        payload.pop("active_boundaries")
        payload.pop("acknowledged_affection")
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
        boundaries: tuple[ActiveBoundary, ...] = (),
        affection: AcknowledgedAffection | None = None,
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
            relationship_growth=tuple(tuple(row) for row in payload.get('relationship_growth', ())),
            relationship_volume=payload.get('relationship_volume', 1),
            relationship_route=payload.get('relationship_route', ''),
            relationship_days=tuple(payload.get('relationship_days', ())),
            volume_started_days=payload.get('volume_started_days', 0),
            route_asked=payload.get('route_asked', False),
            active_boundaries=boundaries,
            acknowledged_affection=affection,
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

    def _validated_v3_rows(
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
                raise LedgerWriteError("stored v3 row version is invalid")
            try:
                payload = self._payload_object(payload_json)
                if set(payload) != _V3_PAYLOAD_FIELDS:
                    raise ValueError("stored v3 fields are invalid")
                if payload["view"] != "snapshot":
                    raise ValueError("stored v3 view is invalid")
                facts = payload["continuation_facts"]
                grants = payload["intimacy_grants"]
                if not isinstance(facts, list) or not isinstance(grants, list):
                    raise ValueError("stored v3 collections are invalid")
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
                raise LedgerWriteError("stored v3 state is invalid") from exc
            if snapshot.version != stored_version:
                raise LedgerWriteError("stored v3 version does not match row")
            if self._v3_snapshot_json(snapshot) != payload_json:
                raise LedgerWriteError("stored v3 state is not canonical")
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
            if set(payload) - {'relationship_growth', 'relationship_volume', 'relationship_route', 'relationship_days',
                               'volume_started_days', 'route_asked'} != _V4_PAYLOAD_FIELDS:
                raise ValueError("stored state fields are invalid")
            if payload["view"] != "snapshot":
                raise ValueError("stored state view is invalid")
            facts = payload["continuation_facts"]
            if not isinstance(facts, list):
                raise ValueError("stored continuation facts must be a list")
            grants = payload["intimacy_grants"]
            if not isinstance(grants, list):
                raise ValueError("stored intimacy grants must be a list")
            boundaries = payload["active_boundaries"]
            if not isinstance(boundaries, list):
                raise ValueError("stored active boundaries must be a list")
            affection_payload = payload["acknowledged_affection"]
            if affection_payload is not None and not isinstance(
                affection_payload, dict
            ):
                raise ValueError("stored acknowledged affection is invalid")
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
                tuple(
                    ActiveBoundary(
                        boundary_id=item["boundary_id"],
                        set_at=item["set_at"],
                        scope=item["scope"],
                    )
                    for item in boundaries
                ),
                (
                    AcknowledgedAffection(
                        intensity=AffectionIntensity(
                            affection_payload["intensity"]
                        ),
                        statement_ref_id=affection_payload["statement_ref_id"],
                        scope=AffectionScope(affection_payload["scope"]),
                    )
                    if affection_payload is not None
                    else None
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
