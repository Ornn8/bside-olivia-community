"""Thin adapter for the accepted LatentSync 1.5 video-reply route."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class LatentSyncReplyError(RuntimeError):
    """Stable product error for the external LatentSync process."""


def _environment_with_ffmpeg(shim_root: Path, cache_root: Path) -> dict[str, str]:
    configured = os.environ.get("OLIVIA_FFMPEG_EXE", "").strip()
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_file():
            raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE")
        executable = str(configured_path)
    else:
        executable = shutil.which("ffmpeg")
    if executable is None:
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError, OSError) as exc:
            raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE") from exc
    resolved = Path(executable).resolve()
    directory = resolved.parent
    if resolved.stem.casefold() != "ffmpeg":
        shim = Path(shim_root) / "ffmpeg.exe"
        shutil.copy2(resolved, shim)
        directory = shim.parent
    environment = dict(os.environ)
    environment["PATH"] = str(directory) + os.pathsep + environment.get("PATH", "")
    cache_root.mkdir(parents=True, exist_ok=True)
    environment["HF_HOME"] = str(cache_root / "huggingface")
    environment["HF_HUB_CACHE"] = str(cache_root / "huggingface" / "hub")
    environment["TORCH_HOME"] = str(cache_root / "torch")
    environment["XDG_CACHE_HOME"] = str(cache_root)
    environment["TEMP"] = str(shim_root)
    environment["TMP"] = str(shim_root)
    return environment


def _prepare_source_clip(
    source_video: Path,
    audio_path: Path,
    prepared_video: Path,
    *,
    environment: dict[str, str],
) -> None:
    """Decode only the needed span into a stable LatentSync input."""

    ffmpeg = shutil.which("ffmpeg", path=environment["PATH"])
    if ffmpeg is None:
        raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE")
    command = [
        ffmpeg,
        "-y",
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-stream_loop",
        "-1",
        "-i",
        str(source_video),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "25",
        "-c:a",
        "aac",
        "-shortest",
        str(prepared_video),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        timeout=1800,
        env=environment,
    )
    if result.returncode != 0 or not prepared_video.is_file():
        raise LatentSyncReplyError("LATENTSYNC_SOURCE_PREPARE_FAILED")


def render_latentsync_video(
    source_video: Path,
    audio_path: Path,
    output_path: Path,
    *,
    python_path: Path,
    latentsync_root: Path,
    timeout_seconds: float = 21600.0,
) -> dict[str, object]:
    """Apply the accepted 1.5 settings to an original-motion source video."""

    source_video = Path(source_video)
    audio_path = Path(audio_path)
    output_path = Path(output_path)
    python_path = Path(python_path)
    latentsync_root = Path(latentsync_root)
    config_path = (
        latentsync_root / "configs" / "unet" / "stage2_efficient.yaml"
    )
    checkpoint_path = latentsync_root / "checkpoints" / "latentsync_unet.pt"
    required = (
        python_path,
        source_video,
        audio_path,
        latentsync_root / "scripts" / "inference.py",
        config_path,
        checkpoint_path,
    )
    if any(not path.is_file() for path in required):
        raise LatentSyncReplyError("LATENTSYNC_INPUT_UNAVAILABLE")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cache_value = os.environ.get("OLIVIA_PROVIDER_CACHE_ROOT", "").strip()
    cache_root = (
        Path(cache_value).expanduser()
        if cache_value
        else output_path.parent.parent / "provider-cache"
    )
    work_root = cache_root / "latentsync-work"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="job-", dir=work_root) as temporary:
        temporary_root = Path(temporary)
        environment = _environment_with_ffmpeg(temporary_root, cache_root)
        prepared_video = temporary_root / "source-h264.mp4"
        working_output = temporary_root / "reply.mp4"
        pipeline_temp = temporary_root / "pipeline-temp"
        _prepare_source_clip(
            source_video,
            audio_path,
            prepared_video,
            environment=environment,
        )
        command = [
            str(python_path),
            "-m",
            "scripts.inference",
            "--unet_config_path",
            str(config_path),
            "--inference_ckpt_path",
            str(checkpoint_path),
            "--video_path",
            str(prepared_video),
            "--audio_path",
            str(audio_path),
            "--video_out_path",
            str(working_output),
            "--inference_steps",
            "20",
            "--guidance_scale",
            "1.5",
            "--temp_dir",
            str(pipeline_temp),
            "--seed",
            "1247",
            "--enable_deepcache",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=latentsync_root,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LatentSyncReplyError("LATENTSYNC_FAILED") from exc
        if result.returncode != 0:
            raise LatentSyncReplyError("LATENTSYNC_FAILED")
        if not working_output.is_file() or working_output.stat().st_size == 0:
            raise LatentSyncReplyError("LATENTSYNC_OUTPUT_MISSING")
        partial_output = output_path.with_suffix(output_path.suffix + ".partial")
        try:
            shutil.copy2(working_output, partial_output)
            partial_output.replace(output_path)
        finally:
            partial_output.unlink(missing_ok=True)
    return {
        "visual_provider": "LatentSync-1.5",
        "inference_steps": 20,
        "guidance_scale": 1.5,
        "deepcache": True,
        "inference_profile": "stage2_efficient",
        "scene_source": "official_motion_video",
    }


__all__ = ["LatentSyncReplyError", "render_latentsync_video"]
