"""Current-user silence is authored and validated, never inferred from timing alone."""
import asyncio
import json

import pytest

from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_orchestrator import ReplyRequest, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from tests.http.test_personal_chat_decision import envelope
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_qq_recovery_pipeline import Engine, NOW
from tests.persona.test_stage_recovery import FactReviewer


class Authority:
    def __init__(self, choice='authorized'):
        self.choice, self.calls = choice, 0

    async def ask(self, state, questions, *, purpose):
        self.calls += 1
        assert state['current_user_text']
        return {'user_silence': self.choice}


def run(body, *, timing, user, authority=None, last_rejection=None):
    engine, reviewer = Engine([body]), FactReviewer()
    engine.requests = []
    original_run = engine.run
    async def capture(request):
        engine.requests.append(request)
        return await original_run(request)
    engine.run = capture
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=Port(plan(timing=timing)),
        silence_authorization_port=authority or Authority())
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True, trusted_time=TrustedTime(NOW))
    metadata = dict(structured=True, proactive=False, channel='qq', raw_user_text=user,
        semantic_kinds=['text'], received_source_id='synthetic-silence', input_revision=0,
        decision_now=NOW.isoformat(), last_decision_rejection_reason=last_rejection)
    token = CURRENT.set(metadata)
    try:
        result = asyncio.run(pipeline.run(ReplyRequest(content=user, request_id='synthetic-silence',
            messages=({'role': 'user', 'content': user},)), context))
        return result, engine, reviewer
    finally:
        CURRENT.reset(token)


@pytest.mark.parametrize('timing', ['wait_user', 'no_reply', 'defer'])
def test_complete_question_reaches_writer_and_quality_gate(timing):
    authority = Authority()
    result, engine, reviewer = run(envelope(text='Here is the explanation.'),
        timing=timing, user='Please explain this result.', authority=authority)
    assert result.state is ReplyState.COMPLETED
    assert result.companion_timing == 'now' and result.companion_delivery == 'text'
    assert engine.calls == reviewer.calls == 1
    assert authority.calls == 0  # The additional permission call is only for silence.
    assert result.companion_decision['plan']['proposal']['timing'] == timing


@pytest.mark.parametrize('timing', ['now', 'wait_user', 'defer', 'no_reply'])
@pytest.mark.parametrize('kind', ['wait_user', 'no_reply'])
def test_explicit_user_silence_has_no_outgoing_body_or_review(timing, kind):
    user = 'Wait until I finish.' if kind == 'wait_user' else 'Please do not answer this message.'
    body = json.loads(envelope(text='', skip=True))
    body['silence'] = dict(kind=kind, evidence=user)
    result, engine, reviewer = run(json.dumps(body), timing=timing, user=user)
    assert result.state is ReplyState.COMPLETED and engine.calls == 1
    assert reviewer.calls == 0
    assert result.companion_timing == kind and result.companion_delivery is None


@pytest.mark.parametrize('silence', [None, {'kind': 'defer', 'evidence': 'Please explain this result.'},
    {'kind': 'wait_user', 'evidence': 'from another message'}])
def test_silence_without_current_authorization_fails_explicitly(silence):
    body = json.loads(envelope(text='', skip=True))
    if silence is not None:
        body['silence'] = silence
    result, engine, reviewer = run(json.dumps(body), timing='no_reply', user='Please explain this result.')
    assert result.state is ReplyState.FAILED and result.error_code == 'PERSONAL_CHAT_DECISION_INVALID'
    assert engine.calls == 1 and reviewer.calls == 0


@pytest.mark.parametrize('user,quote', [
    ('Please explain this result.', 'Please explain'),
    ('She said "do not answer". What does she mean?', 'do not answer'),
    ('I said wait earlier; now answer my question.', 'wait'),
    ('Do not wait; please reply.', 'wait'),
])
def test_current_quote_alone_cannot_authorize_silence(user, quote):
    body = json.loads(envelope(text='', skip=True))
    body['silence'] = dict(kind='wait_user', evidence=quote)
    authority = Authority('reply_required')
    result, engine, reviewer = run(json.dumps(body), timing='wait_user', user=user, authority=authority)
    assert result.state is ReplyState.FAILED and result.decision_rejection_reason == 'SILENCE_NOT_AUTHORIZED'
    assert engine.calls == authority.calls == 1 and reviewer.calls == 0


def test_rejected_silence_feedback_reaches_next_author_attempt():
    authority = Authority()
    result, engine, reviewer = run(envelope(text='Here is the explanation.'),
        timing='wait_user', user='Please explain this result.', authority=authority,
        last_rejection='SILENCE_NOT_AUTHORIZED')
    assert result.state is ReplyState.COMPLETED
    assert authority.calls == 0 and engine.calls == reviewer.calls == 1
    assert '上一候选静默已被独立语义检查拒绝' in str(engine.requests[0].messages)
