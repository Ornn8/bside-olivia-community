import asyncio
import json
from datetime import datetime, timezone

import pytest

from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime, IntimacyRequest
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer, ReviewResult, ReviewStatus, ReviewVerdict, ReviewerScores, ReviewerViolation


class Engine:
    def __init__(self, text):
        self.text, self.requests = text, []

    async def run(self, request):
        self.requests.append(request)
        return ReplyResult('test', ReplyState.COMPLETED, text=self.text)


class Reviewer:
    def __init__(self, *verdicts):
        self.verdicts, self.seen = list(verdicts), []

    def review_with_messages(self, text, context, messages):
        self.seen.append((text, messages))
        verdict = self.verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return ReviewResult(ReviewStatus.COMPLETED, verdict,
            () if verdict is ReviewVerdict.PASS else (ReviewerViolation('MEMORY_FABRICATION', 'hard', 0, len(text)),),
            ReviewerScores(100, 100, 100, 100), IntimacyRequest.NONE, ())


class Rewriter:
    def __init__(self, text):
        self.text, self.calls = text, []

    def rewrite(self, candidate, context, codes):
        self.calls.append(candidate)
        return self.text


class Interpreter:
    def __init__(self, fail=False):
        self.seen, self.fail = [], fail

    async def interpret(self, text):
        self.seen.append(text)
        if self.fail:
            raise RuntimeError('private diagnostic')
        return {'acts': [{'quote': text, 'kind': 'self_statement', 'meaning': '用户已醒来'}]}


def envelope(**changes):
    return dict(text='醒啦，休息得怎么样？', delivery='voice', listening='keep', initiative='keep',
        pause_until=None, letter='keep', letter_until=None, followup_at=None, evidence='',
        sticker='test-sticker', skip=False, **changes)


def execute(text, *, mode=ReplyMode.TEXT_LETTER, reviewer=None, rewriter=None, interpreter=None,
            proactive=False, raw='我醒啦', companion_decision_port=None):
    engine = Engine(text)
    pipeline = ReplyPipeline(engine, reviewer=reviewer or NullReviewer(), rewriter=rewriter or UnavailableRewriter(),
        discover_runtime_ports=False, current_turn_interpreter=interpreter,
        companion_decision_port=companion_decision_port)
    request = ReplyRequest(content=raw, messages=({'role':'system','content':'人物参考'}, {'role':'user','content':raw}), max_input_chars=40000)
    context = ReplyContext.create(mode, trusted_time=TrustedTime(datetime(2026,9,26,6,38,tzinfo=timezone.utc)), future_im_enabled=True)
    token = CURRENT.set({'structured': True, 'raw_user_text':raw, 'channel':'qq',
        'speech_enabled': companion_decision_port is not None,
        'semantic_kinds': ['text', 'audio_speech'] if companion_decision_port is not None else ['text'],
        'decision_now':'2026-09-26T14:38:00+08:00', 'proactive':proactive}) if mode is ReplyMode.FUTURE_IM else None
    try:
        return asyncio.run(pipeline.run(request, context)), engine
    finally:
        if token is not None:
            CURRENT.reset(token)


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
def test_explicit_interpreter_runs_for_user_turns(mode):
    interpreter = Interpreter()
    candidate = json.dumps(envelope(), ensure_ascii=False) if mode is ReplyMode.FUTURE_IM else '醒啦，休息得怎么样？'
    result, engine = execute(candidate, mode=mode, interpreter=interpreter)
    assert result.state is ReplyState.COMPLETED
    assert interpreter.seen == ['我醒啦']
    assert 'current_turn_interpretation' in engine.requests[0].messages[0]['content']


def test_proactive_check_is_not_user_speech():
    interpreter = Interpreter()
    result, _ = execute(json.dumps(envelope()), mode=ReplyMode.FUTURE_IM, interpreter=interpreter, proactive=True)
    assert result.state is ReplyState.COMPLETED
    assert interpreter.seen == []


def test_interpreter_failure_prevents_generation():
    result, engine = execute('reply', mode=ReplyMode.FUTURE_IM, interpreter=Interpreter(fail=True))
    assert result.error_code == 'CURRENT_TURN_INTERPRETATION_FAILED'
    assert not engine.requests


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
def test_explicit_reviewer_checks_decoded_text_and_current_user(mode):
    reviewer = Reviewer(ReviewVerdict.PASS)
    body = '醒啦，休息得怎么样？'
    result, _ = execute(json.dumps(envelope(), ensure_ascii=False) if mode is ReplyMode.FUTURE_IM else body, mode=mode, reviewer=reviewer)
    assert result.state is ReplyState.COMPLETED and result.reviewer_calls == 1
    assert reviewer.seen[0][0] == body
    assert reviewer.seen[0][1][-1]['content'] == '我醒啦'
    assert all('字段必须完整' not in m['content'] for m in reviewer.seen[0][1])


