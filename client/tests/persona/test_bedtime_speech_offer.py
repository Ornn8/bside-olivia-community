"""Exercise the real writer boundary; model recognition is checked separately."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from reply_orchestrator import ReplyRequest, ReplyState
from runtime.personal_chat.presentation import CURRENT
from runtime.reply.companion_decision import CompanionDecisionResult, FrozenCompanionDecision
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_companion_decision import envelope as jev_envelope, project
from tests.persona.test_companion_decision import input_args, sidecar
from tests.persona.test_jev_pipeline import plan
from tests.persona.test_reply_semantic_wiring import Engine, envelope


class SpeechPort:
    def __init__(self, intent=None, offer='bedtime'):
        self.intent, self.offer, self.turns = intent, offer, []

    async def decide(self, turn):
        self.turns.append(turn)
        proposed = plan(kind='text')
        response = {**jev_envelope(), 'plan': proposed, 'tasks': project(proposed)}
        if turn.speech_enabled:
            response['speech_request'] = self.intent
        if turn.bedtime_offer:
            response['speech_offer'] = self.offer
        return CompanionDecisionResult(decision=FrozenCompanionDecision.from_response(turn, response))


def run_turn(*, user='我洗漱完了，准备睡觉。', history=(), channel='qq', ready=True,
             proactive=False, intent=None, candidate=None, mode=ReplyMode.FUTURE_IM, budget=40000, offer='bedtime'):
    port = SpeechPort(intent, offer)
    body = candidate or envelope()
    body.update(sticker=None)
    engine = Engine(json.dumps(body, ensure_ascii=False))
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, companion_decision_port=port)
    request = ReplyRequest(content=user, request_id='synthetic-bedtime', messages=(
        {'role': 'system', 'content': '这是合成验收。你是成年音乐学生林离，今晚已结束练琴，在家休息。'},
        *history, {'role': 'user', 'content': user}), max_input_chars=budget)
    context = ReplyContext.create(mode, future_im_enabled=True,
        trusted_time=TrustedTime(datetime(2026, 10, 6, 14, tzinfo=timezone.utc)))
    token = CURRENT.set(dict(structured=True, raw_user_text=user, channel=channel,
        speech_enabled=ready, semantic_kinds=['text', 'audio_speech'], proactive=proactive,
        bedtime_offer_enabled=ready,
        received_source_id='synthetic-bedtime:user', input_revision=1,
        decision_now='2026-10-06T22:00:00+08:00'))
    try:
        result = asyncio.run(pipeline.run(request, context))
    finally:
        CURRENT.reset(token)
    return result, engine, port


def test_ready_qq_turn_offers_in_existing_writer_call_without_audio_permission():
    result, engine, port = run_turn()
    assert result.state is ReplyState.COMPLETED
    assert len(port.turns) == len(engine.requests) == 1
    messages = engine.requests[0].messages
    assert any('<bedtime_audio_offer>' in m['content'] for m in messages if m['role'] == 'system')
    assert messages[-1] == {'role': 'user', 'content': '我洗漱完了，准备睡觉。'}
    assert result.companion_decision['speech_request'] is None
    assert not json.loads(result.text).get('speech')


@pytest.mark.parametrize('options', [dict(ready=False), dict(channel='wechat'),
    dict(proactive=True), dict(mode=ReplyMode.TEXT_LETTER)])
def test_offer_is_scoped_to_ready_qq_user_turn(options):
    result, engine, _ = run_turn(**options)
    assert result.state is ReplyState.COMPLETED
    assert not any('<bedtime_audio_offer>' in m['content'] for m in engine.requests[0].messages)


def test_confirmed_speech_uses_existing_generation_path_without_another_offer():
    intent = dict(mode='story', target_seconds=180, continuation=False)
    script = dict(title='灯塔', spoken_text='海边有一座小小的灯塔，每到夜晚就亮起温柔的光。' * 4,
                  continuation_summary='小灯塔守护安静的海边。')
    body = envelope()
    body.update(text='好，我给你准备一个睡前故事。', delivery='text', speech=script)
    result, engine, port = run_turn(user='讲个故事吧', intent=intent, candidate=body)
    assert result.state is ReplyState.COMPLETED
    assert len(port.turns) == len(engine.requests) == 1
    assert any('<speech_request>' in m['content'] for m in engine.requests[0].messages)
    # The writer is told how to return the script, not only that audio was requested.
    assert any('<long_audio_task>' in m['content'] and '"spoken_text"' in m['content'] for m in engine.requests[0].messages)
    assert not any('<bedtime_audio_offer>' in m['content'] for m in engine.requests[0].messages)
    assert json.loads(result.text)['speech'] == script


def test_offer_cannot_authorize_writer_to_generate_unsolicited_audio():
    body = envelope()
    body['speech'] = dict(title='未请求音频', spoken_text='这是未请求的长音频。' * 8, continuation_summary='')
    result, _, _ = run_turn(candidate=body)
    assert result.state is ReplyState.COMPLETED
    assert not json.loads(result.text).get('speech')


def test_offer_respects_final_writer_input_budget():
    _, engine, _ = run_turn()
    budget = sum(len(m['content']) for m in engine.requests[0].messages) - 1
    result, engine, port = run_turn(budget=budget)
    assert result.state is ReplyState.COMPLETED
    assert len(engine.requests) == len(port.turns) == 1
    messages = engine.requests[0].messages
    assert sum(len(m['content']) for m in messages) <= budget
    assert '<bedtime_audio_offer>' in str(messages)
    result, engine, _ = run_turn(budget=100)
    assert result.state is ReplyState.FAILED and result.error_code == 'INPUT_TOO_LONG'
    assert not engine.requests


@pytest.mark.parametrize('offer', ['none', 'bedtime', 'clarify'])
def test_writer_uses_exact_frozen_offer_decision(offer):
    result, engine, port = run_turn(offer=offer)
    assert result.companion_decision['speech_offer'] == offer
    assert len(port.turns) == len(engine.requests) == 1
    assert any('决定是' + offer in m['content'] for m in engine.requests[0].messages)


def test_native_offer_is_one_http_call_and_survives_frozen_replay(sidecar):
    from runtime.reply.companion_decision import FrozenCompanionTurn, JevDecisionPort
    turn = FrozenCompanionTurn.create(**input_args(), speech_enabled=True, bedtime_offer=True)
    sidecar['body'] = {**jev_envelope(), 'speech_request': None, 'speech_offer': 'bedtime'}
    result = asyncio.run(JevDecisionPort(sidecar['url']).decide(turn))
    record = result.decision.record()
    assert record['speech_offer'] == 'bedtime'
    assert FrozenCompanionDecision.from_record(turn, record).record() == record
    assert len(sidecar['calls']) == 1
    assert json.loads(sidecar['calls'][0][2])['bedtime_offer'] is True


@pytest.mark.parametrize('value', ['true', 1, None])
def test_offer_scope_rejects_non_boolean_input(value):
    from runtime.reply.companion_decision import CompanionDecisionError, FrozenCompanionTurn
    with pytest.raises(CompanionDecisionError):
        FrozenCompanionTurn.create(**input_args(), speech_enabled=True, bedtime_offer=value)


def test_old_paid_decision_is_not_reclassified_when_offer_is_enabled():
    from runtime.reply.companion_runtime import prepare_decision
    port = SpeechPort()
    args = dict(source_id='synthetic-cache', input_revision=2, as_of='2026-10-06T22:00:00+08:00',
                kinds=['text'], speech_enabled=True)
    old = asyncio.run(prepare_decision(port, [], '我要睡了', **args))
    legacy = old.record()
    legacy.pop('input', None)
    replay = asyncio.run(prepare_decision(port, [], '我要睡了', **args, cached=legacy, bedtime_offer=True))
    assert replay.record() == legacy and len(port.turns) == 1
    assert 'speech_offer' not in replay.record()


@pytest.mark.parametrize('value', [None, True, 1, 'yes', [], {}])
def test_invalid_offer_cannot_become_a_frozen_decision(value):
    from runtime.reply.companion_decision import CompanionDecisionError, FrozenCompanionTurn
    turn = FrozenCompanionTurn.create(**input_args(), speech_enabled=True, bedtime_offer=True)
    response = {**jev_envelope(), 'speech_request': None, 'speech_offer': value}
    with pytest.raises(CompanionDecisionError, match='JEV_RESPONSE_INVALID'):
        FrozenCompanionDecision.from_response(turn, response)


def test_missing_offer_from_old_server_fails_without_guessing():
    from runtime.reply.companion_decision import CompanionDecisionError, FrozenCompanionTurn
    turn = FrozenCompanionTurn.create(**input_args(), speech_enabled=True, bedtime_offer=True)
    with pytest.raises(CompanionDecisionError, match='JEV_RESPONSE_INVALID'):
        FrozenCompanionDecision.from_response(turn, {**jev_envelope(), 'speech_request': None})


def test_offer_requires_speech_capability():
    from runtime.reply.companion_decision import CompanionDecisionError, FrozenCompanionTurn
    with pytest.raises(CompanionDecisionError, match='JEV_INPUT_INVALID'):
        FrozenCompanionTurn.create(**input_args(), bedtime_offer=True)


def test_invitation_and_decline_survive_covered_history_and_replay():
    from runtime.personal_chat.context import READ_WINDOW
    from runtime.reply.companion_runtime import prepare_decision

    texts = [('user', '我要睡了。'), ('assistant', '想听故事还是ASMR？'),
             ('user', '不用啦，今天安静睡。'), ('assistant', '好，晚安。')]
    messages = []
    for i, (role, text) in enumerate(texts):
        source = 'reply:' + str(i // 2)
        actor = 'user' if role == 'user' else 'linli'
        meta = dict(source=source, event_id=source + ':' + actor, actor=actor,
                    evidence_kind='statement_only', truncated=False)
        messages.append(dict(role=role, content='[历史消息 ' + json.dumps(meta) + ']\n' + text))
    messages.append(dict(role='user', content='嗯，我睡了。'))
    args = dict(source_id='synthetic-decline', input_revision=2, as_of='2026-10-06T22:00:00+08:00',
                kinds=['text'], speech_enabled=True, bedtime_offer=True)
    window = READ_WINDOW.set([dict(letter_id='0'), dict(letter_id='1', companion_decision=dict(
        plan=dict(understanding=dict(requirements=[])), source_id_map={}))])
    port = SpeechPort(offer='none')
    try:
        frozen = asyncio.run(prepare_decision(port, messages, '嗯，我睡了。', **args))
        replay = asyncio.run(prepare_decision(port, messages, '嗯，我睡了。', cached=frozen.record(), **args))
    finally:
        READ_WINDOW.reset(window)
    assert [m['text'] for m in port.turns[0].input['messages']] == [t for _, t in texts] + ['嗯，我睡了。']
    assert replay.record() == frozen.record() and len(port.turns) == 1
