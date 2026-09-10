import asyncio
import json
import subprocess
import wave
from pathlib import Path

import pytest

from runtime.media import cover_upload
from runtime.media.local_song_library import LocalSongLibrary
from runtime.video_reply_settings import VideoReplySettingsStore, REPLY_ROUTES


def audio_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b'\0\0' * 1600)


def test_lyrics_recognition_is_separate_cached_and_portable(tmp_path, monkeypatch):
    from runtime.media import ace_cover
    source_id = 'a' * 32
    audio_file(tmp_path / 'cover-inputs' / source_id / 'source.wav')
    paths = {'python': tmp_path / 'runtime/python.exe', 'asr_model': tmp_path / 'model.pt'}
    for path in paths.values():
        path.parent.mkdir(exist_ok=True); path.write_bytes(b'fixture')
    monkeypatch.setattr(ace_cover, 'cover_paths', lambda _: paths)
    monkeypatch.setattr(cover_upload, 'resolve_ffmpeg_executable', lambda _: tmp_path / 'ffmpeg.exe')
    calls = []
    def worker(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        assert request['operation'] == 'transcribe'
        assert 'voice_lora' not in request
        assert kwargs['env']['HF_HUB_OFFLINE'] == '1'
        Path(request['output']).write_text(json.dumps({'lyrics': '自动识别歌词', 'language': 'zh'}), encoding='utf-8')
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(cover_upload, 'run_managed_process', worker)
    for _ in range(2):
        assert cover_upload.recognize_lyrics(tmp_path, source_id, {}) == {'lyrics': '自动识别歌词', 'language': 'zh'}
    assert len(calls) == 1


@pytest.mark.parametrize('tier', ['text', 'audio', 'video'])
@pytest.mark.parametrize('output', ['audio', 'video'])
def test_attached_cover_preflight_keeps_text_and_confirmed_mode(tmp_path, monkeypatch, tier, output):
    import local_server as server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate_tier('video_reply_setting:cover', tier)
    monkeypatch.setattr(server, 'video_reply_settings_store', settings)
    monkeypatch.setattr(server.store, 'letters', [])
    monkeypatch.setattr(server.store, 'request_keys', {})
    monkeypatch.setattr(server, '_reply_route_previews', {})
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    monkeypatch.setattr(server, '_schedule_reply_job', lambda *a, **k: None)
    monkeypatch.setattr(server, '_video_reply_dependencies_ready', lambda: True)
    # A cover installation must not require the separate original-song LoRA.
    monkeypatch.setattr(server, '_route_readiness', lambda *a, **kw: dict.fromkeys(REPLY_ROUTES, kw.get('cover', False)))
    monkeypatch.setattr(server, '_classify_managed_route', lambda *a: pytest.fail('explicit attachment needs no model guess'))
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    source_id = 'a' * 32
    audio_file(tmp_path / 'cover-inputs' / source_id / 'source.wav')
    async def run():
        preview = await server.route('POST', '/toy/letter/route-preview', {'content': '今天想听这首歌', 'cover_source_id': source_id, 'cover_output': output}, {})
        assert preview['code'] == 0, preview
        preview = preview['data']
        assert preview['requested_route'] == 'singing_video'
        assert preview['video_enabled'] == (output == 'video')
        material = {'cover_source_id': source_id, 'cover_output': output, 'cover_lyrics': '人工修正后的歌词', 'route_preview_token': preview['token']}
        if preview['needs_confirmation']: material['route_allow_once'] = 'singing_video'
        if preview['needs_video_confirmation']: material['route_video_once'] = 'singing_video'
        bad = await server.route('POST', '/toy/letter/send', {'content': '今天想听这首歌', 'material': {**material, 'cover_output': 'video' if output == 'audio' else 'audio'}}, {}, defer_reply=True)
        assert bad['code'] == 409
        assert not server.store.letters
        accepted = await server.route('POST', '/toy/letter/send', {'content': '今天想听这首歌', 'material': material}, {}, defer_reply=True)
        assert accepted['code'] == 0, accepted
        saved = server.store.letters[0]
        assert saved['content'] == '今天想听这首歌'
        assert saved['material']['cover_lyrics'] == '人工修正后的歌词'
        assert saved['route_preflight']['reply_mode'] == 'singing_video'
        assert settings.saved_tier() == tier
    asyncio.run(run())


def test_collect_letter_audio_is_idempotent_and_survives_original_removal(tmp_path):
    source = tmp_path / 'media/letter.wav'
    audio_file(source)
    library = LocalSongLibrary(tmp_path, {})
    first = library.import_audio(source, '林离的翻唱')
    assert first['added'] is True
    assert library.import_audio(source, '重复收藏')['added'] is False
    owned = library.media_path(first['id'])
    assert owned.read_bytes() == source.read_bytes()
    source.unlink()
    assert owned.is_file()
    assert library.songs()[0]['media_type'] == 'audio'
    library.delete(first['id'])
    assert not owned.exists()


def test_http_collect_and_range_playback(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    import local_server as server
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    audio_file(tmp_path / 'media/test-letter.wav')
    monkeypatch.setattr(server.store, 'letters', [{'letter_id':'test-letter', 'media_status':'COMPLETED',
        'reply_audio_url':'http://127.0.0.1:8899/toy/media/test-letter.wav'}])
    async def run():
        refused = await server.route('POST','/toy/local-songs/from-letter',{'letter_id':'test-letter'}, {})
        assert refused['code'] == 403
        saved = await server.route('POST','/toy/local-songs/from-letter',{'letter_id':'test-letter'}, {}, companion_confirmed=True)
        assert saved['code'] == 0, saved
        app = web.Application(); app.router.add_route('*','/{tail:.*}',server.handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/toy/local-songs/media/'+saved['data']['id']+'.wav', headers={'Range':'bytes=0-43'})
            assert response.status == 206
            assert response.content_type == 'audio/wav'
            assert len(await response.read()) == 44
    asyncio.run(run())
