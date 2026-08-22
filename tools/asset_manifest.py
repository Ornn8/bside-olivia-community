"""Create and validate privacy-bounded local asset manifests.

The scanner reads user-supplied roots and writes only explicitly requested
outputs. Private manifests contain source-relative paths and hashes, while
the committed summary contains counts only. No source path or media content
is emitted by the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
TOOL_VERSION = "1"
ROOT_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOGICAL_ID_RE = re.compile(r"^asset_[0-9a-f]{32}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm", ".wmv"}
)
AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mid", ".midi", ".mp3", ".ogg", ".opus", ".wav"}
)
MEDIA_CATEGORIES = frozenset({"image", "video", "audio"})
CATEGORIES = frozenset({"image", "video", "audio", "other"})
PROBE_STATUSES = frozenset({"ok", "unavailable", "error", "not_applicable"})
ITEM_REQUIRED_FIELDS = frozenset(
    {
        "logical_id",
        "root_alias",
        "relative_path",
        "extension",
        "category",
        "bytes",
        "sha256",
        "media_metadata",
        "probe_status",
        "reason",
    }
)


class ManifestError(Exception):
    """An expected, privacy-safe CLI failure represented by a short code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class MediaProbeError(Exception):
    pass


class MediaProbeUnavailable(Exception):
    pass


@dataclass(frozen=True)
class RootSpec:
    alias: str
    path: Path


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[str, ...]
    duplicate_sha256_groups: int
    item_count: int
    missing_files: int | None
    hash_mismatches: int

    @property
    def ok(self) -> bool:
        return not self.issues


def _repo_root(value: str | Path | None = None) -> Path:
    candidate = Path(value) if value is not None else Path(__file__).resolve().parents[1]
    try:
        resolved = candidate.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManifestError("repo_root") from exc
    if not resolved.is_dir():
        raise ManifestError("repo_root")
    return resolved


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _evidence_root(repo_root: Path) -> Path:
    return repo_root / ".evidence"


def _relative_repo_path(candidate: Path, repo_root: Path) -> str:
    try:
        return candidate.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ManifestError("output_boundary") from exc


def _git_ignored(repo_root: Path, candidate: Path) -> bool:
    relative = _relative_repo_path(candidate, repo_root)
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", relative],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError("ignore_check") from exc
    return result.returncode == 0


def ensure_private_output_path(value: str | Path, repo_root: str | Path | None = None) -> Path:
    """Resolve an explicitly supplied output and require ignored .evidence scope."""

    root = _repo_root(repo_root)
    try:
        candidate = Path(value).expanduser().resolve(strict=False)
        evidence = _evidence_root(root).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ManifestError("output_boundary") from exc
    if candidate == evidence or not _is_within(candidate, evidence):
        raise ManifestError("output_boundary")
    if not _git_ignored(root, candidate):
        raise ManifestError("output_not_ignored")
    return candidate


def ensure_repo_output_path(
    value: str | Path,
    repo_root: str | Path | None = None,
    *,
    allow_evidence: bool = False,
) -> Path:
    """Resolve a committed sanitized output inside the repository only."""

    root = _repo_root(repo_root)
    try:
        candidate = Path(value).expanduser().resolve(strict=False)
        evidence = _evidence_root(root).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ManifestError("output_boundary") from exc
    if not _is_within(candidate, root) or candidate == root:
        raise ManifestError("output_boundary")
    if not allow_evidence and _is_within(candidate, evidence):
        raise ManifestError("output_boundary")
    return candidate


def parse_root_spec(spec: str) -> RootSpec:
    """Parse alias=path without ever exposing the path in an exception."""

    if not isinstance(spec, str) or "=" not in spec:
        raise ManifestError("root_spec")
    alias, raw_path = spec.split("=", 1)
    if not ROOT_ALIAS_RE.fullmatch(alias) or not raw_path:
        raise ManifestError("root_spec")
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManifestError("root_path") from exc
    if not path.is_dir():
        raise ManifestError("root_path")
    return RootSpec(alias=alias, path=path)


