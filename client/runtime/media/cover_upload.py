"""Bounded local audio uploads, identified by opaque IDs rather than client paths."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import wave
import json
import os
import threading

from runtime.media.managed_subprocess import run_managed_process
from runtime.media.latentsync_reply import LatentSyncReplyError, resolve_ffmpeg_executable


MAX_UPLOAD_BYTES = 256 * 1024 * 1024
_ASR_LOCK = threading.Lock()


def recognize_lyrics(root: Path, source_id: object, environment) -> dict:
    """Transcribe one immutable upload, without loading the music generator."""
    from runtime.media.ace_cover import cover_paths
    source = source_path(root, source_id)
    cached = source.parent / 'lyrics.private.json'
    if cached.is_file():
        return json.loads(cached.read_text(encoding='utf-8'))
    if not _ASR_LOCK.acquire(blocking=False):
        raise ValueError('COVER_TRANSCRIPTION_BUSY')
    try:
        paths = cover_paths(environment)
        if not all(paths[k] and paths[k].is_file() for k in ('asr_model', 'python')):
            raise ValueError('COVER_TRANSCRIPTION_UNAVAILABLE')
        ffmpeg = resolve_ffmpeg_executable(environment)
        request = source.parent / 'transcription.private.json'
        output = source.parent / 'lyrics.partial.json'
        output.unlink(missing_ok=True)
        request.write_text(json.dumps({'operation': 'transcribe', 'source': str(source),
            'asr_model': str(paths['asr_model']), 'ffmpeg': str(ffmpeg), 'output': str(output)}), encoding='utf-8')
        env = {**os.environ, **environment, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'PYTHONIOENCODING': 'utf-8', 'OMP_NUM_THREADS': '4', 'MKL_NUM_THREADS': '4'}
        for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'CONDA_PREFIX'):
            env.pop(key, None)
        result = run_managed_process([str(paths['python']), str(Path(__file__).with_name('ace_cover_worker.py')), str(request)],
                                     timeout_seconds=600, env=env)
        if result.returncode:
            raise ValueError('COVER_TRANSCRIPTION_FAILED')
        data = json.loads(output.read_text(encoding='utf-8'))
        if not isinstance(data.get('lyrics'), str) or not 0 < len(data['lyrics'].strip()) <= 30000:
            raise ValueError('COVER_TRANSCRIPTION_FAILED')
        if not isinstance(data.get('language'), str) or not re.fullmatch(r'[a-z]{2,8}', data['language']):
            data['language'] = 'unknown'
        data = {'lyrics': data['lyrics'], 'language': data['language']}
        output.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        output.replace(cached)
        return data
    except (OSError, subprocess.TimeoutExpired, LatentSyncReplyError):
        raise ValueError('COVER_TRANSCRIPTION_FAILED') from None
    finally:
        _ASR_LOCK.release()


def source_path(root: Path, source_id: object) -> Path:
    if root is None or not isinstance(source_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_id):
        raise ValueError("COVER_SOURCE_REQUIRED")
    directory = (root / "cover-inputs").resolve()
    path = (directory / source_id / "source.wav").resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise ValueError("COVER_SOURCE_REQUIRED")
    return path


def validate_upload(raw: Path, destination: Path, environment) -> float:
    try:
        ffmpeg = resolve_ffmpeg_executable(environment)
    except LatentSyncReplyError:
        raise ValueError("COVER_FFMPEG_UNAVAILABLE") from None
    try:
        result = run_managed_process([str(ffmpeg), "-nostdin", "-v", "error", "-y",
            "-threads", "4", "-protocol_whitelist", "file,pipe", "-i", str(raw),
            "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
            "-threads", "4", str(destination)], timeout_seconds=180)
        if result.returncode:
            raise ValueError("COVER_AUDIO_INVALID")
        with wave.open(str(destination), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        if duration <= 0:
            raise ValueError("COVER_AUDIO_INVALID")
        return duration
    except (OSError, wave.Error, subprocess.TimeoutExpired):
        raise ValueError("COVER_AUDIO_INVALID") from None
    finally:
        raw.unlink(missing_ok=True)
