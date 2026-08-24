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
from pathlib import Path

from latentsync_reply import LatentSyncReplyError, render_latentsync_video
from reply_delivery import ReplyDeliveryPlan, plan_reply_delivery
try:
    from runtime.visual.livetalking import LiveTalkingConfig, capture_candidate_frames
except ImportError:  # Optional visual provider is not part of the portable patch.
    LiveTalkingConfig = object  # type: ignore[misc,assignment]
    capture_candidate_frames = None
from tts import TTSConfig, TTSRequest, TTSService
from tts.delivery import DeliveryAudioError, render_delivery_wav


class ReplyMediaError(RuntimeError):
    pass


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
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise ReplyMediaError("FFMPEG_UNAVAILABLE") from exc


def _tts_config(
    path: Path,
    temporary_root: Path,
    *,
    ordinary_video: bool = False,
) -> TTSConfig:
    settings = _settings(path)
    runtime_root = Path(str(settings.get("runtime_root", "")))
    external_python = runtime_root / "venv" / "Scripts" / "python.exe"
    provider_options = settings.get("provider_options")
    if not isinstance(provider_options, dict):
        provider_options = {}
    else:
        provider_options = dict(provider_options)
    if ordinary_video:
        configured_reference = os.environ.get("OLIVIA_REPLY_VOICE_REFERENCE")
        if configured_reference is not None:
            official_reference = Path(configured_reference)
            if not official_reference.is_file():
                raise ReplyMediaError("VOICE_REFERENCE_UNAVAILABLE")
            settings["reference_audio"] = str(
                _bounded_voice_reference(official_reference, temporary_root)
            )
            settings["leading_trim_seconds"] = 0.0
        provider_options["voice_condition_mode"] = "cross_lingual_audio_only"
    reference_audio = Path(str(settings.get("reference_audio", "")))
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
            "temp_root": str(temporary_root),
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
) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="olivia-reply-", dir=output_path.parent) as temporary:
        root = Path(temporary)
        audio_path = root / "reply.wav"
        frames = root / "frames"
        delivery_plan: ReplyDeliveryPlan | None = None
        if adaptive_delivery:
            delivery_plan = plan_reply_delivery(text)
            try:
                delivery_result = render_delivery_wav(
                    _tts_config(tts_config_path, root, ordinary_video=True),
                    delivery_plan,
                    audio_path,
                )
            except DeliveryAudioError as exc:
                raise ReplyMediaError(str(exc)) from exc
            duration = float(delivery_result.duration_seconds)
        else:
            service = TTSService(_tts_config(tts_config_path, root))
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
                "delivery_block_grouping_applied_to_audio": True,
                "delivery_cue_controls_applied_to_audio": False,
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
                    python_path=latentsync_python_path
                    or Path(os.environ.get("OLIVIA_LATENTSYNC_PYTHON", "")),
                    latentsync_root=latentsync_root
                    or Path(os.environ.get("OLIVIA_LATENTSYNC_ROOT", "")),
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
            _visual_config(visual_config_path),
            audio_path=audio_path,
            output_dir=frames,
            frame_indices=tuple(range(frame_count)),
            worker_path=worker_path,
        )
        _encode_frames(frames, audio_path, output_path, duration)
    return {
        "duration_seconds": round(duration, 3),
        "frame_count": frame_count,
        "audio_provider": "cosyvoice3",
        "visual_provider": "LiveTalking",
        **delivery_metadata,
    }
