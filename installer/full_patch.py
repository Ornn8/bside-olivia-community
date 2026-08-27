"""Install the local HTTP patch into an isolated copy of a Steam install."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from patch_companion_settings import patch_companion_settings
from patch_feapp import patch_feapp
from patch_webplayer import patch_webplayer
from installer.uninstall_safety import (
    MARKER_NAME,
    OWNED_PATHS,
    PRESERVED_PATHS,
    remove_owned_targets,
    safe_owned_targets,
)

APP_ID = "4532590"
MANIFEST_SCHEMA = "olivia.full-patch.v2"
PAYLOAD_DIRS = (
    "asr",
    "control_center",
    "contracts",
    "installer",
    "media_state",
    "tools",
    "tts",
    "linli_character",
)
PAYLOAD_EXTRA_DIRS = ("runtime/visual",)
PAYLOAD_EXTRA_FILES = (
    "runtime/__init__.py",
    "runtime/original_client_media_http.py",
    "runtime/video_reply_settings.py",
)
PAYLOAD_SUFFIXES = {".py", ".json", ".toml", ".ini", ".txt", ".ps1", ".patch"}
PAYLOAD_ROOT_FILES = {
    "local_server.py",
    "original_client_server.py",
    "original_client_settings_ui.py",
    "patch_companion_settings.py",
    "patch_feapp.py",
    "patch_webplayer.py",
    "http_contract.py",
    "llm_gateway.py",
    "memory.py",
    "memory_port.py",
    "memory_prompt.py",
    "local_memory.py",
    "persona_provider.py",
    "reply_orchestrator.py",
}
PAYLOAD_REQUIRED_ROOT_FILES = {
    "letter_status.py",
    "local_server.py",
    "original_client_media_http.py",
    "original_client_server.py",
    "original_client_settings_ui.py",
    "patch_companion_settings.py",
    "patch_feapp.py",
    "patch_webplayer.py",
    "video_reply_settings.py",
}
PAYLOAD_REQUIRED_RELATIVE_FILES = {
    "contracts/letter_status.py",
    "contracts/third_party_manifest.example.json",
    "contracts/third_party_manifest.schema.json",
    "control_center/__init__.py",
    "control_center/app.py",
    "control_center/auth.py",
    "control_center/memory_api.py",
    "control_center/private_world_api.py",
    "control_center/private_world_candidate_api.py",
    "control_center/private_world_candidate_backend.py",
    "control_center/private_world_candidate_ui.py",
    "control_center/runtime.py",
    "runtime/__init__.py",
    "runtime/original_client_media_http.py",
    "runtime/video_reply_settings.py",
}
PAYLOAD_EXCLUDED_ROOT_FILES = {
    "letter_pairs.json",
    "memory_store.json",
    "llm_config.json",
}


class PatchInstallError(RuntimeError):
    """Stable user-facing installation error code."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[0-9a-fA-F]{64}", value)
    )


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PatchInstallError(code) from exc
    if not isinstance(value, dict):
        raise PatchInstallError(code)
    return value


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    value = _load_json(
        Path(path).expanduser().resolve(),
        "PATCH_MANIFEST_INVALID",
    )
    required = {
        "schema_version",
        "steam_app_id",
        "client_version",
        "feapp_sha256",
        "webplayer_sha256",
        "patch_mode",
        "live_status",
        "media_status",
    }
    if (
        set(value) != required
        or value["schema_version"] != MANIFEST_SCHEMA
        or value["steam_app_id"] != APP_ID
        or value["patch_mode"] != "isolated-copy"
        or not _valid_sha256(value["feapp_sha256"])
        or not _valid_sha256(value["webplayer_sha256"])
    ):
        raise PatchInstallError("PATCH_MANIFEST_INVALID")
    return value


