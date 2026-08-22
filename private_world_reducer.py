"""Pure, deterministic PrivateWorld relationship reducer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
import re

from private_world_port import PrivateWorldSnapshot


class ReducerInputError(ValueError):
    code = "PRIVATE_WORLD_EVENT_INVALID"


class ReducerEventKind(str, Enum):
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
                raise ReducerInputError("previous equivalent time is invalid")
        if not isinstance(self.basis_event_ids, tuple):
            object.__setattr__(self, "basis_event_ids", tuple(self.basis_event_ids))
        if self.kind is ReducerEventKind.STAGE_CONFIRMED:
            if not isinstance(self.target_stage, str) or not _STAGE_RE.fullmatch(
                self.target_stage
            ):
                raise ReducerInputError("stage confirmation requires a target stage")
            if (
                not self.basis_event_ids
                or len(self.basis_event_ids) > 8
                or len(set(self.basis_event_ids)) != len(self.basis_event_ids)
                or any(not _TOKEN_RE.fullmatch(value) for value in self.basis_event_ids)
            ):
                raise ReducerInputError("stage confirmation requires explicit evidence")
        elif self.target_stage is not None or self.basis_event_ids:
            raise ReducerInputError("stage evidence is only valid for stage confirmation")


@dataclass(frozen=True)
class FieldDelta:
    field: str
    before: int | str
    after: int | str


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
    return event.occurred_at - event.last_equivalent_at < _DEDUPLICATION_WINDOW


def reduce_private_world(
    snapshot: PrivateWorldSnapshot, event: ReducerEvent
) -> ReducerResult:
    if not isinstance(snapshot, PrivateWorldSnapshot) or not isinstance(
        event, ReducerEvent
    ):
        raise ReducerInputError("typed snapshot and event are required")
    if event.kind in _NO_EFFECT_KINDS:
        return ReducerResult(
            snapshot, ReducerDelta(False, "NO_RELATIONSHIP_EFFECT")
        )
    if _duplicate(event):
        return ReducerResult(snapshot, ReducerDelta(False, "SEMANTIC_DUPLICATE"))

    updates: dict[str, int | str] = {}
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
        updates = {"relationship_stage": event.target_stage or "unknown"}

    changes = tuple(
        FieldDelta(field, getattr(snapshot, field), value)
        for field, value in updates.items()
        if getattr(snapshot, field) != value
    )
    if not changes:
        return ReducerResult(snapshot, ReducerDelta(False, "BOUNDED_NO_CHANGE"))
    next_snapshot = replace(
        snapshot,
        version=snapshot.version + 1,
        **{change.field: change.after for change in changes},
    )
    return ReducerResult(
        next_snapshot,
        ReducerDelta(True, event.kind.value.upper(), changes),
    )