@pytest.mark.parametrize('wrapped', [False, True])
def test_qq_rewrite_changes_only_text_and_preserves_wire_dates(wrapped):
    original = envelope()
    original.update(followup_at='2026-09-26T15:00:00+08:00', evidence='三点联系我')
    reviewer, rewriter = Reviewer(ReviewVerdict.REWRITE, ReviewVerdict.PASS), Rewriter('好，三点再聊。')
    result, _ = execute(json.dumps([original] if wrapped else original), mode=ReplyMode.FUTURE_IM, reviewer=reviewer, rewriter=rewriter, raw='三点联系我')
    assert result.state is ReplyState.COMPLETED
    assert json.loads(result.text) == {**original, 'text':'好，三点再聊。'}
    assert result.reviewer_calls == 2 and result.rewrite_calls == 1
    assert rewriter.calls == [original['text']]
    plan = next(m['content'] for m in reviewer.seen[0][1] if '<reply_delivery_plan>' in m['content'])
    assert 'planned_delivery' in plan and '2026-09-26T15:00:00+08:00' in plan


@pytest.mark.parametrize('verdict', [ReviewVerdict.BLOCK, RuntimeError('private diagnostic')])
def test_review_failure_never_publishes_candidate(verdict):
    result, _ = execute('旧回复', reviewer=Reviewer(verdict))
    assert result.state is ReplyState.FAILED and not result.text
    assert 'private diagnostic' not in str(result)


def test_invalid_qq_json_rejected_before_review():
    reviewer = Reviewer(ReviewVerdict.PASS)
    result, _ = execute('{}', mode=ReplyMode.FUTURE_IM, reviewer=reviewer)
    assert result.error_code == 'PERSONAL_CHAT_DECISION_INVALID'
    assert not reviewer.seen
    assert result.decision_rejection_reason == 'FIELDS'


def test_default_disabled_keeps_existing_behavior():
    result, _ = execute('旧协议文本', mode=ReplyMode.FUTURE_IM)
    assert result.state is ReplyState.COMPLETED and result.quality_status == 'not_checked'
    assert result.reviewer_calls == result.rewrite_calls == 0


@pytest.mark.parametrize('runtime_source', [False, True])
@pytest.mark.parametrize('with_clock', [False, True])
def test_null_reviewer_cannot_authorize_writer_silence_through_legacy_metadata(runtime_source, with_clock):
    class Authority:
        calls = 0

        async def ask(self, state, questions, *, purpose):
            self.calls += 1
            return {'user_silence': 'reply_required'}

    user = 'Please explain this result.'
    body = envelope()
    body.update(text='', skip=True, silence=dict(kind='wait_user', evidence=user))
    engine, authority = Engine(json.dumps(body)), Authority()
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, silence_authorization_port=authority)
    metadata = dict(structured=True, raw_user_text=user, proactive=False)
    if runtime_source:
        metadata['received_source_id'] = 'synthetic-runtime:user'
    if with_clock:
        metadata['decision_now'] = '2026-09-26T14:38:00+08:00'
    token = CURRENT.set(metadata)
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content=user), ReplyContext.create(
            ReplyMode.FUTURE_IM, future_im_enabled=True,
            trusted_time=TrustedTime(datetime(2026, 9, 26, tzinfo=timezone.utc)))))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.FAILED and result.error_code == 'PERSONAL_CHAT_DECISION_INVALID'
    assert not result.silence_authorized and not result.text
    assert len(engine.requests) == 1 and authority.calls == int(with_clock)
    if with_clock:
        assert result.decision_rejection_reason == 'SILENCE_NOT_AUTHORIZED'


def test_null_reviewer_runtime_contract_does_not_accept_legacy_plaintext():
    user, engine = 'Please explain this result.', Engine('legacy plaintext')
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                            discover_runtime_ports=False)
    token = CURRENT.set(dict(structured=True, raw_user_text=user, proactive=False,
                            received_source_id='synthetic-runtime:user',
                            decision_now='2026-09-26T14:38:00+08:00'))
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content=user), ReplyContext.create(
            ReplyMode.FUTURE_IM, future_im_enabled=True,
            trusted_time=TrustedTime(datetime(2026, 9, 26, tzinfo=timezone.utc)))))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.FAILED and result.error_code == 'PERSONAL_CHAT_DECISION_INVALID'
    assert not result.text and not result.silence_authorized


