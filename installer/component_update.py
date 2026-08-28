"""Apply one verified local component package to a managed installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from installer.uninstall_safety import safe_owned_targets


PACKAGE_SCHEMA = "olivia.component-package.v1"
STATE_SCHEMA = "olivia.update-state.v1"
STATE_NAME = ".olivia-update-state.json"
STAGING_ROOT = Path("runtime/update-staging")
_INSTALL_MARKER = ".olivia-full-patch.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_COMPONENT_TARGETS = {"local_backend": "local_backend"}
_COMPONENT_REQUIRED_FILES = {
    "local_backend": frozenset(
        {
            "installer/start_local.py",
            "installer/configure.py",
            "installer/uninstall.py",
        }
    )
}
_LEGACY_VERSION = "0.0.0+legacy"
_LEGACY_MANIFEST_SHA256 = "0" * 64
_LEGACY_PAYLOAD_PATH = "local_backend"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_WINDOWS_RESERVED_CHARS = frozenset('<>:"|?*')
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")}
    | {f"LPT{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")}
)


class ComponentUpdateError(RuntimeError):
    """Stable, user-safe component update failure code."""


def _load_object(raw: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ComponentUpdateError(code) from exc
    if not isinstance(value, dict):
        raise ComponentUpdateError(code)
    return value


def _validate_installation(installation: Path) -> Path:
    root = installation.expanduser().resolve()
    try:
        safe_owned_targets(root)
    except (OSError, ValueError) as exc:
        raise ComponentUpdateError("UPDATE_INSTALLATION_INVALID") from exc
    marker_path = root / _INSTALL_MARKER
    try:
        marker = _load_object(marker_path.read_bytes(), "UPDATE_INSTALLATION_INVALID")
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_INSTALLATION_INVALID") from exc
    if (
        marker.get("schema_version") != "olivia.full-patch.install.v2"
        or marker.get("owned_root") != str(root)
    ):
        raise ComponentUpdateError("UPDATE_INSTALLATION_INVALID")
    return root


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            part.endswith((" ", "."))
            or any(character in _WINDOWS_RESERVED_CHARS for character in part)
            or any(ord(character) < 32 for character in part)
            or stem in _WINDOWS_DEVICE_NAMES
        ):
            raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")
    return path.as_posix()


def _validate_manifest(value: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if set(value) != {"schema_version", "component", "version", "files"}:
        raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")
    component = value.get("component")
    version = value.get("version")
    files = value.get("files")
    if (
        value.get("schema_version") != PACKAGE_SCHEMA
        or component not in _COMPONENT_TARGETS
        or not isinstance(version, str)
        or not _VERSION_RE.fullmatch(version)
        or not isinstance(files, list)
        or not files
    ):
        raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
            raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")
        relative = _validate_relative_path(item.get("path"))
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            relative.casefold() in seen
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")
        seen.add(relative.casefold())
        normalized.append({"path": relative, "size_bytes": size, "sha256": digest})
    if not _COMPONENT_REQUIRED_FILES[component].issubset(
        {item["path"] for item in normalized}
    ):
        raise ComponentUpdateError("UPDATE_MANIFEST_INVALID")
    return component, version, normalized


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH") from exc
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH") from exc
    return digest.hexdigest()


def _verify_staged_tree(root: Path, files: list[dict[str, Any]]) -> None:
    expected = {item["path"]: item for item in files}
    actual: dict[str, Path] = {}
    try:
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in directories:
                if _is_reparse_point(current_path / name):
                    raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH")
            for name in filenames:
                candidate = current_path / name
                if _is_reparse_point(candidate) or not candidate.is_file():
                    raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH")
                relative = candidate.relative_to(root).as_posix()
                if relative in actual:
                    raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH")
                actual[relative] = candidate
    except ComponentUpdateError:
        raise
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH") from exc
    if set(actual) != set(expected):
        raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH")
    for relative, candidate in actual.items():
        item = expected[relative]
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH") from exc
        if size != item["size_bytes"] or _file_sha256(candidate) != item["sha256"]:
            raise ComponentUpdateError("UPDATE_STAGED_TREE_MISMATCH")


def _stage_package(
    package: Path,
    staging: Path,
    expected_manifest_sha256: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    if not _SHA256_RE.fullmatch(expected_manifest_sha256):
        raise ComponentUpdateError("UPDATE_MANIFEST_DIGEST_INVALID")
    try:
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or names.count("manifest.json") != 1:
                raise ComponentUpdateError("UPDATE_PACKAGE_INVALID")
            for info in archive.infolist():
                member_kind = stat.S_IFMT(info.external_attr >> 16)
                if (
                    info.is_dir()
                    or info.flag_bits & 0x1
                    or member_kind not in {0, stat.S_IFREG}
                ):
                    raise ComponentUpdateError("UPDATE_PACKAGE_INVALID")
            manifest_bytes = archive.read("manifest.json")
            actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            if actual_manifest_sha256 != expected_manifest_sha256:
                raise ComponentUpdateError("UPDATE_MANIFEST_DIGEST_MISMATCH")
            component, version, files = _validate_manifest(
                _load_object(manifest_bytes, "UPDATE_MANIFEST_INVALID")
            )
            expected_names = {"manifest.json"} | {
                f"payload/{item['path']}" for item in files
            }
            if set(names) != expected_names:
                raise ComponentUpdateError("UPDATE_PACKAGE_INVALID")
            component_root = staging / "payload"
            for item in files:
                content = archive.read(f"payload/{item['path']}")
                if (
                    len(content) != item["size_bytes"]
                    or hashlib.sha256(content).hexdigest() != item["sha256"]
                ):
                    raise ComponentUpdateError("UPDATE_PAYLOAD_DIGEST_MISMATCH")
                destination = component_root.joinpath(*PurePosixPath(item["path"]).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            _verify_staged_tree(component_root, files)
    except ComponentUpdateError:
        raise
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ComponentUpdateError("UPDATE_PACKAGE_INVALID") from exc
    return component, version, files


def _validate_state_descriptor(component: str, value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "manifest_sha256",
        "payload_path",
    }:
        raise ComponentUpdateError("UPDATE_STATE_INVALID")
    version = value.get("version")
    manifest_sha256 = value.get("manifest_sha256")
    payload_path = value.get("payload_path")
    legacy_descriptor = {
        "version": _LEGACY_VERSION,
        "manifest_sha256": _LEGACY_MANIFEST_SHA256,
        "payload_path": _LEGACY_PAYLOAD_PATH,
    }
    if value == legacy_descriptor:
        return legacy_descriptor
    if (
        component not in _COMPONENT_TARGETS
        or not isinstance(version, str)
        or not _VERSION_RE.fullmatch(version)
        or not isinstance(manifest_sha256, str)
        or not _SHA256_RE.fullmatch(manifest_sha256)
        or payload_path != f"versions/{component}/{version}-{manifest_sha256}"
    ):
        raise ComponentUpdateError("UPDATE_STATE_INVALID")
    return {
        "version": version,
        "manifest_sha256": manifest_sha256,
        "payload_path": payload_path,
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": STATE_SCHEMA,
            "active_components": {},
            "previous_components": {},
        }
    try:
        state = _load_object(path.read_bytes(), "UPDATE_STATE_INVALID")
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_STATE_INVALID") from exc
    if (
        set(state) != {"schema_version", "active_components", "previous_components"}
        or state.get("schema_version") != STATE_SCHEMA
        or not isinstance(state.get("active_components"), dict)
        or not isinstance(state.get("previous_components"), dict)
        or set(state["active_components"]) != {"local_backend"}
    ):
        raise ComponentUpdateError("UPDATE_STATE_INVALID")
    for section_name in ("active_components", "previous_components"):
        section = state[section_name]
        if any(component not in _COMPONENT_TARGETS for component in section):
            raise ComponentUpdateError("UPDATE_STATE_INVALID")
        state[section_name] = {
            component: _validate_state_descriptor(component, descriptor)
            for component, descriptor in section.items()
        }
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    try:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_ACTIVATION_FAILED") from exc


def _next_state(
    current: dict[str, Any],
    *,
    component: str,
    version: str,
    manifest_sha256: str,
    payload_path: str,
) -> dict[str, Any]:
    active = dict(current["active_components"])
    new_descriptor = {
        "version": version,
        "manifest_sha256": manifest_sha256,
        "payload_path": payload_path,
    }
    if active.get(component) == new_descriptor:
        return {
            "schema_version": STATE_SCHEMA,
            "active_components": active,
            "previous_components": dict(current["previous_components"]),
        }
    previous = dict(current["previous_components"])
    previous[component] = active.get(
        component,
        {
            "version": _LEGACY_VERSION,
            "manifest_sha256": _LEGACY_MANIFEST_SHA256,
            "payload_path": _LEGACY_PAYLOAD_PATH,
        },
    )
    active[component] = new_descriptor
    return {
        "schema_version": STATE_SCHEMA,
        "active_components": active,
        "previous_components": previous,
    }


def _resolve_state_payload(
    root: Path,
    component: str,
    descriptor: object,
) -> Path:
    validated = _validate_state_descriptor(component, descriptor)
    current = root
    try:
        for part in PurePosixPath(validated["payload_path"]).parts:
            current = current / part
            metadata = current.lstat()
            if current.is_symlink() or bool(
                getattr(metadata, "st_file_attributes", 0)
                & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ComponentUpdateError("UPDATE_STATE_INVALID")
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_STATE_INVALID") from exc
    if not current.is_dir():
        raise ComponentUpdateError("UPDATE_STATE_INVALID")
    return current


def _refresh_existing_shortcuts(root: Path, active_version: Path) -> None:
    """Best-effort icon refresh for shortcuts that the user kept."""

    if os.name != "nt":
        return
    try:
        script = active_version / "installer" / "Create-Shortcut.ps1"
        powershell = (
            Path(os.environ.get("WINDIR", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not script.is_file() or not powershell.is_file():
            return
        subprocess.run(
            [
                os.fspath(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                os.fspath(script),
                "-InstallRoot",
                os.fspath(root),
                "-RefreshExisting",
            ],
            check=False,
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        # A shortcut is optional UI state; activation itself must remain valid.
        return


def apply_component_update(
    installation: str | os.PathLike[str],
    package: str | os.PathLike[str],
    *,
    expected_manifest_sha256: str,
) -> dict[str, str]:
    """Verify, stage and atomically activate one managed component update."""

    root = _validate_installation(Path(installation))
    package_path = Path(package).expanduser().resolve()
    staging = root / STAGING_ROOT / uuid.uuid4().hex
    state_path = root / STATE_NAME
    try:
        staging.mkdir(parents=True)
        component, version, files = _stage_package(
            package_path,
            staging,
            expected_manifest_sha256.lower(),
        )
        current_state = _read_state(state_path)
        legacy = root / _COMPONENT_TARGETS[component]
        if not legacy.is_dir():
            raise ComponentUpdateError("UPDATE_COMPONENT_UNAVAILABLE")
        relative_version = PurePosixPath(
            "versions",
            component,
            f"{version}-{expected_manifest_sha256.lower()}",
        )
        version_root = root.joinpath(*relative_version.parts)
        version_root.parent.mkdir(parents=True, exist_ok=True)
        if version_root.exists():
            try:
                if _is_reparse_point(version_root) or not version_root.is_dir():
                    raise ComponentUpdateError("UPDATE_VERSION_CONFLICT")
                _verify_staged_tree(version_root, files)
            except ComponentUpdateError as exc:
                raise ComponentUpdateError("UPDATE_VERSION_CONFLICT") from exc
        else:
            os.replace(staging / "payload", version_root)
        staged_state = staging / "next-state.json"
        next_state = _next_state(
            current_state,
            component=component,
            version=version,
            manifest_sha256=expected_manifest_sha256.lower(),
            payload_path=relative_version.as_posix(),
        )
        _write_state(staged_state, next_state)
        os.replace(staged_state, state_path)
        try:
            _refresh_existing_shortcuts(root, version_root)
        except Exception:
            # Never turn a completed atomic activation into an update failure.
            pass
        return {"status": "APPLIED", "component": component, "version": version}
    except ComponentUpdateError:
        raise
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_ACTIVATION_FAILED") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def rollback_component_update(
    installation: str | os.PathLike[str],
    component: str = "local_backend",
) -> dict[str, str]:
    """Atomically swap the active and previous pointers for one component."""

    root = _validate_installation(Path(installation))
    if component not in _COMPONENT_TARGETS:
        raise ComponentUpdateError("UPDATE_COMPONENT_UNSUPPORTED")
    state_path = root / STATE_NAME
    state = _read_state(state_path)
    active = state["active_components"].get(component)
    previous = state["previous_components"].get(component)
    if active is None or previous is None:
        raise ComponentUpdateError("UPDATE_ROLLBACK_UNAVAILABLE")
    _resolve_state_payload(root, component, previous)

    next_state = {
        "schema_version": STATE_SCHEMA,
        "active_components": {**state["active_components"], component: previous},
        "previous_components": {component: active},
    }
    staging = root / STAGING_ROOT / uuid.uuid4().hex
    try:
        staging.mkdir(parents=True)
        staged_state = staging / "rollback-state.json"
        _write_state(staged_state, next_state)
        os.replace(staged_state, state_path)
    except ComponentUpdateError:
        raise
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_ACTIVATION_FAILED") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "status": "ROLLED_BACK",
        "component": component,
        "version": previous["version"],
    }


__all__ = [
    "ComponentUpdateError",
    "apply_component_update",
    "rollback_component_update",
]
