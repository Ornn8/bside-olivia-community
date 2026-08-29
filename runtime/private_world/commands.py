"""Typed commands for controlled PrivateWorld state changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
)


__all__ = [
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
