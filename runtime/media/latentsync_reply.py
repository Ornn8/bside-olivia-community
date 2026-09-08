"""Thin adapter for the accepted LatentSync 1.5 video-reply route."""

from __future__ import annotations

from contextlib import suppress
import json
import logging
import math
import os
import posixpath
import re
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

_FAILURE_PHASES = frozenset({"source_prepare", "inference", "output_validate"})
_INPUT_NAMES = frozenset({"video", "audio", "output_parent"})
_MISSING_COMPONENTS = frozenset({
    "unknown", "video", "audio", "output_parent", "ffmpeg", "ffprobe", "scheduler",
    "unet_config", "unet_weights", "whisper_weights", "whisper_mel_filters",
    "face_detection", "face_landmarks", "mask_image", "vae_config", "vae_weights", "runtime_temp",
})


def project_failure_context(source: Mapping[str, object]) -> dict[str, object]:
    """Only fixed labels and actual booleans may leave the machine."""
    result: dict[str, object] = {}
    if isinstance(source.get("phase"), str) and source["phase"] in _FAILURE_PHASES:
        result["phase"] = source["phase"]
    component = source.get("missing_component")
    if isinstance(component, str) and component in _MISSING_COMPONENTS:
        result["missing_component"] = component
    inputs = source.get("inputs")
    if isinstance(inputs, Mapping):
        projected = {}
        for name in _INPUT_NAMES:
            value = inputs.get(name)
            if isinstance(value, Mapping):
                fields = {key: value[key] for key in ("exists", "readable") if type(value.get(key)) is bool}
                if fields:
                    projected[name] = fields
        if projected:
            result["inputs"] = projected
    exception_type = source.get("exception_type")
    if isinstance(exception_type, str) and exception_type in {"FileNotFoundError", "PermissionError", "RuntimeError", "OSError", "ValueError", "ImportError", "ModuleNotFoundError"}:
        result["exception_type"] = exception_type
    frames = source.get("frames")
    if isinstance(frames, list):
        safe_frames = []
        for frame in frames[-8:]:
            if not isinstance(frame, Mapping):
                continue
            module, line = frame.get("module"), frame.get("line")
            if isinstance(module, str) and len(module) <= 160 and re.fullmatch(r"(?:latentsync|scripts|ffmpeg)(?:\.[A-Za-z_][A-Za-z_0-9]*)+|subprocess", module) and type(line) is int and 0 < line < 100000:
                safe_frames.append({"module": module, "line": line})
        if safe_frames:
            result["frames"] = safe_frames
    return result


