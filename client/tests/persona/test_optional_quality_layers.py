"""Optional expression reviews cannot veto independently checked reply content."""
import asyncio
import json

import pytest

from runtime.reply import jev_questions, reply_model_quality as quality
from runtime.reply.reply_context import ReplyContext, ReplyMode
from runtime.reply.reply_quality_gate import QualityGateStatus, run_reply_quality_gate
from runtime.reply.reply_reviewer import ReviewVerdict
from tests.persona.test_reply_model_quality import (
    FailingQualityGateway, SequencedQualityGateway, _context as _existing_context, _layer_payload,
    _adjudication_payload, _hard_evidence_payload, _layer_score_payload,
    _REVIEW_LAYERS, _run_diagnostic_review,
)


def _context(mode=ReplyMode.TEXT_LETTER):
    return ReplyContext.create(mode, trusted_time=_existing_context().trusted_time,
                               future_im_enabled=True)


class NeverRewrite:
    def rewrite(self, *args, **kwargs):
        raise AssertionError('optional review failure must not buy a rewrite')


@pytest.fixture(autouse=True)
def no_live_review(monkeypatch):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
@pytest.mark.parametrize('layer,code', [
    ('focus_response', 'FOCUS_REVIEW_UNAVAILABLE'),
    ('autonomy_life', 'AUTONOMY_REVIEW_UNAVAILABLE'),
])
@pytest.mark.parametrize('failure', ['transport', 'contract'])
def test_optional_failed_layer_preserves_checked_body_without_rewrite(mode, layer, code, failure):
    candidate = '今天先歇一会儿，想聊的时候再叫我。'
    if failure == 'transport':
        gateway = FailingQualityGateway(failure='layer', failing_layer=layer)
    else:
        replies = {name: [_layer_payload(name)] for name in _REVIEW_LAYERS}
        replies[layer] = ['{']
        gateway = SequencedQualityGateway(candidate=candidate, reviews=[], layer_reviews=replies)
    result, reviewer = _run_diagnostic_review(gateway, candidate, _context(mode))
    assert result.verdict is ReviewVerdict.PASS
    assert [(v.code, v.severity) for v in result.violations] == [(code, 'soft')]
    assert reviewer.last_failure_diagnostics[0].layer == layer
    gate = run_reply_quality_gate(candidate, _context(mode),
        reviewer=type('FrozenReview', (), {'review': lambda self, *a: result})(),
        rewriter=NeverRewrite())
    assert gate.status is QualityGateStatus.ACCEPTED_WITH_WARNINGS
    assert gate.text == candidate and gate.rewrite_calls == 0
    calls = [request['layer'] for request in gateway.review_requests]
    assert all(calls.count(name) == 1 for name in _REVIEW_LAYERS if name != layer)
    assert calls.count(layer) <= 2
    assert gateway.adjudication_requests == []


@pytest.mark.parametrize('layer,code,kind', [
    ('identity_boundary', 'STAGE_DRIFT', 'relationship'),
    ('continuity_memory', 'MEMORY_FABRICATION', 'past_fact'),
    ('voice_style', 'STYLE_DRIFT', 'length_or_mode'),
])
def test_optional_failure_cannot_hide_confirmed_required_finding(layer, code, kind):
    candidate = 'Synthetic unsupported claim.'
    evidence = _hard_evidence_payload(candidate, code, claim_kind=kind)
    replies = {name: [_layer_payload(name)] for name in _REVIEW_LAYERS}
    replies['focus_response'] = ['{']
    replies['autonomy_life'] = ['{']
    replies[layer] = [_layer_score_payload(layer, 0, hard_violations=[code],
        drift_detected=True, hard_evidence=[evidence])]
    gateway = SequencedQualityGateway(candidate=candidate, reviews=[], layer_reviews=replies,
        adjudications=[_adjudication_payload(evidence, 'CONFIRM')])
    result, reviewer = _run_diagnostic_review(gateway, candidate, _context())
    assert result.verdict is ReviewVerdict.REWRITE
    assert (code, 'hard') in [(v.code, v.severity) for v in result.violations]
    assert {v.code for v in result.violations if v.severity == 'soft'} == {
        'FOCUS_REVIEW_UNAVAILABLE', 'AUTONOMY_REVIEW_UNAVAILABLE'}
    gate = run_reply_quality_gate(candidate, _context(),
        reviewer=type('FrozenReview', (), {'review': lambda self, *a: result,
            'confirmed_rewrite_evidence': lambda self, *a: tuple(
                v for v in result.violations if v.severity == 'hard')})(),
        rewriter=NeverRewrite(), allow_rewrite=False)
    assert not gate.accepted
    assert code in gate.violation_codes
    assert len(reviewer.last_failure_diagnostics) == 2


