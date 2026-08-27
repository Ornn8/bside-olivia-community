"""Loopback-only API for user-confirmed optional capability installation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from aiohttp import web


STATUS_PATH = "/toy/capabilities/mem0"
ACTION_PATH = "/toy/capabilities/mem0/action"
CONFIRM_HEADER = "X-Olivia-Capability-Action"
CONFIRM_VALUE = "confirmed"
SESSION_HEADER = "X-Olivia-Setup-Session"
_MAX_JSON_BYTES = 1_024
_LOOPBACK_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_ORIGINS_KEY = web.AppKey("original_client_capability_origins", frozenset)
_MOUNTED_KEY = web.AppKey("original_client_capability_mounted", bool)


class CapabilityAPIError(RuntimeError):
    def __init__(self, code: str, *, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class CapabilityInstaller(Protocol):
    def status(self) -> Any: ...

    def start(self, *, source_mode: str) -> str: ...

    def pause(self) -> str: ...

    def resume(self, *, source_mode: str) -> str: ...

    def uninstall(self, *, remove_model: bool) -> str: ...


SessionAuthorizer = Callable[[str], None]


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
            or candidate in result
        ):
            raise ValueError("trusted origins are invalid")
        result.add(candidate)
    return frozenset(result)


def _authorize(request: web.Request, *, confirmation: bool) -> str:
    try:
        hostname = urlsplit(f"//{request.host}").hostname
    except ValueError as exc:
        raise CapabilityAPIError("CAPABILITY_HOST_FORBIDDEN", status=403) from exc
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CapabilityAPIError("CAPABILITY_HOST_FORBIDDEN", status=403)
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin not in request.app[_ORIGINS_KEY] and not _LOOPBACK_ORIGIN_RE.fullmatch(origin):
        raise CapabilityAPIError("CAPABILITY_ORIGIN_FORBIDDEN", status=403)
    if confirmation and request.headers.get(CONFIRM_HEADER) != CONFIRM_VALUE:
        raise CapabilityAPIError("CAPABILITY_CONFIRMATION_REQUIRED", status=403)
    return origin


def _headers(origin: str | None = None, *, preflight: bool = False) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if origin:
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
    if preflight:
        headers.update(
            {
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    f"Content-Type, {CONFIRM_HEADER}, {SESSION_HEADER}"
                ),
                "Access-Control-Max-Age": "600",
            }
        )
    return headers


async def _json_body(request: web.Request) -> dict[str, object]:
    if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
        raise CapabilityAPIError("CAPABILITY_REQUEST_TOO_LARGE", status=413)
    if request.content_type != "application/json":
        raise CapabilityAPIError("CAPABILITY_CONTENT_TYPE_INVALID", status=415)
    raw = await request.content.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise CapabilityAPIError("CAPABILITY_REQUEST_TOO_LARGE", status=413)
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CapabilityAPIError("CAPABILITY_JSON_INVALID", status=400) from exc
    if not isinstance(payload, dict):
        raise CapabilityAPIError("CAPABILITY_JSON_INVALID", status=400)
    return payload


def _result(value: object) -> str:
    if value not in {"APPLIED", "NOOP", "REJECTED"}:
        raise CapabilityAPIError("CAPABILITY_RESULT_INVALID", status=503)
    return str(value)


def mount_original_client_capability_api(
    app: web.Application,
    installer: CapabilityInstaller,
    *,
    trusted_origins: Sequence[str],
    authorize_session: SessionAuthorizer,
) -> None:
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("CAPABILITY_API_ALREADY_MOUNTED")
    if not callable(authorize_session):
        raise TypeError("a setup session authorizer is required")
    app[_ORIGINS_KEY] = _normalize_origins(trusted_origins)
    app[_MOUNTED_KEY] = True

    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except CapabilityAPIError as exc:
            return web.json_response(
                {"status": "FAILED", "error_code": exc.code},
                status=exc.status,
                headers=_headers(),
            )

    app.middlewares.append(errors)

    async def options(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))

    async def status(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=False)
        try:
            payload = installer.status().to_dict()
        except Exception as exc:
            raise CapabilityAPIError("CAPABILITY_STATUS_UNAVAILABLE", status=503) from exc
        if not isinstance(payload, dict) or payload.get("capability") != "long_term_memory":
            raise CapabilityAPIError("CAPABILITY_STATUS_INVALID", status=503)
        return web.json_response(payload, headers=_headers(origin))

    async def action(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=True)
        try:
            authorize_session(request.headers.get(SESSION_HEADER, ""))
        except Exception as exc:
            raise CapabilityAPIError("CAPABILITY_LOGIN_REQUIRED", status=403) from exc
        payload = await _json_body(request)
        action_name = payload.get("action")
        if action_name == "install" and set(payload) == {"action", "source"}:
            source = payload.get("source")
            if source not in {"auto", "official"}:
                raise CapabilityAPIError("CAPABILITY_FIELDS_INVALID", status=400)
            result = installer.start(source_mode=str(source))
        elif action_name == "pause" and set(payload) == {"action"}:
            result = installer.pause()
        elif action_name == "resume" and set(payload) == {"action", "source"}:
            source = payload.get("source")
            if source not in {"auto", "official"}:
                raise CapabilityAPIError("CAPABILITY_FIELDS_INVALID", status=400)
            result = installer.resume(source_mode=str(source))
        elif action_name == "uninstall" and set(payload) == {"action", "remove_model"}:
            remove_model = payload.get("remove_model")
            if type(remove_model) is not bool:
                raise CapabilityAPIError("CAPABILITY_FIELDS_INVALID", status=400)
            result = installer.uninstall(remove_model=remove_model)
        else:
            raise CapabilityAPIError("CAPABILITY_FIELDS_INVALID", status=400)
        return web.json_response({"status": _result(result)}, headers=_headers(origin))

    app.router.add_get(STATUS_PATH, status)
    app.router.add_post(ACTION_PATH, action)
    for path in (STATUS_PATH, ACTION_PATH):
        app.router.add_options(path, options)


__all__ = ["CapabilityAPIError", "mount_original_client_capability_api"]
