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
import hashlib
import json
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
from conversation_memory_identity import (
    ConversationMemoryIdentityError,
    normalize_conversation_memory_user_id,
)


MEMORY_ADMIN_AUDIT_SCHEMA = 7
_DEFAULT_USER_ID = "local-user"
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_ERROR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_OPERATIONS = frozenset({"add", "delete", "correct", "clear", "pause", "resume"})
_AUDIT_STATUSES = frozenset(
    {
        "completed",
        "noop",
        "pending_clear",
        "replacement_written_delete_pending",
    }
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
        user_id: str = _DEFAULT_USER_ID,
    ) -> None:
        path = Path(audit_path)
        if str(path) in {"", "."} or path.exists() and path.is_dir():
            raise ValueError("an explicit memory admin audit file is required")
        user_id = _normalized_user_id(user_id)
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
                if version not in {0, 1, 4, 5, 6, MEMORY_ADMIN_AUDIT_SCHEMA}:
                    raise ConversationMemoryAdminError(
                        "MEMORY_ADMIN_SCHEMA_UNSUPPORTED"
                    )
                if version in {1, 4, 5, 6}:
                    self._upgrade_operations_schema(connection)
                else:
                    self._create_schema(connection)
                    if version == 0:
                        connection.execute(
                            f"PRAGMA user_version={MEMORY_ADMIN_AUDIT_SCHEMA}"
                        )
        except ConversationMemoryAdminError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_INITIALIZATION_FAILED"
            ) from exc

    @staticmethod
    def _upgrade_operations_schema(connection: sqlite3.Connection) -> None:
        """Add scoped request fingerprints and a durable pending-clear intent."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(memory_admin_operations)"
                )
            }
            connection.execute(
                "ALTER TABLE memory_admin_operations "
                "RENAME TO memory_admin_operations_legacy"
            )
            ConversationMemoryAdminService._create_schema(connection)
            user_id = "user_id" if "user_id" in columns else "?"
            fingerprint = (
                "payload_fingerprint"
                if "payload_fingerprint" in columns
                else "'legacy'"
            )
            target_ids = (
                "target_memory_ids" if "target_memory_ids" in columns else "NULL"
            )
            connection.execute(
                f"""
                INSERT INTO memory_admin_operations (
                    user_id, request_id, operation, payload_fingerprint,
                    target_memory_id, target_memory_ids, replacement_memory_id,
                    replacement_source_id, status, affected_count, reason,
                    created_at, updated_at
                ) SELECT
                    {user_id}, request_id, operation, {fingerprint},
                    target_memory_id, {target_ids}, replacement_memory_id,
                    replacement_source_id, status, affected_count, reason,
                    created_at, updated_at
                FROM memory_admin_operations_legacy
                """,
                () if "user_id" in columns else (_DEFAULT_USER_ID,),
            )
            connection.execute("DROP TABLE memory_admin_operations_legacy")
            connection.execute(f"PRAGMA user_version={MEMORY_ADMIN_AUDIT_SCHEMA}")
            connection.execute("COMMIT")
        except sqlite3.Error:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_admin_operations (
                user_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                operation TEXT NOT NULL CHECK (
                    operation IN ('add', 'delete', 'correct', 'clear', 'pause', 'resume')
                ),
                payload_fingerprint TEXT NOT NULL,
                target_memory_id TEXT,
                target_memory_ids TEXT,
                replacement_memory_id TEXT,
                replacement_source_id TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'completed',
                        'noop',
                        'pending_clear',
                        'replacement_written_delete_pending'
                    )
                ),
                affected_count INTEGER NOT NULL CHECK (affected_count >= 0),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, request_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_admin_pause_windows (
                user_id TEXT NOT NULL,
                pause_request_id TEXT NOT NULL,
                resume_request_id TEXT,
                started_at TEXT NOT NULL,
                resumed_at TEXT,
                PRIMARY KEY (user_id, pause_request_id),
                UNIQUE (user_id, resume_request_id)
            )
            """
        )

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
        fingerprint = _payload_fingerprint("add", {"text": text, "reason": reason})
        source_id = f"manual:{request_id}"
        with self._lifecycle_lock, self._lock:
            existing = self._existing_result(request_id, "add", fingerprint)
            if existing is not None:
                return existing
            self._require_no_pending_clear()
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
                payload_fingerprint=fingerprint,
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
        fingerprint = _payload_fingerprint(
            "delete", {"memory_id": memory_id, "reason": reason}
        )
        with self._lifecycle_lock, self._lock:
            existing = self._existing_result(request_id, "delete", fingerprint)
            if existing is not None:
                return existing
            self._require_no_pending_clear()
            if self._record_by_id(memory_id) is None:
                self._write_audit(
                    request_id=request_id,
                    operation="delete",
                    payload_fingerprint=fingerprint,
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
                payload_fingerprint=fingerprint,
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
        fingerprint = _payload_fingerprint(
            "correct",
            {
                "memory_id": memory_id,
                "corrected_text": corrected_text,
                "reason": reason,
            },
        )
        source_id = f"correction:{request_id}"
        with self._lifecycle_lock, self._lock:
            existing = self._audit_row(request_id)
            if existing is not None:
                self._assert_request_matches(existing, "correct", fingerprint)
                if str(existing["status"]) in {"completed", "noop"}:
                    return self._result_from_row(existing, duplicate=True)
                replacement = self._record_by_source(
                    str(existing["replacement_source_id"] or source_id)
                )
            else:
                replacement = self._record_by_source(source_id)

            self._require_no_pending_clear()

            original = self._record_by_id(memory_id)
            if replacement is None:
                if original is None:
                    self._write_audit(
                        request_id=request_id,
                        operation="correct",
                        payload_fingerprint=fingerprint,
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
                    payload_fingerprint=fingerprint,
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
                payload_fingerprint=fingerprint,
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
        fingerprint = _payload_fingerprint("clear", {"reason": reason})
        with self._lifecycle_lock, self._lock:
            existing = self._audit_row(request_id)
            if existing is not None:
                self._assert_request_matches(existing, "clear", fingerprint)
                if str(existing["status"]) != "pending_clear":
                    return self._result_from_row(existing, duplicate=True)
                return self._complete_pending_clear(existing)
            records = self._records_for_clear()
            if not records:
                self._write_audit(
                    request_id=request_id,
                    operation="clear",
                    payload_fingerprint=fingerprint,
                    status="noop",
                    reason=reason,
                    affected_count=0,
                )
                return MemoryAdminMutationResult(
                    MemoryAdminMutationStatus.NOOP,
                    request_id,
                    "clear",
                )
            memory_ids = tuple(record.memory_id for record in records)
            self._write_audit(
                request_id=request_id,
                operation="clear",
                payload_fingerprint=fingerprint,
                status="pending_clear",
                reason=reason,
                affected_count=0,
                target_memory_ids=memory_ids,
            )
            pending = self._audit_row(request_id)
            if pending is None:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE")
            return self._complete_pending_clear(pending)

    def _complete_pending_clear(
        self,
        row: sqlite3.Row,
    ) -> MemoryAdminMutationResult:
        """Finish only the IDs durably captured before this clear started."""
        request_id = str(row["request_id"])
        reason = str(row["reason"])
        fingerprint = str(row["payload_fingerprint"])
        memory_ids = _target_memory_ids(row["target_memory_ids"])
        affected = int(row["affected_count"])
        current = {record.memory_id for record in self._records_for_clear()}
        for memory_id in memory_ids:
            if memory_id not in current:
                continue
            try:
                deleted = self.memory.delete_memory(memory_id, user_id=self.user_id)
            except Exception as exc:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_CLEAR_FAILED") from exc
            if not deleted:
                current = {record.memory_id for record in self._records_for_clear()}
                if memory_id in current:
                    raise ConversationMemoryAdminError("MEMORY_ADMIN_CLEAR_FAILED")
            else:
                affected += 1
                self._write_audit(
                    request_id=request_id,
                    operation="clear",
                    payload_fingerprint=fingerprint,
                    status="pending_clear",
                    reason=reason,
                    affected_count=affected,
                    target_memory_ids=memory_ids,
                )
                current.discard(memory_id)
        if self._records_for_clear():
            raise ConversationMemoryAdminError("MEMORY_ADMIN_CLEAR_FAILED")
        self._write_audit(
            request_id=request_id,
            operation="clear",
            payload_fingerprint=fingerprint,
            status="completed",
            reason=reason,
            affected_count=affected,
            target_memory_ids=memory_ids,
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
        reason = _text(reason, field_name="reason", maximum=500)
        fingerprint = _payload_fingerprint("pause", {"reason": reason})
        with self._lifecycle_lock, self._lock:
            existing = self._existing_result(request_id, "pause", fingerprint)
            if existing is not None:
                return existing
            try:
                with self._connect() as connection:
                    active = connection.execute(
                        "SELECT 1 FROM memory_admin_pause_windows "
                        "WHERE user_id = ? AND resumed_at IS NULL",
                        (self.user_id,),
                    ).fetchone()
                    if active is not None:
                        status = MemoryAdminMutationStatus.NOOP
                    else:
                        connection.execute(
                            "INSERT INTO memory_admin_pause_windows "
                            "(user_id, pause_request_id, started_at) VALUES (?, ?, ?)",
                            (self.user_id, request_id, datetime.now(timezone.utc).isoformat()),
                        )
                        status = MemoryAdminMutationStatus.APPLIED
                    self._write_audit_in_connection(
                        connection,
                        request_id=request_id,
                        operation="pause",
                        payload_fingerprint=fingerprint,
                        status=(
                            "noop"
                            if status is MemoryAdminMutationStatus.NOOP
                            else "completed"
                        ),
                        reason=reason,
                        affected_count=0,
                    )
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc
        return MemoryAdminMutationResult(status, request_id, "pause")

    def resume(
        self,
        *,
        request_id: str,
        reason: str,
    ) -> MemoryAdminMutationResult:
        request_id = _identifier(request_id, field_name="request_id")
        reason = _text(reason, field_name="reason", maximum=500)
        fingerprint = _payload_fingerprint("resume", {"reason": reason})
        with self._lifecycle_lock, self._lock:
            existing = self._existing_result(request_id, "resume", fingerprint)
            if existing is not None:
                return existing
            try:
                with self._connect() as connection:
                    active = connection.execute(
                        "SELECT pause_request_id FROM memory_admin_pause_windows "
                        "WHERE user_id = ? AND resumed_at IS NULL "
                        "ORDER BY started_at DESC LIMIT 1",
                        (self.user_id,),
                    ).fetchone()
                    if active is None:
                        status = MemoryAdminMutationStatus.NOOP
                    else:
                        connection.execute(
                            "UPDATE memory_admin_pause_windows SET resume_request_id = ?, resumed_at = ? "
                            "WHERE user_id = ? AND pause_request_id = ?",
                            (
                                request_id,
                                datetime.now(timezone.utc).isoformat(),
                                self.user_id,
                                str(active[0]),
                            ),
                        )
                        status = MemoryAdminMutationStatus.APPLIED
                    self._write_audit_in_connection(
                        connection,
                        request_id=request_id,
                        operation="resume",
                        payload_fingerprint=fingerprint,
                        status=(
                            "noop"
                            if status is MemoryAdminMutationStatus.NOOP
                            else "completed"
                        ),
                        reason=reason,
                        affected_count=0,
                    )
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc
        return MemoryAdminMutationResult(status, request_id, "resume")

    def is_paused(self) -> bool:
        with self._lifecycle_lock:
            return self._is_paused()

    def _is_paused(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute(
                    "SELECT 1 FROM memory_admin_pause_windows "
                    "WHERE user_id = ? AND resumed_at IS NULL",
                    (self.user_id,),
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
            if self._has_pending_clear():
                return True
            try:
                with self._connect() as connection:
                    return connection.execute(
                        "SELECT 1 FROM memory_admin_pause_windows "
                        "WHERE user_id = ? AND ((started_at <= ? AND "
                        "(resumed_at IS NULL OR resumed_at >= ?)) OR "
                        "(resumed_at IS NOT NULL AND resumed_at >= ?)) LIMIT 1",
                        (self.user_id, timestamp, timestamp, timestamp),
                    ).fetchone() is not None
            except (OSError, sqlite3.Error) as exc:
                raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc

    def run_write(
        self,
        operation: Callable[[], _T],
        *,
        occurred_at: datetime | None = None,
    ) -> _T | None:
        """Serialize the final lifecycle decision and provider write for this audit file."""
        with self._lifecycle_lock:
            if (
                self._is_paused()
                or self._has_pending_clear()
                or occurred_at is not None and self.blocks_delivery(occurred_at)
            ):
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
                        "SELECT COUNT(*) FROM memory_admin_operations "
                        "WHERE user_id = ?",
                        (self.user_id,),
                    ).fetchone()[0]
                )
                pending_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_admin_operations "
                        "WHERE user_id = ? AND "
                        "status = 'replacement_written_delete_pending'",
                        (self.user_id,),
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

    def _require_available(self) -> None:
        self._require_provider_available()
        if self.is_paused():
            raise ConversationMemoryAdminError("MEMORY_ADMIN_PAUSED")

    def _require_provider_available(self) -> None:
        status = self._provider_status()
        if status.status == "disabled":
            raise ConversationMemoryAdminError("MEMORY_ADMIN_DISABLED")
        if status.status == "unavailable":
            raise ConversationMemoryAdminError("MEMORY_ADMIN_UNAVAILABLE")

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

    def _records(self) -> tuple[ConversationMemoryRecord, ...]:
        self._require_available()
        return self._records_for_clear()

    def _records_for_clear(self) -> tuple[ConversationMemoryRecord, ...]:
        self._require_provider_available()
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
                    "SELECT * FROM memory_admin_operations "
                    "WHERE user_id = ? AND request_id = ?",
                    (self.user_id, request_id),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
            ) from exc

    def _existing_result(
        self,
        request_id: str,
        operation: str,
        payload_fingerprint: str,
    ) -> MemoryAdminMutationResult | None:
        row = self._audit_row(request_id)
        if row is None:
            return None
        self._assert_request_matches(row, operation, payload_fingerprint)
        return self._result_from_row(row, duplicate=True)

    @staticmethod
    def _assert_request_matches(
        row: sqlite3.Row,
        operation: str,
        payload_fingerprint: str,
    ) -> None:
        if str(row["operation"]) != operation:
            raise ConversationMemoryAdminError("MEMORY_ADMIN_REQUEST_CONFLICT")
        # Pre-fingerprint audit rows cannot be reconstructed safely. They retain
        # their historical operation-only replay behavior; all new rows bind the
        # request to the normalized payload.
        if (
            str(row["payload_fingerprint"]) != "legacy"
            and str(row["payload_fingerprint"]) != payload_fingerprint
        ):
            raise ConversationMemoryAdminError("MEMORY_ADMIN_REQUEST_CONFLICT")

    def _has_pending_clear(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute(
                    "SELECT 1 FROM memory_admin_operations "
                    "WHERE user_id = ? AND status = 'pending_clear' LIMIT 1",
                    (self.user_id,),
                ).fetchone() is not None
        except (OSError, sqlite3.Error) as exc:
            raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_UNAVAILABLE") from exc

    def _require_no_pending_clear(self) -> None:
        if self._has_pending_clear():
            raise ConversationMemoryAdminError("MEMORY_ADMIN_CLEAR_PENDING")

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
        payload_fingerprint: str,
        status: str,
        reason: str,
        affected_count: int,
        target_memory_id: str | None = None,
        replacement_memory_id: str | None = None,
        replacement_source_id: str | None = None,
        target_memory_ids: tuple[str, ...] | None = None,
    ) -> None:
        if operation not in _OPERATIONS or status not in _AUDIT_STATUSES:
            raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_INVALID")
        try:
            with self._connect() as connection:
                self._write_audit_in_connection(
                    connection,
                    request_id=request_id,
                    operation=operation,
                    payload_fingerprint=payload_fingerprint,
                    status=status,
                    reason=reason,
                    affected_count=affected_count,
                    target_memory_id=target_memory_id,
                    replacement_memory_id=replacement_memory_id,
                    replacement_source_id=replacement_source_id,
                    target_memory_ids=target_memory_ids,
                )
        except ConversationMemoryAdminError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
            ) from exc

    def _write_audit_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        operation: str,
        payload_fingerprint: str,
        status: str,
        reason: str,
        affected_count: int,
        target_memory_id: str | None = None,
        replacement_memory_id: str | None = None,
        replacement_source_id: str | None = None,
        target_memory_ids: tuple[str, ...] | None = None,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        existing = connection.execute(
            "SELECT operation, payload_fingerprint, target_memory_id, "
            "target_memory_ids, replacement_source_id "
            "FROM memory_admin_operations WHERE user_id = ? AND request_id = ?",
            (self.user_id, request_id),
        ).fetchone()
        if existing is not None and (
            str(existing["operation"]) != operation
            or str(existing["payload_fingerprint"]) != payload_fingerprint
            or (existing["target_memory_id"] or None) != target_memory_id
            or (existing["target_memory_ids"] or None)
            != _serialized_memory_ids(target_memory_ids)
            or (existing["replacement_source_id"] or None) != replacement_source_id
        ):
            raise ConversationMemoryAdminError("MEMORY_ADMIN_REQUEST_CONFLICT")
        connection.execute(
            """
            INSERT INTO memory_admin_operations (
                user_id, request_id, operation, target_memory_id,
                payload_fingerprint, target_memory_ids, replacement_memory_id,
                replacement_source_id, status, affected_count, reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, request_id) DO UPDATE SET
                replacement_memory_id = excluded.replacement_memory_id,
                status = excluded.status,
                affected_count = excluded.affected_count,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                self.user_id,
                request_id,
                operation,
                target_memory_id,
                payload_fingerprint,
                _serialized_memory_ids(target_memory_ids),
                replacement_memory_id,
                replacement_source_id,
                status,
                affected_count,
                reason,
                timestamp,
                timestamp,
            ),
        )


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ConversationMemoryAdminError("MEMORY_ADMIN_IDENTIFIER_INVALID")
    return value


def _normalized_user_id(value: object) -> str:
    try:
        return normalize_conversation_memory_user_id(value)
    except ConversationMemoryIdentityError as exc:
        raise ConversationMemoryAdminError("MEMORY_ADMIN_IDENTIFIER_INVALID") from exc


def _payload_fingerprint(operation: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": dict(payload)},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _serialized_memory_ids(memory_ids: tuple[str, ...] | None) -> str | None:
    if memory_ids is None:
        return None
    if not memory_ids or len(set(memory_ids)) != len(memory_ids):
        raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_INVALID")
    for memory_id in memory_ids:
        _identifier(memory_id, field_name="memory_id")
    return json.dumps(list(memory_ids), separators=(",", ":"))


def _target_memory_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_INVALID")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_INVALID") from exc
    if not isinstance(parsed, list):
        raise ConversationMemoryAdminError("MEMORY_ADMIN_AUDIT_INVALID")
    return tuple(_identifier(item, field_name="memory_id") for item in parsed)


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
