"""Shared, fail-closed uninstall target validation for managed installs."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path, PurePath


MARKER_NAME = ".olivia-full-patch.json"
OWNED_PATHS = (
    "app",
    "local_backend",
    "launcher",
    "versions",
    "START.cmd",
    "START.vbs",
    "CONFIGURE.cmd",
    "UNINSTALL.cmd",
    "runtime/mem0-site-packages",
    "runtime/mem0-site-packages.staging",
    "runtime/update-staging",
    ".olivia-update-state.json",
    MARKER_NAME,
)
PRESERVED_PATHS = ("data", "logs", "third-party", "downloads", "profile")
EMPTY_OWNED_PARENT_PATHS = ("runtime",)

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


def safe_managed_target(root: Path, name: str) -> Path:
    """Validate one caller-owned relative target using the shared boundary."""

    root = root.absolute()
    if _is_reparse_point(root) or not root.is_dir():
        raise ValueError("managed uninstall root is unavailable")
    return _safe_target(root.resolve(), name)


def _remove_managed_python_path_registration(root: Path) -> None:
    """Remove only this installation's absolute Mem0 runtime registration."""

    registered_runtime = (root / "runtime" / "mem0-site-packages").resolve()
    runtime_root = Path(sys.executable).resolve().parent
    for pth_path in runtime_root.glob("*._pth"):
        try:
            lines = pth_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        kept: list[str] = []
        changed = False
        for line in lines:
            try:
                candidate = Path(line.strip())
                matches = candidate.is_absolute() and candidate.resolve() == registered_runtime
            except OSError:
                matches = False
            if matches:
                changed = True
            else:
                kept.append(line)
        if changed:
            temporary = pth_path.with_name(f".{pth_path.name}.uninstall.tmp")
            try:
                temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
                os.replace(temporary, pth_path)
            finally:
                temporary.unlink(missing_ok=True)


def remove_owned_targets(
    root: Path, *, deferred_paths: tuple[str, ...] = ()
) -> None:
    """Remove only validated compiled targets; marker contents are untrusted."""

    deferred = set(deferred_paths)
    if not deferred.issubset(OWNED_PATHS):
        raise ValueError("PATCH_MARKER_INVALID")
    targets = safe_owned_targets(root)
    _remove_managed_python_path_registration(root.resolve())
    for target in targets:
        relative = target.relative_to(root.resolve()).as_posix()
        if relative in deferred:
            continue
        # Re-check each target immediately before deletion to avoid following a
        # path component that was replaced after the initial validation.
        _safe_target(root.resolve(), relative)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
    for relative in EMPTY_OWNED_PARENT_PATHS:
        target = safe_managed_target(root, relative)
        try:
            target.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Unknown or non-empty parents are outside the ownership list.
            pass


__all__ = [
    "EMPTY_OWNED_PARENT_PATHS",
    "MARKER_NAME",
    "OWNED_PATHS",
    "PRESERVED_PATHS",
    "remove_owned_targets",
    "safe_managed_target",
    "safe_owned_targets",
]
