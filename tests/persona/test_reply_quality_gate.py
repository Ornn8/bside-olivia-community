from datetime import datetime, timezone

from runtime.reply.reply_context import (
    IntimacyRequest,
    IntimacyTier,
    PrivateBehaviorView,
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
        self.violation_codes: list[tuple[str, ...]] = []

    def rewrite(self, candidate, context, violation_codes):
        self.calls += 1
        self.violation_codes.append(violation_codes)
        return self.rewritten


def _pass_review(
    *,
    intimacy_request: IntimacyRequest = IntimacyRequest.NONE,
    intimacy_claims: tuple[IntimacyClaim, ...] = (),
) -> ReviewResult:
    return ReviewResult(
        ReviewStatus.COMPLETED,
        ReviewVerdict.PASS,
        (),
        ReviewerScores(100, 100, 100, 100),
        intimacy_request,
        intimacy_claims,
    )


def _unavailable_review() -> ReviewResult:
    return ReviewResult(
        ReviewStatus.UNAVAILABLE,
        ReviewVerdict.UNAVAILABLE,
        (),
        ReviewerScores(0, 0, 0, 0),
        None,
        None,
        error_code="REVIEWER_UNAVAILABLE",
    )


def _context(
    *,
    intimacy_ceiling: IntimacyTier = IntimacyTier.NONE,
) -> ReplyContext:
    return ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
        private_behavior=PrivateBehaviorView(
            intimacy_ceiling=intimacy_ceiling,
        ),
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
    reviewer = NullReviewer()
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


def test_rewrite_uses_fresh_reviewer_claims_for_the_new_candidate() -> None:
    candidate = "Synthetic contact."
    initial_claim = IntimacyClaim(
        "intimacy.initial",
        IntimacyTier.LIGHT_CONTACT,
        0,
        len(candidate),
    )
    reviewer = _Reviewer(
        _pass_review(intimacy_claims=(initial_claim,)),
        _pass_review(),
    )

    result = run_reply_quality_gate(
        candidate,
        _context(intimacy_ceiling=IntimacyTier.LIGHT_CONTACT),
        reviewer=reviewer,
        rewriter=_Rewriter("Safe rewritten candidate."),
    )

    assert result.status is QualityGateStatus.ACCEPTED
    assert result.text == "Safe rewritten candidate."
    assert result.deterministic_checks == 2
    assert result.reviewer_calls == 2
    assert result.rewrite_calls == 1


def test_rewrite_blocks_when_fresh_claim_still_exceeds_request() -> None:
    candidate = "Synthetic contact."
    rewritten = "Rewritten contact remains."
    initial_claim = IntimacyClaim(
        "intimacy.initial",
        IntimacyTier.LIGHT_CONTACT,
        0,
        len(candidate),
    )
    final_claim = IntimacyClaim(
        "intimacy.rewritten",
        IntimacyTier.LIGHT_CONTACT,
        0,
        len(rewritten),
    )
    reviewer = _Reviewer(
        _pass_review(intimacy_claims=(initial_claim,)),
        _pass_review(intimacy_claims=(final_claim,)),
    )

    result = run_reply_quality_gate(
        candidate,
        _context(intimacy_ceiling=IntimacyTier.LIGHT_CONTACT),
        reviewer=reviewer,
        rewriter=_Rewriter(rewritten),
    )

    assert result.status is QualityGateStatus.BLOCKED
    assert result.violation_codes == ("UNSOLICITED_INTIMACY",)
    assert result.reviewer_calls == 2
    assert result.rewrite_calls == 1


def test_reviewer_classified_request_allows_claim_within_ceiling() -> None:
    candidate = "Synthetic requested contact."
    claim = IntimacyClaim(
        "intimacy.requested",
        IntimacyTier.LIGHT_CONTACT,
        0,
        len(candidate),
    )

    result = run_reply_quality_gate(
        candidate,
        _context(intimacy_ceiling=IntimacyTier.LIGHT_CONTACT),
        reviewer=_Reviewer(
            _pass_review(
                intimacy_request=IntimacyRequest.REQUESTED,
                intimacy_claims=(claim,),
            )
        ),
        rewriter=_NeverRewrite(),
    )

    assert result.status is QualityGateStatus.ACCEPTED
    assert result.rewrite_calls == 0


def test_rewrite_blocks_if_request_classification_changes() -> None:
    result = run_reply_quality_gate(
        "<CONTROL>rewrite this",
        _context(),
        reviewer=_Reviewer(
            _pass_review(intimacy_request=IntimacyRequest.REQUESTED),
            _pass_review(intimacy_request=IntimacyRequest.NONE),
        ),
        rewriter=_Rewriter("Safe rewritten candidate."),
    )

    assert result.status is QualityGateStatus.BLOCKED
    assert result.error_code == "INTIMACY_REQUEST_INCONSISTENT"
    assert result.reviewer_calls == 2
    assert result.rewrite_calls == 1


def test_completed_reviewer_rejects_a_second_external_claim_source() -> None:
    candidate = "Synthetic contact."
    result = run_reply_quality_gate(
        candidate,
        _context(intimacy_ceiling=IntimacyTier.LIGHT_CONTACT),
        reviewer=_Reviewer(_pass_review()),
        rewriter=_NeverRewrite(),
        intimacy_claims=(
            IntimacyClaim(
                "intimacy.external",
                IntimacyTier.LIGHT_CONTACT,
                0,
                len(candidate),
            ),
        ),
    )

    assert result.status is QualityGateStatus.BLOCKED
    assert result.error_code == "INTIMACY_CLAIM_SOURCE_CONFLICT"
    assert result.rewrite_calls == 0


def test_reviewer_and_policy_codes_are_stably_deduplicated() -> None:
    candidate = "Synthetic contact."
    claim = IntimacyClaim(
        "intimacy.deduplicated",
        IntimacyTier.LIGHT_CONTACT,
        0,
        len(candidate),
    )
    first = ReviewResult(
        ReviewStatus.COMPLETED,
        ReviewVerdict.REWRITE,
        (
            ReviewerViolation(
                "UNSOLICITED_INTIMACY",
                "hard",
                0,
                len(candidate),
            ),
        ),
        ReviewerScores(80, 80, 80, 80),
        IntimacyRequest.NONE,
        (claim,),
    )
    rewriter = _Rewriter("Safe rewritten candidate.")

    result = run_reply_quality_gate(
        candidate,
        _context(intimacy_ceiling=IntimacyTier.LIGHT_CONTACT),
        reviewer=_Reviewer(first, _pass_review()),
        rewriter=rewriter,
    )

    assert result.status is QualityGateStatus.ACCEPTED
    assert rewriter.violation_codes == [("UNSOLICITED_INTIMACY",)]


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
        IntimacyRequest.NONE,
        (),
    )
    final = ReviewResult(
        ReviewStatus.COMPLETED,
        ReviewVerdict.REWRITE,
        (ReviewerViolation("STYLE_DRIFT", "soft", 0, 5),),
        ReviewerScores(90, 90, 90, 90),
        IntimacyRequest.NONE,
        (),
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
