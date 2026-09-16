from pathlib import Path

import pytest
from runtime import remote_pipeline
from runtime.media.voice_direction import TextOnlyVoicePlan


@pytest.mark.parametrize('no_scene', [True, False])
def test_remote_stages_do_not_require_local_models(tmp_path, monkeypatch, no_scene):
    from runtime.reply.reply_media import render_reply_audio, render_reply_video
    from runtime.media.ace_cover import generate_cover
    from runtime.media.latentsync_reply import render_latentsync_video
    calls = []
    def remote(kind, data, output, **kwargs):
        calls.append((kind, data, kwargs.get('assets', {})))
        return {'duration_seconds': 3}
    monkeypatch.setattr(remote_pipeline, 'generate', remote)
    env = {'OLIVIA_GPU_ROUTE': 'remote'}
    missing = tmp_path / 'no-model'
    output = tmp_path / 'result.wav'
    render_reply_audio('test', output, tts_config_path=missing,
                       voice_performance_plan=TextOnlyVoicePlan('test'), environment=env)
    render_reply_video('test', output, tts_config_path=missing, visual_config_path=missing, worker_path=missing,
                       scene_path=None if no_scene else missing, environment=env, adaptive_delivery=True)
    source = tmp_path / 'source.wav'; source.write_bytes(b'fixture')
    generate_cover(source, output, environment=env)
    render_latentsync_video(missing, source, output, python_path=None, latentsync_root=None, environment=env)
    assert [c[0] for c in calls] == ['tts', 'video', 'cover', 'lipsync']
    assert calls[1][1]['adaptive_delivery'] is True
    assert calls[1][1]['scene_asset'] == 'official-reply-action-base-v1'
    assert calls[3][1]['scene_asset'] == 'official-performance-lipsync-safe-2950f-v1'
    assert 'scene_asset' not in calls[1][2]
    assert 'scene_asset' not in calls[3][2]


def test_remote_readiness_uses_server_capabilities(monkeypatch):
    import local_server
    monkeypatch.setenv('OLIVIA_GPU_ROUTE', 'remote')
    monkeypatch.setattr(remote_pipeline, 'capabilities', lambda _: {'kinds': ['tts', 'video', 'original', 'lipsync']})
    videos = {'voice_reply': True, 'singing_video': True, 'voice_song_video': True}
    assert all(local_server._route_readiness(videos).values())
    assert local_server._route_readiness(videos, cover=True)['singing_video'] is False
    monkeypatch.setattr(remote_pipeline, 'capabilities', lambda _: {'kinds': []})
    assert not any(local_server._route_readiness(videos).values())


def test_shared_manifest_rejects_escape(tmp_path):
    from tools.gpu_shared_manifest import manifest
    root = tmp_path / 'assets'; root.mkdir()
    (root / 'voice.wav').write_bytes(b'reference')
    assert manifest(root, ['voice-v1=voice.wav'])['voice-v1']['sha256']
    outside = tmp_path / 'outside.wav'; outside.write_bytes(b'private')
    with pytest.raises(ValueError): manifest(root, ['voice=../outside.wav'])
    with pytest.raises(ValueError): manifest(root, ['same=voice.wav', 'same=voice.wav'])
