"""Local MIDI to original-compatible performance video renderer.

The renderer keeps the original client contract: a user supplied MIDI file is
turned into an MP4 that the original Olivia UI can play.  It reuses an original
Olivia presence clip for the visual layer and performs a small, deterministic
offline MIDI synthesis for the audio layer.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np


class MidiRenderError(RuntimeError):
    pass


def _vlq(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise MidiRenderError("MIDI_TRUNCATED")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    raise MidiRenderError("MIDI_INVALID_VLQ")


def _track_events(track: bytes) -> list[tuple[int, str, tuple[int, ...]]]:
    events: list[tuple[int, str, tuple[int, ...]]] = []
    offset = 0
    tick = 0
    running_status: int | None = None
    while offset < len(track):
        delta, offset = _vlq(track, offset)
        tick += delta
        if offset >= len(track):
            break
        status = track[offset]
        if status < 0x80:
            if running_status is None:
                raise MidiRenderError("MIDI_RUNNING_STATUS_MISSING")
            status = running_status
        else:
            offset += 1
            if status < 0xF0:
                running_status = status

        if status == 0xFF:
            if offset >= len(track):
                raise MidiRenderError("MIDI_META_TRUNCATED")
            meta_type = track[offset]
            offset += 1
            length, offset = _vlq(track, offset)
            payload = track[offset : offset + length]
            if len(payload) != length:
                raise MidiRenderError("MIDI_META_TRUNCATED")
            offset += length
            if meta_type == 0x51 and length == 3:
                events.append((tick, "tempo", (int.from_bytes(payload, "big"),)))
            if meta_type == 0x2F:
                break
            continue
        if status in (0xF0, 0xF7):
            length, offset = _vlq(track, offset)
            offset += length
            continue

        kind = status & 0xF0
        channel = status & 0x0F
        data_length = 1 if kind in (0xC0, 0xD0) else 2
        payload = track[offset : offset + data_length]
        if len(payload) != data_length:
            raise MidiRenderError("MIDI_EVENT_TRUNCATED")
        offset += data_length
        if kind == 0x90:
            note, velocity = payload
            events.append((tick, "on" if velocity else "off", (channel, note, velocity)))
        elif kind == 0x80:
            note, velocity = payload
            events.append((tick, "off", (channel, note, velocity)))
    return events


def _parse_notes(data: bytes) -> tuple[list[tuple[float, float, int, int]], float]:
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiRenderError("MIDI_INVALID_HEADER")
    header_length = int.from_bytes(data[4:8], "big")
    if header_length < 6 or len(data) < 8 + header_length:
        raise MidiRenderError("MIDI_INVALID_HEADER")
    _fmt, tracks, division = struct.unpack(">HHH", data[8:14])
    if division & 0x8000 or division == 0:
        raise MidiRenderError("MIDI_SMPTE_UNSUPPORTED")
    offset = 8 + header_length
    events: list[tuple[int, str, tuple[int, ...]]] = []
    for _ in range(tracks):
        if data[offset : offset + 4] != b"MTrk" or offset + 8 > len(data):
            raise MidiRenderError("MIDI_TRACK_MISSING")
        length = int.from_bytes(data[offset + 4 : offset + 8], "big")
        start = offset + 8
        end = start + length
        if end > len(data):
            raise MidiRenderError("MIDI_TRACK_TRUNCATED")
        events.extend(_track_events(data[start:end]))
        offset = end

    priority = {"tempo": 0, "off": 1, "on": 2}
    events.sort(key=lambda item: (item[0], priority[item[1]]))
    tempo = 500_000
    last_tick = 0
    seconds = 0.0
    active: dict[tuple[int, int], list[tuple[float, int]]] = {}
    notes: list[tuple[float, float, int, int]] = []
    for tick, kind, values in events:
        seconds += (tick - last_tick) * tempo / 1_000_000 / division
        last_tick = tick
        if kind == "tempo":
            tempo = values[0]
            continue
        channel, note, velocity = values
        key = (channel, note)
        if kind == "on":
            active.setdefault(key, []).append((seconds, velocity))
        elif active.get(key):
            start, start_velocity = active[key].pop(0)
            notes.append((start, max(seconds, start + 0.04), note, start_velocity))

    end_time = seconds
    for (_channel, note), starts in active.items():
        for start, velocity in starts:
            notes.append((start, max(end_time, start + 0.25), note, velocity))
    if not notes:
        raise MidiRenderError("MIDI_HAS_NO_NOTES")
    duration = min(max(end for _, end, _, _ in notes) + 0.4, 300.0)
    return [note for note in notes if note[0] < duration], duration


def synthesize_midi(midi_path: Path, wav_path: Path, *, sample_rate: int = 44_100) -> float:
    notes, duration = _parse_notes(midi_path.read_bytes())
    samples = np.zeros(int(math.ceil(duration * sample_rate)), dtype=np.float32)
    for start, end, note, velocity in notes:
        start_index = int(start * sample_rate)
        end_index = min(len(samples), int((end + 0.25) * sample_rate))
        if end_index <= start_index:
            continue
        t = np.arange(end_index - start_index, dtype=np.float32) / sample_rate
        frequency = 440.0 * (2.0 ** ((note - 69) / 12.0))
        held = max(end - start, 0.04)
        attack = np.minimum(t / 0.012, 1.0)
        release = np.where(t <= held, 1.0, np.maximum(0.0, 1.0 - (t - held) / 0.25))
        decay = np.exp(-1.1 * t)
        tone = (
            np.sin(2 * np.pi * frequency * t)
            + 0.35 * np.sin(4 * np.pi * frequency * t)
            + 0.12 * np.sin(6 * np.pi * frequency * t)
        )
        samples[start_index:end_index] += tone * attack * release * decay * (velocity / 127.0)
    peak = float(np.max(np.abs(samples)))
    if peak > 0:
        samples *= 0.82 / peak
    pcm = np.asarray(samples * 32767, dtype="<i2")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return duration


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise MidiRenderError("FFMPEG_UNAVAILABLE") from exc


def render_performance_video(midi_path: Path, output_path: Path, background_path: Path) -> float:
    if not background_path.is_file():
        raise MidiRenderError("ORIGINAL_VISUAL_UNAVAILABLE")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path = output_path.with_suffix(".wav")
    duration = synthesize_midi(midi_path, wav_path)
    temporary = output_path.with_suffix(".rendering.mp4")
    command = [
        _ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(background_path),
        "-i",
        str(wav_path),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=180, check=False)
        if completed.returncode != 0 or not temporary.is_file():
            raise MidiRenderError("MIDI_VIDEO_RENDER_FAILED")
        temporary.replace(output_path)
    finally:
        wav_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    return duration
