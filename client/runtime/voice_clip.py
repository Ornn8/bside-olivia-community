"""Cutting a long voice message of his (a PCM WAV, what NapCat's get_record gives) into pieces, blocking.

Two users, both about a recording longer than the one minute the hearing service and the sing-back engine take
(2026-11-11 00:05: a 76 s sung voice message was refused whole and she said she could not hear it):

  parts()    the pieces it is HEARD in: each under a minute, each cut at the quietest breath near where it would fall, so a
             word is not cut in two (incoming_media.hear_voice).
  passage()  the stretch of it that is SUNG BACK when it is longer than the engine takes: the clearest one, made of whole
             phrases (her_songs.order). cut() writes it.

numpy is used when it is installed (the hosts have it); without it the pieces are cut at equal distances and the passage is
the first stretch that fits.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
import wave

PART_SECONDS = 44.0        # a piece is about this long ...
SNAP_FRACTION = 0.15       # ... and its two ends move at most this much of it to the quietest place, so at most 1.3 x
PIECE_BYTES = 2_800_000    # a piece stays well inside the hearing service's 4 MB
FRAME_SECONDS = 0.05
PHRASE_GAP_SECONDS = 0.5   # this much quiet ends a phrase
LEAD_SECONDS, TAIL_SECONDS = 0.25, 0.35


def _numpy():
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def read(path):
    """(PCM frames as bytes, rate, channels, sample width): a plain PCM WAV or ValueError('MEDIA_DURATION_INVALID')."""
    try:
        with wave.open(str(path), 'rb') as audio:
            rate, channels, width = audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
            if (audio.getcomptype() != 'NONE' or channels not in (1, 2) or width not in (1, 2, 3, 4)
                    or not 8000 <= rate <= 48000 or not audio.getnframes()):
                raise ValueError('MEDIA_DURATION_INVALID')
            return audio.readframes(audio.getnframes()), rate, channels, width
    except (wave.Error, EOFError, OSError):
        raise ValueError('MEDIA_DURATION_INVALID') from None


def seconds(path) -> float:
    """The length of a PCM WAV from its header; ValueError('MEDIA_DURATION_INVALID') for anything else."""
    try:
        with wave.open(str(path), 'rb') as audio:
            if audio.getcomptype() != 'NONE' or not 8000 <= audio.getframerate() <= 48000 or not audio.getnframes():
                raise ValueError('MEDIA_DURATION_INVALID')
            return audio.getnframes() / audio.getframerate()
    except (wave.Error, EOFError, OSError):
        raise ValueError('MEDIA_DURATION_INVALID') from None


def _levels(raw, rate, channels, width, step):
    """Mean energy of each ``step`` seconds (a list), or None without numpy / for a width other than 16 bit."""
    np = _numpy()
    if np is None or width != 2:
        return None
    samples = np.frombuffer(raw[:len(raw) // (2 * channels) * 2 * channels], dtype='<i2').astype('float64')
    mono = samples.reshape(-1, channels).mean(1) if channels > 1 else samples
    size = max(1, int(rate * step))
    count = len(mono) // size
    if not count:
        return None
    return (mono[:count * size].reshape(count, size) ** 2).mean(1)


def _pcm(raw, rate, channels, width, start, end):
    frame = channels * width
    begin = max(0, int(start * rate)) * frame
    stop = min(len(raw) // frame, int(math.ceil(end * rate))) * frame
    return raw[begin:stop]


def _wav_bytes(pcm, rate, channels, width) -> bytes:
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return out.getvalue()


def parts(path, *, cap=300.0):
    """(his whole length in seconds, [(start, end, WAV bytes)]) of the first ``cap`` seconds in pieces under a minute.
    The ends are in seconds of the recording; a recording that fits one piece is one piece."""
    raw, rate, channels, width = read(path)
    seconds = len(raw) / (rate * channels * width)
    heard = min(seconds, cap)
    byte_rate = rate * channels * width
    base = max(8.0, min(PART_SECONDS, PIECE_BYTES / byte_rate / (1 + 2 * SNAP_FRACTION)))
    count = max(1, math.ceil(heard / base))
    edges = [0.0] + [heard * k / count for k in range(1, count)] + [heard]
    quiet = _levels(raw, rate, channels, width, 0.1) if count > 1 else None
    if quiet is not None:
        reach = int(base * SNAP_FRACTION / 0.1)
        for k in range(1, count):
            middle = int(edges[k] / 0.1)
            low, high = max(1, middle - reach), min(len(quiet) - 3, middle + reach)
            if high > low:
                # the quietest 0.3 s near the even cut: the cut goes in the middle of it
                best = min(range(low, high + 1), key=lambda i: float(quiet[i:i + 3].mean()))
                edges[k] = (best + 1.5) * 0.1
    return seconds, [(a, b, _wav_bytes(_pcm(raw, rate, channels, width, a, b), rate, channels, width))
                     for a, b in zip(edges, edges[1:])]


def passage(path, limit=58.0):
    """(start, end) in seconds of the clearest stretch of at most ``limit`` seconds, made of whole phrases: the one with the
    most singing in it (earlier when equal). The whole recording when it fits, and on a recording nothing can be told
    of (no numpy, silence) its first ``limit`` seconds."""
    raw, rate, channels, width = read(path)
    seconds = len(raw) / (rate * channels * width)
    if seconds <= limit:
        return 0.0, round(seconds, 2)
    first = (0.0, round(limit, 2))
    energy = _levels(raw, rate, channels, width, FRAME_SECONDS)
    if energy is None:
        return first
    np = _numpy()
    db = 10 * np.log10(np.maximum(energy, 1e-3))
    floor, peak = float(np.percentile(db, 10)), float(db.max())
    if peak - floor < 12:
        return first
    loud = db > max(floor + 10, peak - 38)
    gap = int(PHRASE_GAP_SECONDS / FRAME_SECONDS)
    phrases, start, quiet = [], None, 0
    for index, flag in enumerate(loud):
        if flag:
            start = index if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= gap:
                phrases.append((start, index - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        phrases.append((start, len(loud) - quiet))
    if not phrases:
        return first
    room = int(limit / FRAME_SECONDS)
    best, best_score = None, -1
    for k, (a, _b) in enumerate(phrases):
        # as many whole phrases as fit in ``limit`` from this phrase's start
        j = k
        while j + 1 < len(phrases) and phrases[j + 1][1] - a <= room:
            j += 1
        end = phrases[j][1]
        if end - a > room:
            end = a + room         # one phrase longer than the limit: cut inside it
        score = int(loud[a:end].sum())
        if score > best_score:
            best, best_score = (a, end), score
    a, b = best
    low = max(0.0, a * FRAME_SECONDS - LEAD_SECONDS)
    high = min(seconds, b * FRAME_SECONDS + TAIL_SECONDS)
    if high - low > limit:
        high = low + limit
    return round(low, 2), round(high, 2)


def cut(path, start, end, target) -> Path:
    """The stretch ``start``..``end`` seconds of a WAV written to ``target`` (via a .part file), as a WAV."""
    raw, rate, channels, width = read(path)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + '.part')
    try:
        staging.write_bytes(_wav_bytes(_pcm(raw, rate, channels, width, start, end), rate, channels, width))
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)
    return target
