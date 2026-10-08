import json

import pytest

from runtime.reply.reply_context import ReplyMode
from runtime.reply.reply_reviewer import ReviewVerdict
from tests.persona.test_reply_semantic_wiring import Reviewer, Rewriter, execute, envelope
from tests.persona.test_jev_quality import Decisions, transport, request
from runtime.reply.jev_quality import _purpose_state, review_messages, _confirmation_context
from runtime.reply import reply_model_quality as quality


def execute_speech(text, **kwargs):
    from runtime.reply.companion_decision import CompanionDecisionResult, FrozenCompanionDecision
    from tests.persona.test_companion_decision import envelope as companion_envelope, project
    from tests.persona.test_jev_pipeline import plan

    class SpeechPort:
        async def decide(self, turn):
            proposed = plan(kind='audio_speech')
            return CompanionDecisionResult(decision=FrozenCompanionDecision.from_response(turn,
                {**companion_envelope(), 'plan': proposed, 'tasks': project(proposed),
                 'speech_request': dict(mode='story', target_seconds=120, continuation=False)}))

    return execute(text, companion_decision_port=SpeechPort(), **kwargs)


@pytest.mark.parametrize('mode', list(ReplyMode))
def test_every_delivery_mode_reviews_actual_body(mode):
    reviewer = Reviewer(ReviewVerdict.PASS)
    body = '这是一段完整的回复。' * 19  # 190 characters satisfies video delivery.
    candidate = json.dumps({**envelope(), 'text': body}) if mode is ReplyMode.FUTURE_IM else body
    result, _ = execute(candidate, mode=mode, reviewer=reviewer)
    assert result.state.value == 'completed'
    assert reviewer.seen[0][0] == body
    assert result.reviewer_calls == 1


def test_long_speech_is_reviewed_separately_and_repaired_without_changing_controls():
    script = dict(title='夜里的故事', spoken_text='我听过你昨晚的语音。' * 8, continuation_summary='虚构故事摘要')
    payload = {**envelope(), 'text': '整理好后发给你。', 'speech': script}
    reviewer = Reviewer(ReviewVerdict.PASS, ReviewVerdict.REWRITE, ReviewVerdict.PASS)
    repaired = '这只是故事里的雨声。' * 8
    rewriter = Rewriter(repaired)
    result, _ = execute_speech(json.dumps(payload), mode=ReplyMode.FUTURE_IM, reviewer=reviewer, rewriter=rewriter)
    assert result.state.value == 'completed'
    assert [item[0] for item in reviewer.seen] == [payload['text'], script['spoken_text'], repaired]
    assert json.loads(result.text) == {**payload, 'speech': {**script, 'spoken_text': repaired}}
    assert result.reviewer_calls == 3 and result.rewrite_calls == 1
    import hashlib
    assert result.reviewed_content['speech'] == hashlib.sha256(repaired.encode()).hexdigest()
    plan = next(m['content'] for m in reviewer.seen[1][1] if '<reply_delivery_plan>' in m['content'])
    assert script['spoken_text'] not in plan


def test_failed_speech_review_cannot_release_successful_confirmation():
    payload = {**envelope(), 'speech': dict(title='晚安', spoken_text='无依据的经历。' * 10, continuation_summary='')}
    result, _ = execute_speech(json.dumps(payload), mode=ReplyMode.FUTURE_IM,
                        reviewer=Reviewer(ReviewVerdict.PASS, RuntimeError('private')))
    assert result.state.value == 'failed' and not result.text


def test_one_rewrite_budget_is_shared_by_confirmation_and_speech():
    payload = {**envelope(), 'speech': dict(title='晚安', spoken_text='无依据的经历。' * 10, continuation_summary='')}
    rewrite = Rewriter('更正。' * 20)
    result, _ = execute_speech(json.dumps(payload), mode=ReplyMode.FUTURE_IM,
        reviewer=Reviewer(ReviewVerdict.REWRITE, ReviewVerdict.PASS, ReviewVerdict.REWRITE), rewriter=rewrite)
    assert result.state.value == 'failed' and not result.text
    assert len(rewrite.calls) == 1 and result.rewrite_calls == 1


@pytest.mark.parametrize('mode', list(ReplyMode))
def test_all_modes_confirm_fact_allegations_before_rewriting(monkeypatch, mode):
    port = Decisions(confirm=False)
    value = request()
    value['mode'] = mode.value
    result = transport(monkeypatch, port).review_json(value, model='synthetic', timeout_seconds=5)
    assert result['verdict'] == 'pass'
    assert len(port.calls) == 2


