import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


@pytest.mark.parametrize('code', ['PERSONAL_CHAT_PROVIDER_TIMEOUT', 'PERSONAL_CHAT_PROVIDER_PROTOCOL',
                                 'LLM_TIMEOUT', 'PERSONAL_CHAT_GENERATION_TIMEOUT'])
def test_generation_reason_survives_persistence_and_status(code):
    async def scenario():
        rows = []
        async def generate(event, row):
            raise RuntimeError(code)
        async def commit(row):
            pytest.fail('Failed generation must not commit memory')
        async def send(text):
            pytest.fail('Failed generation must not send content')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        with pytest.raises(RuntimeError, match='^' + code + '$'):
            await service.handle(PersonalMessage('qq', '100', '200', '1', 'hello'), send)
        assert rows[-1]['delivery_status'] == 'FAILED'
        assert rows[-1]['error_code'] == code
        server = SimpleNamespace(store=SimpleNamespace(personal_chats=rows))
        assert backend.reply_errors(server, {'status': {'qq': 'CONNECTED'}})['qq'] == code
    asyncio.run(scenario())


def test_unknown_pipeline_error_does_not_expose_private_exception_text():
    assert backend._generation_failure_code('private user content and key') == 'PERSONAL_CHAT_GENERATION_FAILED'


@pytest.mark.parametrize('tier', ['reserved', 'familiar', 'trusted', 'close', 'committed'])
def test_queue_notice_uses_relationship_once_and_keeps_reply(tier):
    from runtime.personal_chat.service import QUEUE_NOTICE, queue_notice_text
    async def scenario():
        rows = [{'initiative_tier': tier, 'initiative_caution': 'normal'}]
        expected = queue_notice_text(rows)
        sent = []
        async def generate(event, row):
            await QUEUE_NOTICE.get()()
            await QUEUE_NOTICE.get()()
            return 'normal reply'
        async def commit(row): pass
        async def send(text):
            sent.append(text)
            return 'ack'
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        await service.handle(PersonalMessage('qq', '100', '200', '1', 'hello'), send)
        assert sent == [expected, 'normal reply']
        assert rows[-1]['queue_notice'] == 'DELIVERED'
        assert rows[-1]['reply_text'] == 'normal reply'
    asyncio.run(scenario())


def test_tense_relationship_notice_stays_restrained():
    from runtime.personal_chat.service import queue_notice_text
    message = queue_notice_text([{'initiative_tier': 'committed', 'initiative_caution': 'high'}])
    assert '你先忙自己的' in message
    assert '陪你' not in message


def test_remote_gpu_queued_progress_notifies_from_worker(monkeypatch, tmp_path):
    from runtime.personal_chat.service import QUEUE_NOTICE
    from runtime.remote_pipeline import PROGRESS_CALLBACK
    monkeypatch.setenv('OLIVIA_TTS_CONFIG', str(tmp_path / 'config.json'))
    release = threading.Event()
    def render(*args, **kwargs):
        assert kwargs['environment']['OLIVIA_MEDIA_CHANNEL'] == 'qq'
        PROGRESS_CALLBACK.get()('generation', {'status': 'queued'})
        assert release.wait(2)
        return {'duration_seconds': 1}
    async def scenario():
        notified = asyncio.Event()
        async def notice():
            notified.set()
        token = QUEUE_NOTICE.set(notice)
        server = SimpleNamespace(media_semaphore=asyncio.Semaphore(1), render_reply_audio=render)
        try:
            task = asyncio.create_task(backend.prepare_chat_audio(server, 'hello', tmp_path / 'voice.wav'))
            await asyncio.wait_for(notified.wait(), 1)
        finally:
            release.set()
            QUEUE_NOTICE.reset(token)
        assert await task == {'duration_seconds': 1}
    asyncio.run(scenario())


def test_voice_submits_while_other_media_holds_slot(monkeypatch, tmp_path):
    monkeypatch.setenv('OLIVIA_TTS_CONFIG', str(tmp_path / 'config.json'))
    async def scenario():
        server = SimpleNamespace(media_semaphore=asyncio.Semaphore(0),
            render_reply_audio=lambda *a, **k: {'duration_seconds': 1})
        result = await asyncio.wait_for(backend.prepare_chat_audio(server, 'hello', tmp_path / 'voice.wav'), 1)
        assert result == {'duration_seconds': 1}
        assert server.media_semaphore.locked()
    asyncio.run(scenario())


