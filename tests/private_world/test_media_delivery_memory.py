import hashlib
import json
import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from datetime import datetime, timezone

from runtime.reply.media_delivery import make_delivery, delivery_references, delivery_outcome
from runtime.reply.recent_correspondence import recent_correspondence
from runtime.private_world.daily_life import DailyLifeStore

NOW = datetime(2026, 9, 9, 9, tzinfo=timezone.utc)


def letter():
    text = '给你唱这一段。'
    return dict(letter_id='sample', reply_revision=1, private_world_delivery_id='sample:1',
        private_world_reply_sha256=hashlib.sha256(text.encode()).hexdigest(),
        reply_text=text, content='请翻唱我提供的音频。', letter_status='COMPLETED',
        private_world_occurred_at=NOW.isoformat(), material=dict(cover_source_id='source-1', cover_lyrics='我永远陪你。'))


def test_media_completion_is_idempotent_and_never_a_current_activity(tmp_path):
    row = letter()
    event = make_delivery(row, component='cover', presentation='audio', occurred_at=NOW)
    row['media_deliveries'] = [event]
    store = DailyLifeStore(tmp_path/'world.sqlite')
    assert store.record_media_delivery(event)
    assert not store.record_media_delivery(event)
    assert store.snapshot(NOW)['current'] is None
    context = store.reply_context('你之前给我唱过歌吗？', now=NOW)
    assert 'source-1' in context and '音频回复中包含翻唱内容' in context
    assert '我永远陪你' not in context
    history = recent_correspondence([row], query='你记得给我唱过什么吗？')
    assert 'media_deliveries' in history and 'source-1' in history
    assert '我永远陪你' not in history


def test_no_event_from_status_or_other_revision_alone():
    row = letter()
    row['media_status'] = 'COMPLETED'
    assert delivery_references(row) == []
    event = make_delivery(row, component='speech', presentation='video', occurred_at=NOW)
    row['media_deliveries'] = [event]
    row['reply_revision'] = 2
    assert delivery_references(row) == []


def test_old_media_completion_survives_unrelated_recent_letters():
    original = letter()
    original['media_deliveries'] = [make_delivery(original, component='cover', presentation='audio', occurred_at=NOW)]
    rows = [original]
    for i in range(4):
        rows.append({**letter(), 'letter_id': f'new-{i}', 'content': '今天整理了书架。',
                     'reply_text': '嗯。', 'private_world_occurred_at': NOW.replace(hour=10+i).isoformat()})
    result = json.loads(recent_correspondence(rows, query='你之前给我唱过吗？'))
    assert any(row.get('media_deliveries') for row in result['letters'])


def test_partial_delivery_has_no_fabricated_singing():
    row = letter()
    row['media_deliveries'] = [make_delivery(row, component='speech', presentation='audio', occurred_at=NOW)]
    row['media_status'] = 'FAILED'
    refs = delivery_references(row)
    assert len(refs) == 1 and refs[0]['component'] == 'speech'
    assert '翻唱' not in json.dumps(refs, ensure_ascii=False)
    assert delivery_outcome(row) == {'cover': '这部分没成功，停在哪一步未知，不能确认已经做出成品', 'speech': '已经发给对方'}
    history = json.loads(recent_correspondence([row], query='上次歌唱好了吗？'))
    assert history['letters'][0]['media_outcome'] == delivery_outcome(row)
    assert '制作之前' in history['letters'][0]['reply_phase']
    row['media_status'] = 'PROCESSING'
    assert delivery_outcome(row)['cover'] == '尚不能确认已发给对方'
    row['media_deliveries'].append(make_delivery(row, component='cover', presentation='audio', occurred_at=NOW))
    assert delivery_outcome(row) == {'cover': '已经发给对方', 'speech': '已经发给对方'}


def test_world_retry_and_stale_job_do_not_duplicate_or_replace_delivery(tmp_path, monkeypatch):
    import local_server as server
    monkeypatch.setattr(server, '_persist_media_state', lambda: None)
    row = letter()
    artifact = tmp_path/'speech.wav'
    artifact.write_bytes(b'synthetic audio')
    class Offline:
        def record_media_delivery(self, event):
            raise sqlite3.OperationalError('synthetic unavailable')
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=Offline()))
    kwargs = dict(reply_text=row['reply_text'], delivery_id='sample:1', path=artifact,
                  components=('speech',), presentation='audio')
    server._record_published_media(row, **kwargs)
    assert row['media_world_status'] == 'PENDING'
    store = DailyLifeStore(tmp_path/'world.sqlite')
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=store, schedule_refresh=lambda now: None))
    monkeypatch.setattr(server.store, 'letters', [row])
    monkeypatch.setattr(server, '_schedule_pending_reply_jobs', lambda: None)
    monkeypatch.setattr(server, '_schedule_pending_media_jobs', lambda: None)
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    asyncio.run(server._start_reply_tasks(None))
    server._record_published_media(row, **kwargs)
    assert row['media_world_status'] == 'COMMITTED'
    assert len(row['media_deliveries']) == len(store.history()['moments']) == 1
    row.update(reply_revision=2, private_world_delivery_id='sample:2')
    server._record_published_media(row, **kwargs)
    assert delivery_references(row) == []
    assert len(store.history()['moments']) == 1


