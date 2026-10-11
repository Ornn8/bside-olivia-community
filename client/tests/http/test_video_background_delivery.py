import asyncio
import json
from pathlib import Path

import pytest

import local_server
from original_client_letter_contract import serialize_letter_detail


def video_letter(**changes):
    return dict(letter_id='background-video', content='synthetic request', reply_text='frozen reply',
                letter_status='COMPLETED', reply_mode='musical_video', reply_video_enabled=True,
                media_status='PROCESSING', reply_not_before=0, **changes)


@pytest.mark.parametrize('state', ['PENDING', 'QUEUED', 'PROCESSING', 'UNAVAILABLE'])
def test_frozen_text_visible_before_video_without_claiming_video_success(state):
    row = video_letter(); row['media_status'] = state
    result = serialize_letter_detail(row, now=1800000000)
    assert result['letterStatus'] == 4 and result['replyText'] == 'frozen reply'
    assert result['replyType'] == 1 and result['replyVideoUrl'] == ''
    assert result.get('videoPending', False) == (state != 'UNAVAILABLE')
    row.update(media_status='COMPLETED', reply_video_url='http://127.0.0.1:8899/toy/media/background-video.mp4')
    done = serialize_letter_detail(row, now=1800000000)
    assert done['replyText'] == result['replyText'] and done['replyVideoUrl'].endswith('.mp4')
    assert not done.get('videoPending', False)


def test_background_video_does_not_gate_new_letters_but_unfinished_text_still_does(monkeypatch):
    row = video_letter()
    monkeypatch.setattr(local_server.store, 'letters', [row])
    assert local_server._active_undelivered_letter() is None
    row['letter_status'] = 'PROCESSING'
    assert local_server._active_undelivered_letter() is row


def test_video_wait_does_not_hold_short_audio_slot(tmp_path, monkeypatch):
    video, speech = video_letter(), video_letter()
    speech.update(letter_id='later-speech', reply_mode='voice_reply', reply_video_enabled=False)
    monkeypatch.setattr(local_server.store, 'letters', [video, speech])
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(local_server, '_persist_media_state', lambda: None)
    monkeypatch.setattr(local_server, '_record_published_media', lambda *args, **kwargs: None)
    monkeypatch.setattr('runtime.remote_pipeline.enabled', lambda env: True)
    monkeypatch.setattr(local_server, '_current_music_performance', lambda env: None)
    started, release = asyncio.Event(), asyncio.Event()

    async def plan(*args):
        return None

    async def off_loop(fn, *args, **kwargs):
        if fn is local_server.render_musical_reply:
            started.set()
            await release.wait()
            Path(args[2]).write_bytes(b'video')
            return {}
        assert fn is local_server.render_reply_audio
        Path(args[1]).write_bytes(b'audio')
        return {'duration_seconds': 4}

    monkeypatch.setattr(local_server, '_voice_plan_for_letter', plan)
    monkeypatch.setattr(local_server, '_music_voice_plan_for_letter', plan)
    monkeypatch.setattr(local_server.asyncio, 'to_thread', off_loop)

    async def scenario():
        monkeypatch.setattr(local_server, 'media_semaphore', asyncio.Semaphore(1))
        monkeypatch.setattr(local_server, 'video_media_semaphore', asyncio.Semaphore(1), raising=False)
        task = asyncio.create_task(local_server._render_media_job(video['letter_id'], video['content'], video['reply_text'], video['reply_mode']))
        try:
            await asyncio.wait_for(started.wait(), .5)
            await asyncio.wait_for(local_server._render_media_job(speech['letter_id'], speech['content'], speech['reply_text'], speech['reply_mode']), .5)
            assert speech['media_status'] == 'COMPLETED' and not task.done()
        finally:
            release.set()
            await task
        assert video['media_status'] == 'COMPLETED'
        assert video['reply_text'] == 'frozen reply'
        assert video['is_read'] == 0 and video['video_completed_at'] > 0

    asyncio.run(scenario())


@pytest.mark.parametrize('saved_order', [True, False])
def test_restart_resumes_original_video_only_with_saved_submission(tmp_path, monkeypatch, saved_order):
    row = video_letter()
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('OLIVIA_GPU_ROUTE', 'remote')
    monkeypatch.setattr(local_server.store, 'letters', [row])
    if saved_order:
        media = tmp_path / 'media'; media.mkdir()
        (media / 'background-video-remote-order.private.json').write_text(json.dumps({
            'fingerprint': 'a'*64, 'task_id': 'original-task',
            'submission': {'request_id': 'original-order', 'kind': 'original_video', 'input': {'lyrics': 'frozen'}}
        }))
    local_server._persist_store_state()
    local_server.store.letters.clear()
    local_server._load_store_state()
    restored = local_server.store.letters[0]
    assert restored['reply_text'] == 'frozen reply'
    assert restored['media_status'] == ('QUEUED' if saved_order else 'UNAVAILABLE')
    scheduled = []
    monkeypatch.setattr(local_server, '_schedule_media_job', lambda *args: scheduled.append(args))
    monkeypatch.setattr(local_server, 'receive_eligibility_from_letter', lambda row: type('Eligibility', (), {'enabled': True})())
    assert local_server._schedule_pending_media_jobs() == int(saved_order)
    if saved_order:
        assert scheduled == [('background-video', 'synthetic request', 'frozen reply', 'musical_video')]
