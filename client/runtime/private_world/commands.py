"""Typed commands for controlled PrivateWorld state changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import re
from typing import ClassVar, TypeAlias

from runtime.reply.reply_context import IntimacyTier, RelationshipStage

from .port import (
    ContinuationAwareness,
    HomeAccess,
    IntimacyGrant,
    LocalContinuationFact,
    PrivateWorldError,
)


class PrivateWorldCommandError(ValueError):
    code = "PRIVATE_WORLD_COMMAND_INVALID"


class PrivateWorldActor(StrEnum):
    LOCAL_USER = "local_user"
    SYSTEM_CANDIDATE = "system_candidate"
    MIGRATION = "migration"


class PrivateWorldCommandSource(StrEnum):
    CONTROL_CENTER = "control_center"
    APPROVED_CANDIDATE = "approved_candidate"
    IMPORT = "import"
    MIGRATION = "migration"


class PrivateWorldCommandKind(StrEnum):
    RECORD_BOUNDARY_RESPECTED = "record_boundary_respected"
    RECORD_CONFLICT = "record_conflict"
    RECORD_REPAIR = "record_repair"
    CONFIRM_RELATIONSHIP_STAGE = "confirm_relationship_stage"
    GRANT_INTIMACY = "grant_intimacy"
    GRANT_NICKNAME = "grant_nickname"
    REVOKE_NICKNAME = "revoke_nickname"
    SET_HOME_ACCESS = "set_home_access"
    UPSERT_CONTINUATION_FACT = "upsert_continuation_fact"
    SET_CONTINUATION_AWARENESS = "set_continuation_awareness"
    DELETE_CONTINUATION_FACT = "delete_continuation_fact"
    INITIALIZE_HISTORICAL_RELATIONSHIP = "initialize_historical_relationship"
    APPLY_HISTORICAL_RELATIONSHIP_EVIDENCE = (
        "apply_historical_relationship_evidence"
    )


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_MAX_REASON_LENGTH = 280
_MAX_EVIDENCE_REFS = 8


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PrivateWorldCommandError(f"{field_name} is invalid")
    return value


def _plain_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise PrivateWorldCommandError(f"{field_name} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > max_length
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        raise PrivateWorldCommandError(f"{field_name} is invalid")
    return normalized


def _evidence_refs(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise PrivateWorldCommandError("evidence refs must be a sequence")
    if not isinstance(values, tuple):
        values = tuple(values)
    if (
        len(values) > _MAX_EVIDENCE_REFS
        or len(values) != len(set(values))
        or any(
            not isinstance(value, str) or not _ID_RE.fullmatch(value)
            for value in values
        )
    ):
        raise PrivateWorldCommandError("evidence refs are invalid")
    return values


_MAX_HISTORY_DAYS = 4000


def _calendar_days(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (tuple, list)):
        raise PrivateWorldCommandError("history days must be a sequence")
    days = tuple(values)
    for day in days:
        try:
            valid = isinstance(day, str) and len(day) == 10 and date.fromisoformat(day).isoformat() == day
        except ValueError:
            valid = False
        if not valid:
            raise PrivateWorldCommandError("history day is invalid")
    if len(days) > _MAX_HISTORY_DAYS or tuple(sorted(set(days))) != days:
        raise PrivateWorldCommandError("history days are invalid")
    return days


def _nickname(value: object) -> str:
    normalized = _plain_text(value, field_name="nickname", max_length=32)
    if any(character.isspace() for character in normalized):
        raise PrivateWorldCommandError("nickname cannot contain whitespace")
    return normalized


@dataclass(frozen=True, kw_only=True)
class PrivateWorldCommand:
    command_id: str
    idempotency_key: str
    actor: PrivateWorldActor
    source: PrivateWorldCommandSource
    occurred_at: datetime
    reason: str
    evidence_refs: tuple[str, ...] = ()

    kind: ClassVar[PrivateWorldCommandKind]

    def __post_init__(self) -> None:
        if self.__class__ is PrivateWorldCommand:
            raise TypeError("a concrete PrivateWorld command is required")
        object.__setattr__(
            self,
            "command_id",
            _identifier(self.command_id, field_name="command id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _identifier(self.idempotency_key, field_name="idempotency key"),
        )
        if not isinstance(self.actor, PrivateWorldActor):
            raise PrivateWorldCommandError("actor is invalid")
        if not isinstance(self.source, PrivateWorldCommandSource):
            raise PrivateWorldCommandError("source is invalid")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise PrivateWorldCommandError(
                "occurred_at must be timezone-aware"
            )
        object.__setattr__(
            self,
            "reason",
            _plain_text(
                self.reason,
                field_name="reason",
                max_length=_MAX_REASON_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _evidence_refs(self.evidence_refs),
        )

    def payload(self) -> dict[str, object]:
        return {}

    def to_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind.value,
            "actor": self.actor.value,
            "source": self.source.value,
            "occurred_at": self.occurred_at.isoformat(),
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "payload": self.payload(),
        }


@dataclass(frozen=True, kw_only=True)
class RecordBoundaryRespected(PrivateWorldCommand):
    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.RECORD_BOUNDARY_RESPECTED
    )


@dataclass(frozen=True, kw_only=True)
class RecordConflict(PrivateWorldCommand):
    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.RECORD_CONFLICT
    )


@dataclass(frozen=True, kw_only=True)
class RecordRepair(PrivateWorldCommand):
    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.RECORD_REPAIR
    )


@dataclass(frozen=True, kw_only=True)
class ConfirmRelationshipStage(PrivateWorldCommand):
    target_stage: RelationshipStage
    basis_event_ids: tuple[str, ...]

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.CONFIRM_RELATIONSHIP_STAGE
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.target_stage, RelationshipStage):
            raise PrivateWorldCommandError("target stage is invalid")
        basis = _evidence_refs(self.basis_event_ids)
        if not basis:
            raise PrivateWorldCommandError(
                "stage confirmation requires evidence"
            )
        object.__setattr__(self, "basis_event_ids", basis)

    def payload(self) -> dict[str, object]:
        return {
            "target_stage": self.target_stage.value,
            "basis_event_ids": list(self.basis_event_ids),
        }


@dataclass(frozen=True, kw_only=True)
class GrantIntimacy(PrivateWorldCommand):
    grant_id: str
    tier: IntimacyTier
    statement: str

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.GRANT_INTIMACY
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        try:
            grant = IntimacyGrant(
                grant_id=self.grant_id,
                tier=self.tier,
                statement=self.statement,
            )
        except PrivateWorldError as exc:
            raise PrivateWorldCommandError(str(exc)) from exc
        if grant.tier is IntimacyTier.NONE:
            raise PrivateWorldCommandError(
                "intimacy grant tier must grant contact"
            )
        object.__setattr__(self, "grant_id", grant.grant_id)
        object.__setattr__(self, "tier", grant.tier)
        object.__setattr__(self, "statement", grant.statement)

    def payload(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "tier": self.tier.value,
            "statement": self.statement,
        }


@dataclass(frozen=True, kw_only=True)
class InitializeHistoricalRelationship(PrivateWorldCommand):
    """Set one bounded baseline from user-authorized ordered imported exchanges."""

    relationship_stage: RelationshipStage
    familiarity: int
    trust: int
    comfort: int
    closeness: int
    tension: int

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.INITIALIZE_HISTORICAL_RELATIONSHIP
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.relationship_stage, RelationshipStage):
            raise PrivateWorldCommandError("relationship stage is invalid")
        for field_name in ("familiarity", "trust", "comfort", "closeness", "tension"):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= 100:
                raise PrivateWorldCommandError(f"{field_name} is invalid")
        if not self.evidence_refs:
            raise PrivateWorldCommandError("historical initialization requires evidence")

    def payload(self) -> dict[str, object]:
        return {
            "relationship_stage": self.relationship_stage.value,
            "familiarity": self.familiarity,
            "trust": self.trust,
            "comfort": self.comfort,
            "closeness": self.closeness,
            "tension": self.tension,
        }


@dataclass(frozen=True, kw_only=True)
class ApplyHistoricalRelationshipEvidence(PrivateWorldCommand):
    """Apply an assessed corpus; optional axes preserve legacy command decoding."""

    familiarity: int
    closeness: int
    trust: int | None = None
    comfort: int | None = None
    tension: int | None = None
    relationship_stage: RelationshipStage | None = None
    # Calendar days (UTC+8) on which the assessed exchanges took place, only
    # those with a known time. They count as days the two talked.
    history_days: tuple[str, ...] = ()

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.APPLY_HISTORICAL_RELATIONSHIP_EVIDENCE
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "history_days", _calendar_days(self.history_days))
        for field_name in ("familiarity", "closeness"):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= 100:
                raise PrivateWorldCommandError(f"{field_name} is invalid")
        axes = (self.trust, self.comfort, self.tension)
        if self.relationship_stage is not None and (not isinstance(self.relationship_stage, RelationshipStage) or self.relationship_stage is RelationshipStage.COMMITTED):
            raise PrivateWorldCommandError('historical relationship stage is invalid')
        if axes != (None, None, None) and any(type(v) is not int or not 0 <= v <= 100 for v in axes):
            raise PrivateWorldCommandError("historical assessment axes must be complete")
        if not self.evidence_refs:
            raise PrivateWorldCommandError(
                "historical relationship evidence is required"
            )

    def payload(self) -> dict[str, object]:
        return {
            "familiarity": self.familiarity,
            "closeness": self.closeness,
            **({"trust": self.trust, "comfort": self.comfort, "tension": self.tension}
               if self.trust is not None else {}),
            **({'relationship_stage': self.relationship_stage.value} if self.relationship_stage is not None else {}),
            **({'history_days': list(self.history_days)} if self.history_days else {}),
        }


@dataclass(frozen=True, kw_only=True)
class GrantNickname(PrivateWorldCommand):
    nickname: str

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.GRANT_NICKNAME
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "nickname", _nickname(self.nickname))

    def payload(self) -> dict[str, object]:
        return {"nickname": self.nickname}


@dataclass(frozen=True, kw_only=True)
class RevokeNickname(PrivateWorldCommand):
    nickname: str

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.REVOKE_NICKNAME
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "nickname", _nickname(self.nickname))

    def payload(self) -> dict[str, object]:
        return {"nickname": self.nickname}


@dataclass(frozen=True, kw_only=True)
class SetHomeAccess(PrivateWorldCommand):
    home_access: HomeAccess

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.SET_HOME_ACCESS
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.home_access, HomeAccess):
            raise PrivateWorldCommandError("home access is invalid")

    def payload(self) -> dict[str, object]:
        return {"home_access": self.home_access.value}


@dataclass(frozen=True, kw_only=True)
class UpsertContinuationFact(PrivateWorldCommand):
    fact_id: str
    statement: str
    awareness: ContinuationAwareness

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.UPSERT_CONTINUATION_FACT
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        try:
            fact = LocalContinuationFact(
                self.fact_id,
                self.statement,
                self.awareness,
            )
        except PrivateWorldError as exc:
            raise PrivateWorldCommandError(str(exc)) from exc
        object.__setattr__(self, "fact_id", fact.fact_id)
        object.__setattr__(self, "statement", fact.statement)
        object.__setattr__(self, "awareness", fact.awareness)

    def payload(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "awareness": self.awareness.value,
        }


@dataclass(frozen=True, kw_only=True)
class SetContinuationAwareness(PrivateWorldCommand):
    fact_id: str
    awareness: ContinuationAwareness

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.SET_CONTINUATION_AWARENESS
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "fact_id",
            _identifier(
                self.fact_id,
                field_name="continuation fact id",
            ),
        )
        if not isinstance(self.awareness, ContinuationAwareness):
            raise PrivateWorldCommandError(
                "continuation awareness is invalid"
            )

    def payload(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "awareness": self.awareness.value,
        }


@dataclass(frozen=True, kw_only=True)
class DeleteContinuationFact(PrivateWorldCommand):
    fact_id: str

    kind: ClassVar[PrivateWorldCommandKind] = (
        PrivateWorldCommandKind.DELETE_CONTINUATION_FACT
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "fact_id",
            _identifier(
                self.fact_id,
                field_name="continuation fact id",
            ),
        )

    def payload(self) -> dict[str, object]:
        return {"fact_id": self.fact_id}


def command_from_record(record: dict) -> PrivateWorldCommand:
    """Decode a previously authorized command, without authorizing new input."""
    kinds = (RecordBoundaryRespected, RecordConflict, RecordRepair,
             ConfirmRelationshipStage, GrantIntimacy, InitializeHistoricalRelationship,
             ApplyHistoricalRelationshipEvidence, GrantNickname, RevokeNickname,
             SetHomeAccess, UpsertContinuationFact, SetContinuationAwareness,
             DeleteContinuationFact)
    constructors = {item.kind.value: item for item in kinds}
    value = dict(record)
    constructor = constructors[value.pop('kind')]
    payload = value.pop('payload')
    if set(value) != {'command_id', 'idempotency_key', 'actor', 'source',
                      'occurred_at', 'reason', 'evidence_refs'} or not isinstance(payload, dict):
        raise PrivateWorldCommandError('stored command is invalid')
    if set(payload) & set(value):
        raise PrivateWorldCommandError('stored command fields overlap')
    value.update(payload)
    value['occurred_at'] = datetime.fromisoformat(value['occurred_at'])
    for key, enum in (('actor', PrivateWorldActor), ('source', PrivateWorldCommandSource),
                      ('target_stage', RelationshipStage), ('relationship_stage', RelationshipStage),
                      ('tier', IntimacyTier), ('home_access', HomeAccess),
                      ('awareness', ContinuationAwareness)):
        if value.get(key) is not None:
            value[key] = enum(value[key])
    result = constructor(**value)
    if result.to_dict() != record:
        raise PrivateWorldCommandError('stored command is not canonical')
    return result


PrivateWorldMutation: TypeAlias = (
    RecordBoundaryRespected
    | RecordConflict
    | RecordRepair
    | ConfirmRelationshipStage
    | GrantIntimacy
    | GrantNickname
    | RevokeNickname
    | SetHomeAccess
    | UpsertContinuationFact
    | SetContinuationAwareness
    | DeleteContinuationFact
    | InitializeHistoricalRelationship
    | ApplyHistoricalRelationshipEvidence
)


__all__ = [
    "ApplyHistoricalRelationshipEvidence",
    "ConfirmRelationshipStage",
    "DeleteContinuationFact",
    "GrantIntimacy",
    "GrantNickname",
    "InitializeHistoricalRelationship",
    "PrivateWorldActor",
    "PrivateWorldCommand",
    "PrivateWorldCommandError",
    "PrivateWorldCommandKind",
    "PrivateWorldCommandSource",
    "PrivateWorldMutation",
    "RecordBoundaryRespected",
    "RecordConflict",
    "RecordRepair",
    "RevokeNickname",
    "SetContinuationAwareness",
    "SetHomeAccess",
    "UpsertContinuationFact",
]
