"""Shared, fail-closed uninstall target validation for managed installs."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePath


MARKER_NAME = ".olivia-full-patch.json"
OWNED_PATHS = (
    "app",
    "local_backend",
    "START.cmd",
    "CONFIGURE.cmd",
    "UNINSTALL.cmd",
    MARKER_NAME,
)
PRESERVED_PATHS = ("data", "logs", "third-party")

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_reparse_point(path: Path) -> bool:
    """Return whether *path* is a symlink or Windows reparse point."""

    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink():
        return True
    return bool(
        getattr(stat_result, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _safe_target(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("invalid managed uninstall path")
    relative = PurePath(name)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ValueError("managed uninstall path escapes root")
    root_resolved = root.resolve()
    target = root / relative
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise ValueError("managed uninstall path cannot be resolved") from exc
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("managed uninstall path escapes root") from exc

    current = root_resolved
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            raise ValueError("managed uninstall path traverses reparse point")
    return target


def safe_owned_targets(root: Path) -> tuple[Path, ...]:
    """Resolve the compiled ownership list, rejecting unsafe targets first."""

    root = root.absolute()
    if _is_reparse_point(root):
        raise ValueError("managed uninstall root is unavailable")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("managed uninstall root is unavailable")
    return tuple(_safe_target(root, name) for name in OWNED_PATHS)


def remove_owned_targets(root: Path) -> None:
    """Remove only validated compiled targets; marker contents are untrusted."""

    targets = safe_owned_targets(root)
    for target in targets:
        # Re-check each target immediately before deletion to avoid following a
        # path component that was replaced after the initial validation.
        _safe_target(root.resolve(), target.relative_to(root.resolve()).as_posix())
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()


__all__ = [
    "MARKER_NAME",
    "OWNED_PATHS",
    "PRESERVED_PATHS",
    "remove_owned_targets",
    "safe_owned_targets",
]
