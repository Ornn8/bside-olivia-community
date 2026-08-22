"""Runtime-only B01 manifest asset resolution.

This adapter validates the manifest at construction and rechecks file
containment and the recorded hash on every resolution.  It never emits source
paths or asset references as part of public media state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools import asset_manifest

from .contracts import ASSET_REF_RE, AssetKind, MediaStateError


class AssetResolutionError(MediaStateError):
    """A manifest or local-file error without sensitive details."""


@dataclass(frozen=True)
class ResolvedAsset:
    """Private adapter value passed only to an injected playback provider."""

    kind: AssetKind
    path: Path
    sha256: str
    reference: str
    duration_seconds: float | None = None


_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if value.startswith("/") or "\\" in value:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _duration(item: Mapping[str, Any], kind: AssetKind) -> float | None:
    metadata = item.get("media_metadata")
    if not isinstance(metadata, Mapping):
        return None
    selected = metadata.get(kind.value)
    if not isinstance(selected, Mapping):
        return None
    value = selected.get("duration_seconds")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


class ManifestAssetResolver:
    """Resolve only the exact local file registered by a B01 manifest."""

    def __init__(self, manifest: Mapping[str, Any], roots: Mapping[str, str | Path]) -> None:
        if not isinstance(manifest, Mapping):
            raise AssetResolutionError("MANIFEST_INVALID")
        report = asset_manifest.validate_manifest_document(manifest)
        if not report.ok:
            raise AssetResolutionError("MANIFEST_INVALID")
        if not isinstance(roots, Mapping):
            raise AssetResolutionError("MANIFEST_ROOT_UNAVAILABLE")

        normalized_roots: dict[str, Path] = {}
        for alias, value in roots.items():
            if not isinstance(alias, str) or _ALIAS_RE.fullmatch(alias) is None:
                raise AssetResolutionError("MANIFEST_ROOT_UNAVAILABLE")
            try:
                normalized_roots[alias] = Path(value).expanduser().resolve(strict=False)
            except (TypeError, OSError, RuntimeError) as exc:
                raise AssetResolutionError("MANIFEST_ROOT_UNAVAILABLE") from exc

        items: dict[str, Mapping[str, Any]] = {}
        for item in manifest.get("items", []):
            if not isinstance(item, Mapping):
                raise AssetResolutionError("MANIFEST_INVALID")
            reference = item.get("logical_id")
            if not isinstance(reference, str) or ASSET_REF_RE.fullmatch(reference) is None:
                raise AssetResolutionError("MANIFEST_INVALID")
            if reference in items:
                raise AssetResolutionError("MANIFEST_INVALID")
            items[reference] = dict(item)
        self._roots = normalized_roots
        self._items = items

    @classmethod
    def from_file(
        cls,
        manifest_path: str | Path,
        roots: Mapping[str, str | Path],
    ) -> "ManifestAssetResolver":
        try:
            with Path(manifest_path).open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AssetResolutionError("MANIFEST_UNAVAILABLE") from exc
        return cls(manifest, roots)

    def resolve(self, asset_ref: str, kind: AssetKind) -> ResolvedAsset:
        if not isinstance(asset_ref, str) or ASSET_REF_RE.fullmatch(asset_ref) is None:
            raise AssetResolutionError("INVALID_ASSET_REFERENCE")
        try:
            kind = AssetKind(kind)
        except ValueError as exc:
            raise AssetResolutionError("INVALID_ASSET_KIND") from exc
        item = self._items.get(asset_ref)
        if item is None:
            raise AssetResolutionError("ASSET_NOT_FOUND")
        if item.get("category") != kind.value:
            raise AssetResolutionError("ASSET_CATEGORY_MISMATCH")
        if item.get("probe_status") == "error":
            raise AssetResolutionError("ASSET_INVALID_MEDIA")

        alias = item.get("root_alias")
        relative = item.get("relative_path")
        root = self._roots.get(alias) if isinstance(alias, str) else None
        if root is None or not _safe_relative_path(relative):
            raise AssetResolutionError("ASSET_PATH_ESCAPE")
        candidate = root.joinpath(*relative.split("/"))
        try:
            resolved_root = root.resolve(strict=False)
            resolved_candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise AssetResolutionError("ASSET_PATH_ESCAPE") from exc
        if not _within(resolved_candidate, resolved_root):
            raise AssetResolutionError("ASSET_PATH_ESCAPE")
        if not resolved_candidate.is_file():
            raise AssetResolutionError("ASSET_MISSING")

        try:
            digest = hashlib.sha256(resolved_candidate.read_bytes()).hexdigest()
            byte_count = resolved_candidate.stat().st_size
        except OSError as exc:
            raise AssetResolutionError("ASSET_MISSING") from exc
        if byte_count != item.get("bytes") or digest != item.get("sha256"):
            raise AssetResolutionError("ASSET_HASH_MISMATCH")
        return ResolvedAsset(
            kind=kind,
            path=resolved_candidate,
            sha256=digest,
            reference=asset_ref,
            duration_seconds=_duration(item, kind),
        )
