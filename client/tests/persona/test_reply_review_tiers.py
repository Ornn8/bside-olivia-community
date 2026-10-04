"""Synthetic acceptance of graded review; no user text or provider calls."""

from datetime import datetime, timezone
from dataclasses import replace

import pytest

from runtime.reply import reply_model_quality as model_quality
from runtime.reply.reply_context import IntimacyRequest, ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_context import IntimacyTier
from runtime.reply.reply_policy import IntimacyClaim, scan_reply
from runtime.reply.reply_quality_gate import QualityGateStatus, run_reply_quality_gate, _without_unsupported_facts
from runtime.reply.reply_reviewer import ReviewerScores, ReviewerViolation, ReviewResult, ReviewStatus, ReviewVerdict


def context(mode=ReplyMode.TEXT_LETTER):
    return ReplyContext.create(mode, trusted_time=TrustedTime(datetime(2026, 10, 4, tzinfo=timezone.utc)),
                               future_im_enabled=mode is ReplyMode.FUTURE_IM)


def result(*violations, verdict=ReviewVerdict.REWRITE):
    return ReviewResult(ReviewStatus.COMPLETED, verdict, tuple(violations),
                        ReviewerScores(95, 95, 95, 95), IntimacyRequest.NONE, ())


class Reviewer:
    def __init__(self, *reviews, confirmed=()):
        self.reviews, self.confirmed, self.candidates = iter(reviews), confirmed, []

    def review(self, candidate, context):
        self.candidates.append(candidate)
        return next(self.reviews)

    def confirmed_rewrite_evidence(self, candidate, context, review):
        return tuple(item for item in self.confirmed if item in review.violations)


class NoRewrite:
    def rewrite(self, *args):
        raise AssertionError('a preference or safe local repair must not require another generation')


class FixedRewrite:
    def __init__(self, text):
        self.text = text

    def rewrite(self, *args):
        return self.text


@pytest.mark.parametrize('mode', tuple(ReplyMode))
@pytest.mark.parametrize('code', ['STYLE_DRIFT', 'GENERIC_COUNSELOR'])
@pytest.mark.parametrize('verdict', [ReviewVerdict.REWRITE, ReviewVerdict.BLOCK])
def test_soft_preferences_keep_the_reviewed_text_without_an_extra_call(mode, code, verdict):
    candidate = 'x' * 190  # Also meets the video length contract.
    reviewer = Reviewer(result(ReviewerViolation(code, 'soft', 0, 10), verdict=verdict))
    actual = run_reply_quality_gate(candidate, context(mode), reviewer=reviewer,
                                    rewriter=NoRewrite(), allow_rewrite=False)
    assert actual.status is QualityGateStatus.ACCEPTED_WITH_WARNINGS
    assert actual.text == candidate
    assert actual.reviewer_calls == len(reviewer.candidates) == 1
    assert actual.rewrite_calls == 0


def test_confirmed_local_fact_is_removed_before_rewrite_and_other_expression_survives():
    fact = '我记得你上周在那家书店挑了三本书。'
    ordinary = '今天能收到你的消息，我很高兴。先慢慢聊，不用为了凑够什么而赶着回复我。'
    candidate = fact + ordinary
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(fact))
    reviewer = Reviewer(result(violation), confirmed=(violation,))
    actual = run_reply_quality_gate(candidate, context(), reviewer=reviewer, rewriter=NoRewrite())
    assert actual.status is QualityGateStatus.ACCEPTED_WITH_WARNINGS
    assert actual.text == ordinary
    assert actual.reviewer_calls == 1 and actual.rewrite_calls == 0
    assert actual.deterministic_checks == 2


@pytest.mark.parametrize('layer,code', [('focus_response', 'GENERIC_COUNSELOR'),
                                       ('autonomy_life', 'IDENTITY_DRIFT')])
