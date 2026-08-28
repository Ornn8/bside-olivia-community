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
    "OLIVIA_LATENTSYNC_ROOT",
    "OLIVIA_MINIMAX_COMFY_ROOT",
    "OLIVIA_ROFORMER_MODEL_PATH",
    "OLIVIA_ROFORMER_CONFIG_PATH",
    "OLIVIA_SEED_VC_ROOT",
}
_MAX_ARCHIVE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
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
        if set(raw) - required - {"license_review_required", "runtime_environment"} or not required.issubset(raw):
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
        seen.add(identifier)
        result.append(VideoBundle(identifier, raw["label"], raw["status"], raw["requires_gpu"], tuple(dependencies), parsed_files, raw.get("license_review_required", False) is True, normalized_runtime))
    if seen != _PUBLIC_BUNDLES:
        raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
    return VideoManifest(payload["version"], tuple(result))


def _sha256_file(path: Path, *, pause: threading.Event | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if pause is not None and pause.is_set():
                raise InterruptedError
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def _verify(path: Path, spec: VideoFile) -> None:
    size, digest = _sha256_file(path)
    if size != spec.size_bytes or digest != spec.sha256:
        raise VideoCapabilityError("VIDEO_FILE_VERIFICATION_FAILED")


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

    def __init__(self, *, data_root: Path, manifest: VideoManifest, opener: Callable[..., Any] = urlopen) -> None:
        if not data_root.is_absolute():
            raise VideoCapabilityError("VIDEO_DATA_ROOT_INVALID")
        self.data_root = data_root.resolve()
        self.install_root = _checked_install_root(self.data_root, create=True)
        self.manifest = manifest
        self._opener = opener
        self._lock = threading.RLock()
        self._commit_lock = _PROMOTION_LOCK
        self._pause = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._status: dict[str, VideoBundleStatus] = {}
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
            ready = all(
                _inside(root, root / relative).exists()
                for relative in (bundle.runtime_environment or {}).values()
            )
            if "OLIVIA_SEED_VC_ROOT" in (bundle.runtime_environment or {}):
                seed_root = _inside(
                    root, root / bundle.runtime_environment["OLIVIA_SEED_VC_ROOT"]
                )
                ready = ready and (
                    seed_root / ".olivia-overlap-frames-patched.json"
                ).is_file()
            if bundle.runtime_environment:
                persisted = load_video_runtime_environment(self.data_root)
                ready = ready and all(
                    persisted.get(key) == str(_inside(root, root / relative))
                    for key, relative in bundle.runtime_environment.items()
                )
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
                ready = (
                    (root / ".ready.json").is_file()
                    and all(_verify_and_true(root / item.relative_path, item) for item in bundle.files)
                    and self._runtime_wiring_ready(root, bundle)
                )
            except (OSError, VideoCapabilityError):
                ready = False
            if ready:
                state, reason = self._assembled_state(bundle)
                self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, state, sum(item.size_bytes for item in bundle.files), sum(item.size_bytes for item in bundle.files), reason_code=reason)
            elif current is None or current.state not in {VideoCapabilityState.FAILED, VideoCapabilityState.PAUSED}:
                self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, VideoCapabilityState.MISSING, 0, sum(item.size_bytes for item in bundle.files))

    def status(self) -> dict[str, object]:
        with self._lock:
            self._load_status()
            bundles = [self._status[item.identifier].to_dict() for item in self.manifest.bundles]
            return {
                "schema_version": "olivia.video-capability-status.v1",
                "status": "READY" if all(item["state"] == "ready" for item in bundles) else "UNAVAILABLE",
                "capability": "video",
                "install_locations": [{"root": "local_data_root", "relative_path": "capabilities/video"}],
                "bundles": bundles,
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
                if not candidate.exists():
                    raise VideoCapabilityError("VIDEO_RUNTIME_WIRING_INCOMPLETE")
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
            (root / ".ready.json").write_text(json.dumps({"schema_version": "olivia.video-bundle.v1", "bundle": bundle.identifier, "version": self.manifest.version}), encoding="utf-8")
            expected.append(_tree_entry(root, ".ready.json"))
            if len({item["path"].casefold() for item in expected}) != len(expected):
                raise VideoCapabilityError("VIDEO_STAGED_TREE_INVALID")
            if _is_reparse_point(root):
                raise VideoCapabilityError("VIDEO_STAGING_INVALID")
            _verify_staged_tree(root, expected)
            self._promote_directory(root, final, refresh_environment=True)
            with self._lock:
                state, reason = self._assembled_state(bundle)
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
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    expected: list[dict[str, object]] = []
    expanded = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                mode = (member.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise VideoCapabilityError("VIDEO_ARCHIVE_LINK_FORBIDDEN")
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
        _verify_staged_tree(destination, expected)
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
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "environment"}
        or payload.get("schema_version") != "olivia.video-runtime-environment.v1"
        or not isinstance(payload.get("environment"), dict)
        or set(payload["environment"]) - _RUNTIME_ENVIRONMENT_KEYS
    ):
        raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
    result: dict[str, str] = {}
    for key, raw in payload["environment"].items():
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
        candidate = _inside(install_root, Path(raw))
        if not candidate.exists():
            raise VideoCapabilityError("VIDEO_RUNTIME_ENVIRONMENT_INVALID")
        result[key] = str(candidate)
    return result


def load_video_runtime_environment(data_root: Path) -> dict[str, str]:
    with _PROMOTION_LOCK:
        return _load_video_runtime_environment(data_root)


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


__all__ = ["apply_seed_vc_overlap_frames_patch", "VideoBundle", "VideoBundleStatus", "VideoCapabilityError", "VideoCapabilityInstaller", "VideoCapabilityState", "VideoFile", "VideoFileInstall", "VideoManifest", "load_video_manifest", "load_video_runtime_environment"]
