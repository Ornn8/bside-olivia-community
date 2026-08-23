"""Non-blocking delivery of canonical letter exchanges to long-term memory.

This service owns no extraction or storage algorithm.  It validates the
canonical exchange, derives one stable source identity, and delegates to the
configured ConversationMemoryPort in a worker thread.  Provider failure never
changes the already-persisted reply.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re

from conversation_memory_port import (
    ConversationMemoryPort,
    MemoryWriteStatus,
)


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ERROR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")


class CanonicalMemoryDeliveryError(ValueError):
    code = "CANONICAL_MEMORY_DELIVERY_INVALID"


class CanonicalMemoryDeliveryStatus(StrEnum):
    WRITTEN = "written"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CanonicalMemoryDelivery:
    letter_id: str
    revision: int
    user_message: str
    assistant_message: str
    occurred_at: datetime
    user_id: str = "local-user"

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.letter_id, "letter_id"),
            (self.user_id, "user_id"),
        ):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise CanonicalMemoryDeliveryError(f"{field_name} is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise CanonicalMemoryDeliveryError("revision must be positive")
        _message(self.user_message, field_name="user_message", maximum=10_000)
        _message(
            self.assistant_message,
            field_name="assistant_message",
            maximum=20_000,
        )
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise CanonicalMemoryDeliveryError(
                "occurred_at must be timezone-aware"
            )
        if len(self.source_id) > 160:
            raise CanonicalMemoryDeliveryError("source_id is too long")

    @property
    def source_id(self) -> str:
        return f"reply:{self.letter_id}:{self.revision}"


@dataclass(frozen=True)
class CanonicalMemoryDeliveryResult:
    status: CanonicalMemoryDeliveryStatus
    source_id: str
    memory_count: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CanonicalMemoryDeliveryStatus):
            raise CanonicalMemoryDeliveryError("status is invalid")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise CanonicalMemoryDeliveryError("source_id is invalid")
        if type(self.memory_count) is not int or self.memory_count < 0:
            raise CanonicalMemoryDeliveryError("memory_count is invalid")
        if self.error_code is not None and not _ERROR_RE.fullmatch(
            self.error_code
        ):
            raise CanonicalMemoryDeliveryError("error_code is invalid")
        if (
            self.status is CanonicalMemoryDeliveryStatus.UNAVAILABLE
            and self.error_code is None
        ):
            raise CanonicalMemoryDeliveryError(
                "unavailable result requires an error code"
            )
        if (
            self.status is not CanonicalMemoryDeliveryStatus.UNAVAILABLE
            and self.error_code is not None
        ):
            raise CanonicalMemoryDeliveryError(
                "successful result cannot carry an error code"
            )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status.value,
            "source_id": self.source_id,
            "memory_count": self.memory_count,
        }
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        return payload


class ConversationMemoryDeliveryCommitter:
    """Commit canonical exchanges without blocking the aiohttp event loop."""

    def __init__(
        self,
        memory: ConversationMemoryPort,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("memory delivery timeout is invalid")
        self.memory = memory
        self.timeout_seconds = float(timeout_seconds)

    async def commit(
        self,
        delivery: CanonicalMemoryDelivery,
    ) -> CanonicalMemoryDeliveryResult:
        if not isinstance(delivery, CanonicalMemoryDelivery):
            raise TypeError("a canonical memory delivery is required")

        provider_status = _provider_status(self.memory)
        if provider_status == "disabled":
            return CanonicalMemoryDeliveryResult(
                CanonicalMemoryDeliveryStatus.SKIPPED,
                delivery.source_id,
            )
        if provider_status == "unavailable":
            return CanonicalMemoryDeliveryResult(
                CanonicalMemoryDeliveryStatus.UNAVAILABLE,
                delivery.source_id,
                error_code=_provider_error(self.memory),
            )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.memory.remember_exchange,
                    user_message=delivery.user_message,
                    assistant_message=delivery.assistant_message,
                    occurred_at=delivery.occurred_at,
                    source_id=delivery.source_id,
                    user_id=delivery.user_id,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return CanonicalMemoryDeliveryResult(
                CanonicalMemoryDeliveryStatus.UNAVAILABLE,
                delivery.source_id,
                error_code="MEM0_WRITE_TIMEOUT",
            )
        except Exception:
            return CanonicalMemoryDeliveryResult(
                CanonicalMemoryDeliveryStatus.UNAVAILABLE,
                delivery.source_id,
                error_code="MEM0_WRITE_FAILED",
            )

        mapping = {
            MemoryWriteStatus.WRITTEN: CanonicalMemoryDeliveryStatus.WRITTEN,
            MemoryWriteStatus.DUPLICATE: CanonicalMemoryDeliveryStatus.DUPLICATE,
            MemoryWriteStatus.SKIPPED: CanonicalMemoryDeliveryStatus.SKIPPED,
            MemoryWriteStatus.UNAVAILABLE: CanonicalMemoryDeliveryStatus.UNAVAILABLE,
        }
        status = mapping[result.status]
        error_code = result.error_code if status is CanonicalMemoryDeliveryStatus.UNAVAILABLE else None
        return CanonicalMemoryDeliveryResult(
            status,
            delivery.source_id,
            memory_count=len(result.memory_ids),
            error_code=error_code or (
                "MEM0_WRITE_FAILED"
                if status is CanonicalMemoryDeliveryStatus.UNAVAILABLE
                else None
            ),
        )


def _message(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CanonicalMemoryDeliveryError(f"{field_name} must be text")
    if (
        not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 and character not in "\n\r\t" for character in value)
        or any(ord(character) == 127 for character in value)
    ):
        raise CanonicalMemoryDeliveryError(f"{field_name} is invalid")
    return value


def _provider_status(memory: ConversationMemoryPort) -> str:
    try:
        status = memory.status().status
    except Exception:
        return "unavailable"
    return status if status in {"available", "degraded", "unavailable", "disabled"} else "unavailable"


def _provider_error(memory: ConversationMemoryPort) -> str:
    try:
        code = memory.status().reason_code
    except Exception:
        code = None
    return code if isinstance(code, str) and _ERROR_RE.fullmatch(code) else "MEM0_WRITE_FAILED"


__all__ = [
    "CanonicalMemoryDelivery",
    "CanonicalMemoryDeliveryError",
    "CanonicalMemoryDeliveryResult",
    "CanonicalMemoryDeliveryStatus",
    "ConversationMemoryDeliveryCommitter",
]
