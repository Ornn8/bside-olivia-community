"""Bound optional QQ speech without changing a promised media delivery."""
import asyncio
from copy import deepcopy
import json
import threading

import pytest

from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService, TURN_IS_CURRENT
from tests.http.test_chat_jev_decision import (
    ordinary_chat_result, pipeline_result, server_fixture,
)


@pytest.mark.parametrize('value, expected', [
    (None, 60), ('5', 5), ('120', 120), ('nan', 60), ('inf', 60),
    ('-1', 60), ('0', 60), ('121', 60), ('junk', 60),
])
def test_default_voice_budget_configuration_is_finite_and_bounded(monkeypatch, value, expected):
    monkeypatch.delenv('OLIVIA_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', raising=False)
    if value is not None:
        monkeypatch.setenv('OLIVIA_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', value)
    assert backend._qq_default_voice_timeout_seconds() == expected


def real_voice_server(monkeypatch, tmp_path, result):
    prepare = backend.prepare_chat_audio
    server, row, *_ = server_fixture(monkeypatch, tmp_path, result)
    monkeypatch.setattr(backend, 'prepare_chat_audio', prepare)
    monkeypatch.setenv('OLIVIA_TTS_CONFIG', str(tmp_path / 'tts.json'))
    monkeypatch.delenv('OLIVIA_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', raising=False)
    return server, row


def test_default_voice_timeout_delivers_reviewed_text_once_and_keeps_receiver_live(monkeypatch, tmp_path):
    result = ordinary_chat_result(delivery='text')
    server, _ = real_voice_server(monkeypatch, tmp_path, result)
    monkeypatch.setattr(backend, '_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', .02, raising=False)
    rows, sent, rendered = [], [], []
    release, completed = threading.Event(), threading.Event()
    server.store.personal_chats = rows
    server._persist_store_state = lambda: None

    def render(text, path, **kwargs):
        rendered.append((text, path))
        if len(rendered) == 1:
            assert release.wait(2)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic-audio')
        completed.set()
        return {'duration_seconds': 2}

    server.render_reply_audio = render
    async def generate(event, row):
        return await backend.generate(server, event, row)
    async def commit(row): pass
    async def send(text):
        sent.append(('text', text))
        return 'text-ack'
    async def audio(path):
        sent.append(('audio', path))
        return 'audio-ack'
    send.audio = audio

    async def scenario():
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('b', 'u')})
        event = PersonalMessage('qq', 'b', 'u', 'first', 'How was your day?')
        try:
            await asyncio.wait_for(service.handle(event, send), .5)
            assert len(rendered) == len(sent) == 1
            assert sent[0][0] == 'text'
            row = rows[0]
            assert row['delivery_status'] == 'DELIVERED' and row['delivered_format'] == 'text'
            assert row['voice_fallback'] == 'PERSONAL_CHAT_TTS_TIMEOUT'
            assert row['delivery_basis'] == 'VOICE_RENDER_TIMEOUT'
            assert row['voice_prepare_status'] == 'timeout'
            assert 0 <= row['voice_prepare_seconds'] < .5
            assert row['voice_prepare_timeout_seconds'] == .02
            assert 'prepared_audio' not in row
            snapshot = deepcopy(row)
            await service.handle(event, send)
            assert len(rendered) == len(sent) == 1
            release.set()
            assert await asyncio.to_thread(completed.wait, 1)
            await asyncio.sleep(.01)
            assert row == snapshot and len(sent) == 1
            assert not rendered[0][1].exists()
            # A subsequent receipt still gets its own normal voice reply.
            next_body = json.loads(result.text)
            next_body['text'] = 'A different answer about your next question'
            result.text = json.dumps(next_body, ensure_ascii=False)
            monkeypatch.setattr(backend, '_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', .5)
            await asyncio.wait_for(service.handle(
                PersonalMessage('qq', 'b', 'u', 'second', 'And what happened next?'), send), .5)
            assert len(rendered) == len(sent) == 2 and sent[-1][0] == 'audio'
            assert rendered[0][1] != rendered[1][1]
            assert rows[-1]['voice_prepare_status'] == 'completed'
            await asyncio.gather(*service.consumer_tasks.values())
        finally:
            release.set()
    asyncio.run(scenario())


