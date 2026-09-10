from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest
from tools import latentsync_diagnostic_worker as worker


def test_decode_and_mux_preserve_paths_as_single_arguments(tmp_path, monkeypatch):
    calls = []
    util = SimpleNamespace(read_video_cv2=lambda _: [1, 2], read_video_decord=lambda _: [1, 2])
    monkeypatch.setitem(sys.modules, 'latentsync', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'latentsync.utils', SimpleNamespace(util=util))
    monkeypatch.setattr(worker.subprocess, 'run', lambda args, **kwargs: calls.append((args, kwargs)))
    pipeline = SimpleNamespace()
    temp = str(tmp_path / '中文 temp')
    output = str(tmp_path / 'output space.mp4')
    worker.install_path_safe_video_io(pipeline, temp_dir=temp, output_path=output)
    source = str(tmp_path / 'source space.mp4')
    assert pipeline.read_video(source, use_decord=False) == [1, 2]
    assert calls[0][0][calls[0][0].index('-i')+1] == source
    command = f"ffmpeg -y -loglevel error -nostdin -i {str(Path(temp) / 'video.mp4')} -i {str(Path(temp) / 'audio.wav')} -c:v libx264 -crf 18 -c:a aac -q:v 0 -q:a 0 {output}"
    pipeline.subprocess.run(command, shell=True)
    assert calls[1][0][-1] == output
    assert str(Path(temp) / 'audio.wav') in calls[1][0]
    assert all(isinstance(args, list) and kwargs == {'check': True} for args, kwargs in calls)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(worker.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.read_video(source)


def test_decoded_empty_video_fails_at_input_boundary(tmp_path, monkeypatch):
    util = SimpleNamespace(read_video_cv2=lambda _: [])
    monkeypatch.setitem(sys.modules, 'latentsync', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'latentsync.utils', SimpleNamespace(util=util))
    pipeline = SimpleNamespace()
    worker.install_path_safe_video_io(pipeline, temp_dir=str(tmp_path), output_path=str(tmp_path/'out.mp4'))
    with pytest.raises(RuntimeError, match='LATENTSYNC_VIDEO_DECODE_EMPTY'):
        pipeline.read_video('fixture.mp4', change_fps=False, use_decord=False)