def _failure_context(phase: str, stderr: bytes | str | None, paths: Mapping[str, Path]) -> dict[str, object]:
    inputs = {}
    for name in _INPUT_NAMES:
        path = paths.get(name)
        if path is None:
            continue
        exists = readable = False
        try:
            exists = path.is_dir() if name == "output_parent" else path.is_file()
            if name == "output_parent":
                readable = exists and os.access(path, os.R_OK)
            elif exists:
                with path.open("rb"):
                    readable = True
        except OSError:
            pass
        inputs[name] = {"exists": exists, "readable": readable}
    result: dict[str, object] = {"phase": phase, "inputs": inputs}
    raw = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
    # Match only missing-file error lines, never arbitrary traceback source text.
    lines = [line for line in raw[-65536:].splitlines() if "filenotfounderror" in line.casefold() or "no such file" in line.casefold()]
    if lines:
        component = "unknown"
        normalize = lambda value: posixpath.normpath(value.replace("\\", "/")).casefold().rstrip("/")
        known = {normalize(str(path)): name for name, path in paths.items()}
        known.update({'ffmpeg': 'ffmpeg', 'ffmpeg.exe': 'ffmpeg'})
        if 'runtime_root' in paths:
            for relative, name in (
                ('checkpoints/whisper/tiny.pt', 'whisper_weights'),
                ('checkpoints/whisper/small.pt', 'whisper_weights'),
                ('stabilityai/sd-vae-ft-mse/diffusion_pytorch_model.bin', 'vae_weights'),
            ):
                known[normalize(str(paths['runtime_root'] / relative))] = name
        for candidate in re.findall(r"['\"]([^'\"]+)['\"]", lines[-1]):
            label = known.get(normalize(candidate))
            if label is None and "runtime_root" in paths:
                label = known.get(normalize(str(paths["runtime_root"] / candidate)))
                resolved = normalize(str(paths["runtime_root"] / candidate))
                temp_root = normalize(str(paths["runtime_root"] / "temp"))
                if resolved == temp_root or resolved.startswith(temp_root + "/"):
                    label = "runtime_temp"
            if label in _MISSING_COMPONENTS:
                component = label
        result["missing_component"] = component
    for line in raw[-65536:].splitlines():
        if line.startswith("OLIVIA_LATENTSYNC_FAILURE=") and len(line) <= 4096:
            try:
                worker = json.loads(line.partition("=")[2])
            except ValueError:
                continue
            if isinstance(worker, Mapping):
                safe = project_failure_context(worker)
                result.update({key: value for key, value in safe.items() if key in {"frames", "exception_type", "missing_component"}})
    return project_failure_context(result)


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
    phase: str = "inference", paths: Mapping[str, Path] | None = None,
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
            record.update(_failure_context(phase, stderr, paths or {}))
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
    """Check the configured FFmpeg used by complete delivery."""

    try:
        resolve_ffmpeg_executable(env)
        return True
    except (RuntimeError, OSError, LatentSyncReplyError):
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

    paths = {"video": source_video, "audio": audio_path, "output_parent": prepared_video.parent}

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
            phase="source_prepare", paths=paths,
        ) from exc
    except OSError as exc:
        raise _reported_process_failure(
            environment, returncode=None, stderr=str(exc), management_failed=True,
            phase="source_prepare", paths=paths,
        ) from exc
    if result.returncode != 0:
        raise _reported_process_failure(
            environment, returncode=result.returncode, stderr=result.stderr,
            phase="source_prepare", paths=paths,
        )
    if not prepared_video.is_file():
        raise LatentSyncReplyError("LATENTSYNC_SOURCE_PREPARE_FAILED")


def _validate_rendered_video(
    video_path: Path,
    *,
    environment: dict[str, str],
    deadline: float,
) -> None:
    paths = {"video": video_path, "output_parent": video_path.parent}
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
            phase="output_validate", paths=paths,
        ) from exc
    except OSError as exc:
        raise _reported_process_failure(
            environment, returncode=None, stderr=str(exc), management_failed=True,
            phase="output_validate", paths=paths,
        ) from exc
    if result.returncode != 0:
        raise _reported_process_failure(
            environment, returncode=result.returncode, stderr=result.stderr,
            phase="output_validate", paths=paths,
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
            phase="output_validate", paths=paths,
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
            str(Path(__file__).resolve().parents[2] / "tools/latentsync_diagnostic_worker.py"),
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
        diagnostic_paths = {
            "video": prepared_video, "audio": audio_path,
            "output_parent": working_output.parent, "runtime_root": latentsync_root,
            "unet_config": config_path, "unet_weights": checkpoint_path,
            "ffmpeg": Path(shutil.which("ffmpeg", path=runtime_environment["PATH"]) or "ffmpeg"),
            "scheduler": latentsync_root / "configs/scheduler_config.json",
            "mask_image": latentsync_root / "latentsync/utils/mask.png",
            "whisper_mel_filters": latentsync_root / "latentsync/whisper/whisper/assets/mel_filters.npz",
            "face_detection": latentsync_root / "checkpoints/auxiliary/models/buffalo_l/det_10g.onnx",
            "face_landmarks": latentsync_root / "checkpoints/auxiliary/models/buffalo_l/2d106det.onnx",
            "vae_config": latentsync_root / "stabilityai/sd-vae-ft-mse/config.json",
            "vae_weights": latentsync_root / "stabilityai/sd-vae-ft-mse/diffusion_pytorch_model.safetensors",
        }
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
                paths=diagnostic_paths,
            )
            raise failure from exc
        except OSError as exc:
            failure = _reported_process_failure(
                source_environment,
                returncode=None,
                stderr=str(exc),
                management_failed=True,
                paths=diagnostic_paths,
            )
            raise failure from exc
        if result.returncode != 0:
            raise _reported_process_failure(
                source_environment, returncode=result.returncode, stderr=result.stderr,
                paths=diagnostic_paths,
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
