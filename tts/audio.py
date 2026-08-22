"""WAV writing and signal metrics without an ffmpeg/TorchCodec dependency."""

from __future__ import annotations

import math
import os
import tempfile
import wave
from pathlib import Path
from typing import Iterable, Sequence


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def write_wav(path: str | Path, sample_rate: int, chunks: Iterable[Sequence[float]]) -> int:
    """Atomically write mono PCM16 and return the frame count."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    frames = 0
    try:
        with wave.open(temporary, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(int(sample_rate))
            for chunk in chunks:
                values = bytearray()
                for sample in chunk:
                    value = int(round(_clamp(float(sample)) * 32767.0))
                    values.extend(value.to_bytes(2, byteorder="little", signed=True))
                    frames += 1
                if values:
                    handle.writeframes(bytes(values))
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return frames


def read_wav_samples(path: str | Path) -> tuple[int, list[float]]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("expected mono PCM16 WAV")
        sample_rate = int(handle.getframerate())
        raw = handle.readframes(handle.getnframes())
    samples = [int.from_bytes(raw[index : index + 2], "little", signed=True) / 32768.0 for index in range(0, len(raw), 2)]
    return sample_rate, samples


def audio_metrics(
    path: str | Path,
    *,
    silence_threshold: float = 0.002,
    truncated: bool = False,
) -> dict[str, float | int | bool | str]:
    sample_rate, samples = read_wav_samples(path)
    count = len(samples)
    peak = max((abs(value) for value in samples), default=0.0)
    clipped = sum(1 for value in samples if abs(value) >= 0.999)
    silent = sum(1 for value in samples if abs(value) <= silence_threshold)
    leading = 0
    while leading < count and abs(samples[leading]) <= silence_threshold:
        leading += 1
    trailing = 0
    while trailing < count and abs(samples[count - trailing - 1]) <= silence_threshold:
        trailing += 1
    return {
        "path": target_name(path),
        "sample_rate": sample_rate,
        "frames": count,
        "duration_seconds": count / sample_rate if sample_rate else 0.0,
        "peak_abs": round(peak, 8),
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1e-9)), 4),
        "clipped_samples": clipped,
        "silence_ratio": round(silent / count, 8) if count else 1.0,
        "leading_silence_seconds": round(leading / sample_rate, 6) if sample_rate else 0.0,
        "trailing_silence_seconds": round(trailing / sample_rate, 6) if sample_rate else 0.0,
        "has_audio": bool(count and peak > silence_threshold),
        "truncated": bool(truncated),
    }


def target_name(path: str | Path) -> str:
    return Path(path).name
