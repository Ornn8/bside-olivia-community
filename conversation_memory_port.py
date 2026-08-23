"""Provider-neutral contracts for new-conversation long-term memory.

Archive storage, Persona evidence, and PrivateWorld state intentionally remain
outside this port. Implementations may use Mem0 or another provider, but callers
see only bounded records and stable results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import re
from typing import Mapping, Protocol, runtime_checkable


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_DOMAIN = "conversation_memory"


class ConversationMemoryError(ValueError):
    code = "CONVERSATION_MEMORY_INVALID"


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ConversationMemoryError(f"{field_name} is invalid")
    return value


def _plain_text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConversationMemoryError(f"{field_name} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ConversationMemoryError(f"{field_name} is invalid")
    return normalized


def _timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ConversationMemoryError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConversationMemoryError(f"{field_name} must be timezone-aware")
    return value


def _metadata(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ConversationMemoryError("metadata must be an object")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", key):
            raise ConversationMemoryError("metadata key is invalid")
        if key in {
            "private_world",
            "hidden_scores",
            "control_state",
            "system_prompt",
            "api_key",
            "credential",
        }:
            raise ConversationMemoryError("metadata key is reserved")
        if item is None or isinstance(item, (bool, int, float, str)):
            normalized[key] = item
        else:
            raise ConversationMemoryError("metadata value is not scalar")
    if len(normalized) > 24:
        raise ConversationMemoryError("metadata is too large")
    return normalized


class MemoryWriteStatus(StrEnum):
    WRITTEN = "written"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ConversationMemoryRecord:
    memory_id: str
    text: str
    user_id: str
    source_id: str
    score: float | None = None
    occurred_at: datetime | None = None
    created_at: datetime | None = None
    domain: str = _DOMAIN
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "memory_id",
            _identifier(self.memory_id, field_name="memory_id"),
        )
        object.__setattr__(
            self,
            "text",
            _plain_text(self.text, field_name="memory text", maximum=2000),
        )
        object.__setattr__(
            self,
            "user_id",
            _identifier(self.user_id, field_name="user_id"),
        )
        object.__setattr__(
            self,
            "source_id",
            _identifier(self.source_id, field_name="source_id"),
        )
        if self.domain != _DOMAIN:
            raise ConversationMemoryError("memory domain is invalid")
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not 0.0 <= float(self.score) <= 1.0
        ):
            raise ConversationMemoryError("memory score is invalid")
        if self.occurred_at is not None:
            _timestamp(self.occurred_at, field_name="occurred_at")
        if self.created_at is not None:
            _timestamp(self.created_at, field_name="created_at")
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def to_prompt_dict(self) -> dict[str, object]:
        """Return only fields allowed to enter bounded prompt references."""

        payload: dict[str, object] = {
            "memory_id": self.memory_id,
            "text": self.text,
            "source_id": self.source_id,
            "domain": self.domain,
        }
        if self.score is not None:
            payload["score"] = round(float(self.score), 6)
        if self.occurred_at is not None:
            payload["occurred_at"] = self.occurred_at.isoformat()
        if self.created_at is not None:
            payload["created_at"] = self.created_at.isoformat()
        return payload


@dataclass(frozen=True)
class MemoryWriteResult:
    status: MemoryWriteStatus
    source_id: str
    memory_ids: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MemoryWriteStatus):
            raise ConversationMemoryError("write status is invalid")
        object.__setattr__(
            self,
            "source_id",
            _identifier(self.source_id, field_name="source_id"),
        )
        if len(self.memory_ids) > 64:
            raise ConversationMemoryError("too many memory ids")
        for memory_id in self.memory_ids:
            _identifier(memory_id, field_name="memory_id")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not re.fullmatch(r"^[A-Z][A-Z0-9_]{0,95}$", self.error_code)
        ):
            raise ConversationMemoryError("error_code is invalid")
        if self.status is MemoryWriteStatus.UNAVAILABLE and self.error_code is None:
            raise ConversationMemoryError("unavailable writes require an error code")
        if self.status is not MemoryWriteStatus.UNAVAILABLE and self.error_code is not None:
            raise ConversationMemoryError("successful writes cannot carry an error code")


@dataclass(frozen=True)
class ConversationMemoryStatus:
    status: str
    enabled: bool
    provider: str
    storage: str
    reason_code: str | None = None
    memory_count: int | None = None

    def __post_init__(self) -> None:
        if self.status not in {"available", "degraded", "unavailable", "disabled"}:
            raise ConversationMemoryError("provider status is invalid")
        if type(self.enabled) is not bool:
            raise ConversationMemoryError("enabled flag is invalid")
        _identifier(self.provider, field_name="provider")
        _identifier(self.storage, field_name="storage")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str)
            or not re.fullmatch(r"^[A-Z][A-Z0-9_]{0,95}$", self.reason_code)
        ):
            raise ConversationMemoryError("reason_code is invalid")
        if self.memory_count is not None and (
            type(self.memory_count) is not int or self.memory_count < 0
        ):
            raise ConversationMemoryError("memory_count is invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "enabled": self.enabled,
            "provider": self.provider,
            "storage": self.storage,
        }
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        if self.memory_count is not None:
            payload["memory_count"] = self.memory_count
        return payload


@runtime_checkable
class ConversationMemoryPort(Protocol):
    enabled: bool

    def search_context(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationMemoryRecord, ...]: ...

    def remember_exchange(
        self,
        *,
        user_message: str,
        assistant_message: str,
        occurred_at: datetime,
        source_id: str,
        user_id: str,
    ) -> MemoryWriteResult: ...

    def list_memories(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> tuple[ConversationMemoryRecord, ...]: ...

    def add_manual_memory(
        self,
        text: str,
        *,
        user_id: str,
        source_id: str,
    ) -> ConversationMemoryRecord: ...

    def delete_memory(self, memory_id: str, *, user_id: str) -> bool: ...

    def clear_user(self, *, user_id: str) -> int: ...

    def export_user(self, *, user_id: str) -> dict[str, object]: ...

    def status(self) -> ConversationMemoryStatus: ...


class NullConversationMemoryPort:
    enabled = False

    def search_context(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationMemoryRecord, ...]:
        del query, user_id, limit
        return ()

    def remember_exchange(
        self,
        *,
        user_message: str,
        assistant_message: str,
        occurred_at: datetime,
        source_id: str,
        user_id: str,
    ) -> MemoryWriteResult:
        del user_message, assistant_message, occurred_at, user_id
        return MemoryWriteResult(MemoryWriteStatus.SKIPPED, source_id)

    def list_memories(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> tuple[ConversationMemoryRecord, ...]:
        del user_id, limit
        return ()

    def add_manual_memory(
        self,
        text: str,
        *,
        user_id: str,
        source_id: str,
    ) -> ConversationMemoryRecord:
        del text, user_id, source_id
        raise RuntimeError("CONVERSATION_MEMORY_DISABLED")

    def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
        del memory_id, user_id
        return False

    def clear_user(self, *, user_id: str) -> int:
        del user_id
        return 0

    def export_user(self, *, user_id: str) -> dict[str, object]:
        return {
            "schema_version": "p03.conversation-memory-export.v1",
            "user_id": user_id,
            "records": [],
        }

    def status(self) -> ConversationMemoryStatus:
        return ConversationMemoryStatus("disabled", False, "none", "none")


class UnavailableConversationMemoryPort(NullConversationMemoryPort):
    enabled = False

    def __init__(self, reason_code: str) -> None:
        if not isinstance(reason_code, str) or not re.fullmatch(
            r"^[A-Z][A-Z0-9_]{0,95}$",
            reason_code,
        ):
            raise ConversationMemoryError("reason_code is invalid")
        self.reason_code = reason_code

    def remember_exchange(
        self,
        *,
        user_message: str,
        assistant_message: str,
        occurred_at: datetime,
        source_id: str,
        user_id: str,
    ) -> MemoryWriteResult:
        del user_message, assistant_message, occurred_at, user_id
        return MemoryWriteResult(
            MemoryWriteStatus.UNAVAILABLE,
            source_id,
            error_code=self.reason_code,
        )

    def status(self) -> ConversationMemoryStatus:
        return ConversationMemoryStatus(
            "unavailable",
            False,
            "none",
            "none",
            reason_code=self.reason_code,
        )


__all__ = [
    "ConversationMemoryError",
    "ConversationMemoryPort",
    "ConversationMemoryRecord",
    "ConversationMemoryStatus",
    "MemoryWriteResult",
    "MemoryWriteStatus",
    "NullConversationMemoryPort",
    "UnavailableConversationMemoryPort",
]