@pytest.mark.parametrize('timing', ['wait_user', 'defer', 'no_reply'])
@pytest.mark.parametrize('candidate_kind', ['plaintext', 'unplanned_speech'])
def test_reconsidered_silence_requires_envelope_with_partial_runtime_metadata(timing, candidate_kind):
    from tests.persona.test_jev_pipeline import Port, plan

    user = 'Please explain this result.'
    body = 'legacy plaintext'
    if candidate_kind == 'unplanned_speech':
        value = envelope()
        value['speech'] = dict(title='Synthetic speech',
            spoken_text='Synthetic speech without a media plan. ' * 3,
            continuation_summary='Synthetic summary')
        body = json.dumps(value)
    engine, port = Engine(body), Port(plan(timing=timing))
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                            discover_runtime_ports=False, companion_decision_port=port)
    token = CURRENT.set(dict(structured=True, raw_user_text=user, proactive=False,
                            decision_now='2026-09-26T14:38:00+08:00'))
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content=user, request_id='partial-runtime'),
            ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
                trusted_time=TrustedTime(datetime(2026, 9, 26, tzinfo=timezone.utc)))))
    finally:
        CURRENT.reset(token)
    assert len(port.turns) == len(engine.requests) == 1
    if candidate_kind == 'plaintext':
        assert result.state is ReplyState.FAILED and result.error_code == 'PERSONAL_CHAT_DECISION_INVALID'
        assert not result.text and not result.silence_authorized
        assert result.decision_rejection_reason == 'JSON_SYNTAX'
    else:
        assert result.state is ReplyState.COMPLETED and result.error_code is None
        assert json.loads(result.text) == {
            key: item for key, item in value.items() if key != 'speech'}
        assert 'speech' not in json.loads(result.text)
        assert not result.silence_authorized


def test_qq_rewrite_control_markup_rejected():
    result, _ = execute(json.dumps(envelope()), mode=ReplyMode.FUTURE_IM,
        reviewer=Reviewer(ReviewVerdict.REWRITE, ReviewVerdict.PASS), rewriter=Rewriter('[[chat:voice]]'))
    assert result.state is ReplyState.FAILED and not result.text


def test_still_rejected_rewrite_is_not_published_or_retried():
    reviewer = Reviewer(ReviewVerdict.REWRITE, ReviewVerdict.BLOCK)
    rewriter = Rewriter('另一个无依据说法')
    result, _ = execute('无依据说法', reviewer=reviewer, rewriter=rewriter)
    assert result.state is ReplyState.FAILED and not result.text
    assert result.reviewer_calls == 2 and result.rewrite_calls == 1
    assert len(rewriter.calls) == 1


def test_proactive_skip_has_no_text_to_review():
    payload = envelope()
    payload.update(text='', skip=True)
    reviewer = Reviewer(ReviewVerdict.PASS)
    result, _ = execute(json.dumps(payload), mode=ReplyMode.FUTURE_IM, reviewer=reviewer, proactive=True)
    assert result.state is ReplyState.COMPLETED and not reviewer.seen


def test_im_interpreter_and_reviewer_get_raw_user_not_image_observation():
    interpreter, reviewer = Interpreter(), Reviewer(ReviewVerdict.PASS)
    engine = Engine(json.dumps(envelope()))
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
        current_turn_interpreter=interpreter, discover_runtime_ports=False)
    user = '我醒啦'
    observation = '系统图片观察：窗边有人'
    content = user + observation
    request = ReplyRequest(content=content, messages=({'role':'system','content':'人物参考'},
        {'role':'user','content':content}), max_input_chars=40000)
    context = ReplyContext.create(ReplyMode.FUTURE_IM,
        trusted_time=TrustedTime(datetime(2026,9,26,6,38,tzinfo=timezone.utc)), future_im_enabled=True)
    token = CURRENT.set({'structured':True, 'raw_user_text':user, 'incoming_observation_context':observation,
        'decision_now':'2026-09-26T14:38:00+08:00', 'proactive':False})
    try:
        result = asyncio.run(pipeline.run(request, context))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.COMPLETED
    assert interpreter.seen == [user]
    assert reviewer.seen[0][1][-1]['content'] == user
    assert any(observation in m['content'] for m in reviewer.seen[0][1] if m['role'] == 'system')
