"""Stable root launcher for an atomically selected backend version."""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import sys
from pathlib import Path, PurePosixPath

STATE_NAME = ".olivia-update-state.json"
STATE_SCHEMA = "olivia.update-state.v1"
INSTALL_SCHEMA = "olivia.full-patch.install.v2"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_LEGACY_VERSION = "0.0.0+legacy"
_LEGACY_MANIFEST_SHA256 = "0" * 64
_LEGACY_PAYLOAD_PATH = "local_backend"


class VersionLauncherError(RuntimeError):
    """Stable, user-safe launcher failure code."""


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VersionLauncherError("UPDATE_STATE_INVALID") from exc
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validated_relative_path(
    value: object,
    version: object,
    digest: object,
) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not isinstance(version, str)
        or not isinstance(digest, str)
        or not _VERSION_RE.fullmatch(version)
        or not _SHA256_RE.fullmatch(digest)
    ):
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    relative = PurePosixPath(value)
    if (
        version == _LEGACY_VERSION
        and digest == _LEGACY_MANIFEST_SHA256
        and value == _LEGACY_PAYLOAD_PATH
    ):
        expected = PurePosixPath(_LEGACY_PAYLOAD_PATH)
    else:
        expected = PurePosixPath(
            "versions",
            "local_backend",
            f"{version}-{digest}",
        )
    if relative != expected or relative.as_posix() != value:
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    return relative


def _safe_version_path(root: Path, value: object, version: object, digest: object) -> Path:
    relative = _validated_relative_path(value, version, digest)
    current = root
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            raise VersionLauncherError("UPDATE_STATE_INVALID")
    if not current.is_dir():
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    return current


def resolve_active_backend(installation: str | os.PathLike[str]) -> Path:
    """Resolve one complete backend tree from one atomic state-file read."""

    root = Path(installation).expanduser().absolute()
    try:
        if _is_reparse_point(root) or not root.is_dir():
            raise VersionLauncherError("UPDATE_INSTALLATION_INVALID")
        root = root.resolve()
        marker_path = root / ".olivia-full-patch.json"
        if _is_reparse_point(marker_path):
            raise VersionLauncherError("UPDATE_INSTALLATION_INVALID")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            not isinstance(marker, dict)
            or marker.get("schema_version") != INSTALL_SCHEMA
            or marker.get("owned_root") != str(root)
        ):
            raise VersionLauncherError("UPDATE_INSTALLATION_INVALID")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionLauncherError("UPDATE_INSTALLATION_INVALID") from exc
    state_path = root / STATE_NAME
    if not state_path.exists():
        legacy = root / "local_backend"
        if not legacy.is_dir() or _is_reparse_point(legacy):
            raise VersionLauncherError("UPDATE_COMPONENT_UNAVAILABLE")
        return legacy
    if _is_reparse_point(state_path):
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionLauncherError("UPDATE_STATE_INVALID") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != STATE_SCHEMA
        or set(state) != {"schema_version", "active_components", "previous_components"}
        or not isinstance(state.get("active_components"), dict)
        or set(state["active_components"]) != {"local_backend"}
        or not isinstance(state.get("previous_components"), dict)
        or not set(state["previous_components"]).issubset({"local_backend"})
    ):
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    active = state["active_components"]["local_backend"]
    if not isinstance(active, dict) or set(active) != {
        "version",
        "manifest_sha256",
        "payload_path",
    }:
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    active_path = _safe_version_path(
        root,
        active.get("payload_path"),
        active.get("version"),
        active.get("manifest_sha256"),
    )
    previous = state["previous_components"].get("local_backend")
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {
            "version",
            "manifest_sha256",
            "payload_path",
        }:
            raise VersionLauncherError("UPDATE_STATE_INVALID")
        _validated_relative_path(
            previous.get("payload_path"),
            previous.get("version"),
            previous.get("manifest_sha256"),
        )
    return active_path


def _entrypoint_arguments(
    action: str,
    installation: Path,
) -> tuple[Path, list[str]]:
    backend = resolve_active_backend(installation)
    root = installation.expanduser().resolve()
    entrypoints = {
        "start": ("start_local.py", "--install-root"),
        "configure": ("configure.py", "--installation"),
        "uninstall": ("uninstall.py", "--installation"),
    }
    script_name, root_option = entrypoints[action]
    entrypoint = backend / "installer" / script_name
    if (
        not entrypoint.is_file()
        or _is_reparse_point(entrypoint.parent)
        or _is_reparse_point(entrypoint)
    ):
        raise VersionLauncherError("UPDATE_COMPONENT_UNAVAILABLE")
    return entrypoint, [root_option, os.fspath(root)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="olivia-version-launcher")
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("action", choices=("start", "configure", "uninstall"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        entrypoint, root_arguments = _entrypoint_arguments(
            args.action,
            args.install_root,
        )
        previous_argv = sys.argv
        previous_path = list(sys.path)
        try:
            sys.argv = [os.fspath(entrypoint), *root_arguments, *args.arguments]
            sys.path.insert(0, os.fspath(entrypoint.parents[1]))
            runpy.run_path(os.fspath(entrypoint), run_name="__main__")
        finally:
            sys.argv = previous_argv
            sys.path[:] = previous_path
        return 0
    except VersionLauncherError as exc:
        print(json.dumps({"status": "ERROR", "code": str(exc)}))
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)


__all__ = ["VersionLauncherError", "main", "resolve_active_backend"]


if __name__ == "__main__":
    raise SystemExit(main())