def test_fact_review_and_confirmation_keep_the_full_frozen_original_window():
    rows = [dict(source=f'old:{i}', actor='user', text='十一月' if i == 0 else '别的事情') for i in range(4)]
    layer = quality._LayerAuthority('continuity_memory', '', ('MEMORY_FABRICATION',), '', '', '')
    messages = review_messages(layer, candidate='你说过十一月。', current_user_input='几月？', mode='future_im',
                              memory_evidence={'recent_dialogue': json.dumps(rows)})
    scoped = _purpose_state(layer, messages, {})['input']
    assert scoped['recent_turns'] == rows
    confirmed = _confirmation_context('continuity_fact', {'continuity_memory': scoped})
    assert confirmed['recent_turns'] == rows


def test_story_summary_survives_projection_with_fiction_scope_only():
    from runtime.reply.reply_model_quality import _assembled_memory_evidence
    from runtime.reply.fact_attribution import story_evidence
    metadata = {'source_id': 'speech:prior', 'kind': 'fiction_summary', 'text': '月亮城的小雨继续旅行。'}
    messages = ({'role': 'system', 'content': story_evidence(metadata)},)
    assert 'fiction_summary' in _assembled_memory_evidence(messages)
    assert 'speech:prior' in _assembled_memory_evidence(messages)


def test_every_fact_span_is_checked_and_separately_confirmed(monkeypatch):
    class SpanFindings(Decisions):
        async def ask(self, state, questions, **kwargs):
            answers = await super().ask(state, questions, **kwargs)
            if 'confirmation_rules' not in state:
                for key in questions:
                    code = key.split(':', 1)[1]
                    if code == 'OFF_TURN_REPLY':
                        answers[key] = 'current'  # a whole-reply question, not a span
                    elif code.isupper():
                        answers[key] = 'none'
                    elif code.startswith('fact:'):
                        answers[key] = 'unsupported'
            return answers
    port = SpanFindings()
    result = transport(monkeypatch, port).review_json(request('你站在窗边。你昨天去了车站。'), model='synthetic', timeout_seconds=5)
    memory = [item for item in result['violations'] if item['code'] == 'MEMORY_FABRICATION']
    assert len(memory) == 2
    assert memory[0]['evidence']['end'] <= memory[1]['evidence']['start']
    confirmation = port.calls[1][0]['confirmation_rules']
    assert len(confirmation) == 2
    assert len([key for key in port.calls[1][1] if ':kind:' in key]) == 2


def test_fact_support_cannot_invent_an_original_id(monkeypatch):
    class UnknownSource(Decisions):
        async def ask(self, state, questions, **kwargs):
            answers = await super().ask(state, questions, **kwargs)
            for key in questions:
                if ':fact:' in key:
                    answers[key] = 'invented_source'
            return answers
    with pytest.raises(RuntimeError, match='quality model unavailable'):
        transport(monkeypatch, UnknownSource()).review_json(request(), model='synthetic', timeout_seconds=5)


def test_source_catalog_keeps_speaker_and_time_without_promoting_old_role_words():
    from runtime.reply.jev_quality import _fact_sources
    row = dict(event_id='reply:1:linli', actor='linli', time='2026-09-20T10:00:00Z',
               evidence_kind='statement_only', text='我听过语音。')
    catalog = _fact_sources({'current_user_input': '你听过吗？', 'recent_turns': [row]})
    assert catalog['e0']['kind'] == 'current_input'
    assert catalog['e1']['actor'] == 'linli' and catalog['e1']['evidence_kind'] == 'statement_only'
    assert catalog['e1']['source_id'] == row['event_id'] and catalog['e1']['time'] == row['time']


def test_fiction_summary_is_continuation_data_and_cannot_be_real_fact_support():
    from runtime.reply.jev_quality import _fact_sources
    fiction = {'tag': 'evidence_summary', 'value': {'fragment_id': 'speech.continuation',
        'text': {'kind': 'fiction_summary', 'source_id': 'speech:1', 'text': '用户在月亮城爬山。'}}}
    sources = _fact_sources({'current_user_input': '我累了。', 'selected_memory': [fiction]})
    assert list(sources) == ['e0']


