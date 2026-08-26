"""Thin, external-only LiveTalking assembly adapter for B11.

This module owns configuration, provenance, health and process delegation only.
The actual visual inference remains in the fixed LiveTalking upstream runtime.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping, Sequence


LIVE_TALKING_SOURCE = "https://github.com/lipku/LiveTalking"
LIVE_TALKING_REVISION = "a97f01ba366e55eeed94e88d6bae38ed77b3a1b9"
LIVE_TALKING_LICENSE = "Apache-2.0"
REQUIRED_DEPENDENCIES = (
    "aiohttp_cors",
    "aiortc",
    "torch",
    "cv2",
    "numpy",
    "soundfile",
    "librosa",
    "scipy",
    "resampy",
    "tqdm",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_AVATAR_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_FRAME_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
_RESERVED_DEVICE_NAMES = frozenset(
    {
        "AUX",
        "CLOCK$",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "CON",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
        "NUL",
        "PRN",
    }
)


class LiveTalkingConfigError(ValueError):
    """Raised when an external LiveTalking reference is unsafe or incomplete."""


class LiveTalkingRuntimeError(RuntimeError):
    """Raised when the delegated LiveTalking process cannot be started safely."""


def _is_reserved_device_segment(part: str) -> bool:
    normalized = part.rstrip(" .")
    return normalized.split(".", 1)[0].upper() in _RESERVED_DEVICE_NAMES


def _external_path(value: Path | str) -> bool:
    candidate = PureWindowsPath(str(value))
    # Provider assets may live on any local Windows volume.  Keep the
    # reference-only boundary strict: no relative/drive-relative paths, UNC
    # shares, URLs, device paths, traversal, or a bare drive root.
    return (
        candidate.is_absolute()
        and bool(re.fullmatch(r"[A-Za-z]:", candidate.drive))
        and len(candidate.parts) > 1
        and not any(part in {".", ".."} for part in candidate.parts)
        and not any(_is_reserved_device_segment(part) for part in candidate.parts)
    )


def _path(value: Path | str, field: str) -> Path:
    raw = str(value)
    if raw.startswith("~") and (len(raw) == 1 or raw[1] in "/\\"):
        raise LiveTalkingConfigError(f"{field} must be an absolute local Windows path")
    result = Path(value).expanduser()
    if not _external_path(result):
        raise LiveTalkingConfigError(f"{field} must be an absolute local Windows path")
    return result


@dataclass(frozen=True)
class LiveTalkingConfig:
    """Reference-only configuration for one fixed LiveTalking candidate."""

    runtime_root: Path
    checkpoint_path: Path
    checkpoint_sha256: str
    avatar_payload: Path
    original_reference: Path
    work_root: Path
    python_executable: Path | None = None
    avatar_id: str = "b11_olivia"
    checkpoint_url: str = ""
    checkpoint_revision: str = ""
    checkpoint_license: str = ""
    upstream_source: str = LIVE_TALKING_SOURCE
    upstream_revision: str = LIVE_TALKING_REVISION
    upstream_license: str = LIVE_TALKING_LICENSE

    def __post_init__(self) -> None:
        for field in (
            "runtime_root",
            "checkpoint_path",
            "avatar_payload",
            "original_reference",
            "work_root",
        ):
            object.__setattr__(self, field, Path(getattr(self, field)))
        if self.python_executable is not None:
            object.__setattr__(self, "python_executable", Path(self.python_executable))

    def validate(self) -> None:
        for field in (
            "runtime_root",
            "checkpoint_path",
            "avatar_payload",
            "original_reference",
            "work_root",
        ):
            _path(getattr(self, field), field)
        if self.python_executable is not None:
            _path(self.python_executable, "python_executable")
        if not isinstance(self.checkpoint_sha256, str) or not _SHA256.fullmatch(self.checkpoint_sha256.lower()):
            raise LiveTalkingConfigError("checkpoint_sha256 must be a 64-character SHA-256")
        if not isinstance(self.avatar_id, str) or not _AVATAR_ID.fullmatch(self.avatar_id):
            raise LiveTalkingConfigError("avatar_id is invalid")
        expected_payload = (PureWindowsPath(str(self.runtime_root)) / "data" / "avatars" / self.avatar_id).as_posix().casefold()
        actual_payload = PureWindowsPath(str(self.avatar_payload)).as_posix().casefold()
        if actual_payload != expected_payload:
            raise LiveTalkingConfigError("avatar_payload must be runtime_root/data/avatars/avatar_id")
        if not isinstance(self.checkpoint_url, str) or not self.checkpoint_url.startswith(("https://", "http://")):
            raise LiveTalkingConfigError("checkpoint_url must be an HTTP(S) provenance URL")
        if not isinstance(self.checkpoint_revision, str) or not self.checkpoint_revision.strip():
            raise LiveTalkingConfigError("checkpoint_revision is required")
        if not isinstance(self.checkpoint_license, str) or not self.checkpoint_license.strip():
            raise LiveTalkingConfigError("checkpoint_license is required")
        if not isinstance(self.upstream_source, str) or not self.upstream_source.startswith("https://github.com/"):
            raise LiveTalkingConfigError("upstream_source must point to the official GitHub upstream")
        if not _REVISION.fullmatch(str(self.upstream_revision).lower()):
            raise LiveTalkingConfigError("upstream_revision must be a full fixed revision")
        if self.upstream_license != LIVE_TALKING_LICENSE:
            raise LiveTalkingConfigError("upstream_license must preserve the LiveTalking license")

    def python(self) -> Path:
        return self.python_executable or (self.runtime_root / "venv" / "Scripts" / "python.exe")


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise LiveTalkingRuntimeError("DELEGATE_CANCELLED")


def _sha256(path: Path, *, cancel_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _raise_if_cancelled(cancel_event)
            digest.update(chunk)
    return digest.hexdigest()


def _has_frame_files(path: Path) -> bool:
    try:
        return any(item.is_file() and item.suffix.lower() in _FRAME_SUFFIXES for item in path.iterdir())
    except OSError:
        return False


def _dependency_status(
    config: LiveTalkingConfig,
    *,
    cancel_event: threading.Event | None = None,
    process_callback: Callable[[Any | None], None] | None = None,
) -> dict[str, bool]:
    _raise_if_cancelled(cancel_event)
    executable = config.python()
    if not executable.is_file():
        return {name: False for name in REQUIRED_DEPENDENCIES}
    script = (
        "import importlib.util, json; "
        f"names={list(REQUIRED_DEPENDENCIES)!r}; "
        "print(json.dumps({name: importlib.util.find_spec(name) is not None for name in names}))"
    )
    process = None
    try:
        process = subprocess.Popen(
            [str(executable), "-c", script],
            cwd=str(config.work_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if process_callback is not None:
            process_callback(process)
        deadline = time.monotonic() + 30.0
        while True:
            _raise_if_cancelled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_delegated_process(process)
                return {name: False for name in REQUIRED_DEPENDENCIES}
            try:
                stdout, _stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        _raise_if_cancelled(cancel_event)
        parsed = json.loads(stdout.strip()) if process.returncode == 0 else {}
        return {name: bool(parsed.get(name, False)) for name in REQUIRED_DEPENDENCIES}
    except LiveTalkingRuntimeError:
        if process is not None:
            _stop_delegated_process(process)
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError):
        return {name: False for name in REQUIRED_DEPENDENCIES}
    finally:
        if process_callback is not None:
            process_callback(None)


DependencyProbe = Callable[[LiveTalkingConfig], Mapping[str, bool]]


def runtime_health(
    config: LiveTalkingConfig,
    *,
    dependency_probe: DependencyProbe | None = None,
    cancel_event: threading.Event | None = None,
    process_callback: Callable[[Any | None], None] | None = None,
) -> dict[str, Any]:
    """Return a truthful, read-only readiness report for the external candidate."""

    health: dict[str, Any] = {
        "schema_version": "b11.livetalking.health.v1",
        "status": "UNAVAILABLE",
        "ready": False,
        "provider": "LiveTalking",
        "reason_codes": [],
        "external_assets_copied": False,
        "generated_media_committed": False,
        "network_called": False,
        "provenance": {
            "source": config.upstream_source,
            "upstream_revision": config.upstream_revision,
            "upstream_license": config.upstream_license,
            "checkpoint_url": config.checkpoint_url,
            "checkpoint_revision": config.checkpoint_revision,
            "checkpoint_license": config.checkpoint_license,
            "checkpoint_sha256": config.checkpoint_sha256,
        },
        "dependencies": {},
        "assets": {},
    }
    try:
        config.validate()
    except LiveTalkingConfigError:
        health["reason_codes"] = ["CONFIG_INVALID"]
        health["reason"] = health["reason_codes"][0]
        return health
    _raise_if_cancelled(cancel_event)

    asset_checks = {
        "runtime_root": config.runtime_root.is_dir(),
        "checkpoint_path": config.checkpoint_path.is_file(),
        "avatar_payload": (
            config.avatar_payload.is_dir()
            and (config.avatar_payload / "full_imgs").is_dir()
            and (config.avatar_payload / "face_imgs").is_dir()
            and (config.avatar_payload / "coords.pkl").is_file()
            and _has_frame_files(config.avatar_payload / "full_imgs")
            and _has_frame_files(config.avatar_payload / "face_imgs")
        ),
        "original_reference": config.original_reference.is_file(),
        "work_root": config.work_root.is_dir(),
    }
    health["assets"] = {
        "runtime_root": {"path": str(config.runtime_root), "present": asset_checks["runtime_root"]},
        "checkpoint_path": {
            "path": str(config.checkpoint_path),
            "present": asset_checks["checkpoint_path"],
            "sha256": (
                _sha256(config.checkpoint_path, cancel_event=cancel_event)
                if asset_checks["checkpoint_path"]
                else None
            ),
        },
        "avatar_payload": {"path": str(config.avatar_payload), "complete": asset_checks["avatar_payload"]},
        "original_reference": {"path": str(config.original_reference), "present": asset_checks["original_reference"]},
        "work_root": {"path": str(config.work_root), "present": asset_checks["work_root"]},
    }
    reasons: list[str] = []
    if not asset_checks["runtime_root"]:
        reasons.append("RUNTIME_ROOT_MISSING")
    if not asset_checks["checkpoint_path"]:
        reasons.append("CHECKPOINT_MISSING")
    elif health["assets"]["checkpoint_path"]["sha256"].lower() != config.checkpoint_sha256.lower():
        reasons.append("CHECKPOINT_HASH_MISMATCH")
    if not asset_checks["avatar_payload"]:
        reasons.append("AVATAR_PAYLOAD_INCOMPLETE")
    if not asset_checks["original_reference"]:
        reasons.append("ORIGINAL_REFERENCE_MISSING")
    if not asset_checks["work_root"]:
        reasons.append("WORK_ROOT_MISSING")

    _raise_if_cancelled(cancel_event)
    if dependency_probe is None:
        dependencies = dict(
            _dependency_status(
                config,
                cancel_event=cancel_event,
                process_callback=process_callback,
            )
        )
    else:
        dependencies = dict(dependency_probe(config))
        _raise_if_cancelled(cancel_event)
    health["dependencies"] = {name: bool(dependencies.get(name, False)) for name in REQUIRED_DEPENDENCIES}
    if not all(health["dependencies"].values()):
        reasons.append("DEPENDENCY_MISSING")
        health["missing_dependencies"] = [name for name, present in health["dependencies"].items() if not present]

    health["reason_codes"] = reasons
    if not reasons:
        health["status"] = "HEALTHY"
        health["ready"] = True
    elif reasons:
        health["reason"] = reasons[0]
    return health


def build_worker_command(
    config: LiveTalkingConfig,
    *,
    audio_path: Path | str,
    output_dir: Path | str,
    frame_indices: Sequence[int],
    worker_path: Path | str,
) -> list[str]:
    """Build a no-install/no-download command that delegates to the pinned upstream."""

    config.validate()
    audio = _path(audio_path, "audio_path")
    output = _path(output_dir, "output_dir")
    worker = _path(worker_path, "worker_path")
    indices = tuple(frame_indices)
    if not indices or any(not isinstance(index, int) or index < 0 for index in indices):
        raise LiveTalkingConfigError("frame_indices must contain non-negative integers")
    return [
        str(config.python()),
        str(worker),
        "--runtime-root",
        str(config.runtime_root),
        "--checkpoint",
        str(config.checkpoint_path),
        "--checkpoint-sha256",
        config.checkpoint_sha256,
        "--avatar-payload",
        str(config.avatar_payload),
        "--avatar-id",
        config.avatar_id,
        "--work-root",
        str(config.work_root),
        "--original-reference",
        str(config.original_reference),
        "--audio",
        str(audio),
        "--output-dir",
        str(output),
        "--frame-indices",
        ",".join(str(index) for index in indices),
        "--upstream-revision",
        config.upstream_revision,
    ]


def capture_candidate_frames(
    config: LiveTalkingConfig,
    *,
    audio_path: Path | str,
    output_dir: Path | str,
    frame_indices: Sequence[int],
    worker_path: Path | str,
    dependency_probe: DependencyProbe | None = None,
    runner: Callable[..., Any] | None = None,
    cancel_event: threading.Event | None = None,
    process_callback: Callable[[Any | None], None] | None = None,
    process_factory: Callable[..., Any] = subprocess.Popen,
    timeout_seconds: float = 900.0,
) -> list[Path]:
    """Start the delegated worker only after the candidate is fully ready."""

    _raise_if_cancelled(cancel_event)
    health = runtime_health(
        config,
        dependency_probe=dependency_probe,
        cancel_event=cancel_event,
        process_callback=process_callback,
    )
    if health["status"] != "HEALTHY":
        raise LiveTalkingRuntimeError(f"RUNTIME_NOT_READY: {','.join(health['reason_codes'])}")
    audio = _path(audio_path, "audio_path")
    if not audio.is_file():
        raise LiveTalkingRuntimeError("AUDIO_MISSING")
    output = _path(output_dir, "output_dir")
    output.mkdir(parents=True, exist_ok=True)
    command = build_worker_command(
        config,
        audio_path=audio,
        output_dir=output,
        frame_indices=frame_indices,
        worker_path=worker_path,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise LiveTalkingRuntimeError("DELEGATE_CANCELLED")
    if runner is not None:
        try:
            result = runner(
                command,
                cwd=str(config.work_root),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LiveTalkingRuntimeError("DELEGATE_START_FAILED") from exc
    else:
        process = None
        try:
            process = process_factory(
                command,
                cwd=str(config.work_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if process_callback is not None:
                process_callback(process)
            deadline = time.monotonic() + timeout_seconds
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    _stop_delegated_process(process)
                    raise LiveTalkingRuntimeError("DELEGATE_CANCELLED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_delegated_process(process)
                    raise LiveTalkingRuntimeError("DELEGATE_TIMEOUT")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if cancel_event is not None and cancel_event.is_set():
                raise LiveTalkingRuntimeError("DELEGATE_CANCELLED")
            result = type(
                "DelegateResult",
                (),
                {"returncode": process.returncode, "stdout": stdout, "stderr": stderr},
            )()
        except LiveTalkingRuntimeError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            if process is not None:
                _stop_delegated_process(process)
            raise LiveTalkingRuntimeError("DELEGATE_START_FAILED") from exc
        finally:
            if process_callback is not None:
                process_callback(None)
    if getattr(result, "returncode", 1) != 0:
        raise LiveTalkingRuntimeError("DELEGATE_FAILED")
    expected = [output / f"frame_{index:04d}.png" for index in frame_indices]
    missing = [path for path in expected if not path.is_file()]
    if missing:
        raise LiveTalkingRuntimeError("FRAME_OUTPUT_MISSING")
    return expected


def _stop_delegated_process(process: Any, *, timeout_seconds: float = 1.0) -> None:
    """Terminate one owned worker, escalating to kill after a short bound."""

    try:
        if getattr(process, "poll", lambda: None)() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout_seconds)
    except (OSError, subprocess.SubprocessError):
        return
