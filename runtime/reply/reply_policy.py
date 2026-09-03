"""Deterministic checks for reply text and typed policy evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from runtime.reply.reply_context import (
    IntimacyRequest,
    IntimacyTier,
    OutputConstraints,
    ReplyContext,
    ReplyMode,
)


class ViolationSeverity(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class ViolationCode(StrEnum):
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    VIDEO_REPLY_LENGTH_OUT_OF_RANGE = "VIDEO_REPLY_LENGTH_OUT_OF_RANGE"
    STAGE_DIRECTION_IN_SPOKEN_TEXT = "STAGE_DIRECTION_IN_SPOKEN_TEXT"
    INTERNAL_CONTROL_MARKUP = "INTERNAL_CONTROL_MARKUP"
    PRIVATE_STATE_EXPOSED = "PRIVATE_STATE_EXPOSED"
    PERMANENT_AVAILABILITY_PROMISE = "PERMANENT_AVAILABILITY_PROMISE"
    EXCLUSIVE_RELATIONSHIP_PROMISE = "EXCLUSIVE_RELATIONSHIP_PROMISE"
    UNAUTHORIZED_SHARED_HISTORY = "UNAUTHORIZED_SHARED_HISTORY"
    UNSOLICITED_INTIMACY = "UNSOLICITED_INTIMACY"
    INTIMACY_EXCEEDS_GRANT = "INTIMACY_EXCEEDS_GRANT"


@dataclass(frozen=True)
class Violation:
    code: ViolationCode
    severity: ViolationSeverity
    start: int
    end: int


@dataclass(frozen=True)
class ReplyPolicyResult:
    violations: tuple[Violation, ...]

    @property
    def passed(self) -> bool:
        return not any(item.severity is ViolationSeverity.HARD for item in self.violations)


@dataclass(frozen=True)
class SharedHistoryClaim:
    claim_id: str
    start: int
    end: int
    authorized: bool

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,96}", self.claim_id
        ):
            raise ValueError("claim_id must be a stable identifier")
        if type(self.start) is not int or type(self.end) is not int:
            raise ValueError("claim span must use integers")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("claim span is invalid")
        if type(self.authorized) is not bool:
            raise ValueError("claim authorization must be boolean")


@dataclass(frozen=True)
class IntimacyClaim:
    claim_id: str
    tier: IntimacyTier
    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,96}", self.claim_id
        ):
            raise ValueError("claim_id must be a stable identifier")
        if not isinstance(self.tier, IntimacyTier):
            raise ValueError("claim tier must be an intimacy tier")
        if type(self.start) is not int or type(self.end) is not int:
            raise ValueError("claim span must use integers")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("claim span is invalid")


_CONTROL_MARKUP_RE = re.compile(
    r"</?(?:PERSONA_POLICY|PRIVATE_WORLD|CONTROL|SYSTEM|CONSTITUTION)(?:\s[^>]*)?>|\[\[CONTROL:",
    re.IGNORECASE,
)
_PRIVATE_STATE_RE = re.compile(
    r"(?i)(?:[\"']?)(?:familiarity|trust|comfort|closeness|tension|relationship_stage|intimacy_ceiling|granted_intimacy|intimacy_request|nickname_permission|home_access|home_history_allowed)(?:[\"']?)\s*[:=]"
)
_STAGE_DIRECTION_RE = re.compile(
    r"(?m)^\s*(?:[\(（\[【][^\n]{1,120}[\)）\]】]|\*[^\n*]{1,120}\*)\s*$"
)
_PERMANENT_PROMISE_RE = re.compile(
    "|".join(
        re.escape(value)
        for value in (
            "我会永远在线",
            "我永远不会离开你",
            "I will always be online",
            "I will never leave you",
        )
    ),
    re.IGNORECASE,
)
_EXCLUSIVE_PROMISE_RE = re.compile(
    "|".join(
        re.escape(value)
        for value in (
            "我只属于你",
            "你只需要我",
            "你只能有我",
            "I belong only to you",
            "you only need me",
        )
    ),
    re.IGNORECASE,
)
_INTIMACY_TIER_ORDER = (
    IntimacyTier.NONE,
    IntimacyTier.LIGHT_CONTACT,
    IntimacyTier.CLOSE_CONTACT,
)


def scan_reply(
    candidate: str,
    context: ReplyContext,
    *,
    shared_history_claims: tuple[SharedHistoryClaim, ...] = (),
    intimacy_claims: tuple[IntimacyClaim, ...] = (),
) -> ReplyPolicyResult:
    if not isinstance(candidate, str):
        raise TypeError("candidate must be text")
    if not isinstance(context, ReplyContext):
        raise TypeError("context must be ReplyContext")
    violations: list[Violation] = []
    limit = min(
        context.output_constraints.max_characters,
        OutputConstraints.for_mode(context.mode).max_characters,
    )
    if len(candidate) > limit:
        violations.append(
            Violation(
                ViolationCode.OUTPUT_LIMIT_EXCEEDED,
                ViolationSeverity.HARD,
                limit,
                len(candidate),
            )
        )
    compact_length = len("".join(candidate.split()))
    if (
        context.mode in {ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO}
        and not 180 <= compact_length <= 200
    ):
        violations.append(
            Violation(
                ViolationCode.VIDEO_REPLY_LENGTH_OUT_OF_RANGE,
                ViolationSeverity.HARD,
                0,
                len(candidate),
            )
        )
    if context.output_constraints.channel.value == "spoken_text":
        match = _STAGE_DIRECTION_RE.search(candidate)
        if match is not None:
            violations.append(
                Violation(
                    ViolationCode.STAGE_DIRECTION_IN_SPOKEN_TEXT,
                    ViolationSeverity.HARD,
                    match.start(),
                    match.end(),
                )
            )
    for code, pattern in (
        (ViolationCode.INTERNAL_CONTROL_MARKUP, _CONTROL_MARKUP_RE),
        (ViolationCode.PRIVATE_STATE_EXPOSED, _PRIVATE_STATE_RE),
        (ViolationCode.PERMANENT_AVAILABILITY_PROMISE, _PERMANENT_PROMISE_RE),
        (ViolationCode.EXCLUSIVE_RELATIONSHIP_PROMISE, _EXCLUSIVE_PROMISE_RE),
    ):
        match = pattern.search(candidate)
        if match is not None:
            violations.append(
                Violation(code, ViolationSeverity.HARD, match.start(), match.end())
            )
    for claim in shared_history_claims:
        if not isinstance(claim, SharedHistoryClaim) or claim.end > len(candidate):
            raise ValueError("shared history claim is invalid for candidate")
        authority_ids = {
            boundary.boundary_id
            for boundary in context.private_behavior.active_boundaries
        }
        authority_ids.update(
            fact.fact_id for fact in context.private_behavior.known_continuations
        )
        if context.private_behavior.acknowledged_affection is not None:
            authority_ids.add(
                context.private_behavior.acknowledged_affection.statement_ref_id
            )
        if not claim.authorized or claim.claim_id not in authority_ids:
            violations.append(
                Violation(
                    ViolationCode.UNAUTHORIZED_SHARED_HISTORY,
                    ViolationSeverity.HARD,
                    claim.start,
                    claim.end,
                )
            )
    for claim in intimacy_claims:
        if not isinstance(claim, IntimacyClaim) or claim.end > len(candidate):
            raise ValueError("intimacy claim is invalid for candidate")
        if (
            context.intimacy_request is IntimacyRequest.NONE
            and claim.tier is not IntimacyTier.NONE
        ):
            violations.append(
                Violation(
                    ViolationCode.UNSOLICITED_INTIMACY,
                    ViolationSeverity.HARD,
                    claim.start,
                    claim.end,
                )
            )
        if _INTIMACY_TIER_ORDER.index(
            claim.tier
        ) > _INTIMACY_TIER_ORDER.index(
            context.private_behavior.intimacy_ceiling
        ):
            violations.append(
                Violation(
                    ViolationCode.INTIMACY_EXCEEDS_GRANT,
                    ViolationSeverity.HARD,
                    claim.start,
                    claim.end,
                )
            )
    return ReplyPolicyResult(tuple(violations))