def test_speech_changed_after_review_is_rejected_before_provider_or_send(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from runtime.personal_chat.speech import deliver
    row = dict(speech_delivery_status='PENDING', speech_script=dict(title='晚安',
        spoken_text='后来改过的正文。' * 20, continuation_summary=''),
        content_review={'version': 1, 'hashes': {'speech': 'old-candidate'}})
    with pytest.raises(ValueError, match='SPEECH_REVIEW_CONTENT_CHANGED'):
        asyncio.run(deliver(SimpleNamespace(), row, None))


def test_summary_projection_ignores_archived_body_and_preserves_source():
    from runtime.reply.fact_attribution import story_evidence
    summary = {'kind': 'fiction_summary', 'source_id': 'story:1', 'text': '摘要。',
               'spoken_text': '不应参与检索的正文。' * 1000}
    projected = story_evidence(summary)
    assert 'story:1' in projected and '摘要' in projected
    assert '不应参与检索的正文' not in projected and 'spoken_text' not in projected


def test_rewritten_speech_is_canonicalized_before_fresh_review_and_sealing():
    import hashlib
    payload = {**envelope(), 'speech': dict(title='晚安', spoken_text='无依据的经历。' * 10, continuation_summary='')}
    raw = ('这是故事里的雨声。' + r'\n') * 8
    canonical = raw.replace(r'\n', '\n')
    reviewer = Reviewer(ReviewVerdict.PASS, ReviewVerdict.REWRITE, ReviewVerdict.PASS)
    result, _ = execute_speech(json.dumps(payload), mode=ReplyMode.FUTURE_IM, reviewer=reviewer, rewriter=Rewriter(raw))
    assert result.state.value == 'completed'
    assert reviewer.seen[-1][0] == canonical
    assert json.loads(result.text)['speech']['spoken_text'] == canonical
    assert result.reviewed_content['speech'] == hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.parametrize('hashes', [None, [], 'invalid'])
def test_malformed_review_seal_fails_before_provider(hashes):
    import asyncio
    from types import SimpleNamespace
    from runtime.personal_chat.speech import deliver
    row = dict(speech_script=dict(title='晚安', spoken_text='这是故事。' * 20, continuation_summary=''),
               content_review={'version': 1, 'hashes': hashes})
    with pytest.raises(ValueError, match='SPEECH_REVIEW_CONTENT_CHANGED'):
        asyncio.run(deliver(SimpleNamespace(), row, None))


@pytest.mark.parametrize('scope', ['story', 'asmr', 'asmr_story'])
def test_actual_pipeline_uses_frozen_speech_scope_and_only_prior_summary(scope):
    import asyncio
    from datetime import datetime, timezone
    from runtime.personal_chat.presentation import CURRENT
    from runtime.reply.companion_decision import FrozenCompanionDecision, CompanionDecisionResult
    from runtime.reply.reply_pipeline import ReplyPipeline
    from runtime.reply.reply_context import ReplyContext, TrustedTime
    from reply_orchestrator import ReplyRequest
    from tests.persona.test_reply_semantic_wiring import Engine
    from tests.persona.test_companion_decision import envelope as companion_envelope, project
    from tests.persona.test_jev_pipeline import plan
    class Port:
        async def decide(self, turn):
            assert turn.speech_enabled
            proposed = plan(kind='audio_speech')
            return CompanionDecisionResult(decision=FrozenCompanionDecision.from_response(turn,
                {**companion_envelope(), 'plan': proposed, 'tasks': project(proposed),
                 'speech_request': dict(mode=scope, target_seconds=180, continuation=True)}))
    class ScopedReviewer(Reviewer):
        def review_with_messages(self, text, context, messages):
            self.contexts.append(context)
            return super().review_with_messages(text, context, messages)
    script = dict(title='小灯', spoken_text='月亮城的小雨推开窗。' * 15, continuation_summary='小雨继续旅行。')
    engine = Engine(json.dumps({**envelope(), 'speech': script}))
    reviewer = ScopedReviewer(ReviewVerdict.PASS, ReviewVerdict.PASS)
    reviewer.contexts = []
    request = ReplyRequest(content='接着讲吧。', messages=({'role': 'system', 'content': '角色'},
        {'role': 'user', 'content': '接着讲吧。'}), max_input_chars=40000)
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
        trusted_time=TrustedTime(datetime(2026, 9, 26, 6, 38, tzinfo=timezone.utc)))
    token = CURRENT.set(dict(structured=True, raw_user_text='接着讲吧。', channel='qq', speech_enabled=True,
        semantic_kinds=['text', 'audio_speech'], decision_now='2026-09-26T14:38:00+08:00',
        story_continuation=dict(kind='fiction_summary', source_id='speech:prior', text='前篇摘要小雨在旅行。',
                                spoken_text='绝不能用于检索的旧正文。' * 1000)))
    try:
        result = asyncio.run(ReplyPipeline(engine, reviewer=reviewer, rewriter=Rewriter('unused'),
            discover_runtime_ports=False, companion_decision_port=Port()).run(request, context))
    finally:
        CURRENT.reset(token)
    assert result.state.value == 'completed', result.error_code
    assert reviewer.contexts[1].output_constraints.content_scope == scope
    assert reviewer.contexts[0].output_constraints.content_scope == 'ordinary'
    generated = str(engine.requests[0].messages)
    assert '前篇摘要' in generated and 'speech:prior' in generated
    assert '绝不能用于检索的旧正文' not in generated
