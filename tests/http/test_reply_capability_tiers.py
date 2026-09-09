import asyncio

import pytest

from runtime.video_reply_settings import VideoReplySettingsStore, REPLY_ROUTES, routed_video
from letter_triage import TriageResult, restrict_reply_route


@pytest.mark.parametrize('mode,video,bounded', [
    ('voice_reply',False,False), ('voice_reply',True,True),
    ('voice_song_video',False,False), ('voice_song_video',True,True),
    ('spoken_video',True,True), ('text_letter',False,False),
])
def test_only_video_speech_gets_duration_constraint(monkeypatch, mode, video, bounded):
    from types import SimpleNamespace
    import local_server as server
    seen = []
    async def run(request, context):
        seen.append(request.content)
    monkeypatch.setattr(server, 'reply_pipeline', SimpleNamespace(run=run))
    asyncio.run(server._run_reply_pipeline_for_letter(
        {'letter_id':'duration-check', 'reply_video_enabled':video},
        '请详细回答。', mode, idempotency_key=None))
    assert ('40到50秒' in seen[0]) is bounded
    if not bounded:
        assert seen[0] == '请详细回答。'


@pytest.mark.parametrize('tier', ['text', 'audio', 'video'])
def test_tier_is_a_capability_ceiling_and_persists(tmp_path, tier):
    store = VideoReplySettingsStore.initialize(tmp_path)
    store.mutate_tier('video_reply_setting:tier', tier)
    restored = VideoReplySettingsStore(tmp_path)
    assert restored.saved_tier() == tier
    assert restored.routes_snapshot() == dict.fromkeys(REPLY_ROUTES, tier != 'text')
    assert restored.videos_snapshot() == dict.fromkeys(REPLY_ROUTES, tier == 'video')
    text = TriageResult('normal', 'text_letter', 'synthetic', 'completed', False)
    assert restrict_reply_route(text, restored.routes_snapshot()).reply_mode == 'text_letter'


def test_video_tier_does_not_force_video_for_voice_or_explicit_audio():
    allowed = dict.fromkeys(REPLY_ROUTES, True)
    assert not routed_video('text_letter', (), allowed)
    assert not routed_video('voice_reply', ('explicit_voice_reply_request',), allowed)
    assert routed_video('voice_reply', ('explicit_video_output_request',), allowed)
    assert routed_video('singing_video', (), allowed)
    assert not routed_video('singing_video', ('explicit_audio_output_request',), allowed)
    assert not routed_video('voice_reply', ('explicit_video_output_request',), dict.fromkeys(REPLY_ROUTES, False))


def test_http_tier_save_does_not_select_a_reply_mode(tmp_path, monkeypatch):
    import local_server as server
    store = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(server, 'video_reply_settings_store', store)
    monkeypatch.setattr(server, '_route_readiness', lambda *a: dict.fromkeys(REPLY_ROUTES, True))
    async def run():
        saved = await server.route('POST', '/toy/settings/reply-routes', {'request_id':'video_reply_setting:test','tier':'audio'}, {})
        assert saved['code'] == 0
        assert saved['data']['tier'] == 'audio'
        assert all(saved['data']['routes'].values())
        assert not any(saved['data']['videos'].values())
        read = await server.route('GET', '/toy/settings/reply-routes', {}, {})
        assert read['data']['tier'] == 'audio'
    asyncio.run(run())


@pytest.mark.parametrize('tier', ['text', 'audio', 'video'])
@pytest.mark.parametrize('mode,contexts', [
    ('text_letter', ()),
    ('voice_reply', ()),
    ('singing_video', ()),
    ('voice_song_video', ()),
    ('voice_reply', ('explicit_voice_reply_request',)),
    ('voice_reply', ('explicit_video_output_request', 'explicit_voice_reply_request')),
    ('singing_video', ('explicit_performance_or_adaptation_request', 'explicit_audio_output_request')),
    ('voice_song_video', ('explicit_voice_and_song_request', 'explicit_video_output_request')),
])
def test_tier_preview_send_matrix(tmp_path, monkeypatch, tier, mode, contexts):
    import wave
    from types import SimpleNamespace
    import local_server as server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate_tier('video_reply_setting:matrix', tier)
    monkeypatch.setattr(server, 'video_reply_settings_store', settings)
    monkeypatch.setattr(server.store, 'letters', [])
    monkeypatch.setattr(server.store, 'request_keys', {})
    monkeypatch.setattr(server, '_reply_route_previews', {})
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    monkeypatch.setattr(server, '_schedule_reply_job', lambda *a, **k: None)
    monkeypatch.setattr(server, '_video_reply_dependencies_ready', lambda: True)
    monkeypatch.setattr(server, '_route_readiness', lambda *a: dict.fromkeys(REPLY_ROUTES, True))
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    source_id = 'a' * 32
    audio = tmp_path / 'cover-inputs' / source_id / 'source.wav'
    audio.parent.mkdir(parents=True)
    with wave.open(str(audio), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 1600)
    calls = []
    async def classify(content):
        calls.append(content)
        return TriageResult('normal', mode, 'synthetic', 'completed', mode != 'text_letter', contexts)
    monkeypatch.setattr(server, 'emotion_triage', SimpleNamespace(classify=classify))
    async def run():
        preview = (await server.route('POST', '/toy/letter/route-preview', {'content':'验收信件'}, {}))['data']
        material = {'route_preview_token':preview['token']}
        body = {'content':'验收信件', 'material':material}
        if preview['needs_confirmation'] or preview['needs_video_confirmation']:
            blocked = await server.route('POST', '/toy/letter/send', body, {}, defer_reply=True)
            assert blocked['code'] == 409
            assert not server.store.letters
            if preview['needs_confirmation']: material['route_allow_once'] = mode
            if preview['needs_video_confirmation']: material['route_video_once'] = mode
        expected_mode = 'text_letter' if tier == 'text' and not contexts else mode
        if expected_mode in ('singing_video', 'voice_song_video'):
            material.update(cover_source_id=source_id, cover_lyrics='验收歌词')
        accepted = await server.route('POST', '/toy/letter/send', body, {}, defer_reply=True)
        assert accepted['code'] == 0, accepted
        letter = server.store.letters[0]
        assert letter['route_preflight']['reply_mode'] == expected_mode
        assert letter['reply_capability_tier'] == tier
        actual_video = routed_video(expected_mode, contexts, letter['reply_route_videos'])
        expected_video = expected_mode != 'text_letter' and (
            'explicit_video_output_request' in contexts or
            (tier == 'video' and mode in ('singing_video', 'voice_song_video') and 'explicit_audio_output_request' not in contexts))
        assert actual_video == expected_video
        assert settings.saved_tier() == tier
        assert settings.routes_snapshot() == dict.fromkeys(REPLY_ROUTES, tier != 'text')
        assert settings.videos_snapshot() == dict.fromkeys(REPLY_ROUTES, tier == 'video')
        assert len(calls) == 1
    asyncio.run(run())
