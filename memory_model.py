"""Verified offline FastEmbed model cache for the optional Mem0 runtime.

The public repository contains only a manifest and verification logic.  Model
bytes are downloaded explicitly by the installer after license consent, checked
against the manifest, extracted without following archive links, and kept under
the installation-owned data root.  Runtime startup only accepts a cache whose
marker and required file hashes still match.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
from typing import Callable, Mapping
from urllib.parse import urlsplit
import uuid


MEMORY_MODEL_SCHEMA = "olivia.memory-model.v1"
MEMORY_MODEL_MARKER_SCHEMA = "olivia.memory-model-marker.v1"
MEMORY_MODEL_MARKER_NAME = ".olivia-memory-model.json"
_MAX_ARCHIVE_MEMBERS = 64
_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ALLOWED_ARCHIVE_HOST = "storage.googleapis.com"


class MemoryModelError(RuntimeError):
    """Stable model-provisioning failure without a machine-specific path."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MemoryModelManifest:
    schema_version: str
    provider: str
    provider_version: str
    model: str
    dimensions: int
    license: str
    archive_url: str
    archive_size: int
    archive_sha256: str
    archive_root: str
    required_files: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.archive_url)
        except ValueError as exc:
            raise MemoryModelError("MEMORY_MODEL_MANIFEST_INVALID") from exc
        if (
            self.schema_version != MEMORY_MODEL_SCHEMA
            or self.provider != "fastembed"
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", self.provider_version)
            or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", self.model)
            or type(self.dimensions) is not int
            or not 64 <= self.dimensions <= 8192
            or self.license.casefold() not in {"mit", "apache-2.0"}
            or parsed.scheme != "https"
            or parsed.hostname != _ALLOWED_ARCHIVE_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or type(self.archive_size) is not int
            or not 1 <= self.archive_size <= _MAX_UNCOMPRESSED_BYTES
            or not _SHA256_RE.fullmatch(self.archive_sha256)
            or not _TOKEN_RE.fullmatch(self.archive_root)
            or not self.required_files
            or len(self.required_files) > _MAX_ARCHIVE_MEMBERS
            or len(set(self.required_files)) != len(self.required_files)
        ):
            raise MemoryModelError("MEMORY_MODEL_MANIFEST_INVALID")
        for value in self.required_files:
            if not _safe_relative_name(value, allow_root=False):
                raise MemoryModelError("MEMORY_MODEL_MANIFEST_INVALID")

    def marker_identity(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "model": self.model,
            "dimensions": self.dimensions,
            "archive_sha256": self.archive_sha256,
        }


@dataclass(frozen=True)
class MemoryModelStatus:
    status: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"READY", "UNAVAILABLE"}:
            raise ValueError("memory model status is invalid")
        if self.reason_code is not None and not re.fullmatch(
            r"^[A-Z][A-Z0-9_]{0,95}$", self.reason_code
        ):
            raise ValueError("memory model reason is invalid")

    @property
    def ready(self) -> bool:
        return self.status == "READY"

    def to_dict(self) -> dict[str, str]:
        payload = {"status": self.status}
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        return payload


def _safe_relative_name(value: object, *, allow_root: bool) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    if not allow_root and len(path.parts) != 1:
        return False
    return True


