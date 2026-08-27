"""Bounded reply quality gate with a global one-rewrite maximum."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from reply_context import ReplyContext
from reply_policy import scan_reply
from reply_reviewer import ReviewResult, ReviewStatus, ReviewVerdict


class QualityGateStatus(StrEnum):
    ACCEPTED = "accepted"
    ACCEPTED_DEGRADED = "accepted_degraded"
    ACCEPTED_WITH_WARNINGS = "accepted_with_warnings"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class QualityGateResult:
    status: QualityGateStatus
    text: str
    violation_codes: tuple[str, ...]
    deterministic_checks: int
    reviewer_calls: int
    rewrite_calls: int
    error_code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status is not QualityGateStatus.BLOCKED


class ReviewerPort(Protocol):
    def review(self, candidate: str, context: ReplyContext) -> ReviewResult: ...


class RewriterPort(Protocol):
    def rewrite(
        self,
        candidate: str,
        context: ReplyContext,
        violation_codes: tuple[str, ...],
    ) -> str: ...


def run_reply_quality_gate(
    candidate: str,
    context: ReplyContext,
    *,
    reviewer: ReviewerPort,
    rewriter: RewriterPort,
    generation_messages: Sequence[Mapping[str, Any]] = (),
) -> QualityGateResult:
    deterministic = scan_reply(candidate, context)
    review = _review_candidate(
        reviewer,
        candidate,
        context,
        generation_messages,
    )
    deterministic_codes = tuple(
        item.code.value for item in deterministic.violations
    )
    review_codes = tuple(item.code for item in review.violations)
    if (
        review.verdict is ReviewVerdict.UNAVAILABLE
        and review.status is not ReviewStatus.DISABLED
    ):
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            deterministic_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=0,
            error_code=review.error_code,
        )
    if deterministic.passed and review.verdict is ReviewVerdict.UNAVAILABLE:
        return QualityGateResult(
            QualityGateStatus.ACCEPTED_DEGRADED,
            candidate,
            deterministic_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=0,
            error_code=review.error_code,
        )
    rewrite_required = not deterministic.passed or review.verdict in {
        ReviewVerdict.REWRITE,
        ReviewVerdict.BLOCK,
    }
    if not rewrite_required:
        return QualityGateResult(
            (
                QualityGateStatus.ACCEPTED_WITH_WARNINGS
                if review.violations
                else QualityGateStatus.ACCEPTED
            ),
            candidate,
            deterministic_codes + review_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=0,
        )
    try:
        rewritten = _rewrite_candidate(
            rewriter,
            candidate,
            context,
            deterministic_codes + review_codes,
            generation_messages,
        )
    except Exception:
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            deterministic_codes + review_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=1,
            error_code="REWRITE_FAILED",
        )
    if not isinstance(rewritten, str) or not rewritten.strip():
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            deterministic_codes + review_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=1,
            error_code="REWRITE_FAILED",
        )
    final_deterministic = scan_reply(rewritten, context)
    final_review = _review_candidate(
        reviewer,
        rewritten,
        context,
        generation_messages,
    )
    final_codes = tuple(
        item.code.value for item in final_deterministic.violations
    ) + tuple(item.code for item in final_review.violations)
    if (
        not final_deterministic.passed
        or final_review.verdict is ReviewVerdict.BLOCK
    ):
        status = QualityGateStatus.BLOCKED
    elif final_review.verdict is ReviewVerdict.UNAVAILABLE:
        status = (
            QualityGateStatus.ACCEPTED_DEGRADED
            if final_review.status is ReviewStatus.DISABLED
            else QualityGateStatus.BLOCKED
        )
    elif final_review.verdict is ReviewVerdict.REWRITE:
        has_hard_review = any(
            item.severity == "hard" for item in final_review.violations
        )
        status = (
            QualityGateStatus.BLOCKED
            if has_hard_review
            else QualityGateStatus.ACCEPTED_WITH_WARNINGS
        )
    else:
        status = QualityGateStatus.ACCEPTED
    return QualityGateResult(
        status,
        rewritten,
        final_codes,
        deterministic_checks=2,
        reviewer_calls=2,
        rewrite_calls=1,
        error_code=final_review.error_code,
    )


def _review_candidate(
    reviewer: ReviewerPort,
    candidate: str,
    context: ReplyContext,
    generation_messages: Sequence[Mapping[str, Any]],
) -> ReviewResult:
    extended = getattr(reviewer, "review_with_messages", None)
    if callable(extended):
        return extended(candidate, context, generation_messages)
    return reviewer.review(candidate, context)


def _rewrite_candidate(
    rewriter: RewriterPort,
    candidate: str,
    context: ReplyContext,
    violation_codes: tuple[str, ...],
    generation_messages: Sequence[Mapping[str, Any]],
) -> str:
    extended = getattr(rewriter, "rewrite_with_messages", None)
    if callable(extended):
        return extended(
            candidate,
            context,
            violation_codes,
            generation_messages,
        )
    return rewriter.rewrite(candidate, context, violation_codes)