def test_explicit_audio_does_not_use_optional_default_voice_budget(monkeypatch, tmp_path):
    server, row = real_voice_server(monkeypatch, tmp_path,
        pipeline_result(delivery='audio_speech'))
    monkeypatch.setattr(backend, '_QQ_DEFAULT_VOICE_TIMEOUT_SECONDS', .01, raising=False)
    release = threading.Event()
    def render(text, path, **kwargs):
        assert release.wait(2)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
        return {'duration_seconds': 1}
    server.render_reply_audio = render
    async def scenario():
        task = asyncio.create_task(backend.generate(server,
            PersonalMessage('qq', 'b', 'u', 'voice', 'Use voice, please'), row))
        try:
            await asyncio.sleep(.04)
            assert not task.done()  # Still honoring the audio contract.
            release.set()
            assert await asyncio.wait_for(task, .5) == 'Hello'
        finally:
            release.set()
    asyncio.run(scenario())
    assert row['voice_prepare_timeout_seconds'] == backend._VOICE_RENDER_TIMEOUT_SECONDS
    assert row['voice_prepare_status'] == 'completed' and row.get('prepared_audio')
    assert row['requested_format'] == 'voice' and 'voice_fallback' not in row


def test_explicit_audio_timeout_fails_its_media_contract_instead_of_sending_text(monkeypatch, tmp_path):
    server, row = real_voice_server(monkeypatch, tmp_path,
        pipeline_result(delivery='audio_speech'))
    monkeypatch.setattr(backend, '_VOICE_RENDER_TIMEOUT_SECONDS', .04)
    release = threading.Event()
    def render(text, path, **kwargs):
        assert release.wait(2)
        return {'duration_seconds': 1}
    server.render_reply_audio = render
    async def scenario():
        try:
            with pytest.raises(RuntimeError, match='JEV_PLAN_UNSUPPORTED'):
                await backend.generate(server,
                    PersonalMessage('qq', 'b', 'u', 'voice', 'Use voice, please'), row)
        finally:
            release.set()
    asyncio.run(scenario())
    assert row['voice_prepare_status'] == 'timeout'
    assert row['voice_fallback'] == 'PERSONAL_CHAT_TTS_TIMEOUT'
    assert row['delivery_basis'] == 'JEV_MEDIA_PLAN'
    assert 'prepared_audio' not in row and row.get('delivery_status') != 'DELIVERED'


@pytest.mark.parametrize('obsolete', ['revision', 'receipt'])
def test_finished_audio_for_obsolete_turn_is_not_adopted(monkeypatch, tmp_path, obsolete):
    server, row = real_voice_server(monkeypatch, tmp_path, ordinary_chat_result(delivery='text'))
    started, release = threading.Event(), threading.Event()
    current = [True]
    paths = []
    def render(text, path, **kwargs):
        paths.append(path)
        started.set()
        assert release.wait(2)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
        return {'duration_seconds': 1}
    server.render_reply_audio = render
    async def scenario():
        token = TURN_IS_CURRENT.set(lambda: current[0])
        task = asyncio.create_task(backend.generate(server,
            PersonalMessage('qq', 'b', 'u', 'stale', 'How was your day?'), row))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            if obsolete == 'revision':
                row['input_revision'] += 1
            else:
                current[0] = False
            release.set()
            await asyncio.wait_for(task, .5)
        finally:
            release.set()
            TURN_IS_CURRENT.reset(token)
    asyncio.run(scenario())
    assert 'prepared_audio' not in row and not paths[0].exists()


def test_cancelled_voice_wait_discards_late_artifact_and_never_restarts_worker(monkeypatch, tmp_path):
    monkeypatch.setenv('OLIVIA_TTS_CONFIG', str(tmp_path / 'tts.json'))
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    path = tmp_path / 'owned.wav'
    def render(text, target, **kwargs):
        calls.append(target)
        started.set()
        assert release.wait(2)
        target.write_bytes(b'late-audio')
        completed.set()
        return {'duration_seconds': 1}
    async def scenario():
        task = asyncio.create_task(backend.prepare_chat_audio(
            type('Server', (), {'render_reply_audio': staticmethod(render)})(),
            'Checked text', path, timeout_seconds=.5))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(completed.wait, 1)
            await asyncio.sleep(.01)
        finally:
            release.set()
    asyncio.run(scenario())
    assert calls == [path] and not path.exists()
