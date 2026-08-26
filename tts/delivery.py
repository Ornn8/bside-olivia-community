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


_END_OF_PROMPT_TOKEN = "<|endofprompt|>"


@dataclass(frozen=True)
class DeliveryAudioResult:
    duration_seconds: float
    sample_rate: int
    segment_count: int
    quality_report: dict[str, object] | None = None


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
    plan: ReplyDeliveryPlan | object,
) -> dict[str, object]:
    """Render the frozen reply in one inference so the voice never resets."""

    units = tuple(plan.speech_units())
    spoken_text = str(getattr(plan, "spoken_text", "") or "".join(unit.text for unit in units))
    if _END_OF_PROMPT_TOKEN in spoken_text:
        raise DeliveryAudioError("TTS_DIRECTED_TEXT_CONTAINS_CONTROL_TOKEN")
    if len(units) == 1:
        speed = float(units[0].speed)
        gain_db = float(getattr(units[0], "gain_db", 0.0))
    else:
        total_chars = max(1, sum(len(unit.text) for unit in units))
        speed = sum(len(unit.text) * float(unit.speed) for unit in units) / total_chars
        gain_db = sum(
            len(unit.text) * float(getattr(unit, "gain_db", 0.0)) for unit in units
        ) / total_chars
    common: dict[str, object] = {
        "runtime_root": config.runtime_root,
        "model_dir": config.model_dir,
        "reference_audio": config.reference_audio,
        "fp16": bool(config.fp16),
    }
    overall_emotion = str(getattr(plan, "overall_emotion", "") or "").strip()
    if overall_emotion:
        short_instruction = str(
            getattr(plan, "short_instruction", "") or overall_emotion
        ).strip().rstrip("。")
        forbidden = ("<|", "|>", "[", "]", "<", ">", "不要", "禁止", "朗读", "提示词")
        if not 12 <= len(short_instruction) <= 24 or any(
            token in short_instruction for token in forbidden
        ):
            raise DeliveryAudioError("TTS_INSTRUCTION_INVALID")
        options = getattr(config, "provider_options", {}) or {}
        return {
            **common,
            "voice_condition_mode": "instruct2_single_pass",
            "llm_variant": "base",
            "text": spoken_text,
            "instruct_text": (
                "You are a helpful assistant. "
                + short_instruction
                + "。<|endofprompt|>"
            ),
            "quality_forbidden_text": short_instruction,
            "quality_gate_model": str(options.get("quality_gate_model", "base") or "base"),
            "quality_gate_cache_root": str(options.get("quality_gate_cache_root", "") or ""),
            "quality_max_cer": 0.18,
            "speed": 1.0,
            "gain_db": max(-0.75, min(0.75, gain_db)),
            "duration_target_seconds": [40.0, 50.0],
            "max_attempts": 3,
            "seed": 200717,
            "performance_control_mode": "single_pass_llm_short_instruct",
            "director_segment_count": len(getattr(plan, "cues", units)),
        }
    return {
        **common,
        "voice_condition_mode": "contextual_long_form",
        "blocks": [spoken_text],
        "block_controls": [
            {
                "speed": max(0.96, min(1.15, speed)),
                "pause_after_seconds": 0.0,
                "gain_db": max(-1.5, min(1.5, gain_db)),
            }
        ],
        "speed": max(0.96, min(1.15, speed)),
        "cross_fade_seconds": 0.08,
        "seed": 200717,
        "performance_control_mode": "single_pass_global",
        "director_segment_count": len(getattr(plan, "cues", units)),
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
        Path(__file__).with_name("external_audio_quality_worker.py").is_file(),
        Path(config.runtime_root).is_dir(),
        Path(config.model_dir).is_dir(),
        Path(config.reference_audio).is_file(),
    ))


def render_delivery_wav(
    config: TTSConfig,
    plan: ReplyDeliveryPlan | object,
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
    quality_request_path = work / "quality-request.json"
    quality_output_path = work / "quality-result.json"
    temporary_output = work / "speech.wav"
    try:
        request = build_external_delivery_request(config, plan)
        environment = dict(os.environ)
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "MODELSCOPE_OFFLINE": "1",
            }
        )
        def run_worker(payload: dict[str, object]) -> None:
            temporary_output.unlink(missing_ok=True)
            request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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

        def run_quality_gate(payload: dict[str, object]) -> dict[str, object]:
            quality_worker = Path(__file__).with_name("external_audio_quality_worker.py")
            if not quality_worker.is_file():
                raise DeliveryAudioError("TTS_CONTENT_GATE_UNAVAILABLE")
            quality_request_path.write_text(
                json.dumps(
                    {
                        "audio_path": str(temporary_output),
                        "expected_text": str(payload.get("text", "")),
                        "forbidden_text": str(payload.get("quality_forbidden_text", "")),
                        "model": str(payload.get("quality_gate_model", "base")),
                        "cache_root": str(payload.get("quality_gate_cache_root", "")),
                        "max_cer": float(payload.get("quality_max_cer", 0.18)),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            quality_output_path.unlink(missing_ok=True)
            try:
                completed = subprocess.run(
                    [str(executable), str(quality_worker), "--request", str(quality_request_path), "--output", str(quality_output_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    check=False,
                    timeout=min(timeout_seconds, 600.0),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DeliveryAudioError("TTS_CONTENT_GATE_UNAVAILABLE") from exc
            if completed.returncode != 0 or not quality_output_path.is_file():
                raise DeliveryAudioError("TTS_CONTENT_GATE_UNAVAILABLE")
            try:
                report = json.loads(quality_output_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeliveryAudioError("TTS_CONTENT_GATE_UNAVAILABLE") from exc
            if not isinstance(report, dict) or not isinstance(report.get("passed"), bool):
                raise DeliveryAudioError("TTS_CONTENT_GATE_UNAVAILABLE")
            return report

        quality_report: dict[str, object] | None = None
        if request.get("voice_condition_mode") == "instruct2_single_pass":
            duration_candidate_seen = False
            for attempt in range(max(1, min(3, int(request.get("max_attempts", 1))))):
                candidate = dict(request)
                candidate["seed"] = int(request.get("seed", 200717)) + attempt
                candidate["max_attempts"] = 1
                run_worker(candidate)
                sample_rate, frame_count = _validate_wav(temporary_output)
                try:
                    sample_rate, frame_count = _fit_overlong_wav(
                        temporary_output, frame_count / sample_rate
                    )
                    validate_delivery_duration(frame_count / sample_rate)
                except DeliveryAudioError:
                    continue
                duration_candidate_seen = True
                quality_report = run_quality_gate(candidate)
                quality_report.update(
                    attempt=attempt + 1,
                    seed=candidate["seed"],
                    duration_seconds=round(frame_count / sample_rate, 3),
                )
                if quality_report["passed"] is True:
                    break
            else:
                if duration_candidate_seen:
                    raise DeliveryAudioError("TTS_CONTENT_GATE_REJECTED")
                raise DeliveryAudioError("TTS_DELIVERY_DURATION_OUT_OF_RANGE")
        else:
            run_worker(request)
            sample_rate, frame_count = _validate_wav(temporary_output)
            sample_rate, frame_count = _fit_overlong_wav(
                temporary_output, frame_count / sample_rate
            )
            validate_delivery_duration(frame_count / sample_rate)
        temporary_output.replace(output_path)
        return DeliveryAudioResult(
            duration_seconds=frame_count / sample_rate,
            sample_rate=sample_rate,
            segment_count=len(plan.speech_units()),
            quality_report=quality_report,
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