def test_a_layer_score_without_proof_is_a_warning_not_an_entire_letter_rewrite(layer, code):
    layers = [model_quality._LayerResult(name, 2, (), False,
              intimacy_request=IntimacyRequest.NONE if name == 'identity_boundary' else None)
              for name in model_quality._LAYER_SPECS]
    index = next(i for i, item in enumerate(layers) if item.layer == layer)
    layers[index] = model_quality._LayerResult(layer, 0, (code,), True)
    aggregate = model_quality._aggregate_layer_results(layers, candidate='A synthetic reply.', evidence_bound=True)
    assert aggregate['verdict'] == 'pass'
    assert aggregate['violations'][0]['severity'] == 'soft'


@pytest.mark.parametrize('kind', ['forced_question', 'generic_assistant_tone', 'fixed_structure',
                                 'forced_uplift', 'voice_mismatch'])
def test_proven_style_preference_does_not_become_a_hard_delivery_requirement(kind):
    layers = [model_quality._LayerResult(name, 2, (), False,
              intimacy_request=IntimacyRequest.NONE if name == 'identity_boundary' else None)
              for name in model_quality._LAYER_SPECS]
    evidence = model_quality._HardReviewEvidence('style', 'STYLE_DRIFT', 0, 5, kind, 'none', 'STYLE')
    layers[1] = model_quality._LayerResult('voice_style', 0, ('STYLE_DRIFT',), True,
                                          hard_evidence=(evidence,))
    aggregate = model_quality._aggregate_layer_results(layers, candidate='A synthetic reply.', evidence_bound=True)
    assert aggregate['verdict'] == 'pass'
    assert aggregate['violations'][0]['severity'] == 'soft'


def test_proven_text_integrity_still_requires_repair():
    layers = [model_quality._LayerResult(name, 2, (), False,
              intimacy_request=IntimacyRequest.NONE if name == 'identity_boundary' else None)
              for name in model_quality._LAYER_SPECS]
    evidence = model_quality._HardReviewEvidence('shape', 'STYLE_DRIFT', 0, 5, 'length_or_mode', 'none', 'BROKEN')
    layers[1] = model_quality._LayerResult('voice_style', 0, ('STYLE_DRIFT',), True,
                                          hard_evidence=(evidence,))
    aggregate = model_quality._aggregate_layer_results(layers, candidate='A synthetic reply.', evidence_bound=True)
    assert aggregate['verdict'] == 'rewrite'
    assert aggregate['violations'][0]['severity'] == 'hard'


def test_short_complete_reply_is_preserved_after_a_confirmed_history_sentence_is_removed():
    fact, reply = '你上周在云湖哭了一夜。', '想你了。晚安。'
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(fact))
    actual = run_reply_quality_gate(fact + reply, context(),
                                   reviewer=Reviewer(result(violation), confirmed=(violation,)),
                                   rewriter=NoRewrite())
    assert actual.accepted and actual.text == reply
    assert actual.reviewer_calls == 1 and actual.rewrite_calls == 0


def test_partial_fact_span_removes_its_whole_conditional_sentence():
    candidate = '如果你去了那家店，我也去过。想你了。'
    start, end = candidate.index('我'), candidate.index('。')
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', start, end)
    repaired = _without_unsupported_facts(candidate, (violation,), ReplyMode.TEXT_LETTER)
    assert repaired == ('想你了。', ())


def test_unchanged_contact_claim_is_mapped_and_still_checked_after_fact_removal():
    fact, contact = '你昨天去了云湖。', '我握住你的手。'
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(fact))
    claim = IntimacyClaim('contact', IntimacyTier.LIGHT_CONTACT, len(fact), len(fact + contact))
    repaired, claims = _without_unsupported_facts(fact + contact, (violation,), ReplyMode.TEXT_LETTER, (claim,))
    assert repaired == contact
    assert claims == (IntimacyClaim('contact', IntimacyTier.LIGHT_CONTACT, 0, len(contact)),)
    assert not scan_reply(repaired, context(), intimacy_claims=claims).passed