def test_voice_timeout_does_not_take_or_release_other_media_slot(monkeypatch, tmp_path):
    monkeypatch.setenv('OLIVIA_TTS_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(backend, '_VOICE_RENDER_TIMEOUT_SECONDS', .02)
    release = threading.Event()
    def render(*args, **kwargs):
        assert release.wait(3)
        return {'duration_seconds': 1}
    async def scenario():
        server = SimpleNamespace(media_semaphore=asyncio.Semaphore(1), render_reply_audio=render)
        try:
            with pytest.raises(TimeoutError):
                await backend.prepare_chat_audio(server, 'hello', tmp_path / 'voice.wav')
            assert not server.media_semaphore.locked()
        finally:
            release.set()
        await asyncio.wait_for(server.media_semaphore.acquire(), 1)
        server.media_semaphore.release()
    asyncio.run(scenario())


def test_user_message_preempts_proactive_generation_without_killing_loop():
    async def scenario():
        started = asyncio.Event()
        rows, sent = [], []
        async def generate(event, row):
            if row.get('origin') == 'proactive':
                started.set()
                await asyncio.Event().wait()
            return 'user reply'
        async def commit(row): pass
        async def send(text):
            sent.append(text)
            return 'ack'
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        proactive = asyncio.create_task(service.proactive(PersonalMessage('qq', '100', '200', 'p', ''), send))
        await started.wait()
        await asyncio.wait_for(service.handle(PersonalMessage('qq', '100', '200', 'u', 'hello'), send), .5)
        await proactive
        assert rows[0]['delivery_status'] == 'SKIPPED'
        assert sent == ['user reply']
    asyncio.run(scenario())


def test_setup_returns_reply_failure_even_when_connection_is_verified(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from runtime.personal_chat import setup
    from tests.http.test_personal_chat_setup import _Server
    server = _Server(tmp_path)
    server.store.personal_chats = [{'channel': 'qq', 'delivery_status': 'FAILED',
        'error_code': 'PERSONAL_CHAT_GENERATION_FAILED', 'content': 'private-content'}]
    monkeypatch.setattr(setup, '_selected_channels', lambda server: {'qq'})
    async def scenario():
        app = web.Application()
        setup.install_setup_routes(app, server)
        app[backend._RUNTIME] = {'status': {'qq': 'CONNECTED'}, 'errors': {},
            'e2e_verified_at': {'qq': '2026-09-24T00:00:00+00:00'}}
        async with TestClient(TestServer(app)) as client:
            response = await client.get(setup.STATUS_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE})
            assert response.status == 200
            body = await response.json()
            assert body['listeners']['qq'] == 'CONNECTED'
            assert body['reply_errors']['qq'] == 'PERSONAL_CHAT_GENERATION_FAILED'
            assert 'private-content' not in json.dumps(body)
    asyncio.run(scenario())


def test_async_store_keeps_loop_live_orders_snapshots_and_flushes_cancel(monkeypatch, tmp_path):
    import local_server as server
    started, release = threading.Event(), threading.Event()
    writes = []
    native = server.Store()
    monkeypatch.setattr(server, 'store', native)
    monkeypatch.setattr(server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(server, '_store_state_error_code', None)
    original = server._atomic_write_store_file
    def write(path, serialized):
        if path.name == 'state.json':
            writes.append(json.loads(serialized)['settings']['fixture'])
            if len(writes) == 1:
                started.set()
                assert release.wait(3)
        original(path, serialized)
    monkeypatch.setattr(server, '_atomic_write_store_file', write)
    async def scenario():
        native.settings['fixture'] = 'first'
        first = asyncio.create_task(server._persist_store_state_async())
        try:
            while not started.is_set():
                await asyncio.sleep(.001)
            native.settings['fixture'] = 'second'
            second = asyncio.create_task(server._persist_store_state_async())
            await asyncio.sleep(.01)
            first.cancel()
            await asyncio.sleep(.01)
            assert not first.done()
            assert writes == ['first']
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert writes == ['first', 'second']
        assert json.loads((tmp_path / 'state.json').read_text(encoding='utf-8'))['settings']['fixture'] == 'second'
        assert (tmp_path / 'state.json').read_bytes() == (tmp_path / 'state.json.bak').read_bytes()
    asyncio.run(scenario())
