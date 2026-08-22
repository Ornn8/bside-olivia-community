"""Provider-free contracts for private relationship state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable
import re


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
_HIDDEN_NAMES = ("familiarity", "trust", "comfort", "closeness", "tension")


def _validate_score(name: str, value: int) -> None:
    if type(value) is not int or not 0 <= value <= 100:
        raise PrivateWorldError(f"{name} must be an integer from 0 to 100")


def _validate_tokens(values: tuple[str, ...]) -> None:
    if len(values) > 16 or len(set(values)) != len(values):
        raise PrivateWorldError("nickname permissions must be unique and bounded")
    if any(not isinstance(value, str) or not _TOKEN_RE.fullmatch(value) for value in values):
        raise PrivateWorldError("nickname permission is invalid")


def _validate_shared(
    relationship_stage: str,
    nickname_permissions: tuple[str, ...],
    home_access: HomeAccess,
    continuation_awareness: ContinuationAwareness,
) -> None:
    if not isinstance(relationship_stage, str) or not _TOKEN_RE.fullmatch(
        relationship_stage
    ):
        raise PrivateWorldError("relationship stage is invalid")
    _validate_tokens(nickname_permissions)
    if not isinstance(home_access, HomeAccess):
        raise PrivateWorldError("home access is invalid")
    if not isinstance(continuation_awareness, ContinuationAwareness):
        raise PrivateWorldError("continuation awareness is invalid")


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

    def __post_init__(self) -> None:
        for name in _HIDDEN_NAMES:
            _validate_score(name, getattr(self, name))
        _validate_shared(
            self.relationship_stage,
            self.nickname_permissions,
            self.home_access,
            self.continuation_awareness,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "view": "control",
            **{name: getattr(self, name) for name in _HIDDEN_NAMES},
            "relationship_stage": self.relationship_stage,
            "nickname_permissions": list(self.nickname_permissions),
            "home_access": self.home_access.value,
            "continuation_awareness": self.continuation_awareness.value,
        }


@dataclass(frozen=True)
class PrivateWorldCharacterView:
    relationship_stage: str
    nickname_permissions: tuple[str, ...]
    home_access: HomeAccess
    continuation_awareness: ContinuationAwareness

    def __post_init__(self) -> None:
        _validate_shared(
            self.relationship_stage,
            self.nickname_permissions,
            self.home_access,
            self.continuation_awareness,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "view": "character",
            "relationship_stage": self.relationship_stage,
            "nickname_permissions": list(self.nickname_permissions),
            "home_access": self.home_access.value,
            "continuation_awareness": self.continuation_awareness.value,
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

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise PrivateWorldError("snapshot version must be positive")
        for name in _HIDDEN_NAMES:
            _validate_score(name, getattr(self, name))
        if not isinstance(self.nickname_permissions, tuple):
            object.__setattr__(self, "nickname_permissions", tuple(self.nickname_permissions))
        _validate_shared(
            self.relationship_stage,
            self.nickname_permissions,
            self.home_access,
            self.continuation_awareness,
        )

    def control_view(self) -> PrivateWorldControlView:
        return PrivateWorldControlView(
            *(getattr(self, name) for name in _HIDDEN_NAMES),
            self.relationship_stage,
            self.nickname_permissions,
            self.home_access,
            self.continuation_awareness,
        )

    def character_view(self) -> PrivateWorldCharacterView:
        return PrivateWorldCharacterView(
            self.relationship_stage,
            self.nickname_permissions,
            self.home_access,
            self.continuation_awareness,
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
