"""Pure, deterministic PrivateWorld relationship and command reducer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
import re

from private_world_commands import (
    ConfirmRelationshipStage,
    DeleteContinuationFact,
    GrantNickname,
    InitializeHistoricalRelationship,
    PrivateWorldCommand,
    RecordBoundaryRespected,
    RecordConflict,
    RecordRepair,
    RevokeNickname,
    SetContinuationAwareness,
    SetHomeAccess,
    UpsertContinuationFact,
)
from private_world_port import (
    LocalContinuationFact,
    PrivateWorldSnapshot,
)


class ReducerInputError(ValueError):
    code = "PRIVATE_WORLD_EVENT_INVALID"


class ReducerEventKind(str, Enum):
    CANONICAL_REPLY_DELIVERED = "canonical_reply_delivered"
    BOUNDARY_RESPECTED = "boundary_respected"
    CONFLICT = "conflict"
    REPAIR = "repair"
    STAGE_CONFIRMED = "stage_confirmed"
    HIGH_FREQUENCY_MESSAGE = "high_frequency_message"
    GIFT = "gift"
    REPEATED_PHRASE = "repeated_phrase"
    CONFESSION = "confession"
    INACTIVITY = "inactivity"


_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_STAGE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DEDUPLICATION_WINDOW = timedelta(hours=24)
_NO_EFFECT_KINDS = {
    ReducerEventKind.CANONICAL_REPLY_DELIVERED,
    ReducerEventKind.HIGH_FREQUENCY_MESSAGE,
    ReducerEventKind.GIFT,
    ReducerEventKind.REPEATED_PHRASE,
    ReducerEventKind.CONFESSION,
    ReducerEventKind.INACTIVITY,
}


@dataclass(frozen=True)
class ReducerEvent:
    kind: ReducerEventKind
    occurred_at: datetime
    semantic_key: str
    last_equivalent_at: datetime | None = None
    target_stage: str | None = None
    basis_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReducerEventKind):
            raise ReducerInputError("event kind is invalid")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ReducerInputError("event time must be timezone-aware")
        if not isinstance(self.semantic_key, str) or not _TOKEN_RE.fullmatch(
            self.semantic_key
        ):
            raise ReducerInputError("semantic key is invalid")
        if self.last_equivalent_at is not None:
            if (
                not isinstance(self.last_equivalent_at, datetime)
                or self.last_equivalent_at.tzinfo is None
                or self.last_equivalent_at.utcoffset() is None
                or self.last_equivalent_at > self.occurred_at
            ):
                raise ReducerInputError(
                    "previous equivalent time is invalid"
                )
        if not isinstance(self.basis_event_ids, tuple):
            object.__setattr__(
                self,
                "basis_event_ids",
                tuple(self.basis_event_ids),
            )
        if self.kind is ReducerEventKind.STAGE_CONFIRMED:
            if not isinstance(self.target_stage, str) or not _STAGE_RE.fullmatch(
                self.target_stage
            ):
                raise ReducerInputError(
                    "stage confirmation requires a target stage"
                )
            if (
                not self.basis_event_ids
                or len(self.basis_event_ids) > 8
                or len(set(self.basis_event_ids)) != len(self.basis_event_ids)
                or any(
                    not _TOKEN_RE.fullmatch(value)
                    for value in self.basis_event_ids
                )
            ):
                raise ReducerInputError(
                    "stage confirmation requires explicit evidence"
                )
        elif self.target_stage is not None or self.basis_event_ids:
            raise ReducerInputError(
                "stage evidence is only valid for stage confirmation"
            )


@dataclass(frozen=True)
class FieldDelta:
    field: str
    before: object
    after: object


@dataclass(frozen=True)
class ReducerDelta:
    applied: bool
    reason_code: str
    changes: tuple[FieldDelta, ...] = ()


@dataclass(frozen=True)
class ReducerResult:
    snapshot: PrivateWorldSnapshot
    delta: ReducerDelta


def _bounded(value: int, change: int) -> int:
    return min(100, max(0, value + change))


def _duplicate(event: ReducerEvent) -> bool:
    if event.last_equivalent_at is None:
        return False
    return (
        event.occurred_at - event.last_equivalent_at
        < _DEDUPLICATION_WINDOW
    )


def _no_change(
    snapshot: PrivateWorldSnapshot,
    reason_code: str,
) -> ReducerResult:
    return ReducerResult(
        snapshot,
        ReducerDelta(False, reason_code),
    )


def _apply_updates(
    snapshot: PrivateWorldSnapshot,
    updates: dict[str, object],
    *,
    reason_code: str,
    unchanged_reason: str = "BOUNDED_NO_CHANGE",
) -> ReducerResult:
    changes = tuple(
        FieldDelta(field, getattr(snapshot, field), value)
        for field, value in updates.items()
        if getattr(snapshot, field) != value
    )
    if not changes:
        return _no_change(snapshot, unchanged_reason)
    next_snapshot = replace(
        snapshot,
        version=snapshot.version + 1,
        **{change.field: change.after for change in changes},
    )
    return ReducerResult(
        next_snapshot,
        ReducerDelta(True, reason_code, changes),
    )


def reduce_private_world(
    snapshot: PrivateWorldSnapshot,
    event: ReducerEvent,
) -> ReducerResult:
    if not isinstance(snapshot, PrivateWorldSnapshot) or not isinstance(
        event,
        ReducerEvent,
    ):
        raise ReducerInputError("typed snapshot and event are required")
    if event.kind in _NO_EFFECT_KINDS:
        return _no_change(snapshot, "NO_RELATIONSHIP_EFFECT")
    if _duplicate(event):
        return _no_change(snapshot, "SEMANTIC_DUPLICATE")

    updates: dict[str, object] = {}
    if event.kind is ReducerEventKind.BOUNDARY_RESPECTED:
        updates = {
            "trust": _bounded(snapshot.trust, 1),
            "comfort": _bounded(snapshot.comfort, 1),
        }
    elif event.kind is ReducerEventKind.CONFLICT:
        updates = {
            "trust": _bounded(snapshot.trust, -2),
            "comfort": _bounded(snapshot.comfort, -2),
            "tension": _bounded(snapshot.tension, 3),
        }
    elif event.kind is ReducerEventKind.REPAIR:
        updates = {
            "trust": _bounded(snapshot.trust, 1),
            "comfort": _bounded(snapshot.comfort, 1),
            "tension": _bounded(snapshot.tension, -2),
        }
    elif event.kind is ReducerEventKind.STAGE_CONFIRMED:
        updates = {
            "relationship_stage": event.target_stage or "unknown",
        }

    return _apply_updates(
        snapshot,
        updates,
        reason_code=event.kind.value.upper(),
    )


def _relationship_command_event(
    command: PrivateWorldCommand,
) -> ReducerEvent | None:
    if isinstance(command, RecordBoundaryRespected):
        kind = ReducerEventKind.BOUNDARY_RESPECTED
    elif isinstance(command, RecordConflict):
        kind = ReducerEventKind.CONFLICT
    elif isinstance(command, RecordRepair):
        kind = ReducerEventKind.REPAIR
    elif isinstance(command, ConfirmRelationshipStage):
        return ReducerEvent(
            kind=ReducerEventKind.STAGE_CONFIRMED,
            occurred_at=command.occurred_at,
            semantic_key=command.idempotency_key,
            target_stage=command.target_stage.value,
            basis_event_ids=command.basis_event_ids,
        )
    else:
        return None
    return ReducerEvent(
        kind=kind,
        occurred_at=command.occurred_at,
        semantic_key=command.idempotency_key,
    )


def reduce_private_world_command(
    snapshot: PrivateWorldSnapshot,
    command: PrivateWorldCommand,
) -> ReducerResult:
    """Apply one validated control command without persistence or I/O."""

    if not isinstance(snapshot, PrivateWorldSnapshot) or not isinstance(
        command,
        PrivateWorldCommand,
    ):
        raise ReducerInputError(
            "typed snapshot and command are required"
        )

    relationship_event = _relationship_command_event(command)
    if relationship_event is not None:
        return reduce_private_world(snapshot, relationship_event)

    if isinstance(command, InitializeHistoricalRelationship):
        if snapshot != PrivateWorldSnapshot():
            return _no_change(snapshot, "HISTORY_ALREADY_INITIALIZED")
        return _apply_updates(
            snapshot,
            {
                "relationship_stage": command.relationship_stage.value,
                "familiarity": command.familiarity,
                "trust": command.trust,
                "comfort": command.comfort,
                "closeness": command.closeness,
                "tension": command.tension,
            },
            reason_code="INITIALIZE_HISTORICAL_RELATIONSHIP",
            unchanged_reason="HISTORY_INITIALIZED_NO_CHANGE",
        )

    if isinstance(command, GrantNickname):
        if command.nickname in snapshot.nickname_permissions:
            return _no_change(snapshot, "NICKNAME_ALREADY_GRANTED")
        if len(snapshot.nickname_permissions) >= 16:
            return _no_change(snapshot, "NICKNAME_LIMIT_REACHED")
        return _apply_updates(
            snapshot,
            {
                "nickname_permissions": (
                    *snapshot.nickname_permissions,
                    command.nickname,
                ),
            },
            reason_code=command.kind.value.upper(),
        )

    if isinstance(command, RevokeNickname):
        if command.nickname not in snapshot.nickname_permissions:
            return _no_change(snapshot, "NICKNAME_NOT_GRANTED")
        return _apply_updates(
            snapshot,
            {
                "nickname_permissions": tuple(
                    value
                    for value in snapshot.nickname_permissions
                    if value != command.nickname
                ),
            },
            reason_code=command.kind.value.upper(),
        )

    if isinstance(command, SetHomeAccess):
        return _apply_updates(
            snapshot,
            {"home_access": command.home_access},
            reason_code=command.kind.value.upper(),
            unchanged_reason="HOME_ACCESS_UNCHANGED",
        )

    if isinstance(command, UpsertContinuationFact):
        facts = list(snapshot.continuation_facts)
        replacement = LocalContinuationFact(
            command.fact_id,
            command.statement,
            command.awareness,
        )
        for index, fact in enumerate(facts):
            if fact.fact_id == command.fact_id:
                if fact == replacement:
                    return _no_change(
                        snapshot,
                        "CONTINUATION_UNCHANGED",
                    )
                facts[index] = replacement
                break
        else:
            if len(facts) >= 32:
                return _no_change(
                    snapshot,
                    "CONTINUATION_LIMIT_REACHED",
                )
            facts.append(replacement)
        return _apply_updates(
            snapshot,
            {"continuation_facts": tuple(facts)},
            reason_code=command.kind.value.upper(),
        )

    if isinstance(command, SetContinuationAwareness):
        facts = list(snapshot.continuation_facts)
        for index, fact in enumerate(facts):
            if fact.fact_id == command.fact_id:
                if fact.awareness is command.awareness:
                    return _no_change(
                        snapshot,
                        "CONTINUATION_UNCHANGED",
                    )
                facts[index] = replace(
                    fact,
                    awareness=command.awareness,
                )
                return _apply_updates(
                    snapshot,
                    {"continuation_facts": tuple(facts)},
                    reason_code=command.kind.value.upper(),
                )
        return _no_change(snapshot, "CONTINUATION_NOT_FOUND")

    if isinstance(command, DeleteContinuationFact):
        remaining = tuple(
            fact
            for fact in snapshot.continuation_facts
            if fact.fact_id != command.fact_id
        )
        if len(remaining) == len(snapshot.continuation_facts):
            return _no_change(
                snapshot,
                "CONTINUATION_NOT_FOUND",
            )
        return _apply_updates(
            snapshot,
            {"continuation_facts": remaining},
            reason_code=command.kind.value.upper(),
        )

    raise ReducerInputError("unsupported PrivateWorld command")
