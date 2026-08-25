"""Adapt existing memory and PrivateWorld services to the original settings API.

The adapter owns no extraction, retrieval, reduction, decision, or persistence
logic. It maps already-bounded service results into the transport dataclasses
used by the patched original Olivia settings view.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from conversation_memory_admin import MemoryAdminStatus
from conversation_memory_port import ConversationMemoryRecord
from original_client_companion_api import (
    CompanionCandidateSummary,
    CompanionCapability,
    CompanionContinuationSummary,
    CompanionMemorySummary,
    CompanionPrivateWorldSummary,
    CompanionReadStatus,
    CompanionVideoReplySetting,
    OriginalClientCompanionReadBackend,
)
from private_world_candidates import (
    CandidateStatus,
    PrivateWorldCandidate,
)


_RELATIONSHIP_FIELDS = (
    "familiarity",
    "trust",
    "comfort",
    "closeness",
    "tension",
)
_LEVELS = frozenset({"unknown", "low", "medium", "high"})
_HOME_ACCESS = frozenset(
    {"no_access", "visit_access", "errand_access", "domestic_access"}
)
_AWARENESS = frozenset({"control_only", "pending", "character_known"})


class OriginalClientCompanionBackendError(RuntimeError):
    """Stable adapter failure with no provider or storage detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class MemoryAdminReadPort(Protocol):
    def status(self) -> MemoryAdminStatus: ...

    def list_memories(
        self,
        *,
        query: str | None = None,
        limit: int = 100,
    ) -> Sequence[ConversationMemoryRecord]: ...


@runtime_checkable
class PrivateWorldReadPort(Protocol):
    def snapshot(self) -> Mapping[str, object]: ...


@runtime_checkable
class CandidateReadPort(Protocol):
    def list_candidates(
        self,
        *,
        status: CandidateStatus | None = None,
        now: datetime | None = None,
    ) -> Sequence[PrivateWorldCandidate]: ...


def _capability_failure(code: str) -> CompanionCapability:
    return CompanionCapability("unavailable", reason_code=code)


def _aware_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OriginalClientCompanionBackendError("COMPANION_TIME_INVALID")
    return value


