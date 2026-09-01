"""Resumable, verified installer for optional ordinary and music video bundles."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

from installer.component_update import (
    ComponentUpdateError,
    _is_reparse_point,
    _validate_relative_path,
    _verify_staged_tree,
)
from runtime.media.managed_voice_reference import (
    ManagedVoiceReferenceError,
    resolve_managed_voice_reference,
    resolve_managed_voice_reference_transcript,
)


_SHA256 = 64
_PUBLIC_BUNDLES = {"ordinary_video", "music_video"}
_SOURCE_MODES = {"auto", "official"}
_SOURCE_IDS = {"domestic", "official"}
_RUNTIME_ENVIRONMENT_FILE = "runtime-environment.json"
_RUNTIME_ENVIRONMENT_KEYS = {
    "OLIVIA_FFMPEG_EXE",
    "OLIVIA_COSYVOICE_ROOT",
    "OLIVIA_COSYVOICE_PYTHON",
    "OLIVIA_COSYVOICE_MODEL_ROOT",
    "OLIVIA_BREEZE_TTS_ROOT",
    "OLIVIA_BREEZE_TTS_PYTHON",
    "OLIVIA_BREEZE_TTS_MODEL_ROOT",
    "OLIVIA_BREEZE_TTS_MODEL_LICENSE",
    "OLIVIA_LATENTSYNC_PYTHON",
    "OLIVIA_LATENTSYNC_ROOT",
    "OLIVIA_MINIMAX_COMFY_PYTHON",
    "OLIVIA_MINIMAX_COMFY_ROOT",
    "OLIVIA_MINIMAX_WORKER",
    "OLIVIA_ROFORMER_EXE",
    "OLIVIA_ROFORMER_PYTHON",
    "OLIVIA_ROFORMER_MODEL_PATH",
    "OLIVIA_ROFORMER_CONFIG_PATH",
    "OLIVIA_SEED_VC_ROOT",
    "OLIVIA_TTS_CONFIG",
    "OLIVIA_TTS_QUALITY_GATE_CACHE_ROOT",
    "OLIVIA_ORDINARY_ACTION_BASE",
    "OLIVIA_OFFICIAL_REPLY_REFERENCE",
    "OLIVIA_MUSIC_PERFORMANCE_BASE",
    "OLIVIA_REPLY_VOICE_REFERENCE",
    "OLIVIA_PROVIDER_CACHE_ROOT",
}
_PORTABLE_RUNTIME_ENVIRONMENT_KEYS = frozenset(("OLIVIA_BREEZE_TTS_PYTHON", "OLIVIA_LATENTSYNC_PYTHON", "OLIVIA_MINIMAX_COMFY_PYTHON", "OLIVIA_ROFORMER_PYTHON"))
_MAX_ARCHIVE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_RUNTIME_ARCHIVE_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
_RUNTIME_PORTABILITY_TIMEOUT_SECONDS = 20.0
_RUNTIME_MANIFEST_MAX_WORKERS = 8
_RUNTIME_MANIFEST_PENDING_PER_WORKER = 2
_RUNTIME_HOST_UNAVAILABLE = "VIDEO_RUNTIME_HOST_UNAVAILABLE"
_RUNTIME_IMPORT_CACHE = ".video-runtime-import-cache"
_RUNTIME_IMPORT_CHECKPOINT = ".runtime-import-checkpoint.json"
_RUNTIME_HOST_DEPENDENCIES = frozenset(
    {"breeze_tts2", "latentsync", "minimax_music3", "roformer"}
)
_SEED_VC_PATCH_SHA256 = "f61ffb5193514ee3e34a439ebcd89c6168cf4bdb6a8d960513ee471d8840f2a6"
_BREEZE_MINIMUM_VRAM_MIB = 10 * 1024
_BREEZE_RUNTIME_REQUIREMENTS = "installer/breeze-runtime-requirements.txt"
_BREEZE_RUNTIME_REQUIREMENTS_SHA256 = (
    "efc8292ae94e7ec7d7eb3c2d3430c9bd666638c7bacec75d595145c78f08a4cd"
)
_BREEZE_RUNTIME_MARKER = ".olivia-breeze-runtime.json"
_PROMOTION_LOCK = threading.RLock()


class VideoCapabilityError(ValueError):
    """Raised for invalid manifests or unsafe installation inputs."""


class _RuntimeHostUnavailable(RuntimeError):
    """The portable runtime process could not start on this host."""


def _unavailable_breeze_hardware(
    reason_code: str,
    *,
    vendor: str = "unknown",
    detected_vram_mib: int | None = None,
) -> dict[str, object]:
    return {
        "status": "UNAVAILABLE",
        "vendor": vendor,
        "minimum_vram_mib": _BREEZE_MINIMUM_VRAM_MIB,
        "detected_vram_mib": detected_vram_mib,
        "reason_code": reason_code,
    }


def _probe_breeze_hardware() -> dict[str, object]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return _unavailable_breeze_hardware("BREEZE_TTS_NVIDIA_GPU_REQUIRED")
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = [
            int(line.strip())
            for line in completed.stdout.splitlines()
            if line.strip().isdigit()
        ]
    except (OSError, ValueError, subprocess.SubprocessError):
        values = []
        completed = None
    if completed is None or completed.returncode != 0 or not values:
        return _unavailable_breeze_hardware(
            "BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED", vendor="NVIDIA"
        )
    # Product inference uses CUDA's default device, so eligibility must be
    # based on GPU 0 rather than a larger secondary adapter.
    detected = values[0]
    if detected < _BREEZE_MINIMUM_VRAM_MIB:
        return _unavailable_breeze_hardware(
            "BREEZE_TTS_10GB_VRAM_REQUIRED",
            vendor="NVIDIA",
            detected_vram_mib=detected,
        )
    return {
        "status": "READY",
        "vendor": "NVIDIA",
        "minimum_vram_mib": _BREEZE_MINIMUM_VRAM_MIB,
        "detected_vram_mib": detected,
        "reason_code": None,
    }


def _validated_breeze_hardware(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return _unavailable_breeze_hardware("BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED")
    expected = {
        "status",
        "vendor",
        "minimum_vram_mib",
        "detected_vram_mib",
        "reason_code",
    }
    if (
        set(value) != expected
        or value.get("status") not in {"READY", "UNAVAILABLE"}
        or value.get("vendor") not in {"NVIDIA", "unknown"}
        or value.get("minimum_vram_mib") != _BREEZE_MINIMUM_VRAM_MIB
        or (
            value.get("detected_vram_mib") is not None
            and type(value.get("detected_vram_mib")) is not int
        )
        or (
            value.get("status") == "READY"
            and (
                value.get("vendor") != "NVIDIA"
                or int(value.get("detected_vram_mib") or 0)
                < _BREEZE_MINIMUM_VRAM_MIB
                or value.get("reason_code") is not None
            )
        )
        or (
            value.get("status") == "UNAVAILABLE"
            and not isinstance(value.get("reason_code"), str)
        )
    ):
        return _unavailable_breeze_hardware("BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED")
    return dict(value)


def _breeze_reference_text(
    environment: Mapping[str, str], data_root: Path
) -> str | None:
    """Recover the private exact transcript without logging or redistributing it."""

    configured = environment.get("OLIVIA_TTS_CONFIG")
    if configured:
        try:
            path = Path(configured)
            if path.is_file() and path.stat().st_size <= 1024 * 1024:
                payload = json.loads(path.read_text(encoding="utf-8"))
                settings = payload.get("settings") if isinstance(payload, dict) else None
                value = settings.get("reference_text") if isinstance(settings, dict) else None
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    try:
        return resolve_managed_voice_reference_transcript(data_root)
    except ManagedVoiceReferenceError:
        pass
    value = os.environ.get("OLIVIA_TTS_REFERENCE_TEXT")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class VideoCapabilityState(StrEnum):
    MISSING = "missing"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    READY = "ready"
    PAUSED = "paused"
    FAILED = "failed"
    LICENSE_REVIEW_REQUIRED = "license_review_required"
    PREREQUISITES_REQUIRED = "prerequisites_required"


@dataclass(frozen=True)
class VideoFileInstall:
    kind: str
    destination: str
    strip_components: int = 0


@dataclass(frozen=True)
class VideoRuntimePatch:
    identifier: str
    relative_path: str
    target_path: str
    sha256: str


@dataclass(frozen=True)
class VideoRuntimeArtifact:
    identifier: str
    part_ids: tuple[str, ...]
    archive_size_bytes: int
    archive_sha256: str
    destination: str
    strip_components: int = 0


@dataclass(frozen=True)
class VideoFile:
    identifier: str
    relative_path: str
    size_bytes: int
    sha256: str
    license: str
    sources: Mapping[str, str]
    redistributable: bool = True
    install: VideoFileInstall | None = None

    @property
    def id(self) -> str:
        return self.identifier


@dataclass(frozen=True)
class VideoBundle:
    identifier: str
    label: str
    status: str
    requires_gpu: bool
    dependencies: tuple[str, ...]
    files: tuple[VideoFile, ...]
    license_review_required: bool = False
    runtime_environment: Mapping[str, str] | None = None
    runtime_patches: tuple[VideoRuntimePatch, ...] = ()
    runtime_artifacts: tuple[VideoRuntimeArtifact, ...] = ()

    @property
    def id(self) -> str:
        return self.identifier


@dataclass(frozen=True)
class VideoManifest:
    version: str
    bundles: tuple[VideoBundle, ...]


@dataclass(frozen=True)
class VideoBundleStatus:
    bundle: str
    state: VideoCapabilityState
    downloaded_bytes: int
    total_bytes: int
    current_file: str | None = None
    source: str | None = None
    reason_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "id": self.bundle,
            "state": self.state.value,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "remaining_bytes": max(0, self.total_bytes - self.downloaded_bytes),
        }
        if self.current_file:
            value["current_file"] = self.current_file
        if self.source:
            value["source"] = self.source
        if self.reason_code:
            value["reason_code"] = self.reason_code
        return value


def _safe_relative(value: object) -> str:
    try:
        return _validate_relative_path(value)
    except ComponentUpdateError as exc:
        raise VideoCapabilityError("VIDEO_MANIFEST_PATH_INVALID") from exc


def _safe_sha(value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256:
        raise VideoCapabilityError("VIDEO_MANIFEST_SHA256_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise VideoCapabilityError("VIDEO_MANIFEST_SHA256_INVALID") from exc
    return value.lower()


def _safe_url(value: object) -> str:
    if not isinstance(value, str):
        raise VideoCapabilityError("VIDEO_MANIFEST_SOURCE_INVALID")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise VideoCapabilityError("VIDEO_MANIFEST_SOURCE_INVALID")
    if parsed.query or parsed.fragment:
        raise VideoCapabilityError("VIDEO_MANIFEST_SOURCE_INVALID")
    return value


def _load_file(raw: object) -> VideoFile:
    if not isinstance(raw, dict):
        raise VideoCapabilityError("VIDEO_MANIFEST_FILE_INVALID")
    required = {"id", "path", "size_bytes", "sha256", "license", "sources"}
    if set(raw) - required - {"redistributable", "install"} or not required.issubset(raw):
        raise VideoCapabilityError("VIDEO_MANIFEST_FILE_INVALID")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise VideoCapabilityError("VIDEO_MANIFEST_FILE_INVALID")
    size = raw.get("size_bytes")
    if type(size) is not int or size < 0:
        raise VideoCapabilityError("VIDEO_MANIFEST_FILE_INVALID")
    license_name = raw.get("license")
    if not isinstance(license_name, str) or not license_name.strip():
        raise VideoCapabilityError("VIDEO_MANIFEST_FILE_INVALID")
    sources = raw.get("sources")
    if not isinstance(sources, dict) or set(sources) - _SOURCE_IDS:
        raise VideoCapabilityError("VIDEO_MANIFEST_SOURCE_INVALID")
    normalized_sources = {key: _safe_url(value) for key, value in sources.items()}
    install = raw.get("install")
    parsed_install = None
    if install is not None:
        if (
            not isinstance(install, dict)
            or set(install) != {"kind", "destination", "strip_components"}
            or install.get("kind") != "zip"
            or type(install.get("strip_components")) is not int
            or not 0 <= install["strip_components"] <= 4
        ):
            raise VideoCapabilityError("VIDEO_MANIFEST_INSTALL_INVALID")
        parsed_install = VideoFileInstall(
            "zip",
            _safe_relative(install.get("destination")),
            install["strip_components"],
        )
    return VideoFile(
        identifier,
        _safe_relative(raw.get("path")),
        size,
        _safe_sha(raw.get("sha256")),
        license_name.strip(),
        normalized_sources,
        raw.get("redistributable", True) is True,
        parsed_install,
    )


def _load_runtime_patch(raw: object) -> VideoRuntimePatch:
    if not isinstance(raw, dict) or set(raw) != {"id", "path", "target", "sha256"}:
        raise VideoCapabilityError("VIDEO_MANIFEST_PATCH_INVALID")
    identifier = raw.get("id")
    if (
        not isinstance(identifier, str)
        or not identifier
        or "/" in identifier
        or "\\" in identifier
    ):
        raise VideoCapabilityError("VIDEO_MANIFEST_PATCH_INVALID")
    return VideoRuntimePatch(
        identifier,
        _safe_relative(raw.get("path")),
        _safe_relative(raw.get("target")),
        _safe_sha(raw.get("sha256")),
    )


def _load_runtime_artifact(raw: object) -> VideoRuntimeArtifact:
    required = {
        "id",
        "part_ids",
        "archive_size_bytes",
        "archive_sha256",
        "destination",
        "strip_components",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise VideoCapabilityError("VIDEO_MANIFEST_RUNTIME_ARTIFACT_INVALID")
    identifier = raw.get("id")
    part_ids = raw.get("part_ids")
    size = raw.get("archive_size_bytes")
    strip_components = raw.get("strip_components")
    if (
        not isinstance(identifier, str)
        or not identifier
        or "/" in identifier
        or "\\" in identifier
        or not isinstance(part_ids, list)
        or not part_ids
        or not all(isinstance(item, str) and item for item in part_ids)
        or len(set(part_ids)) != len(part_ids)
        or type(size) is not int
        or size <= 0
        or type(strip_components) is not int
        or not 0 <= strip_components <= 4
    ):
        raise VideoCapabilityError("VIDEO_MANIFEST_RUNTIME_ARTIFACT_INVALID")
    return VideoRuntimeArtifact(
        identifier=identifier,
        part_ids=tuple(part_ids),
        archive_size_bytes=size,
        archive_sha256=_safe_sha(raw.get("archive_sha256")),
        destination=_safe_relative(raw.get("destination")),
        strip_components=strip_components,
    )


def load_video_manifest(path: Path) -> VideoManifest:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoCapabilityError("VIDEO_MANIFEST_UNAVAILABLE") from exc
    if not isinstance(payload, dict) or set(payload) - {"schema_version", "version", "bundles", "provenance"}:
        raise VideoCapabilityError("VIDEO_MANIFEST_INVALID")
    bundles = payload.get("bundles")
    if payload.get("schema_version") != "olivia.video-capability-bom.v1" or not isinstance(payload.get("version"), str) or not isinstance(bundles, list):
        raise VideoCapabilityError("VIDEO_MANIFEST_INVALID")
    result: list[VideoBundle] = []
    seen: set[str] = set()
    for raw in bundles:
        if not isinstance(raw, dict):
            raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
        required = {"id", "label", "status", "requires_gpu", "dependencies", "files"}
        if set(raw) - required - {"license_review_required", "runtime_environment", "runtime_patches", "runtime_artifacts"} or not required.issubset(raw):
            raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
        identifier = raw.get("id")
        if identifier not in _PUBLIC_BUNDLES or identifier in seen:
            raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
        dependencies = raw.get("dependencies")
        files = raw.get("files")
        if not isinstance(raw.get("label"), str) or raw.get("status") != "FIXED" or type(raw.get("requires_gpu")) is not bool or not isinstance(dependencies, list) or not all(isinstance(item, str) and item for item in dependencies) or not isinstance(files, list):
            raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
        parsed_files = tuple(_load_file(item) for item in files)
        if len({item.identifier for item in parsed_files}) != len(parsed_files) or len({item.relative_path.casefold() for item in parsed_files}) != len(parsed_files):
            raise VideoCapabilityError("VIDEO_MANIFEST_FILE_INVALID")
        runtime_environment = raw.get("runtime_environment", {})
        if (
            not isinstance(runtime_environment, dict)
            or set(runtime_environment) - _RUNTIME_ENVIRONMENT_KEYS
            or not all(isinstance(key, str) for key in runtime_environment)
        ):
            raise VideoCapabilityError("VIDEO_MANIFEST_RUNTIME_INVALID")
        normalized_runtime = {
            key: _safe_relative(value) for key, value in runtime_environment.items()
        }
        runtime_patches = raw.get("runtime_patches", [])
        if not isinstance(runtime_patches, list):
            raise VideoCapabilityError("VIDEO_MANIFEST_PATCH_INVALID")
        parsed_patches = tuple(_load_runtime_patch(item) for item in runtime_patches)
        if (
            len({item.identifier for item in parsed_patches}) != len(parsed_patches)
            or len({item.target_path.casefold() for item in parsed_patches})
            != len(parsed_patches)
        ):
            raise VideoCapabilityError("VIDEO_MANIFEST_PATCH_INVALID")
        runtime_artifacts = raw.get("runtime_artifacts", [])
        if not isinstance(runtime_artifacts, list):
            raise VideoCapabilityError("VIDEO_MANIFEST_RUNTIME_ARTIFACT_INVALID")
        parsed_runtime_artifacts = tuple(
            _load_runtime_artifact(item) for item in runtime_artifacts
        )
        file_by_id = {item.identifier: item for item in parsed_files}
        runtime_part_ids = tuple(
            part_id
            for artifact in parsed_runtime_artifacts
            for part_id in artifact.part_ids
        )
        if (
            len({item.identifier for item in parsed_runtime_artifacts})
            != len(parsed_runtime_artifacts)
            or len(set(runtime_part_ids)) != len(runtime_part_ids)
            or any(
                part_id not in file_by_id
                or file_by_id[part_id].install is not None
                for artifact in parsed_runtime_artifacts
                for part_id in artifact.part_ids
            )
            or any(
                artifact.archive_size_bytes
                != sum(file_by_id[part_id].size_bytes for part_id in artifact.part_ids)
                for artifact in parsed_runtime_artifacts
            )
        ):
            raise VideoCapabilityError("VIDEO_MANIFEST_RUNTIME_ARTIFACT_INVALID")
        seen.add(identifier)
        result.append(VideoBundle(identifier, raw["label"], raw["status"], raw["requires_gpu"], tuple(dependencies), parsed_files, raw.get("license_review_required", False) is True, normalized_runtime, parsed_patches, parsed_runtime_artifacts))
    if seen != _PUBLIC_BUNDLES:
        raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
    return VideoManifest(payload["version"], tuple(result))


def _sha256_file(
    path: Path,
    *,
    pause: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if pause is not None and pause.is_set():
                raise InterruptedError
            digest.update(chunk)
            total += len(chunk)
            if progress is not None:
                progress(total)
    return total, digest.hexdigest()


def _runtime_archive_identity(path: Path) -> tuple[str, int, int, int, int] | None:
    try:
        metadata = path.stat()
    except OSError:
        return None
    return (str(path), metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _runtime_archive_fingerprint(path: Path) -> str | None:
    entries = None
    for attempt in range(8):
        try:
            with zipfile.ZipFile(path) as archive:
                entries = [
                    (
                        member.filename,
                        member.CRC,
                        member.file_size,
                        member.compress_size,
                        member.flag_bits,
                        member.external_attr,
                    )
                    for member in archive.infolist()
                ]
            break
        except zipfile.BadZipFile:
            return None
        except OSError:
            if attempt == 7:
                return None
            time.sleep(min(0.1 * (2**attempt), 2.0))
    if entries is None:
        return None
    return hashlib.sha256(
        json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _runtime_import_cache(data_root: Path) -> Path:
    cache_root = data_root / "capabilities" / _RUNTIME_IMPORT_CACHE
    if cache_root.exists() and (
        _is_reparse_point(cache_root) or not cache_root.is_dir()
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_IMPORT_CHECKPOINT_INVALID")
    return cache_root


def _discard_runtime_import_checkpoint(cache_root: Path, candidate: Path) -> None:
    (cache_root / _RUNTIME_IMPORT_CHECKPOINT).unlink(missing_ok=True)
    try:
        _reject_reparse_tree(candidate)
        shutil.rmtree(candidate)
    except (FileNotFoundError, OSError, VideoCapabilityError):
        pass


def _discard_stale_runtime_checkpoint(cache_root: Path) -> None:
    checkpoint = cache_root / _RUNTIME_IMPORT_CHECKPOINT
    try:
        payload: Any = json.loads(checkpoint.read_text(encoding="utf-8"))
        candidate_name = payload.get("candidate") if isinstance(payload, dict) else None
        if (
            isinstance(candidate_name, str)
            and candidate_name.startswith(".runtime-staging-")
            and Path(candidate_name).name == candidate_name
        ):
            candidate = _inside(cache_root, cache_root / candidate_name)
            _discard_runtime_import_checkpoint(cache_root, candidate)
            return
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, VideoCapabilityError):
        pass
    checkpoint.unlink(missing_ok=True)


def _write_runtime_import_checkpoint(
    cache_root: Path,
    *,
    archive_identity: str,
    candidate: Path,
    next_member_index: int = 0,
) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    relative_candidate = candidate.relative_to(cache_root).as_posix()
    payload: dict[str, object] = {
        "schema_version": "olivia.video-runtime-import-checkpoint.v1",
        "archive_identity": archive_identity,
        "candidate": relative_candidate,
        "next_member_index": next_member_index,
    }
    temporary = cache_root / f"{_RUNTIME_IMPORT_CHECKPOINT}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, cache_root / _RUNTIME_IMPORT_CHECKPOINT)
    finally:
        temporary.unlink(missing_ok=True)


def _read_runtime_import_checkpoint(
    cache_root: Path,
    archive_identity: str,
) -> tuple[Path, int] | None:
    checkpoint = cache_root / _RUNTIME_IMPORT_CHECKPOINT
    try:
        payload: Any = json.loads(checkpoint.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) not in (
            {"schema_version", "archive_identity", "candidate", "next_member_index"},
        )
        or payload.get("schema_version") != "olivia.video-runtime-import-checkpoint.v1"
        or payload.get("archive_identity") != archive_identity
        or type(payload.get("next_member_index")) is not int
        or payload["next_member_index"] < 0
        or not isinstance(payload.get("candidate"), str)
    ):
        return None
    candidate_name = payload["candidate"]
    if (
        not candidate_name.startswith(".runtime-staging-")
        or Path(candidate_name).name != candidate_name
    ):
        return None
    try:
        candidate = _inside(cache_root, cache_root / candidate_name)
        if not candidate.is_dir():
            return None
        _reject_reparse_tree(candidate)
    except (OSError, VideoCapabilityError):
        return None
    return candidate, payload["next_member_index"]


def _verify(path: Path, spec: VideoFile) -> None:
    size, digest = _sha256_file(path)
    if size != spec.size_bytes or digest != spec.sha256:
        raise VideoCapabilityError("VIDEO_FILE_VERIFICATION_FAILED")


def _portable_python_runtime(python: Path, runtime_root: Path) -> bool:
    if not python.is_file():
        return False
    script = (
        "from pathlib import Path; import sys; "
        "root=Path(sys.argv[1]).resolve(); "
        "inside=lambda value: (path:=Path(value).resolve()) == root or root in path.parents; "
        "assert inside(sys.executable) and inside(sys.prefix) and inside(sys.base_prefix); "
        "assert all(inside(value) for value in sys.path if value)"
    )
    runtime_environment = dict(os.environ)
    runtime_environment.pop("PYTHONHOME", None)
    runtime_environment.pop("PYTHONPATH", None)
    runtime_environment.update(PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
    try:
        completed = subprocess.run(
            [str(python), "-I", "-B", "-c", script, str(runtime_root)],
            cwd=runtime_root,
            env=runtime_environment,
            capture_output=True,
            check=False,
            timeout=_RUNTIME_PORTABILITY_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _RuntimeHostUnavailable from exc
    returncode = int(completed.returncode)
    if os.name == "nt" and returncode & 0xFFFFFFFF >= 0xC0000000:
        raise _RuntimeHostUnavailable
    return returncode == 0


def _runtime_environment_is_portable(
    environment: Mapping[str, str], runtime_root: Path
) -> bool:
    if set(environment) != _PORTABLE_RUNTIME_ENVIRONMENT_KEYS:
        return False
    candidates = [Path(environment[key]) for key in _PORTABLE_RUNTIME_ENVIRONMENT_KEYS]
    return bool(candidates) and all(
        _portable_python_runtime(candidate, runtime_root) for candidate in candidates
    )


def _verify_runtime_manifest_files(
    specifications: list[tuple[Path, int, str]],
    *,
    total_bytes: int,
    progress: Callable[[int, int], None] | None,
) -> None:
    if not specifications:
        return
    workers = min(
        len(specifications),
        _RUNTIME_MANIFEST_MAX_WORKERS,
        max(1, os.cpu_count() or 1),
    )
    pending_limit = workers * _RUNTIME_MANIFEST_PENDING_PER_WORKER
    stop = threading.Event()
    progress_lock = threading.Lock()
    file_progress = [0] * len(specifications)
    checked_bytes = 0

    def report(index: int, current: int) -> None:
        nonlocal checked_bytes
        if progress is None:
            return
        with progress_lock:
            delta = max(0, current - file_progress[index])
            if delta == 0:
                return
            file_progress[index] = current
            checked_bytes = min(total_bytes, checked_bytes + delta)
            progress(checked_bytes, total_bytes)

    def verify(index: int) -> tuple[int, str]:
        candidate, _expected_size, _expected_digest = specifications[index]
        return _sha256_file(
            candidate,
            pause=stop,
            progress=(None if progress is None else lambda current: report(index, current)),
        )

    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="olivia-runtime-verify",
    )
    pending: deque[tuple[int, Future[tuple[int, str]]]] = deque()
    next_index = 0
    try:
        while next_index < len(specifications) and len(pending) < pending_limit:
            pending.append((next_index, executor.submit(verify, next_index)))
            next_index += 1
        while pending:
            index, future = pending.popleft()
            try:
                actual = future.result()
            except OSError as exc:
                stop.set()
                raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from exc
            _candidate, expected_size, expected_digest = specifications[index]
            if actual != (expected_size, expected_digest):
                stop.set()
                raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
            while next_index < len(specifications) and len(pending) < pending_limit:
                pending.append((next_index, executor.submit(verify, next_index)))
                next_index += 1
    finally:
        stop.set()
        for _index, future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def _load_runtime_root_manifest(
    runtime_root: Path,
    manifest_sha256: str,
    *,
    verify_files: bool,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    if not runtime_root.is_absolute() or not runtime_root.is_dir():
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    root = runtime_root.resolve(strict=True)
    if _is_reparse_point(root):
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    manifest_path = _inside(root, root / "runtime-manifest.json")
    try:
        size, digest = _sha256_file(manifest_path)
        payload: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from exc
    if size <= 0 or digest != _safe_sha(manifest_sha256):
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "version", "environment", "files"} | ({"build_inputs"} if payload.get("schema_version") == "olivia.video-runtime-root.v2" else set())
        or payload.get("schema_version") not in {"olivia.video-runtime-root.v1", "olivia.video-runtime-root.v2"}
        or (payload.get("schema_version") == "olivia.video-runtime-root.v2" and (
            not isinstance(payload.get("build_inputs"), dict)
            or set(payload["build_inputs"]) != {"schema_version", "components"}
            or payload["build_inputs"].get("schema_version") != "olivia.video-runtime-build-inputs.v1"
            or not isinstance(payload["build_inputs"].get("components"), dict)
            or set(payload["build_inputs"]["components"]) != {"breeze", "latentsync", "minimax", "roformer"}
        ))
        or not isinstance(payload.get("version"), str)
        or not 1 <= len(payload["version"]) <= 64
        or not isinstance(payload.get("environment"), dict)
        or not isinstance(payload.get("files"), list)
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    raw_environment = payload["environment"]
    if (
        not raw_environment
        or set(raw_environment) - _RUNTIME_ENVIRONMENT_KEYS
        or not all(isinstance(key, str) for key in raw_environment)
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    environment: dict[str, str] = {}
    environment_paths: dict[str, str] = {}
    for key, raw in raw_environment.items():
        try:
            relative = _safe_relative(raw)
            candidate = _inside(root, root / relative)
        except (OSError, VideoCapabilityError):
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from None
        if not candidate.exists() or _is_reparse_point(candidate):
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
        environment[key] = str(candidate)
        environment_paths[key] = relative
    files: dict[str, tuple[int, str]] = {}
    seen_files: set[str] = set()
    total_bytes = 0
    for raw in payload["files"]:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"path", "size_bytes", "sha256"}
            or type(raw.get("size_bytes")) is not int
            or raw["size_bytes"] < 0
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
        total_bytes += raw["size_bytes"]
    if verify_files and progress is not None:
        progress(0, total_bytes)
    specifications: list[tuple[Path, int, str]] = []
    for raw in payload["files"]:
        try:
            relative = _safe_relative(raw.get("path"))
            digest = _safe_sha(raw.get("sha256"))
            candidate = _inside(root, root / relative)
        except (OSError, VideoCapabilityError):
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from None
        folded = relative.casefold()
        if folded in seen_files:
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
        seen_files.add(folded)
        files[relative] = (raw["size_bytes"], digest)
        specifications.append((candidate, raw["size_bytes"], digest))
    if verify_files:
        _verify_runtime_manifest_files(
            specifications,
            total_bytes=total_bytes,
            progress=progress,
        )
    required_files = {
        relative
        for key, relative in environment_paths.items()
        if Path(environment[key]).is_file()
    }
    if not required_files or not all(
        any(path.casefold() == required.casefold() for path in files)
        for required in required_files
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    if verify_files:
        declared = {path.casefold() for path in files}
        actual: set[str] = set()
        try:
            for candidate in root.rglob("*"):
                if candidate.is_dir():
                    continue
                relative = candidate.relative_to(root).as_posix()
                if relative.casefold() == "runtime-manifest.json":
                    continue
                if not candidate.is_file() or _is_reparse_point(candidate):
                    raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
                actual.add(relative.casefold())
        except OSError as exc:
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from exc
        if actual != declared:
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    return environment


def write_runtime_root_manifest(
    runtime_root: Path,
    *,
    version: str,
    environment: Mapping[str, str],
    build_inputs: Mapping[str, object] | None = None,
) -> str:
    """Hash an exact portable runtime tree and return its manifest SHA-256."""

    if (
        not runtime_root.is_absolute()
        or not runtime_root.is_dir()
        or not isinstance(version, str)
        or not 1 <= len(version) <= 64
        or not environment
        or set(environment) - _RUNTIME_ENVIRONMENT_KEYS
        or (build_inputs is not None and (not isinstance(build_inputs, Mapping) or set(build_inputs) != {"schema_version", "components"} or build_inputs.get("schema_version") != "olivia.video-runtime-build-inputs.v1" or not isinstance(build_inputs.get("components"), dict) or set(build_inputs["components"]) != {"breeze", "latentsync", "minimax", "roformer"}))
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    root = runtime_root.resolve(strict=True)
    _reject_reparse_tree(root)
    normalized_environment: dict[str, str] = {}
    for key, raw in environment.items():
        try:
            relative = _safe_relative(raw)
            candidate = _inside(root, root / relative)
        except (OSError, VideoCapabilityError):
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from None
        if not candidate.exists() or _is_reparse_point(candidate):
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
        normalized_environment[key] = relative
    files: list[dict[str, object]] = []
    for candidate in root.rglob("*"):
        if candidate.is_dir():
            continue
        relative = candidate.relative_to(root).as_posix()
        if relative.casefold() == "runtime-manifest.json":
            continue
        if not candidate.is_file() or _is_reparse_point(candidate):
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
        size, digest = _sha256_file(candidate)
        files.append({"path": relative, "size_bytes": size, "sha256": digest})
    files.sort(key=lambda item: str(item["path"]).casefold())
    if not files:
        raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
    payload = {
        "schema_version": "olivia.video-runtime-root.v2" if build_inputs is not None else "olivia.video-runtime-root.v1",
        "version": version,
        "environment": dict(sorted(normalized_environment.items())),
        "files": files,
    }
    if build_inputs is not None:
        payload["build_inputs"] = dict(build_inputs)
    target = root / "runtime-manifest.json"
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, target)
        return _sha256_file(target)[1]
    except OSError as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_MANIFEST_WRITE_FAILED") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _inside(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise VideoCapabilityError("VIDEO_PATH_ESCAPE") from exc
    return resolved


def _reject_reparse_tree(root: Path) -> None:
    if _is_reparse_point(root):
        raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if any(_is_reparse_point(current_path / name) for name in (*directories, *filenames)):
            raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")


def _checked_install_root(data_root: Path, *, create: bool) -> Path:
    if create:
        data_root.mkdir(parents=True, exist_ok=True)
    current = data_root
    for name in ("capabilities", "video"):
        current = current / name
        if current.exists():
            if _is_reparse_point(current) or not current.is_dir():
                raise VideoCapabilityError("VIDEO_INSTALL_ROOT_INVALID")
        elif create:
            current.mkdir()
    return current


def _restore_interrupted_promotions(install_root: Path) -> None:
    with _PROMOTION_LOCK:
        for bundle_id in (*_PUBLIC_BUNDLES, "runtime"):
            final = install_root / bundle_id
            backup = install_root / f".{bundle_id}.backup"
            if not backup.exists():
                continue
            try:
                _reject_reparse_tree(backup)
                if final.exists():
                    _reject_reparse_tree(final)
                    shutil.rmtree(final)
                os.replace(backup, final)
            except (OSError, ComponentUpdateError) as exc:
                raise VideoCapabilityError("VIDEO_BUNDLE_RECOVERY_FAILED") from exc


class VideoCapabilityInstaller:
    """Threaded, resumable public installer used by the local client API."""

    def __init__(
        self,
        *,
        data_root: Path,
        manifest: VideoManifest,
        opener: Callable[..., Any] = urlopen,
        readiness_probe: Callable[[Mapping[str, str]], Mapping[str, object]] | None = None,
        artifact_roots: tuple[Path, ...] = (),
        runtime_archives: tuple[Path, ...] = (),
        runtime_archive_roots: tuple[Path, ...] = (),
        runtime_environment_applier: Callable[[Mapping[str, str]], object] | None = None,
        runtime_progress: Callable[[str, int, int], object] | None = None,
        hardware_probe: Callable[[], Mapping[str, object]] | None = None,
        runtime_package_runner: Callable[[Path, Path, Path], object] | None = None,
        runtime_package_verifier: Callable[[Path, Path], bool] | None = None,
    ) -> None:
        if not data_root.is_absolute():
            raise VideoCapabilityError("VIDEO_DATA_ROOT_INVALID")
        self.data_root = data_root.resolve()
        self.install_root = _checked_install_root(self.data_root, create=True)
        self.manifest = manifest
        self._opener = opener
        self._readiness_probe = readiness_probe
        if any(not root.is_absolute() or not root.is_dir() for root in artifact_roots):
            raise VideoCapabilityError("VIDEO_ARTIFACT_ROOT_INVALID")
        self._artifact_roots = tuple(root.resolve() for root in artifact_roots)
        if any(
            not archive.is_absolute()
            or not archive.is_file()
            or archive.is_symlink()
            or archive.suffix.casefold() != ".zip"
            for archive in runtime_archives
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
        self._runtime_archives = tuple(archive.resolve() for archive in runtime_archives)
        if any(not root.is_absolute() or (root.exists() and (not root.is_dir() or _is_reparse_point(root))) for root in runtime_archive_roots):
            raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
        self._runtime_archive_roots = tuple(root.resolve() for root in runtime_archive_roots)
        self._runtime_environment_applier = runtime_environment_applier
        self._runtime_progress = runtime_progress
        self._hardware_probe = hardware_probe or _probe_breeze_hardware
        self._runtime_package_runner = (
            runtime_package_runner or self._install_breeze_runtime_packages
        )
        self._runtime_package_verifier = (
            runtime_package_verifier or self._verify_breeze_runtime_process
        )
        self._requires_breeze_hardware = any(
            "breeze_tts2" in bundle.dependencies for bundle in manifest.bundles
        )
        self._hardware = self._refresh_hardware()
        self._lock = threading.RLock()
        self._commit_lock = _PROMOTION_LOCK
        self._pause = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._runtime_thread: threading.Thread | None = None
        self._runtime_archive_identity: tuple[str, int, int, int, int] | None = None
        self._runtime_failed_archive_identities: set[tuple[str, int, int, int, int]] = set()
        self._status: dict[str, VideoBundleStatus] = {}
        self._runtime_import: dict[str, object] = {
            "state": "idle",
            "checked_bytes": 0,
            "total_bytes": 0,
        }
        _restore_interrupted_promotions(self.install_root)
        self._load_status()
        self._maybe_start_runtime_prepare()

    def _refresh_hardware(self) -> dict[str, object] | None:
        if not self._requires_breeze_hardware:
            return None
        try:
            value = self._hardware_probe()
        except Exception:
            value = None
        self._hardware = _validated_breeze_hardware(value)
        return self._hardware

    def _bundle(self, bundle_id: str) -> VideoBundle:
        for bundle in self.manifest.bundles:
            if bundle.identifier == bundle_id:
                return bundle
        raise VideoCapabilityError("VIDEO_BUNDLE_UNKNOWN")

    def _final_root(self, bundle: VideoBundle) -> Path:
        return self.install_root / bundle.identifier

    def _staging_root(self, bundle: VideoBundle) -> Path:
        staging = self.install_root / ".staging"
        if staging.exists() and _is_reparse_point(staging):
            raise VideoCapabilityError("VIDEO_STAGING_INVALID")
        return staging / f"{bundle.identifier}-{uuid.uuid4().hex}"

    def _download_root(self, bundle: VideoBundle) -> Path:
        return self.install_root / ".downloads" / bundle.identifier

    def _runtime_wiring_ready(self, root: Path, bundle: VideoBundle) -> bool:
        try:
            persisted = _load_video_runtime_environment(
                self.data_root, restore_backups=False
            )
            ready = all(
                key in persisted and Path(persisted[key]).exists()
                for key in (bundle.runtime_environment or {})
            )
            if "OLIVIA_SEED_VC_ROOT" in (bundle.runtime_environment or {}):
                seed_root = Path(persisted["OLIVIA_SEED_VC_ROOT"])
                ready = ready and (
                    seed_root / ".olivia-overlap-frames-patched.json"
                ).is_file()
            return ready
        except (OSError, VideoCapabilityError):
            return False

    @staticmethod
    def _assembled_state(
        bundle: VideoBundle,
    ) -> tuple[VideoCapabilityState, str | None]:
        if bundle.license_review_required:
            return (
                VideoCapabilityState.LICENSE_REVIEW_REQUIRED,
                "VIDEO_LICENSED_DEPENDENCIES_REQUIRED",
            )
        if "official_video_assets" in bundle.dependencies:
            return (
                VideoCapabilityState.PREREQUISITES_REQUIRED,
                "VIDEO_NATIVE_PATH_SELECTION_UNAVAILABLE",
            )
        return VideoCapabilityState.READY, None

    def _runtime_dependency_state(
        self,
        bundle: VideoBundle,
        *,
        probe_cache: dict[str, Mapping[str, object]] | None = None,
    ) -> tuple[VideoCapabilityState, str | None]:
        if self._readiness_probe is None:
            return self._assembled_state(bundle)
        result = probe_cache.get("result") if probe_cache is not None else None
        if result is None:
            try:
                environment = dict(os.environ)
                environment.update(load_video_runtime_environment(self.data_root))
                environment["OLIVIA_LOCAL_DATA_ROOT"] = str(self.data_root)
                result = self._readiness_probe(environment)
            except Exception:
                result = {}
            if not isinstance(result, Mapping):
                result = {}
            if probe_cache is not None:
                probe_cache["result"] = result
        if bundle.identifier == "ordinary_video":
            missing = result.get("ordinary_missing_dependencies")
            ready = isinstance(missing, (list, tuple)) and not missing
        else:
            ready = result.get("music_ready") is True
        if ready:
            return VideoCapabilityState.READY, None
        return (
            VideoCapabilityState.PREREQUISITES_REQUIRED,
            "VIDEO_RUNTIME_DEPENDENCIES_MISSING",
        )

    def _installed_state(
        self,
        root: Path,
        bundle: VideoBundle,
        *,
        probe_cache: dict[str, Mapping[str, object]] | None = None,
    ) -> tuple[VideoCapabilityState, str | None]:
        if (
            "OLIVIA_BREEZE_TTS_PYTHON" in (bundle.runtime_environment or {})
            and not self._breeze_runtime_marker_ready(root)
            and not self._external_breeze_runtime_ready()
        ):
            return (
                VideoCapabilityState.PREREQUISITES_REQUIRED,
                "BREEZE_TTS_RUNTIME_UNAVAILABLE",
            )
        if not self._runtime_wiring_ready(root, bundle):
            return (
                VideoCapabilityState.PREREQUISITES_REQUIRED,
                "VIDEO_RUNTIME_PREREQUISITES_MISSING",
            )
        try:
            profile = json.loads(
                (self.install_root / _RUNTIME_ENVIRONMENT_FILE).read_text(
                    encoding="utf-8"
                )
            )
            if isinstance(profile, dict) and set(profile) == {
                "schema_version",
                "environment",
                "external_environment",
                "runtime_root",
                "manifest_sha256",
            }:
                return VideoCapabilityState.READY, None
            if isinstance(profile, dict) and profile.get("host_status") == {
                "status": "READY",
                "reason_code": None,
            }:
                return VideoCapabilityState.READY, None
            if isinstance(profile, dict) and profile.get("host_status") == {
                "status": "UNAVAILABLE",
                "reason_code": _RUNTIME_HOST_UNAVAILABLE,
            }:
                return VideoCapabilityState.PREREQUISITES_REQUIRED, _RUNTIME_HOST_UNAVAILABLE
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        return self._runtime_dependency_state(bundle, probe_cache=probe_cache)

    def _external_breeze_runtime_ready(self) -> bool:
        try:
            profile = json.loads(
                (self.install_root / _RUNTIME_ENVIRONMENT_FILE).read_text(
                    encoding="utf-8"
                )
            )
            external = profile.get("external_environment")
            candidate = Path(external["OLIVIA_BREEZE_TTS_PYTHON"])
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ):
            return False
        return candidate.is_file()

    def _load_status(self) -> None:
        probe_cache: dict[str, Mapping[str, object]] = {}
        for bundle in self.manifest.bundles:
            current = self._status.get(bundle.identifier)
            thread = self._threads.get(bundle.identifier)
            if current is not None and current.state in {
                VideoCapabilityState.QUEUED,
                VideoCapabilityState.DOWNLOADING,
                VideoCapabilityState.VERIFYING,
            } and thread is not None and thread.is_alive():
                continue
            root = self._final_root(bundle)
            try:
                runtime_part_ids = {
                    part_id
                    for artifact in bundle.runtime_artifacts
                    for part_id in artifact.part_ids
                }
                content_ready = (
                    _ready_marker_matches(root, bundle, self.manifest.version)
                    and all(
                        _size_matches(root, root / item.relative_path, item)
                        for item in bundle.files
                        if item.identifier not in runtime_part_ids
                    )
                    and self._runtime_artifacts_ready(root, bundle)
                )
            except (OSError, ComponentUpdateError, VideoCapabilityError):
                content_ready = False
            if content_ready:
                state, reason = self._installed_state(
                    root,
                    bundle,
                    probe_cache=probe_cache,
                )
                self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, state, sum(item.size_bytes for item in bundle.files), sum(item.size_bytes for item in bundle.files), reason_code=reason)
            elif current is None or current.state not in {VideoCapabilityState.FAILED, VideoCapabilityState.PAUSED}:
                self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, VideoCapabilityState.MISSING, 0, sum(item.size_bytes for item in bundle.files))

    def status(self) -> dict[str, object]:
        with self._lock:
            self._refresh_hardware()
            if self._runtime_import["state"] not in {
                "queued",
                "extracting",
                "checking",
                "testing",
            }:
                self._load_status()
            self._maybe_start_runtime_prepare()
            bundles = [self._status[item.identifier].to_dict() for item in self.manifest.bundles]
            if self._hardware is not None and self._hardware["status"] != "READY":
                for item, bundle in zip(bundles, self.manifest.bundles, strict=True):
                    if bundle.requires_gpu and item["state"] not in {
                        "queued",
                        "downloading",
                        "verifying",
                    }:
                        item["state"] = "prerequisites_required"
                        item["reason_code"] = str(self._hardware["reason_code"])
            if self._runtime_import["state"] == "failed":
                for item in bundles:
                    if item["state"] == "ready":
                        item["state"] = "prerequisites_required"
                        item["reason_code"] = str(
                            self._runtime_import.get(
                                "reason_code", "VIDEO_RUNTIME_IMPORT_FAILED"
                            )
                        )
            result = {
                "schema_version": "olivia.video-capability-status.v2",
                "status": "READY" if all(item["state"] == "ready" for item in bundles) else "UNAVAILABLE",
                "capability": "video",
                "install_locations": [{"root": "local_data_root", "relative_path": "capabilities/video"}],
                "bundles": bundles,
                "runtime_import": dict(self._runtime_import),
            }
            if self._hardware is not None:
                result["hardware"] = dict(self._hardware)
            return result

    def _set_runtime_import_state(
        self, state: str, *, reason_code: str | None = None
    ) -> None:
        with self._lock:
            self._runtime_import["state"] = state
            self._runtime_import.pop("reason_code", None)
            if reason_code:
                self._runtime_import["reason_code"] = reason_code
            checked_bytes = int(self._runtime_import.get("checked_bytes", 0))
            total_bytes = int(self._runtime_import.get("total_bytes", 0))
        self._report_runtime_progress(state, checked_bytes, total_bytes)

    def _report_runtime_progress(
        self, state: str, checked_bytes: int, total_bytes: int
    ) -> None:
        if self._runtime_progress is None:
            return
        try:
            self._runtime_progress(state, checked_bytes, total_bytes)
        except Exception:
            pass

    @staticmethod
    def _runtime_import_error_code(exc: Exception) -> str:
        if isinstance(exc, VideoCapabilityError):
            return str(exc)
        return "VIDEO_RUNTIME_IMPORT_FAILED"

    def _update_runtime_import_progress(self, checked_bytes: int, total_bytes: int) -> None:
        with self._lock:
            self._runtime_import = {
                "state": "checking",
                "checked_bytes": checked_bytes,
                "total_bytes": total_bytes,
            }
        self._report_runtime_progress("checking", checked_bytes, total_bytes)

    def _set(self, bundle: VideoBundle, state: VideoCapabilityState, downloaded: int, *, current: str | None = None, source: str | None = None, reason: str | None = None) -> None:
        self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, state, downloaded, sum(item.size_bytes for item in bundle.files), current, source, reason)

    def _managed_runtime_path(
        self, environment: Mapping[str, str], key: str, *, directory: bool
    ) -> Path:
        raw = environment.get(key)
        if not isinstance(raw, str) or not raw:
            raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE")
        try:
            candidate = _inside(self.install_root, Path(raw))
        except (OSError, VideoCapabilityError):
            raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE") from None
        if (candidate.is_dir() if directory else candidate.is_file()):
            return candidate
        raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE")

    def _generate_managed_tts_config(
        self,
        environment: dict[str, str],
        *,
        reference_environment: Mapping[str, str] | None = None,
    ) -> None:
        """Publish the Breeze profile from installed, managed paths only."""

        if not self._requires_breeze_hardware:
            if "OLIVIA_TTS_CONFIG" not in environment:
                raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE")
            return
        breeze_root = self._managed_runtime_path(
            environment, "OLIVIA_BREEZE_TTS_ROOT", directory=True
        )
        model_root = self._managed_runtime_path(
            environment, "OLIVIA_BREEZE_TTS_MODEL_ROOT", directory=True
        )
        model_license = self._managed_runtime_path(
            environment, "OLIVIA_BREEZE_TTS_MODEL_LICENSE", directory=False
        )
        configured_reference = self._managed_runtime_path(
            environment, "OLIVIA_REPLY_VOICE_REFERENCE", directory=False
        )
        try:
            reference = resolve_managed_voice_reference(self.data_root)
        except (ManagedVoiceReferenceError, OSError):
            raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE") from None
        try:
            if reference.resolve() != configured_reference.resolve():
                raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE")
        except OSError:
            raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE") from None
        external_python = self._managed_runtime_path(
            environment, "OLIVIA_BREEZE_TTS_PYTHON", directory=False
        )
        reference_text = _breeze_reference_text(
            reference_environment or environment, self.data_root
        )
        if reference_text is None:
            raise VideoCapabilityError("VIDEO_RUNTIME_TTS_REFERENCE_TEXT_UNAVAILABLE")
        generated_temporary: Path | None = None
        try:
            generated_root = _inside(
                self.install_root, self.install_root / "generated"
            )
            if generated_root.exists():
                if not generated_root.is_dir() or _is_reparse_point(generated_root):
                    raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")
                _reject_reparse_tree(generated_root)
            else:
                generated_root.mkdir(parents=True)
            generated_root = _inside(
                self.install_root, self.install_root / "generated"
            )
            if not generated_root.is_dir() or _is_reparse_point(generated_root):
                raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")
            generated_config = _inside(
                generated_root, generated_root / "tts_local.json"
            )
            generated_temporary = _inside(
                generated_root,
                generated_root
                / f"{generated_config.name}.{uuid.uuid4().hex}.tmp",
            )
            for candidate in (generated_config, generated_temporary):
                if candidate.exists() and (
                    not candidate.is_file() or _is_reparse_point(candidate)
                ):
                    raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")
            _reject_reparse_tree(generated_root)
            generated_temporary.write_text(
                json.dumps(
                    {
                        "schema_version": "b10b.module-config.v1",
                        "module_id": "tts-local",
                        "profile": "breeze-tts2-int8-hybrid",
                        "settings": {
                            "provider": "breeze_tts2",
                            "runtime_root": str(breeze_root),
                            "model_dir": str(model_root),
                            "reference_audio": str(reference),
                            "reference_text": reference_text,
                            "language": "zh",
                            "license_id": "BreezeBlue-Research-and-Non-Commercial-1.0",
                            "fallback": "text",
                            "fp16": True,
                            "provider_options": {
                                "external_python": str(external_python),
                                "model_variant": "int8_hybrid",
                                "model_license_path": str(model_license),
                                "quality_gate_python": str(external_python),
                                "quality_gate_cache_root": str(
                                    self.data_root
                                    / "provider-cache"
                                    / "breeze-quality-gate"
                                ),
                                "dtype": "bf16",
                                "device": "cuda",
                                "attention": "eager",
                                "decode_mode": "eager",
                                "cfg_scale": 4.0,
                                "seed": 200717,
                                "max_new_tokens": 650,
                            },
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            checked_root = _inside(
                self.install_root, self.install_root / "generated"
            )
            if (
                checked_root != generated_root
                or _is_reparse_point(checked_root)
                or not checked_root.is_dir()
            ):
                raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")
            generated_config = _inside(
                checked_root, checked_root / generated_config.name
            )
            generated_temporary = _inside(
                checked_root, checked_root / generated_temporary.name
            )
            _reject_reparse_tree(checked_root)
            if (
                not generated_temporary.is_file()
                or _is_reparse_point(generated_temporary)
                or (
                    generated_config.exists()
                    and (
                        not generated_config.is_file()
                        or _is_reparse_point(generated_config)
                    )
                )
            ):
                raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")
            os.replace(generated_temporary, generated_config)
            checked_root = _inside(
                self.install_root, self.install_root / "generated"
            )
            checked_config = _inside(
                checked_root, checked_root / generated_config.name
            )
            if (
                checked_root != generated_root
                or _is_reparse_point(checked_root)
                or not checked_config.is_file()
                or _is_reparse_point(checked_config)
            ):
                raise VideoCapabilityError("VIDEO_REPARSE_POINT_FORBIDDEN")
        except (OSError, VideoCapabilityError) as exc:
            raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE") from exc
        finally:
            if generated_temporary is not None:
                try:
                    safe_temporary = _inside(
                        self.install_root, generated_temporary
                    )
                    safe_generated_root = _inside(
                        self.install_root, self.install_root / "generated"
                    )
                    if (
                        safe_temporary.parent == safe_generated_root
                        and not _is_reparse_point(safe_generated_root)
                        and safe_temporary.is_file()
                        and not _is_reparse_point(safe_temporary)
                    ):
                        safe_temporary.unlink(missing_ok=True)
                except (OSError, VideoCapabilityError):
                    pass
        environment["OLIVIA_TTS_CONFIG"] = str(generated_config)

    def _write_runtime_environment(self) -> None:
        try:
            previous = _load_video_runtime_environment(
                self.data_root, restore_backups=False
            )
        except (OSError, VideoCapabilityError):
            previous = {}
        music_bundle = next(
            (bundle for bundle in self.manifest.bundles if bundle.identifier == "music_video"),
            None,
        )
        music = self._final_root(music_bundle) if music_bundle is not None else None
        if music is not None and (music / ".ready.json").is_file():
            self._install_managed_minimax_worker()
        environment: dict[str, str] = {}
        for key, value in previous.items():
            try:
                candidate = _inside(self.install_root, Path(value))
            except (OSError, VideoCapabilityError):
                continue
            if candidate.exists():
                environment[key] = str(candidate)
        environment.update(self._installed_bundle_environment())
        ordinary_bundle = next(
            (bundle for bundle in self.manifest.bundles if bundle.identifier == "ordinary_video"),
            None,
        )
        ordinary = (
            self._final_root(ordinary_bundle) if ordinary_bundle is not None else None
        )
        managed_tts_keys = {
            "OLIVIA_BREEZE_TTS_ROOT",
            "OLIVIA_BREEZE_TTS_PYTHON",
            "OLIVIA_BREEZE_TTS_MODEL_ROOT",
            "OLIVIA_BREEZE_TTS_MODEL_LICENSE",
        }
        if (
            ordinary is not None
            and (ordinary / ".ready.json").is_file()
            and managed_tts_keys
            <= set((ordinary_bundle.runtime_environment or {}).keys())
        ):
            self._generate_managed_tts_config(
                environment,
                reference_environment={**previous, **environment},
            )
        self.install_root.mkdir(parents=True, exist_ok=True)
        target = self.install_root / _RUNTIME_ENVIRONMENT_FILE
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "schema_version": "olivia.video-runtime-environment.v1",
            "environment": environment,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, target)

    def _merge_runtime_environment(self) -> None:
        """Add newly installed paths without dropping an imported runtime profile."""

        target = self.install_root / _RUNTIME_ENVIRONMENT_FILE
        try:
            _load_video_runtime_environment(self.data_root, restore_backups=False)
            payload = json.loads(target.read_text(encoding="utf-8"))
            environment = payload["environment"]
            if (
                payload.get("schema_version")
                != "olivia.video-runtime-environment.v1"
                or not isinstance(environment, dict)
            ):
                raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, VideoCapabilityError):
            self._write_runtime_environment()
            return
        for bundle in self.manifest.bundles:
            root = self._final_root(bundle)
            if not (root / ".ready.json").is_file():
                continue
            for key, relative in (bundle.runtime_environment or {}).items():
                candidate = _inside(root, root / relative)
                if candidate.exists():
                    environment[key] = str(candidate)
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, target)

    def _promote_directory(
        self, staging: Path, final: Path, *, refresh_environment: bool = False
    ) -> None:
        backup = self.install_root / f".{final.name}.backup"
        with self._commit_lock:
            if backup.exists():
                _restore_interrupted_promotions(self.install_root)
            if final.exists():
                _reject_reparse_tree(final)
                os.replace(final, backup)
            try:
                os.replace(staging, final)
                if refresh_environment:
                    self._write_runtime_environment()
            except Exception:
                if final.exists():
                    shutil.rmtree(final, ignore_errors=True)
                if backup.exists():
                    os.replace(backup, final)
                raise
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)

    def start(self, *, bundle_id: str, source_mode: str = "auto", offline_root: Path | None = None, accept_licenses: bool = False) -> str:
        bundle = self._bundle(bundle_id)
        if source_mode not in _SOURCE_MODES:
            raise VideoCapabilityError("VIDEO_SOURCE_MODE_INVALID")
        if bundle.license_review_required and accept_licenses is not True:
            raise VideoCapabilityError("VIDEO_LICENSE_REVIEW_REQUIRED")
        hardware = self._refresh_hardware()
        if bundle.requires_gpu and hardware is not None and hardware["status"] != "READY":
            reason = str(hardware["reason_code"])
            with self._lock:
                current = self._status.get(
                    bundle_id,
                    VideoBundleStatus(bundle_id, VideoCapabilityState.MISSING, 0, 0),
                )
                self._set(
                    bundle,
                    VideoCapabilityState.PREREQUISITES_REQUIRED,
                    current.downloaded_bytes,
                    reason=reason,
                )
            raise VideoCapabilityError(reason)
        with self._lock:
            thread = self._threads.get(bundle_id)
            if thread is not None and thread.is_alive():
                return "NOOP"
            current_status = self._status.get(
                bundle_id,
                VideoBundleStatus(bundle_id, VideoCapabilityState.MISSING, 0, 0),
            )
            if current_status.state in {
                VideoCapabilityState.READY,
                VideoCapabilityState.LICENSE_REVIEW_REQUIRED,
            } or (
                current_status.state == VideoCapabilityState.PREREQUISITES_REQUIRED
                and current_status.downloaded_bytes >= current_status.total_bytes > 0
            ):
                return "NOOP"
            self._pause.clear()
            self._set(bundle, VideoCapabilityState.QUEUED, 0, source=source_mode)
            thread = threading.Thread(target=self._run, args=(bundle, source_mode, offline_root), name=f"olivia-video-{bundle_id}", daemon=True)
            self._threads[bundle_id] = thread
            thread.start()
        return "APPLIED"

    def pause(self) -> str:
        with self._lock:
            active = any(thread.is_alive() for thread in self._threads.values())
            if not active:
                return "NOOP"
            self._pause.set()
            return "APPLIED"

    def resume(self, *, bundle_id: str, source_mode: str = "auto", accept_licenses: bool = False) -> str:
        return self.start(bundle_id=bundle_id, source_mode=source_mode, accept_licenses=accept_licenses)

    def retry(self, *, bundle_id: str, source_mode: str = "auto", accept_licenses: bool = False) -> str:
        return self.start(bundle_id=bundle_id, source_mode=source_mode, accept_licenses=accept_licenses)

    def import_offline(self, *, bundle_id: str, offline_root: Path, source_mode: str = "official", accept_licenses: bool = False) -> str:
        return self.start(bundle_id=bundle_id, source_mode=source_mode, offline_root=offline_root, accept_licenses=accept_licenses)

    def import_runtime_root(
        self,
        *,
        runtime_root: Path,
        manifest_sha256: str,
    ) -> str:
        self._update_runtime_import_progress(0, 0)
        try:
            result = self._import_runtime_root(
                runtime_root=runtime_root,
                manifest_sha256=manifest_sha256,
            )
        except Exception as exc:
            self._set_runtime_import_state(
                "failed", reason_code=self._runtime_import_error_code(exc)
            )
            raise
        with self._lock:
            self._runtime_import["state"] = "ready"
            self._runtime_import["checked_bytes"] = self._runtime_import["total_bytes"]
            checked_bytes = int(self._runtime_import["checked_bytes"])
            total_bytes = int(self._runtime_import["total_bytes"])
        self._report_runtime_progress("ready", checked_bytes, total_bytes)
        return result

    def import_runtime_archive(self, *, runtime_archive: Path) -> str:
        try:
            archive = runtime_archive.resolve(strict=True)
        except OSError as exc:
            raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID") from exc
        if (
            not archive.is_file()
            or archive.is_symlink()
            or archive.suffix.casefold() != ".zip"
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
        self._update_runtime_extract_progress(0, 0)
        identity = _runtime_archive_fingerprint(archive)
        if identity is None:
            raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
        cache_root = _runtime_import_cache(self.data_root)
        checkpoint = _read_runtime_import_checkpoint(cache_root, identity)
        if checkpoint is None:
            _discard_stale_runtime_checkpoint(cache_root)
            staging = cache_root / f".runtime-staging-{uuid.uuid4().hex}"
            _write_runtime_import_checkpoint(
                cache_root, archive_identity=identity, candidate=staging
            )
            next_member_index = 0
        else:
            staging, next_member_index = checkpoint
        final = self.install_root / "runtime"
        backup = self.install_root / ".runtime.backup"
        try:
            resume = staging.exists()
            _extract_runtime_zip_safely(
                archive,
                staging,
                **({"resume": True} if resume else {}),
                next_member_index=next_member_index,
                checkpoint_progress=lambda index: _write_runtime_import_checkpoint(
                    cache_root,
                    archive_identity=identity,
                    candidate=staging,
                    next_member_index=index,
                ),
                progress=self._update_runtime_extract_progress,
            )
            with self._lock:
                self._runtime_import = {
                    "state": "checking",
                    "checked_bytes": 0,
                    "total_bytes": 0,
                }
            manifest_path = staging / "runtime-manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
            manifest_sha256 = _sha256_file(manifest_path)[1]
            try:
                _load_runtime_root_manifest(
                    staging,
                    manifest_sha256,
                    verify_files=True,
                    progress=self._update_runtime_import_progress,
                )
            except Exception:
                _discard_runtime_import_checkpoint(cache_root, staging)
                raise
            with self._commit_lock:
                if backup.exists():
                    _restore_interrupted_promotions(self.install_root)
                if final.exists():
                    _reject_reparse_tree(final)
                    os.replace(final, backup)
                shutil.copytree(staging, final, copy_function=os.link)
            try:
                result = self.import_runtime_root(
                    runtime_root=final.resolve(),
                    manifest_sha256=manifest_sha256,
                )
                if backup.exists():
                    if backup.is_dir():
                        shutil.rmtree(backup)
                    else:
                        backup.unlink()
                _discard_runtime_import_checkpoint(cache_root, staging)
                return result
            except BaseException as exc:
                rollback_error: Exception | None = None
                with self._commit_lock:
                    try:
                        if final.exists():
                            _reject_reparse_tree(final)
                            shutil.rmtree(final)
                        if backup.exists():
                            os.replace(backup, final)
                    except Exception as restore_exc:
                        rollback_error = restore_exc
                if rollback_error is not None:
                    raise VideoCapabilityError(
                        "VIDEO_RUNTIME_IMPORT_ROLLBACK_FAILED"
                    ) from rollback_error
                raise
        except BaseException as exc:
            if backup.exists() and not final.exists():
                try:
                    os.replace(backup, final)
                except OSError as rollback_exc:
                    exc = VideoCapabilityError(
                        "VIDEO_RUNTIME_IMPORT_ROLLBACK_FAILED"
                    )
                    exc.__cause__ = rollback_exc
            self._set_runtime_import_state(
                "failed", reason_code=self._runtime_import_error_code(exc)
            )
            raise
        finally:
            pass

    def start_runtime_archive_import(self, *, runtime_archive: Path) -> str:
        try:
            archive = runtime_archive.resolve(strict=True)
        except OSError as exc:
            raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID") from exc
        identity = _runtime_archive_identity(archive)
        if (
            identity is None
            or not archive.is_file()
            or archive.is_symlink()
            or archive.suffix.casefold() != ".zip"
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
        with self._lock:
            if self._runtime_thread is not None and self._runtime_thread.is_alive():
                return "NOOP"
            if (
                self._runtime_import["state"] == "ready"
                and self._runtime_archive_identity == identity
            ):
                return "NOOP"
            self._runtime_import = {
                "state": "queued",
                "checked_bytes": 0,
                "total_bytes": 0,
            }
            self._runtime_archive_identity = identity
            thread = threading.Thread(
                target=self._run_runtime_archive,
                args=(archive, identity),
                name="olivia-video-runtime",
                daemon=True,
            )
            self._runtime_thread = thread
            thread.start()
        return "APPLIED"

    def _update_runtime_extract_progress(
        self, checked_bytes: int, total_bytes: int
    ) -> None:
        with self._lock:
            self._runtime_import = {
                "state": "extracting",
                "checked_bytes": checked_bytes,
                "total_bytes": total_bytes,
            }
        self._report_runtime_progress("extracting", checked_bytes, total_bytes)

    def _resume_persisted_runtime(self) -> bool:
        try:
            payload = json.loads(
                (self.install_root / _RUNTIME_ENVIRONMENT_FILE).read_text(encoding="utf-8")
            )
            if (
                not isinstance(payload, dict)
                or set(payload.get("external_environment", {})) != _PORTABLE_RUNTIME_ENVIRONMENT_KEYS
                or payload.get("host_status") not in (None, {
                    "status": "READY",
                    "reason_code": None,
                }, {
                    "status": "UNAVAILABLE",
                    "reason_code": _RUNTIME_HOST_UNAVAILABLE,
                })
            ):
                return False
            environment = load_video_runtime_environment(self.data_root)
        except (OSError, UnicodeError, json.JSONDecodeError, VideoCapabilityError):
            return False
        try:
            self._install_managed_minimax_worker()
        except VideoCapabilityError:
            self._set_runtime_import_state(
                "failed", reason_code="VIDEO_RUNTIME_WORKER_UNAVAILABLE"
            )
            return True
        try:
            if self._runtime_environment_applier is not None:
                self._runtime_environment_applier(environment)
        except Exception:
            self._set_runtime_import_state(
                "failed", reason_code="VIDEO_RUNTIME_ENVIRONMENT_ACTIVATION_FAILED"
            )
            return True
        self._runtime_import = {
            "state": "ready",
            "checked_bytes": 0,
            "total_bytes": 0,
        }
        host_status = payload.get("host_status")
        if isinstance(host_status, dict) and host_status.get("status") == "UNAVAILABLE":
            self._runtime_import["reason_code"] = _RUNTIME_HOST_UNAVAILABLE
        return True

    def _resume_bundled_runtime(self) -> bool:
        try:
            profile = json.loads(
                (self.install_root / _RUNTIME_ENVIRONMENT_FILE).read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(profile, dict) or "external_environment" in profile:
                return False
            self._write_runtime_environment()
            environment = load_video_runtime_environment(self.data_root)
        except VideoCapabilityError as exc:
            self._set_runtime_import_state("failed", reason_code=str(exc))
            return True
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not all(
            key in environment and Path(environment[key]).is_file()
            for key in _PORTABLE_RUNTIME_ENVIRONMENT_KEYS
        ):
            return False
        if self._readiness_probe is None:
            self._set_runtime_import_state(
                "failed", reason_code="VIDEO_RUNTIME_PROBE_UNAVAILABLE"
            )
            return True
        probe_environment = dict(os.environ)
        probe_environment.update(environment)
        probe_environment["OLIVIA_LOCAL_DATA_ROOT"] = str(self.data_root)
        try:
            readiness = self._readiness_probe(probe_environment)
        except Exception:
            self._set_runtime_import_state(
                "failed", reason_code="VIDEO_RUNTIME_PROBE_FAILED"
            )
            return True
        if not isinstance(readiness, Mapping):
            self._set_runtime_import_state(
                "failed", reason_code="VIDEO_RUNTIME_PROBE_FAILED"
            )
            return True
        ordinary_missing = readiness.get("ordinary_missing_dependencies")
        ready = (
            isinstance(ordinary_missing, (list, tuple))
            and not ordinary_missing
            and readiness.get("music_ready") is True
        )
        if not ready:
            dependencies = readiness.get("dependencies")
            if not isinstance(dependencies, list):
                self._set_runtime_import_state(
                    "failed", reason_code="VIDEO_RUNTIME_PROBE_FAILED"
                )
                return True
            missing_ids = {
                str(item["id"])
                for item in dependencies
                if isinstance(item, Mapping)
                and isinstance(item.get("id"), str)
                and item.get("state") == "missing"
            }
            if missing_ids and missing_ids <= _RUNTIME_HOST_DEPENDENCIES:
                return False
            self._set_runtime_import_state(
                "failed", reason_code="VIDEO_RUNTIME_DEPENDENCIES_MISSING"
            )
            return True
        try:
            if self._runtime_environment_applier is not None:
                self._runtime_environment_applier(environment)
        except Exception:
            self._set_runtime_import_state(
                "failed", reason_code="VIDEO_RUNTIME_ENVIRONMENT_ACTIVATION_FAILED"
            )
            return True
        self._runtime_import = {
            "state": "ready",
            "checked_bytes": 0,
            "total_bytes": 0,
        }
        return True

    def _maybe_start_runtime_prepare(self) -> None:
        state = self._runtime_import["state"]
        if state not in {"idle", "required", "failed"}:
            return
        if self._runtime_thread is not None and self._runtime_thread.is_alive():
            return
        if state == "failed" and not self._runtime_archives and not self._runtime_archive_roots:
            return
        if self._resume_bundled_runtime() or self._resume_persisted_runtime():
            return
        if any(
            self._status.get(bundle.identifier) is None
            or self._status[bundle.identifier].state
            not in {VideoCapabilityState.READY, VideoCapabilityState.PREREQUISITES_REQUIRED}
            for bundle in self.manifest.bundles
        ):
            return
        try:
            discovered = tuple(candidate.resolve() for root in self._runtime_archive_roots if root.is_dir() and not _is_reparse_point(root) for candidate in root.glob("Olivia-video-runtime-*.zip") if candidate.is_file() and not candidate.is_symlink())
        except OSError:
            discovered = ()
        archives = tuple(dict.fromkeys((*self._runtime_archives, *discovered)))
        archive = next((path for path in archives if path.is_file()), None)
        if archive is None:
            if state == "failed":
                return
            self._runtime_import = {
                "state": "required",
                "checked_bytes": 0,
                "total_bytes": 0,
                "reason_code": "VIDEO_RUNTIME_ARCHIVE_REQUIRED",
            }
            return
        identified = tuple((archive, identity) for archive in archives if archive.is_file() and not archive.is_symlink() if (identity := _runtime_archive_identity(archive)) is not None)
        if not identified:
            return
        self._runtime_failed_archive_identities.intersection_update(identity for _, identity in identified)
        candidate = next(
            (item for item in identified if state != "failed" or item[1] not in self._runtime_failed_archive_identities),
            None,
        )
        if candidate is None:
            return
        archive, identity = candidate
        self._runtime_import["state"] = "queued"
        self._runtime_archive_identity = identity
        thread = threading.Thread(
            target=self._run_runtime_archive,
            args=(archive, identity),
            name="olivia-video-runtime",
            daemon=True,
        )
        self._runtime_thread = thread
        thread.start()

    def _run_runtime_archive(self, archive: Path, identity: tuple[str, int, int, int, int]) -> None:
        try:
            self.import_runtime_archive(runtime_archive=archive)
        except Exception as exc:
            self._runtime_failed_archive_identities.add(identity)
            self._set_runtime_import_state(
                "failed", reason_code=self._runtime_import_error_code(exc)
            )

    def _installed_bundle_environment(self) -> dict[str, str]:
        ordinary = self._final_root(self._bundle("ordinary_video"))
        candidates = {
            "OLIVIA_BREEZE_TTS_ROOT": ordinary / "breeze" / "runtime",
            "OLIVIA_BREEZE_TTS_MODEL_ROOT": ordinary / "breeze" / "model",
            "OLIVIA_BREEZE_TTS_MODEL_LICENSE": ordinary / "breeze" / "model" / "LICENSE",
            "OLIVIA_COSYVOICE_ROOT": ordinary / "cosyvoice" / "runtime",
            "OLIVIA_COSYVOICE_MODEL_ROOT": ordinary / "cosyvoice" / "model",
            "OLIVIA_REPLY_VOICE_REFERENCE": self.install_root
            / "shared"
            / "linli-reference.wav",
        }
        for bundle in self.manifest.bundles:
            root = self._final_root(bundle)
            for key, relative in (bundle.runtime_environment or {}).items():
                candidates.setdefault(key, root / relative)
        return {
            key: str(path.resolve()) for key, path in candidates.items() if path.exists()
        }

    def _install_managed_minimax_worker(self) -> None:
        try:
            self._install_managed_minimax_worker_transaction()
        except Exception as exc:
            if (
                isinstance(exc, VideoCapabilityError)
                and str(exc) == "VIDEO_RUNTIME_WORKER_UNAVAILABLE"
            ):
                raise
            raise VideoCapabilityError("VIDEO_RUNTIME_WORKER_UNAVAILABLE") from exc

    def _install_managed_minimax_worker_transaction(self) -> None:
        source_root = Path(__file__).resolve().parent / "tools"
        sources = (
            source_root / "minimax_profile.py",
            source_root / "minimax_music3_worker.py",
        )
        bundle = self._bundle("music_video")
        music = self._final_root(bundle)
        relative = (bundle.runtime_environment or {}).get(
            "OLIVIA_MINIMAX_WORKER"
        )
        if not relative:
            if "minimax_music3" in bundle.dependencies:
                raise VideoCapabilityError("VIDEO_RUNTIME_WORKER_UNAVAILABLE")
            return
        if any(
            not source.is_file() or _is_reparse_point(source) for source in sources
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_WORKER_UNAVAILABLE")
        _reject_reparse_tree(music)
        worker_target = _inside(music, music / relative)
        targets = (
            _inside(music, worker_target.with_name("minimax_profile.py")),
            worker_target,
        )
        worker_target.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(worker_target.parent) or any(
            target.exists() and _is_reparse_point(target) for target in targets
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_WORKER_UNAVAILABLE")
        transaction = uuid.uuid4().hex
        staged = tuple(
            target.with_name(f"{target.name}.{transaction}.tmp") for target in targets
        )
        backups: dict[Path, Path] = {}
        originals: dict[Path, tuple[int, str] | None] = {}
        published: list[Path] = []
        succeeded = False
        try:
            for source, temporary in zip(sources, staged, strict=True):
                shutil.copy2(source, temporary)
            for target, temporary in zip(targets, staged, strict=True):
                if target.exists():
                    originals[target] = _sha256_file(target)
                    backup = target.with_name(f"{target.name}.{transaction}.bak")
                    os.replace(target, backup)
                    backups[target] = backup
                else:
                    originals[target] = None
                os.replace(temporary, target)
                published.append(target)
            succeeded = True
        except Exception as exc:
            rollback_error: Exception | None = None
            for target in reversed(targets):
                try:
                    if target in published:
                        target.unlink(missing_ok=True)
                except Exception as cleanup_exc:
                    rollback_error = rollback_error or cleanup_exc
                backup = backups.get(target)
                if backup is not None:
                    try:
                        if backup.exists():
                            os.replace(backup, target)
                    except Exception as restore_exc:
                        rollback_error = rollback_error or restore_exc
            for target, expected in originals.items():
                backup = backups.get(target)
                try:
                    if expected is None:
                        target.unlink(missing_ok=True)
                    elif backup is not None and backup.exists():
                        os.replace(backup, target)
                except Exception as reconcile_exc:
                    rollback_error = rollback_error or reconcile_exc
            for target, expected in originals.items():
                try:
                    restored = (
                        not target.exists()
                        if expected is None
                        else target.is_file() and _sha256_file(target) == expected
                    )
                except Exception as verify_exc:
                    restored = False
                    rollback_error = rollback_error or verify_exc
                if not restored and rollback_error is None:
                    rollback_error = RuntimeError("managed worker rollback incomplete")
            raise VideoCapabilityError("VIDEO_RUNTIME_WORKER_UNAVAILABLE") from (
                rollback_error or exc
            )
        finally:
            for temporary in staged:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if succeeded:
                for backup in backups.values():
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _import_runtime_root(
        self,
        *,
        runtime_root: Path,
        manifest_sha256: str,
    ) -> str:
        try:
            root = runtime_root.resolve(strict=True)
        except OSError as exc:
            raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from exc
        _reject_reparse_tree(root)
        external_environment = _load_runtime_root_manifest(
            root,
            manifest_sha256,
            verify_files=True,
            progress=self._update_runtime_import_progress,
        )
        self._set_runtime_import_state("testing")
        host_unavailable = False
        try:
            portable = _runtime_environment_is_portable(external_environment, root)
        except _RuntimeHostUnavailable:
            portable = False
            host_unavailable = True
        if not portable and not host_unavailable:
            raise VideoCapabilityError("VIDEO_RUNTIME_NOT_PORTABLE")
        self._install_managed_minimax_worker()
        try:
            environment = _load_video_runtime_environment(
                self.data_root, restore_backups=False
            )
        except VideoCapabilityError:
            environment = {}
        for key, value in self._installed_bundle_environment().items():
            environment.setdefault(key, value)
        environment.update(external_environment)
        self._generate_managed_tts_config(environment)
        ready = False
        probe_exception = False
        if not host_unavailable:
            if self._readiness_probe is None:
                raise VideoCapabilityError("VIDEO_RUNTIME_PROBE_UNAVAILABLE")
            probe_environment = dict(os.environ)
            probe_environment.update(environment)
            probe_environment["OLIVIA_LOCAL_DATA_ROOT"] = str(self.data_root)
            try:
                readiness = self._readiness_probe(probe_environment)
                ordinary_missing = readiness.get("ordinary_missing_dependencies")
                ready = (
                    isinstance(ordinary_missing, (list, tuple))
                    and not ordinary_missing
                    and readiness.get("music_ready") is True
                )
            except Exception:
                probe_exception = True
                ready = False
        if not ready and not host_unavailable and not probe_exception:
            dependencies = readiness.get("dependencies")
            if not isinstance(dependencies, list):
                raise VideoCapabilityError("VIDEO_RUNTIME_PROBE_FAILED")
            missing_ids: set[str] = set()
            for dependency in dependencies:
                if (
                    not isinstance(dependency, Mapping)
                    or not isinstance(dependency.get("id"), str)
                    or dependency.get("state") not in {"ready", "missing"}
                ):
                    raise VideoCapabilityError("VIDEO_RUNTIME_PROBE_FAILED")
                if dependency["state"] == "missing":
                    missing_ids.add(dependency["id"])
            if not missing_ids or not missing_ids <= _RUNTIME_HOST_DEPENDENCIES:
                raise VideoCapabilityError("VIDEO_RUNTIME_PROBE_FAILED")
        host_status = {
            "status": "READY" if portable and ready else "UNAVAILABLE",
            "reason_code": None if portable and ready else _RUNTIME_HOST_UNAVAILABLE,
        }
        self.install_root.mkdir(parents=True, exist_ok=True)
        target = self.install_root / _RUNTIME_ENVIRONMENT_FILE
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        persisted_environment: dict[str, str] = {}
        for key, value in environment.items():
            if key in external_environment:
                persisted_environment[key] = value
                continue
            try:
                Path(value).resolve().relative_to(self.install_root.resolve())
            except ValueError:
                continue
            persisted_environment[key] = value
        payload = {
            "schema_version": "olivia.video-runtime-environment.v1",
            "environment": persisted_environment,
            "external_environment": external_environment,
            "runtime_root": str(root),
            "manifest_sha256": _safe_sha(manifest_sha256),
            "host_status": host_status,
        }
        backup = target.with_name(f"{target.name}.{uuid.uuid4().hex}.backup")
        previous_environment = {key: os.environ.get(key) for key in environment}
        applier_started = False
        published = False
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            if target.exists():
                os.replace(target, backup)
            if self._runtime_environment_applier is not None:
                applier_started = True
                try:
                    self._runtime_environment_applier(environment)
                except Exception as exc:
                    raise VideoCapabilityError(
                        "VIDEO_RUNTIME_ENVIRONMENT_ACTIVATION_FAILED"
                    ) from exc
            os.replace(temporary, target)
            published = True
        except Exception as exc:
            if applier_started:
                for key, value in previous_environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            if backup.exists():
                os.replace(backup, target)
            if isinstance(exc, OSError):
                raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_WRITE_FAILED") from exc
            raise
        finally:
            temporary.unlink(missing_ok=True)
            if published:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass
        with self._lock:
            if host_status["status"] == "UNAVAILABLE":
                self._runtime_import["reason_code"] = _RUNTIME_HOST_UNAVAILABLE
            self._load_status()
        return "APPLIED"

    def _run(self, bundle: VideoBundle, source_mode: str, offline_root: Path | None) -> None:
        root = self._staging_root(bundle)
        try:
            if self._run_append_only_upgrade(bundle, source_mode, offline_root):
                return
            root.mkdir(parents=True, exist_ok=True)
            download_root = self._download_root(bundle)
            download_root.mkdir(parents=True, exist_ok=True)
            if any(_is_reparse_point(path) for path in (root.parent, root, download_root.parent, download_root)):
                raise VideoCapabilityError("VIDEO_STAGING_INVALID")
            downloaded = 0
            source_used = source_mode
            for item in bundle.files:
                if self._pause.is_set():
                    raise InterruptedError
                self._set(bundle, VideoCapabilityState.DOWNLOADING, downloaded, current=item.relative_path, source=source_used)
                cached = _inside(download_root, download_root / item.relative_path)
                cached.parent.mkdir(parents=True, exist_ok=True)
                if offline_root is not None:
                    self._copy_offline(offline_root, item, cached)
                elif self._reuse_local_artifact(item, cached):
                    source_used = "local"
                else:
                    source_used = self._download(
                        item,
                        cached,
                        source_mode,
                        progress=lambda current, source: self._update_download_progress(
                            bundle,
                            downloaded + current,
                            current=item.relative_path,
                            source=source,
                        ),
                    )
                _verify(cached, item)
                target = _inside(root, root / item.relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cached, target)
                downloaded += item.size_bytes
            expected = [
                {"path": item.relative_path, "size_bytes": item.size_bytes, "sha256": item.sha256}
                for item in bundle.files
                if not any(
                    item.identifier in artifact.part_ids
                    for artifact in bundle.runtime_artifacts
                )
            ]
            expected.extend(self._assemble_archives(root, bundle))
            self._set(bundle, VideoCapabilityState.VERIFYING, downloaded, source=source_used)
            final = self._final_root(bundle)
            if len({item["path"].casefold() for item in expected}) != len(expected):
                raise VideoCapabilityError("VIDEO_STAGED_TREE_INVALID")
            if _is_reparse_point(root):
                raise VideoCapabilityError("VIDEO_STAGING_INVALID")
            _verify_staged_tree(root, expected)
            self._bootstrap_breeze_runtime(root, bundle)
            (root / ".ready.json").write_text(
                json.dumps(
                    {
                        "schema_version": "olivia.video-bundle.v1",
                        "bundle": bundle.identifier,
                        "version": self.manifest.version,
                    }
                ),
                encoding="utf-8",
            )
            self._promote_directory(root, final, refresh_environment=True)
            for artifact in bundle.runtime_artifacts:
                for part_id in artifact.part_ids:
                    cached_part = _inside(
                        download_root, download_root / next(
                            item.relative_path
                            for item in bundle.files
                            if item.identifier == part_id
                        )
                    )
                    cached_part.unlink(missing_ok=True)
            with self._lock:
                state, reason = self._installed_state(final, bundle)
                self._set(
                    bundle,
                    state,
                    downloaded,
                    source=source_used,
                    reason=reason,
                )
                self._maybe_start_runtime_prepare()
        except InterruptedError:
            with self._lock:
                self._set(bundle, VideoCapabilityState.PAUSED, self._status.get(bundle.identifier, VideoBundleStatus(bundle.identifier, VideoCapabilityState.PAUSED, 0, 0)).downloaded_bytes, source=source_mode)
        except Exception as exc:
            with self._lock:
                reason = (
                    str(exc)
                    if isinstance(exc, VideoCapabilityError)
                    else "VIDEO_BUNDLE_INSTALL_FAILED"
                )
                self._set(bundle, VideoCapabilityState.FAILED, self._status.get(bundle.identifier, VideoBundleStatus(bundle.identifier, VideoCapabilityState.FAILED, 0, 0)).downloaded_bytes, source=source_mode, reason=reason)
        finally:
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)

    def _run_append_only_upgrade(
        self,
        bundle: VideoBundle,
        source_mode: str,
        offline_root: Path | None,
    ) -> bool:
        """Add new direct files without restaging an already-ready large bundle."""

        root = self._final_root(bundle)
        marker_path = root / ".ready.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if (
            marker.get("schema_version") != "olivia.video-bundle.v1"
            or marker.get("bundle") != bundle.identifier
            or not isinstance(marker.get("version"), str)
            or marker.get("version") == self.manifest.version
        ):
            return False
        missing: list[VideoFile] = []
        existing_bytes = 0
        runtime_part_ids = {
            part_id
            for artifact in bundle.runtime_artifacts
            for part_id in artifact.part_ids
        }
        if not self._runtime_artifacts_ready(root, bundle):
            return False
        for item in bundle.files:
            if item.identifier in runtime_part_ids:
                existing_bytes += item.size_bytes
                continue
            target = _inside(root, root / item.relative_path)
            if target.exists():
                if not _verify_and_true(target, item):
                    return False
                existing_bytes += item.size_bytes
                continue
            if item.install is not None:
                return False
            missing.append(item)
        download_root = self._download_root(bundle)
        download_root.mkdir(parents=True, exist_ok=True)
        source_used = source_mode
        downloaded = existing_bytes
        for item in missing:
            if self._pause.is_set():
                raise InterruptedError
            self._set(
                bundle,
                VideoCapabilityState.DOWNLOADING,
                downloaded,
                current=item.relative_path,
                source=source_used,
            )
            cached = _inside(download_root, download_root / item.relative_path)
            cached.parent.mkdir(parents=True, exist_ok=True)
            if offline_root is not None:
                self._copy_offline(offline_root, item, cached)
            elif self._reuse_local_artifact(item, cached):
                source_used = "local"
            else:
                source_used = self._download(
                    item,
                    cached,
                    source_mode,
                    progress=lambda current, source: self._update_download_progress(
                        bundle,
                        downloaded + current,
                        current=item.relative_path,
                        source=source,
                    ),
                )
            _verify(cached, item)
            target = _inside(root, root / item.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copy2(cached, temporary)
                _verify(temporary, item)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            downloaded += item.size_bytes

        self._set(
            bundle,
            VideoCapabilityState.VERIFYING,
            downloaded,
            source=source_used,
        )
        temporary_marker = marker_path.with_name(
            f"{marker_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary_marker.write_text(
            json.dumps(
                {
                    "schema_version": "olivia.video-bundle.v1",
                    "bundle": bundle.identifier,
                    "version": self.manifest.version,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary_marker, marker_path)
        self._merge_runtime_environment()
        with self._lock:
            state, reason = self._installed_state(root, bundle)
            self._set(
                bundle,
                state,
                downloaded,
                source=source_used,
                reason=reason,
            )
        return True

    def _reuse_local_artifact(self, item: VideoFile, target: Path) -> bool:
        if _verify_and_true(target, item):
            return True
        parts = PurePosixPath(item.relative_path).parts
        for artifact_root in self._artifact_roots:
            for offset in range(len(parts)):
                candidate = _inside(
                    artifact_root,
                    artifact_root / Path(*parts[offset:]),
                )
                if not _verify_and_true(candidate, item):
                    continue
                if candidate != target.resolve():
                    shutil.copy2(candidate, target)
                return True
        return False

    def _assemble_archives(self, root: Path, bundle: VideoBundle) -> list[dict[str, object]]:
        expected: list[dict[str, object]] = []
        for item in bundle.files:
            if item.install is None:
                continue
            archive_path = _inside(root, root / item.relative_path)
            destination = _inside(root, root / item.install.destination)
            extracted = _extract_zip_safely(
                archive_path,
                destination,
                strip_components=item.install.strip_components,
            )
            expected.extend({**entry, "path": f"{item.install.destination}/{entry['path']}"} for entry in extracted)
        file_by_id = {item.identifier: item for item in bundle.files}
        for artifact in bundle.runtime_artifacts:
            temporary_archive = root / f".{artifact.identifier}.{uuid.uuid4().hex}.zip"
            digest = hashlib.sha256()
            written_bytes = 0
            try:
                with temporary_archive.open("xb") as output:
                    for part_id in artifact.part_ids:
                        part = file_by_id[part_id]
                        part_path = _inside(root, root / part.relative_path)
                        with part_path.open("rb") as source:
                            while chunk := source.read(8 * 1024 * 1024):
                                output.write(chunk)
                                digest.update(chunk)
                                written_bytes += len(chunk)
                if (
                    written_bytes != artifact.archive_size_bytes
                    or digest.hexdigest() != artifact.archive_sha256
                ):
                    raise VideoCapabilityError("VIDEO_RUNTIME_ARTIFACT_INVALID")
                destination = _inside(root, root / artifact.destination)
                extracted = _extract_zip_safely(
                    temporary_archive,
                    destination,
                    strip_components=artifact.strip_components,
                    maximum_expanded_bytes=_MAX_RUNTIME_ARCHIVE_EXPANDED_BYTES,
                )
                expected.extend(
                    {
                        **entry,
                        "path": f"{artifact.destination}/{entry['path']}",
                    }
                    for entry in extracted
                )
                marker = root / ".runtime-artifacts" / f"{artifact.identifier}.json"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    json.dumps(
                        {
                            "schema_version": "olivia.video-runtime-artifact.v1",
                            "id": artifact.identifier,
                            "archive_size_bytes": artifact.archive_size_bytes,
                            "archive_sha256": artifact.archive_sha256,
                            "destination": artifact.destination,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                expected.append(_tree_entry(root, marker.relative_to(root).as_posix()))
            finally:
                temporary_archive.unlink(missing_ok=True)
            for part_id in artifact.part_ids:
                _inside(root, root / file_by_id[part_id].relative_path).unlink(missing_ok=True)
        for runtime_patch in bundle.runtime_patches:
            apply_runtime_text_patch(
                bundle_root=root,
                patch_path=_inside(
                    Path(__file__).resolve().parent,
                    Path(__file__).resolve().parent / runtime_patch.relative_path,
                ),
                target_path=runtime_patch.target_path,
                expected_sha256=runtime_patch.sha256,
                patch_id=runtime_patch.identifier,
            )
            for relative in (
                runtime_patch.target_path,
                f".patches/{runtime_patch.identifier}.json",
            ):
                expected = [entry for entry in expected if entry["path"] != relative]
                expected.append(_tree_entry(root, relative))
        seed_root = root / "seed_vc" / "runtime"
        if bundle.identifier == "music_video" and (seed_root / "inference.py").is_file():
            apply_seed_vc_overlap_frames_patch(
                seed_root,
                Path(__file__).resolve().parent
                / "installer"
                / "seed-vc-overlap-frames.patch",
            )
            for relative in (
                "seed_vc/runtime/inference.py",
                "seed_vc/runtime/.olivia-overlap-frames-patched.json",
            ):
                expected = [entry for entry in expected if entry["path"] != relative]
                expected.append(_tree_entry(root, relative))
        return expected

    @staticmethod
    def _runtime_artifacts_ready(root: Path, bundle: VideoBundle) -> bool:
        for artifact in bundle.runtime_artifacts:
            try:
                marker = json.loads(
                    (
                        root / ".runtime-artifacts" / f"{artifact.identifier}.json"
                    ).read_text(encoding="utf-8")
                )
                destination = _inside(root, root / artifact.destination)
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                VideoCapabilityError,
            ):
                return False
            if marker != {
                "schema_version": "olivia.video-runtime-artifact.v1",
                "id": artifact.identifier,
                "archive_size_bytes": artifact.archive_size_bytes,
                "archive_sha256": artifact.archive_sha256,
                "destination": artifact.destination,
            } or not destination.is_dir():
                return False
        return True

    @staticmethod
    def _breeze_requirements_path() -> Path:
        return Path(__file__).resolve().parent / _BREEZE_RUNTIME_REQUIREMENTS

    @staticmethod
    def _configure_embedded_python(python_path: Path) -> Path:
        candidates = sorted(python_path.parent.glob("python*._pth"))
        if len(candidates) != 1:
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_PTH_INVALID")
        pth = candidates[0]
        python_zip = next(
            (path.name for path in python_path.parent.glob("python*.zip") if path.is_file()),
            None,
        )
        if python_zip is None:
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_STDLIB_MISSING")
        (python_path.parent / "Lib" / "site-packages").mkdir(
            parents=True, exist_ok=True
        )
        pth.write_text(
            f"{python_zip}\n.\nLib/site-packages\nimport site\n",
            encoding="utf-8",
        )
        return python_path.parent / "Lib" / "site-packages"

    @staticmethod
    def _install_breeze_runtime_packages(
        python_path: Path, site_packages: Path, requirements: Path
    ) -> None:
        environment = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
            environment.pop(key, None)
        environment.update(
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            PIP_NO_INPUT="1",
            PYTHONNOUSERSITE="1",
            PYTHONSAFEPATH="1",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--no-deps",
                "--only-binary=:all:",
                "--find-links",
                str(python_path.parent.parent / "wheels"),
                "--extra-index-url",
                "https://download.pytorch.org/whl/cu128",
                "--target",
                str(site_packages),
                "--requirement",
                str(requirements),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=7200.0,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_INSTALL_FAILED")

    @staticmethod
    def _verify_breeze_runtime_process(python_path: Path, runtime_root: Path) -> bool:
        script = (
            "import _distutils_hack, soundfile, torch, transformers, whisper; "
            "assert torch.__version__ == '2.9.1+cu128'; "
            "assert torch.version.cuda == '12.8'; "
            "assert torch.cuda.is_available(); "
            "torch.ones(1, device='cuda')"
        )
        environment = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
            environment.pop(key, None)
        environment.update(PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
        try:
            completed = subprocess.run(
                [str(python_path), "-I", "-B", "-c", script],
                cwd=runtime_root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=180.0,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def _bootstrap_breeze_runtime(self, root: Path, bundle: VideoBundle) -> None:
        if "OLIVIA_BREEZE_TTS_PYTHON" not in (bundle.runtime_environment or {}):
            return
        relative_python = (bundle.runtime_environment or {}).get(
            "OLIVIA_BREEZE_TTS_PYTHON"
        )
        if relative_python is None:
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_PYTHON_PATH_MISSING")
        python_path = _inside(root, root / relative_python)
        if not python_path.is_file():
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_PYTHON_EXE_MISSING")
        requirements = self._breeze_requirements_path()
        try:
            requirements_sha256 = hashlib.sha256(requirements.read_bytes()).hexdigest()
        except OSError as exc:
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_LOCK_UNAVAILABLE") from exc
        if requirements_sha256 != _BREEZE_RUNTIME_REQUIREMENTS_SHA256:
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_LOCK_INVALID")
        site_packages = self._configure_embedded_python(python_path)
        self._runtime_package_runner(python_path, site_packages, requirements)
        if not self._runtime_package_verifier(python_path, root / "breeze" / "runtime"):
            raise VideoCapabilityError("BREEZE_TTS_RUNTIME_START_FAILED")
        (root / _BREEZE_RUNTIME_MARKER).write_text(
            json.dumps(
                {
                    "schema_version": "olivia.breeze-runtime.v1",
                    "requirements_sha256": requirements_sha256,
                    "python": relative_python,
                    "torch": "2.9.1+cu128",
                    "cuda": "12.8",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _breeze_runtime_marker_ready(root: Path) -> bool:
        try:
            marker = json.loads((root / _BREEZE_RUNTIME_MARKER).read_text(encoding="utf-8"))
            python_path = _inside(root, root / str(marker["python"]))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, VideoCapabilityError):
            return False
        return (
            marker.get("schema_version") == "olivia.breeze-runtime.v1"
            and marker.get("requirements_sha256")
            == _BREEZE_RUNTIME_REQUIREMENTS_SHA256
            and marker.get("torch") == "2.9.1+cu128"
            and marker.get("cuda") == "12.8"
            and python_path.is_file()
        )

    def _copy_offline(self, offline_root: Path, item: VideoFile, target: Path) -> None:
        if offline_root.is_file() and zipfile.is_zipfile(offline_root):
            with zipfile.ZipFile(offline_root) as archive:
                member = _safe_relative(item.relative_path)
                if member not in archive.namelist():
                    raise VideoCapabilityError("VIDEO_OFFLINE_FILE_MISSING")
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
            return
        source = _inside(offline_root.resolve(), offline_root / item.relative_path)
        if not source.is_file():
            raise VideoCapabilityError("VIDEO_OFFLINE_FILE_MISSING")
        shutil.copy2(source, target)

    def _update_download_progress(
        self,
        bundle: VideoBundle,
        downloaded: int,
        *,
        current: str,
        source: str,
    ) -> None:
        with self._lock:
            self._set(
                bundle,
                VideoCapabilityState.DOWNLOADING,
                downloaded,
                current=current,
                source=source,
            )

    def _download(
        self,
        item: VideoFile,
        target: Path,
        source_mode: str,
        *,
        progress: Callable[[int, str], None] | None = None,
    ) -> str:
        sources = [source_mode] if source_mode == "official" else ["domestic", "official"]
        part = target.with_name(target.name + ".part")
        for source_index, source_id in enumerate(sources):
            url = item.sources.get(source_id)
            if not url:
                continue
            try:
                existing = part.stat().st_size if part.is_file() else 0
                headers = {"User-Agent": "bside-olivia-video-installer/1"}
                if existing:
                    headers["Range"] = f"bytes={existing}-"
                response = self._opener(Request(url, headers=headers), timeout=30)
                status = getattr(response, "status", 200)
                if existing and status != 206:
                    existing = 0
                    part.unlink(missing_ok=True)
                mode = "ab" if existing else "wb"
                with response, part.open(mode) as stream:
                    while chunk := response.read(1024 * 1024):
                        if self._pause.is_set():
                            raise InterruptedError
                        stream.write(chunk)
                        existing += len(chunk)
                        if progress is not None:
                            progress(existing, source_id)
                if part.stat().st_size != item.size_bytes:
                    raise VideoCapabilityError("VIDEO_FILE_SIZE_INVALID")
                _verify(part, item)
                os.replace(part, target)
                return source_id
            except InterruptedError:
                raise
            except (HTTPError, URLError, OSError, TimeoutError, VideoCapabilityError):
                if any(item.sources.get(candidate) for candidate in sources[source_index + 1 :]):
                    part.unlink(missing_ok=True)
                continue
        raise VideoCapabilityError("VIDEO_DOWNLOAD_FAILED")


def _verify_and_true(path: Path, item: VideoFile) -> bool:
    if not path.is_file():
        return False
    try:
        _verify(path, item)
    except (OSError, VideoCapabilityError):
        return False
    return True


def _ready_marker_matches(root: Path, bundle: VideoBundle, version: str) -> bool:
    marker = root / ".ready.json"
    if not _regular_file_without_reparse_ancestors(root, marker):
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ComponentUpdateError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == {
        "schema_version": "olivia.video-bundle.v1",
        "bundle": bundle.identifier,
        "version": version,
    }


def _regular_file_without_reparse_ancestors(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
        if not root.is_dir() or _is_reparse_point(root):
            return False
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if not current.is_dir() or _is_reparse_point(current):
                return False
        return path.is_file() and not _is_reparse_point(path)
    except (OSError, ComponentUpdateError, ValueError):
        return False


def _size_matches(root: Path, path: Path, item: VideoFile) -> bool:
    try:
        return (
            _regular_file_without_reparse_ancestors(root, path)
            and path.stat().st_size == item.size_bytes
        )
    except (OSError, ComponentUpdateError):
        return False


def _tree_entry(root: Path, relative: str) -> dict[str, object]:
    path = _inside(root, root / relative)
    size, digest = _sha256_file(path)
    return {"path": relative, "size_bytes": size, "sha256": digest}


def _extract_zip_safely(
    archive_path: Path,
    destination: Path,
    *,
    strip_components: int,
    maximum_expanded_bytes: int | None = None,
) -> list[dict[str, object]]:
    if maximum_expanded_bytes is None:
        maximum_expanded_bytes = _MAX_ARCHIVE_EXPANDED_BYTES
    destination.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    expected: list[dict[str, object]] = []
    expanded = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                mode = (member.external_attr >> 16) & 0o170000
                raw = member.filename[:-1] if member.is_dir() and member.filename.endswith("/") else member.filename
                try:
                    validated = _validate_relative_path(raw)
                except ComponentUpdateError as exc:
                    raise VideoCapabilityError("VIDEO_ARCHIVE_PATH_INVALID") from exc
                path = PurePosixPath(validated)
                if any(part in {"", ".", ".."} for part in path.parts):
                    raise VideoCapabilityError("VIDEO_ARCHIVE_PATH_INVALID")
                parts = path.parts[strip_components:]
                if not parts:
                    continue
                try:
                    relative = _validate_relative_path(PurePosixPath(*parts).as_posix())
                except ComponentUpdateError as exc:
                    raise VideoCapabilityError("VIDEO_ARCHIVE_PATH_INVALID") from exc
                if mode == 0o120000:
                    continue
                collision_key = relative.casefold()
                if collision_key in written:
                    raise VideoCapabilityError("VIDEO_ARCHIVE_DUPLICATE_PATH")
                written.add(collision_key)
                expanded += member.file_size
                if expanded > maximum_expanded_bytes:
                    raise VideoCapabilityError("VIDEO_ARCHIVE_TOO_LARGE")
                target = _inside(destination, destination / relative)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written_bytes = 0
                with archive.open(member) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        written_bytes += len(chunk)
                expected.append({"path": relative, "size_bytes": written_bytes, "sha256": digest.hexdigest()})
        for item in expected:
            if _tree_entry(destination, str(item["path"])) != item:
                raise VideoCapabilityError("VIDEO_ARCHIVE_INVALID")
        return expected
    except (OSError, zipfile.BadZipFile, ComponentUpdateError) as exc:
        raise VideoCapabilityError("VIDEO_ARCHIVE_INVALID") from exc


def _extract_runtime_zip_safely(
    archive_path: Path,
    destination: Path,
    *,
    resume: bool = False,
    next_member_index: int = 0,
    checkpoint_progress: Callable[[int], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Extract a published portable runtime; its signed manifest verifies files next."""

    destination.mkdir(parents=True, exist_ok=resume)
    if resume:
        _reject_reparse_tree(destination)
    written: set[str] = set()
    extracted_bytes = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            total_bytes = sum(member.file_size for member in members)
            if total_bytes > _MAX_RUNTIME_ARCHIVE_EXPANDED_BYTES:
                raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_TOO_LARGE")
            if progress is not None:
                progress(0, total_bytes)
            normalized: list[tuple[zipfile.ZipInfo, str]] = []
            for member in members:
                mode = (member.external_attr >> 16) & 0o170000
                raw = (
                    member.filename[:-1]
                    if member.is_dir() and member.filename.endswith("/")
                    else member.filename
                )
                try:
                    relative = _validate_relative_path(raw)
                except ComponentUpdateError as exc:
                    raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID") from exc
                path = PurePosixPath(relative)
                if any(part in {"", ".", ".."} for part in path.parts) or mode == 0o120000:
                    raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
                folded = relative.casefold()
                if folded in written:
                    raise VideoCapabilityError("VIDEO_ARCHIVE_DUPLICATE_PATH")
                written.add(folded)
                normalized.append((member, relative))
            start_index = 0
            if resume:
                checkpoint_index = min(next_member_index, len(normalized))
                start_index = max(0, checkpoint_index - 256)
                for member, relative in normalized[start_index:checkpoint_index]:
                    if member.is_dir():
                        continue
                    target = _inside(destination, destination / relative)
                    if (
                        not target.is_file()
                        or _is_reparse_point(target)
                        or target.stat().st_size != member.file_size
                    ):
                        break
                    start_index += 1
            for index, (member, relative) in enumerate(normalized[start_index:], start=start_index):
                target = _inside(destination, destination / relative)
                if member.is_dir():
                    if target.exists() and not target.is_dir():
                        raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if not target.is_file() or _is_reparse_point(target):
                        raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
                    if target.stat().st_size == member.file_size:
                        extracted_bytes += member.file_size
                        if progress is not None:
                            progress(extracted_bytes, total_bytes)
                        continue
                    target.unlink()
                with archive.open(member) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        extracted_bytes += len(chunk)
                        if progress is not None:
                            progress(extracted_bytes, total_bytes)
                if checkpoint_progress is not None and (index + 1) % 256 == 0:
                    checkpoint_progress(index + 1)
            if checkpoint_progress is not None:
                checkpoint_progress(len(normalized))
            if "runtime-manifest.json" not in {
                value.casefold() for _, value in normalized
            }:
                raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID")
    except (OSError, zipfile.BadZipFile, ComponentUpdateError) as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_ARCHIVE_INVALID") from exc