def _steam_roots_from_vdf(steam_root: Path) -> list[Path]:
    roots = [steam_root]
    try:
        text = (
            steam_root / "steamapps" / "libraryfolders.vdf"
        ).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return roots
    roots.extend(
        Path(match.group(1).replace("\\\\", "\\"))
        for match in re.finditer(
            r'"path"\s+"([^"]+)"',
            text,
            flags=re.IGNORECASE,
        )
    )
    return roots


def discover_steam_install(
    library_roots: Iterable[Path] | None = None,
) -> Path:
    """Find AppID 4532590 from Steam registry/manifests without fixed drives."""

    roots = [Path(root) for root in (library_roots or [])]
    if not roots:
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Valve\Steam",
            ) as key:
                roots.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except (ImportError, OSError):
            pass
        roots.extend(
            Path(f"{drive}:/steam")
            for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{drive}:/steam").is_dir()
        )
    expanded: list[Path] = []
    for root in roots:
        expanded.extend(_steam_roots_from_vdf(root.expanduser().resolve()))
    seen: set[Path] = set()
    for root in expanded:
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        manifest = root / "steamapps" / f"appmanifest_{APP_ID}.acf"
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(
            r'"installdir"\s+"([^"]+)"',
            text,
            flags=re.IGNORECASE,
        )
        if match:
            candidate = (
                root / "steamapps" / "common" / match.group(1)
            ).resolve()
            if candidate.is_dir():
                return candidate
    raise PatchInstallError("OFFICIAL_INSTALL_NOT_FOUND")


def validate_official_source(
    source: str | os.PathLike[str],
    manifest: dict[str, Any],
) -> tuple[str, Path, Path]:
    root = Path(source).expanduser().resolve()
    version = str(manifest["client_version"])
    version_root = root / version
    resources = version_root / "resources"
    required = (
        root / "launcher.exe",
        version_root / "Olivia.exe",
        resources / "feapp.dat",
        resources / "webplayer.dat",
    )
    if not all(path.is_file() for path in required):
        raise PatchInstallError("OFFICIAL_INSTALL_NOT_FOUND")
    feapp = required[2]
    webplayer = required[3]
    if (
        _sha256(feapp).lower()
        != str(manifest["feapp_sha256"]).lower()
        or _sha256(webplayer).lower()
        != str(manifest["webplayer_sha256"]).lower()
    ):
        raise PatchInstallError("UNSUPPORTED_OFFICIAL_VERSION")
    return version, feapp, webplayer


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _ignore_official_copy(directory: str, names: list[str]) -> set[str]:
    """Exclude repository/evidence caches that are never runtime assets."""

    excluded = {".git", ".evidence", ".pytest_cache", "__pycache__"}
    return {
        name
        for name in names
        if name in excluded or name.endswith(".pyc")
    }


def _is_non_runtime_payload_file(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered == "pytest.ini"
        or (
            lowered.startswith("requirements-dev")
            and lowered.endswith(".txt")
        )
        or (lowered.startswith("test_") and lowered.endswith(".py"))
    )


def _ignore_payload_copy(directory: str, names: list[str]) -> set[str]:
    excluded = _ignore_official_copy(directory, names)
    excluded.update(
        name for name in names if _is_non_runtime_payload_file(name)
    )
    return excluded


def _git_tracked_payload_files(payload_root: Path) -> set[str] | None:
    """Return tracked paths for a clone/worktree, or None for an archive."""

    git_marker = payload_root / ".git"
    if not git_marker.is_dir() and not git_marker.is_file():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(payload_root), "ls-files", "-z"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PatchInstallError("PATCH_PAYLOAD_GIT_QUERY_FAILED") from exc
    output = result.stdout
    try:
        if isinstance(output, bytes):
            values = output.decode("utf-8").split("\0")
        elif isinstance(output, str):
            values = output.split("\0")
        else:
            raise TypeError("git ls-files returned no text")
    except (UnicodeError, TypeError) as exc:
        raise PatchInstallError("PATCH_PAYLOAD_GIT_QUERY_FAILED") from exc
    return {value for value in values if value}


