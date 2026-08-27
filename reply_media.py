"""Real CosyVoice -> pinned LiveTalking reply video assembly."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from latentsync_reply import LatentSyncReplyError, media_runtime_available, render_latentsync_video, resolve_ffmpeg_executable
from runtime.media.media_paths import resolve_media_path
from reply_delivery import ReplyDeliveryPlan, plan_reply_delivery
from voice_direction import VoicePerformancePlan
try:
    from runtime.visual.livetalking import LiveTalkingConfig, capture_candidate_frames
except ImportError:  # Optional visual provider is not part of the portable patch.
    LiveTalkingConfig = object  # type: ignore[misc,assignment]
    capture_candidate_frames = None
from tts import TTSConfig, TTSRequest, TTSService
from tts.delivery import DeliveryAudioError, delivery_configured, render_delivery_wav


class ReplyMediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompleteVideoDelivery:
    tts: TTSConfig
    visual: LiveTalkingConfig
    worker: Path


def _bounded_voice_reference(source: Path, temporary_root: Path) -> Path:
    """Use only the reviewed clean 4.85-second voice prompt."""

    try:
        with wave.open(str(source), "rb") as reference:
            frame_rate = reference.getframerate()
            if frame_rate <= 0:
                raise wave.Error("invalid frame rate")
            maximum_frames = int(frame_rate * 4.85)
            if reference.getnframes() <= maximum_frames:
                return source
            parameters = reference.getparams()
            frames = reference.readframes(maximum_frames)
    except (OSError, EOFError, wave.Error) as exc:
        raise ReplyMediaError("VOICE_REFERENCE_INVALID") from exc

    temporary_root.mkdir(parents=True, exist_ok=True)
    bounded = temporary_root / "voice-reference.wav"
    try:
        with wave.open(str(bounded), "wb") as target:
            target.setparams(parameters)
            target.writeframes(frames)
    except (OSError, wave.Error) as exc:
        raise ReplyMediaError("VOICE_REFERENCE_INVALID") from exc
    return bounded


def _settings(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplyMediaError("PROVIDER_CONFIG_UNAVAILABLE") from exc
    settings = raw.get("settings") if isinstance(raw, dict) else None
    if not isinstance(settings, dict):
        raise ReplyMediaError("PROVIDER_CONFIG_INVALID")
    return dict(settings)


def _ffmpeg() -> str:
    try:
        return str(resolve_ffmpeg_executable())
    except LatentSyncReplyError as exc:
        raise ReplyMediaError("FFMPEG_UNAVAILABLE") from exc


def _tts_config(
    path: Path,
    temporary_root: Path,
    *,
    ordinary_video: bool = False,
    env: Mapping[str, str] | None = None,
) -> TTSConfig:
    settings = _settings(path)
    environment = os.environ if env is None else env

    def configured_path(value: object) -> Path:
        resolved = resolve_media_path(value, environment)
        if resolved is None:
            raise ReplyMediaError("TTS_CONFIG_PATH_UNAVAILABLE")
        return resolved

    runtime_root = configured_path(settings.get("runtime_root", ""))
    model_dir = configured_path(settings.get("model_dir", ""))
    reference_audio = configured_path(settings.get("reference_audio", ""))
    external_python = runtime_root / "venv" / "Scripts" / "python.exe"
    settings.update(
        {
            "runtime_root": str(runtime_root),
            "model_dir": str(model_dir),
            "reference_audio": str(reference_audio),
        }
    )
    provider_options = settings.get("provider_options")
    if not isinstance(provider_options, dict):
        provider_options = {}
    else:
        provider_options = dict(provider_options)
    for key in ("numba_cache_dir", "quality_gate_cache_root", "wetext_fst_root"):
        if key in provider_options and str(provider_options[key]).strip():
            provider_options[key] = str(configured_path(provider_options[key]))
    if ordinary_video:
        configured_reference = environment.get("OLIVIA_REPLY_VOICE_REFERENCE")
        if configured_reference is not None and str(configured_reference).strip():
            official_reference = configured_path(configured_reference)
            if not official_reference.is_file():
                raise ReplyMediaError("VOICE_REFERENCE_UNAVAILABLE")
            settings["reference_audio"] = str(
                _bounded_voice_reference(official_reference, temporary_root)
            )
            settings["leading_trim_seconds"] = 0.0
        provider_options["voice_condition_mode"] = "cross_lingual_audio_only"
        quality_cache_root = environment.get(
            "OLIVIA_TTS_QUALITY_GATE_CACHE_ROOT"
        )
        if quality_cache_root is not None and str(quality_cache_root).strip():
            provider_options["quality_gate_cache_root"] = str(
                configured_path(quality_cache_root)
            )
    reference_audio = Path(str(settings["reference_audio"]))
    leading_trim = settings.get("leading_trim_seconds")
    if leading_trim is None and reference_audio.is_file():
        try:
            with wave.open(str(reference_audio), "rb") as source:
                leading_trim = source.getnframes() / source.getframerate()
        except (OSError, EOFError, wave.Error, ZeroDivisionError):
            leading_trim = 0.0
    provider_options.update(
        {
            "external_python": str(external_python),
            "temp_root": str(temporary_root.resolve(strict=False)),
        }
    )
    settings.update(
        {
            "enabled": True,
            "leading_trim_seconds": leading_trim or 0.0,
            "provider_options": provider_options,
        }
    )
    return TTSConfig.from_mapping(settings)


def _visual_config(path: Path) -> LiveTalkingConfig:
    if capture_candidate_frames is None:
        raise ReplyMediaError("THIRD_PARTY_VISUAL_NOT_INSTALLED")
    settings = _settings(path)
    fields = {
        "runtime_root",
        "checkpoint_path",
        "checkpoint_sha256",
        "avatar_payload",
        "original_reference",
        "work_root",
        "python_executable",
        "avatar_id",
        "checkpoint_url",
        "checkpoint_revision",
        "checkpoint_license",
        "upstream_source",
        "upstream_revision",
        "upstream_license",
    }
    return LiveTalkingConfig(**{key: value for key, value in settings.items() if key in fields})


def assemble_complete_video_delivery(
    tts_config_path: Path,
    visual_config_path: Path,
    worker_path: Path,
    temporary_root: Path,
    env: Mapping[str, str] | None = None,
    *,
    require_quality_gate: bool = False,
) -> CompleteVideoDelivery:
    """Pure configuration seam shared by availability and the real renderer."""

    try:
        tts = _tts_config(tts_config_path, temporary_root, ordinary_video=True, env=env)
        visual = _visual_config(visual_config_path)
        visual.validate()
    except (ReplyMediaError, ValueError, TypeError) as exc:
        raise ReplyMediaError("COMPLETE_VIDEO_CONFIG_UNAVAILABLE") from exc
    worker = Path(worker_path)
    if (
        not delivery_configured(tts)
        or not worker.is_file()
        or not media_runtime_available(env)
    ):
        raise ReplyMediaError("COMPLETE_VIDEO_CONFIG_UNAVAILABLE")
    if require_quality_gate and not delivery_configured(
        tts, require_quality_gate=True
    ):
        raise ReplyMediaError("TTS_CONTENT_GATE_UNAVAILABLE")
    return CompleteVideoDelivery(tts, visual, worker)


def _encode_frames(frames: Path, audio: Path, output: Path, duration: float) -> None:
    temporary = output.with_suffix(".rendering.mp4")
    command = [
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        "25",
        "-i",
        str(frames / "frame_%04d.png"),
        "-i",
        str(audio),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=300, check=False)
        if result.returncode != 0 or not temporary.is_file():
            raise ReplyMediaError("REPLY_VIDEO_ENCODE_FAILED")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def render_reply_video(
    text: str,
    output_path: Path,
    *,
    tts_config_path: Path,
    visual_config_path: Path,
    worker_path: Path,
    scene_path: Path | None = None,
    latentsync_python_path: Path | None = None,
    latentsync_root: Path | None = None,
    adaptive_delivery: bool = False,
    voice_performance_plan: VoicePerformancePlan | None = None,
    enforce_content_gate: bool = False,
    environment: Mapping[str, str] | None = None,
    ffmpeg_path: Path | None = None,
    provider_cache_root: Path | None = None,
) -> dict[str, object]:
    if scene_path is not None and (
        latentsync_python_path is None
        or latentsync_root is None
        or not latentsync_python_path.is_absolute()
        or not latentsync_root.is_absolute()
    ):
        raise ReplyMediaError("LATENTSYNC_INPUT_UNAVAILABLE")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="olivia-reply-", dir=output_path.parent) as temporary:
        root = Path(temporary)
        delivery = assemble_complete_video_delivery(
            tts_config_path,
            visual_config_path,
            worker_path,
            root,
            environment,
            require_quality_gate=enforce_content_gate,
        )
        audio_path = root / "reply.wav"
        frames = root / "frames"
        delivery_plan: ReplyDeliveryPlan | VoicePerformancePlan | None = None
        if adaptive_delivery:
            delivery_plan = voice_performance_plan or plan_reply_delivery(text)
            if delivery_plan.spoken_text != text:
                raise ReplyMediaError("VOICE_DIRECTION_TEXT_MISMATCH")
            try:
                delivery_result = render_delivery_wav(
                    delivery.tts,
                    delivery_plan,
                    audio_path,
                    enforce_content_gate=enforce_content_gate,
                )
            except DeliveryAudioError as exc:
                raise ReplyMediaError(str(exc)) from exc
            duration = float(delivery_result.duration_seconds)
        else:
            service = TTSService(delivery.tts)
            try:
                result = asyncio.run(
                    service.synthesize(
                        TTSRequest(text, stream=False),
                        output_path=audio_path,
                    )
                )
            finally:
                service.close()
            if result.status != "completed" or not audio_path.is_file() or not result.duration_seconds:
                raise ReplyMediaError(result.error_code or "TTS_UNAVAILABLE")
            duration = float(result.duration_seconds)
        if not audio_path.is_file() or duration <= 0:
            raise ReplyMediaError("TTS_EMPTY_AUDIO")
        frame_count = max(1, int(math.floor(duration * 25.0)))
        delivery_metadata = (
            {
                "delivery_plan": delivery_plan.to_dict(),
                "delivery_plan_applied_to_audio": True,
                "delivery_audio_mode": "single_pass_continuous",
                "per_segment_audio_controls_applied": False,
                "voice_emotion_control": (
                    "llm_global_instruct2"
                    if isinstance(delivery_plan, VoicePerformancePlan)
                    else "legacy_global_pace"
                ),
                # LatentSync derives the face performance from the paced audio
                # while the accepted original-motion video keeps body/scene motion.
                "visual_cues_backend_control": False,
            }
            if delivery_plan is not None
            else {}
        )
        if scene_path is not None:
            try:
                visual_metadata = render_latentsync_video(
                    scene_path,
                    audio_path,
                    output_path,
                python_path=latentsync_python_path,
                latentsync_root=latentsync_root,
                environment=environment,
                ffmpeg_path=ffmpeg_path,
                provider_cache_root=provider_cache_root,
                )
            except LatentSyncReplyError as exc:
                raise ReplyMediaError(str(exc)) from exc
            return {
                "duration_seconds": round(duration, 3),
                "frame_count": frame_count,
                "audio_provider": "cosyvoice3",
                **delivery_metadata,
                **visual_metadata,
            }
        if capture_candidate_frames is None:
            raise ReplyMediaError("THIRD_PARTY_VISUAL_NOT_INSTALLED")
        capture_candidate_frames(
            delivery.visual,
            audio_path=audio_path,
            output_dir=frames,
            frame_indices=tuple(range(frame_count)),
            worker_path=delivery.worker,
        )
        _encode_frames(frames, audio_path, output_path, duration)
    return {
        "duration_seconds": round(duration, 3),
        "frame_count": frame_count,
        "audio_provider": "cosyvoice3",
        "visual_provider": "LiveTalking",
        **delivery_metadata,
    }
