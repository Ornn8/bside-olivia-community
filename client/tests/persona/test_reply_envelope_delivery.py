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
