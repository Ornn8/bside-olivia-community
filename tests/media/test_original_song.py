import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.media import ace_cover, original_song, cover_reply


def test_original_audio_has_no_source_and_preserves_approved_parameters(tmp_path, monkeypatch):
    paths = ace_cover.cover_paths({'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)})
    calls = []
    monkeypatch.setattr(original_song, 'original_configured', lambda _: True)
    monkeypatch.setattr(original_song, 'original_paths', lambda _: paths)
    monkeypatch.setattr(original_song, 'cached_song_plan', lambda *a: SimpleNamespace(lyrics='synthetic original lyrics'))

    def run(command, **kwargs):
        request_file = Path(command[-1])
        request = json.loads(request_file.read_text(encoding='utf8'))
        calls.append(request)
        Path(request['output']).write_bytes(b'generated music')
        (request_file.parent / 'progress.json').write_text('{"duration_seconds":110}')
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(ace_cover, 'run_managed_process', run)
    output = tmp_path / 'song.wav'
    result = original_song.render_original_reply('letter', 'reply', output, environment={}, render_video=False)
    assert result['task_type'] == 'text2music'
    request = calls[0]
    assert request['source'] is None
    assert (request['duration'], request['bpm'], request['keyscale'], request['timesignature']) == (110, 68, 'Bb major', '4')
    assert request['caption'] == original_song.CAPTION
    assert '68 BPM' not in request['caption']
    assert request['reference'] == str(paths['reference'])
    original_song.render_original_reply('letter', 'reply', output, environment={}, render_video=False)
    assert len(calls) == 1


def test_original_video_uses_full_mix_without_separator_or_voice_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(original_song, 'original_configured', lambda _: True)
    monkeypatch.setattr(original_song, 'cached_song_plan', lambda *a: SimpleNamespace(lyrics='synthetic'))
    monkeypatch.setattr(original_song, 'generate_ace', lambda *a, **k: {'duration_seconds': 110})
    monkeypatch.setattr(cover_reply, 'separate_vocals', lambda *a, **k: pytest.fail('no separation'))
    seen = []
    def face(scene, vocals, mix, output, **kwargs):
        assert vocals == mix
        seen.append('song video')
        output.write_bytes(b'video')
    monkeypatch.setattr(cover_reply, 'render_full_face_performance', face)
    monkeypatch.setattr(cover_reply, 'render_reply_video', lambda *a, **k: seen.append('speech video'))
    monkeypatch.setattr(cover_reply, 'concat_videos', lambda *a, **k: seen.append('join'))
    scene = tmp_path / 'scene.mp4'
    scene.touch()
    original_song.render_original_reply('letter', 'reply', tmp_path/'out.mp4', environment={},
        normal_video_path=tmp_path/'speech.mp4', song_video_path=tmp_path/'song.mp4',
        official_reply_reference_path=scene, performance_video_path=scene,
        tts_config_path=scene, visual_config_path=scene, worker_path=scene, spoken_action_base_path=scene)
    assert seen == ['song video', 'speech video', 'join']


def test_original_never_falls_back_to_cover_voice(tmp_path):
    paths = original_song.original_paths({'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)})
    assert paths['voice_lora'] is None
    assert not original_song.original_configured({'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path)})
