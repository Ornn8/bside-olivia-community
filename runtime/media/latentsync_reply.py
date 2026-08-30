"""Thin adapter for the accepted LatentSync 1.5 video-reply route."""

from __future__ import annotations

from contextlib import suppress
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Mapping

from runtime.media.managed_subprocess import run_managed_process
from runtime.media.media_paths import resolve_media_path


_DEFAULT_LATENTSYNC_TIMEOUT_SECONDS = 1800.0
_MAX_LATENTSYNC_TIMEOUT_SECONDS = 3600.0
_LOGGER = logging.getLogger(__name__)


class LatentSyncReplyError(RuntimeError):
    """Stable product error for the external LatentSync process."""

    def __init__(self, code: str, *, diagnostic: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


def _process_diagnostic(
    *,
    returncode: int | None,
    stderr: bytes | str | None,
    timed_out: bool = False,
    start_failed: bool = False,
    management_failed: bool = False,
) -> str:
    if isinstance(stderr, bytes):
        payload = stderr
    elif stderr is None:
        payload = b""
    else:
        payload = str(stderr).encode("utf-8", errors="replace")
    normalized = payload.decode("utf-8", errors="replace").casefold()
    if timed_out:
        category = "process_timeout"
    elif start_failed:
        category = "process_start_failure"
    elif management_failed:
        category = "process_management_failure"
    elif "cuda" in normalized and "out of memory" in normalized:
        category = "cuda_out_of_memory"
    elif "dll load failed" in normalized or "winerror 126" in normalized:
        category = "runtime_dependency_missing"
    elif "no module named" in normalized or "modulenotfounderror" in normalized:
        category = "python_module_missing"
    elif "filenotfounderror" in normalized or "no such file" in normalized:
        category = "configured_path_missing"
    elif "cuda" in normalized:
        category = "cuda_runtime_failure"
    else:
        category = "external_process_failure"
    return (
        f"returncode={returncode if returncode is not None else 'unknown'};"
        f"stderr_category={category}"
    )


def _reported_process_failure(
    environment: Mapping[str, str], *, returncode: int | None,
    stderr: bytes | str | None, timed_out: bool = False, start_failed: bool = False,
    management_failed: bool = False,
) -> LatentSyncReplyError:
    diagnostic = _process_diagnostic(
        returncode=returncode, stderr=stderr,
        timed_out=timed_out, start_failed=start_failed,
        management_failed=management_failed,
    )
    _LOGGER.warning("LatentSync process failed: %s", diagnostic)
    data_root = resolve_media_path(environment.get("OLIVIA_LOCAL_DATA_ROOT", ""), environment)
    if data_root is not None:
        try:
            log_root = data_root / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            record = {
                "provider": "latentsync",
                "error_code": "LATENTSYNC_FAILED",
                "diagnostic": diagnostic,
            }
            with (log_root / "media-provider.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass
    return LatentSyncReplyError("LATENTSYNC_FAILED", diagnostic=diagnostic)


def _latentsync_timeout_seconds(
    requested: float | None,
    environment: Mapping[str, str],
) -> float:
    raw: object = (
        environment.get("OLIVIA_LATENTSYNC_TIMEOUT_SECONDS")
        if requested is None
        else requested
    )
    if raw in (None, ""):
        return _DEFAULT_LATENTSYNC_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LATENTSYNC_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_LATENTSYNC_TIMEOUT_SECONDS
    return min(value, _MAX_LATENTSYNC_TIMEOUT_SECONDS)


def resolve_ffmpeg_executable(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the same FFmpeg executable used by the LatentSync renderer."""

    environment = os.environ if env is None else env
    configured = str(environment.get("OLIVIA_FFMPEG_EXE", "")).strip()
    if configured:
        configured_path = resolve_media_path(configured, environment)
        if configured_path is None or not configured_path.is_file():
            raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE")
        executable = configured_path
    else:
        executable = shutil.which("ffmpeg")
    if executable is None:
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError, OSError) as exc:
            raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE") from exc
    resolved = Path(executable).resolve()
    if not resolved.is_file():
        raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE")
    return resolved


def media_runtime_available(env: Mapping[str, str] | None = None) -> bool:
    """Check the FFmpeg resolver and frame probe used by complete delivery."""

    try:
        resolve_ffmpeg_executable(env)
        import imageio_ffmpeg

        return callable(getattr(imageio_ffmpeg, "count_frames_and_secs", None))
    except (ImportError, RuntimeError, OSError, LatentSyncReplyError):
        return False


def _environment_with_ffmpeg(
    shim_root: Path,
    cache_root: Path,
    *,
    ffmpeg_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    resolved = (
        resolve_ffmpeg_executable(environment)
        if ffmpeg_path is None
        else Path(ffmpeg_path)
    )
    if not resolved.is_absolute() or not resolved.is_file():
        raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE")
    directory = resolved.parent
    if resolved.stem.casefold() != "ffmpeg":
        shim = Path(shim_root) / "ffmpeg.exe"
        shutil.copy2(resolved, shim)
        directory = shim.parent
    runtime_environment = dict(os.environ if environment is None else environment)
    runtime_environment["PATH"] = str(directory) + os.pathsep + runtime_environment.get("PATH", "")
    cache_root.mkdir(parents=True, exist_ok=True)
    runtime_environment["HF_HOME"] = str(cache_root / "huggingface")
    runtime_environment["HF_HUB_CACHE"] = str(cache_root / "huggingface" / "hub")
    runtime_environment["TORCH_HOME"] = str(cache_root / "torch")
    runtime_environment["XDG_CACHE_HOME"] = str(cache_root)
    runtime_environment["TEMP"] = str(shim_root)
    runtime_environment["TMP"] = str(shim_root)
    return runtime_environment


def _prepare_source_clip(
    source_video: Path,
    audio_path: Path,
    prepared_video: Path,
    *,
    environment: dict[str, str],
    deadline: float,
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
    try:
        result = run_managed_process(
            command, deadline=deadline, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise _reported_process_failure(
            environment, returncode=None, stderr=exc.stderr, timed_out=True,
        ) from exc
    except OSError as exc:
        raise _reported_process_failure(
            environment, returncode=None, stderr=str(exc), management_failed=True,
        ) from exc
    if result.returncode != 0:
        raise _reported_process_failure(
            environment, returncode=result.returncode, stderr=result.stderr,
        )
    if not prepared_video.is_file():
        raise LatentSyncReplyError("LATENTSYNC_SOURCE_PREPARE_FAILED")


def _validate_rendered_video(
    video_path: Path,
    *,
    environment: dict[str, str],
    deadline: float,
) -> None:
    ffmpeg = shutil.which("ffmpeg", path=environment["PATH"])
    if ffmpeg is None:
        raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror", "-nostats",
        "-progress", "pipe:1", "-i", str(video_path), "-map", "0:v:0",
        "-map", "0:a:0", "-f", "null", os.devnull,
    ]
    try:
        result = run_managed_process(
            command, deadline=deadline, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise _reported_process_failure(
            environment, returncode=None, stderr=exc.stderr, timed_out=True,
        ) from exc
    except OSError as exc:
        raise _reported_process_failure(
            environment, returncode=None, stderr=str(exc), management_failed=True,
        ) from exc
    if result.returncode != 0:
        raise _reported_process_failure(
            environment, returncode=result.returncode, stderr=result.stderr,
        )
    progress = dict(
        line.split("=", 1)
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if "=" in line
    )
    try:
        frames = int(progress.get("frame", "0"))
        duration_us = int(progress.get("out_time_us", progress.get("out_time_ms", "0")))
    except ValueError:
        frames = duration_us = 0
    if progress.get("progress") != "end" or frames <= 0 or duration_us <= 0:
        raise _reported_process_failure(
            environment, returncode=result.returncode, stderr="decoded_video_invalid",
        )


def render_latentsync_video(
    source_video: Path,
    audio_path: Path,
    output_path: Path,
    *,
    python_path: Path,
    latentsync_root: Path,
    timeout_seconds: float | None = None,
    ffmpeg_path: Path | None = None,
    provider_cache_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
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
    source_environment = os.environ if environment is None else environment
    timeout_seconds = _latentsync_timeout_seconds(
        timeout_seconds,
        source_environment,
    )
    deadline = time.monotonic() + timeout_seconds
    cache_root = provider_cache_root
    if cache_root is None:
        cache_root = resolve_media_path(
            source_environment.get("OLIVIA_PROVIDER_CACHE_ROOT", ""),
            source_environment,
        )
    if cache_root is None:
        cache_root = output_path.parent.parent / "provider-cache"
    cache_root = Path(cache_root)
    if not cache_root.is_absolute():
        raise LatentSyncReplyError("LATENTSYNC_INPUT_UNAVAILABLE")
    work_root = cache_root / "latentsync-work"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="job-",
        dir=work_root,
        ignore_cleanup_errors=True,
    ) as temporary:
        temporary_root = Path(temporary)
        runtime_environment = _environment_with_ffmpeg(
            temporary_root,
            cache_root,
            ffmpeg_path=ffmpeg_path,
            environment=source_environment,
        )
        prepared_video = temporary_root / "source-h264.mp4"
        working_output = temporary_root / "reply.mp4"
        pipeline_temp = temporary_root / "pipeline-temp"
        _prepare_source_clip(
            source_video,
            audio_path,
            prepared_video,
            environment=runtime_environment,
            deadline=deadline,
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
            result = run_managed_process(
                command,
                cwd=latentsync_root,
                deadline=deadline,
                env=runtime_environment,
            )
        except subprocess.TimeoutExpired as exc:
            failure = _reported_process_failure(
                source_environment,
                returncode=None,
                stderr=exc.stderr,
                timed_out=True,
            )
            raise failure from exc
        except OSError as exc:
            failure = _reported_process_failure(
                source_environment,
                returncode=None,
                stderr=str(exc),
                management_failed=True,
            )
            raise failure from exc
        if result.returncode != 0:
            raise _reported_process_failure(
                source_environment, returncode=result.returncode, stderr=result.stderr,
            )
        if not working_output.is_file() or working_output.stat().st_size == 0:
            raise LatentSyncReplyError("LATENTSYNC_OUTPUT_MISSING")
        _validate_rendered_video(
            working_output,
            environment=runtime_environment,
            deadline=deadline,
        )
        partial_output = output_path.with_suffix(output_path.suffix + ".partial")
        try:
            shutil.copy2(working_output, partial_output)
            partial_output.replace(output_path)
        finally:
            with suppress(OSError):
                partial_output.unlink(missing_ok=True)
        metadata = {
            "visual_provider": "LatentSync-1.5",
            "inference_steps": 20,
            "guidance_scale": 1.5,
            "deepcache": True,
            "inference_profile": "stage2_efficient",
            "scene_source": "official_motion_video",
        }
    return metadata


__all__ = [
    "LatentSyncReplyError",
    "media_runtime_available",
    "render_latentsync_video",
    "resolve_ffmpeg_executable",
]
