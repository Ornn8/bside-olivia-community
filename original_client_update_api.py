"""Loopback-only actions for user-downloaded local Olivia patches."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
import json
import os
import re
from pathlib import Path
import subprocess
from typing import Any, Protocol
from urllib.parse import urlsplit

from aiohttp import web

from installer.component_update import (
    ComponentUpdateError,
    apply_component_update,
    rollback_component_update,
)


ACTION_PATH = "/toy/updates/local/action"
CONFIRM_HEADER = "X-Olivia-Update-Action"
SESSION_HEADER = "X-Olivia-Setup-Session"
_MAX_JSON_BYTES = 8_192
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_LOOPBACK_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_ORIGINS_KEY = web.AppKey("original_client_update_origins", frozenset)
_MOUNTED_KEY = web.AppKey("original_client_update_mounted", bool)
_REPARSE_POINT = 0x0400


class UpdateAPIError(RuntimeError):
    def __init__(self, code: str, *, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class ComponentUpdater(Protocol):
    def apply(self, package: Path, manifest_sha256: str) -> Mapping[str, object]: ...

    def rollback(self) -> Mapping[str, object]: ...


SessionAuthorizer = Callable[[str], None]
PatchPicker = Callable[[], Path | None]


class LocalComponentUpdater:
    def __init__(self, installation: Path) -> None:
        if not installation.is_absolute():
            raise ValueError("an absolute installation path is required")
        self._installation = installation.resolve()

    def apply(self, package: Path, manifest_sha256: str) -> Mapping[str, object]:
        return apply_component_update(
            self._installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    def rollback(self) -> Mapping[str, object]:
        return rollback_component_update(self._installation)


def _normalize_origins(values: Sequence[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        candidate = value.rstrip("/") if isinstance(value, str) else ""
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("trusted origins are invalid")
        result.add(candidate)
    return frozenset(result)


def _authorize(request: web.Request, *, confirmation: bool) -> str:
    try:
        hostname = urlsplit(f"//{request.host}").hostname
    except ValueError as exc:
        raise UpdateAPIError("UPDATE_HOST_FORBIDDEN", status=403) from exc
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise UpdateAPIError("UPDATE_HOST_FORBIDDEN", status=403)
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin not in request.app[_ORIGINS_KEY] and not _LOOPBACK_ORIGIN_RE.fullmatch(origin):
        raise UpdateAPIError("UPDATE_ORIGIN_FORBIDDEN", status=403)
    if confirmation and request.headers.get(CONFIRM_HEADER) != "confirmed":
        raise UpdateAPIError("UPDATE_CONFIRMATION_REQUIRED", status=403)
    return origin


def _headers(origin: str | None = None, *, preflight: bool = False) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if origin:
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
    if preflight:
        headers.update(
            {
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    f"Content-Type, {CONFIRM_HEADER}, {SESSION_HEADER}"
                ),
                "Access-Control-Max-Age": "600",
            }
        )
    return headers


async def _json_body(request: web.Request) -> dict[str, object]:
    if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
        raise UpdateAPIError("UPDATE_REQUEST_TOO_LARGE", status=413)
    if request.content_type != "application/json":
        raise UpdateAPIError("UPDATE_CONTENT_TYPE_INVALID", status=415)
    raw = await request.content.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise UpdateAPIError("UPDATE_REQUEST_TOO_LARGE", status=413)
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateAPIError("UPDATE_JSON_INVALID", status=400) from exc
    if not isinstance(payload, dict):
        raise UpdateAPIError("UPDATE_JSON_INVALID", status=400)
    return payload


def _package_path(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
    path = Path(value).expanduser()
    if not path.is_absolute() or path.suffix.casefold() != ".oliviapatch":
        raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
    try:
        metadata = path.lstat()
        if path.is_symlink() or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ) or not path.is_file():
            raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
        return path.resolve(strict=True)
    except OSError as exc:
        raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400) from exc


def _select_windows_patch() -> Path | None:
    if os.name != "nt":
        raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503)
    powershell = (
        Path(os.environ.get("SystemRoot", "C:\\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog;"
        "$dialog.Filter = 'Olivia patch (*.oliviapatch)|*.oliviapatch';"
        "$dialog.CheckFileExists = $true;"
        "$dialog.Multiselect = $false;"
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialog.FileName) }"
    )
    try:
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503) from exc
    if completed.returncode != 0:
        raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503)
    selected = completed.stdout.strip()
    return None if not selected else _package_path(selected)


def _public_result(value: Mapping[str, object]) -> dict[str, object]:
    status = value.get("status")
    component = value.get("component")
    version = value.get("version")
    if (
        status not in {"APPLIED", "ROLLED_BACK"}
        or component != "local_backend"
        or not isinstance(version, str)
        or not _VERSION_RE.fullmatch(version)
    ):
        raise UpdateAPIError("UPDATE_RESULT_INVALID", status=503)
    return {
        "status": status,
        "component": component,
        "version": version,
        "restart_required": True,
    }


def mount_original_client_update_api(
    app: web.Application,
    updater: ComponentUpdater,
    *,
    trusted_origins: Sequence[str],
    authorize_session: SessionAuthorizer,
    select_patch: PatchPicker | None = None,
) -> None:
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("UPDATE_API_ALREADY_MOUNTED")
    if not callable(authorize_session):
        raise TypeError("a setup session authorizer is required")
    app[_ORIGINS_KEY] = _normalize_origins(trusted_origins)
    app[_MOUNTED_KEY] = True
    control_lock = asyncio.Lock()
    picker = select_patch or _select_windows_patch

    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except UpdateAPIError as exc:
            return web.json_response(
                {"status": "FAILED", "error_code": exc.code},
                status=exc.status,
                headers=_headers(),
            )

    app.middlewares.append(errors)

    async def options(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))

    async def action(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=True)
        try:
            authorize_session(request.headers.get(SESSION_HEADER, ""))
        except Exception as exc:
            raise UpdateAPIError("UPDATE_LOGIN_REQUIRED", status=403) from exc
        payload = await _json_body(request)
        if payload == {"action": "select"}:
            try:
                selected = await asyncio.to_thread(picker)
                if selected is None:
                    result = {"status": "CANCELLED", "restart_required": False}
                else:
                    result = {
                        "status": "SELECTED",
                        "package_path": str(_package_path(str(selected))),
                        "restart_required": False,
                    }
            except UpdateAPIError:
                raise
            except Exception as exc:
                raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503) from exc
            return web.json_response(result, headers=_headers(origin))
        if payload.get("action") == "apply" and set(payload) == {
            "action", "package_path", "manifest_sha256"
        }:
            digest = payload.get("manifest_sha256")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
            call = updater.apply
            args = (_package_path(payload.get("package_path")), digest)
        elif payload == {"action": "rollback"}:
            call = updater.rollback
            args = ()
        else:
            raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
        try:
            async with control_lock:
                result = await asyncio.to_thread(call, *args)
        except ComponentUpdateError as exc:
            code = str(exc)
            if not re.fullmatch(r"UPDATE_[A-Z0-9_]{3,90}", code):
                code = "UPDATE_ACTION_UNAVAILABLE"
            raise UpdateAPIError(code, status=409) from exc
        except Exception as exc:
            raise UpdateAPIError("UPDATE_ACTION_UNAVAILABLE", status=503) from exc
        return web.json_response(_public_result(result), headers=_headers(origin))

    app.router.add_post(ACTION_PATH, action)
    app.router.add_options(ACTION_PATH, options)


__all__ = [
    "ACTION_PATH",
    "CONFIRM_HEADER",
    "LocalComponentUpdater",
    "PatchPicker",
    "SESSION_HEADER",
    "UpdateAPIError",
    "mount_original_client_update_api",
]
