"""QQ is instant chat: one Jev question per reply decides whether this reply is short or long."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from reply_orchestrator import ReplyRequest, ReplyState
from runtime.personal_chat.context import freeze_read_window, recent_messages
from runtime.personal_chat.decision import INSTRUCTION
from runtime.personal_chat.presentation import CURRENT
from runtime.reply import reply_length
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_bedtime_speech_offer import SpeechPort
from tests.persona.test_reply_semantic_wiring import Engine, envelope

NOW = datetime(2026, 10, 12, 14, tzinfo=timezone.utc)
SHORT_BLOCK = '<reply_length>short</reply_length>'
LONG_BLOCK = '<reply_length>long</reply_length>'


def body(**changes):
    return json.dumps({**envelope(), 'sticker': None, **changes}, ensure_ascii=False)


HISTORY = [{'role': 'user' if i % 2 == 0 else 'her', 'text': f'第{i}句'} for i in range(12)]


class LengthPort:
    def __init__(self, answer='short', error=None, gate=None):
        self.answer, self.error, self.gate, self.calls = answer, error, gate, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        if self.gate is not None:
            await self.gate()
        if self.error is not None:
            raise self.error
        return self.answer if isinstance(self.answer, dict) else {'length': self.answer}


def run_turn(port, *, user='今天好累呀。', raw=None, channel='qq', proactive=False, mode=ReplyMode.FUTURE_IM,
             companion=None, recent=HISTORY, engine=None, extra=None, **options):
    engine = engine or Engine(body())
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, companion_decision_port=companion,
                             reply_length_port=port, **options)
    request = ReplyRequest(content=user, request_id='synthetic-length', messages=(
        {'role': 'system', 'content': '这是合成验收。你是成年音乐学生，今晚已结束练琴，在家休息。'},
        {'role': 'user', 'content': user}), max_input_chars=40000)
    context = ReplyContext.create(mode, future_im_enabled=True, trusted_time=TrustedTime(NOW))
    metadata = dict(structured=True, raw_user_text=user if raw is None else raw, channel=channel, proactive=proactive,
        recent_messages=recent, semantic_kinds=['text', 'audio_speech'], speech_enabled=True,
        received_source_id='synthetic-length:user', input_revision=1, decision_now='2026-10-12T22:00:00+08:00')
    token = CURRENT.set({**metadata, **(extra or {})})
    try:
        result = asyncio.run(pipeline.run(request, context))
    finally:
        CURRENT.reset(token)
    return result, engine


def length_messages(engine):
    return [m for m in engine.requests[0].normalized_messages()
            if m['role'] == 'system' and '<reply_length>' in m['content']]


@pytest.mark.parametrize('answer', ['short', 'long'])
def test_writer_gets_the_block_and_rule_of_the_judged_length(answer):
    port = LengthPort(answer)
    result, engine = run_turn(port)
    assert result.state is ReplyState.COMPLETED and result.reply_length == answer
    messages = engine.requests[0].normalized_messages()
    (note,) = length_messages(engine)
    assert note['content'] == f'<reply_length>{answer}</reply_length>\n' + reply_length.RULES[answer]
    other = LONG_BLOCK if answer == 'short' else SHORT_BLOCK
    assert not any(other in m['content'] for m in messages)
    # Per-turn state sits next to the current input, which stays last.
    assert messages.index(note) == len(messages) - 2 and messages[-1]['role'] == 'user'
    assert 'reply_length' in result.stage_timing_seconds
    assert INSTRUCTION in ''.join(m['content'] for m in messages)


def test_rules_are_the_owner_worded_lines():
    assert reply_length.RULES['short'] == ('这一条只说一两句话，最多三句，一般几十个字；想说的多就挑最想说的那一句，'
                                          '其余留到下一条再说；不写成多段，不逐条回应每个细节。')
    assert reply_length.RULES['long'] == ('这一条可以多说几句把事情说清楚或陪对方多聊一会儿，'
                                         '但仍是聊天口吻，不写成书信、列表或报告，一般不超过两百字。')


def test_judgment_gets_the_recent_messages_and_the_current_text_and_nothing_else():
    port = LengthPort('short')
    run_turn(port, user='帮我看看这个。', recent=HISTORY[-10:])
    (state, questions, purpose), = port.calls
    assert purpose == 'reply-length'
    assert state == {'recent_messages': HISTORY[-10:], 'current': '帮我看看这个。'}
    assert list(questions) == ['length']
    criteria = questions['length']['criteria']
    assert list(criteria) == ['short', 'long']
    assert all(set(value) == {'what', 'not_for', 'examples'} for value in criteria.values())
    assert 'recent_messages' in questions['length']['instructions'] and 'current' in questions['length']['instructions']


def test_oversized_current_text_is_trimmed_in_the_middle():
    port = LengthPort('short')
    run_turn(port, user='开头' + '很长' * 2000 + '结尾')
    sent = port.calls[0][0]['current']
    assert len(sent) < 1600 and sent.startswith('开头') and sent.endswith('结尾')


def test_recent_messages_are_the_last_ten_of_this_conversation_both_sides_in_order():
    def row(index, text, reply, **changes):
        return {**dict(letter_id=f'im-{index}', channel='qq', binding_id='b1', created_at=index,
                       delivery_status='DELIVERED', content=text, reply_text=reply), **changes}
    rows = [row(i, f'用户{i}', f'她{i}') for i in range(7)]
    rows.insert(2, dict(row(20, '别的会话', '别的回复'), binding_id='b2'))
    rows.insert(3, dict(row(21, '另一个渠道', '另一个回复'), channel='wechat'))
    rows.append(dict(letter_id='letter-1', content='一封信', reply_text='回信', letter_status='COMPLETED', created_at=8))
    rows.append(row(9, '还没回复的消息', '草稿', delivery_status='RECEIVED'))
    rows.append(row(10, '', '她主动说的话', origin='proactive'))
    rows.append(row(30, '当前这一条', ''))
    window = freeze_read_window(rows, channel='qq', binding_id='b1', current_id='im-30')
    messages = recent_messages(window, channel='qq', binding_id='b1')
    assert len(messages) == 10
    assert messages[-1] == {'role': 'her', 'text': '她主动说的话'}
    assert messages[-2] == {'role': 'user', 'text': '还没回复的消息'}
    assert messages[:8] == [{'role': role, 'text': f'{who}{i}'} for i in range(3, 7)
                            for role, who in (('user', '用户'), ('her', '她'))]
    assert not any(m['text'] in {'别的会话', '另一个渠道', '一封信', '当前这一条', '草稿'} for m in messages)
    assert recent_messages(window, channel='qq', binding_id='b1', limit=3)[-1] == messages[-1]


def test_long_stored_messages_keep_both_ends():
    window = [dict(letter_id='x', channel='qq', binding_id='b', content='头' + '中' * 3000 + '尾', reply_text='嗯')]
    first, _ = recent_messages(window, channel='qq', binding_id='b')
    assert len(first['text']) < 600 and first['text'].startswith('头') and first['text'].endswith('尾')


def test_story_and_asmr_turns_skip_the_judgment_and_keep_their_own_rules():
    started = asyncio.Event()
    async def never():
        started.set()
        await asyncio.Event().wait()
    port = LengthPort(gate=never)
    intent = dict(mode='story', target_seconds=180, continuation=False)
    script = dict(title='灯塔', spoken_text='海边有一座小小的灯塔，每到夜晚就亮起温柔的光。' * 4,
                  continuation_summary='小灯塔守护安静的海边。')
    story = body(text='好，我给你讲一个。', delivery='text', speech=script)
    result, engine = run_turn(port, user='讲个故事吧', companion=SpeechPort(intent),
                              engine=Engine(story))
    assert result.state is ReplyState.COMPLETED and result.reply_length is None
    assert any('<speech_request>' in m['content'] for m in engine.requests[0].normalized_messages())
    assert not length_messages(engine)


def test_piano_clips_keep_their_own_rule():
    from runtime.reply.companion_decision import CompanionDecisionResult, FrozenCompanionDecision, MODEL
    from tests.media.test_piano import CAPS
    from tests.persona.test_companion_decision import project
    from tests.persona.test_jev_pipeline import plan

    class PianoPort:
        async def decide(self, turn):
            value = plan()
            response = dict(schema_version='companion-shadow/1', backend='jev', model=MODEL,
                contract_valid=True, status='valid_contract', fallback=False, action_executed=False,
                production_approved=False, latency_ms=1, plan=value, tasks=project(value),
                api_calls=1, usage={'input_tokens': 1}, piano_request=dict(source='original', piece_hint='夜曲', seconds=30))
            return CompanionDecisionResult(decision=FrozenCompanionDecision.from_response(turn, response))
    result, engine = run_turn(LengthPort('long'), user='弹一段夜曲给我听', companion=PianoPort(),
                              extra=dict(piano_experience=CAPS, speech_enabled=False))
    assert result.state is ReplyState.COMPLETED and result.reply_length is None
    assert any('<piano_task>' in m['content'] for m in engine.requests[0].normalized_messages())
    assert not length_messages(engine)


@pytest.mark.parametrize('kwargs', [dict(proactive=True, raw=''), dict(proactive=True), dict(raw='   ')])
def test_proactive_and_empty_turns_are_short_without_asking(kwargs):
    port = LengthPort('long')
    result, engine = run_turn(port, **kwargs)
    assert result.state is ReplyState.COMPLETED and result.reply_length == 'short'
    assert not port.calls
    (note,) = length_messages(engine)
    assert note['content'].startswith(SHORT_BLOCK)


@pytest.mark.parametrize('port', [
    LengthPort(error=RuntimeError('JEV_TIMEOUT')),
    LengthPort(error=ValueError('JEV_RESPONSE_INVALID')),
    LengthPort(answer={'length': 'medium'}),
    LengthPort(answer={}),
    LengthPort(answer=None)])
def test_a_failed_judgment_is_short_and_never_fails_the_reply(port):
    result, engine = run_turn(port)
    assert result.state is ReplyState.COMPLETED and result.reply_length == 'short'
    (note,) = length_messages(engine)
    assert note['content'].startswith(SHORT_BLOCK)


def test_a_slow_judgment_times_out_to_short(monkeypatch):
    monkeypatch.setattr(reply_length, 'TIMEOUT_SECONDS', .05)
    async def never():
        await asyncio.Event().wait()
    result, engine = run_turn(LengthPort('long', gate=never))
    assert result.state is ReplyState.COMPLETED and result.reply_length == 'short'
    assert length_messages(engine)[0]['content'].startswith(SHORT_BLOCK)


@pytest.mark.parametrize('options', [dict(channel='wechat'), dict(mode=ReplyMode.TEXT_LETTER)])
def test_other_channels_and_letters_are_not_judged(options):
    port = LengthPort('long')
    result, engine = run_turn(port, **options)
    assert not port.calls and result.reply_length is None
    assert not length_messages(engine)


def test_without_a_configured_port_the_writer_messages_are_unchanged():
    result, engine = run_turn(None)
    assert result.state is ReplyState.COMPLETED and result.reply_length is None
    assert not length_messages(engine)


def test_judgment_runs_with_world_and_emotion_not_after_them():
    async def scenario():
        world_started, ask_started, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        async def world(text, *, now):
            order.append('world')
            world_started.set()
            await ask_started.wait()
            return ()

        async def emotion(text, *, now):
            await ask_started.wait()

        async def gate():
            order.append('ask')
            ask_started.set()
            await world_started.wait()
            await asyncio.sleep(.02)

        class Writer(Engine):
            gateway = SimpleNamespace(adapter=SimpleNamespace(daily_life=object(),
                prepare_daily_life_fragments=world, prepare_character_emotion=emotion))
        engine = Writer(body())
        port = LengthPort('long', gate=gate)
        pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                                 discover_runtime_ports=False, reply_length_port=port)
        context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True, trusted_time=TrustedTime(NOW))
        token = CURRENT.set(dict(structured=True, raw_user_text='今晚不赶时间。', channel='qq',
            recent_messages=[], received_source_id='synthetic-parallel:user', input_revision=1,
            decision_now='2026-10-12T22:00:00+08:00'))
        try:
            # World and emotion wait for the question to start, the question waits for the world:
            # a sequential step would deadlock here.
            result = await asyncio.wait_for(pipeline.run(ReplyRequest(content='今晚不赶时间。'), context), 2)
        finally:
            CURRENT.reset(token)
        assert result.state is ReplyState.COMPLETED, result.error_code
        assert result.reply_length == 'long' and set(order) == {'world', 'ask'}
        assert any(LONG_BLOCK in m['content'] for m in engine.requests[0].normalized_messages())
    asyncio.run(scenario())


def test_the_writer_waits_for_the_judgment_before_its_prompt_is_assembled():
    async def scenario():
        engine_calls, release = [], asyncio.Event()

        async def gate():
            await release.wait()

        class Writer(Engine):
            async def run(self, request):
                engine_calls.append(release.is_set())
                return await super().run(request)
        engine = Writer(body())
        pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                                 discover_runtime_ports=False, reply_length_port=LengthPort('long', gate=gate))
        context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True, trusted_time=TrustedTime(NOW))
        token = CURRENT.set(dict(structured=True, raw_user_text='详细说说。', channel='qq', recent_messages=[],
            received_source_id='synthetic-wait:user', input_revision=1, decision_now='2026-10-12T22:00:00+08:00'))
        try:
            task = asyncio.create_task(pipeline.run(ReplyRequest(messages=(
                {'role': 'system', 'content': '合成人设'}, {'role': 'user', 'content': '详细说说。'})), context))
            await asyncio.sleep(.05)
            assert not task.done() and not engine_calls
            release.set()
            result = await asyncio.wait_for(task, 2)
        finally:
            CURRENT.reset(token)
        assert result.state is ReplyState.COMPLETED and engine_calls == [True] and result.reply_length == 'long'
    asyncio.run(scenario())


def test_an_unfinished_judgment_does_not_outlive_a_cancelled_reply():
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def gate():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        pipeline = ReplyPipeline(Engine('{}'), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                                 discover_runtime_ports=False, reply_length_port=LengthPort(gate=gate))
        context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True, trusted_time=TrustedTime(NOW))
        token = CURRENT.set(dict(structured=True, raw_user_text='在吗？', channel='qq', recent_messages=[],
            received_source_id='synthetic-cancel:user', input_revision=1, decision_now='2026-10-12T22:00:00+08:00'))
        try:
            task = asyncio.create_task(pipeline.run(ReplyRequest(messages=(
                {'role': 'system', 'content': '合成人设'}, {'role': 'user', 'content': '在吗？'})), context))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            CURRENT.reset(token)
        assert stopped.is_set()
    asyncio.run(scenario())


def test_the_chat_rule_defers_to_the_block_without_carrying_the_tag_itself():
    assert 'reply_length' in INSTRUCTION
    assert '<reply_length>' not in INSTRUCTION
    assert '长文或认真倾诉认真回应，不硬截长度' not in INSTRUCTION
