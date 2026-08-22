"""Bounded reply quality gate with a global one-rewrite maximum."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from reply_context import ReplyContext
from reply_policy import scan_reply
from reply_reviewer import ReviewResult, ReviewVerdict


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
) -> QualityGateResult:
    deterministic = scan_reply(candidate, context)
    review = reviewer.review(candidate, context)
    deterministic_codes = tuple(item.code.value for item in deterministic.violations)
    review_codes = tuple(item.code for item in review.violations)
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
            QualityGateStatus.ACCEPTED,
            candidate,
            deterministic_codes + review_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=0,
        )
    try:
        rewritten = rewriter.rewrite(
            candidate,
            context,
            deterministic_codes + review_codes,
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
    final_review = reviewer.review(rewritten, context)
    final_codes = tuple(item.code.value for item in final_deterministic.violations) + tuple(
        item.code for item in final_review.violations
    )
    if not final_deterministic.passed or final_review.verdict is ReviewVerdict.BLOCK:
        status = QualityGateStatus.BLOCKED
    elif final_review.verdict is ReviewVerdict.UNAVAILABLE:
        status = QualityGateStatus.ACCEPTED_DEGRADED
    elif final_review.verdict is ReviewVerdict.REWRITE:
        has_hard_review = any(item.severity == "hard" for item in final_review.violations)
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