def _load_video_runtime_environment(
    data_root: Path, *, restore_backups: bool = True
) -> dict[str, str]:
    if not data_root.is_absolute():
        raise VideoCapabilityError("VIDEO_DATA_ROOT_INVALID")
    install_root = _checked_install_root(data_root.resolve(), create=False)
    if restore_backups:
        _restore_interrupted_promotions(install_root)
    path = install_root / _RUNTIME_ENVIRONMENT_FILE
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID") from exc
    managed_fields = {"schema_version", "environment"}
    managed_fields_with_host = managed_fields | {"host_status"}
    external_fields = managed_fields | {
        "external_environment",
        "runtime_root",
        "manifest_sha256",
    }
    external_fields_with_host = external_fields | {"host_status"}
    if (
        not isinstance(payload, dict)
        or set(payload) not in {
            frozenset(managed_fields),
            frozenset(managed_fields_with_host),
            frozenset(external_fields),
            frozenset(external_fields_with_host),
        }
        or payload.get("schema_version") != "olivia.video-runtime-environment.v1"
        or not isinstance(payload.get("environment"), dict)
        or set(payload["environment"]) - _RUNTIME_ENVIRONMENT_KEYS
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
    host_status = payload.get("host_status")
    if host_status is not None and host_status not in ({
        "status": "READY",
        "reason_code": None,
    }, {
        "status": "UNAVAILABLE",
        "reason_code": _RUNTIME_HOST_UNAVAILABLE,
    }):
        raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
    external_environment: dict[str, str] | None = None
    if set(payload) in (external_fields, external_fields_with_host):
        raw_root = payload.get("runtime_root")
        declared_external = payload.get("external_environment")
        if (
            not isinstance(raw_root, str)
            or not Path(raw_root).is_absolute()
            or not isinstance(declared_external, dict)
            or set(declared_external) - _RUNTIME_ENVIRONMENT_KEYS
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
        try:
            runtime_root = Path(raw_root).resolve(strict=True)
            if _is_reparse_point(runtime_root):
                raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
            manifest = _inside(runtime_root, runtime_root / "runtime-manifest.json")
            if _sha256_file(manifest)[1] != _safe_sha(
                payload.get("manifest_sha256", "")
            ):
                raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
            external_environment = {}
            for key, raw in declared_external.items():
                if not isinstance(raw, str) or not Path(raw).is_absolute():
                    raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
                candidate = _inside(runtime_root, Path(raw).resolve(strict=True))
                if _is_reparse_point(candidate):
                    raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
                external_environment[key] = str(candidate)
        except (OSError, VideoCapabilityError) as exc:
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID") from exc
        if external_environment != declared_external:
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
    result: dict[str, str] = {}
    for key, raw in payload["environment"].items():
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
        candidate = Path(raw).resolve()
        if external_environment is None:
            candidate = _inside(install_root, candidate)
        elif key in external_environment:
            if external_environment.get(key) != str(candidate):
                raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
        else:
            candidate = _inside(install_root, candidate)
        if not candidate.exists():
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
        result[key] = str(candidate)
    return result


def load_video_runtime_environment(data_root: Path) -> dict[str, str]:
    with _PROMOTION_LOCK:
        return _load_video_runtime_environment(data_root)


def apply_runtime_text_patch(
    *,
    bundle_root: Path,
    patch_path: Path,
    target_path: str,
    expected_sha256: str,
    patch_id: str,
) -> None:
    """Apply one pinned exact-text runtime patch without Git or patch.exe."""

    if _safe_sha(expected_sha256) != _sha256_file(patch_path)[1]:
        raise VideoCapabilityError("VIDEO_RUNTIME_PATCH_HASH_MISMATCH")
    try:
        payload: Any = json.loads(patch_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_PATCH_INVALID") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "target", "replacements"}
        or payload.get("schema_version") != "olivia.runtime-text-patch.v1"
        or payload.get("target") != target_path
        or not isinstance(payload.get("replacements"), list)
        or not payload["replacements"]
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_PATCH_INVALID")
    target = _inside(bundle_root, bundle_root / _safe_relative(target_path))
    try:
        with target.open("r", encoding="utf-8", newline="") as stream:
            source = stream.read()
    except (OSError, UnicodeError) as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_PATCH_TARGET_INVALID") from exc
    uses_crlf = "\r\n" in source and source.count("\r\n") == source.count("\n")
    patched = source.replace("\r\n", "\n") if uses_crlf else source
    for replacement in payload["replacements"]:
        if (
            not isinstance(replacement, dict)
            or set(replacement) != {"before", "after"}
            or not isinstance(replacement.get("before"), str)
            or not replacement["before"]
            or not isinstance(replacement.get("after"), str)
            or patched.count(replacement["before"]) != 1
        ):
            raise VideoCapabilityError("VIDEO_RUNTIME_PATCH_SOURCE_MISMATCH")
        patched = patched.replace(replacement["before"], replacement["after"], 1)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as stream:
            stream.write(patched.replace("\n", "\r\n") if uses_crlf else patched)
        os.replace(temporary, target)
        marker = _inside(bundle_root, bundle_root / ".patches" / f"{_safe_relative(patch_id)}.json")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "schema_version": "olivia.runtime-patch-marker.v1",
                    "patch_id": patch_id,
                    "sha256": expected_sha256,
                    "target": target_path,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_PATCH_WRITE_FAILED") from exc
    finally:
        temporary.unlink(missing_ok=True)


def apply_seed_vc_overlap_frames_patch(
    seed_root: Path, patch_path: Path
) -> None:
    """Apply the pinned Seed-VC overlap option patch without requiring Git."""

    try:
        patch = patch_path.read_text(encoding="utf-8")
        patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        source_path = _inside(seed_root.resolve(), seed_root / "inference.py")
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VideoCapabilityError("VIDEO_SEED_VC_PATCH_UNAVAILABLE") from exc
    if patch_sha != _SEED_VC_PATCH_SHA256:
        raise VideoCapabilityError("VIDEO_SEED_VC_PATCH_INVALID")
    changes = (
        (
            "    overlap_frame_len = 16",
            "    overlap_frame_len = args.overlap_frames",
        ),
        (
            '    parser.add_argument("--fp16", type=str2bool, default=True)',
            '    parser.add_argument("--fp16", type=str2bool, default=True)\n'
            '    parser.add_argument("--overlap-frames", type=int, default=16)',
        ),
    )
    for before, after in changes:
        if f"-{before}" not in patch or f"+{after.splitlines()[-1]}" not in patch:
            raise VideoCapabilityError("VIDEO_SEED_VC_PATCH_INVALID")
        if source.count(before) != 1:
            raise VideoCapabilityError("VIDEO_SEED_VC_SOURCE_MISMATCH")
        source = source.replace(before, after, 1)
    temporary = source_path.with_suffix(".patched")
    temporary.write_text(source, encoding="utf-8")
    os.replace(temporary, source_path)
    verified = source_path.read_text(encoding="utf-8")
    if any(after not in verified for _before, after in changes):
        raise VideoCapabilityError("VIDEO_SEED_VC_PATCH_VERIFICATION_FAILED")
    marker = seed_root / ".olivia-overlap-frames-patched.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "olivia.seed-vc-patch.v1",
                "patch_sha256": patch_sha,
                "source_sha256": hashlib.sha256(verified.encode("utf-8")).hexdigest(),
                "overlap_frames_default": 16,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


__all__ = ["apply_runtime_text_patch", "apply_seed_vc_overlap_frames_patch", "VideoBundle", "VideoBundleStatus", "VideoCapabilityError", "VideoCapabilityInstaller", "VideoCapabilityState", "VideoFile", "VideoFileInstall", "VideoManifest", "VideoRuntimePatch", "load_video_manifest", "load_video_runtime_environment", "write_runtime_root_manifest"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build a verified Olivia video runtime root manifest")
    parser.add_argument("runtime_root", type=Path)
    parser.add_argument("environment_json", type=Path)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    raw_environment = json.loads(
        arguments.environment_json.read_text(encoding="utf-8-sig")
    )
    if not isinstance(raw_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_environment.items()
    ):
        raise SystemExit("environment_json must contain one string-to-string object")
    print(
        write_runtime_root_manifest(
            arguments.runtime_root.resolve(),
            version=arguments.version,
            environment=raw_environment,
        )
    )