def _payload_path_is_tracked(
    payload_root: Path,
    path: Path,
    tracked_files: set[str] | None,
) -> bool:
    if tracked_files is None:
        return True
    try:
        relative = path.resolve().relative_to(payload_root.resolve())
    except ValueError:
        return False
    return relative.as_posix() in tracked_files


def _payload_tree_contains_tracked(
    payload_root: Path,
    directory: Path,
    tracked_files: set[str] | None,
) -> bool:
    if tracked_files is None:
        return True
    try:
        relative = directory.resolve().relative_to(payload_root.resolve())
    except ValueError:
        return False
    prefix = relative.as_posix().rstrip("/") + "/"
    return any(path.startswith(prefix) for path in tracked_files)


def _ignore_tracked_payload_copy(
    payload_root: Path,
    tracked_files: set[str] | None,
):
    def ignore(directory: str, names: list[str]) -> set[str]:
        excluded = _ignore_payload_copy(directory, names)
        if tracked_files is None:
            return excluded
        directory_path = Path(directory)
        for name in names:
            source = directory_path / name
            if source.is_dir():
                if not _payload_tree_contains_tracked(
                    payload_root,
                    source,
                    tracked_files,
                ):
                    excluded.add(name)
            elif not _payload_path_is_tracked(
                payload_root,
                source,
                tracked_files,
            ):
                excluded.add(name)
        return excluded

    return ignore


def copy_project_payload(
    payload_root: Path,
    destination: Path,
) -> list[str]:
    """Copy project source/config scripts, never original or model payloads."""

    copied: list[str] = []
    tracked_files = _git_tracked_payload_files(payload_root)
    destination.mkdir(parents=True, exist_ok=True)
    for child in payload_root.iterdir():
        if child.name in {
            ".git",
            ".github",
            ".evidence",
            ".venv",
            "tests",
            "docs",
        }:
            continue
        if child.is_file() and (
            child.name in PAYLOAD_EXCLUDED_ROOT_FILES
            or _is_non_runtime_payload_file(child.name)
        ):
            continue
        target = destination / child.name
        if child.is_dir() and child.name in PAYLOAD_DIRS:
            shutil.copytree(
                child,
                target,
                dirs_exist_ok=True,
                ignore=_ignore_tracked_payload_copy(
                    payload_root,
                    tracked_files,
                ),
            )
            copied.append(child.name + "/")
        elif child.is_file() and (
            child.name in PAYLOAD_ROOT_FILES
            or child.suffix.lower() in PAYLOAD_SUFFIXES
        ) and _payload_path_is_tracked(payload_root, child, tracked_files):
            _copy_file(child, target)
            copied.append(child.name)
    for relative in PAYLOAD_EXTRA_DIRS:
        source_dir = payload_root / Path(*relative.split("/"))
        if source_dir.is_dir() and _payload_tree_contains_tracked(
            payload_root,
            source_dir,
            tracked_files,
        ):
            target_dir = destination / Path(*relative.split("/"))
            shutil.copytree(
                source_dir,
                target_dir,
                dirs_exist_ok=True,
                ignore=_ignore_tracked_payload_copy(
                    payload_root,
                    tracked_files,
                ),
            )
            copied.append(relative + "/")
    for relative in PAYLOAD_EXTRA_FILES:
        source_file = payload_root / Path(*relative.split("/"))
        if source_file.is_file() and _payload_path_is_tracked(
            payload_root,
            source_file,
            tracked_files,
        ):
            target_file = destination / Path(*relative.split("/"))
            _copy_file(source_file, target_file)
            copied.append(relative)
    if any(
        not (destination / name).is_file()
        for name in PAYLOAD_REQUIRED_ROOT_FILES
    ) or any(
        not (destination / Path(*relative.split("/"))).is_file()
        for relative in PAYLOAD_REQUIRED_RELATIVE_FILES
    ):
        raise PatchInstallError("PATCH_PAYLOAD_INCOMPLETE")
    return sorted(copied)


