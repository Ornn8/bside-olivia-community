"""Small, resumable installer for the optional ordinary and music video bundles.

The installer owns only ``data/capabilities/video`` below the configured local
data root.  It downloads into a staging directory, verifies every declared
file, and promotes a complete bundle in one rename.  Private Olivia assets are
never downloaded: they are accepted only through a user-provided, hash-bound
offline manifest.
"""

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
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile


_SHA256 = 64
_PUBLIC_BUNDLES = {"ordinary_video", "music_video"}
_SOURCE_MODES = {"auto", "official"}
_SOURCE_IDS = {"domestic", "official"}
_PRIVATE_MANIFEST = ".olivia-video-assets.json"
_PRIVATE_FILES = {"ordinary_action_base", "official_reply_reference", "music_performance_base"}


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


@dataclass(frozen=True)
class VideoFile:
    identifier: str
    relative_path: str
    size_bytes: int
    sha256: str
    license: str
    sources: Mapping[str, str]
    redistributable: bool = True

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
    if not isinstance(value, str) or not value.strip():
        raise VideoCapabilityError("VIDEO_MANIFEST_PATH_INVALID")
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VideoCapabilityError("VIDEO_MANIFEST_PATH_INVALID")
    return path.as_posix()


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
    if set(raw) - required - {"redistributable"} or not required.issubset(raw):
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
    return VideoFile(
        identifier,
        _safe_relative(raw.get("path")),
        size,
        _safe_sha(raw.get("sha256")),
        license_name.strip(),
        normalized_sources,
        raw.get("redistributable", True) is True,
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
        if set(raw) - required - {"license_review_required"} or not required.issubset(raw):
            raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
        identifier = raw.get("id")
        if identifier not in _PUBLIC_BUNDLES or identifier in seen:
            raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
        dependencies = raw.get("dependencies")
        files = raw.get("files")
        if not isinstance(raw.get("label"), str) or raw.get("status") != "FIXED" or type(raw.get("requires_gpu")) is not bool or not isinstance(dependencies, list) or not all(isinstance(item, str) and item for item in dependencies) or not isinstance(files, list):
            raise VideoCapabilityError("VIDEO_MANIFEST_BUNDLE_INVALID")
        parsed_files = tuple(_load_file(item) for item in files)
        if len({item.identifier for item in parsed_files}) != len(parsed_files) or len({item.relative_path for item in parsed_files}) != len(parsed_files):
            raise VideoCapabilityError("VIDEO_MANIFEST_FILE_INVALID")
        seen.add(identifier)
        result.append(VideoBundle(identifier, raw["label"], raw["status"], raw["requires_gpu"], tuple(dependencies), parsed_files, raw.get("license_review_required", False) is True))
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


class VideoCapabilityInstaller:
    """Threaded, resumable public installer used by the local client API."""

    def __init__(self, *, data_root: Path, manifest: VideoManifest, opener: Callable[..., Any] = urlopen) -> None:
        if not data_root.is_absolute():
            raise VideoCapabilityError("VIDEO_DATA_ROOT_INVALID")
        self.data_root = data_root.resolve()
        self.install_root = self.data_root / "capabilities" / "video"
        self.manifest = manifest
        self._opener = opener
        self._lock = threading.RLock()
        self._pause = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._status: dict[str, VideoBundleStatus] = {}
        self._load_status()

    def _bundle(self, bundle_id: str) -> VideoBundle:
        for bundle in self.manifest.bundles:
            if bundle.identifier == bundle_id:
                return bundle
        raise VideoCapabilityError("VIDEO_BUNDLE_UNKNOWN")

    def _final_root(self, bundle: VideoBundle) -> Path:
        return self.install_root / bundle.identifier

    def _staging_root(self, bundle: VideoBundle) -> Path:
        return self.install_root / ".staging" / bundle.identifier

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
                ready = (root / ".ready.json").is_file() and all(_verify_and_true(root / item.relative_path, item) for item in bundle.files)
            except (OSError, VideoCapabilityError):
                ready = False
            if ready:
                self._status[bundle.identifier] = VideoBundleStatus(bundle.identifier, VideoCapabilityState.READY, sum(item.size_bytes for item in bundle.files), sum(item.size_bytes for item in bundle.files))
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

    def start(self, *, bundle_id: str, source_mode: str = "auto", offline_root: Path | None = None) -> str:
        bundle = self._bundle(bundle_id)
        if source_mode not in _SOURCE_MODES:
            raise VideoCapabilityError("VIDEO_SOURCE_MODE_INVALID")
        with self._lock:
            thread = self._threads.get(bundle_id)
            if thread is not None and thread.is_alive():
                return "NOOP"
            if self._status.get(bundle_id, VideoBundleStatus(bundle_id, VideoCapabilityState.MISSING, 0, 0)).state is VideoCapabilityState.READY:
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

    def resume(self, *, bundle_id: str, source_mode: str = "auto") -> str:
        return self.start(bundle_id=bundle_id, source_mode=source_mode)

    def retry(self, *, bundle_id: str, source_mode: str = "auto") -> str:
        return self.start(bundle_id=bundle_id, source_mode=source_mode)

    def import_offline(self, *, bundle_id: str, offline_root: Path, source_mode: str = "official") -> str:
        return self.start(bundle_id=bundle_id, source_mode=source_mode, offline_root=offline_root)

    def import_configured_offline(self, *, bundle_id: str, environment: Mapping[str, str]) -> str:
        raw = str(environment.get("OLIVIA_VIDEO_OFFLINE_ROOT", "")).strip()
        if not raw:
            raise VideoCapabilityError("VIDEO_OFFLINE_PACKAGE_NOT_SELECTED")
        root = Path(raw).expanduser()
        if not root.is_absolute():
            project_root = Path(str(environment.get("OLIVIA_PROJECT_ROOT", ""))).expanduser()
            if not project_root.is_absolute():
                raise VideoCapabilityError("VIDEO_OFFLINE_PACKAGE_NOT_SELECTED")
            root = project_root / root
        if not root.exists():
            raise VideoCapabilityError("VIDEO_OFFLINE_PACKAGE_NOT_SELECTED")
        return self.import_offline(bundle_id=bundle_id, offline_root=root)

    def import_official_assets(self, source_root: Path) -> str:
        """Import only a hash-bound private manifest from an explicitly chosen root."""
        source_root = source_root.resolve()
        manifest_path = _inside(source_root, source_root / _PRIVATE_MANIFEST)
        try:
            payload: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VideoCapabilityError("VIDEO_PRIVATE_ASSET_MANIFEST_REQUIRED") from exc
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "assets"} or payload["schema_version"] != "olivia.private-video-assets.v1" or not isinstance(payload["assets"], list):
            raise VideoCapabilityError("VIDEO_PRIVATE_ASSET_MANIFEST_INVALID")
        staging = self.install_root / ".staging" / "private-assets"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            for raw in payload["assets"]:
                if not isinstance(raw, dict) or set(raw) != {"id", "path", "size_bytes", "sha256"} or raw["id"] not in _PRIVATE_FILES or type(raw["size_bytes"]) is not int:
                    raise VideoCapabilityError("VIDEO_PRIVATE_ASSET_MANIFEST_INVALID")
                source = _inside(source_root, source_root / _safe_relative(raw["path"]))
                target = _inside(staging, staging / f"{raw['id']}{source.suffix.lower()}")
                _verify(source, VideoFile(str(raw["id"]), source.name, raw["size_bytes"], _safe_sha(raw["sha256"]), "private-user-supplied", {}))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            if {item["id"] for item in payload["assets"]} != _PRIVATE_FILES:
                raise VideoCapabilityError("VIDEO_PRIVATE_ASSET_MANIFEST_INVALID")
            final = self.install_root / "private-assets"
            backup = self.install_root / ".private-assets.backup"
            if backup.exists():
                shutil.rmtree(backup)
            if final.exists():
                os.replace(final, backup)
            try:
                os.replace(staging, final)
            except Exception:
                if backup.exists() and not final.exists():
                    os.replace(backup, final)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            (final / ".ready.json").write_text(json.dumps({"schema_version": "olivia.private-video-assets.v1", "assets": sorted(_PRIVATE_FILES)}), encoding="utf-8")
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return "APPLIED"

    def import_configured_assets(self, environment: Mapping[str, str]) -> str:
        """Copy the three already-selected official files without publishing paths."""
        project_root = Path(str(environment.get("OLIVIA_PROJECT_ROOT", ""))).expanduser()
        assets: list[dict[str, object]] = []
        for asset_id, key in (
            ("ordinary_action_base", "OLIVIA_ORDINARY_ACTION_BASE"),
            ("official_reply_reference", "OLIVIA_OFFICIAL_REPLY_REFERENCE"),
            ("music_performance_base", "OLIVIA_MUSIC_PERFORMANCE_BASE"),
        ):
            raw = str(environment.get(key, "")).strip()
            if not raw:
                raise VideoCapabilityError("VIDEO_PRIVATE_ASSETS_NOT_FOUND")
            source = Path(raw).expanduser()
            if not source.is_absolute() and project_root.is_absolute():
                source = project_root / source
            source = source.resolve()
            if not source.is_file():
                raise VideoCapabilityError("VIDEO_PRIVATE_ASSETS_NOT_FOUND")
            size, digest = _sha256_file(source)
            assets.append({"id": asset_id, "path": source.name, "size_bytes": size, "sha256": digest, "source": source})
        staging = self.install_root / ".staging" / "private-assets"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            for asset in assets:
                target = staging / f"{asset['id']}{Path(str(asset['path'])).suffix.lower()}"
                shutil.copy2(Path(str(asset["source"])), target)
            final = self.install_root / "private-assets"
            backup = self.install_root / ".private-assets.backup"
            if backup.exists():
                shutil.rmtree(backup)
            if final.exists():
                os.replace(final, backup)
            os.replace(staging, final)
            if backup.exists():
                shutil.rmtree(backup)
            (final / ".ready.json").write_text(json.dumps({"schema_version": "olivia.private-video-assets.v1", "assets": sorted(item["id"] for item in assets)}), encoding="utf-8")
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return "APPLIED"

    def _run(self, bundle: VideoBundle, source_mode: str, offline_root: Path | None) -> None:
        try:
            root = self._staging_root(bundle)
            root.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            source_used = source_mode
            for item in bundle.files:
                if self._pause.is_set():
                    raise InterruptedError
                self._set(bundle, VideoCapabilityState.DOWNLOADING, downloaded, current=item.relative_path, source=source_used)
                target = _inside(root, root / item.relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if offline_root is not None:
                    self._copy_offline(offline_root, item, target)
                else:
                    source_used = self._download(item, target, source_mode)
                _verify(target, item)
                downloaded += item.size_bytes
            self._set(bundle, VideoCapabilityState.VERIFYING, downloaded, source=source_used)
            final = self._final_root(bundle)
            backup = self.install_root / f".{bundle.identifier}.backup"
            if backup.exists():
                shutil.rmtree(backup)
            if final.exists():
                os.replace(final, backup)
            try:
                os.replace(root, final)
            except Exception:
                if backup.exists() and not final.exists():
                    os.replace(backup, final)
                raise
            (final / ".ready.json").write_text(json.dumps({"schema_version": "olivia.video-bundle.v1", "bundle": bundle.identifier, "version": self.manifest.version}), encoding="utf-8")
            if backup.exists():
                shutil.rmtree(backup)
            with self._lock:
                self._set(bundle, VideoCapabilityState.READY, downloaded, source=source_used)
        except InterruptedError:
            with self._lock:
                self._set(bundle, VideoCapabilityState.PAUSED, self._status.get(bundle.identifier, VideoBundleStatus(bundle.identifier, VideoCapabilityState.PAUSED, 0, 0)).downloaded_bytes, source=source_mode)
        except Exception:
            with self._lock:
                self._set(bundle, VideoCapabilityState.FAILED, self._status.get(bundle.identifier, VideoBundleStatus(bundle.identifier, VideoCapabilityState.FAILED, 0, 0)).downloaded_bytes, source=source_mode, reason="VIDEO_BUNDLE_INSTALL_FAILED")

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

    def _download(self, item: VideoFile, target: Path, source_mode: str) -> str:
        sources = [source_mode] if source_mode == "official" else ["domestic", "official"]
        part = target.with_name(target.name + ".part")
        for source_id in sources:
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
                if part.stat().st_size != item.size_bytes:
                    raise VideoCapabilityError("VIDEO_FILE_SIZE_INVALID")
                _verify(part, item)
                os.replace(part, target)
                return source_id
            except InterruptedError:
                raise
            except (HTTPError, URLError, OSError, TimeoutError, VideoCapabilityError):
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


__all__ = [
    "VideoBundle",
    "VideoBundleStatus",
    "VideoCapabilityError",
    "VideoCapabilityInstaller",
    "VideoCapabilityState",
    "VideoFile",
    "VideoManifest",
    "load_video_manifest",
]
