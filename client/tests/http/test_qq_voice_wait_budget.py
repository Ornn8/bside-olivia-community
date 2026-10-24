"""Bound optional QQ speech without changing a promised media delivery."""
import asyncio
import threading

import pytest

from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import TURN_IS_CURRENT
from tests.http.test_chat_jev_decision import (
    pipeline_result, server_fixture,
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


def test_explicit_audio_on_qq_waits_the_qq_voice_budget(monkeypatch, tmp_path):
    # Every QQ voice waits at most the QQ budget; past it the words go as text (owner 2026-10-24).
    server, row = real_voice_server(monkeypatch, tmp_path,
        pipeline_result(delivery='audio_speech'))
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
            assert not task.done()  # still waiting inside the budget
            release.set()
            assert await asyncio.wait_for(task, .5) == 'Hello'
        finally:
            release.set()
    asyncio.run(scenario())
    assert row['voice_prepare_timeout_seconds'] == backend._qq_default_voice_timeout_seconds() == 60
    assert row['voice_prepare_status'] == 'completed' and row.get('prepared_audio')
    assert row['requested_format'] == 'voice' and 'voice_fallback' not in row


def test_explicit_audio_timeout_sends_the_written_text(monkeypatch, tmp_path):
    server, row = real_voice_server(monkeypatch, tmp_path,
        pipeline_result(delivery='audio_speech'))
    monkeypatch.setattr(backend, '_qq_default_voice_timeout_seconds', lambda: .04)
    release = threading.Event()
    def render(text, path, **kwargs):
        assert release.wait(2)
        return {'duration_seconds': 1}
    server.render_reply_audio = render
    async def scenario():
        try:
            return await backend.generate(server,
                PersonalMessage('qq', 'b', 'u', 'voice', 'Use voice, please'), row)
        finally:
            release.set()
    assert asyncio.run(scenario())
    assert row['voice_prepare_status'] == 'timeout'
    assert row['voice_fallback'] == 'PERSONAL_CHAT_TTS_TIMEOUT'
    assert row['delivery_basis'] == 'VOICE_RENDER_TIMEOUT'
    assert 'prepared_audio' not in row


@pytest.mark.parametrize('obsolete', ['revision', 'receipt'])
def test_finished_audio_for_obsolete_turn_is_not_adopted(monkeypatch, tmp_path, obsolete):
    server, row = real_voice_server(monkeypatch, tmp_path, pipeline_result(delivery='audio_speech'))
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
