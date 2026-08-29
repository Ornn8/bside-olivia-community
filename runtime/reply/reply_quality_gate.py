"""Bounded reply quality gate with a global one-rewrite maximum."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from runtime.reply.reply_context import ReplyContext
from runtime.reply.reply_policy import IntimacyClaim, scan_reply
from runtime.reply.reply_reviewer import ReviewResult, ReviewStatus, ReviewVerdict


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


def _stable_codes(*groups: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for code in group:
            if code not in seen:
                seen.add(code)
                ordered.append(code)
    return tuple(ordered)


def run_reply_quality_gate(
    candidate: str,
    context: ReplyContext,
    *,
    reviewer: ReviewerPort,
    rewriter: RewriterPort,
    generation_messages: Sequence[Mapping[str, Any]] = (),
    intimacy_claims: tuple[IntimacyClaim, ...] = (),
) -> QualityGateResult:
    review = _review_candidate(
        reviewer,
        candidate,
        context,
        generation_messages,
    )
    if intimacy_claims and review.status is ReviewStatus.COMPLETED:
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            (),
            deterministic_checks=0,
            reviewer_calls=1,
            rewrite_calls=0,
            error_code="INTIMACY_CLAIM_SOURCE_CONFLICT",
        )
    reviewed_context = (
        replace(context, intimacy_request=review.intimacy_request)
        if review.status is ReviewStatus.COMPLETED
        else context
    )
    effective_intimacy_claims = (
        intimacy_claims
        if intimacy_claims
        else (
            review.intimacy_claims or ()
            if review.status is ReviewStatus.COMPLETED
            else ()
        )
    )
    deterministic = scan_reply(
        candidate,
        reviewed_context,
        intimacy_claims=effective_intimacy_claims,
    )
    deterministic_codes = tuple(
        item.code.value for item in deterministic.violations
    )
    review_codes = tuple(item.code for item in review.violations)
    initial_codes = _stable_codes(deterministic_codes, review_codes)
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
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=0,
        )
    try:
        rewritten = _rewrite_candidate(
            rewriter,
            candidate,
            reviewed_context,
            initial_codes,
            generation_messages,
        )
    except Exception:
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=1,
            error_code="REWRITE_FAILED",
        )
    if not isinstance(rewritten, str) or not rewritten.strip():
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=1,
            error_code="REWRITE_FAILED",
        )
    if (
        effective_intimacy_claims
        and review.status is not ReviewStatus.COMPLETED
    ):
        # Explicit claims are bound to the original candidate. A disabled or
        # unavailable reviewer cannot produce fresh evidence for rewritten
        # text, so accepting here would fail open.
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            rewritten,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=1,
            error_code="FRESH_INTIMACY_CLAIMS_REQUIRED",
        )
    final_review = _review_candidate(
        reviewer,
        rewritten,
        reviewed_context,
        generation_messages,
    )
    if (
        final_review.status is ReviewStatus.COMPLETED
        and final_review.intimacy_request
        is not reviewed_context.intimacy_request
    ):
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            rewritten,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=2,
            rewrite_calls=1,
            error_code="INTIMACY_REQUEST_INCONSISTENT",
        )
    if (
        effective_intimacy_claims
        and final_review.status is not ReviewStatus.COMPLETED
    ):
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            rewritten,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=2,
            rewrite_calls=1,
            error_code="FRESH_INTIMACY_CLAIMS_REQUIRED",
        )
    final_deterministic = scan_reply(
        rewritten,
        reviewed_context,
        intimacy_claims=(
            final_review.intimacy_claims or ()
            if final_review.status is ReviewStatus.COMPLETED
            else ()
        ),
    )
    final_codes = _stable_codes(
        tuple(
            item.code.value for item in final_deterministic.violations
        ),
        tuple(item.code for item in final_review.violations),
    )
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