def test_partial_contact_claim_overlap_falls_back_to_reviewed_rewrite():
    candidate = '你昨天去了云湖。我握住你的手。'
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, 3)
    claim = IntimacyClaim('contact', IntimacyTier.LIGHT_CONTACT, 4, len(candidate))
    assert _without_unsupported_facts(candidate, (violation,), ReplyMode.TEXT_LETTER, (claim,)) is None


def test_unknown_finding_and_protocol_are_not_accepted_as_preferences():
    for code, candidate in [('UNKNOWN_SOFT', '普通回复。'), ('STYLE_DRIFT', '<CONTROL>普通回复。')]:
        actual = run_reply_quality_gate(candidate, context(),
                                       reviewer=Reviewer(result(ReviewerViolation(code, 'soft', 0, 2))),
                                       rewriter=NoRewrite(), allow_rewrite=False)
        assert not actual.accepted


@pytest.mark.parametrize('code', ['UNKNOWN_SOFT', 'BOUNDARY_BREACH'])
def test_final_local_fact_cleanup_does_not_hide_an_unresolved_nonstyle_finding(code):
    fact, reply = '你昨天去了云湖。', '想你了。晚安。'
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(fact))
    unresolved = ReviewerViolation(code, 'soft', len(fact), len(fact + reply))
    reviewer = Reviewer(result(verdict=ReviewVerdict.PASS), result(violation, unresolved), confirmed=(violation,))
    actual = run_reply_quality_gate('<CONTROL>初稿。', context(), reviewer=reviewer,
                                   rewriter=FixedRewrite(fact + reply))
    assert not actual.accepted
    assert actual.text == fact + reply
    assert code in actual.violation_codes


def test_quoted_fact_sentence_removal_includes_its_closing_quote():
    candidate = '你上周说过：“我去了云湖。”想你了。'
    start, end = candidate.index('我去了'), candidate.index('。')
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', start, end)
    actual = run_reply_quality_gate(candidate, context(),
                                   reviewer=Reviewer(result(violation), confirmed=(violation,)),
                                   rewriter=NoRewrite())
    assert actual.accepted and actual.text == '想你了。'
    assert actual.reviewer_calls == 1 and actual.rewrite_calls == 0


def test_local_deletion_inside_a_multisentence_quote_keeps_its_matching_quote():
    candidate = '我说：“想你了。你昨天去了云湖。”晚安。'
    start, end = candidate.index('你昨天'), candidate.index('云湖') + 2
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', start, end)
    reviewer = Reviewer(result(violation), confirmed=(violation,))
    actual = run_reply_quality_gate(candidate, context(), reviewer=reviewer,
                                   rewriter=NoRewrite())
    assert actual.accepted and actual.text == '我说：“想你了。”晚安。'
    assert actual.reviewer_calls == 1 and actual.rewrite_calls == 0


@pytest.mark.parametrize('leading,trailing', [('  ', ''), ('', '  '), ('  ', '  ')])
def test_valid_contact_claim_is_preserved_when_local_repair_strips_edge_whitespace(leading, trailing):
    fact, contact = '你昨天去了云湖。', '我握住你的手。'
    candidate = fact + leading + contact + trailing
    violation = ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(fact))
    claim = IntimacyClaim('contact', IntimacyTier.LIGHT_CONTACT, len(fact), len(candidate))
    review = replace(result(violation), intimacy_request=IntimacyRequest.REQUESTED, intimacy_claims=(claim,))
    ctx = context()
    ctx = replace(ctx, private_behavior=replace(ctx.private_behavior, intimacy_ceiling=IntimacyTier.LIGHT_CONTACT))
    actual = run_reply_quality_gate(candidate, ctx, reviewer=Reviewer(review, confirmed=(violation,)),
                                   rewriter=NoRewrite())
    assert actual.accepted and actual.text == contact
    assert actual.reviewer_calls == 1 and actual.rewrite_calls == 0
