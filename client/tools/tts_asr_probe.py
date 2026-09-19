"""Offline semantic ASR probe for B06 WAV acceptance evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
import wave
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tts.audio import audio_metrics  # noqa: E402


def _semantic_text(value: str) -> str:
    normalized = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE).lower()
    return normalized.translate(str.maketrans({"测": "測", "试": "試"}))


def _repair_windows_argument(value: str) -> str:
    """Repair UTF-8 text decoded through a legacy PowerShell code page."""

    for encoding in ("gbk", "big5"):
        try:
            candidate = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if candidate != value:
            return candidate
    return value


def _read_pcm16_for_whisper(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frame_count = handle.getnframes()
        if sample_width != 2 or channels < 1:
            raise ValueError("ASR_WAV_PCM16_REQUIRED")
        samples = np.frombuffer(handle.readframes(frame_count), dtype="<i2").astype("float32")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples /= 32768.0
    if sample_rate == 16000:
        return samples, sample_rate
    target_count = max(1, round(len(samples) * 16000 / sample_rate))
    source_x = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    target_x = np.linspace(0.0, 1.0, num=target_count, endpoint=False)
    return np.interp(target_x, source_x, samples).astype("float32"), sample_rate


def transcribe_audio(
    audio_path: str | Path,
    *,
    model_name: str,
    download_root: str | Path,
    language: str,
    must_contain: Sequence[str] = (),
) -> dict[str, Any]:
    """Transcribe an existing local WAV without downloading an ASR model."""

    path = Path(audio_path)
    repaired_tokens = [_repair_windows_argument(str(token)) for token in must_contain]
    result: dict[str, Any] = {
        "audio_file": path.name,
        "model": model_name,
        "language": language,
        "must_contain": repaired_tokens,
    }
    try:
        result["audio"] = audio_metrics(path)
    except (OSError, ValueError) as exc:
        result.update({"status": "FAIL", "error_code": "ASR_AUDIO_INVALID"})
        return result

    model_file = Path(download_root) / f"{model_name}.pt"
    if not model_file.is_file():
        result.update({"status": "FAIL", "error_code": "ASR_MODEL_NOT_LOCAL"})
        return result

    try:
        import torch
        import whisper
    except Exception:
        result.update({"status": "FAIL", "error_code": "ASR_DEPENDENCY_MISSING"})
        return result

    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        audio_samples, input_sample_rate = _read_pcm16_for_whisper(path)
        result["decoder"] = {
            "type": "stdlib-wave-pcm16",
            "input_sample_rate": input_sample_rate,
            "model_sample_rate": 16000,
        }
    except (OSError, ValueError) as exc:
        result.update({"status": "FAIL", "error_code": "ASR_WAV_DECODE_FAILED"})
        return result
    try:
        model = whisper.load_model(
            model_name,
            device=device,
            download_root=str(download_root),
        )
        transcription = model.transcribe(
            audio_samples,
            language=language,
            fp16=device == "cuda",
            temperature=0,
        )
    except Exception as exc:
        detail = re.sub(r"[A-Za-z]:\\[^\r\n]*", "<private-path>", str(exc))[:300]
        result.update(
            {
                "status": "FAIL",
                "error_code": "ASR_TRANSCRIBE_FAILED",
                "exception_type": type(exc).__name__,
                "detail": detail,
            }
        )
        return result

    text = str(transcription.get("text", "")).strip()
    normalized = _semantic_text(text)
    matches = {
        token: _semantic_text(token) in normalized
        for token in repaired_tokens
        if str(token).strip()
    }
    segments = [
        {
            "start": round(float(segment.get("start", 0.0)), 3),
            "end": round(float(segment.get("end", 0.0)), 3),
            "text": str(segment.get("text", "")).strip(),
        }
        for segment in transcription.get("segments", [])
    ]
    result.update(
        {
            "status": "PASS" if text and all(matches.values()) else "FAIL",
            "device": device,
            "text": text,
            "segments": segments,
            "semantic_matches": matches,
            "error_code": None if text and all(matches.values()) else "ASR_SEMANTIC_MISMATCH",
        }
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B06 offline semantic ASR probe")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="base")
    parser.add_argument("--download-root", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--must-contain", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    value = transcribe_audio(
        args.audio,
        model_name=args.model,
        download_root=args.download_root,
        language=args.language,
        must_contain=args.must_contain,
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": value["status"], "audio_file": value["audio_file"], "text": value.get("text", "")}, ensure_ascii=False))
    return 0 if value["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
