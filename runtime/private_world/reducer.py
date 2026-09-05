"""Pure, deterministic PrivateWorld relationship and command reducer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import re

from .commands import (
    ApplyHistoricalRelationshipEvidence,
    ConfirmRelationshipStage,
    DeleteContinuationFact,
    GrantIntimacy,
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
from .port import (
    AcknowledgedAffection,
    ActiveBoundary,
    AffectionIntensity,
    AffectionScope,
    IntimacyGrant,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)
from runtime.reply.reply_context import (
    IntimacyTier,
    ReplyContextError,
    intimacy_ceiling_for_stage,
)


class ReducerInputError(ValueError):
    code = "PRIVATE_WORLD_EVENT_INVALID"


class ReducerEventKind(str, Enum):
    CANONICAL_REPLY_DELIVERED = "canonical_reply_delivered"
    BOUNDARY_RESPECTED = "boundary_respected"
    SUPPORT_RECEIVED = "support_received"
    CONFLICT = "conflict"
    REPAIR = "repair"
    STAGE_CONFIRMED = "stage_confirmed"
    INTIMACY_GRANTED = "intimacy_granted"
    HIGH_FREQUENCY_MESSAGE = "high_frequency_message"
    GIFT = "gift"
    REPEATED_PHRASE = "repeated_phrase"
    CONFESSION = "confession"
    INACTIVITY = "inactivity"
    CHARACTER_BOUNDARY_SET = "character_boundary_set"
    CHARACTER_BOUNDARY_WITHDRAWN = "character_boundary_withdrawn"
    CHARACTER_AFFECTION_ACKNOWLEDGED = "character_affection_acknowledged"


_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_STAGE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DEDUPLICATION_WINDOW = timedelta(hours=24)
_GROWTH_WINDOW = timedelta(days=7)
_WEEKLY_GROWTH_CAP = 6
_INTIMACY_TIER_ORDER = (
    IntimacyTier.NONE,
    IntimacyTier.LIGHT_CONTACT,
    IntimacyTier.CLOSE_CONTACT,
)
_AFFECTION_INTENSITY_ORDER = (
    AffectionIntensity.WARMTH,
    AffectionIntensity.CARE,
    AffectionIntensity.LOVE,
)
_CHARACTER_FACT_KINDS = {
    ReducerEventKind.CHARACTER_BOUNDARY_SET,
    ReducerEventKind.CHARACTER_BOUNDARY_WITHDRAWN,
    ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED,
}
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
    intimacy_grant: IntimacyGrant | None = None
    canonical_reply_id: str | None = None
    boundary: ActiveBoundary | None = None
    boundary_id: str | None = None
    acknowledged_affection: AcknowledgedAffection | None = None
    asserted_affection_scope: AffectionScope | None = None

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
        if self.kind in _CHARACTER_FACT_KINDS:
            if (
                not isinstance(self.canonical_reply_id, str)
                or not _TOKEN_RE.fullmatch(self.canonical_reply_id)
            ):
                raise ReducerInputError(
                    "character fact requires a canonical reply reference"
                )
            if self.target_stage is not None or self.basis_event_ids or self.intimacy_grant:
                raise ReducerInputError("character fact payload is invalid")
            if self.kind is ReducerEventKind.CHARACTER_BOUNDARY_SET:
                if not isinstance(self.boundary, ActiveBoundary):
                    raise ReducerInputError("boundary set requires a typed boundary")
                if datetime.fromisoformat(
                    self.boundary.set_at.replace("Z", "+00:00")
                ) != self.occurred_at.astimezone(timezone.utc):
                    raise ReducerInputError("boundary set time must match delivery")
                if (
                    self.boundary_id is not None
                    or self.acknowledged_affection is not None
                    or self.asserted_affection_scope is not None
                ):
                    raise ReducerInputError("boundary set payload is invalid")
            elif self.kind is ReducerEventKind.CHARACTER_BOUNDARY_WITHDRAWN:
                if (
                    not isinstance(self.boundary_id, str)
                    or not _TOKEN_RE.fullmatch(self.boundary_id)
                ):
                    raise ReducerInputError("boundary withdrawal requires an id")
                if (
                    self.boundary is not None
                    or self.acknowledged_affection is not None
                    or self.asserted_affection_scope is not None
                ):
                    raise ReducerInputError("boundary withdrawal payload is invalid")
            else:
                if not isinstance(self.acknowledged_affection, AcknowledgedAffection):
                    raise ReducerInputError(
                        "affection acknowledgement requires a typed statement"
                    )
                if not isinstance(self.asserted_affection_scope, AffectionScope):
                    raise ReducerInputError("affection scope evidence is required")
                if self.acknowledged_affection.scope is not self.asserted_affection_scope:
                    raise ReducerInputError("affection scope cannot be widened")
                if self.boundary is not None or self.boundary_id is not None:
                    raise ReducerInputError("affection payload is invalid")
        elif self.kind is ReducerEventKind.STAGE_CONFIRMED:
            if self.intimacy_grant is not None:
                raise ReducerInputError(
                    "stage confirmation cannot carry an intimacy grant"
                )
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
        elif self.kind is ReducerEventKind.INTIMACY_GRANTED:
            if not isinstance(self.intimacy_grant, IntimacyGrant):
                raise ReducerInputError(
                    "intimacy grant event requires a typed grant"
                )
            if self.intimacy_grant.tier is IntimacyTier.NONE:
                raise ReducerInputError(
                    "intimacy grant tier must grant contact"
                )
            if self.target_stage is not None or self.basis_event_ids:
                raise ReducerInputError(
                    "intimacy grant cannot carry stage evidence"
                )
        elif (
            self.target_stage is not None
            or self.basis_event_ids
            or self.intimacy_grant is not None
            or self.canonical_reply_id is not None
            or self.boundary is not None
            or self.boundary_id is not None
            or self.acknowledged_affection is not None
            or self.asserted_affection_scope is not None
        ):
            raise ReducerInputError(
                "event payload is invalid for this event kind"
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


def _growth_window(
    snapshot: PrivateWorldSnapshot,
    now: datetime,
) -> tuple[str, int]:
    now_utc = now.astimezone(timezone.utc)
    if snapshot.growth_window_start:
        started_at = datetime.fromisoformat(
            snapshot.growth_window_start.replace("Z", "+00:00")
        )
        if now_utc - started_at < _GROWTH_WINDOW:
            return snapshot.growth_window_start, snapshot.growth_used
    return now_utc.isoformat(), 0


def _add_growth_metadata(
    snapshot: PrivateWorldSnapshot,
    now: datetime,
    updates: dict[str, object],
) -> None:
    growth_start, growth_used = _growth_window(snapshot, now)
    if growth_used + 1 <= _WEEKLY_GROWTH_CAP:
        updates.update(
            {
                "growth_window_start": growth_start,
                "growth_used": growth_used + 1,
            }
        )


def _intimacy_exceeds_stage(
    stage: str,
    tier: IntimacyTier,
) -> bool:
    try:
        ceiling = intimacy_ceiling_for_stage(stage)
    except ReplyContextError:
        ceiling = IntimacyTier.NONE
    return _INTIMACY_TIER_ORDER.index(tier) > _INTIMACY_TIER_ORDER.index(
        ceiling
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
    reason_code = event.kind.value.upper()
    if event.kind in {ReducerEventKind.BOUNDARY_RESPECTED, ReducerEventKind.SUPPORT_RECEIVED}:
        updates = {
            "trust": _bounded(snapshot.trust, 1),
            "comfort": _bounded(snapshot.comfort, 1),
        }
        growth_start, growth_used = _growth_window(
            snapshot,
            event.occurred_at,
        )
        if growth_used + 1 <= _WEEKLY_GROWTH_CAP:
            updates.update(
                {
                    "familiarity": _bounded(snapshot.familiarity, 1),
                    "growth_window_start": growth_start,
                    "growth_used": growth_used + 1,
                }
            )
        else:
            reason_code = "GROWTH_CAP_REACHED"
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
        if event.target_stage != snapshot.relationship_stage:
            updates.update(
                {
                    "closeness": _bounded(snapshot.closeness, 5),
                    "familiarity": _bounded(snapshot.familiarity, 3),
                }
            )
    elif event.kind is ReducerEventKind.INTIMACY_GRANTED:
        grant = event.intimacy_grant
        if grant is None:
            raise ReducerInputError("intimacy grant event is invalid")
        if _intimacy_exceeds_stage(snapshot.relationship_stage, grant.tier):
            return _no_change(snapshot, "INTIMACY_EXCEEDS_STAGE")
        if any(
            existing.grant_id == grant.grant_id
            for existing in snapshot.intimacy_grants
        ):
            return _no_change(snapshot, "INTIMACY_ALREADY_GRANTED")
        if len(snapshot.intimacy_grants) >= 16:
            return _no_change(snapshot, "INTIMACY_LIMIT_REACHED")
        updates = {
            "intimacy_grants": (*snapshot.intimacy_grants, grant),
        }
        growth_start, growth_used = _growth_window(
            snapshot,
            event.occurred_at,
        )
        if growth_used + 2 <= _WEEKLY_GROWTH_CAP:
            updates.update(
                {
                    "closeness": _bounded(snapshot.closeness, 2),
                    "growth_window_start": growth_start,
                    "growth_used": growth_used + 2,
                }
            )
        else:
            return _apply_updates(
                snapshot,
                updates,
                reason_code="GROWTH_CAP_REACHED",
            )
    elif event.kind is ReducerEventKind.CHARACTER_BOUNDARY_SET:
        boundary = event.boundary
        if boundary is None:
            raise ReducerInputError("boundary set event is invalid")
        boundaries = list(snapshot.active_boundaries)
        for index, existing in enumerate(boundaries):
            if existing.boundary_id == boundary.boundary_id:
                if existing == boundary:
                    return _no_change(snapshot, "BOUNDARY_UNCHANGED")
                boundaries[index] = boundary
                break
        else:
            if len(boundaries) >= 16:
                return _no_change(snapshot, "BOUNDARY_LIMIT_REACHED")
            boundaries.append(boundary)
        updates = {"active_boundaries": tuple(boundaries)}
        _add_growth_metadata(snapshot, event.occurred_at, updates)
    elif event.kind is ReducerEventKind.CHARACTER_BOUNDARY_WITHDRAWN:
        remaining = tuple(
            boundary
            for boundary in snapshot.active_boundaries
            if boundary.boundary_id != event.boundary_id
        )
        if len(remaining) == len(snapshot.active_boundaries):
            return _no_change(snapshot, "BOUNDARY_NOT_FOUND")
        updates = {"active_boundaries": remaining}
    elif event.kind is ReducerEventKind.CHARACTER_AFFECTION_ACKNOWLEDGED:
        affection = event.acknowledged_affection
        if affection is None:
            raise ReducerInputError("affection event is invalid")
        existing = snapshot.acknowledged_affection
        if existing is not None and _AFFECTION_INTENSITY_ORDER.index(
            affection.intensity
        ) <= _AFFECTION_INTENSITY_ORDER.index(existing.intensity):
            return _no_change(snapshot, "AFFECTION_NOT_STRONGER")
        updates = {"acknowledged_affection": affection}
        _add_growth_metadata(snapshot, event.occurred_at, updates)

    return _apply_updates(
        snapshot,
        updates,
        reason_code=reason_code,
        unchanged_reason=(
            "GROWTH_CAP_REACHED"
            if reason_code == "GROWTH_CAP_REACHED"
            else "BOUNDED_NO_CHANGE"
        ),
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
    elif isinstance(command, GrantIntimacy):
        return ReducerEvent(
            kind=ReducerEventKind.INTIMACY_GRANTED,
            occurred_at=command.occurred_at,
            semantic_key=command.idempotency_key,
            intimacy_grant=IntimacyGrant(
                grant_id=command.grant_id,
                tier=command.tier,
                statement=command.statement,
            ),
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

    if isinstance(command, ApplyHistoricalRelationshipEvidence):
        return _apply_updates(
            snapshot,
            {
                "familiarity": max(
                    snapshot.familiarity,
                    command.familiarity,
                ),
                "closeness": max(
                    snapshot.closeness,
                    command.closeness,
                ),
            },
            reason_code="APPLY_HISTORICAL_RELATIONSHIP_EVIDENCE",
            unchanged_reason="HISTORICAL_RELATIONSHIP_EVIDENCE_NO_CHANGE",
        )

    if isinstance(command, InitializeHistoricalRelationship):
        if snapshot != PrivateWorldSnapshot():
            return _no_change(snapshot, "HISTORY_ALREADY_INITIALIZED")
        initialized = _apply_updates(
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
        if initialized.delta.applied:
            return initialized
        return ReducerResult(
            replace(snapshot, version=snapshot.version + 1),
            ReducerDelta(True, "INITIALIZE_HISTORICAL_RELATIONSHIP"),
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
