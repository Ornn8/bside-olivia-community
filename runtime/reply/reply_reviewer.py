"""Strict, provider-neutral reply-review adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Mapping, Protocol

from jsonschema import Draft202012Validator

from runtime.reply.reply_context import (
    IntimacyRequest,
    IntimacyTier,
    ReplyContext,
)
from runtime.reply.reply_policy import IntimacyClaim


_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "reply_review.schema.json"
_VALIDATOR = Draft202012Validator(
    json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
)


class ReviewStatus(StrEnum):
    COMPLETED = "completed"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


class ReviewVerdict(StrEnum):
    PASS = "pass"
    REWRITE = "rewrite"
    BLOCK = "block"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ReviewerScores:
    persona_consistency: int
    factual_consistency: int
    relationship_boundary: int
    mode_compliance: int


@dataclass(frozen=True)
class ReviewerViolation:
    code: str
    severity: str
    start: int
    end: int


@dataclass(frozen=True)
class ReviewReference:
    reference_id: str
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,96}",
            self.reference_id,
        ):
            raise ValueError(
                "reference_id must be a stable identifier"
            )
        if (
            not isinstance(self.summary, str)
            or not self.summary.strip()
            or len(self.summary) > 600
            or re.search(
                r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
                self.summary,
            )
        ):
            raise ValueError("reference summary is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "reference_id": self.reference_id,
            "summary": self.summary.strip(),
        }


@dataclass(frozen=True)
class TrustedCharacterReply:
    evidence_id: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,96}",
            self.evidence_id,
        ):
            raise ValueError("evidence_id must be a stable identifier")
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > 1200
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", self.text)
        ):
            raise ValueError("trusted character reply is invalid")


@dataclass(frozen=True)
class TrustedReviewEvidence:
    character_replies: tuple[TrustedCharacterReply, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.character_replies, tuple) or any(
            not isinstance(item, TrustedCharacterReply)
            for item in self.character_replies
        ):
            raise TypeError("character_replies must be a typed tuple")
        ids = tuple(item.evidence_id for item in self.character_replies)
        if len(set(ids)) != len(ids):
            raise ValueError("trusted character reply ids must be unique")
        if sum(len(item.text.strip()) for item in self.character_replies) > 1200:
            raise ValueError("trusted character reply history is too large")


@dataclass(frozen=True)
class ReviewResult:
    status: ReviewStatus
    verdict: ReviewVerdict
    violations: tuple[ReviewerViolation, ...]
    scores: ReviewerScores
    intimacy_request: IntimacyRequest | None
    intimacy_claims: tuple[IntimacyClaim, ...] | None
    error_code: str | None = None

    def __post_init__(self) -> None:
        completed = self.status is ReviewStatus.COMPLETED
        if completed != isinstance(self.intimacy_request, IntimacyRequest):
            raise ValueError("completed review requires intimacy request")
        if completed != isinstance(self.intimacy_claims, tuple):
            raise ValueError("completed review requires intimacy claims")
        if self.intimacy_claims is not None:
            if any(
                not isinstance(claim, IntimacyClaim)
                for claim in self.intimacy_claims
            ):
                raise ValueError("intimacy claims must be typed")
            claim_ids = tuple(
                claim.claim_id for claim in self.intimacy_claims
            )
            if len(set(claim_ids)) != len(claim_ids):
                raise ValueError("intimacy claim ids must be unique")


@dataclass(frozen=True)
class ReviewerConfig:
    model: str
    timeout_seconds: float = 10.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not re.fullmatch(
            r"[A-Za-z0-9._:/-]{1,128}",
            self.model,
        ):
            raise ValueError(
                "reviewer model must be a stable identifier"
            )
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                "reviewer timeout must be positive"
            )
        if type(self.enabled) is not bool:
            raise ValueError(
                "reviewer enabled must be boolean"
            )


class ReviewTransport(Protocol):
    def review_json(
        self,
        request: dict[str, object],
        *,
        model: str,
        timeout_seconds: float,
    ) -> object: ...


class NullReviewer:
    def review(
        self,
        candidate: str,
        context: ReplyContext,
        *,
        references: tuple[ReviewReference, ...] = (),
    ) -> ReviewResult:
        return _failure(
            ReviewStatus.DISABLED,
            "REVIEWER_DISABLED",
        )


class JsonReviewerAdapter:
    def __init__(
        self,
        transport: ReviewTransport,
        config: ReviewerConfig,
    ) -> None:
        self.transport = transport
        self.config = config

    def review(
        self,
        candidate: str,
        context: ReplyContext,
        *,
        references: tuple[ReviewReference, ...] = (),
    ) -> ReviewResult:
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("candidate is required")
        if not isinstance(context, ReplyContext):
            raise TypeError("context must be ReplyContext")
        if not self.config.enabled:
            return _failure(
                ReviewStatus.DISABLED,
                "REVIEWER_DISABLED",
            )
        request: dict[str, object] = {
            "candidate": candidate,
            "mode": context.mode.value,
            "output_constraints": (
                context.output_constraints.to_dict()
            ),
            "world_facts": [
                fact.to_dict()
                for fact in context.world_facts
            ],
            "known_continuations": [
                fact.to_dict()
                for fact in (
                    context.private_behavior.known_continuations
                )
            ],
            "relationship_context": {
                "relationship_stage": (
                    context.private_behavior.relationship_stage.value
                ),
                "intimacy_ceiling": (
                    context.private_behavior.intimacy_ceiling.value
                ),
                "granted_intimacy": (
                    context.private_behavior.granted_intimacy.value
                ),
            },
            "references": [
                reference.to_dict()
                for reference in references
            ],
        }
        try:
            response = self.transport.review_json(
                request,
                model=self.config.model,
                timeout_seconds=float(
                    self.config.timeout_seconds
                ),
            )
        except Exception:
            return _failure(
                ReviewStatus.UNAVAILABLE,
                "REVIEWER_UNAVAILABLE",
            )
        if (
            not isinstance(response, Mapping)
            or list(_VALIDATOR.iter_errors(response))
        ):
            return _failure(
                ReviewStatus.INVALID_RESPONSE,
                "REVIEWER_RESPONSE_INVALID",
            )
        try:
            scores = response["scores"]
            violations = tuple(
                ReviewerViolation(
                    code=str(item["code"]),
                    severity=str(item["severity"]),
                    start=int(item["evidence"]["start"]),
                    end=int(item["evidence"]["end"]),
                )
                for item in response["violations"]
            )
            if any(
                item.end <= item.start
                or item.end > len(candidate)
                for item in violations
            ):
                return _failure(
                    ReviewStatus.INVALID_RESPONSE,
                    "REVIEWER_RESPONSE_INVALID",
                )
            intimacy_claims = tuple(
                IntimacyClaim(
                    claim_id=str(item["claim_id"]),
                    tier=IntimacyTier(str(item["tier"])),
                    start=int(item["start"]),
                    end=int(item["end"]),
                )
                for item in response["intimacy_claims"]
            )
            if (
                len({claim.claim_id for claim in intimacy_claims})
                != len(intimacy_claims)
                or any(claim.end > len(candidate) for claim in intimacy_claims)
            ):
                return _failure(
                    ReviewStatus.INVALID_RESPONSE,
                    "REVIEWER_RESPONSE_INVALID",
                )
            return ReviewResult(
                status=ReviewStatus(
                    str(response["status"])
                ),
                verdict=ReviewVerdict(
                    str(response["verdict"])
                ),
                violations=violations,
                scores=ReviewerScores(
                    int(scores["persona_consistency"]),
                    int(scores["factual_consistency"]),
                    int(scores["relationship_boundary"]),
                    int(scores["mode_compliance"]),
                ),
                intimacy_request=IntimacyRequest(
                    str(response["intimacy_request"])
                ),
                intimacy_claims=intimacy_claims,
            )
        except (KeyError, TypeError, ValueError):
            return _failure(
                ReviewStatus.INVALID_RESPONSE,
                "REVIEWER_RESPONSE_INVALID",
            )


def _failure(
    status: ReviewStatus,
    error_code: str,
) -> ReviewResult:
    return ReviewResult(
        status=status,
        verdict=ReviewVerdict.UNAVAILABLE,
        violations=(),
        scores=ReviewerScores(0, 0, 0, 0),
        intimacy_request=None,
        intimacy_claims=None,
        error_code=error_code,
    )
