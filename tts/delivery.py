"""One-process CosyVoice rendering for a non-spoken reply delivery plan."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from latentsync_reply import LatentSyncReplyError, resolve_ffmpeg_executable
from reply_delivery import ReplyDeliveryPlan

from .contracts import TTSConfig


class DeliveryAudioError(RuntimeError):
    """Stable ordinary-reply audio rendering failure."""


@dataclass(frozen=True)
class DeliveryAudioResult:
    duration_seconds: float
    sample_rate: int
    segment_count: int


def delivery_tempo_factor(duration_seconds: float) -> float | None:
    """Allow only a tiny whole-utterance correction; never rescue bad copy."""

    duration = float(duration_seconds)
    if duration <= 50.0:
        return None
    if duration > 52.0:
        return None
    return round(duration / 50.0, 4)


def validate_delivery_duration(duration_seconds: float) -> None:
    """Fail closed rather than producing a rushed or half-speed reply."""

    if not 40.0 <= float(duration_seconds) <= 50.0:
        raise DeliveryAudioError("TTS_DELIVERY_DURATION_OUT_OF_RANGE")


def build_external_delivery_request(
    config: TTSConfig,
    plan: ReplyDeliveryPlan,
) -> dict[str, object]:
    """Keep delivery control separate from the one continuous spoken payload."""

    return {
        "runtime_root": config.runtime_root,
        "model_dir": config.model_dir,
        "reference_audio": config.reference_audio,
        "fp16": bool(config.fp16),
        "voice_condition_mode": "cross_lingual_audio_only",
        "blocks": [unit.text for unit in plan.speech_units()],
        "speed": 1.0,
        "cross_fade_seconds": 0.08,
        "seed": 200717,
    }


def _validate_wav(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise DeliveryAudioError("TTS_EXTERNAL_AUDIO_INVALID")
            return source.getframerate(), source.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise DeliveryAudioError("TTS_EXTERNAL_AUDIO_INVALID") from exc


def _ffmpeg() -> str:
    try:
        return str(resolve_ffmpeg_executable())
    except LatentSyncReplyError as exc:
        raise DeliveryAudioError("FFMPEG_UNAVAILABLE") from exc


def _fit_overlong_wav(path: Path, duration_seconds: float) -> tuple[int, int]:
    duration = float(duration_seconds)
    if duration > 52.0:
        raise DeliveryAudioError("TTS_DELIVERY_DURATION_OUT_OF_RANGE")

    factor = delivery_tempo_factor(duration)
    if factor is None:
        return _validate_wav(path)
    fitted = path.with_name("speech-fitted.wav")
    try:
        completed = subprocess.run(
            [
                _ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-filter:a",
                f"atempo={factor:.4f}",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(fitted),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryAudioError("TTS_DURATION_FIT_FAILED") from exc
    if completed.returncode != 0 or not fitted.is_file():
        raise DeliveryAudioError("TTS_DURATION_FIT_FAILED")
    sample_rate, frame_count = _validate_wav(fitted)
    fitted_duration = frame_count / sample_rate
    validate_delivery_duration(fitted_duration)
    fitted.replace(path)
    return sample_rate, frame_count


def delivery_configured(config: TTSConfig) -> bool:
    """Read-only closure check shared by delivery preflight and rendering."""

    return all((
        Path(str(config.provider_options.get("external_python", "") or "")).is_file(),
        Path(__file__).with_name("external_cosyvoice_worker.py").is_file(),
        Path(config.runtime_root).is_dir(),
        Path(config.model_dir).is_dir(),
        Path(config.reference_audio).is_file(),
    ))


def render_delivery_wav(
    config: TTSConfig,
    plan: ReplyDeliveryPlan,
    output_path: Path,
    *,
    timeout_seconds: float = 3600.0,
) -> DeliveryAudioResult:
    """Render all delivery segments while loading the maintained model once."""

    if not delivery_configured(config) or not plan.cues:
        raise DeliveryAudioError("TTS_DELIVERY_UNAVAILABLE")
    executable = Path(str(config.provider_options.get("external_python", "") or ""))
    worker = Path(__file__).with_name("external_cosyvoice_worker.py")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_parent = str(config.provider_options.get("temp_root", "") or "").strip()
    try:
        work = Path(tempfile.mkdtemp(prefix="olivia-delivery-", dir=temp_parent or output_path.parent))
    except OSError as exc:
        raise DeliveryAudioError("TTS_TEMP_CONFIG_INVALID") from exc
    request_path = work / "request.json"
    temporary_output = work / "speech.wav"
    try:
        request_path.write_text(
            json.dumps(build_external_delivery_request(config, plan), ensure_ascii=False),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "MODELSCOPE_OFFLINE": "1",
            }
        )
        try:
            completed = subprocess.run(
                [str(executable), str(worker), "--request", str(request_path), "--output", str(temporary_output)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryAudioError("TTS_EXTERNAL_PROCESS_UNAVAILABLE") from exc
        if completed.returncode != 0 or not temporary_output.is_file():
            raise DeliveryAudioError("TTS_EXTERNAL_PROCESS_FAILED")
        sample_rate, frame_count = _validate_wav(temporary_output)
        if frame_count <= 0 or sample_rate <= 0:
            raise DeliveryAudioError("TTS_EMPTY_AUDIO")
        sample_rate, frame_count = _fit_overlong_wav(
            temporary_output,
            frame_count / sample_rate,
        )
        validate_delivery_duration(frame_count / sample_rate)
        temporary_output.replace(output_path)
        return DeliveryAudioResult(
            duration_seconds=frame_count / sample_rate,
            sample_rate=sample_rate,
            segment_count=len(plan.speech_units()),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


__all__ = [
    "DeliveryAudioError",
    "DeliveryAudioResult",
    "build_external_delivery_request",
    "delivery_tempo_factor",
    "render_delivery_wav",
    "validate_delivery_duration",
]
