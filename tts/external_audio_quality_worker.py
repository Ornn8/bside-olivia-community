"""Offline ASR content gate for an ordinary-reply TTS candidate.

This worker runs after the CosyVoice process exits, so the two models never
compete for GPU memory.  It deliberately has no product-package imports.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import wave
from pathlib import Path


_WHISPER_BASE_SHA256 = "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e"


def normalize_transcript(text: str) -> str:
    return re.sub(r"[^\u3400-\u9fffA-Za-z0-9]+", "", str(text)).casefold()


def _edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _longest_extra_insertion(expected: str, actual: str) -> int:
    matcher = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    return max(
        (
            actual_end - actual_start
            for tag, _expected_start, _expected_end, actual_start, actual_end in matcher.get_opcodes()
            if tag == "insert"
        ),
        default=0,
    )


def _longest_contiguous_omission(expected: str, actual: str) -> int:
    matcher = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    return max(
        (
            expected_end - expected_start
            for tag, expected_start, expected_end, actual_start, actual_end in matcher.get_opcodes()
            if tag == "delete"
        ),
        default=0,
    )


def _has_added_repetition(expected: str, actual: str, width: int = 2) -> bool:
    if len(actual) < width * 2:
        return False
    for start in range(len(actual) - width + 1):
        phrase = actual[start : start + width]
        if actual.count(phrase) > max(1, expected.count(phrase)):
            return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_pcm16_for_whisper(path: Path):
    """Decode the product WAV directly so ASR never depends on an ffmpeg alias."""

    import numpy as np

    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        samples = np.frombuffer(
            source.readframes(source.getnframes()), dtype="<i2"
        ).astype("float32")
    if sample_width != 2 or channels < 1:
        raise RuntimeError("TTS_CONTENT_AUDIO_INVALID")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples /= 32768.0
    if sample_rate == 16000:
        return samples
    target_count = max(1, round(len(samples) * 16000 / sample_rate))
    source_x = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    target_x = np.linspace(0.0, 1.0, num=target_count, endpoint=False)
    return np.interp(target_x, source_x, samples).astype("float32")


def assess_transcript(
    expected_text: str,
    transcript: str,
    forbidden_text: str,
    *,
    max_cer: float = 0.18,
) -> dict[str, object]:
    expected = normalize_transcript(expected_text)
    actual = normalize_transcript(transcript)
    forbidden = normalize_transcript(forbidden_text)
    if not expected or not actual:
        return {
            "passed": False,
            "error_code": "TTS_CONTENT_EMPTY",
            "transcript": str(transcript).strip(),
        }

    cer = _edit_distance(expected, actual) / len(expected)
    length_ratio = len(actual) / len(expected)
    instruction_overlap = max(
        (
            block.size
            for block in difflib.SequenceMatcher(
                a=forbidden,
                b=actual,
                autojunk=False,
            ).get_matching_blocks()
        ),
        default=0,
    )
    longest_extra = _longest_extra_insertion(expected, actual)
    longest_omission = _longest_contiguous_omission(expected, actual)
    repeated = _has_added_repetition(expected, actual)
    checks = {
        "cer": cer <= max_cer,
        "length_ratio": len(actual) == len(expected),
        "instruction_overlap": instruction_overlap < 4,
        "extra_speech": longest_extra == 0,
        "contiguous_omission": longest_omission == 0,
        "repetition": not repeated,
    }
    return {
        "passed": all(checks.values()),
        "error_code": None if all(checks.values()) else "TTS_CONTENT_MISMATCH",
        "transcript": str(transcript).strip(),
        "cer": round(cer, 4),
        "length_ratio": round(length_ratio, 4),
        "instruction_overlap_chars": instruction_overlap,
        "longest_extra_insertion_chars": longest_extra,
        "longest_contiguous_omission_chars": longest_omission,
        "added_repetition": repeated,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))

    model_name = str(request.get("model", "base") or "base").strip()
    if model_name != "base":
        raise RuntimeError("only the pinned Whisper base gate is supported")
    cache_value = str(request.get("cache_root", "") or "").strip()
    cache_root = Path(cache_value) if cache_value else Path.home() / ".cache" / "whisper"
    checkpoint = cache_root / "base.pt"
    if not checkpoint.is_file():
        raise RuntimeError("offline Whisper base checkpoint unavailable")
    if _sha256(checkpoint) != _WHISPER_BASE_SHA256:
        raise RuntimeError("offline Whisper base checkpoint hash mismatch")

    import torch
    import whisper

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model("base", device=device, download_root=str(cache_root))
    result = model.transcribe(
        _read_pcm16_for_whisper(Path(str(request["audio_path"]))),
        language="zh",
        fp16=device == "cuda",
        temperature=0,
    )
    report = assess_transcript(
        str(request["expected_text"]),
        str(result.get("text", "")),
        str(request.get("forbidden_text", "")),
        max_cer=float(request.get("max_cer", 0.18)),
    )
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
