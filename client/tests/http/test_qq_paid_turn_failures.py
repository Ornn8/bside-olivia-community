"""A written reply is never lost to its speech, and a short balance is never retried."""
import asyncio
from types import SimpleNamespace

import pytest

from reply_orchestrator import ReplyState
from runtime.cloud_service import CloudError
from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService
from tests.http.test_chat_jev_decision import pipeline_result, server_fixture
from tests.http.test_chat_jev_proactive import environment, run_contact
from tests.http.test_personal_chat_provider_recovery import _fixture


def speech_turn(monkeypatch, tmp_path, result, failure):
    """The real backend and service for one turn whose speech render fails."""
    server, _, _, _, _ = server_fixture(monkeypatch, tmp_path, result)
    rows, sent, generated = [], [], []
    server.store.personal_chats = rows
    server._persist_store_state = lambda: None
    async def unavailable(*a, **kwargs): raise failure
    monkeypatch.setattr(backend, 'prepare_chat_audio', unavailable)
    async def generate(event, row):
        generated.append(event.exchange_id)
        return await backend.generate(server, event, row)
    async def commit(row): pass
    async def text_send(text):
        sent.append(text)
        return 'confirmed-text'
    async def audio_send(path): raise AssertionError('missing audio must not be sent')
    text_send.audio = audio_send
    return PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')}), rows, sent, generated, text_send


@pytest.mark.parametrize('planned', [False, True])
@pytest.mark.parametrize('failure, fallback, basis', [
    (RuntimeError('synthetic media wallet rejection'), 'PERSONAL_CHAT_TTS_UNAVAILABLE', 'VOICE_RENDER_FAILED'),
    (TimeoutError(), 'PERSONAL_CHAT_TTS_TIMEOUT', 'VOICE_RENDER_TIMEOUT'),
])
def test_speech_failure_delivers_the_written_reply_once_without_regenerating(
        monkeypatch, tmp_path, planned, failure, fallback, basis):
    # Writer-chosen voice with no plan, and a JEV audio plan: both lost the paid reply before.
    result = pipeline_result(delivery='audio_speech') if planned else pipeline_result(record=False)
    service, rows, sent, generated, text_send = speech_turn(monkeypatch, tmp_path, result, failure)
    event = PersonalMessage('qq', 'b', 'u', '1', 'Hello')
    async def scenario():
        await service.handle(event, text_send)
        await service.handle(event, text_send)  # A platform replay must neither regenerate nor resend.
        await asyncio.gather(*service.consumer_tasks.values())
    asyncio.run(scenario())
    assert sent == ['Hello'] and len(generated) == 1
    row = rows[0]
    assert row['delivery_status'] == 'DELIVERED' and row['delivered_format'] == 'text'
    assert row['generation_attempts'] == 1 and 'error_code' not in row
    assert row['voice_fallback'] == fallback and row['delivery_basis'] == basis
    assert 'prepared_audio' not in row


@pytest.mark.parametrize('failure, fallback, basis', [
    (RuntimeError('synthetic media wallet rejection'), 'PERSONAL_CHAT_TTS_UNAVAILABLE', 'VOICE_RENDER_FAILED'),
    (TimeoutError(), 'PERSONAL_CHAT_TTS_TIMEOUT', 'VOICE_RENDER_TIMEOUT'),
])
def test_proactive_audio_contact_is_not_sent_as_text_when_the_voice_fails(monkeypatch, tmp_path, failure, fallback, basis):
    env = environment(monkeypatch, tmp_path, medium='audio_speech')
    attempts = []
    async def unavailable(server, text, path, **kwargs):
        attempts.append(text)
        raise failure
    monkeypatch.setattr(backend, 'prepare_chat_audio', unavailable)
    run_contact(env)
    run_contact(env)  # The same contact is never regenerated.
    assert len(env.calls) == len(env.writes) == 1 and len(attempts) == 1
    assert env.sent == []  # Her own contact is a voice message or nothing, never its text.
    row = env.rows[0]
    assert row['delivery_status'] == 'SKIPPED' and row['skip_reason'] == 'PROACTIVE_NO_REPLY'
    assert row['proactive_decision']['decision']['medium'] == 'audio_speech'
    assert row['voice_fallback'] == fallback and row['delivery_basis'] == basis
    assert row.get('error_code') is None and row['generation_attempts'] == 1
    assert not env.photos and not env.audio and not env.committed


