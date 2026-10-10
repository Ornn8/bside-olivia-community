import json
from types import SimpleNamespace

import pytest

from runtime.media import original_song
from runtime.media.song_content import _planner_contract, _plan_from_lyrics_response

LIMITS = {'lyrics': 3000, 'style': 120, 'duration_control': False}


def test_native_style_contract_keeps_full_song_and_rejects_overflow():
    contract = _planner_contract(240, style_max_chars=120)
    assert 'at most 120 characters' in contract and '40 original' in contract
    assert 'at most 1000 characters' in _planner_contract(240)
    data = {'verse': ['窗外的风轻轻吹'] * 20, 'chorus': ['把这首歌唱给你'] * 20, 'style': 'x' * 120}
    assert _plan_from_lyrics_response(json.dumps(data), 240, style_max_chars=120).duration_seconds == 240
    data['style'] += 'x'
    with pytest.raises(ValueError, match='SONG_STYLE_INVALID'):
        _plan_from_lyrics_response(json.dumps(data), 240, style_max_chars=120)
    assert _plan_from_lyrics_response(json.dumps(data), 240).duration_seconds == 240


def test_native_planner_delivers_bound_to_actual_gateway_messages():
    from runtime.media.song_content import plan_song_content
    from tests.media.test_song_content_pipeline import RecordingGateway
    data = {'verse': ['窗外的风轻轻吹'] * 20, 'chorus': ['把这首歌唱给你'] * 20,
            'style': 'Warm female vocals, piano, jazz bass, calm resolving ending'}
    gateway = RecordingGateway(json.dumps(data))
    plan = plan_song_content('synthetic letter', 'synthetic reply', 240, gateway=gateway, style_max_chars=120)
    assert len(gateway.calls) == 1 and plan.suno_style == data['style']
    messages = '\n'.join(message['content'] for message in gateway.calls[0][0])
    assert 'at most 120 characters' in messages and '40 original' in messages
    assert 'at most 1000 characters' not in messages


def test_native_fallback_passes_style_bound_and_keeps_user_style(tmp_path, monkeypatch):
    from runtime import remote_pipeline
    monkeypatch.setattr(remote_pipeline, 'capabilities', lambda _: {
        'original_music_provider': 'suno_v6', 'original_music_input_limits': LIMITS})
    planned, sent = [], []
    def planner(content, reply, duration, **options):
        planned.append((duration, options))
        return SimpleNamespace(lyrics='synthetic 40-line plan', suno_style='planned style')
    monkeypatch.setattr(original_song, 'plan_song_content', planner)
    monkeypatch.setattr(original_song, 'cached_song_plan', lambda path, content, reply, duration, create, **options: create())
    monkeypatch.setattr(remote_pipeline, 'generate', lambda kind, data, output, **kw: sent.append(data) or {})
    env = {'OLIVIA_GPU_ROUTE': 'remote', 'OLIVIA_ORIGINAL_MUSIC_OPTIONS': json.dumps({'caption': 'user jazz style'})}
    original_song.render_original_reply('letter', 'reply', tmp_path / 'song.wav', environment=env, render_video=False)
    assert planned == [(240, {'style_max_chars': 120})]
    assert sent[0]['music_options']['caption'] == 'user jazz style'


@pytest.mark.parametrize('saved,code', [({'duration': 210}, 'MUSIC_DURATION_UNSUPPORTED'),
                                       ({'caption': 'x' * 121}, 'MUSIC_STYLE_TOO_LONG')])
def test_native_saved_conflict_stops_before_planning_or_generation(tmp_path, monkeypatch, saved, code):
    from runtime import remote_pipeline
    monkeypatch.setattr(remote_pipeline, 'capabilities', lambda _: {
        'server_media_planning': True, 'original_music_provider': 'suno_v6', 'original_music_input_limits': LIMITS})
    monkeypatch.setattr(remote_pipeline, 'generate', lambda *a, **kw: pytest.fail('must not submit'))
    monkeypatch.setattr(original_song, 'plan_song_content', lambda *a, **kw: pytest.fail('must not plan'))
    env = {'OLIVIA_GPU_ROUTE': 'remote', 'OLIVIA_ORIGINAL_MUSIC_OPTIONS': json.dumps(saved)}
    with pytest.raises(original_song.MusicReplyError, match=code):
        original_song.render_original_reply('letter', 'reply', tmp_path / 'song.wav', environment=env, render_video=False)
    assert json.loads(env['OLIVIA_ORIGINAL_MUSIC_OPTIONS']) == saved


def test_native_server_planning_preserves_user_style(tmp_path, monkeypatch):
    from runtime import remote_pipeline
    monkeypatch.setattr(remote_pipeline, 'capabilities', lambda _: {
        'server_media_planning': True, 'original_music_provider': 'suno_v6', 'original_music_input_limits': LIMITS})
    sent = []
    monkeypatch.setattr(remote_pipeline, 'generate', lambda kind, data, output, **kw: sent.append(data) or {})
    env = {'OLIVIA_GPU_ROUTE': 'remote', 'OLIVIA_ORIGINAL_MUSIC_OPTIONS': json.dumps({'caption': 'user jazz style'})}
    original_song.render_original_reply('letter', 'reply', tmp_path / 'song.wav', environment=env, render_video=False)
    assert sent[0]['music_options']['caption'] == 'user jazz style'
    assert sent[0]['media_request']['duration_seconds'] == 240


def test_native_cache_does_not_reuse_legacy_oversized_style(tmp_path):
    from runtime.media.song_plan_cache import cached_song_plan
    from runtime.media.song_content import SongContentPlan
    from runtime.media.music_caption import render_minimax_caption
    data = {'verse': ['窗外的风轻轻吹'] * 20, 'chorus': ['把这首歌唱给你'] * 20, 'style': 'x' * 121}
    semantic = _plan_from_lyrics_response(json.dumps(data), 240)
    def plan(style):
        return SongContentPlan(semantic.emotion_arc.value, semantic.lyrics,
                               render_minimax_caption(semantic), 240, semantic, style)
    path = tmp_path / 'plan.json'
    assert cached_song_plan(path, 'letter', 'reply', 240, lambda: plan(data['style'])).suno_style == data['style']
    assert cached_song_plan(path, 'letter', 'reply', 240, lambda: plan('new bounded style'), style_max_chars=120).suno_style == 'new bounded style'
    assert cached_song_plan(path, 'letter', 'reply', 240, lambda: pytest.fail('must reuse'), style_max_chars=120).suno_style == 'new bounded style'
