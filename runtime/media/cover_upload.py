"""Bounded local audio uploads, identified by opaque IDs rather than client paths."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import wave

from runtime.media.managed_subprocess import run_managed_process
from runtime.media.latentsync_reply import LatentSyncReplyError, resolve_ffmpeg_executable


MAX_UPLOAD_BYTES = 256 * 1024 * 1024


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
