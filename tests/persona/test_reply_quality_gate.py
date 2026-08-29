from datetime import datetime, timezone

from runtime.reply.reply_context import (
    IntimacyTier,
    ReplyContext,
    ReplyMode,
    TrustedTime,
)
from runtime.reply.reply_policy import IntimacyClaim
from runtime.reply.reply_quality_gate import QualityGateStatus, run_reply_quality_gate
from runtime.reply.reply_reviewer import (
    NullReviewer,
    ReviewerScores,
    ReviewerViolation,
    ReviewResult,
    ReviewStatus,
    ReviewVerdict,
)


class _NeverRewrite:
    def rewrite(self, candidate, context, violation_codes):
        raise AssertionError("rewrite must not be called")


class _Reviewer:
    def __init__(self, *results: ReviewResult) -> None:
        self.results = list(results)
        self.calls = 0

    def review(self, candidate, context):
        result = self.results[self.calls]
        self.calls += 1
        return result


class _Rewriter:
    def __init__(self, rewritten: str) -> None:
        self.rewritten = rewritten
        self.calls = 0

    def rewrite(self, candidate, context, violation_codes):
        self.calls += 1
        return self.rewritten


def _pass_review() -> ReviewResult:
    return ReviewResult(
        ReviewStatus.COMPLETED,
        ReviewVerdict.PASS,
        (),
        ReviewerScores(100, 100, 100, 100),
    )


def _unavailable_review() -> ReviewResult:
    return ReviewResult(
        ReviewStatus.UNAVAILABLE,
        ReviewVerdict.UNAVAILABLE,
        (),
        ReviewerScores(0, 0, 0, 0),
        "REVIEWER_UNAVAILABLE",
    )


def _context() -> ReplyContext:
    return ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )


def _video_context() -> ReplyContext:
    return ReplyContext.create(
        ReplyMode.MUSICAL_VIDEO,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )


def test_clean_candidate_passes_degraded_when_reviewer_is_disabled() -> None:
    result = run_reply_quality_gate(
        "A clean synthetic candidate.",
        _context(),
        reviewer=NullReviewer(),
        rewriter=_NeverRewrite(),
    )

    assert result.status is QualityGateStatus.ACCEPTED_DEGRADED
    assert result.accepted is True
    assert result.text == "A clean synthetic candidate."
    assert result.reviewer_calls == 1
    assert result.rewrite_calls == 0


def test_hard_candidate_is_rewritten_once_then_rechecked_before_acceptance() -> None:
    reviewer = _Reviewer(_pass_review(), _pass_review())
    rewriter = _Rewriter("A clean rewritten candidate.")

    result = run_reply_quality_gate(
        "<CONTROL>bad candidate",
        _context(),
        reviewer=reviewer,
        rewriter=rewriter,
    )

    assert result.status is QualityGateStatus.ACCEPTED
    assert result.text == "A clean rewritten candidate."
    assert result.deterministic_checks == 2
    assert result.reviewer_calls == 2
    assert result.rewrite_calls == 1
    assert reviewer.calls == 2
    assert rewriter.calls == 1


def test_candidate_bound_intimacy_claim_fails_closed_after_rewrite() -> None:
    candidate = "Synthetic contact."
    reviewer = _Reviewer(_pass_review(), _pass_review())
    rewriter = _Rewriter("Safe rewritten candidate.")

    result = run_reply_quality_gate(
        candidate,
        _context(),
        reviewer=reviewer,
        rewriter=rewriter,
        intimacy_claims=(
            IntimacyClaim(
                "intimacy.synthetic",
                IntimacyTier.LIGHT_CONTACT,
                0,
                len(candidate),
            ),
        ),
    )

    assert result.status is QualityGateStatus.BLOCKED
    assert result.accepted is False
    assert result.text == "Safe rewritten candidate."
    assert result.error_code == "FRESH_INTIMACY_CLAIMS_REQUIRED"
    assert result.deterministic_checks == 1
    assert result.reviewer_calls == 1
    assert result.rewrite_calls == 1


def test_second_hard_result_is_blocked_without_a_hidden_rewrite_loop() -> None:
    reviewer = _Reviewer(_pass_review(), _pass_review())
    rewriter = _Rewriter("<SYSTEM>still invalid")

    result = run_reply_quality_gate(
        "<CONTROL>first invalid",
        _context(),
        reviewer=reviewer,
        rewriter=rewriter,
    )

    assert result.status is QualityGateStatus.BLOCKED
    assert result.accepted is False
    assert result.rewrite_calls == 1
    assert result.reviewer_calls == 2
    assert rewriter.calls == 1


def test_enabled_reviewer_failure_after_rewrite_is_blocked() -> None:
    reviewer = _Reviewer(_pass_review(), _unavailable_review())

    result = run_reply_quality_gate(
        "<CONTROL>first invalid",
        _context(),
        reviewer=reviewer,
        rewriter=_Rewriter("A clean rewritten candidate."),
    )

    assert result.status is QualityGateStatus.BLOCKED
    assert result.accepted is False
    assert result.error_code == "REVIEWER_UNAVAILABLE"
    assert result.reviewer_calls == 2
    assert result.rewrite_calls == 1


def test_short_video_reply_uses_the_single_global_rewrite_budget() -> None:
    reviewer = _Reviewer(_pass_review(), _pass_review())
    rewriter = _Rewriter("林" * 190)

    result = run_reply_quality_gate(
        "太短。",
        _video_context(),
        reviewer=reviewer,
        rewriter=rewriter,
    )

    assert result.status is QualityGateStatus.ACCEPTED
    assert result.rewrite_calls == 1
    assert reviewer.calls == 2
    assert rewriter.calls == 1


def test_short_video_rewrite_is_blocked_without_a_second_rewrite() -> None:
    reviewer = _Reviewer(_pass_review(), _pass_review())
    rewriter = _Rewriter("还是太短。")

    result = run_reply_quality_gate(
        "太短。",
        _video_context(),
        reviewer=reviewer,
        rewriter=rewriter,
    )

    assert result.status is QualityGateStatus.BLOCKED
    assert result.violation_codes == ("VIDEO_REPLY_LENGTH_OUT_OF_RANGE",)
    assert result.rewrite_calls == 1
    assert rewriter.calls == 1


def test_final_soft_review_issue_is_accepted_with_warnings_after_one_rewrite() -> None:
    first = ReviewResult(
        ReviewStatus.COMPLETED,
        ReviewVerdict.REWRITE,
        (),
        ReviewerScores(80, 80, 80, 80),
    )
    final = ReviewResult(
        ReviewStatus.COMPLETED,
        ReviewVerdict.REWRITE,
        (ReviewerViolation("STYLE_DRIFT", "soft", 0, 5),),
        ReviewerScores(90, 90, 90, 90),
    )

    result = run_reply_quality_gate(
        "Initial candidate.",
        _context(),
        reviewer=_Reviewer(first, final),
        rewriter=_Rewriter("Final candidate."),
    )

    assert result.status is QualityGateStatus.ACCEPTED_WITH_WARNINGS
    assert result.accepted is True
    assert result.violation_codes == ("STYLE_DRIFT",)
    assert result.rewrite_calls == 1