def parse_root_specs(specs: Iterable[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for spec in specs:
        parsed = parse_root_spec(spec)
        if parsed.alias in roots:
            raise ManifestError("duplicate_root_alias")
        roots[parsed.alias] = parsed.path
    if not roots:
        raise ManifestError("root_required")
    return dict(sorted(roots.items()))


def classify_extension(extension: str) -> str:
    extension = extension.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    return "other"


def _extension(relative_path: str) -> str:
    return PurePosixPath(relative_path).suffix.lower()


def _safe_relative_path(relative_path: Any) -> bool:
    if not isinstance(relative_path, str) or not relative_path:
        return False
    if "\x00" in relative_path or "\\" in relative_path:
        return False
    if relative_path.startswith("/") or WINDOWS_ABSOLUTE_RE.match(relative_path):
        return False
    parts = relative_path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _safe_alias(alias: Any) -> bool:
    return isinstance(alias, str) and ROOT_ALIAS_RE.fullmatch(alias) is not None


def logical_id(root_alias: str, category: str, relative_path: str) -> str:
    payload = f"{root_alias}\0{category}\0{relative_path}".encode("utf-8")
    return "asset_" + hashlib.sha256(payload).hexdigest()[:32]


def _safe_relative_for_source(path: Path, root: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
        if not _is_within(resolved, root):
            raise ManifestError("path_escape")
        relative = path.relative_to(root).as_posix()
    except ManifestError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ManifestError("path_escape") from exc
    if not _safe_relative_path(relative):
        raise ManifestError("path_escape")
    return relative


def _iter_files(root: Path) -> Iterable[Path]:
    def visit(directory: Path) -> Iterable[Path]:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise ManifestError("read_root") from exc
        for entry in entries:
            candidate = Path(entry.path)
            try:
                if entry.is_symlink():
                    resolved = candidate.resolve(strict=True)
                    if not _is_within(resolved, root):
                        raise ManifestError("path_escape")
                    if resolved.is_dir():
                        # Do not follow directory links; this avoids cycles and
                        # keeps each root's enumeration unambiguous.
                        continue
                    if resolved.is_file():
                        yield candidate
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from visit(candidate)
                elif entry.is_file(follow_symlinks=False):
                    yield candidate
            except ManifestError:
                raise
            except OSError as exc:
                raise ManifestError("read_root") from exc

    yield from visit(root)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                digest.update(block)
    except OSError as exc:
        raise ManifestError("read_file") from exc
    return total, digest.hexdigest()


def _empty_media_metadata() -> dict[str, Any]:
    return {"image": None, "video": None, "audio": None}


def _image_metadata_from_header(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = handle.read(2 * 1024 * 1024)
    except OSError as exc:
        raise MediaProbeError from exc

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise MediaProbeError
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if not width or not height:
            raise MediaProbeError
        return {
            "format": "PNG",
            "width": width,
            "height": height,
            "bit_depth": data[24] if len(data) > 24 else None,
        }

    if data[:6] in {b"GIF87a", b"GIF89a"}:
        if len(data) < 10:
            raise MediaProbeError
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        if not width or not height:
            raise MediaProbeError
        return {"format": "GIF", "width": width, "height": height}

    if data[:2] == b"BM":
        if len(data) < 26:
            raise MediaProbeError
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = abs(int.from_bytes(data[22:26], "little", signed=True))
        if width <= 0 or height <= 0:
            raise MediaProbeError
        return {"format": "BMP", "width": width, "height": height}

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if len(data) < 16:
            raise MediaProbeError
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return {"format": "WEBP", "width": width, "height": height}
        # A valid WebP with a codec variant not handled by the stdlib still
        # yields useful format metadata.
        if chunk in {b"VP8 ", b"VP8L"}:
            return {"format": "WEBP"}
        raise MediaProbeError

    if data[:2] == b"\xff\xd8":
        index = 2
        sof_markers = {
            *range(0xC0, 0xC4),
            *range(0xC5, 0xC8),
            *range(0xC9, 0xCC),
            *range(0xCD, 0xD0),
        }
        while index + 3 < len(data):
            while index < len(data) and data[index] != 0xFF:
                index += 1
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index : index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in sof_markers:
                if length < 7:
                    raise MediaProbeError
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                if not width or not height:
                    raise MediaProbeError
                return {"format": "JPEG", "width": width, "height": height}
            index += length
        raise MediaProbeError

    if path.suffix.lower() not in {".ico", ".tif", ".tiff"}:
        raise MediaProbeError

    # Pillow is optional. It improves coverage for TIFF/ICO and unusual
    # JPEG/WebP layouts without making the CLI depend on a third-party package.
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise MediaProbeUnavailable from exc
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise MediaProbeError
            return {"format": str(image.format or "unknown"), "width": width, "height": height}
    except MediaProbeError:
        raise
    except Exception as exc:  # Pillow exposes several format-specific errors.
        raise MediaProbeError from exc


def _wav_metadata(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            sample_width = handle.getsampwidth()
    except (OSError, wave.Error) as exc:
        raise MediaProbeError from exc
    if channels <= 0 or sample_rate <= 0:
        raise MediaProbeError
    return {
        "format": "WAV",
        "channels": channels,
        "sample_rate": sample_rate,
        "sample_width_bytes": sample_width,
        "frames": frames,
        "duration_seconds": round(frames / sample_rate, 6),
    }


def _midi_metadata(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            header = handle.read(14)
    except OSError as exc:
        raise MediaProbeError from exc
    if len(header) < 14 or header[:4] != b"MThd":
        raise MediaProbeError
    length = int.from_bytes(header[4:8], "big")
    if length < 6:
        raise MediaProbeError
    return {
        "format": "MIDI",
        "file_format": int.from_bytes(header[8:10], "big"),
        "tracks": int.from_bytes(header[10:12], "big"),
        "ticks_per_beat": int.from_bytes(header[12:14], "big"),
    }


def _ratio(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(value)
    except (ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return round(result, 6)


def _ffprobe_metadata(path: Path, category: str) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise MediaProbeUnavailable
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeUnavailable from exc
    if result.returncode != 0:
        raise MediaProbeError
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise MediaProbeError from exc
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise MediaProbeError
    selected = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == category),
        None,
    )
    if not isinstance(selected, dict):
        raise MediaProbeError
    metadata: dict[str, Any] = {}
    codec = selected.get("codec_name")
    if isinstance(codec, str) and codec:
        metadata["codec"] = codec
    if category == "video":
        for key in ("width", "height"):
            value = selected.get(key)
            if isinstance(value, int) and value > 0:
                metadata[key] = value
        frame_rate = _ratio(selected.get("avg_frame_rate") or selected.get("r_frame_rate"))
        if frame_rate is not None:
            metadata["frame_rate"] = frame_rate
    if category == "audio":
        channels = selected.get("channels")
        if isinstance(channels, int) and channels > 0:
            metadata["channels"] = channels
        sample_rate = selected.get("sample_rate")
        try:
            sample_rate_int = int(sample_rate)
        except (TypeError, ValueError):
            sample_rate_int = 0
        if sample_rate_int > 0:
            metadata["sample_rate"] = sample_rate_int
    format_data = payload.get("format")
    if isinstance(format_data, dict):
        duration = format_data.get("duration")
        try:
            duration_float = float(duration)
        except (TypeError, ValueError):
            duration_float = 0.0
        if math.isfinite(duration_float) and duration_float >= 0:
            metadata["duration_seconds"] = round(duration_float, 6)
    if not metadata:
        raise MediaProbeError
    return metadata


def probe_media(path: Path, category: str) -> tuple[dict[str, Any], str, str | None]:
    metadata = _empty_media_metadata()
    if category == "other":
        return metadata, "not_applicable", "not_media"
    try:
        if category == "image":
            probed = _image_metadata_from_header(path)
        elif path.suffix.lower() == ".wav" and category == "audio":
            probed = _wav_metadata(path)
        elif path.suffix.lower() in {".mid", ".midi"} and category == "audio":
            probed = _midi_metadata(path)
        else:
            probed = _ffprobe_metadata(path, category)
    except MediaProbeUnavailable:
        return metadata, "unavailable", "probe_tool_unavailable"
    except MediaProbeError:
        return metadata, "error", "invalid_media"
    except (OSError, RuntimeError):
        return metadata, "error", "unreadable"
    metadata[category] = probed
    return metadata, "ok", None


def scan_roots(roots: Mapping[str, Path]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    root_records: list[dict[str, Any]] = []
    for alias, root_value in sorted(roots.items()):
        root = Path(root_value)
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ManifestError("root_path") from exc
        if not resolved_root.is_dir():
            raise ManifestError("root_path")
        root_count = 0
        for path in _iter_files(resolved_root):
            relative_path = _safe_relative_for_source(path, resolved_root)
            extension = _extension(relative_path)
            category = classify_extension(extension)
            byte_count, digest = _hash_file(path)
            media_metadata, probe_status, reason = probe_media(path, category)
            items.append(
                {
                    "logical_id": logical_id(alias, category, relative_path),
                    "root_alias": alias,
                    "relative_path": relative_path,
                    "extension": extension,
                    "category": category,
                    "bytes": byte_count,
                    "sha256": digest,
                    "media_metadata": media_metadata,
                    "probe_status": probe_status,
                    "reason": reason,
                }
            )
            root_count += 1
        root_records.append({"alias": alias, "item_count": root_count})
    items.sort(key=lambda item: (item["root_alias"], item["relative_path"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": "private_asset_manifest",
        "tool_version": TOOL_VERSION,
        "roots": root_records,
        "items": items,
    }


def _valid_metadata_shape(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"image", "video", "audio"}:
        return False
    return all(entry is None or isinstance(entry, dict) for entry in value.values())


def validate_manifest_document(
    manifest: Any,
    roots: Mapping[str, Path] | None = None,
) -> ValidationReport:
    issues: list[str] = []
    if not isinstance(manifest, dict):
        return ValidationReport(("schema",), 0, 0, None, 0)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        issues.append("schema")
    if manifest.get("manifest_kind") != "private_asset_manifest":
        issues.append("schema")
    if manifest.get("tool_version") != TOOL_VERSION:
        issues.append("schema")

    raw_roots = manifest.get("roots")
    root_aliases: set[str] = set()
    expected_counts: dict[str, int] = {}
    if not isinstance(raw_roots, list):
        issues.append("schema")
        raw_roots = []
    for root in raw_roots:
        if not isinstance(root, dict) or set(root) != {"alias", "item_count"}:
            issues.append("schema")
            continue
        alias = root.get("alias")
        count = root.get("item_count")
        if not _safe_alias(alias) or alias in root_aliases:
            issues.append("schema")
        else:
            root_aliases.add(alias)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            issues.append("schema")
        elif isinstance(alias, str):
            expected_counts[alias] = count

    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        issues.append("schema")
        raw_items = []
    seen_ids: set[str] = set()
    sha_counts: Counter[str] = Counter()
    valid_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict) or not ITEM_REQUIRED_FIELDS.issubset(item):
            issues.append("schema")
            continue
        if set(item) != ITEM_REQUIRED_FIELDS:
            issues.append("schema")
        alias = item.get("root_alias")
        relative_path = item.get("relative_path")
        category = item.get("category")
        extension = item.get("extension")
        if not _safe_alias(alias) or alias not in root_aliases:
            issues.append("schema")
        if not _safe_relative_path(relative_path):
            issues.append("path_escape")
        expected_extension = _extension(relative_path) if isinstance(relative_path, str) else None
        if not isinstance(extension, str) or extension != expected_extension:
            issues.append("schema")
        if category not in CATEGORIES or (
            isinstance(extension, str) and category != classify_extension(extension)
        ):
            issues.append("schema")
        byte_count = item.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            issues.append("schema")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            issues.append("hash_format")
        else:
            sha_counts[digest] += 1
        identifier = item.get("logical_id")
        if (
            not isinstance(identifier, str)
            or not LOGICAL_ID_RE.fullmatch(identifier)
            or not isinstance(alias, str)
            or not isinstance(category, str)
            or not isinstance(relative_path, str)
            or identifier != logical_id(alias, category, relative_path)
            or identifier in seen_ids
        ):
            issues.append("schema")
        else:
            seen_ids.add(identifier)
        if not _valid_metadata_shape(item.get("media_metadata")):
            issues.append("schema")
        status = item.get("probe_status")
        reason = item.get("reason")
        if status not in PROBE_STATUSES or not (reason is None or isinstance(reason, str)):
            issues.append("schema")
        if status == "not_applicable" and reason != "not_media":
            issues.append("schema")
        if status == "ok" and reason is not None:
            issues.append("schema")
        valid_items.append(item)

    actual_counts = Counter(item.get("root_alias") for item in valid_items if isinstance(item.get("root_alias"), str))
    for alias, expected in expected_counts.items():
        if actual_counts[alias] != expected:
            issues.append("schema")
    if set(actual_counts) - root_aliases:
        issues.append("schema")

    missing_files: int | None = None
    hash_mismatches = 0
    if roots is not None:
        missing_files = 0
        for alias, root in roots.items():
            if not _safe_alias(alias) or not Path(root).is_dir():
                issues.append("root_path")
        for item in valid_items:
            alias = item.get("root_alias")
            relative_path = item.get("relative_path")
            if alias not in roots or not isinstance(relative_path, str) or not _safe_relative_path(relative_path):
                continue
            root = Path(roots[alias])
            candidate = root.joinpath(*relative_path.split("/"))
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                issues.append("path_escape")
                continue
            if not _is_within(resolved, root.resolve(strict=False)):
                issues.append("path_escape")
                continue
            if not resolved.is_file():
                missing_files += 1
                continue
            try:
                byte_count, digest = _hash_file(resolved)
            except ManifestError:
                missing_files += 1
                continue
            if byte_count != item.get("bytes") or digest != item.get("sha256"):
                hash_mismatches += 1
        if missing_files:
            issues.append("missing_file")
        if hash_mismatches:
            issues.append("hash_mismatch")

    duplicate_groups = sum(1 for count in sha_counts.values() if count > 1)
    return ValidationReport(
        tuple(issues),
        duplicate_groups,
        len(raw_items),
        missing_files,
        hash_mismatches,
    )


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest_read") from exc


def build_sanitized_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    by_alias: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    by_extension: Counter[str] = Counter()
    by_probe_status: Counter[str] = Counter()
    for root in manifest.get("roots", []):
        alias = root.get("alias")
        if isinstance(alias, str):
            by_alias[alias] += 0
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        for counter, key in (
            (by_alias, "root_alias"),
            (by_category, "category"),
            (by_extension, "extension"),
            (by_probe_status, "probe_status"),
        ):
            value = item.get(key)
            if isinstance(value, str):
                counter[value] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "counts": {
            "by_alias": dict(sorted(by_alias.items())),
            "by_category": dict(sorted(by_category.items())),
            "by_extension": dict(sorted(by_extension.items())),
            "by_probe_status": dict(sorted(by_probe_status.items())),
        },
    }


def schema_document() -> dict[str, Any]:
    metadata_schema = {
        "type": ["object", "null"],
        "additionalProperties": True,
    }
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(ITEM_REQUIRED_FIELDS),
        "properties": {
            "logical_id": {"type": "string", "pattern": r"^asset_[0-9a-f]{32}$"},
            "root_alias": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"},
            "relative_path": {
                "type": "string",
                "pattern": r"^(?!/)(?!\\)(?![A-Za-z]:[\\/])(?!.*(?:^|/)\.\.(?:/|$))[^\u0000]+$",
            },
            "extension": {"type": "string", "pattern": r"^(|\.[a-z0-9][a-z0-9._+-]*)$"},
            "category": {"enum": ["audio", "image", "other", "video"]},
            "bytes": {"type": "integer", "minimum": 0},
            "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
            "media_metadata": {
                "type": "object",
                "additionalProperties": False,
                "required": ["audio", "image", "video"],
                "properties": {
                    "image": metadata_schema,
                    "video": metadata_schema,
                    "audio": metadata_schema,
                },
            },
            "probe_status": {"enum": ["error", "not_applicable", "ok", "unavailable"]},
            "reason": {"type": ["string", "null"]},
        },
    }
    return {
        "$schema": "https://example.invalid/endpoint",
        "$id": "asset-manifest.schema.json",
        "title": "Private local asset manifest",
        "type": "object",
        "additionalProperties": False,
        "required": ["items", "manifest_kind", "roots", "schema_version", "tool_version"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "manifest_kind": {"const": "private_asset_manifest"},
            "tool_version": {"const": TOOL_VERSION},
            "roots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["alias", "item_count"],
                    "properties": {
                        "alias": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"},
                        "item_count": {"type": "integer", "minimum": 0},
                    },
                },
            },
            "items": {"type": "array", "items": item_schema},
        },
    }


def example_document() -> dict[str, Any]:
    """A sanitized example: counts only, with no source-relative names."""

    return {
        "schema_version": SCHEMA_VERSION,
        "counts": {
            "by_alias": {"fixture_a": 3},
            "by_category": {"audio": 1, "image": 1, "other": 1},
            "by_extension": {".bin": 1, ".png": 1, ".wav": 1},
            "by_probe_status": {"error": 1, "ok": 2},
        },
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ManifestError("output_write") from exc


def _read_private_manifest(value: str | Path, repo_root: Path) -> tuple[Path, Any]:
    path = ensure_private_output_path(value, repo_root)
    return path, _read_json(path)


def _issue_count(report: ValidationReport, code: str) -> int:
    return sum(issue == code for issue in report.issues)


def _print_validation(report: ValidationReport) -> None:
    print(f"status={'PASS' if report.ok else 'FAIL'}")
    print(f"items={report.item_count}")
    print(f"schema_errors={_issue_count(report, 'schema')}")
    print(f"hash_format_errors={_issue_count(report, 'hash_format')}")
    print(f"duplicate_sha256_groups={report.duplicate_sha256_groups}")
    print(f"missing_files={report.missing_files if report.missing_files is not None else 'not_checked'}")
    print(f"hash_mismatches={report.hash_mismatches}")
    print(f"path_escape_errors={_issue_count(report, 'path_escape')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan read-only roots into a private manifest")
    scan.add_argument("--root", action="append", required=True, metavar="ALIAS=PATH")
    scan.add_argument("--output", required=True, metavar="PATH")

    validate = subparsers.add_parser("validate", help="validate a private manifest")
    validate.add_argument("--manifest", required=True, metavar="PATH")
    validate.add_argument("--root", action="append", default=[], metavar="ALIAS=PATH")

    summary = subparsers.add_parser("summary", help="write a sanitized count-only summary")
    summary.add_argument("--manifest", required=True, metavar="PATH")
    summary.add_argument("--output", required=True, metavar="PATH")

    schema = subparsers.add_parser("schema", help="write the committed schema and sanitized example")
    schema.add_argument("--schema-output", required=True, metavar="PATH")
    schema.add_argument("--example-output", required=True, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo_root = _repo_root(args.repo_root)
        if args.command == "scan":
            output = ensure_private_output_path(args.output, repo_root)
            roots = parse_root_specs(args.root)
            manifest = scan_roots(roots)
            _write_json(output, manifest)
            print("status=PASS")
            print(f"items={len(manifest['items'])}")
            print(f"roots={len(manifest['roots'])}")
            print("private_output_written=1")
            return 0

        if args.command == "validate":
            manifest_path, manifest = _read_private_manifest(args.manifest, repo_root)
            del manifest_path
            roots = parse_root_specs(args.root) if args.root else None
            report = validate_manifest_document(manifest, roots)
            _print_validation(report)
            return 0 if report.ok else 1

        if args.command == "summary":
            _, manifest = _read_private_manifest(args.manifest, repo_root)
            report = validate_manifest_document(manifest)
            if not report.ok:
                raise ManifestError("manifest_invalid")
            output = ensure_repo_output_path(args.output, repo_root)
            _write_json(output, build_sanitized_summary(manifest))
            print("status=PASS")
            print("sanitized_output_written=1")
            return 0

        if args.command == "schema":
            schema_output = ensure_repo_output_path(args.schema_output, repo_root)
            example_output = ensure_repo_output_path(args.example_output, repo_root)
            if schema_output == example_output:
                raise ManifestError("output_collision")
            _write_json(schema_output, schema_document())
            _write_json(example_output, example_document())
            print("status=PASS")
            print("schema_output_written=1")
            print("example_output_written=1")
            return 0
    except ManifestError as exc:
        print(f"status=ERROR:{exc.code}")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
