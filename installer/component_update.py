"""Apply one verified local component package to a managed installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
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
    return component, version, normalized


def _stage_package(
    package: Path,
    staging: Path,
    expected_manifest_sha256: str,
) -> tuple[str, str]:
    if not _SHA256_RE.fullmatch(expected_manifest_sha256):
        raise ComponentUpdateError("UPDATE_MANIFEST_DIGEST_INVALID")
    try:
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or names.count("manifest.json") != 1:
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
    except ComponentUpdateError:
        raise
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ComponentUpdateError("UPDATE_PACKAGE_INVALID") from exc
    return component, version


def _write_state(
    path: Path,
    *,
    component: str,
    version: str,
    manifest_sha256: str,
) -> None:
    state = {
        "schema_version": STATE_SCHEMA,
        "active_components": {
            component: {
                "version": version,
                "manifest_sha256": manifest_sha256,
            }
        },
    }
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


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
    active: Path | None = None
    backup: Path | None = None
    state_path = root / STATE_NAME
    state_backup = staging / "previous-state.json"
    active_backed_up = False
    state_published = False
    try:
        staging.mkdir(parents=True)
        component, version = _stage_package(
            package_path,
            staging,
            expected_manifest_sha256.lower(),
        )
        active = root / _COMPONENT_TARGETS[component]
        if not active.is_dir():
            raise ComponentUpdateError("UPDATE_COMPONENT_UNAVAILABLE")
        backup = staging / "previous-component"
        staged_state = staging / "next-state.json"
        _write_state(
            staged_state,
            component=component,
            version=version,
            manifest_sha256=expected_manifest_sha256.lower(),
        )
        if state_path.exists():
            shutil.copyfile(state_path, state_backup)
        os.replace(active, backup)
        active_backed_up = True
        os.replace(staging / "payload", active)
        os.replace(staged_state, state_path)
        state_published = True
        return {"status": "APPLIED", "component": component, "version": version}
    except ComponentUpdateError:
        raise
    except OSError as exc:
        raise ComponentUpdateError("UPDATE_ACTIVATION_FAILED") from exc
    finally:
        if active_backed_up and not state_published and active is not None and backup is not None:
            if active.is_dir():
                shutil.rmtree(active, ignore_errors=True)
            if backup.exists():
                os.replace(backup, active)
            if state_backup.exists():
                os.replace(state_backup, state_path)
            else:
                state_path.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


__all__ = ["ComponentUpdateError", "apply_component_update"]
