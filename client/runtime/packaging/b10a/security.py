"""Path and output-safety primitives used by the B10A manager."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from .errors import B10AError


_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|credential|password|secret|token)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_WINDOWS_RESERVED_NAMES = {
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


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    # These are safe metadata (the value or credential is never emitted).
    if normalized.endswith(("_env", "_present", "_source")) or normalized in {"redact_secrets"}:
        return False
    return bool(_SECRET_KEY.search(key))


def redact(value: Any) -> Any:
    """Return a JSON-safe value with secret-looking fields and strings masked."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                redacted[str(key)] = "<redacted>"
            else:
                redacted[str(key)] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _ASSIGNMENT.sub(r"\1\2<redacted>", _BEARER.sub("Bearer <redacted>", value))
    return value


def redacted_json(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True)


def validate_relative_path(value: str, *, field: str) -> str:
    """Validate a manifest-owned path before it is joined to a data root."""

    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise B10AError("INVALID_MANIFEST", f"{field} must be a non-empty relative path.")
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    if normalized.startswith("/") or windows.is_absolute() or windows.drive:
        raise B10AError(
            "PATH_ESCAPE",
            f"{field} must stay relative to the B10A data root.",
            {"field": field},
        )
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts) or ":" in normalized:
        raise B10AError(
            "PATH_ESCAPE",
            f"{field} contains a path traversal component.",
            {"field": field},
        )
    if any(part.rstrip(" .").split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES for part in parts):
        raise B10AError(
            "PATH_ESCAPE",
            f"{field} uses a Windows reserved device name.",
            {"field": field},
        )
    return "/".join(parts)


def safe_owned_path(data_root: Path, relative: str, *, field: str = "owned_path") -> Path:
    """Resolve a manager-owned path and reject symlink/reparse escapes."""

    normalized = validate_relative_path(relative, field=field)
    if os.path.islink(data_root):
        raise B10AError(
            "PATH_ESCAPE",
            "The B10A data root may not be a symlink or reparse point.",
            {"field": field},
        )
    root = data_root.resolve(strict=False)
    candidate = (data_root / Path(*normalized.split("/"))).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise B10AError(
            "PATH_ESCAPE",
            f"{field} resolves outside the B10A data root.",
            {"field": field, "path": normalized},
        ) from exc

    # A symlink/reparse point is never an owned deletion target. Checking the
    # lexical path and resolved path covers both the final item and parents.
    current = data_root
    for part in normalized.split("/"):
        current = current / part
        if os.path.islink(current):
            raise B10AError(
                "PATH_ESCAPE",
                f"{field} crosses a symlink or reparse point.",
                {"field": field, "path": normalized},
            )
    return candidate


def ensure_regular_owned_file(path: Path, *, field: str) -> None:
    if not path.exists():
        return
    if path.is_dir() or path.is_symlink() or os.path.islink(path):
        raise B10AError(
            "OWNERSHIP_CONFLICT",
            f"{field} is not a regular manager-owned file.",
            {"path": str(path)},
        )
