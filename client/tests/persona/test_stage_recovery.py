"""A retry resumes only successful frozen stages; it never skips final review."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from runtime.reply.reply_context import IntimacyRequest, IntimacyTier, ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyResult, ReplyState
from runtime.reply.reply_policy import IntimacyClaim
from runtime.reply.reply_quality_gate import run_reply_quality_gate
from runtime.reply.reply_reviewer import (
    ReviewerScores, ReviewerViolation, ReviewResult, ReviewStatus, ReviewVerdict,
    TrustedCharacterReply, TrustedReviewEvidence,
)
from runtime.reply.stage_recovery import StageRecovery, canonical_hash


ORIGINAL = '昨天你说过要爬泰山。'
REPAIRED = '具体月份我记不准。'
MESSAGES = ({'role': 'user', 'content': '我没有说过爬泰山。'},)


def _context():
    return ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
        trusted_time=TrustedTime(datetime(2026, 10, 3, tzinfo=timezone.utc)))


def _pass():
    return ReviewResult(ReviewStatus.COMPLETED, ReviewVerdict.PASS, (),
        ReviewerScores(100, 100, 100, 100), IntimacyRequest.NONE, ())


def _unavailable():
    return ReviewResult(ReviewStatus.UNAVAILABLE, ReviewVerdict.UNAVAILABLE, (),
        ReviewerScores(0, 0, 0, 0), None, None, 'REVIEWER_UNAVAILABLE')


class FactReviewer:
    def __init__(self, fail_final=False):
        self.calls = 0
        self.evidence_calls = 0
        self.fail_final = fail_final
        self.evidence = {}

    def review_with_messages(self, candidate, context, generation_messages, *, trusted_evidence=TrustedReviewEvidence()):
        self.calls += 1
        if candidate == ORIGINAL:
            span = ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(candidate))
            self.evidence[candidate] = (span,)
            return ReviewResult(ReviewStatus.COMPLETED, ReviewVerdict.REWRITE, (span,),
                ReviewerScores(90, 0, 100, 100), IntimacyRequest.NONE, ())
        if self.fail_final:
            self.fail_final = False
            return _unavailable()
        return _pass()

    def confirmed_rewrite_evidence(self, candidate, context, review):
        self.evidence_calls += 1
        return self.evidence.pop(candidate)


class FactRewriter:
    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    def rewrite_with_evidence(self, candidate, context, codes, messages, confirmed):
        self.calls += 1
        assert confirmed == (ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(ORIGINAL)),)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError('REWRITE_PROVIDER_UNAVAILABLE')
        return REPAIRED


def _two_attempts(*, fail_rewrite=False, fail_final=False):
    cache = StageRecovery('same-turn:input-0', lambda: True)
    base_review, base_rewrite = FactReviewer(fail_final), FactRewriter(fail_rewrite)
    qualities = []
    writer_calls = 0
    for attempt in range(2):
        token = cache.token()
        candidate = cache.get_writer(request_id=f'attempt-{attempt}')
        if candidate is None:
            writer_calls += 1
            cache.mark_call('writer')
            candidate = ReplyResult(f'attempt-{attempt}', ReplyState.COMPLETED, ORIGINAL)
            assert cache.put_writer(candidate, token=token)
        qualities.append(run_reply_quality_gate(candidate.text, _context(),
            reviewer=cache.wrap_reviewer(base_review), rewriter=cache.wrap_rewriter(base_rewrite),
            generation_messages=MESSAGES))
    return cache, qualities, writer_calls, base_review, base_rewrite


def test_rewrite_transient_retry_reuses_writer_and_adjudicated_first_review():
    cache, qualities, writer_calls, reviewer, rewriter = _two_attempts(fail_rewrite=True)

    assert not qualities[0].accepted and qualities[0].error_code == 'REWRITE_PROVIDER_UNAVAILABLE'
    assert qualities[1].accepted and qualities[1].text == REPAIRED
    assert writer_calls == 1
    assert reviewer.calls == 2 and reviewer.evidence_calls == 1
    assert rewriter.calls == 2  # The failed rewrite is not cached.
    assert cache.actual_calls == {'writer': 1, 'reviewer': 2, 'rewriter': 2}
    assert cache.cache_hits == {'writer': 1, 'reviewer': 1, 'rewriter': 0}


def test_final_review_retry_reuses_successful_rewrite_but_cannot_deliver_unreviewed_text():
    cache, qualities, writer_calls, reviewer, rewriter = _two_attempts(fail_final=True)

    assert not qualities[0].accepted and qualities[0].error_code == 'REVIEWER_UNAVAILABLE'
    assert qualities[1].accepted and qualities[1].text == REPAIRED
    assert writer_calls == 1
    assert reviewer.calls == 3 and reviewer.evidence_calls == 1
    assert rewriter.calls == 1
    assert cache.actual_calls == {'writer': 1, 'reviewer': 3, 'rewriter': 1}
    assert cache.cache_hits == {'writer': 1, 'reviewer': 1, 'rewriter': 1}


def test_changed_input_and_late_result_never_reuse_previous_candidate():
    current = True
    cache = StageRecovery('revision-0', lambda: current)
    old_token = cache.token()
    candidate = ReplyResult('attempt-1', ReplyState.COMPLETED, REPAIRED)
    assert cache.put_writer(candidate, token=old_token)
    assert cache.get_writer(request_id='attempt-2').request_id == 'attempt-2'
    cache.bind_input('revision-1', lambda: current)
    assert cache.get_writer() is None
    assert not cache.put_writer(candidate, token=old_token)
    assert cache.put_writer(candidate, token=cache.token())
    current = False
    assert cache.get_writer() is None
    assert not cache.put_writer(candidate, token=cache.token())


@pytest.mark.parametrize('result', [
    ReplyResult('failed', ReplyState.FAILED, REPAIRED, 'LLM_TIMEOUT'),
    ReplyResult('empty', ReplyState.COMPLETED, '   '),
    ReplyResult('bad', ReplyState.COMPLETED, REPAIRED, 'LLM_INTERNAL'),
])
def test_failed_or_empty_writer_candidate_is_not_cached(result):
    cache = StageRecovery('input', lambda: True)
    assert not cache.put_writer(result, token=cache.token())
    assert cache.get_writer() is None


class CountingReviewer:
    def __init__(self):
        self.calls = 0

    def review_with_messages(self, *args, **kwargs):
        self.calls += 1
        return _pass()


@pytest.mark.parametrize('changed', ['candidate', 'context', 'messages', 'trusted', 'claims'])
def test_review_cache_binds_every_frozen_input(changed):
    cache = StageRecovery('input', lambda: True)
    base = CountingReviewer()
    context, candidate, messages, trusted = _context(), REPAIRED, MESSAGES, TrustedReviewEvidence()
    port = cache.wrap_reviewer(base, key_context={'claims': ()})
    assert port.review_with_messages(candidate, context, messages, trusted_evidence=trusted) == _pass()
    assert port.review_with_messages(candidate, context, messages, trusted_evidence=trusted) == _pass()
    assert base.calls == 1
    if changed == 'candidate':
        candidate += '补充。'
    elif changed == 'context':
        context = replace(context, trusted_time=TrustedTime(context.trusted_time.instant + timedelta(seconds=1)))
    elif changed == 'messages':
        messages = ({'role': 'user', 'content': '我刚才更正了。'},)
    elif changed == 'trusted':
        trusted = TrustedReviewEvidence((TrustedCharacterReply('original:1', '我没有提过这件事。'),))
    else:
        port = cache.wrap_reviewer(base, key_context={'claims': ('different-claim',)})
    port.review_with_messages(candidate, context, messages, trusted_evidence=trusted)
    assert base.calls == 2


def test_incomplete_review_is_not_cached_and_exception_classification_is_preserved():
    class Reviewer:
        calls = 0

        def review(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('REVIEWER_UNAVAILABLE')
            return _unavailable() if self.calls == 2 else _pass()

    cache, base = StageRecovery('input', lambda: True), Reviewer()
    port = cache.wrap_reviewer(base)
    with pytest.raises(RuntimeError, match='^REVIEWER_UNAVAILABLE$'):
        port.review(REPAIRED, _context())
    assert port.review(REPAIRED, _context()).status is ReviewStatus.UNAVAILABLE
    assert port.review(REPAIRED, _context()).status is ReviewStatus.COMPLETED
    assert port.review(REPAIRED, _context()).status is ReviewStatus.COMPLETED
    assert base.calls == 3 and cache.cache_hits['reviewer'] == 1


def test_empty_rewrite_and_invalid_normalization_are_not_cached():
    class Rewriter:
        calls = 0

        def rewrite(self, *args):
            self.calls += 1
            return ('   ', '{invalid-json}', REPAIRED)[min(self.calls - 1, 2)]

    def validate(text):
        if text.startswith('{'):
            raise ValueError('private rejected output')
        return text

    cache, base = StageRecovery('input', lambda: True), Rewriter()
    port = cache.wrap_rewriter(base, validate=validate)
    assert port.rewrite(ORIGINAL, _context(), ('MEMORY_FABRICATION',)) == '   '
    with pytest.raises(RuntimeError, match='^REWRITE_OUTPUT_INVALID$'):
        port.rewrite(ORIGINAL, _context(), ('MEMORY_FABRICATION',))
    assert port.rewrite(ORIGINAL, _context(), ('MEMORY_FABRICATION',)) == REPAIRED
    assert port.rewrite(ORIGINAL, _context(), ('MEMORY_FABRICATION',)) == REPAIRED
    assert base.calls == 3 and cache.cache_hits['rewriter'] == 1


def test_late_review_result_cannot_fill_cache_for_new_input():
    cache = StageRecovery('old-input', lambda: True)

    class LateReviewer:
        calls = 0

        def review(self, *args):
            self.calls += 1
            if self.calls == 1:
                cache.bind_input('new-input', lambda: True)
            return _pass()

    base = LateReviewer()
    port = cache.wrap_reviewer(base)
    port.review(REPAIRED, _context())
    port.review(REPAIRED, _context())
    assert base.calls == 2


@pytest.mark.parametrize('close_method', ['invalidate', 'close'])
def test_closing_turn_rejects_late_writer_and_review_results(close_method):
    cache = StageRecovery('input', lambda: True)
    old_token = cache.token()

    class ClosingReviewer:
        calls = 0

        def review(self, *args):
            self.calls += 1
            getattr(cache, close_method)()
            return _pass()

    base = ClosingReviewer()
    port = cache.wrap_reviewer(base)
    assert port.review(REPAIRED, _context()).status is ReviewStatus.COMPLETED
    assert cache.token() != old_token
    assert not cache.put_writer(ReplyResult('late', ReplyState.COMPLETED, REPAIRED), token=old_token)
    assert cache.get_writer() is None
    cache.bind_input('new-input', lambda: True)
    port.review(REPAIRED, _context())
    assert base.calls == 2


def test_unknown_key_types_disable_caching_without_inspecting_object_repr():
    class PrivateObject:
        def __repr__(self):
            raise AssertionError('must not stringify unknown private objects')

    first = canonical_hash(_context(), MESSAGES)
    assert isinstance(first, str) and len(first) == 64
    assert first == canonical_hash(_context(), MESSAGES)
    assert first != canonical_hash(_context(), ({'role': 'user', 'content': '另一封原文。'},))
    assert canonical_hash(PrivateObject()) is None
    assert canonical_hash({'secret': PrivateObject()}) is None
    cache, base = StageRecovery('input', lambda: True), CountingReviewer()
    port = cache.wrap_reviewer(base, key_context=PrivateObject())
    port.review_with_messages(REPAIRED, _context(), MESSAGES)
    port.review_with_messages(REPAIRED, _context(), MESSAGES)
    assert base.calls == 2


@pytest.mark.parametrize('stage', ['reviewer', 'rewriter'])
def test_closed_input_never_starts_a_new_provider_stage(stage):
    cache = StageRecovery('input', lambda: True)
    calls = []

    class Provider:
        def review(self, *args):
            calls.append('reviewer')
            return _pass()

        def rewrite(self, *args):
            calls.append('rewriter')
            return REPAIRED

    cache.close()
    with pytest.raises(RuntimeError, match='^JEV_INPUT_SUPERSEDED$'):
        if stage == 'reviewer':
            cache.wrap_reviewer(Provider()).review(ORIGINAL, _context())
        else:
            cache.wrap_rewriter(Provider()).rewrite(ORIGINAL, _context(), ('MEMORY_FABRICATION',))
    assert calls == []
    assert cache.actual_calls[stage] == 0


def test_late_hard_review_after_cancellation_cannot_start_rewrite_or_final_review():
    cache = StageRecovery('input', lambda: True)
    entered, release = Event(), Event()

    class LateReviewer(FactReviewer):
        def review_with_messages(self, *args, **kwargs):
            result = super().review_with_messages(*args, **kwargs)
            entered.set()
            assert release.wait(3)
            return result

    reviewer, rewriter = LateReviewer(), FactRewriter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(run_reply_quality_gate, ORIGINAL, _context(),
            reviewer=cache.wrap_reviewer(reviewer), rewriter=cache.wrap_rewriter(rewriter),
            generation_messages=MESSAGES)
        try:
            assert entered.wait(3)
            cache.invalidate()
        finally:
            release.set()
        result = pending.result(timeout=5)

    assert not result.accepted
    assert reviewer.calls == 1 and rewriter.calls == 0
    assert cache.actual_calls['reviewer'] == 1 and cache.actual_calls['rewriter'] == 0


def test_late_successful_rewrite_after_cancellation_cannot_start_final_provider_review():
    cache = StageRecovery('input', lambda: True)
    entered, release = Event(), Event()

    class LateRewriter(FactRewriter):
        def rewrite_with_evidence(self, *args):
            result = super().rewrite_with_evidence(*args)
            entered.set()
            assert release.wait(3)
            return result

    reviewer, rewriter = FactReviewer(), LateRewriter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(run_reply_quality_gate, ORIGINAL, _context(),
            reviewer=cache.wrap_reviewer(reviewer), rewriter=cache.wrap_rewriter(rewriter),
            generation_messages=MESSAGES)
        try:
            assert entered.wait(3)
            cache.invalidate()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='^JEV_INPUT_SUPERSEDED$'):
            pending.result(timeout=5)

    assert reviewer.calls == 1 and rewriter.calls == 1


def test_confirmed_evidence_binds_adjudicated_request_but_not_changed_permissions():
    class RequestedReviewer(FactReviewer):
        def review_with_messages(self, *args, **kwargs):
            return replace(super().review_with_messages(*args, **kwargs),
                           intimacy_request=IntimacyRequest.REQUESTED)

    cache, base = StageRecovery('input', lambda: True), RequestedReviewer()
    context = _context()
    adjudicated = replace(context, intimacy_request=IntimacyRequest.REQUESTED)
    first = cache.wrap_reviewer(base)
    review = first.review_with_messages(ORIGINAL, context, MESSAGES)
    evidence = first.confirmed_rewrite_evidence(ORIGINAL, adjudicated, review)
    retry = cache.wrap_reviewer(base)
    cached_review = retry.review_with_messages(ORIGINAL, context, MESSAGES)
    assert retry.confirmed_rewrite_evidence(ORIGINAL, adjudicated, cached_review) == evidence
    assert base.calls == base.evidence_calls == 1
    assert context.private_behavior.granted_intimacy is IntimacyTier.NONE
    assert adjudicated.private_behavior == context.private_behavior
    altered = replace(adjudicated,
        private_behavior=replace(adjudicated.private_behavior, home_history_allowed=True))
    with pytest.raises(RuntimeError, match='^REWRITE_EVIDENCE_INVALID$'):
        retry.confirmed_rewrite_evidence(ORIGINAL, altered, cached_review)
    assert base.evidence_calls == 1


def test_cached_request_review_cannot_resolve_conflicting_explicit_claims():
    cache, base, rewriter = StageRecovery('input', lambda: True), FactReviewer(), FactRewriter()
    claims = (IntimacyClaim('explicit:1', IntimacyTier.LIGHT_CONTACT, 0, len(ORIGINAL)),)
    for _ in range(2):
        result = run_reply_quality_gate(ORIGINAL, _context(),
            reviewer=cache.wrap_reviewer(base, key_context=claims),
            rewriter=cache.wrap_rewriter(rewriter), generation_messages=MESSAGES,
            intimacy_claims=claims)
        assert not result.accepted and result.error_code == 'INTIMACY_CLAIM_SOURCE_CONFLICT'
    assert base.calls == 1 and rewriter.calls == 0


def test_cached_rewrite_cannot_override_inconsistent_final_intimacy_judgment():
    class InconsistentReviewer(FactReviewer):
        def review_with_messages(self, candidate, *args, **kwargs):
            review = super().review_with_messages(candidate, *args, **kwargs)
            return replace(review, intimacy_request=(IntimacyRequest.REQUESTED
                if candidate == ORIGINAL else IntimacyRequest.NONE))

    cache, base, rewriter = StageRecovery('input', lambda: True), InconsistentReviewer(), FactRewriter()
    for _ in range(2):
        result = run_reply_quality_gate(ORIGINAL, _context(),
            reviewer=cache.wrap_reviewer(base), rewriter=cache.wrap_rewriter(rewriter),
            generation_messages=MESSAGES)
        assert not result.accepted and result.error_code == 'INTIMACY_REQUEST_INCONSISTENT'
    assert base.calls == 2 and base.evidence_calls == 1 and rewriter.calls == 1
