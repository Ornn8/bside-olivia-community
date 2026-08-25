"""Typed, provider-free inputs for the reply pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re


class ReplyContextError(ValueError):
    code = "REPLY_CONTEXT_INVALID"


class UnsupportedReplyMode(ReplyContextError):
    code = "REPLY_MODE_UNSUPPORTED"


class ReplyMode(str, Enum):
    TEXT_LETTER = "text_letter"
    SPOKEN_VIDEO = "spoken_video"
    MUSICAL_VIDEO = "musical_video"
    FUTURE_IM = "future_im"


class OutputChannel(str, Enum):
    LETTER = "letter"
    SPOKEN_TEXT = "spoken_text"
    INSTANT_MESSAGE = "instant_message"


class WorldFactKind(str, Enum):
    PUBLIC_CANON = "public_canon"
    TRUSTED_RUNTIME = "trusted_runtime"


class BehaviorLevel(str, Enum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RelationshipStage(str, Enum):
    UNKNOWN = "unknown"
    ACQUAINTANCE = "acquaintance"
    FAMILIAR = "familiar"
    CLOSE = "close"


class NicknamePermission(str, Enum):
    NOT_ALLOWED = "not_allowed"
    ALLOWED = "allowed"


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class TrustedTime:
    instant: datetime
    source: str = "system_clock"

    def __post_init__(self) -> None:
        if self.instant.tzinfo is None or self.instant.utcoffset() is None:
            raise ReplyContextError("trusted time must be timezone-aware")
        if not isinstance(self.source, str) or not _ID_RE.fullmatch(
            self.source.strip()
        ):
            raise ReplyContextError(
                "trusted time source must be a stable identifier"
            )
        object.__setattr__(
            self,
            "instant",
            self.instant.astimezone(timezone.utc),
        )
        object.__setattr__(self, "source", self.source.strip())


@dataclass(frozen=True)
class TrustedWorldFact:
    fact_id: str
    source_id: str
    statement: str
    kind: WorldFactKind = WorldFactKind.PUBLIC_CANON

    def __post_init__(self) -> None:
        for value in (self.fact_id, self.source_id):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise ReplyContextError(
                    "world fact identifiers must be stable"
                )
        if (
            not isinstance(self.statement, str)
            or not self.statement.strip()
            or len(self.statement) > 600
            or _CONTROL_RE.search(self.statement)
        ):
            raise ReplyContextError("world fact statement is invalid")
        if not isinstance(self.kind, WorldFactKind):
            raise ReplyContextError("world fact kind is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "fact_id": self.fact_id,
            "source_id": self.source_id,
            "statement": self.statement,
            "kind": self.kind.value,
        }


@dataclass(frozen=True)
class KnownContinuationFact:
    fact_id: str
    statement: str

    def __post_init__(self) -> None:
        if not isinstance(self.fact_id, str) or not _ID_RE.fullmatch(
            self.fact_id
        ):
            raise ReplyContextError(
                "continuation fact identifier must be stable"
            )
        if (
            not isinstance(self.statement, str)
            or not self.statement.strip()
            or len(self.statement) > 600
            or _CONTROL_RE.search(self.statement)
        ):
            raise ReplyContextError(
                "continuation fact statement is invalid"
            )
        object.__setattr__(self, "statement", self.statement.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
        }


@dataclass(frozen=True)
class PrivateBehaviorView:
    familiarity: BehaviorLevel = BehaviorLevel.UNKNOWN
    trust: BehaviorLevel = BehaviorLevel.UNKNOWN
    comfort: BehaviorLevel = BehaviorLevel.UNKNOWN
    closeness: BehaviorLevel = BehaviorLevel.UNKNOWN
    tension: BehaviorLevel = BehaviorLevel.UNKNOWN
    relationship_stage: RelationshipStage = RelationshipStage.UNKNOWN
    nickname_permission: NicknamePermission = NicknamePermission.NOT_ALLOWED
    home_history_allowed: bool = False
    known_continuations: tuple[KnownContinuationFact, ...] = ()

    def __post_init__(self) -> None:
        expected_types = (
            (self.familiarity, BehaviorLevel),
            (self.trust, BehaviorLevel),
            (self.comfort, BehaviorLevel),
            (self.closeness, BehaviorLevel),
            (self.tension, BehaviorLevel),
            (self.relationship_stage, RelationshipStage),
            (self.nickname_permission, NicknamePermission),
        )
        if any(
            not isinstance(value, expected)
            for value, expected in expected_types
        ):
            raise ReplyContextError(
                "private behavior is outside the bounded view"
            )
        if type(self.home_history_allowed) is not bool:
            raise ReplyContextError(
                "home history permission must be boolean"
            )
        if isinstance(self.known_continuations, (str, bytes)):
            raise ReplyContextError(
                "known continuations must be a typed sequence"
            )
        facts = tuple(self.known_continuations)
        if (
            len(facts) > 32
            or any(
                not isinstance(fact, KnownContinuationFact)
                for fact in facts
            )
            or len({fact.fact_id for fact in facts}) != len(facts)
        ):
            raise ReplyContextError(
                "known continuations must be typed, unique, and bounded"
            )
        object.__setattr__(self, "known_continuations", facts)

    def to_dict(self) -> dict[str, object]:
        return {
            "familiarity": self.familiarity.value,
            "trust": self.trust.value,
            "comfort": self.comfort.value,
            "closeness": self.closeness.value,
            "tension": self.tension.value,
            "relationship_stage": self.relationship_stage.value,
            "nickname_permission": self.nickname_permission.value,
            "home_history_allowed": self.home_history_allowed,
            "known_continuations": [
                fact.to_dict() for fact in self.known_continuations
            ],
        }


@dataclass(frozen=True)
class OutputConstraints:
    channel: OutputChannel
    max_characters: int = 1_200
    plain_text_only: bool = True
    allow_stage_directions: bool = False
    allow_control_markup: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.channel, OutputChannel):
            raise ReplyContextError("output channel is invalid")
        if type(self.max_characters) is not int or self.max_characters < 1:
            raise ReplyContextError("max_characters must be positive")
        if (
            type(self.plain_text_only) is not bool
            or not self.plain_text_only
        ):
            raise ReplyContextError("reply output must be plain text")
        if type(self.allow_stage_directions) is not bool:
            raise ReplyContextError(
                "allow_stage_directions must be boolean"
            )
        if (
            type(self.allow_control_markup) is not bool
            or self.allow_control_markup
        ):
            raise ReplyContextError("control markup is not allowed")
        if (
            self.channel is OutputChannel.SPOKEN_TEXT
            and self.allow_stage_directions
        ):
            raise ReplyContextError(
                "spoken text cannot contain stage directions"
            )

    @classmethod
    def for_mode(cls, mode: ReplyMode) -> "OutputConstraints":
        if mode is ReplyMode.TEXT_LETTER:
            return cls(OutputChannel.LETTER)
        if mode in {ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO}:
            return cls(OutputChannel.SPOKEN_TEXT)
        if mode is ReplyMode.FUTURE_IM:
            return cls(OutputChannel.INSTANT_MESSAGE)
        raise UnsupportedReplyMode()

    def to_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel.value,
            "max_characters": self.max_characters,
            "plain_text_only": self.plain_text_only,
            "allow_stage_directions": self.allow_stage_directions,
            "allow_control_markup": self.allow_control_markup,
        }


class ReplyModeAdapter:
    def from_wire(self, value: str) -> ReplyMode:
        if value == "text":
            return ReplyMode.TEXT_LETTER
        if value == "video":
            return ReplyMode.SPOKEN_VIDEO
        raise UnsupportedReplyMode()

    def to_wire(self, mode: ReplyMode) -> str:
        if mode is ReplyMode.TEXT_LETTER:
            return "text"
        if mode in {ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO}:
            return "video"
        raise UnsupportedReplyMode()


@dataclass(frozen=True)
class ReplyContext:
    mode: ReplyMode
    trusted_time: TrustedTime
    world_facts: tuple[TrustedWorldFact, ...] = ()
    private_behavior: PrivateBehaviorView = field(
        default_factory=PrivateBehaviorView
    )
    output_constraints: OutputConstraints = field(
        default_factory=lambda: OutputConstraints.for_mode(
            ReplyMode.TEXT_LETTER
        )
    )
    future_im_enabled: bool = False

    @classmethod
    def create(
        cls,
        mode: ReplyMode,
        *,
        trusted_time: TrustedTime,
        world_facts: tuple[TrustedWorldFact, ...] = (),
        private_behavior: PrivateBehaviorView | None = None,
        output_constraints: OutputConstraints | None = None,
        future_im_enabled: bool = False,
    ) -> "ReplyContext":
        if mode is ReplyMode.FUTURE_IM and not future_im_enabled:
            raise UnsupportedReplyMode()
        constraints = output_constraints or OutputConstraints.for_mode(mode)
        if (
            constraints.channel
            is not OutputConstraints.for_mode(mode).channel
        ):
            raise ReplyContextError(
                "output channel does not match reply mode"
            )
        facts = tuple(world_facts)
        if any(
            not isinstance(fact, TrustedWorldFact) for fact in facts
        ):
            raise ReplyContextError(
                "world facts must use the trusted fact type"
            )
        if len({fact.fact_id for fact in facts}) != len(facts):
            raise ReplyContextError(
                "world fact identifiers must be unique"
            )
        return cls(
            mode,
            trusted_time,
            facts,
            private_behavior or PrivateBehaviorView(),
            constraints,
            future_im_enabled,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ReplyMode):
            raise UnsupportedReplyMode()
        if not isinstance(self.trusted_time, TrustedTime):
            raise ReplyContextError("trusted time is required")
        if type(self.future_im_enabled) is not bool:
            raise ReplyContextError(
                "future_im_enabled must be boolean"
            )
        if (
            self.mode is ReplyMode.FUTURE_IM
            and not self.future_im_enabled
        ):
            raise UnsupportedReplyMode()
        if not isinstance(self.world_facts, tuple):
            object.__setattr__(
                self,
                "world_facts",
                tuple(self.world_facts),
            )
        if any(
            not isinstance(fact, TrustedWorldFact)
            for fact in self.world_facts
        ):
            raise ReplyContextError(
                "world facts must use the trusted fact type"
            )
        if (
            len({fact.fact_id for fact in self.world_facts})
            != len(self.world_facts)
        ):
            raise ReplyContextError(
                "world fact identifiers must be unique"
            )
        if not isinstance(
            self.private_behavior,
            PrivateBehaviorView,
        ):
            raise ReplyContextError(
                "private behavior view is invalid"
            )
        if not isinstance(
            self.output_constraints,
            OutputConstraints,
        ):
            raise ReplyContextError(
                "output constraints are invalid"
            )
        expected_channel = OutputConstraints.for_mode(
            self.mode
        ).channel
        if self.output_constraints.channel is not expected_channel:
            raise ReplyContextError(
                "output channel does not match reply mode"
            )

    def to_dict(self) -> dict[str, object]:
        try:
            wire_mode: str | None = ReplyModeAdapter().to_wire(
                self.mode
            )
        except UnsupportedReplyMode:
            wire_mode = None
        return {
            "mode": self.mode.value,
            "wire_mode": wire_mode,
            "trusted_time": {
                "instant": self.trusted_time.instant.isoformat(),
                "source": self.trusted_time.source,
            },
            "world_facts": [
                fact.to_dict() for fact in self.world_facts
            ],
            "private_behavior": self.private_behavior.to_dict(),
            "output_constraints": (
                self.output_constraints.to_dict()
            ),
        }