def _mapping(value: object, *, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OriginalClientCompanionBackendError(code)
    return value


def _sequence(value: object, *, code: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise OriginalClientCompanionBackendError(code)
    return value


def _text(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OriginalClientCompanionBackendError(code)
    return value.strip()


def _integer(value: object, *, code: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise OriginalClientCompanionBackendError(code)
    return value


def _memory_timestamp(record: ConversationMemoryRecord) -> str:
    value = record.created_at or record.occurred_at
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise OriginalClientCompanionBackendError(
            "COMPANION_MEMORY_TIME_UNAVAILABLE"
        )
    return value.isoformat()


class OriginalClientCompanionServiceBackend(OriginalClientCompanionReadBackend):
    """Read adapter over optional services already owned by the local runtime."""

    def __init__(
        self,
        *,
        memory_admin: MemoryAdminReadPort | None = None,
        private_world: PrivateWorldReadPort | None = None,
        candidates: CandidateReadPort | None = None,
        video_reply_settings: object | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        for value, protocol, name in (
            (memory_admin, MemoryAdminReadPort, "memory admin"),
            (private_world, PrivateWorldReadPort, "PrivateWorld read port"),
            (candidates, CandidateReadPort, "candidate read port"),
        ):
            if value is not None and not isinstance(value, protocol):
                raise TypeError(f"{name} is invalid")
        self._memory_admin = memory_admin
        self._private_world = private_world
        self._candidates = candidates
        self._video_reply_settings = video_reply_settings
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _memory_status(self) -> CompanionCapability:
        if self._memory_admin is None:
            return CompanionCapability(
                "disabled",
                reason_code="COMPANION_MEMORY_DISABLED",
            )
        try:
            status = self._memory_admin.status()
            if not isinstance(status, MemoryAdminStatus):
                raise TypeError("invalid memory status")
            count = (
                status.memory_count
                if status.status in {"available", "degraded"}
                else None
            )
            return CompanionCapability(
                status.status,
                reason_code=status.reason_code,
                count=count,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _capability_failure("COMPANION_MEMORY_UNAVAILABLE")

    def _private_world_status(self) -> CompanionCapability:
        if self._private_world is None:
            return CompanionCapability(
                "disabled",
                reason_code="COMPANION_PRIVATE_WORLD_DISABLED",
            )
        try:
            self.private_world_summary()
            return CompanionCapability("available")
        except (OSError, RuntimeError, TypeError, ValueError):
            return _capability_failure("COMPANION_PRIVATE_WORLD_UNAVAILABLE")

    def _candidate_status(self) -> CompanionCapability:
        if self._candidates is None:
            return CompanionCapability(
                "disabled",
                reason_code="COMPANION_CANDIDATES_DISABLED",
            )
        try:
            values = self._pending_candidates()
            return CompanionCapability("available", count=len(values))
        except (OSError, RuntimeError, TypeError, ValueError):
            return _capability_failure("COMPANION_CANDIDATES_UNAVAILABLE")

    def _video_reply_setting(self) -> CompanionVideoReplySetting | None:
        if self._video_reply_settings is None:
            return None
        reader = getattr(
            self._video_reply_settings,
            "read_video_reply_enabled",
            None,
        )
        if not callable(reader):
            return CompanionVideoReplySetting(True)
        try:
            value = reader()
        except (OSError, RuntimeError, TypeError, ValueError):
            return CompanionVideoReplySetting(True)
        return CompanionVideoReplySetting(value if type(value) is bool else True)

    def read_status(self) -> CompanionReadStatus:
        return CompanionReadStatus(
            memory=self._memory_status(),
            private_world=self._private_world_status(),
            candidates=self._candidate_status(),
            video_reply=self._video_reply_setting(),
        )

    def list_memories(
        self,
        *,
        query: str | None,
        limit: int,
    ) -> tuple[CompanionMemorySummary, ...]:
        if self._memory_admin is None:
            raise OriginalClientCompanionBackendError(
                "COMPANION_MEMORY_DISABLED"
            )
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OriginalClientCompanionBackendError(
                "COMPANION_MEMORY_LIMIT_INVALID"
            )
        try:
            records = tuple(
                self._memory_admin.list_memories(query=query, limit=limit)
            )
        except Exception as exc:
            raise OriginalClientCompanionBackendError(
                "COMPANION_MEMORY_UNAVAILABLE"
            ) from exc
        if len(records) > limit or any(
            not isinstance(record, ConversationMemoryRecord)
            for record in records
        ):
            raise OriginalClientCompanionBackendError(
                "COMPANION_MEMORY_INVALID"
            )
        return tuple(
            CompanionMemorySummary(
                memory_id=record.memory_id,
                text=record.text,
                source_id=record.source_id,
                created_at=_memory_timestamp(record),
                score=record.score,
            )
            for record in records
        )

    def private_world_summary(self) -> CompanionPrivateWorldSummary:
        if self._private_world is None:
            raise OriginalClientCompanionBackendError(
                "COMPANION_PRIVATE_WORLD_DISABLED"
            )
        try:
            payload = _mapping(
                self._private_world.snapshot(),
                code="COMPANION_PRIVATE_WORLD_INVALID",
            )
            levels_raw = _mapping(
                payload.get("levels"),
                code="COMPANION_PRIVATE_WORLD_INVALID",
            )
            if set(levels_raw) != set(_RELATIONSHIP_FIELDS):
                raise OriginalClientCompanionBackendError(
                    "COMPANION_PRIVATE_WORLD_INVALID"
                )
            levels = {
                name: _text(
                    levels_raw[name],
                    code="COMPANION_PRIVATE_WORLD_INVALID",
                )
                for name in _RELATIONSHIP_FIELDS
            }
            if any(value not in _LEVELS for value in levels.values()):
                raise OriginalClientCompanionBackendError(
                    "COMPANION_PRIVATE_WORLD_INVALID"
                )

            nicknames_raw = _sequence(
                payload.get("nickname_permissions", ()),
                code="COMPANION_PRIVATE_WORLD_INVALID",
            )
            nicknames = tuple(
                _text(value, code="COMPANION_PRIVATE_WORLD_INVALID")
                for value in nicknames_raw
            )

            facts_raw = _sequence(
                payload.get("continuation_facts", ()),
                code="COMPANION_PRIVATE_WORLD_INVALID",
            )
            facts: list[CompanionContinuationSummary] = []
            for raw in facts_raw:
                item = _mapping(
                    raw,
                    code="COMPANION_PRIVATE_WORLD_INVALID",
                )
                awareness = _text(
                    item.get("awareness"),
                    code="COMPANION_PRIVATE_WORLD_INVALID",
                )
                if awareness not in _AWARENESS:
                    raise OriginalClientCompanionBackendError(
                        "COMPANION_PRIVATE_WORLD_INVALID"
                    )
                facts.append(
                    CompanionContinuationSummary(
                        fact_id=_text(
                            item.get("fact_id"),
                            code="COMPANION_PRIVATE_WORLD_INVALID",
                        ),
                        statement=_text(
                            item.get("statement"),
                            code="COMPANION_PRIVATE_WORLD_INVALID",
                        ),
                        awareness=awareness,
                    )
                )

            home_access = _text(
                payload.get("home_access"),
                code="COMPANION_PRIVATE_WORLD_INVALID",
            )
            if home_access not in _HOME_ACCESS:
                raise OriginalClientCompanionBackendError(
                    "COMPANION_PRIVATE_WORLD_INVALID"
                )

            return CompanionPrivateWorldSummary(
                version=_integer(
                    payload.get("version"),
                    code="COMPANION_PRIVATE_WORLD_INVALID",
                ),
                relationship_stage=_text(
                    payload.get("relationship_stage"),
                    code="COMPANION_PRIVATE_WORLD_INVALID",
                ),
                levels=levels,
                nickname_permissions=nicknames,
                home_access=home_access,
                continuation_facts=tuple(facts),
            )
        except OriginalClientCompanionBackendError:
            raise
        except Exception as exc:
            raise OriginalClientCompanionBackendError(
                "COMPANION_PRIVATE_WORLD_UNAVAILABLE"
            ) from exc

    def _pending_candidates(self) -> tuple[PrivateWorldCandidate, ...]:
        if self._candidates is None:
            raise OriginalClientCompanionBackendError(
                "COMPANION_CANDIDATES_DISABLED"
            )
        now = _aware_now(self._now())
        try:
            values = tuple(
                self._candidates.list_candidates(
                    status=CandidateStatus.PENDING,
                    now=now,
                )
            )
        except Exception as exc:
            raise OriginalClientCompanionBackendError(
                "COMPANION_CANDIDATES_UNAVAILABLE"
            ) from exc
        if any(
            not isinstance(value, PrivateWorldCandidate)
            or value.status is not CandidateStatus.PENDING
            for value in values
        ):
            raise OriginalClientCompanionBackendError(
                "COMPANION_CANDIDATES_INVALID"
            )
        return values

    def list_candidates(
        self,
        *,
        limit: int,
    ) -> tuple[CompanionCandidateSummary, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OriginalClientCompanionBackendError(
                "COMPANION_CANDIDATE_LIMIT_INVALID"
            )
        return tuple(
            CompanionCandidateSummary(
                candidate_id=value.candidate_id,
                candidate_type=value.candidate_type.value,
                summary=value.summary,
                created_at=value.created_at.isoformat(),
                expires_at=value.expires_at.isoformat(),
            )
            for value in self._pending_candidates()[:limit]
        )


__all__ = [
    "CandidateReadPort",
    "MemoryAdminReadPort",
    "OriginalClientCompanionBackendError",
    "OriginalClientCompanionServiceBackend",
    "PrivateWorldReadPort",
]
