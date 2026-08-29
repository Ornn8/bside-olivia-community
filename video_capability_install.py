"""Resumable, verified installer for optional ordinary and music video bundles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import threading
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
    "OLIVIA_ORDINARY_ACTION_BASE",
    "OLIVIA_OFFICIAL_REPLY_REFERENCE",
    "OLIVIA_MUSIC_PERFORMANCE_BASE",
    "OLIVIA_REPLY_VOICE_REFERENCE",
    "OLIVIA_PROVIDER_CACHE_ROOT",
}
_MAX_ARCHIVE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
_RUNTIME_PORTABILITY_TIMEOUT_SECONDS = 20.0
_SEED_VC_PATCH_SHA256 = "f61ffb5193514ee3e34a439ebcd89c6168cf4bdb6a8d960513ee471d8840f2a6"
_PROMOTION_LOCK = threading.RLock()


class VideoCapabilityError(ValueError):
    """Raised for invalid manifests or unsafe installation inputs."""


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
        if set(raw) - required - {"license_review_required", "runtime_environment", "runtime_patches"} or not required.issubset(raw):
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
        seen.add(identifier)
        result.append(VideoBundle(identifier, raw["label"], raw["status"], raw["requires_gpu"], tuple(dependencies), parsed_files, raw.get("license_review_required", False) is True, normalized_runtime, parsed_patches))
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
        "assert inside(sys.executable) and inside(sys.prefix) and inside(sys.base_prefix)"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", script, str(runtime_root)],
            cwd=runtime_root,
            capture_output=True,
            check=False,
            timeout=_RUNTIME_PORTABILITY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _runtime_environment_is_portable(
    environment: Mapping[str, str], runtime_root: Path
) -> bool:
    required = (
        "OLIVIA_COSYVOICE_PYTHON",
        "OLIVIA_LATENTSYNC_PYTHON",
        "OLIVIA_MINIMAX_COMFY_PYTHON",
        "OLIVIA_ROFORMER_PYTHON",
    )
    if any(key not in environment for key in required):
        return False
    candidates = [Path(environment[key]) for key in required]
    if "OLIVIA_ROFORMER_EXE" in environment:
        executable = Path(environment["OLIVIA_ROFORMER_EXE"])
        candidates.append(
            executable
            if executable.name.casefold() == "python.exe"
            else executable.parent / "python.exe"
        )
    return bool(candidates) and all(
        _portable_python_runtime(candidate, runtime_root) for candidate in candidates
    )


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
        or set(payload) != {"schema_version", "version", "environment", "files"}
        or payload.get("schema_version") != "olivia.video-runtime-root.v1"
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
    checked_bytes = 0
    if verify_files and progress is not None:
        progress(0, total_bytes)
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
        if verify_files:
            try:
                actual_size, actual_digest = _sha256_file(
                    candidate,
                    progress=(
                        None
                        if progress is None
                        else lambda current, base=checked_bytes: progress(
                            min(total_bytes, base + current), total_bytes
                        )
                    ),
                )
            except OSError as exc:
                raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID") from exc
            if (actual_size, actual_digest) != files[relative]:
                raise VideoCapabilityError("VIDEO_RUNTIME_ROOT_INVALID")
            checked_bytes += actual_size
            if progress is not None:
                progress(checked_bytes, total_bytes)
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
) -> str:
    """Hash an exact portable runtime tree and return its manifest SHA-256."""

    if (
        not runtime_root.is_absolute()
        or not runtime_root.is_dir()
        or not isinstance(version, str)
        or not 1 <= len(version) <= 64
        or not environment
        or set(environment) - _RUNTIME_ENVIRONMENT_KEYS
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
        "schema_version": "olivia.video-runtime-root.v1",
        "version": version,
        "environment": dict(sorted(normalized_environment.items())),
        "files": files,
    }
    target = root / "runtime-manifest.json"
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
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
        for bundle_id in _PUBLIC_BUNDLES:
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
        self._lock = threading.RLock()
        self._commit_lock = _PROMOTION_LOCK
        self._pause = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._status: dict[str, VideoBundleStatus] = {}
        self._runtime_import: dict[str, object] = {
            "state": "idle",
            "checked_bytes": 0,
            "total_bytes": 0,
        }
        _restore_interrupted_promotions(self.install_root)
        self._load_status()

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
            persisted = load_video_runtime_environment(self.data_root)
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
        self, bundle: VideoBundle
    ) -> tuple[VideoCapabilityState, str | None]:
        if self._readiness_probe is None:
            return self._assembled_state(bundle)
        try:
            environment = dict(os.environ)
            environment.update(load_video_runtime_environment(self.data_root))
            environment["OLIVIA_LOCAL_DATA_ROOT"] = str(self.data_root)
            result = self._readiness_probe(environment)
            if bundle.identifier == "ordinary_video":
                missing = result.get("ordinary_missing_dependencies")
                ready = isinstance(missing, (list, tuple)) and not missing
            else:
                ready = result.get("music_ready") is True
        except Exception:
            ready = False
        if ready:
            return VideoCapabilityState.READY, None
        return (
            VideoCapabilityState.PREREQUISITES_REQUIRED,
            "VIDEO_RUNTIME_DEPENDENCIES_MISSING",
        )

    def _installed_state(
        self, root: Path, bundle: VideoBundle
    ) -> tuple[VideoCapabilityState, str | None]:
        if not self._runtime_wiring_ready(root, bundle):
            return (
                VideoCapabilityState.PREREQUISITES_REQUIRED,
                "VIDEO_RUNTIME_PREREQUISITES_MISSING",
            )
        return self._runtime_dependency_state(bundle)

    def _load_status(self) -> None:
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
                content_ready = (
                    _ready_marker_matches(root, bundle, self.manifest.version)
                    and all(
                        _size_matches(root, root / item.relative_path, item)
                        for item in bundle.files
                    )
                )
            except (OSError, ComponentUpdateError, VideoCapabilityError):
                content_ready = False
            if content_ready:
                state, reason = self._installed_state(root, bundle)
                self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, state, sum(item.size_bytes for item in bundle.files), sum(item.size_bytes for item in bundle.files), reason_code=reason)
            elif current is None or current.state not in {VideoCapabilityState.FAILED, VideoCapabilityState.PAUSED}:
                self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, VideoCapabilityState.MISSING, 0, sum(item.size_bytes for item in bundle.files))

    def status(self) -> dict[str, object]:
        with self._lock:
            if self._runtime_import["state"] not in {"checking", "testing"}:
                self._load_status()
            bundles = [self._status[item.identifier].to_dict() for item in self.manifest.bundles]
            return {
                "schema_version": "olivia.video-capability-status.v1",
                "status": "READY" if all(item["state"] == "ready" for item in bundles) else "UNAVAILABLE",
                "capability": "video",
                "install_locations": [{"root": "local_data_root", "relative_path": "capabilities/video"}],
                "bundles": bundles,
                "runtime_import": dict(self._runtime_import),
            }

    def _set_runtime_import_state(self, state: str) -> None:
        with self._lock:
            self._runtime_import["state"] = state

    def _update_runtime_import_progress(self, checked_bytes: int, total_bytes: int) -> None:
        with self._lock:
            self._runtime_import = {
                "state": "checking",
                "checked_bytes": checked_bytes,
                "total_bytes": total_bytes,
            }

    def _set(self, bundle: VideoBundle, state: VideoCapabilityState, downloaded: int, *, current: str | None = None, source: str | None = None, reason: str | None = None) -> None:
        self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, state, downloaded, sum(item.size_bytes for item in bundle.files), current, source, reason)

    def _write_runtime_environment(self) -> None:
        environment: dict[str, str] = {}
        for bundle in self.manifest.bundles:
            root = self._final_root(bundle)
            if not (root / ".ready.json").is_file():
                continue
            for key, relative in (bundle.runtime_environment or {}).items():
                candidate = _inside(root, root / relative)
                if candidate.exists():
                    environment[key] = str(candidate)
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
        with self._lock:
            thread = self._threads.get(bundle_id)
            if thread is not None and thread.is_alive():
                return "NOOP"
            if self._status.get(bundle_id, VideoBundleStatus(bundle_id, VideoCapabilityState.MISSING, 0, 0)).state in {
                VideoCapabilityState.READY,
                VideoCapabilityState.LICENSE_REVIEW_REQUIRED,
                VideoCapabilityState.PREREQUISITES_REQUIRED,
            }:
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
        with self._lock:
            self._runtime_import = {
                "state": "checking",
                "checked_bytes": 0,
                "total_bytes": 0,
            }
        try:
            result = self._import_runtime_root(
                runtime_root=runtime_root,
                manifest_sha256=manifest_sha256,
            )
        except Exception:
            self._set_runtime_import_state("failed")
            raise
        with self._lock:
            self._runtime_import["state"] = "ready"
            self._runtime_import["checked_bytes"] = self._runtime_import["total_bytes"]
        return result

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
        if not _runtime_environment_is_portable(external_environment, root):
            raise VideoCapabilityError("VIDEO_RUNTIME_NOT_PORTABLE")
        try:
            environment = load_video_runtime_environment(self.data_root)
        except VideoCapabilityError:
            environment = {}
        environment.update(external_environment)
        if "OLIVIA_TTS_CONFIG" not in environment:
            cosy_root = Path(environment.get("OLIVIA_COSYVOICE_ROOT", ""))
            model_root = Path(
                environment.get(
                    "OLIVIA_COSYVOICE_MODEL_ROOT",
                    str(
                        self._final_root(self._bundle("ordinary_video"))
                        / "cosyvoice"
                        / "model"
                    ),
                )
            )
            reference = Path(environment.get("OLIVIA_REPLY_VOICE_REFERENCE", ""))
            if not cosy_root.is_dir() or not model_root.is_dir() or not reference.is_file():
                raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE")
            generated_root = self.install_root / "generated"
            generated_root.mkdir(parents=True, exist_ok=True)
            generated_config = generated_root / "tts_local.json"
            generated_temporary = generated_config.with_name(
                f"{generated_config.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                generated_temporary.write_text(
                    json.dumps(
                        {
                            "schema_version": "b10b.module-config.v1",
                            "module_id": "tts-local",
                            "profile": "verified-offline-runtime",
                            "settings": {
                                "provider": "cosyvoice3",
                                "runtime_root": str(cosy_root),
                                "model_dir": str(model_root),
                                "reference_audio": str(reference),
                                "fallback": "text",
                                "fp16": True,
                            },
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                os.replace(generated_temporary, generated_config)
            except OSError as exc:
                raise VideoCapabilityError("VIDEO_RUNTIME_TTS_CONFIG_UNAVAILABLE") from exc
            finally:
                generated_temporary.unlink(missing_ok=True)
            environment["OLIVIA_TTS_CONFIG"] = str(generated_config)
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
            ready = False
        if not ready:
            raise VideoCapabilityError("VIDEO_RUNTIME_PROBE_FAILED")
        self.install_root.mkdir(parents=True, exist_ok=True)
        target = self.install_root / _RUNTIME_ENVIRONMENT_FILE
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "schema_version": "olivia.video-runtime-environment.v1",
            "environment": environment,
            "external_environment": external_environment,
            "runtime_root": str(root),
            "manifest_sha256": _safe_sha(manifest_sha256),
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError as exc:
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_WRITE_FAILED") from exc
        finally:
            temporary.unlink(missing_ok=True)
        with self._lock:
            self._load_status()
        return "APPLIED"

    def _run(self, bundle: VideoBundle, source_mode: str, offline_root: Path | None) -> None:
        root = self._staging_root(bundle)
        try:
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
            ]
            expected.extend(self._assemble_archives(root, bundle))
            self._set(bundle, VideoCapabilityState.VERIFYING, downloaded, source=source_used)
            final = self._final_root(bundle)
            if len({item["path"].casefold() for item in expected}) != len(expected):
                raise VideoCapabilityError("VIDEO_STAGED_TREE_INVALID")
            if _is_reparse_point(root):
                raise VideoCapabilityError("VIDEO_STAGING_INVALID")
            _verify_staged_tree(root, expected)
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
            with self._lock:
                state, reason = self._installed_state(final, bundle)
                self._set(
                    bundle,
                    state,
                    downloaded,
                    source=source_used,
                    reason=reason,
                )
        except InterruptedError:
            with self._lock:
                self._set(bundle, VideoCapabilityState.PAUSED, self._status.get(bundle.identifier, VideoBundleStatus(bundle.identifier, VideoCapabilityState.PAUSED, 0, 0)).downloaded_bytes, source=source_mode)
        except Exception:
            with self._lock:
                self._set(bundle, VideoCapabilityState.FAILED, self._status.get(bundle.identifier, VideoBundleStatus(bundle.identifier, VideoCapabilityState.FAILED, 0, 0)).downloaded_bytes, source=source_mode, reason="VIDEO_BUNDLE_INSTALL_FAILED")
        finally:
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)

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
) -> list[dict[str, object]]:
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
                if expanded > _MAX_ARCHIVE_EXPANDED_BYTES:
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


def _load_video_runtime_environment(data_root: Path) -> dict[str, str]:
    if not data_root.is_absolute():
        raise VideoCapabilityError("VIDEO_DATA_ROOT_INVALID")
    install_root = _checked_install_root(data_root.resolve(), create=False)
    _restore_interrupted_promotions(install_root)
    path = install_root / _RUNTIME_ENVIRONMENT_FILE
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID") from exc
    managed_fields = {"schema_version", "environment"}
    external_fields = managed_fields | {
        "external_environment",
        "runtime_root",
        "manifest_sha256",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) not in {frozenset(managed_fields), frozenset(external_fields)}
        or payload.get("schema_version") != "olivia.video-runtime-environment.v1"
        or not isinstance(payload.get("environment"), dict)
        or set(payload["environment"]) - _RUNTIME_ENVIRONMENT_KEYS
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
    external_environment: dict[str, str] | None = None
    if set(payload) == external_fields:
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
            external_environment = _load_runtime_root_manifest(
                Path(raw_root),
                str(payload.get("manifest_sha256", "")),
                verify_files=False,
            )
        except VideoCapabilityError as exc:
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
