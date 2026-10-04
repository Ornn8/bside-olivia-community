"""Bounded reply quality gate with a global one-rewrite maximum."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from runtime.reply.reply_context import ReplyContext, ReplyMode
from runtime.reply.reply_policy import IntimacyClaim, scan_reply
from runtime.reply.reply_reviewer import (
    ReviewerViolation,
    ReviewResult,
    ReviewStatus,
    ReviewVerdict,
)
from runtime.reply.reply_reviewer import TrustedReviewEvidence
from runtime.diagnostics.failure_context import REWRITE_ERROR_CODES
from runtime.reply.reply_review_policy import SOFT_STYLE_CODES, fact_sentence_spans, preserves_quote_balance


# Modes without a delivery length contract, where whole sentences may be dropped.
_TRIMMABLE_MODES = frozenset({ReplyMode.TEXT_LETTER, ReplyMode.VOICE_REPLY, ReplyMode.FUTURE_IM})


def _without_unsupported_facts(text, violations, mode, intimacy_claims=()):
    """Remove confirmed fact sentences and preserve claims on unchanged text."""
    if mode not in _TRIMMABLE_MODES or not violations or any(
            item.code != 'MEMORY_FABRICATION' or not 0 <= item.start < item.end <= len(text)
            for item in violations):
        return None
    spans = fact_sentence_spans(text, ((item.start, item.end) for item in violations))
    keep, cursor = [], 0
    for start, end in spans:
        keep.append(text[cursor:start])
        cursor = end
    keep.append(text[cursor:])
    trimmed = ''.join(keep)
    if not any(char.isalnum() for char in trimmed) or not preserves_quote_balance(text, trimmed):
        return None
    leading = len(trimmed) - len(trimmed.lstrip())
    repaired = trimmed.strip()
    mapped = []
    for claim in intimacy_claims:
        if any(start <= claim.start and claim.end <= end for start, end in spans):
            continue
        if any(start < claim.end and claim.start < end for start, end in spans):
            return None  # A partial claim cannot be rebound safely.
        shift = sum(end - start for start, end in spans if end <= claim.start) + leading
        # A valid original span may include the whitespace removed by strip.
        start, end = max(0, claim.start - shift), min(len(repaired), claim.end - shift)
        if end <= start:
            return None
        mapped.append(replace(claim, start=start, end=end))
    return repaired, tuple(mapped)


class QualityGateStatus(StrEnum):
    ACCEPTED = "accepted"
    ACCEPTED_DEGRADED = "accepted_degraded"
    ACCEPTED_WITH_WARNINGS = "accepted_with_warnings"
    BLOCKED = "blocked"


class DeliveryRepairDisposition(StrEnum):
    NONE = "none"
    VIDEO_LENGTH = "video_length"


@dataclass(frozen=True)
class QualityGateResult:
    status: QualityGateStatus
    text: str
    violation_codes: tuple[str, ...]
    deterministic_checks: int
    reviewer_calls: int
    rewrite_calls: int
    error_code: str | None = None
    delivery_repair_disposition: DeliveryRepairDisposition = (
        DeliveryRepairDisposition.NONE
    )

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


def _delivery_repair_disposition(
    deterministic_codes: tuple[str, ...],
    review: ReviewResult,
) -> DeliveryRepairDisposition:
    if (
        frozenset(deterministic_codes)
        == frozenset({"VIDEO_REPLY_LENGTH_OUT_OF_RANGE"})
        and not any(item.severity == "hard" for item in review.violations)
        and review.verdict is not ReviewVerdict.BLOCK
        and (
            review.verdict is not ReviewVerdict.UNAVAILABLE
            or review.status is ReviewStatus.DISABLED
        )
    ):
        return DeliveryRepairDisposition.VIDEO_LENGTH
    return DeliveryRepairDisposition.NONE


def run_reply_quality_gate(
    candidate: str,
    context: ReplyContext,
    *,
    reviewer: ReviewerPort,
    rewriter: RewriterPort,
    generation_messages: Sequence[Mapping[str, Any]] = (),
    trusted_evidence: TrustedReviewEvidence = TrustedReviewEvidence(),
    intimacy_claims: tuple[IntimacyClaim, ...] = (),
    allow_rewrite: bool = True,
    normalize_rewrite=None,
) -> QualityGateResult:
    review = _review_candidate(
        reviewer,
        candidate,
        context,
        generation_messages,
        trusted_evidence,
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
    initial_delivery_repair = _delivery_repair_disposition(
        deterministic_codes,
        review,
    )
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
    if (
        review.status is ReviewStatus.COMPLETED
        and review.verdict in (ReviewVerdict.REWRITE, ReviewVerdict.BLOCK)
        and deterministic.passed
        and review.violations
        and all(
            item.severity == 'soft' and item.code in SOFT_STYLE_CODES
            for item in review.violations
        )
    ):
        # A style preference is not a requirement for another generation.
        return QualityGateResult(
            QualityGateStatus.ACCEPTED_WITH_WARNINGS,
            candidate,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=0,
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
        confirmed_evidence = _confirmed_rewrite_evidence(
            reviewer,
            candidate,
            reviewed_context,
            review,
            initial_codes,
        )
    except Exception:
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=0,
            error_code="REWRITE_EVIDENCE_INVALID",
        )
    hard_findings = tuple(item for item in review.violations if item.severity == 'hard')
    local_repair = (
        _without_unsupported_facts(candidate, confirmed_evidence, reviewed_context.mode,
                                   effective_intimacy_claims)
        if deterministic.passed and hard_findings
        and set(hard_findings) == set(confirmed_evidence)
        and all(item.severity == 'hard' or item.code in SOFT_STYLE_CODES for item in review.violations)
        else None
    )
    if local_repair is not None:
        repaired, claims = local_repair
        if scan_reply(repaired, reviewed_context, intimacy_claims=claims).passed:
            return QualityGateResult(QualityGateStatus.ACCEPTED_WITH_WARNINGS, repaired, initial_codes,
                                     deterministic_checks=2, reviewer_calls=1, rewrite_calls=0)
    if not allow_rewrite:
        return QualityGateResult(QualityGateStatus.BLOCKED, candidate, initial_codes,
            deterministic_checks=1, reviewer_calls=1, rewrite_calls=0,
            error_code='REWRITE_BUDGET_EXHAUSTED')
    try:
        rewritten = _rewrite_candidate(
            rewriter,
            candidate,
            reviewed_context,
            initial_codes,
            generation_messages,
            confirmed_evidence,
        )
        if normalize_rewrite is not None:
            try:
                rewritten = normalize_rewrite(rewritten)
            except Exception as exc:
                raise RuntimeError("REWRITE_OUTPUT_INVALID") from exc
    except Exception as exc:
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=1,
            error_code=(str(exc) if str(exc) in REWRITE_ERROR_CODES else "REWRITE_FAILED"),
            delivery_repair_disposition=initial_delivery_repair,
        )
    if not isinstance(rewritten, str) or not rewritten.strip():
        return QualityGateResult(
            QualityGateStatus.BLOCKED,
            candidate,
            initial_codes,
            deterministic_checks=1,
            reviewer_calls=1,
            rewrite_calls=1,
            error_code="REWRITE_OUTPUT_EMPTY",
            delivery_repair_disposition=initial_delivery_repair,
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
        trusted_evidence,
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
    final_hard = tuple(item for item in final_review.violations if item.severity == 'hard')
    try:
        final_evidence = _confirmed_rewrite_evidence(
            reviewer, rewritten, reviewed_context, final_review, final_codes,
        ) if final_review.status is ReviewStatus.COMPLETED and final_hard else ()
    except Exception:
        return QualityGateResult(QualityGateStatus.BLOCKED, rewritten, final_codes,
                                 deterministic_checks=2, reviewer_calls=2, rewrite_calls=1,
                                 error_code='REWRITE_EVIDENCE_INVALID')
    trimmed = (
        _without_unsupported_facts(rewritten, final_evidence, reviewed_context.mode,
                                   final_review.intimacy_claims or ())
        if final_deterministic.passed
        and final_hard and set(final_hard) == set(final_evidence)
        and all(item.severity == 'hard' or item.code in SOFT_STYLE_CODES for item in final_review.violations)
        and final_review.status is ReviewStatus.COMPLETED
        and final_review.verdict in (ReviewVerdict.BLOCK, ReviewVerdict.REWRITE)
        else None
    )
    if trimmed is not None and not scan_reply(trimmed[0], reviewed_context, intimacy_claims=trimmed[1]).passed:
        trimmed = None
    if trimmed is not None:
        rewritten, status = trimmed[0], QualityGateStatus.ACCEPTED_WITH_WARNINGS
    elif final_deterministic.passed and final_review.status is ReviewStatus.COMPLETED and final_review.violations and all(
        item.severity == 'soft' and item.code in SOFT_STYLE_CODES for item in final_review.violations
    ):
        status = QualityGateStatus.ACCEPTED_WITH_WARNINGS
    elif (
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
        status = QualityGateStatus.BLOCKED
    else:
        status = QualityGateStatus.ACCEPTED
    return QualityGateResult(
        status,
        rewritten,
        final_codes,
        deterministic_checks=3 if trimmed is not None else 2,
        reviewer_calls=2,
        rewrite_calls=1,
        error_code=final_review.error_code,
        delivery_repair_disposition=_delivery_repair_disposition(
            tuple(
                item.code.value
                for item in final_deterministic.violations
            ),
            final_review,
        ),
    )


def _review_candidate(
    reviewer: ReviewerPort,
    candidate: str,
    context: ReplyContext,
    generation_messages: Sequence[Mapping[str, Any]],
    trusted_evidence: TrustedReviewEvidence,
) -> ReviewResult:
    extended = getattr(reviewer, "review_with_messages", None)
    if callable(extended):
        if trusted_evidence.character_replies:
            return extended(
                candidate,
                context,
                generation_messages,
                trusted_evidence=trusted_evidence,
            )
        return extended(candidate, context, generation_messages)
    return reviewer.review(candidate, context)


def _rewrite_candidate(
    rewriter: RewriterPort,
    candidate: str,
    context: ReplyContext,
    violation_codes: tuple[str, ...],
    generation_messages: Sequence[Mapping[str, Any]],
    confirmed_violations: tuple[ReviewerViolation, ...],
) -> str:
    evidence_aware = getattr(rewriter, "rewrite_with_evidence", None)
    if callable(evidence_aware):
        return evidence_aware(
            candidate,
            context,
            violation_codes,
            generation_messages,
            confirmed_violations,
        )
    extended = getattr(rewriter, "rewrite_with_messages", None)
    if callable(extended):
        return extended(
            candidate,
            context,
            violation_codes,
            generation_messages,
        )
    return rewriter.rewrite(candidate, context, violation_codes)


def _confirmed_rewrite_evidence(
    reviewer: ReviewerPort,
    candidate: str,
    context: ReplyContext,
    review: ReviewResult,
    violation_codes: tuple[str, ...],
) -> tuple[ReviewerViolation, ...]:
    source = getattr(reviewer, "confirmed_rewrite_evidence", None)
    if not callable(source):
        return ()
    evidence = source(candidate, context, review)
    if not isinstance(evidence, tuple) or any(
        not isinstance(item, ReviewerViolation)
        for item in evidence
    ):
        raise TypeError("confirmed rewrite evidence must be a typed tuple")
    if len(evidence) > 16:
        raise ValueError("confirmed rewrite evidence exceeds limit")
    signatures: set[tuple[str, int, int]] = set()
    for item in evidence:
        signature = (item.code, item.start, item.end)
        if (
            item.severity != "hard"
            or item not in review.violations
            or item.code not in violation_codes
            or isinstance(item.start, bool)
            or not isinstance(item.start, int)
            or isinstance(item.end, bool)
            or not isinstance(item.end, int)
            or item.start < 0
            or item.end <= item.start
            or item.end > len(candidate)
            or signature in signatures
        ):
            raise ValueError("confirmed rewrite evidence is invalid")
        signatures.add(signature)
    return evidence
