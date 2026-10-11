"""Wire-envelope tolerance never bypasses review of the body actually delivered."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json

import pytest

from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import ReviewVerdict
from tests.http.test_personal_chat_decision import envelope
from tests.persona.test_reply_semantic_wiring import Engine, Reviewer


@pytest.mark.parametrize('encoding', ['object', 'escaped', 'double_encoded'])
def test_recovered_control_body_is_reviewed_once_and_sent_without_protocol(encoding):
    from runtime.personal_chat.decision import decode
    from runtime.personal_chat.events import PersonalMessage
    from runtime.personal_chat.qq import text_segments
    from runtime.personal_chat.service import PersonalChatService

    text = '当然可以，我在听你说。'
    inner = envelope(text=text, delivery='voice', initiative='open', sticker='inner-sticker')
    if encoding == 'escaped':
        inner = json.dumps(inner, ensure_ascii=False)[1:-1]
    elif encoding == 'double_encoded':
        inner = json.dumps(inner, ensure_ascii=False)
    engine, reviewer = Engine(envelope(text=inner)), Reviewer(ReviewVerdict.PASS)
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False)
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
        trusted_time=TrustedTime(datetime(2026, 10, 5, tzinfo=timezone.utc)))
    token = CURRENT.set(dict(structured=True, channel='qq', raw_user_text='陪我说会话',
                            received_source_id='synthetic', decision_now='2026-10-05T08:00:00+08:00'))
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content='陪我说会话', messages=(
            {'role':'user', 'content':'陪我说会话'},)), context))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.COMPLETED
    assert [entry[0] for entry in reviewer.seen] == [text]
    assert len(engine.requests) == result.reviewer_calls == 1 and result.rewrite_calls == 0
    assert result.reviewed_content == {'text': hashlib.sha256(text.encode()).hexdigest()}
    decoded = decode(result.text, user='陪我说会话', now=1)
    assert decoded['text'] == text and decoded['initiative'] == 'keep'

    async def delivery():
        rows, sent = [], []
        async def generate(event, row):
            return decoded['text']
        async def commit(row):
            pass
        async def send(value):
            sent.append(text_segments(value))
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.handle(PersonalMessage('qq', '100', '200', 'synthetic', '陪我说会话'), send)
        assert rows[0]['delivery_status'] == 'DELIVERED'
        assert sent == [[{'type':'text', 'data':{'text':text.replace('。', '\n').strip()}}]]
    asyncio.run(delivery())


@pytest.mark.parametrize('encoding', ['escaped', 'double_encoded'])
def test_rewrite_normalization_recovers_body_without_replacing_outer_controls(encoding):
    from runtime.personal_chat.decision import decode
    from tests.persona.test_reply_semantic_wiring import Rewriter, execute

    text = '好的，等你回来再说。'
    inner = envelope(text=text, delivery='voice', initiative='open', sticker='inner-sticker')
    inner = json.dumps(inner, ensure_ascii=False)
    if encoding == 'escaped':
        inner = inner[1:-1]
    original = envelope(text='这是需要修改的候选正文。', initiative='pause', evidence='先别主动联系')
    reviewer = Reviewer(ReviewVerdict.REWRITE, ReviewVerdict.PASS)
    rewriter = Rewriter(inner)
    result, engine = execute(original, mode=ReplyMode.FUTURE_IM, reviewer=reviewer,
                             rewriter=rewriter, raw='先别主动联系')
    assert result.state is ReplyState.COMPLETED
    assert [entry[0] for entry in reviewer.seen] == ['这是需要修改的候选正文。', text]
    assert result.reviewer_calls == 2 and result.rewrite_calls == len(rewriter.calls) == 1
    assert len(engine.requests) == 1
    decoded = decode(result.text, user='先别主动联系', now=1)
    assert decoded['text'] == text and decoded['initiative'] == 'pause'
    assert decoded['delivery'] == 'text' and decoded['sticker'] is None


@pytest.mark.parametrize('channel', ['qq', 'wechat'])
@pytest.mark.parametrize('variant', ['text_only', 'missing_control', 'brackets', 'unrequested_speech', 'bad_control'])
def test_normalized_envelope_reviews_preserved_body_once(channel, variant):
    text = '列表是 [[1, 2], [3, 4]]。' if variant == 'brackets' else '当然可以，我在听你说。'
    body = json.loads(envelope(text=text))
    if variant == 'text_only':
        body = {'text': text}
    elif variant == 'missing_control':
        body.pop('initiative')
    elif variant == 'unrequested_speech':
        body['speech'] = {'private-script': 'not requested and never reviewed or sent'}
    elif variant == 'bad_control':
        body.update(initiative='erase', followup_at='cancel', evidence=42)
    engine, reviewer = Engine(json.dumps(body)), Reviewer(ReviewVerdict.PASS)
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False)
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
        trusted_time=TrustedTime(datetime(2026, 10, 4, tzinfo=timezone.utc)))
    token = CURRENT.set(dict(structured=True, channel=channel, raw_user_text='陪我说会话',
                            decision_now='2026-10-04T08:00:00+08:00'))
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content='陪我说会话', messages=(
            {'role':'user', 'content':'陪我说会话'},)), context))
    finally:
        CURRENT.reset(token)
    assert result.state is ReplyState.COMPLETED
    assert [entry[0] for entry in reviewer.seen] == [text]
    assert len(engine.requests) == result.reviewer_calls == 1
    assert result.rewrite_calls == 0
    assert result.reviewed_content == {'text': hashlib.sha256(text.encode()).hexdigest()}
    if variant == 'unrequested_speech':
        assert 'speech' not in json.loads(result.text)
        assert result.decision_dropped_media == 'UNREQUESTED_SPEECH'