def _load_json(path: Path, code: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemoryModelError(code) from exc
    if not isinstance(value, Mapping):
        raise MemoryModelError(code)
    return value


def load_memory_model_manifest(path: str | os.PathLike[str]) -> MemoryModelManifest:
    value = _load_json(
        Path(path).expanduser().resolve(),
        "MEMORY_MODEL_MANIFEST_INVALID",
    )
    required = {
        "schema_version",
        "provider",
        "provider_version",
        "model",
        "dimensions",
        "license",
        "archive_url",
        "archive_size",
        "archive_sha256",
        "archive_root",
        "required_files",
    }
    if set(value) != required or not isinstance(value.get("required_files"), list):
        raise MemoryModelError("MEMORY_MODEL_MANIFEST_INVALID")
    try:
        return MemoryModelManifest(
            schema_version=str(value["schema_version"]),
            provider=str(value["provider"]),
            provider_version=str(value["provider_version"]),
            model=str(value["model"]),
            dimensions=value["dimensions"],  # type: ignore[arg-type]
            license=str(value["license"]),
            archive_url=str(value["archive_url"]),
            archive_size=value["archive_size"],  # type: ignore[arg-type]
            archive_sha256=str(value["archive_sha256"]).casefold(),
            archive_root=str(value["archive_root"]),
            required_files=tuple(value["required_files"]),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise MemoryModelError("MEMORY_MODEL_MANIFEST_INVALID") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise MemoryModelError("MEMORY_MODEL_FILE_UNREADABLE") from exc
    return digest.hexdigest()


def verify_model_archive(path: Path, manifest: MemoryModelManifest) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNREADABLE") from exc
    if size != manifest.archive_size:
        raise MemoryModelError("MEMORY_MODEL_ARCHIVE_SIZE_MISMATCH")
    if sha256_file(path) != manifest.archive_sha256:
        raise MemoryModelError("MEMORY_MODEL_ARCHIVE_HASH_MISMATCH")


def _validated_members(
    archive: tarfile.TarFile,
    manifest: MemoryModelManifest,
) -> tuple[tarfile.TarInfo, ...]:
    members = tuple(archive.getmembers())
    if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
        raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
    seen: set[str] = set()
    total = 0
    present: set[str] = set()
    for member in members:
        name = member.name.rstrip("/")
        if not _safe_relative_name(name, allow_root=True):
            raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
        parts = PurePosixPath(name).parts
        if not parts or parts[0] != manifest.archive_root:
            raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
        folded = name.casefold()
        if folded in seen:
            raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
        seen.add(folded)
        if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isfifo():
            raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
        if not member.isdir() and not member.isfile():
            raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
        if member.isfile():
            if member.size < 0:
                raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
            total += member.size
            if total > _MAX_UNCOMPRESSED_BYTES:
                raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
            if len(parts) == 2:
                present.add(parts[1])
    if not set(manifest.required_files).issubset(present):
        raise MemoryModelError("MEMORY_MODEL_FILES_MISSING")
    return members


def extract_model_archive(
    archive_path: str | os.PathLike[str],
    cache_root: str | os.PathLike[str],
    manifest: MemoryModelManifest,
) -> Path:
    """Safely extract and atomically replace the one manifest-owned model dir."""

    archive_path = Path(archive_path).expanduser().resolve()
    cache_root = Path(cache_root).expanduser().resolve()
    verify_model_archive(archive_path, manifest)
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=".olivia-memory-model-",
                dir=cache_root,
            )
        )
    except OSError as exc:
        raise MemoryModelError("MEMORY_MODEL_CACHE_UNWRITABLE") from exc

    backup: Path | None = None
    final = cache_root / manifest.archive_root
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = _validated_members(archive, manifest)
            for member in members:
                relative = PurePosixPath(member.name.rstrip("/"))
                target = staging.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise MemoryModelError("MEMORY_MODEL_ARCHIVE_UNSAFE")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1 << 20)
        staged_model = staging / manifest.archive_root
        if not staged_model.is_dir():
            raise MemoryModelError("MEMORY_MODEL_FILES_MISSING")
        for name in manifest.required_files:
            if not (staged_model / name).is_file():
                raise MemoryModelError("MEMORY_MODEL_FILES_MISSING")
        if final.exists():
            backup = cache_root / (
                f".{manifest.archive_root}.backup-{uuid.uuid4().hex}"
            )
            os.replace(final, backup)
        os.replace(staged_model, final)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        return final
    except MemoryModelError:
        if backup is not None and backup.exists() and not final.exists():
            os.replace(backup, final)
        raise
    except (OSError, tarfile.TarError) as exc:
        if backup is not None and backup.exists() and not final.exists():
            os.replace(backup, final)
        raise MemoryModelError("MEMORY_MODEL_EXTRACTION_FAILED") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def configure_offline_model_environment(
    cache_root: Path,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    target = os.environ if environment is None else environment
    target["FASTEMBED_CACHE_PATH"] = str(cache_root.resolve())
    target["HF_HUB_OFFLINE"] = "1"
    target["HF_HUB_DISABLE_TELEMETRY"] = "1"
    target["DO_NOT_TRACK"] = "1"
    return target


def verify_fastembed_model(
    cache_root: Path,
    manifest: MemoryModelManifest,
    *,
    embedding_factory: Callable[..., object] | None = None,
) -> None:
    """Load the installed model offline and verify its actual vector width."""

    for name in manifest.required_files:
        if not (cache_root / manifest.archive_root / name).is_file():
            raise MemoryModelError("MEMORY_MODEL_FILES_MISSING")
    configure_offline_model_environment(cache_root)
    try:
        installed_version = importlib.metadata.version("fastembed")
    except importlib.metadata.PackageNotFoundError as exc:
        raise MemoryModelError("MEMORY_MODEL_PROVIDER_UNAVAILABLE") from exc
    if installed_version != manifest.provider_version:
        raise MemoryModelError("MEMORY_MODEL_PROVIDER_VERSION_MISMATCH")
    try:
        if embedding_factory is None:
            from fastembed import TextEmbedding

            embedding_factory = TextEmbedding
        model = embedding_factory(
            model_name=manifest.model,
            cache_dir=str(cache_root),
            local_files_only=True,
            threads=1,
        )
        values = list(model.embed(["这是一条离线中文向量自检文本。"]))  # type: ignore[attr-defined]
    except MemoryModelError:
        raise
    except Exception as exc:
        raise MemoryModelError("MEMORY_MODEL_OFFLINE_LOAD_FAILED") from exc
    if len(values) != 1 or len(values[0]) != manifest.dimensions:
        raise MemoryModelError("MEMORY_MODEL_DIMENSION_MISMATCH")


def _file_metadata(cache_root: Path, manifest: MemoryModelManifest) -> dict[str, object]:
    model_root = cache_root / manifest.archive_root
    result: dict[str, object] = {}
    for name in manifest.required_files:
        path = model_root / name
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise MemoryModelError("MEMORY_MODEL_FILES_MISSING") from exc
        result[name] = {
            "size": size,
            "sha256": sha256_file(path),
        }
    return result


def write_model_marker(cache_root: Path, manifest: MemoryModelManifest) -> Path:
    cache_root = cache_root.expanduser().resolve()
    payload = {
        "schema_version": MEMORY_MODEL_MARKER_SCHEMA,
        **manifest.marker_identity(),
        "files": _file_metadata(cache_root, manifest),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    temporary: Path | None = None
    marker = cache_root / MEMORY_MODEL_MARKER_NAME
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=cache_root,
            prefix=".olivia-memory-model-marker-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(encoded)
            temporary = Path(stream.name)
        os.replace(temporary, marker)
        return marker
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise MemoryModelError("MEMORY_MODEL_MARKER_WRITE_FAILED") from exc


def validate_model_cache(
    cache_root: str | os.PathLike[str],
    manifest: MemoryModelManifest,
) -> MemoryModelStatus:
    root = Path(cache_root).expanduser().resolve()
    marker_path = root / MEMORY_MODEL_MARKER_NAME
    try:
        marker = _load_json(marker_path, "MEMORY_MODEL_NOT_READY")
    except MemoryModelError as exc:
        return MemoryModelStatus("UNAVAILABLE", exc.code)
    identity = manifest.marker_identity()
    if (
        marker.get("schema_version") != MEMORY_MODEL_MARKER_SCHEMA
        or any(marker.get(name) != value for name, value in identity.items())
        or not isinstance(marker.get("files"), Mapping)
    ):
        return MemoryModelStatus("UNAVAILABLE", "MEMORY_MODEL_CACHE_INVALID")
    files = marker["files"]
    if set(files) != set(manifest.required_files):
        return MemoryModelStatus("UNAVAILABLE", "MEMORY_MODEL_CACHE_INVALID")
    for name in manifest.required_files:
        metadata = files.get(name)
        path = root / manifest.archive_root / name
        if (
            not isinstance(metadata, Mapping)
            or type(metadata.get("size")) is not int
            or not isinstance(metadata.get("sha256"), str)
            or not _SHA256_RE.fullmatch(str(metadata["sha256"]))
        ):
            return MemoryModelStatus("UNAVAILABLE", "MEMORY_MODEL_CACHE_INVALID")
        try:
            if path.stat().st_size != metadata["size"]:
                return MemoryModelStatus("UNAVAILABLE", "MEMORY_MODEL_CACHE_INVALID")
            if sha256_file(path) != metadata["sha256"]:
                return MemoryModelStatus("UNAVAILABLE", "MEMORY_MODEL_CACHE_INVALID")
        except (OSError, MemoryModelError):
            return MemoryModelStatus("UNAVAILABLE", "MEMORY_MODEL_CACHE_INVALID")
    return MemoryModelStatus("READY")


__all__ = [
    "MEMORY_MODEL_MARKER_NAME",
    "MEMORY_MODEL_SCHEMA",
    "MemoryModelError",
    "MemoryModelManifest",
    "MemoryModelStatus",
    "configure_offline_model_environment",
    "extract_model_archive",
    "load_memory_model_manifest",
    "sha256_file",
    "validate_model_cache",
    "verify_fastembed_model",
    "verify_model_archive",
    "write_model_marker",
]
