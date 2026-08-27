"""Durable, content-free outbox for canonical conversation-memory delivery.

The current letter store is already written atomically before optional stages
run.  This outbox scans only persisted COMPLETED revisions, delegates the exact
user/assistant exchange to :mod:`runtime.memory.conversation_memory_delivery`, and records
only source identity, terminal status, attempts, and stable error codes in its
own SQLite journal.  No message text is copied into the journal.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Protocol, Sequence, runtime_checkable

from runtime.memory.conversation_memory_delivery import (
    CanonicalMemoryDelivery,
    CanonicalMemoryDeliveryError,
    CanonicalMemoryDeliveryResult,
    CanonicalMemoryDeliveryStatus,
)


OUTBOX_SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_ERROR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_TERMINAL = frozenset(
    {
        CanonicalMemoryDeliveryStatus.WRITTEN.value,
        CanonicalMemoryDeliveryStatus.DUPLICATE.value,
    }
)


class ConversationMemoryOutboxError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OutboxScanResult:
    status: str
    discovered: int = 0
    delivered: int = 0
    duplicates: int = 0
    pending: int = 0
    ignored: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"available", "degraded", "unavailable"}:
            raise ValueError("outbox scan status is invalid")
        for name in ("discovered", "delivered", "duplicates", "pending", "ignored"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.error_code is not None and not _ERROR_RE.fullmatch(self.error_code):
            raise ValueError("outbox error code is invalid")
        if self.status == "unavailable" and self.error_code is None:
            raise ValueError("unavailable outbox result requires an error code")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "discovered": self.discovered,
            "delivered": self.delivered,
            "duplicates": self.duplicates,
            "pending": self.pending,
            "ignored": self.ignored,
        }
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        return payload


@runtime_checkable
class CanonicalMemoryCommitter(Protocol):
    async def commit(
        self,
        delivery: CanonicalMemoryDelivery,
    ) -> CanonicalMemoryDeliveryResult: ...


class CanonicalMemoryOutbox:
    """Recoverable exactly-once coordinator over persisted letter revisions."""

    def __init__(
        self,
        state_path: Path,
        journal_path: Path,
        committer: CanonicalMemoryCommitter,
        *,
        user_id: str = "local-user",
    ) -> None:
        state = Path(state_path)
        journal = Path(journal_path)
        if str(state) in {"", "."} or state.exists() and state.is_dir():
            raise ValueError("an explicit letter state file is required")
        if str(journal) in {"", "."} or journal.exists() and journal.is_dir():
            raise ValueError("an explicit outbox journal file is required")
        if not isinstance(committer, CanonicalMemoryCommitter):
            raise TypeError("a canonical memory committer is required")
        if not isinstance(user_id, str) or not _ID_RE.fullmatch(user_id):
            raise ValueError("outbox user_id is invalid")
        journal.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = state
        self.journal_path = journal
        self.committer = committer
        self.user_id = user_id
        self._scan_lock = asyncio.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.journal_path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, OUTBOX_SCHEMA_VERSION}:
                    raise ConversationMemoryOutboxError(
                        "MEMORY_OUTBOX_SCHEMA_UNSUPPORTED"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS canonical_memory_deliveries (
                        source_id TEXT PRIMARY KEY,
                        letter_id TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        status TEXT NOT NULL CHECK (
                            status IN ('written', 'duplicate', 'pending', 'unavailable')
                        ),
                        attempts INTEGER NOT NULL CHECK (attempts >= 0),
                        last_error_code TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_canonical_memory_letter_revision
                        ON canonical_memory_deliveries(letter_id, revision);
                    """
                )
                connection.execute(f"PRAGMA user_version={OUTBOX_SCHEMA_VERSION}")
        except ConversationMemoryOutboxError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ConversationMemoryOutboxError(
                "MEMORY_OUTBOX_INITIALIZATION_FAILED"
            ) from exc

    async def scan_once(self) -> OutboxScanResult:
        async with self._scan_lock:
            try:
                letters = self._read_letters()
            except ConversationMemoryOutboxError as exc:
                return OutboxScanResult("unavailable", error_code=exc.code)

            discovered = delivered = duplicates = pending = ignored = 0
            for row in letters:
                delivery = _delivery_from_row(row, user_id=self.user_id)
                if delivery is None:
                    ignored += 1
                    continue
                discovered += 1
                if self._is_terminal(delivery.source_id):
                    duplicates += 1
                    continue
                result = await self.committer.commit(delivery)
                if result.status is CanonicalMemoryDeliveryStatus.WRITTEN:
                    delivered += 1
                    self._record(delivery, result)
                elif result.status is CanonicalMemoryDeliveryStatus.DUPLICATE:
                    duplicates += 1
                    self._record(delivery, result)
                elif result.status is CanonicalMemoryDeliveryStatus.SKIPPED:
                    ignored += 1
                    self._record(delivery, result)
                else:
                    pending += 1
                    self._record(delivery, result)

            status = "degraded" if pending else "available"
            return OutboxScanResult(
                status,
                discovered=discovered,
                delivered=delivered,
                duplicates=duplicates,
                pending=pending,
                ignored=ignored,
            )

    def health(self) -> dict[str, object]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT status, COUNT(*) FROM canonical_memory_deliveries "
                    "GROUP BY status"
                ).fetchall()
                attempts = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(attempts), 0) "
                        "FROM canonical_memory_deliveries"
                    ).fetchone()[0]
                )
        except (OSError, sqlite3.Error, ValueError):
            return {
                "status": "unavailable",
                "provider": "sqlite-outbox",
                "reason_code": "MEMORY_OUTBOX_STORAGE_UNAVAILABLE",
            }
        counts = {str(status): int(count) for status, count in rows}
        pending = counts.get("pending", 0) + counts.get("unavailable", 0)
        return {
            "status": "degraded" if pending else "available",
            "provider": "sqlite-outbox",
            "schema_version": OUTBOX_SCHEMA_VERSION,
            "terminal_count": counts.get("written", 0) + counts.get("duplicate", 0),
            "pending_count": pending,
            "attempt_count": attempts,
        }

    def _read_letters(self) -> tuple[Mapping[str, object], ...]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConversationMemoryOutboxError(
                "MEMORY_OUTBOX_STATE_UNAVAILABLE"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ConversationMemoryOutboxError("MEMORY_OUTBOX_STATE_INVALID")
        letters = payload.get("letters", ())
        if not isinstance(letters, Sequence) or isinstance(letters, (str, bytes)):
            raise ConversationMemoryOutboxError("MEMORY_OUTBOX_STATE_INVALID")
        return tuple(row for row in letters if isinstance(row, Mapping))

    def _is_terminal(self, source_id: str) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT status FROM canonical_memory_deliveries "
                    "WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise ConversationMemoryOutboxError(
                "MEMORY_OUTBOX_STORAGE_UNAVAILABLE"
            ) from exc
        return row is not None and str(row[0]) in _TERMINAL

    def _record(
        self,
        delivery: CanonicalMemoryDelivery,
        result: CanonicalMemoryDeliveryResult,
    ) -> None:
        status = result.status.value
        if status == CanonicalMemoryDeliveryStatus.SKIPPED.value:
            # A successful no-op extraction is terminal.  The v1 journal's
            # existing duplicate state is the content-free terminal marker.
            status = CanonicalMemoryDeliveryStatus.DUPLICATE.value
        error_code = result.error_code
        if status == "pending" and error_code is None:
            error_code = "MEMORY_PROVIDER_DISABLED"
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO canonical_memory_deliveries (
                        source_id, letter_id, revision, status,
                        attempts, last_error_code, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        status = excluded.status,
                        attempts = canonical_memory_deliveries.attempts + 1,
                        last_error_code = excluded.last_error_code,
                        updated_at = excluded.updated_at
                    """,
                    (
                        delivery.source_id,
                        delivery.letter_id,
                        delivery.revision,
                        status,
                        error_code,
                        timestamp,
                    ),
                )
        except (OSError, sqlite3.Error) as exc:
            raise ConversationMemoryOutboxError(
                "MEMORY_OUTBOX_STORAGE_UNAVAILABLE"
            ) from exc


async def run_outbox_forever(
    outbox: CanonicalMemoryOutbox,
    *,
    interval_seconds: float = 5.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll atomic state snapshots; cancellation never changes canonical text."""

    if not isinstance(outbox, CanonicalMemoryOutbox):
        raise TypeError("a canonical memory outbox is required")
    if not 0.25 <= interval_seconds <= 3600:
        raise ValueError("outbox interval is invalid")
    stopper = stop_event or asyncio.Event()
    while not stopper.is_set():
        try:
            await outbox.scan_once()
        except (ConversationMemoryOutboxError, OSError, sqlite3.Error):
            pass
        try:
            await asyncio.wait_for(stopper.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


def _delivery_from_row(
    row: Mapping[str, object],
    *,
    user_id: str,
) -> CanonicalMemoryDelivery | None:
    if str(row.get("letter_status", "")).upper() != "COMPLETED":
        return None
    letter_id = row.get("letter_id")
    revision = row.get("reply_revision", 1)
    user_message = row.get("content")
    assistant_message = row.get("reply_text")
    occurred_at = _row_time(row)
    try:
        return CanonicalMemoryDelivery(
            letter_id=letter_id,  # type: ignore[arg-type]
            revision=revision,  # type: ignore[arg-type]
            user_message=user_message,  # type: ignore[arg-type]
            assistant_message=assistant_message,  # type: ignore[arg-type]
            occurred_at=occurred_at,
            user_id=user_id,
        )
    except (CanonicalMemoryDeliveryError, TypeError, ValueError):
        return None


def _row_time(row: Mapping[str, object]) -> datetime:
    for key in (
        "private_world_occurred_at",
        "replied_at",
        "created_at",
    ):
        parsed = _timestamp(row.get(key))
        if parsed is not None:
            return parsed
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed
    return None


__all__ = [
    "CanonicalMemoryCommitter",
    "CanonicalMemoryOutbox",
    "ConversationMemoryOutboxError",
    "OUTBOX_SCHEMA_VERSION",
    "OutboxScanResult",
    "run_outbox_forever",
]