def test_optional_failure_still_allows_confirmed_fact_removal_without_rewrite():
    candidate = '我记得上次去过虚构湖边。你来了真好。'
    supported = '你来了真好。'
    evidence = _hard_evidence_payload(candidate, 'MEMORY_FABRICATION',
                                      end=len(candidate) - len(supported))
    replies = {name: [_layer_payload(name)] for name in _REVIEW_LAYERS}
    replies['focus_response'] = ['{']
    replies['continuity_memory'] = [_layer_score_payload('continuity_memory', 0,
        hard_violations=['MEMORY_FABRICATION'], drift_detected=True, hard_evidence=[evidence])]
    result, _ = _run_diagnostic_review(SequencedQualityGateway(candidate=candidate, reviews=[],
        layer_reviews=replies, adjudications=[_adjudication_payload(evidence, 'CONFIRM')]), candidate, _context())
    gate = run_reply_quality_gate(candidate, _context(),
        reviewer=type('FrozenReview', (), {'review': lambda self, *a: result,
            'confirmed_rewrite_evidence': lambda self, *a: tuple(
                v for v in result.violations if v.severity == 'hard')})(),
        rewriter=NeverRewrite())
    assert gate.accepted and gate.text == supported and gate.rewrite_calls == 0
    assert 'FOCUS_REVIEW_UNAVAILABLE' in gate.violation_codes


@pytest.mark.parametrize('layer', ['identity_boundary', 'voice_style', 'continuity_memory'])
def test_hard_review_transport_failure_still_blocks(layer):
    result, reviewer = _run_diagnostic_review(
        FailingQualityGateway(failure='layer', failing_layer=layer), context=_context(ReplyMode.FUTURE_IM))
    assert result.verdict is ReviewVerdict.UNAVAILABLE
    assert result.error_code == 'REVIEWER_UNAVAILABLE'
    assert reviewer.last_failure_diagnostics[0].layer == layer


@pytest.mark.parametrize('layer', ['focus_response', 'autonomy_life'])
def test_optional_layer_cancellation_remains_cancellation(layer):
    class Cancelled(FailingQualityGateway):
        async def complete(self, messages, **kwargs):
            system = str(messages[0].get('content', ''))
            if 'P02_REPLY_REVIEW_JSON' in system:
                request = json.loads(messages[-1]['content'])
                if request['layer'] == layer:
                    raise asyncio.CancelledError()
            return await super().complete(messages, **kwargs)
    with pytest.raises(asyncio.CancelledError):
        _run_diagnostic_review(Cancelled(failure='unused'), context=_context())


@pytest.mark.parametrize('layer,code', [
    ('focus_response', 'FOCUS_REVIEW_UNAVAILABLE'),
    ('autonomy_life', 'AUTONOMY_REVIEW_UNAVAILABLE'),
])
def test_combined_jev_complete_hard_layers_survive_optional_bad_schema(monkeypatch, layer, code):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: object())
    from runtime.reply import jev_quality
    async def reviewed(port, requests, candidate, evidence_bound, **kwargs):
        assert len(requests) == 5
        replies = ['{' if item.name == layer else _layer_payload(item.name) for item, _ in requests]
        return replies, {item.name: [] for item, _ in requests}
    monkeypatch.setattr(jev_quality, 'review_layers_json', reviewed)
    result, reviewer = _run_diagnostic_review(FailingQualityGateway(failure='unused'))
    assert result.verdict is ReviewVerdict.PASS
    assert result.violations[0].code == code
    assert reviewer.last_failure_diagnostics[0].layer == layer


def test_combined_jev_transport_failure_cannot_claim_hard_checks_passed(monkeypatch):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: object())
    from runtime.reply import jev_quality
    async def unavailable(*args, **kwargs):
        raise ValueError('JEV_UNAVAILABLE')
    monkeypatch.setattr(jev_quality, 'review_layers_json', unavailable)
    result, _ = _run_diagnostic_review(FailingQualityGateway(failure='unused'))
    assert result.verdict is ReviewVerdict.UNAVAILABLE


def test_successful_review_clears_optional_failure_receipt():
    gateway = SequencedQualityGateway(candidate='合成正文', reviews=[], layer_reviews={
        name: (['{', _layer_payload(name)] if name == 'focus_response'
               else [_layer_payload(name), _layer_payload(name)]) for name in _REVIEW_LAYERS})
    result, reviewer = _run_diagnostic_review(gateway)
    assert result.verdict is ReviewVerdict.PASS
    assert reviewer.last_failure_diagnostics
    clean = reviewer.review('第二条合成正文', _context())
    assert clean.verdict is ReviewVerdict.PASS
    assert not clean.violations and reviewer.last_failure_diagnostics == ()
