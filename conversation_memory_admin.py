"""Auditable user administration over the provider-neutral memory port.

The service provides bounded list/search/add/delete/correct/clear/export
operations without reaching into Qdrant or Mem0 internals.  Its SQLite audit
contains only request identities, target/replacement IDs, statuses, reasons,
and timestamps—never memory text.  Correction is ordered as add replacement,
then delete old, so a partial provider failure cannot silently erase the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import re
import sqlite3
import threading
from typing import Callable, Mapping, TypeVar

from conversation_memory_port import (
    ConversationMemoryPort,
    ConversationMemoryRecord,
    ConversationMemoryStatus,
)


MEMORY_ADMIN_AUDIT_SCHEMA = 2
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_ERROR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_OPERATIONS = frozenset({"add", "delete", "correct", "clear", "pause", "resume"})
_AUDIT_STATUSES = frozenset(
    {"completed", "noop", "replacement_written_delete_pending"}
)
_LIFECYCLE_LOCKS: dict[str, threading.RLock] = {}
_LIFECYCLE_LOCKS_GUARD = threading.Lock()
_T = TypeVar("_T")


class ConversationMemoryAdminError(RuntimeError):
    def __init__(self, code: str) -> None:
        if not _ERROR_RE.fullmatch(code):
            raise ValueError("memory admin error code is invalid")
        self.code = code
        super().__init__(code)


class MemoryAdminMutationStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    NOOP = "noop"


@dataclass(frozen=True)
class MemoryAdminMutationResult:
    status: MemoryAdminMutationStatus
    request_id: str
    operation: str
    affected_count: int = 0
    target_memory_id: str | None = None
    replacement_memory_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MemoryAdminMutationStatus):
            raise ValueError("memory admin result status is invalid")
        _identifier(self.request_id, field_name="request_id")
        if self.operation not in _OPERATIONS:
            raise ValueError("memory admin operation is invalid")
        if type(self.affected_count) is not int or self.affected_count < 0:
            raise ValueError("affected_count is invalid")
        for value, field_name in (
            (self.target_memory_id, "target_memory_id"),
            (self.replacement_memory_id, "replacement_memory_id"),
        ):
            if value is not None:
                _identifier(value, field_name=field_name)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status.value,
            "request_id": self.request_id,
            "operation": self.operation,
            "affected_count": self.affected_count,
        }
        if self.target_memory_id is not None:
            payload["target_memory_id"] = self.target_memory_id
        if self.replacement_memory_id is not None:
            payload["replacement_memory_id"] = self.replacement_memory_id
        return payload


@dataclass(frozen=True)
class MemoryAdminStatus:
    status: str
    provider: str
    enabled: bool
    memory_count: int | None
    audit_count: int
    pending_correction_count: int
    reason_code: str | None = None
    paused: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"available", "degraded", "unavailable", "disabled"}:
            raise ValueError("memory admin status is invalid")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("memory admin provider is invalid")
        if type(self.enabled) is not bool:
            raise ValueError("memory admin enabled flag is invalid")
        if type(self.paused) is not bool:
            raise ValueError("memory admin paused flag is invalid")
        if self.memory_count is not None and (
            type(self.memory_count) is not int or self.memory_count < 0
        ):
            raise ValueError("memory_count is invalid")
        for name in ("audit_count", "pending_correction_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} is invalid")
        if self.reason_code is not None and not _ERROR_RE.fullmatch(self.reason_code):
            raise ValueError("memory admin reason code is invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "provider": self.provider,
            "enabled": self.enabled,
            "audit_count": self.audit_count,
            "pending_correction_count": self.pending_correction_count,
        }
        if self.memory_count is not None:
            payload["memory_count"] = self.memory_count
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        if self.paused:
            payload["paused"] = True
        return payload


class ConversationMemoryAdminService:
    """Serialize user mutations and preserve recoverable correction intent."""

    def __init__(
        self,
        memory: ConversationMemoryPort,
        audit_path: Path,
        *,
        user_id: str = "local-user",
    ) -> None:
        path = Path(audit_path)
        if str(path) in {"", "."} or path.exists() and path.is_dir():
            raise ValueError("an explicit memory admin audit file is required")
        _identifier(user_id, field_name="user_id")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.memory = memory
        self.audit_path = path
        self.user_id = user_id
        self._lock = threading.RLock()
        self._lifecycle_lock = _lifecycle_lock(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.audit_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, 1, MEMORY_ADMIN_AUDIT_SCHEMA}:
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_SCHEMA_UNSUPPORTED"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memory_admin_operations (
                        request_id TEXT PRIMARY KEY,
                        operation TEXT NOT NULL CHECK (
                            operation IN ('add', 'delete', 'correct', 'clear')
                        ),
                        target_memory_id TEXT,
                        replacement_memory_id TEXT,
                        replacement_source_id TEXT,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'completed',
                                'noop',
                                'replacement_written_delete_pending'
                            )
                        ),
                        affected_count INTEGER NOT NULL CHECK (affected_count >= 0),
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS memory_admin_pause_windows (
                        pause_request_id TEXT PRIMARY KEY,
                        resume_request_id TEXT UNIQUE,
                        started_at TEXT NOT NULL,
                        resumed_at TEXT
                    );
                    """
                )
                connection.execute(
                    f"PRAGMA user_version={MEMORY_ADMIN_AUDIT_SCHEMA}"
                )
        except ConversationMemoryAdminError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_INITIALIZATION_FAILED"
            ) from exc

    def list_memories(
        self,
        *,
        query: str | None = None,
        limit: int = 100,
    ) -> tuple[ConversationMemoryRecord, ...]:
        self._require_available()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ConversationMemoryAdminError("MEMORY_ADMIN_LIMIT_INVALID")
        try:
            if query is None or not query.strip():
                return self.memory.list_memories(
                    user_id=self.user_id,
                    limit=limit,
                )
            if not isinstance(query, str) or len(query) > 2000:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_QUERY_INVALID")
            return self.memory.search_context(
                query.strip(),
                user_id=self.user_id,
                limit=min(limit, 100),
            )
        except ConversationMemoryAdminError:
            raise
        except Exception as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_READ_FAILED"
            ) from exc

    def add(
        self,
        text: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        request_id = _identifier(request_id, field_name="request_id")
        text = _text(text, field_name="memory text", maximum=2000)
        reason = _text(reason, field_name="reason", maximum=500)
        source_id = f"manual:{request_id}"
        with self._lifecycle_lock, self._lock:
            existing = self._existing_result(request_id, "add")
            if existing is not None:
                return existing
            record = self._record_by_source(source_id)
            if record is None:
                try:
                    record = self.memory.add_manual_memory(
                        text,
                        user_id=self.user_id,
                        source_id=source_id,
                    )
                except Exception as exc:
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_ADD_FAILED"
                    ) from exc
            self._write_audit(
                request_id=request_id,
                operation="add",
                status="completed",
                reason=reason,
                replacement_memory_id=record.memory_id,
                replacement_source_id=source_id,
                affected_count=1,
            )
            return MemoryAdminMutationResult(
                MemoryAdminMutationStatus.APPLIED,
                request_id,
                "add",
                affected_count=1,
                replacement_memory_id=record.memory_id,
            )

    def delete(
        self,
        memory_id: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        memory_id = _identifier(memory_id, field_name="memory_id")
        request_id = _identifier(request_id, field_name="request_id")
        reason = _text(reason, field_name="reason", maximum=500)
        with self._lifecycle_lock, self._lock:
            existing = self._existing_result(request_id, "delete")
            if existing is not None:
                return existing
            if self._record_by_id(memory_id) is None:
                self._write_audit(
                    request_id=request_id,
                    operation="delete",
                    status="noop",
                    reason=reason,
                    target_memory_id=memory_id,
                    affected_count=0,
                )
                return MemoryAdminMutationResult(
                    MemoryAdminMutationStatus.NOOP,
                    request_id,
                    "delete",
                    target_memory_id=memory_id,
                )
            try:
                deleted = self.memory.delete_memory(
                    memory_id,
                    user_id=self.user_id,
                )
            except Exception as exc:
                raise ConversationMemoryAdminError(
                    "MEMORY_ADMIN_DELETE_FAILED"
                ) from exc
            if not deleted and self._record_by_id(memory_id) is not None:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_DELETE_FAILED")
            self._write_audit(
                request_id=request_id,
                operation="delete",
                status="completed",
                reason=reason,
                target_memory_id=memory_id,
                affected_count=1,
            )
            return MemoryAdminMutationResult(
                MemoryAdminMutationStatus.APPLIED,
                request_id,
                "delete",
                affected_count=1,
                target_memory_id=memory_id,
            )

    def correct(
        self,
        memory_id: str,
        corrected_text: str,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        memory_id = _identifier(memory_id, field_name="memory_id")
        request_id = _identifier(request_id, field_name="request_id")
        corrected_text = _text(
            corrected_text,
            field_name="corrected memory text",
            maximum=2000,
        )
        reason = _text(reason, field_name="reason", maximum=500)
        source_id = f"correction:{request_id}"
        with self._lifecycle_lock, self._lock:
            existing = self._audit_row(request_id)
            if existing is not None:
                if str(existing["operation"]) != "correct":
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_REQUEST_CONFLICT"
                    )
                if str(existing["status"]) in {"completed", "noop"}:
                    return self._result_from_row(existing, duplicate=True)
                replacement = self._record_by_source(
                    str(existing["replacement_source_id"] or source_id)
                )
            else:
                replacement = self._record_by_source(source_id)

            original = self._record_by_id(memory_id)
            if replacement is None:
                if original is None:
                    self._write_audit(
                        request_id=request_id,
                        operation="correct",
                        status="noop",
                        reason=reason,
                        target_memory_id=memory_id,
                        replacement_source_id=source_id,
                        affected_count=0,
                    )
                    return MemoryAdminMutationResult(
                        MemoryAdminMutationStatus.NOOP,
                        request_id,
                        "correct",
                        target_memory_id=memory_id,
                    )
                try:
                    replacement = self.memory.add_manual_memory(
                        corrected_text,
                        user_id=self.user_id,
                        source_id=source_id,
                    )
                except Exception as exc:
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_CORRECTION_WRITE_FAILED"
                    ) from exc
                self._write_audit(
                    request_id=request_id,
                    operation="correct",
                    status="replacement_written_delete_pending",
                    reason=reason,
                    target_memory_id=memory_id,
                    replacement_memory_id=replacement.memory_id,
                    replacement_source_id=source_id,
                    affected_count=1,
                )

            if original is not None:
                try:
                    deleted = self.memory.delete_memory(
                        memory_id,
                        user_id=self.user_id,
                    )
                except Exception as exc:
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_CORRECTION_DELETE_FAILED"
                    ) from exc
                if not deleted and self._record_by_id(memory_id) is not None:
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_CORRECTION_DELETE_FAILED"
                    )

            self._write_audit(
                request_id=request_id,
                operation="correct",
                status="completed",
                reason=reason,
                target_memory_id=memory_id,
                replacement_memory_id=replacement.memory_id,
                replacement_source_id=source_id,
                affected_count=2 if original is not None else 1,
            )
            return MemoryAdminMutationResult(
                MemoryAdminMutationStatus.APPLIED,
                request_id,
                "correct",
                affected_count=2 if original is not None else 1,
                target_memory_id=memory_id,
                replacement_memory_id=replacement.memory_id,
            )

    def clear(
        self,
        *,
        request_id: str,
        reason: str,
        confirmed: bool,
    ) -> MemoryAdminMutationResult:
        request_id = _identifier(request_id, field_name="request_id")
        reason = _text(reason, field_name="reason", maximum=500)
        if confirmed is not True:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_CONFIRMATION_REQUIRED"
            )
        with self._lock:
            existing = self._existing_result(request_id, "clear")
            if existing is not None:
                return existing
            records = self._records(allow_paused=True)
            if not records:
                self._write_audit(
                    request_id=request_id,
                    operation="clear",
                    status="noop",
                    reason=reason,
                    affected_count=0,
                )
                return MemoryAdminMutationResult(
                    MemoryAdminMutationStatus.NOOP,
                    request_id,
                    "clear",
                )
            try:
                count = self.memory.clear_user(user_id=self.user_id)
            except Exception as exc:
                raise ConversationMemoryAdminError(
                    "MEMORY_ADMIN_CLEAR_FAILED"
                ) from exc
            if count <= 0 and self._records(allow_paused=True):
                raise ConversationMemoryAdminError("MEMORY_ADMIN_CLEAR_FAILED")
            affected = max(count, len(records))
            self._write_audit(
                request_id=request_id,
                operation="clear",
                status="completed",
                reason=reason,
                affected_count=affected,
            )
            return MemoryAdminMutationResult(
                MemoryAdminMutationStatus.APPLIED,
                request_id,
                "clear",
                affected_count=affected,
            )

    def pause(
        self,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        request_id = _identifier(request_id, field_name="request_id")
        _text(reason, field_name="reason", maximum=500)
        with self._lifecycle_lock, self._lock:
            try:
                with self._connect() as connection:
                    existing = connection.execute(
                        "SELECT 1 FROM memory_admin_pause_windows WHERE pause_request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if existing is not None:
                        return MemoryAdminMutationResult(
                            MemoryAdminMutationStatus.DUPLICATE, request_id, "pause"
                        )
                    active = connection.execute(
                        "SELECT 1 FROM memory_admin_pause_windows WHERE resumed_at IS NULL"
                    ).fetchone()
                    if active is not None:
                        return MemoryAdminMutationResult(
                            MemoryAdminMutationStatus.NOOP, request_id, "pause"
                        )
                    connection.execute(
                        "INSERT INTO memory_admin_pause_windows (pause_request_id, started_at) VALUES (?, ?)",
                        (request_id, datetime.now(timezone.utc).isoformat()),
                    )
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc
        return MemoryAdminMutationResult(MemoryAdminMutationStatus.APPLIED, request_id, "pause")

    def resume(
        self,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        request_id = _identifier(request_id, field_name="request_id")
        _text(reason, field_name="reason", maximum=500)
        with self._lifecycle_lock, self._lock:
            try:
                with self._connect() as connection:
                    existing = connection.execute(
                        "SELECT 1 FROM memory_admin_pause_windows WHERE resume_request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if existing is not None:
                        return MemoryAdminMutationResult(
                            MemoryAdminMutationStatus.DUPLICATE, request_id, "resume"
                        )
                    active = connection.execute(
                        "SELECT pause_request_id FROM memory_admin_pause_windows "
                        "WHERE resumed_at IS NULL ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                    if active is None:
                        return MemoryAdminMutationResult(
                            MemoryAdminMutationStatus.NOOP, request_id, "resume"
                        )
                    connection.execute(
                        "UPDATE memory_admin_pause_windows SET resume_request_id = ?, resumed_at = ? "
                        "WHERE pause_request_id = ?",
                        (request_id, datetime.now(timezone.utc).isoformat(), str(active[0])),
                    )
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc
        return MemoryAdminMutationResult(MemoryAdminMutationStatus.APPLIED, request_id, "resume")

    def is_paused(self) -> bool:
        with self._lifecycle_lock:
            return self._is_paused()

    def _is_paused(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute(
                    "SELECT 1 FROM memory_admin_pause_windows WHERE resumed_at IS NULL"
                ).fetchone() is not None
        except (OSError, sqlite3.Error) as exc:
            raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc

    def blocks_delivery(self, occurred_at: datetime) -> bool:
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            raise ConversationMemoryAdminError("MEMORY_ADMIN_DELIVERY_TIME_INVALID")
        timestamp = occurred_at.astimezone(timezone.utc).isoformat()
        with self._lifecycle_lock:
            if self._is_paused():
                return True
            try:
                with self._connect() as connection:
                    return connection.execute(
                        "SELECT 1 FROM memory_admin_pause_windows "
                        "WHERE started_at <= ? AND (resumed_at IS NULL OR resumed_at >= ?) LIMIT 1",
                        (timestamp, timestamp),
                    ).fetchone() is not None
            except (OSError, sqlite3.Error) as exc:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc

    def run_write(self, operation: Callable[[], _T]) -> _T | None:
        """Serialize the final pause check and provider write for this audit file."""
        with self._lifecycle_lock:
            if self._is_paused():
                return None
            return operation()

    def export(self) -> dict[str, object]:
        self._require_available()
        try:
            return self.memory.export_user(user_id=self.user_id)
        except Exception as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_EXPORT_FAILED"
            ) from exc

    def status(self) -> MemoryAdminStatus:
        provider = self._provider_status()
        try:
            with self._connect() as connection:
                audit_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_admin_operations"
                    ).fetchone()[0]
                )
                pending_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_admin_operations "
                        "WHERE status = 'replacement_written_delete_pending'"
                    ).fetchone()[0]
                )
        except (OSError, sqlite3.Error, ValueError):
            return MemoryAdminStatus(
                "unavailable",
                provider.provider,
                False,
                provider.memory_count,
                0,
                0,
                reason_code="MEMORY_ADMIN_AUDIT_UNAVAILABLE",
            )
        paused = self.is_paused()
        return MemoryAdminStatus(
            "degraded" if paused else provider.status,
            provider.provider,
            provider.enabled,
            provider.memory_count,
            audit_count,
            pending_count,
            reason_code="MEMORY_ADMIN_PAUSED" if paused else provider.reason_code,
            paused=paused,
        )

    def _require_available(self, *, allow_paused: bool = False) -> None:
        status = self._provider_status()
        if status.status == "disabled":
            raise ConversationMemoryAdminError("MEMORY_ADMIN_DISABLED")
        if status.status == "unavailable":
            raise ConversationMemoryAdminError("MEMORY_ADMIN_UNAVAILABLE")
        if not allow_paused and self.is_paused():
            raise ConversationMemoryAdminError("MEMORY_ADMIN_PAUSED")

    def _provider_status(self) -> ConversationMemoryStatus:
        try:
            status = self.memory.status()
        except Exception as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_UNAVAILABLE"
            ) from exc
        if not isinstance(status, ConversationMemoryStatus):
            raise ConversationMemoryAdminError("MEMORY_ADMIN_UNAVAILABLE")
        return status

    def _records(
        self, *, allow_paused: bool = False
    ) -> tuple[ConversationMemoryRecord, ...]:
        self._require_available(allow_paused=allow_paused)
        try:
            return self.memory.list_memories(
                user_id=self.user_id,
                limit=1000,
            )
        except Exception as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_READ_FAILED"
            ) from exc

    def _record_by_id(self, memory_id: str) -> ConversationMemoryRecord | None:
        return next(
            (record for record in self._records() if record.memory_id == memory_id),
            None,
        )

    def _record_by_source(self, source_id: str) -> ConversationMemoryRecord | None:
        return next(
            (record for record in self._records() if record.source_id == source_id),
            None,
        )

    def _audit_row(self, request_id: str) -> sqlite3.Row | None:
        try:
            with self._connect() as connection:
                return connection.execute(
                    "SELECT * FROM memory_admin_operations WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
            ) from exc

    def _existing_result(
        self,
        request_id: str,
        operation: str,
    ) -> MemoryAdminMutationResult | None:
        row = self._audit_row(request_id)
        if row is None:
            return None
        if str(row["operation"]) != operation:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_REQUEST_CONFLICT"
            )
        return self._result_from_row(row, duplicate=True)

    @staticmethod
    def _result_from_row(
        row: sqlite3.Row,
        *,
        duplicate: bool,
    ) -> MemoryAdminMutationResult:
        status = (
            MemoryAdminMutationStatus.DUPLICATE
            if duplicate
            else (
                MemoryAdminMutationStatus.NOOP
                if str(row["status"]) == "noop"
                else MemoryAdminMutationStatus.APPLIED
            )
        )
        return MemoryAdminMutationResult(
            status,
            str(row["request_id"]),
            str(row["operation"]),
            affected_count=int(row["affected_count"]),
            target_memory_id=(
                str(row["target_memory_id"])
                if row["target_memory_id"] is not None
                else None
            ),
            replacement_memory_id=(
                str(row["replacement_memory_id"])
                if row["replacement_memory_id"] is not None
                else None
            ),
        )

    def _write_audit(
        self,
        *,
        request_id: str,
        operation: str,
        status: str,
        reason: str,
        affected_count: int,
        target_memory_id: str | None = None,
        replacement_memory_id: str | None = None,
        replacement_source_id: str | None = None,
    ) -> None:
        if operation not in _OPERATIONS or status not in _AUDIT_STATUSES:
            raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_INVALID")
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT operation, target_memory_id, replacement_source_id "
                    "FROM memory_admin_operations WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if existing is not None and (
                    str(existing["operation"]) != operation
                    or (existing["target_memory_id"] or None) != target_memory_id
                    or (existing["replacement_source_id"] or None)
                    != replacement_source_id
                ):
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_REQUEST_CONFLICT"
                    )
                connection.execute(
                    """
                    INSERT INTO memory_admin_operations (
                        request_id, operation, target_memory_id,
                        replacement_memory_id, replacement_source_id,
                        status, affected_count, reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(request_id) DO UPDATE SET
                        replacement_memory_id = excluded.replacement_memory_id,
                        status = excluded.status,
                        affected_count = excluded.affected_count,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (
                        request_id,
                        operation,
                        target_memory_id,
                        replacement_memory_id,
                        replacement_source_id,
                        status,
                        affected_count,
                        reason,
                        timestamp,
                        timestamp,
                    ),
                )
        except ConversationMemoryAdminError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
            ) from exc


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ConversationMemoryAdminError("MEMORY_ADMIN_IDENTIFIER_INVALID")
    return value


def _text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConversationMemoryAdminError("MEMORY_ADMIN_TEXT_INVALID")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ConversationMemoryAdminError("MEMORY_ADMIN_TEXT_INVALID")
    return normalized


def _lifecycle_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _LIFECYCLE_LOCKS_GUARD:
        return _LIFECYCLE_LOCKS.setdefault(key, threading.RLock())


__all__ = [
    "ConversationMemoryAdminError",
    "ConversationMemoryAdminService",
    "MEMORY_ADMIN_AUDIT_SCHEMA",
    "MemoryAdminMutationResult",
    "MemoryAdminMutationStatus",
    "MemoryAdminStatus",
]
