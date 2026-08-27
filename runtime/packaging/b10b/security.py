"""Path and output-safety primitives for B10B-owned state."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from .errors import B10BError


_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|credential|password|secret|token)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_RESERVED = {
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


def _is_reparse_point(path: Path) -> bool:
    """Return true for symlinks and Windows reparse points."""

    if path.is_symlink() or os.path.islink(path):
        return True
    try:
        attributes = path.stat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _root_has_reparse_point(root: Path) -> bool:
    current = root
    while True:
        if _is_reparse_point(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def ensure_safe_root(root: Path) -> None:
    """Reject a data root or any existing parent that is a reparse point."""

    if _root_has_reparse_point(root):
        raise B10BError("PATH_ESCAPE", "The B10B data root may not be a symlink or reparse point.")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _ASSIGNMENT.sub(r"\1\2<redacted>", _BEARER.sub("Bearer <redacted>", value))
    return value


def validate_relative_path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise B10BError("INVALID_MANIFEST", f"{field} must be a non-empty relative path.")
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    if (
        value != value.strip()
        or normalized.startswith("/")
        or normalized.endswith("/")
        or "//" in normalized
        or windows.is_absolute()
        or windows.drive
    ):
        raise B10BError("PATH_ESCAPE", f"{field} must stay relative to the B10B data root.")
    parts = normalized.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts) or ":" in normalized:
        raise B10BError("PATH_ESCAPE", f"{field} contains a path traversal component.")
    if any(part.rstrip(" .").split(".", 1)[0].upper() in _RESERVED for part in parts):
        raise B10BError("PATH_ESCAPE", f"{field} uses a Windows reserved device name.")
    return "/".join(parts)


def safe_owned_path(data_root: Path, relative: str, *, field: str) -> Path:
    normalized = validate_relative_path(relative, field=field)
    ensure_safe_root(data_root)
    root = data_root.resolve(strict=False)
    candidate = (data_root / Path(*normalized.split("/"))).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise B10BError("PATH_ESCAPE", f"{field} resolves outside the B10B data root.") from exc
    current = data_root
    for part in normalized.split("/"):
        current = current / part
        if _is_reparse_point(current):
            raise B10BError("PATH_ESCAPE", f"{field} crosses a symlink or reparse point.")
    return candidate


def ensure_regular_owned_file(path: Path, *, field: str) -> None:
    if _is_reparse_point(path) or (path.exists() and path.is_dir()):
        raise B10BError("OWNERSHIP_CONFLICT", f"{field} is not a regular B10B-owned file.")


def is_sensitive_key(value: str) -> bool:
    """Return whether a customization key would represent secret material."""

    return bool(_SECRET_KEY.search(str(value)))


def _is_reserved_device_segment(part: str) -> bool:
    normalized = part.split(".", 1)[0].rstrip(" .")
    return normalized.upper() in _RESERVED


def is_external_reference(value: str, *, policy: str = "absolute") -> bool:
    """Validate a reference without resolving or copying the referenced asset.

    Caller-provided assets may reside on any local Windows volume.  This
    helper deliberately validates syntax only; callers retain ownership and
    must apply their existing copy/hash/delete rules before touching a path.
    """

    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    candidate = PureWindowsPath(value)
    if policy == "logical_asset":
        return bool(re.fullmatch(r"asset_[0-9a-f]{32}", value))
    # A local absolute path has one drive letter and a non-root component.
    # This excludes relative and drive-relative paths, UNC/device paths and
    # URL-like strings without depending on the host OS path flavour.
    if (
        not candidate.is_absolute()
        or not re.fullmatch(r"[A-Za-z]:", candidate.drive)
        or len(candidate.parts) <= 1
        or any(part in {".", ".."} for part in candidate.parts)
        or any(part.endswith((" ", ".")) for part in candidate.parts[1:])
        or any(_is_reserved_device_segment(part) for part in candidate.parts)
    ):
        return False
    return True