def test_proactive_tick_with_failed_speech_spends_its_opportunity_and_releases_the_slot(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from runtime.personal_chat.initiative import Initiative
    from runtime.reply import proactive_runtime
    env = environment(monkeypatch, tmp_path, medium='audio_speech')
    async def unavailable(*a, **kwargs): raise RuntimeError('synthetic media wallet rejection')
    monkeypatch.setattr(backend, 'prepare_chat_audio', unavailable)
    runtime = {'status': {'qq': 'CONNECTED'}, 'service': env.service}
    monkeypatch.setattr(backend, 'selected_channels', lambda _: {'qq'})
    now = [datetime(2026, 9, 27, 12, tzinfo=timezone.utc).timestamp()]
    initiative = Initiative(env.rows, clock=lambda: now[0], interval=lambda: 3600,
        profile_provider=lambda: proactive_runtime.live_profile(env.server))
    initiative.received(PersonalMessage('qq', 'bot', 'owner', 'prior', '此前收到的原话'), env.sender)
    initiative.due = now[0]
    asyncio.run(backend._proactive_contact(env.server, runtime, initiative))
    assert env.sent == [] and env.rows[0]['delivery_status'] == 'SKIPPED'
    assert env.server._jev_contact_channel is None
    assert initiative.due == now[0] + 3600  # Counted as an attempt, not left due to fire again.


def balance_result(code):
    return SimpleNamespace(state=ReplyState.FAILED, error_code=code, retryable=None, failure_context={}, text='')


@pytest.mark.parametrize('code, stored', [
    ('JEV_BALANCE_INSUFFICIENT', 'JEV_BALANCE_INSUFFICIENT'),
    ('JEV_BILLING_HTTP_402', 'JEV_BILLING_HTTP_402'),
    ('PROVIDER_QUOTA_EXHAUSTED', 'PERSONAL_CHAT_PROVIDER_QUOTA_EXHAUSTED'),
])
def test_insufficient_balance_is_not_retried_and_says_so(tmp_path, monkeypatch, code, stored):
    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch, [])
    runs = []
    async def run(request, context):
        runs.append(request.request_id)
        return balance_result(code)
    server.reply_pipeline = SimpleNamespace(run=run)
    async def scenario():
        app, runner = await boot()
        try:
            event = PersonalMessage('qq', '100', '200', 'short', 'private-user-message')
            await captured['handler'](event, send)
            await captured['handler'](event, send)  # A replay is not another attempt either.
        finally:
            await runner.cleanup()
    asyncio.run(scenario())
    row = server.store.personal_chats[0]
    assert len(runs) == 1 and row['generation_attempts'] == 1
    assert row['delivery_status'] == 'FAILED' and row['generation_retryable'] is False
    assert row['error_code'] == stored
    assert len(sent) == 1 and '余额不足' in sent[0] and '回复生成失败' not in sent[0]


@pytest.mark.parametrize('code', ['LLM_TIMEOUT', 'REVIEWER_UNAVAILABLE'])
def test_ordinary_failures_keep_their_single_retry(tmp_path, monkeypatch, code):
    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch, [])
    runs = []
    async def run(request, context):
        runs.append(request.request_id)
        return balance_result(code)
    server.reply_pipeline = SimpleNamespace(run=run)
    async def scenario():
        app, runner = await boot()
        try:
            await captured['handler'](PersonalMessage('qq', '100', '200', 'busy', 'private-user-message'), send)
        finally:
            await runner.cleanup()
    asyncio.run(scenario())
    row = server.store.personal_chats[0]
    assert len(runs) == row['generation_attempts'] == 2
    assert row['delivery_status'] == 'FAILED' and row.get('generation_retryable') is not False
    assert len(sent) == 1 and '回复生成失败' in sent[0]


@pytest.mark.parametrize('failure', [CloudError('GPU_INSUFFICIENT_BALANCE', 402), RuntimeError('JEV_BALANCE_INSUFFICIENT')])
def test_service_never_regenerates_after_a_balance_error_from_any_generation_stage(failure):
    async def scenario():
        rows, sent, generated = [], [], []
        async def generate(event, row):
            generated.append(1)
            raise failure
        async def commit(row): pass
        async def send(text):
            sent.append(text)
            return 'ack'
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        event = PersonalMessage('qq', 'b', 'u', '1', 'Hello')
        for _ in range(2):
            with pytest.raises(Exception):
                await service.handle(event, send)
        await service.notify_generation_failure(event, send)
        assert len(generated) == 1 and rows[0]['generation_retryable'] is False
        assert rows[0]['error_code'] in {'PERSONAL_CHAT_PROVIDER_QUOTA_EXHAUSTED', 'JEV_BALANCE_INSUFFICIENT'}
        assert len(sent) == 1 and '余额不足' in sent[0]
    asyncio.run(scenario())
