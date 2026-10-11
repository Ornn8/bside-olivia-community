import asyncio
import json
import hashlib
from types import SimpleNamespace

import pytest

from runtime.diagnostics.reply_telemetry import Collector
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService
from tests.http.test_chat_jev_decision import ordinary_chat_result, server_fixture


@pytest.mark.parametrize('mode,expected', [('voice_only', ['audio']), ('text_then_voice', ['text', 'audio']), ('text_only', ['text'])])
def test_default_voice_sends_only_the_voice_and_replay_never_renders_twice(monkeypatch, tmp_path, mode, expected):
    from runtime import cloud_policy
    policy = cloud_policy.CloudPolicy(tmp_path, '2.2.16')
    policy.policy['qq_default_voice']['mode'] = mode
    monkeypatch.setattr(cloud_policy, '_active', policy)
    # Default QQ voice is the reply itself: the same words are never sent as text first.
    server, _, *_ = server_fixture(monkeypatch, tmp_path, ordinary_chat_result(delivery='text'))
    rows, sent, renders = [], [], []
    server.store.personal_chats = rows
    server._persist_store_state = lambda: None

    async def scenario():
        async def render(server, text, path, **options):
            renders.append(text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'audio')
            return {'duration_seconds': 1}
        monkeypatch.setattr(backend, 'prepare_chat_audio', render)
        async def send(text):
            sent.append(('text', text))
            return 'text-ack'
        async def audio(path):
            sent.append(('audio', str(path)))
            return 'voice-ack'
        send.audio = audio
        async def commit(row): pass
        service = PersonalChatService(rows, lambda: None,
            lambda event, row: backend.generate(server, event, row), commit, {'qq': ('b', 'u')},
            default_voice=lambda row, send: backend.deliver_default_voice(server, row, send))
        event = PersonalMessage('qq', 'b', 'u', 'first', 'hello')
        await service.handle(event, send)
        await service.handle(event, send)
        await asyncio.gather(*service.speech_tasks.values())
        assert len(renders) == (0 if mode == 'text_only' else 1) and [x[0] for x in sent] == expected
        assert rows[0]['delivery_status'] == 'DELIVERED'
        if mode == 'text_then_voice':
            assert rows[0]['default_voice_status'] == 'DELIVERED'
        else:
            assert 'default_voice_status' not in rows[0]
        await asyncio.gather(*service.consumer_tasks.values())
    asyncio.run(scenario())


def test_segment_fields_are_bounded_once_and_only_sent_with_schema3(tmp_path):
    c = Collector(tmp_path, clock=lambda: 1000)
    c.account('synthetic')
    row = dict(letter_id='private', channel='qq', created_at=1000, delivery_status='DELIVERED',
        reply_timing_seconds={'queue_wait': 1.25, 'world': 2, 'emotion': float('nan'),
                              'writer': True, 'quality': float('inf'), 'secret': 42,
                              'decision': 10**1000, 'media_in': -1, 'send_ack': 'secret'})
    c.observe([row]); c.observe([row])
    assert all('timing_seconds' not in x for x in c.batch(1) + c.batch(2))
    events = [x for x in c.batch(3) if 'timing_seconds' in x]
    assert len(events) == 1
    assert events[0]['stage'] == 'reply'
    assert events[0]['timing_seconds'] == {'queue_wait': 1.25, 'world': 2.0}
    assert 'private' not in json.dumps(events)
    restored = Collector(tmp_path, clock=lambda: 1000)
    restored.account('synthetic'); restored.observe([row])
    assert restored.batch(3) == c.batch(3)
    async def ack(events):
        assert all('timing_seconds' not in x for x in events)
        return {'accepted': [e['event_id'] for e in events], 'supported_schemas': [1, 2, 3]}
    asyncio.run(c.upload(ack))
    assert c.schema == 3


