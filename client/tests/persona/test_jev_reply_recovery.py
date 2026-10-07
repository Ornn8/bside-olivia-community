"""Received text survives auxiliary outages without acquiring action authority."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from reply_orchestrator import ReplyRequest, ReplyState
from runtime.personal_chat.presentation import CURRENT
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer, ReviewVerdict
from runtime.reply.world_context_selection import WorldSelectionError
from tests.persona.test_jev_pipeline import Port
from tests.persona.test_reply_semantic_wiring import Engine, Interpreter, Reviewer
from tests.http.test_personal_chat_decision import envelope

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


async def invoke(pipeline, mode, raw, *, metadata=None, request=None):
    context = ReplyContext.create(mode, future_im_enabled=True, trusted_time=TrustedTime(NOW))
    token = CURRENT.set(dict(structured=True, channel='qq', raw_user_text=raw,
        received_source_id='synthetic-receipt', input_revision=0, turn_is_current=lambda: True,
        semantic_kinds=['text', 'audio_speech', 'image'], decision_now=NOW.isoformat(),
        **(metadata or {}))) if mode is ReplyMode.FUTURE_IM else None
    try:
        return await pipeline.run(request or ReplyRequest(content=raw, request_id='synthetic-reply',
            messages=({'role': 'system', 'content': '核心人格与已经核实的关系不改变。'},
                      {'role': 'user', 'content': raw}), max_input_chars=40000), context)
    finally:
        if token is not None:
            CURRENT.reset(token)


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
@pytest.mark.parametrize('code', ['JEV_UNAVAILABLE', 'JEV_TIMEOUT', 'JEV_PROVIDER_HTTP_429',
                                  'JEV_HTTP_502', 'JEV_HTTP_504'])
def test_auxiliary_transport_failure_can_complete_reviewed_text(mode, code, monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    raw = '早上好，今天怎么样？'
    engine = Engine(envelope(delivery='text', sticker=None)
                    if mode is ReplyMode.FUTURE_IM else '早上好。')
    reviewer, old = Reviewer(ReviewVerdict.PASS), Interpreter()
    port = Port(error=code)
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=port, current_turn_interpreter=old)
    result = asyncio.run(invoke(pipeline, mode, raw))
    assert result.state is ReplyState.COMPLETED, result.error_code
    assert len(engine.requests) == len(reviewer.seen) == len(port.turns) == 1
    assert not old.seen and result.companion_decision is None
    assert result.companion_delivery == 'text'
    assert result.degraded_stages == {'decision': code}
    assert engine.requests[0].messages[-1]['content'] == raw
    assert '核心人格与已经核实的关系不改变。' in str(engine.requests[0].messages)


@pytest.mark.parametrize('changes', [dict(delivery='voice'), dict(initiative='pause', evidence='先别找我'),
    dict(skip=True, text='', silence={'kind': 'no_reply', 'evidence': '先别找我'}),
    dict(letter_invitation=True), dict(followup_at='2026-10-07T06:00:00+00:00', evidence='明天找我')])
def test_recovery_candidate_cannot_execute_controls_silence_or_media(changes, monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    engine = Engine(envelope(**{'delivery': 'text', 'sticker': None, **changes}))
    pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=Port(error='JEV_UNAVAILABLE'))
    result = asyncio.run(invoke(pipeline, ReplyMode.FUTURE_IM, '先别找我，明天找我'))
    assert result.state is ReplyState.FAILED and not result.text
    assert result.decision_rejection_reason == 'RECOVERY_ACTION_WITHOUT_PLAN'


def test_media_request_keeps_original_and_allows_only_clarification(monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    raw = '请发一段唱歌视频，再给我一张照片。'
    text = '视频和照片暂时还没完成，我先接住你的消息。'
    engine = Engine(envelope(text=text, delivery='text', sticker=None))
    pipeline = ReplyPipeline(engine, reviewer=Reviewer(ReviewVerdict.PASS), rewriter=UnavailableRewriter(),
        discover_runtime_ports=False, companion_decision_port=Port(error='JEV_UNAVAILABLE'))
    result = asyncio.run(invoke(pipeline, ReplyMode.FUTURE_IM, raw))
    assert result.state is ReplyState.COMPLETED and text in result.text
    assert result.companion_decision is None and result.companion_delivery == 'text'
    assert engine.requests[0].messages[-1]['content'] == raw
    assert '尚未完成' in str(engine.requests[0].messages)


def test_letter_world_and_emotion_are_concurrent_and_joined(monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    async def scenario():
        world_started, emotion_started = asyncio.Event(), asyncio.Event()
        async def world(text, *, now):
            world_started.set()
            await asyncio.wait_for(emotion_started.wait(), .3)
            return ()
        async def emotion(text, *, now):
            emotion_started.set()
            await asyncio.wait_for(world_started.wait(), .3)
        engine = Engine('收到啦。')
        engine.gateway = SimpleNamespace(adapter=SimpleNamespace(daily_life=object(),
            prepare_daily_life_fragments=world, prepare_character_emotion=emotion))
        pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
                                 discover_runtime_ports=False)
        result = await invoke(pipeline, ReplyMode.TEXT_LETTER, '早安', request=ReplyRequest(content='早安'))
        assert result.state is ReplyState.COMPLETED
        assert world_started.is_set() and emotion_started.is_set() and len(engine.requests) == 1
    asyncio.run(scenario())


def test_unavailable_world_keeps_review_and_unknown_disclosure(monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    async def world(text, *, now):
        raise WorldSelectionError('JEV_UNAVAILABLE')
    engine, reviewer = Engine('收到啦。'), Reviewer(ReviewVerdict.PASS)
    engine.gateway = SimpleNamespace(adapter=SimpleNamespace(daily_life=object(),
        prepare_daily_life_fragments=world))
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False)
    result = asyncio.run(invoke(pipeline, ReplyMode.TEXT_LETTER, '午饭吃了什么？',
        request=ReplyRequest(content='午饭吃了什么？')))
    assert result.state is ReplyState.COMPLETED and len(reviewer.seen) == 1
    assert result.degraded_stages == {'world': 'JEV_UNAVAILABLE'}
    assert '未知' in str(reviewer.seen[0][1])


def test_invalid_world_failure_cancels_and_joins_other_preparation(monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def world(*args, **kwargs):
            await asyncio.wait_for(started.wait(), .3)
            raise WorldSelectionError('JEV_WORLD_SELECTION_INVALID')
        async def emotion(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        engine = Engine('不能生成。')
        engine.gateway = SimpleNamespace(adapter=SimpleNamespace(daily_life=object(),
            prepare_daily_life_fragments=world, prepare_character_emotion=emotion))
        pipeline = ReplyPipeline(engine, reviewer=Reviewer(ReviewVerdict.PASS),
            rewriter=UnavailableRewriter(), discover_runtime_ports=False)
        result = await invoke(pipeline, ReplyMode.TEXT_LETTER, '早安', request=ReplyRequest(content='早安'))
        assert result.error_code == 'JEV_WORLD_SELECTION_INVALID' and not engine.requests
        assert stopped.is_set() and not result.degraded_stages
    asyncio.run(scenario())


def test_necessary_review_outage_still_blocks_recovered_text(monkeypatch):
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    pipeline = ReplyPipeline(Engine('收到啦。'), reviewer=Reviewer(RuntimeError('synthetic outage')),
        rewriter=UnavailableRewriter(), discover_runtime_ports=False,
        companion_decision_port=Port(error='JEV_UNAVAILABLE'))
    result = asyncio.run(invoke(pipeline, ReplyMode.TEXT_LETTER, '早安'))
    assert result.state is ReplyState.FAILED and not result.text
    assert result.error_code == 'REVIEW_FAILED'


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM])
@pytest.mark.parametrize('kind', ['image', 'audio_speech'])
def test_world_outage_cannot_replace_a_valid_requested_media_plan(mode, kind, monkeypatch):
    from tests.persona.test_jev_pipeline import plan
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    async def world(text, *, now):
        raise WorldSelectionError('JEV_UNAVAILABLE')
    engine = Engine('不能冒充媒体完成。')
    engine.gateway = SimpleNamespace(adapter=SimpleNamespace(daily_life=object(),
        prepare_daily_life_fragments=world))
    pipeline = ReplyPipeline(engine, reviewer=Reviewer(ReviewVerdict.PASS),
        rewriter=UnavailableRewriter(), discover_runtime_ports=False,
        companion_decision_port=Port(plan(kind=kind)))
    from runtime.reply.companion_runtime import TURN_CONTEXT
    token = TURN_CONTEXT.set({'semantic_kinds': ['text', 'image', 'audio_speech']})
    try:
        result = asyncio.run(invoke(pipeline, mode, '请发一张照片或一段语音。',
            request=ReplyRequest(content='请发一张照片或一段语音。', max_input_chars=40000)))
    finally:
        TURN_CONTEXT.reset(token)
    assert result.state is ReplyState.FAILED and result.error_code == 'JEV_UNAVAILABLE'
    assert result.companion_decision['plan'] == plan(kind=kind)
    assert not result.text and not engine.requests


@pytest.mark.parametrize('restart', [False, True])
def test_letter_review_retry_reuses_private_writer_without_publishing_early(tmp_path, monkeypatch, restart):
    from runtime.reply.companion_runtime import TURN_CONTEXT
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    engine = Engine('已经生成的私有候选。')
    reviewer = Reviewer(RuntimeError('synthetic unavailable'), ReviewVerdict.PASS)
    pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
                             discover_runtime_ports=False, recovery_root=tmp_path)
    async def scenario():
        token = TURN_CONTEXT.set(dict(received_source_id='synthetic-letter', input_revision=0,
            recovery_namespace='synthetic-account', turn_is_current=lambda: True))
        try:
            first = await invoke(pipeline, ReplyMode.TEXT_LETTER, '早安')
            assert first.error_code == 'REVIEW_FAILED' and not first.text
            next_engine = Engine('不应该再生成的新正文。') if restart else engine
            next_pipeline = ReplyPipeline(next_engine, reviewer=Reviewer(ReviewVerdict.PASS),
                rewriter=UnavailableRewriter(), discover_runtime_ports=False, recovery_root=tmp_path) if restart else pipeline
            second = await invoke(next_pipeline, ReplyMode.TEXT_LETTER, '早安')
            assert second.state is ReplyState.COMPLETED and second.text == '已经生成的私有候选。'
            assert len(engine.requests) == 1 and (not restart or not next_engine.requests)
            assert second.stage_cache_hits['writer'] == 1
            import sqlite3
            with sqlite3.connect(tmp_path / 'reply-recovery/private-candidates.sqlite3') as connection:
                assert connection.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0
        finally:
            TURN_CONTEXT.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize('changed', ['account', 'revision', 'raw', 'context', 'writer_model', 'review_model'])
def test_restart_never_reuses_a_candidate_for_changed_authority(tmp_path, monkeypatch, changed):
    from dataclasses import replace
    from runtime.reply.companion_runtime import TURN_CONTEXT
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    async def scenario():
        metadata = dict(received_source_id='synthetic-letter', input_revision=0,
            recovery_namespace='account-a', turn_is_current=lambda: True)
        raw = '早安'
        async def run(engine, reviewer, values, *, context_changed=False):
            token = TURN_CONTEXT.set(values)
            try:
                context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(NOW))
                if context_changed:
                    context = replace(context, world_state_available=False)
                pipeline = ReplyPipeline(engine, reviewer=reviewer, rewriter=UnavailableRewriter(),
                    discover_runtime_ports=False, recovery_root=tmp_path)
                return await pipeline.run(ReplyRequest(content=raw, request_id='synthetic',
                    messages=({'role': 'system', 'content': '核心人格'}, {'role': 'user', 'content': raw}),
                    max_input_chars=40000), context)
            finally:
                TURN_CONTEXT.reset(token)
        def writer(body, model):
            engine = Engine(body)
            engine.gateway = SimpleNamespace(adapter=SimpleNamespace(config={'model': model}))
            return engine
        first_engine = writer('旧候选', 'writer-a')
        failed = Reviewer(RuntimeError('synthetic unavailable'))
        failed.adapter = SimpleNamespace(config={'model': 'review-a'})
        first = await run(first_engine, failed, metadata)
        assert first.error_code == 'REVIEW_FAILED'
        if changed == 'account': metadata['recovery_namespace'] = 'account-b'
        if changed == 'revision': metadata['input_revision'] = 1
        if changed == 'raw': raw = '午安'
        next_engine = writer('新候选', 'writer-b' if changed == 'writer_model' else 'writer-a')
        reviewer = Reviewer(ReviewVerdict.PASS)
        reviewer.adapter = SimpleNamespace(config={'model': 'review-b' if changed == 'review_model' else 'review-a'})
        second = await run(next_engine, reviewer, metadata, context_changed=changed == 'context')
        assert second.state is ReplyState.COMPLETED and second.text == '新候选'
        assert len(next_engine.requests) == len(reviewer.seen) == 1
    asyncio.run(scenario())
