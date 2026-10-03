"""Injected finite-answer protocol tests; these do not measure model accuracy."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from runtime.personal_chat.decision import decode
from runtime.reply import companion_runtime, jev_questions
from runtime.reply.companion_decision import FrozenCompanionTurn
from tests.http.test_personal_chat_decision import envelope
from tests.persona.test_jev_pipeline import plan, Port


class Questions:
    def __init__(self, answer=None, *, error=None):
        self.answer = {'user_silence': 'authorized'} if answer is None else answer
        self.error, self.calls = error, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append(deepcopy(dict(state=state, questions=questions, purpose=purpose)))
        if self.error is not None:
            raise self.error
        return self.answer


@pytest.mark.parametrize('kind,user', [
    ('wait_user', '我还没说完，先等我继续'),
    ('no_reply', '这条只是记录，不用回复我'),
])
def test_exact_authorized_answer_allows_validated_current_user_silence(kind, user):
    silence = dict(kind=kind, evidence=user)
    decision = decode(envelope(text='', skip=True, silence=silence), user=user, now=1,
                      allow_user_silence=True)
    port = Questions()
    allowed = asyncio.run(companion_runtime.authorize_user_silence(user, decision['silence'], port=port))
    assert allowed is True and len(port.calls) == 1
    packet = port.calls[0]
    assert packet['state'] == dict(current_user_text=user, proposed_silence=silence)
    assert packet['purpose'] == 'personal_chat_user_silence'
    assert set(packet['questions']) == {'user_silence'}
    assert set(packet['questions']['user_silence']) == {'instructions', 'criteria'}
    assert set(packet['questions']['user_silence']['criteria']) == {'authorized', 'reply_required'}


@pytest.mark.parametrize('user,kind,quote', [
    ('朋友说“先等我说完”，你怎么看？', 'wait_user', '先等我说完'),
    ('先等我说完——算了，我说完了，现在回答吧', 'wait_user', '先等我说完'),
    ('不是让你不用回复，请告诉我你的想法', 'no_reply', '不用回复'),
    ('先等我说完这句话：请解释今天的课表', 'wait_user', '先等我说完'),
])
def test_independent_reply_required_answer_rejects_literal_quote_without_silence_authority(user, kind, quote):
    # Provenance validation alone accepts each quote. The independent semantic
    # answer must still be consumed; the answer is a double, not an LLM result.
    decision = decode(envelope(text='', skip=True, silence=dict(kind=kind, evidence=quote)),
                      user=user, now=1, allow_user_silence=True)
    port = Questions({'user_silence': 'reply_required'})
    allowed = asyncio.run(companion_runtime.authorize_user_silence(user, decision['silence'], port=port))
    assert allowed is False and len(port.calls) == 1
    assert port.calls[0]['state']['current_user_text'] == user
    assert port.calls[0]['state']['proposed_silence'] == dict(kind=kind, evidence=quote)


@pytest.mark.parametrize('answer', [
    {}, {'user_silence': True}, {'user_silence': 'yes'},
    {'user_silence': {'choice': 'authorized'}},
    {'user_silence': 'authorized', 'other': 'authorized'},
    ['authorized'], 'authorized',
])
def test_arbitrary_or_malformed_semantic_answers_never_authorize_silence(answer):
    with pytest.raises(companion_runtime.CompanionRuntimeError, match='^JEV_RESPONSE_INVALID$'):
        asyncio.run(companion_runtime.authorize_user_silence('等我说完',
            dict(kind='wait_user', evidence='等我说完'), port=Questions(answer)))


def test_configured_questions_are_used_when_no_port_is_injected(monkeypatch):
    port = Questions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    assert asyncio.run(companion_runtime.authorize_user_silence('不用回复',
        dict(kind='no_reply', evidence='不用回复'))) is True
    assert len(port.calls) == 1


def test_unconfigured_semantic_port_fails_closed(monkeypatch):
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)
    with pytest.raises(companion_runtime.CompanionRuntimeError, match='^JEV_UNAVAILABLE$'):
        asyncio.run(companion_runtime.authorize_user_silence('不用回复',
            dict(kind='no_reply', evidence='不用回复')))


@pytest.mark.parametrize('error,code', [
    (ValueError('JEV_TIMEOUT'), 'JEV_TIMEOUT'),
    (ValueError('JEV_RESPONSE_INVALID'), 'JEV_RESPONSE_INVALID'),
    (RuntimeError('private upstream diagnostic'), 'JEV_UNAVAILABLE'),
])
def test_semantic_failures_keep_only_finite_error_codes(error, code):
    with pytest.raises(companion_runtime.CompanionRuntimeError, match='^' + code + '$'):
        asyncio.run(companion_runtime.authorize_user_silence('不用回复',
            dict(kind='no_reply', evidence='不用回复'), port=Questions(error=error)))


def test_cancelled_semantic_call_is_not_an_authorization():
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(companion_runtime.authorize_user_silence('不用回复',
            dict(kind='no_reply', evidence='不用回复'), port=Questions(error=asyncio.CancelledError())))


@pytest.mark.parametrize('silence', [
    None, dict(kind='defer', evidence='不用回复'),
    dict(kind='no_reply', evidence='历史原话'),
    dict(kind='no_reply', evidence='不用回复', extra=True),
])
def test_gate_rejects_invalid_input_before_semantic_request(silence):
    port = Questions()
    with pytest.raises(companion_runtime.CompanionRuntimeError, match='^JEV_INPUT_INVALID$'):
        asyncio.run(companion_runtime.authorize_user_silence('不用回复', silence, port=port))
    assert not port.calls


def frozen_decision(value):
    turn = FrozenCompanionTurn.create(messages=[dict(source_id='synthetic-current-user',
        role='user', text='稍后给我照片，现在先回答问题')], current_source_id='synthetic-current-user',
        capabilities=dict(kinds=['text'], synchronize=False, playback_events=False,
            compose_audio=False, compose_video=False, split_spoken_content=False),
        environment=dict(can_read=None, can_view=None, can_listen=None), forbidden_kinds=[],
        as_of=datetime(2026, 10, 3, tzinfo=timezone.utc), input_revision=0)
    return asyncio.run(Port(value).decide(turn)).decision


@pytest.mark.parametrize('delivery', ['text', 'voice_default'])
def test_silent_proposal_fallback_keeps_pending_media_finite_and_unfulfilled(delivery):
    value = plan(timing='defer')
    value['understanding']['requirements'] = [dict(id='later_photo', fulfillment='pending',
        alternatives=[dict(kinds=['image'], min_assets=1, max_assets=1)], evidence_turn_ids=['t1'])]
    original = deepcopy(value)
    decision = frozen_decision(value)
    messages = companion_runtime.project_decision([dict(role='user', content='现在先回答问题')],
        decision, max_input_chars=40000, delivery=delivery)
    note = next(message['content'] for message in messages if '<companion_decision>' in message['content'])
    payload = json.loads(note.split('<companion_decision>\n')[1].split('\n</companion_decision>')[0])
    assert payload['timing'] == 'defer'
    assert payload['pending_requirements'] == [dict(fulfillment='pending', kinds=['image'])]
    assert 'synthetic-current-user' not in note and 'later_photo' not in note
    assert decision.plan == original
    assert '本轮仍需回应' in note and '尚未完成' in note
    assert 'followup_at' in note


def test_normal_media_projection_does_not_gain_silent_fallback_metadata():
    decision = frozen_decision(plan())
    messages = companion_runtime.project_decision([dict(role='user', content='现在先回答问题')],
        decision, max_input_chars=40000, delivery='text')
    assert 'pending_requirements' not in json.dumps(messages, ensure_ascii=False)
