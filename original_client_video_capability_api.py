"""Loopback API for the user-confirmed ordinary/music video installer."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Protocol, Any
from urllib.parse import urlsplit

from aiohttp import web


STATUS_PATH = "/toy/capabilities/video"
ACTION_PATH = "/toy/capabilities/video/action"
CONFIRM_HEADER = "X-Olivia-Capability-Action"
CONFIRM_VALUE = "confirmed"
SESSION_HEADER = "X-Olivia-Setup-Session"
_MAX_JSON_BYTES = 2048
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_LOOPBACK_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_ORIGINS_KEY = web.AppKey("original_client_video_capability_origins", frozenset)
_MOUNTED_KEY = web.AppKey("original_client_video_capability_mounted", bool)


class VideoCapabilityAPIError(RuntimeError):
    def __init__(self, code: str, *, status: int) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class VideoCapabilityAPIInstaller(Protocol):
    def status(self) -> dict[str, object]: ...
    def start(self, *, bundle_id: str, source_mode: str = "auto", accept_licenses: bool = False) -> str: ...
    def pause(self) -> str: ...
    def resume(self, *, bundle_id: str, source_mode: str = "auto", accept_licenses: bool = False) -> str: ...
    def retry(self, *, bundle_id: str, source_mode: str = "auto", accept_licenses: bool = False) -> str: ...
    def import_runtime_root(self, *, runtime_root: Path, manifest_sha256: str) -> str: ...


def _runtime_root_path(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise VideoCapabilityAPIError("VIDEO_RUNTIME_ROOT_INVALID", status=400)
    path = Path(value).expanduser()
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise VideoCapabilityAPIError("VIDEO_RUNTIME_ROOT_INVALID", status=400)
        return path.resolve(strict=True)
    except OSError as exc:
        raise VideoCapabilityAPIError("VIDEO_RUNTIME_ROOT_INVALID", status=400) from exc


def _select_windows_runtime_root() -> Path | None:
    if os.name != "nt":
        raise VideoCapabilityAPIError("VIDEO_RUNTIME_PICKER_UNAVAILABLE", status=503)
    powershell = (
        Path(os.environ.get("SystemRoot", "C:\\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$dialog.Description = '选择 Olivia 视频运行时根目录';"
        "$dialog.ShowNewFolderButton = $false;"
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialog.SelectedPath) }"
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
        raise VideoCapabilityAPIError("VIDEO_RUNTIME_PICKER_UNAVAILABLE", status=503) from exc
    if completed.returncode != 0:
        raise VideoCapabilityAPIError("VIDEO_RUNTIME_PICKER_UNAVAILABLE", status=503)
    selected = completed.stdout.strip()
    return None if not selected else _runtime_root_path(selected)


def _authorize(request: web.Request, *, confirmation: bool) -> str:
    try:
        hostname = urlsplit(f"//{request.host}").hostname
    except ValueError as exc:
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_HOST_FORBIDDEN", status=403) from exc
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_HOST_FORBIDDEN", status=403)
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin not in request.app[_ORIGINS_KEY] and not _LOOPBACK_ORIGIN_RE.fullmatch(origin):
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_ORIGIN_FORBIDDEN", status=403)
    if confirmation and request.headers.get(CONFIRM_HEADER) != CONFIRM_VALUE:
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_CONFIRMATION_REQUIRED", status=403)
    return origin


async def _body(request: web.Request) -> dict[str, object]:
    if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_REQUEST_TOO_LARGE", status=413)
    if request.content_type != "application/json":
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_CONTENT_TYPE_INVALID", status=415)
    raw = await request.content.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_REQUEST_TOO_LARGE", status=413)
    try:
        value: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_JSON_INVALID", status=400) from exc
    if not isinstance(value, dict):
        raise VideoCapabilityAPIError("VIDEO_CAPABILITY_JSON_INVALID", status=400)
    return value


def mount_original_client_video_capability_api(
    app: web.Application,
    installer: VideoCapabilityAPIInstaller,
    *,
    trusted_origins: Sequence[str],
    authorize_session,
    select_runtime_root=None,
) -> None:
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("VIDEO_CAPABILITY_API_ALREADY_MOUNTED")
    app[_ORIGINS_KEY] = frozenset(value.rstrip("/") for value in trusted_origins)
    app[_MOUNTED_KEY] = True
    control_lock = asyncio.Lock()
    runtime_picker = select_runtime_root or _select_windows_runtime_root

    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except VideoCapabilityAPIError as exc:
            return web.json_response({"status": "FAILED", "error_code": exc.code}, status=exc.status)

    app.middlewares.append(errors)

    async def options(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=False)
        return web.Response(status=204, headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": f"Content-Type, {CONFIRM_HEADER}, {SESSION_HEADER}",
            "Cache-Control": "no-store",
        })

    async def status(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=False)
        async with control_lock:
            payload = await asyncio.to_thread(installer.status)
        if not isinstance(payload, dict) or payload.get("capability") != "video":
            raise VideoCapabilityAPIError("VIDEO_CAPABILITY_STATUS_INVALID", status=503)
        return web.json_response(payload, headers={"Access-Control-Allow-Origin": origin, "Cache-Control": "no-store"})

    async def action(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=True)
        try:
            authorize_session(request.headers.get(SESSION_HEADER, ""))
        except Exception as exc:
            raise VideoCapabilityAPIError("VIDEO_CAPABILITY_LOGIN_REQUIRED", status=403) from exc
        payload = await _body(request)
        action_name = payload.get("action")
        bundle_id = payload.get("bundle_id")
        source = payload.get("source", "auto")
        if action_name in {"install", "resume", "retry"}:
            fields = {"action", "bundle_id", "source"}
            if (
                set(payload) not in (fields, fields | {"accept_licenses"})
                or not isinstance(bundle_id, str)
                or source not in {"auto", "official"}
                or type(payload.get("accept_licenses", False)) is not bool
            ):
                raise VideoCapabilityAPIError("VIDEO_CAPABILITY_FIELDS_INVALID", status=400)
            call = installer.start if action_name == "install" else getattr(installer, str(action_name))
            kwargs = {
                "bundle_id": bundle_id,
                "source_mode": str(source),
                "accept_licenses": payload.get("accept_licenses", False),
            }
        elif action_name == "pause":
            if set(payload) != {"action"}:
                raise VideoCapabilityAPIError("VIDEO_CAPABILITY_FIELDS_INVALID", status=400)
            call, kwargs = installer.pause, {}
        elif action_name == "select_runtime":
            if set(payload) != {"action"}:
                raise VideoCapabilityAPIError("VIDEO_CAPABILITY_FIELDS_INVALID", status=400)
            selected = await asyncio.to_thread(runtime_picker)
            result = {"status": "CANCELLED"}
            if selected is not None:
                result = {
                    "status": "SELECTED",
                    "runtime_root": str(_runtime_root_path(str(selected))),
                }
            return web.json_response(
                result,
                headers={"Access-Control-Allow-Origin": origin, "Cache-Control": "no-store"},
            )
        elif action_name == "import_runtime":
            if (
                set(payload) != {"action", "runtime_root", "manifest_sha256"}
                or not isinstance(payload.get("manifest_sha256"), str)
                or not _SHA256_RE.fullmatch(str(payload["manifest_sha256"]).lower())
            ):
                raise VideoCapabilityAPIError("VIDEO_CAPABILITY_FIELDS_INVALID", status=400)
            call = installer.import_runtime_root
            kwargs = {
                "runtime_root": _runtime_root_path(payload.get("runtime_root")),
                "manifest_sha256": str(payload["manifest_sha256"]).lower(),
            }
        elif action_name in {"import_official", "import_offline"}:
            expected = {"action"} if action_name == "import_official" else {"action", "bundle_id"}
            if set(payload) != expected or (
                action_name == "import_offline" and not isinstance(bundle_id, str)
            ):
                raise VideoCapabilityAPIError("VIDEO_CAPABILITY_FIELDS_INVALID", status=400)
            raise VideoCapabilityAPIError(
                "VIDEO_NATIVE_PATH_SELECTION_UNAVAILABLE", status=503
            )
        else:
            raise VideoCapabilityAPIError("VIDEO_CAPABILITY_FIELDS_INVALID", status=400)
        try:
            async with control_lock:
                result = await asyncio.to_thread(call, **kwargs)
        except Exception as exc:
            raise VideoCapabilityAPIError("VIDEO_CAPABILITY_ACTION_UNAVAILABLE", status=503) from exc
        if result not in {"APPLIED", "NOOP", "REJECTED"}:
            raise VideoCapabilityAPIError("VIDEO_CAPABILITY_RESULT_INVALID", status=503)
        return web.json_response({"status": result}, headers={"Access-Control-Allow-Origin": origin, "Cache-Control": "no-store"})

    app.router.add_get(STATUS_PATH, status)
    app.router.add_post(ACTION_PATH, action)
    for path in (STATUS_PATH, ACTION_PATH):
        app.router.add_options(path, options)


__all__ = ["ACTION_PATH", "STATUS_PATH", "mount_original_client_video_capability_api"]
