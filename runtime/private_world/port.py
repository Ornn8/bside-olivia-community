"""Provider-free contracts for private relationship and local-continuation state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable
import re

from runtime.reply.reply_context import IntimacyTier


class PrivateWorldError(ValueError):
    code = "PRIVATE_WORLD_INVALID"


class HomeAccess(str, Enum):
    NO_ACCESS = "no_access"
    VISIT_ACCESS = "visit_access"
    ERRAND_ACCESS = "errand_access"
    DOMESTIC_ACCESS = "domestic_access"


class ContinuationAwareness(str, Enum):
    CONTROL_ONLY = "control_only"
    CHARACTER_KNOWN = "character_known"
    PENDING = "pending"


_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:Z|\+00:00)$"
)
_HIDDEN_NAMES = ("familiarity", "trust", "comfort", "closeness", "tension")
_INTIMACY_TIER_ORDER = (
    IntimacyTier.NONE,
    IntimacyTier.LIGHT_CONTACT,
    IntimacyTier.CLOSE_CONTACT,
)


def _validate_score(name: str, value: int) -> None:
    if type(value) is not int or not 0 <= value <= 100:
        raise PrivateWorldError(f"{name} must be an integer from 0 to 100")


def _plain_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise PrivateWorldError(f"{field_name} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise PrivateWorldError(f"{field_name} is invalid")
    return normalized


def _validate_nicknames(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise PrivateWorldError("nickname permissions must be a sequence")
    if not isinstance(values, tuple):
        values = tuple(values)
    normalized = tuple(
        _plain_text(value, field_name="nickname", max_length=32) for value in values
    )
    if any(any(character.isspace() for character in value) for value in normalized):
        raise PrivateWorldError("nickname permissions cannot contain whitespace")
    if len(normalized) > 16 or len(set(normalized)) != len(normalized):
        raise PrivateWorldError("nickname permissions must be unique and bounded")
    return normalized


@dataclass(frozen=True)
class LocalContinuationFact:
    fact_id: str
    statement: str
    awareness: ContinuationAwareness = ContinuationAwareness.PENDING

    def __post_init__(self) -> None:
        if not isinstance(self.fact_id, str) or not _TOKEN_RE.fullmatch(self.fact_id):
            raise PrivateWorldError("continuation fact id is invalid")
        object.__setattr__(
            self,
            "statement",
            _plain_text(
                self.statement,
                field_name="continuation statement",
                max_length=600,
            ),
        )
        if not isinstance(self.awareness, ContinuationAwareness):
            raise PrivateWorldError("continuation awareness is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "awareness": self.awareness.value,
        }


@dataclass(frozen=True)
class IntimacyGrant:
    grant_id: str
    tier: IntimacyTier
    statement: str

    def __post_init__(self) -> None:
        if not isinstance(self.grant_id, str) or not _TOKEN_RE.fullmatch(
            self.grant_id
        ):
            raise PrivateWorldError("intimacy grant id is invalid")
        if not isinstance(self.tier, IntimacyTier):
            raise PrivateWorldError("intimacy grant tier is invalid")
        object.__setattr__(
            self,
            "statement",
            _plain_text(
                self.statement,
                field_name="intimacy grant statement",
                max_length=200,
            ),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "grant_id": self.grant_id,
            "tier": self.tier.value,
            "statement": self.statement,
        }


def _validate_facts(
    values: tuple[LocalContinuationFact, ...],
) -> tuple[LocalContinuationFact, ...]:
    if isinstance(values, (str, bytes)):
        raise PrivateWorldError("continuation facts must be a sequence")
    if not isinstance(values, tuple):
        values = tuple(values)
    if len(values) > 32 or any(
        not isinstance(value, LocalContinuationFact) for value in values
    ):
        raise PrivateWorldError("continuation facts must be typed and bounded")
    identifiers = tuple(value.fact_id for value in values)
    if len(set(identifiers)) != len(identifiers):
        raise PrivateWorldError("continuation fact ids must be unique")
    return values


def _validate_grants(
    values: tuple[IntimacyGrant, ...],
) -> tuple[IntimacyGrant, ...]:
    if isinstance(values, (str, bytes)):
        raise PrivateWorldError("intimacy grants must be a sequence")
    if not isinstance(values, tuple):
        values = tuple(values)
    if len(values) > 16 or any(
        not isinstance(value, IntimacyGrant) for value in values
    ):
        raise PrivateWorldError("intimacy grants must be typed and bounded")
    identifiers = tuple(value.grant_id for value in values)
    if len(set(identifiers)) != len(identifiers):
        raise PrivateWorldError("intimacy grant ids must be unique")
    return values


def _validate_growth_window_start(value: str) -> str:
    if value == "":
        return value
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _UTC_TIMESTAMP_RE.fullmatch(value)
    ):
        raise PrivateWorldError("growth window start is invalid")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateWorldError("growth window start is invalid") from exc
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise PrivateWorldError("growth window start must be UTC")
    return value


def _validate_shared(
    relationship_stage: str,
    nickname_permissions: tuple[str, ...],
    home_access: HomeAccess,
    continuation_awareness: ContinuationAwareness,
    continuation_facts: tuple[LocalContinuationFact, ...],
) -> tuple[tuple[str, ...], tuple[LocalContinuationFact, ...]]:
    if not isinstance(relationship_stage, str) or not _TOKEN_RE.fullmatch(
        relationship_stage
    ):
        raise PrivateWorldError("relationship stage is invalid")
    nicknames = _validate_nicknames(nickname_permissions)
    facts = _validate_facts(continuation_facts)
    if not isinstance(home_access, HomeAccess):
        raise PrivateWorldError("home access is invalid")
    if not isinstance(continuation_awareness, ContinuationAwareness):
        raise PrivateWorldError("continuation awareness is invalid")
    return nicknames, facts


@dataclass(frozen=True)
class PrivateWorldControlView:
    familiarity: int
    trust: int
    comfort: int
    closeness: int
    tension: int
    relationship_stage: str
    nickname_permissions: tuple[str, ...]
    home_access: HomeAccess
    continuation_awareness: ContinuationAwareness
    continuation_facts: tuple[LocalContinuationFact, ...] = ()
    intimacy_grants: tuple[IntimacyGrant, ...] = ()
    growth_window_start: str = ""
    growth_used: int = 0

    def __post_init__(self) -> None:
        for name in _HIDDEN_NAMES:
            _validate_score(name, getattr(self, name))
        nicknames, facts = _validate_shared(
            self.relationship_stage,
            self.nickname_permissions,
            self.home_access,
            self.continuation_awareness,
            self.continuation_facts,
        )
        object.__setattr__(self, "nickname_permissions", nicknames)
        object.__setattr__(self, "continuation_facts", facts)
        object.__setattr__(
            self,
            "intimacy_grants",
            _validate_grants(self.intimacy_grants),
        )
        object.__setattr__(
            self,
            "growth_window_start",
            _validate_growth_window_start(self.growth_window_start),
        )
        if type(self.growth_used) is not int or not 0 <= self.growth_used <= 255:
            raise PrivateWorldError("growth used must be an integer from 0 to 255")

    def to_dict(self) -> dict[str, object]:
        return {
            "view": "control",
            **{name: getattr(self, name) for name in _HIDDEN_NAMES},
            "relationship_stage": self.relationship_stage,
            "nickname_permissions": list(self.nickname_permissions),
            "home_access": self.home_access.value,
            "continuation_awareness": self.continuation_awareness.value,
            "continuation_facts": [
                fact.to_dict() for fact in self.continuation_facts
            ],
            "intimacy_grants": [
                grant.to_dict() for grant in self.intimacy_grants
            ],
            "growth_window_start": self.growth_window_start,
            "growth_used": self.growth_used,
        }


@dataclass(frozen=True)
class PrivateWorldCharacterView:
    relationship_stage: str
    nickname_permissions: tuple[str, ...]
    home_history_allowed: bool
    continuation_known: bool
    continuation_facts: tuple[LocalContinuationFact, ...] = ()
    granted_intimacy: IntimacyTier = IntimacyTier.NONE

    def __post_init__(self) -> None:
        if not isinstance(self.relationship_stage, str) or not _TOKEN_RE.fullmatch(
            self.relationship_stage
        ):
            raise PrivateWorldError("relationship stage is invalid")
        object.__setattr__(
            self,
            "nickname_permissions",
            _validate_nicknames(self.nickname_permissions),
        )
        if type(self.home_history_allowed) is not bool:
            raise PrivateWorldError("home history permission must be boolean")
        if type(self.continuation_known) is not bool:
            raise PrivateWorldError("continuation known must be boolean")
        facts = _validate_facts(self.continuation_facts)
        if any(
            fact.awareness is not ContinuationAwareness.CHARACTER_KNOWN
            for fact in facts
        ):
            raise PrivateWorldError(
                "character view may contain only character-known continuation facts"
            )
        object.__setattr__(self, "continuation_facts", facts)
        if not isinstance(self.granted_intimacy, IntimacyTier):
            raise PrivateWorldError("granted intimacy is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "view": "character",
            "relationship_stage": self.relationship_stage,
            "granted_intimacy": self.granted_intimacy.value,
            "nickname_permissions": list(self.nickname_permissions),
            "home_history_allowed": self.home_history_allowed,
            "continuation_known": self.continuation_known,
            "continuation_facts": [
                fact.to_dict() for fact in self.continuation_facts
            ],
        }


@dataclass(frozen=True)
class PrivateWorldSnapshot:
    version: int = 1
    familiarity: int = 0
    trust: int = 0
    comfort: int = 0
    closeness: int = 0
    tension: int = 0
    relationship_stage: str = "unknown"
    nickname_permissions: tuple[str, ...] = ()
    home_access: HomeAccess = HomeAccess.NO_ACCESS
    continuation_awareness: ContinuationAwareness = ContinuationAwareness.CONTROL_ONLY
    continuation_facts: tuple[LocalContinuationFact, ...] = ()
    intimacy_grants: tuple[IntimacyGrant, ...] = ()
    growth_window_start: str = ""
    growth_used: int = 0

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise PrivateWorldError("snapshot version must be positive")
        for name in _HIDDEN_NAMES:
            _validate_score(name, getattr(self, name))
        nicknames, facts = _validate_shared(
            self.relationship_stage,
            self.nickname_permissions,
            self.home_access,
            self.continuation_awareness,
            self.continuation_facts,
        )
        object.__setattr__(self, "nickname_permissions", nicknames)
        object.__setattr__(self, "continuation_facts", facts)
        object.__setattr__(
            self,
            "intimacy_grants",
            _validate_grants(self.intimacy_grants),
        )
        object.__setattr__(
            self,
            "growth_window_start",
            _validate_growth_window_start(self.growth_window_start),
        )
        if type(self.growth_used) is not int or not 0 <= self.growth_used <= 255:
            raise PrivateWorldError("growth used must be an integer from 0 to 255")

    def control_view(self) -> PrivateWorldControlView:
        return PrivateWorldControlView(
            familiarity=self.familiarity,
            trust=self.trust,
            comfort=self.comfort,
            closeness=self.closeness,
            tension=self.tension,
            relationship_stage=self.relationship_stage,
            nickname_permissions=self.nickname_permissions,
            home_access=self.home_access,
            continuation_awareness=self.continuation_awareness,
            continuation_facts=self.continuation_facts,
            intimacy_grants=self.intimacy_grants,
            growth_window_start=self.growth_window_start,
            growth_used=self.growth_used,
        )

    def character_view(self) -> PrivateWorldCharacterView:
        known_facts = tuple(
            fact
            for fact in self.continuation_facts
            if fact.awareness is ContinuationAwareness.CHARACTER_KNOWN
        )
        continuation_known = (
            self.continuation_awareness
            is ContinuationAwareness.CHARACTER_KNOWN
            or bool(known_facts)
        )
        granted_intimacy = max(
            (grant.tier for grant in self.intimacy_grants),
            key=_INTIMACY_TIER_ORDER.index,
            default=IntimacyTier.NONE,
        )
        return PrivateWorldCharacterView(
            relationship_stage=self.relationship_stage,
            nickname_permissions=self.nickname_permissions,
            home_history_allowed=self.home_access is not HomeAccess.NO_ACCESS,
            continuation_known=continuation_known,
            continuation_facts=known_facts,
            granted_intimacy=granted_intimacy,
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.control_view().to_dict()
        payload["view"] = "snapshot"
        return {"version": self.version, **payload}


@runtime_checkable
class PrivateWorldPort(Protocol):
    def snapshot(self) -> PrivateWorldSnapshot: ...

    def control_view(self) -> PrivateWorldControlView: ...

    def character_view(self) -> PrivateWorldCharacterView: ...


class NullPrivateWorldPort:
    """Disabled adapter with no persistence, network, or mutation surface."""

    _snapshot = PrivateWorldSnapshot()

    def snapshot(self) -> PrivateWorldSnapshot:
        return self._snapshot

    def control_view(self) -> PrivateWorldControlView:
        return self._snapshot.control_view()

    def character_view(self) -> PrivateWorldCharacterView:
        return self._snapshot.character_view()