def copy_official_runtime(
    source: Path,
    destination: Path,
    version: str,
) -> list[str]:
    """Copy only the Steam client runtime, never repository files beside it."""

    destination.mkdir(parents=True, exist_ok=True)
    launcher = source / "launcher.exe"
    version_root = source / version
    if not launcher.is_file() or not version_root.is_dir():
        raise PatchInstallError("OFFICIAL_INSTALL_NOT_FOUND")
    _copy_file(launcher, destination / "launcher.exe")
    shutil.copytree(
        version_root,
        destination / version,
        ignore=_ignore_official_copy,
    )
    return ["launcher.exe", version + "/"]


def _write_start_scripts(root: Path, port: int) -> None:
    (root / "START.cmd").write_text(
        f"@echo off\nsetlocal\nset ROOT=%~dp0\nset OLIVIA_PORT={port}\nset PYTHON_EXE=%LOCALAPPDATA%\\BSideOliviaLocal\\runtime\\python-3.12.10-embed-amd64\\python.exe\nif not exist \"%PYTHON_EXE%\" (echo PYTHON_UNAVAILABLE & exit /b 2)\n\"%PYTHON_EXE%\" \"%ROOT%local_backend\\installer\\start_local.py\" --install-root \"%ROOT%.\" --port %OLIVIA_PORT%\nexit /b %ERRORLEVEL%\n",
        encoding="utf-8",
    )
    (root / "UNINSTALL.cmd").write_text(
        "@echo off\nsetlocal\nset ROOT=%~dp0\nset PYTHON_EXE=%LOCALAPPDATA%\\BSideOliviaLocal\\runtime\\python-3.12.10-embed-amd64\\python.exe\nif not exist \"%PYTHON_EXE%\" (echo PYTHON_UNAVAILABLE & exit /b 2)\n\"%PYTHON_EXE%\" \"%ROOT%local_backend\\installer\\uninstall.py\" --installation \"%ROOT%.\" --apply\nexit /b %ERRORLEVEL%\n",
        encoding="utf-8",
    )
    (root / "CONFIGURE.cmd").write_text(
        "@echo off\nsetlocal\nset ROOT=%~dp0\nset PYTHON_EXE=%LOCALAPPDATA%\\BSideOliviaLocal\\runtime\\python-3.12.10-embed-amd64\\python.exe\nif not exist \"%PYTHON_EXE%\" (echo PYTHON_UNAVAILABLE & exit /b 2)\n\"%PYTHON_EXE%\" \"%ROOT%local_backend\\installer\\configure.py\" --installation \"%ROOT%.\"\nexit /b %ERRORLEVEL%\n",
        encoding="utf-8",
    )


def _read_marker(path: Path) -> dict[str, Any]:
    marker = _load_json(path, "PATCH_MARKER_INVALID")
    if (
        marker.get("schema_version") != "olivia.full-patch.install.v2"
        or marker.get("owned_root") != str(path.parent.resolve())
    ):
        raise PatchInstallError("PATCH_MARKER_INVALID")
    return marker


