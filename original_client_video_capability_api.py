"""Loopback API for the user-confirmed ordinary/music video installer."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
import re
from typing import Protocol, Any
from urllib.parse import urlsplit

from aiohttp import web


STATUS_PATH = "/toy/capabilities/video"
ACTION_PATH = "/toy/capabilities/video/action"
CONFIRM_HEADER = "X-Olivia-Capability-Action"
CONFIRM_VALUE = "confirmed"
SESSION_HEADER = "X-Olivia-Setup-Session"
_MAX_JSON_BYTES = 2048
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
) -> None:
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("VIDEO_CAPABILITY_API_ALREADY_MOUNTED")
    app[_ORIGINS_KEY] = frozenset(value.rstrip("/") for value in trusted_origins)
    app[_MOUNTED_KEY] = True
    control_lock = asyncio.Lock()

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