def test_invalid_legacy_binding_does_not_break_media_playback(tmp_path, monkeypatch):
    import local_server as server
    row = letter()
    row.update(media_status='COMPLETED', private_world_reply_sha256='bad-hash')
    path = tmp_path/'ready.wav'
    path.write_bytes(b'synthetic audio')
    monkeypatch.setattr(server, 'daily_life_runtime', None)
    server._record_published_media(row, reply_text=row['reply_text'], delivery_id='sample:1',
        path=path, components=('speech',), presentation='audio')
    assert row['media_status'] == 'COMPLETED'
    assert row['media_world_status'] == 'UNAVAILABLE'
    assert not row.get('media_deliveries')


def test_old_renderer_cannot_publish_into_new_revision(tmp_path, monkeypatch):
    import local_server as server
    row = letter()
    row.update(reply_mode='voice_reply', reply_video_enabled=False)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(server.store, 'letters', [row])
    monkeypatch.setattr(server, 'media_semaphore', asyncio.Semaphore(1))
    monkeypatch.setattr(server, '_local_data_root', lambda *a: tmp_path)
    monkeypatch.setattr(server, '_persist_media_state', lambda: None)
    async def plan(*args): return None
    monkeypatch.setattr(server, '_voice_plan_for_letter', plan)
    def speech(text, path, **kw):
        entered.set()
        assert release.wait(5)
        path.write_bytes(b'old speech')
        return {'duration_seconds': 1}
    monkeypatch.setattr(server, 'render_reply_audio', speech)
    async def run():
        task = asyncio.create_task(server._render_media_job('sample', row['content'], row['reply_text'], 'voice_reply'))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            row.update(reply_revision=2, private_world_delivery_id='sample:2', media_status='PENDING')
        finally:
            release.set()
            await task
    asyncio.run(run())
    assert row['media_status'] == 'PENDING'
    assert not row.get('reply_audio_url')
    assert not row.get('media_deliveries')


def test_startup_world_replay_tolerates_status_persist_failure(tmp_path, monkeypatch):
    import local_server as server
    row = letter()
    row['media_deliveries'] = [make_delivery(row, component='speech', presentation='audio', occurred_at=NOW)]
    row['media_world_status'] = 'PENDING'
    world = DailyLifeStore(tmp_path/'world.sqlite')
    monkeypatch.setattr(server.store, 'letters', [row])
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=world, schedule_refresh=lambda now: None))
    monkeypatch.setattr(server, '_schedule_pending_reply_jobs', lambda: None)
    monkeypatch.setattr(server, '_schedule_pending_media_jobs', lambda: None)
    calls = []
    def unavailable():
        calls.append(1)
        raise OSError('synthetic unavailable')
    monkeypatch.setattr(server, '_persist_store_state', unavailable)
    asyncio.run(server._start_reply_tasks(None))
    asyncio.run(server._start_reply_tasks(None))
    assert len(calls) == 1
    assert row['media_world_status'] == 'COMMITTED'
    assert len(world.history()['moments']) == 1


def test_failed_cover_keeps_only_published_speech_then_retry_adds_cover(tmp_path, monkeypatch):
    import local_server as server
    import runtime.media.cover_reply as covers
    import runtime.media.cover_upload as uploads
    import runtime.reply.reply_media as media
    row = letter()
    row.update(reply_mode='voice_song_video', reply_video_enabled=False, music_provider='ace_step_xl_cover')
    world = DailyLifeStore(tmp_path/'world.sqlite')
    monkeypatch.setattr(server.store, 'letters', [row])
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=world))
    monkeypatch.setattr(server, 'media_semaphore', asyncio.Semaphore(1))
    monkeypatch.setattr(server, '_local_data_root', lambda *a: tmp_path)
    monkeypatch.setattr(server, '_persist_media_state', lambda: None)
    monkeypatch.setattr(server, '_record_media_job_failure', lambda *a: None)
    monkeypatch.setattr(server, '_current_music_performance', lambda *a: None)
    async def plan(*args): return None
    monkeypatch.setattr(server, '_voice_plan_for_letter', plan)
    def speech(text, path, **kw):
        path.write_bytes(b'speech')
        return {'duration_seconds': 1}
    monkeypatch.setattr(server, 'render_reply_audio', speech)
    monkeypatch.setattr(uploads, 'source_path', lambda *a: tmp_path/'source.wav')
    calls = []
    def cover(content, text, path, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise server.MusicReplyError('MEDIA_PROVIDER_UNAVAILABLE')
        path.write_bytes(b'cover')
        return {}
    monkeypatch.setattr(covers, 'render_cover_reply', cover)
    monkeypatch.setattr(media, 'concatenate_reply_audio', lambda speech, song, output, env: output.write_bytes(b'combined'))
    for count in (1, 2, 2):
        asyncio.run(server._render_media_job('sample', row['content'], row['reply_text'], 'voice_song_video'))
        assert len(row['media_deliveries']) == count
        assert len(world.history()['moments']) == count
    assert {e['component'] for e in row['media_deliveries']} == {'speech', 'cover'}