@pytest.mark.parametrize('failure', ['render', 'send', 'unconfirmed', 'cancel'])
def test_optional_voice_failure_never_retries_or_changes_text(monkeypatch, tmp_path, failure):
    text = 'Reviewed canonical text'
    row = dict(letter_id='synthetic', channel='qq', delivery_status='DELIVERED', reply_text=text,
        default_voice_status='PENDING', delivery_receipt=dict(confirmed=True,
            text_sha256=hashlib.sha256(text.encode()).hexdigest()))
    calls, sent, persisted = [], [], []
    server = SimpleNamespace(_state_root=lambda: tmp_path)
    async def persist(server):
        persisted.append(row['default_voice_status'])
    monkeypatch.setattr(backend, 'persist_chat', persist)
    async def render(server, frozen, path, **kwargs):
        calls.append(frozen)
        assert persisted == ['RENDERING']
        if failure == 'render': raise TimeoutError()
        if failure == 'cancel': raise asyncio.CancelledError()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
        return {'duration_seconds': 1}
    monkeypatch.setattr(backend, 'prepare_chat_audio', render)
    async def audio(path):
        sent.append(path)
        assert persisted[-1] == 'SENDING'
        if failure == 'send': raise TimeoutError()
        return None
    async def run():
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await backend.deliver_default_voice(server, row, SimpleNamespace(audio=audio))
        else:
            await backend.deliver_default_voice(server, row, SimpleNamespace(audio=audio))
        await backend.deliver_default_voice(server, row, SimpleNamespace(audio=audio))
    asyncio.run(run())
    assert calls == [text] and len(sent) <= 1
    assert row['delivery_status'] == 'DELIVERED' and row['reply_text'] == text
    assert row['default_voice_status'] == ('FAILED' if failure in {'render', 'cancel'} else 'UNKNOWN')
    assert not list(tmp_path.rglob('*.wav'))


@pytest.mark.parametrize('state', ['RENDERING', 'SENDING', 'UNKNOWN', 'DELIVERED', 'FAILED'])
def test_interrupted_optional_voice_is_not_resubmitted(state):
    row = dict(delivery_status='DELIVERED', default_voice_status=state)
    asyncio.run(backend.deliver_default_voice(None, row, None))
    assert row['default_voice_status'] == state


def test_writer_transport_subtiming_uses_actual_gateway_scope(monkeypatch):
    from llm_gateway import OpenAICompatibleAdapter
    from runtime.diagnostics import reply_telemetry
    from runtime.diagnostics.reply_telemetry import TIMINGS
    clock = [100.0]
    monkeypatch.setattr(reply_telemetry, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    adapter = object.__new__(OpenAICompatibleAdapter)
    async def post(*args, **kwargs):
        clock[0] += .25
        return {'synthetic': True}
    monkeypatch.setattr(adapter, '_post_json_impl', post)
    row = {}
    token = TIMINGS.set(row)
    try:
        assert asyncio.run(adapter._post_json({}, 'synthetic', label='personal-chat-json')) == {'synthetic': True}
    finally:
        TIMINGS.reset(token)
    assert row['reply_timing_seconds']['writer_http'] == .25


def test_telemetry_downgrade_and_exhausted_failure_are_once(tmp_path):
    c = Collector(tmp_path, clock=lambda: 1000)
    c.account('synthetic')
    c.schema = 3
    row = dict(letter_id='private', channel='qq', created_at=1000, delivery_status='FAILED',
        generation_attempts=1, reply_timing_seconds={'writer': 2})
    c.observe([row])
    assert not any('timing_seconds' in e for e in c.batch(3))
    row['generation_attempts'] = 2
    c.observe([row]); c.observe([row])
    assert len([e for e in c.batch(3) if 'timing_seconds' in e]) == 1
    async def unavailable(events): raise TimeoutError()
    asyncio.run(c.upload(unavailable))
    assert c.schema == 1
    assert not any('timing_seconds' in e for e in c.batch(c.schema))