def _marker_matches_current_install(
    marker: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    return (
        marker.get("official_feapp_sha256")
        == manifest["feapp_sha256"]
        and marker.get("official_webplayer_sha256")
        == manifest["webplayer_sha256"]
        and marker.get("original_client_only") is True
        and marker.get("companion_settings_embedded") is True
        and marker.get("webplayer_local_media") is True
    )


def install_full_patch(
    official_source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    payload_root: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
    *,
    port: int = 8899,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    source = Path(official_source).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    payload = Path(payload_root).expanduser().resolve()
    version, _source_feapp, _source_webplayer = validate_official_source(
        source,
        manifest,
    )
    if target == source or target in source.parents or source in target.parents:
        raise PatchInstallError("INSTALL_ROOT_OVERLAPS_OFFICIAL")
    marker_path = target / MARKER_NAME
    if target.exists():
        if marker_path.is_file():
            marker = _read_marker(marker_path)
            if _marker_matches_current_install(marker, manifest):
                return {"status": "ALREADY_INSTALLED", **marker}
        raise PatchInstallError("INSTALL_ROOT_ALREADY_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target
    try:
        copied_runtime = copy_official_runtime(
            source,
            staging / "app",
            version,
        )
        copied = copy_project_payload(payload, staging / "local_backend")
        resources = staging / "app" / version / "resources"
        feapp = resources / "feapp.dat"
        webplayer = resources / "webplayer.dat"
        if (
            _sha256(feapp).lower()
            != str(manifest["feapp_sha256"]).lower()
            or _sha256(webplayer).lower()
            != str(manifest["webplayer_sha256"]).lower()
        ):
            raise PatchInstallError("STAGED_COPY_VERIFICATION_FAILED")
        if not 1 <= port <= 65535:
            raise PatchInstallError("INVALID_PORT")

        base_http = f"http://127.0.0.1:{port}"
        endpoint_patch = patch_feapp(
            feapp,
            f"ws://127.0.0.1:{port}/ws",
            work_root=resources,
        )
        settings_patch = patch_companion_settings(
            feapp,
            base_http,
            work_root=resources,
        )
        player_patch = patch_webplayer(
            webplayer,
            work_root=resources,
        )

        _write_start_scripts(staging, port)
        (staging / "data").mkdir()
        marker = {
            "schema_version": "olivia.full-patch.install.v2",
            "steam_app_id": APP_ID,
            "client_version": version,
            "official_source": str(source),
            "owned_root": str(target),
            "official_feapp_sha256": manifest["feapp_sha256"],
            "patched_feapp_sha256": _sha256(feapp),
            "backup_feapp_sha256": endpoint_patch["backup_sha256"],
            "companion_backup_feapp_sha256": settings_patch[
                "backup_sha256"
            ],
            "companion_settings_status": settings_patch["status"],
            "companion_settings_ui_version": settings_patch["ui_version"],
            "official_webplayer_sha256": manifest["webplayer_sha256"],
            "patched_webplayer_sha256": _sha256(webplayer),
            "backup_webplayer_sha256": player_patch["backup_sha256"],
            "webplayer_patch_status": player_patch["status"],
            "original_client_only": True,
            "companion_settings_embedded": True,
            "webplayer_local_media": True,
            "port": port,
            "owned_paths": list(OWNED_PATHS),
            "preserved_paths": list(PRESERVED_PATHS),
            "runtime_entries": copied_runtime,
            "payload_entries": copied,
            "live_status": manifest["live_status"],
            "media_status": manifest["media_status"],
        }
        (staging / MARKER_NAME).write_text(
            json.dumps(
                marker,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return {"status": "INSTALLED", **marker}


def uninstall_full_patch(
    installation: str | os.PathLike[str],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    root = Path(installation).expanduser().absolute()
    marker_path = root / MARKER_NAME
    if not root.is_dir() or not marker_path.is_file():
        raise PatchInstallError("PATCH_MARKER_NOT_FOUND")
    marker = _read_marker(marker_path)
    try:
        safe_owned_targets(root)
    except ValueError as exc:
        raise PatchInstallError("PATCH_MARKER_INVALID") from exc
    plan = {
        "status": "UNINSTALLED" if apply else "DRY_RUN",
        "owned_paths": list(OWNED_PATHS),
        "preserved_paths": list(PRESERVED_PATHS),
    }
    if not apply:
        return plan
    try:
        remove_owned_targets(root)
    except ValueError as exc:
        raise PatchInstallError("PATCH_MARKER_INVALID") from exc
    return plan
