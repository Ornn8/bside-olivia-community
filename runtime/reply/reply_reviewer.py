"""Strict, provider-neutral reply-review adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Mapping, Protocol

from jsonschema import Draft202012Validator

from runtime.reply.reply_context import ReplyContext


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
class ReviewResult:
    status: ReviewStatus
    verdict: ReviewVerdict
    violations: tuple[ReviewerViolation, ...]
    scores: ReviewerScores
    error_code: str | None = None


@dataclass(frozen=True)
class ReviewerConfig:
    model: str
    timeout_seconds: float = 10.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,96}",
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
        error_code=error_code,
    )
