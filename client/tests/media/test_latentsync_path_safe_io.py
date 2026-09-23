from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest
from tools import latentsync_diagnostic_worker as worker


def test_decode_and_mux_preserve_paths_as_single_arguments(tmp_path, monkeypatch):
    calls = []
    util = SimpleNamespace(read_video_cv2=lambda _: [1, 2], read_video_decord=lambda _: [1, 2], write_video=lambda *a: None)
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
    util = SimpleNamespace(read_video_cv2=lambda _: [], write_video=lambda *a: None)
    monkeypatch.setitem(sys.modules, 'latentsync', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'latentsync.utils', SimpleNamespace(util=util))
    pipeline = SimpleNamespace()
    worker.install_path_safe_video_io(pipeline, temp_dir=str(tmp_path), output_path=str(tmp_path/'out.mp4'))
    with pytest.raises(RuntimeError, match='LATENTSYNC_VIDEO_DECODE_EMPTY'):
        pipeline.read_video('fixture.mp4', change_fps=False, use_decord=False)


def test_scene_cache_reuses_content_across_paths_and_invalidates_changed_bytes(tmp_path, monkeypatch):
    calls = []
    util = SimpleNamespace(read_video_cv2=lambda p: Path(p).read_bytes())
    monkeypatch.setitem(sys.modules, 'latentsync', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'latentsync.utils', SimpleNamespace(util=util))
    def convert(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(Path(args[args.index('-i') + 1]).read_bytes())
    monkeypatch.setattr(worker.subprocess, 'run', convert)
    cache = tmp_path / 'cache'
    pipeline = SimpleNamespace()
    worker.install_path_safe_video_io(pipeline, temp_dir=str(tmp_path/'tmp'),
        output_path=str(tmp_path/'out.mp4'), cache_dir=str(cache))
    a, b = tmp_path/'a.mp4', tmp_path/'b.mp4'
    a.write_bytes(b'first scene');b.write_bytes(a.read_bytes())
    assert pipeline.read_video(a, use_decord=False) == b'first scene'
    assert pipeline.read_video(b, use_decord=False) == b'first scene'
    assert len(calls) == 1
    a.write_bytes(b'new scene')
    assert pipeline.read_video(a, use_decord=False) == b'new scene'
    assert len(calls) == 2 and len(list(cache.glob('fps25-crf18-*.mp4'))) == 1
    b.write_bytes(b'failed scene')
    def fail(args, **kwargs):
        Path(args[-1]).write_bytes(b'partial')
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(worker.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):pipeline.read_video(b, use_decord=False)
    assert pipeline.read_video(a, use_decord=False) == b'new scene'
    assert len(list(cache.glob('fps25-crf18-*.mp4'))) == 1


def test_optimized_mux_copies_h264_without_reencoding(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, 'latentsync', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'latentsync.utils', SimpleNamespace(util=SimpleNamespace()))
    monkeypatch.setattr(worker.subprocess, 'run', lambda args, **kw: calls.append(args))
    pipeline = SimpleNamespace()
    temp, output = str(tmp_path/'tmp'), str(tmp_path/'out.mp4')
    worker.install_path_safe_video_io(pipeline, temp_dir=temp, output_path=output, cache_dir=str(tmp_path/'cache'))
    assert pipeline.write_video is worker.write_h264_video
    command = f"ffmpeg -y -loglevel error -nostdin -i {str(Path(temp) / 'video.mp4')} -i {str(Path(temp) / 'audio.wav')} -c:v libx264 -crf 18 -c:a aac -q:v 0 -q:a 0 {output}"
    pipeline.subprocess.run(command, shell=True)
    assert calls[0][calls[0].index('-c:v')+1] == 'copy'
    assert '-crf' not in calls[0]
    with pytest.raises(RuntimeError):pipeline.subprocess.run(command+';unexpected', shell=True)


def test_h264_writer_real_encode_decode_preserves_frame_count_size_and_rgb(tmp_path, monkeypatch):
    import numpy as np
    from imageio_ffmpeg import get_ffmpeg_exe
    executable = get_ffmpeg_exe()
    original = subprocess.Popen
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda args, **kw: original([executable, *args[1:]], **kw))
    frames = np.zeros((8, 24, 32, 3), dtype=np.uint8)
    frames[:] = [240, 20, 20]
    output = tmp_path/'encoded video.mp4'
    worker.write_h264_video(output, frames, 25)
    decoded = subprocess.run([executable, '-v', 'error', '-i', str(output), '-f', 'rawvideo',
        '-pix_fmt', 'rgb24', '-'], check=True, capture_output=True).stdout
    actual = np.frombuffer(decoded, dtype=np.uint8).reshape(frames.shape)
    assert np.max(np.abs(actual.astype(int) - frames.astype(int))) <= 4
