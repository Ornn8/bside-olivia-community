"""Loopback-only aiohttp management API for the Companion Control Center."""

from __future__ import annotations

import json
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from private_world_commands import PrivateWorldCommandError
from private_world_service import (
    PrivateWorldCommandLedger,
    PrivateWorldCommandService,
)

from .auth import (
    CONTROL_CSRF_HEADER,
    CONTROL_SESSION_COOKIE,
    ControlAuthError,
    ControlSessionManager,
)
from .private_world_api import (
    PrivateWorldAPIError,
    PrivateWorldControlAPI,
)


AUTH_KEY = web.AppKey("control_center.auth", ControlSessionManager)
PRIVATE_WORLD_API_KEY = web.AppKey(
    "control_center.private_world_api",
    PrivateWorldControlAPI,
)
SESSION_TOKEN_KEY = web.AppKey("control_center.session_token", str)

_PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/control/health"),
        ("POST", "/control/api/session/bootstrap"),
    }
)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; connect-src 'self'; "
        "img-src 'self' data:; media-src 'self' blob:; "
        "script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class ControlRequestError(RuntimeError):
    def __init__(self, code: str, *, http_status: int = 400) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def _response(
    data: dict[str, object],
    *,
    status: int = 200,
) -> web.Response:
    return web.json_response(
        {"ok": True, "data": data},
        status=status,
    )


def _error_response(status: int, code: str) -> web.Response:
    return web.json_response(
        {"ok": False, "error": {"code": code}},
        status=status,
    )


def _host_name(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        return parsed.hostname
    except ValueError:
        return None


def _loopback_request_host(request: web.Request) -> bool:
    return _host_name(request.host) in _LOOPBACK_HOSTS


def _same_loopback_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return False
    request_host = urlsplit(f"//{request.host}")
    origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    request_port = request_host.port or (
        443 if request.scheme == "https" else 80
    )
    return (
        parsed.hostname == request_host.hostname
        and origin_port == request_port
    )


def _auth_error_status(code: str) -> int:
    if code in {"CONTROL_CSRF_REQUIRED", "CONTROL_CSRF_INVALID"}:
        return 403
    return 401


@web.middleware
async def control_boundary_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    response: web.StreamResponse
    try:
        if not _loopback_request_host(request):
            raise ControlRequestError(
                "CONTROL_HOST_FORBIDDEN",
                http_status=403,
            )
        origin_present = bool(request.headers.get("Origin"))
        if origin_present and not _same_loopback_origin(request):
            raise ControlRequestError(
                "CONTROL_ORIGIN_FORBIDDEN",
                http_status=403,
            )
        if request.method in _MUTATING_METHODS and not origin_present:
            raise ControlRequestError(
                "CONTROL_ORIGIN_FORBIDDEN",
                http_status=403,
            )

        if (request.method, request.path) not in _PUBLIC_ROUTES:
            token = request.cookies.get(CONTROL_SESSION_COOKIE)
            request.app[AUTH_KEY].authenticate(token)
            request[SESSION_TOKEN_KEY] = token or ""
            if request.method in _MUTATING_METHODS:
                request.app[AUTH_KEY].validate_csrf(
                    token,
                    request.headers.get(CONTROL_CSRF_HEADER),
                )

        response = await handler(request)
    except ControlRequestError as exc:
        response = _error_response(exc.http_status, exc.code)
    except ControlAuthError as exc:
        response = _error_response(_auth_error_status(exc.code), exc.code)
    except PrivateWorldAPIError as exc:
        response = _error_response(exc.http_status, exc.code)
    except PrivateWorldCommandError:
        response = _error_response(400, "PRIVATE_WORLD_COMMAND_INVALID")
    except json.JSONDecodeError:
        response = _error_response(400, "CONTROL_JSON_INVALID")
    except web.HTTPException as exc:
        code = {
            404: "CONTROL_ROUTE_NOT_FOUND",
            405: "CONTROL_METHOD_NOT_ALLOWED",
            413: "CONTROL_BODY_TOO_LARGE",
            415: "CONTROL_CONTENT_TYPE_INVALID",
        }.get(exc.status, "CONTROL_HTTP_ERROR")
        response = _error_response(exc.status, code)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        response = _error_response(500, "CONTROL_INTERNAL_ERROR")

    response.headers.update(_SECURITY_HEADERS)
    response.headers.pop("Access-Control-Allow-Origin", None)
    return response


async def _json_body(request: web.Request) -> object:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        return await request.json(loads=json.loads)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise json.JSONDecodeError("invalid", "", 0) from exc


async def health(request: web.Request) -> web.Response:
    del request
    return _response(
        {
            "status": "READY",
            "authentication": "required",
            "network_scope": "loopback",
        }
    )


async def bootstrap(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if not isinstance(body, dict) or set(body) != {"token"}:
        raise ControlRequestError("CONTROL_BODY_FIELDS_INVALID")
    credentials = request.app[AUTH_KEY].bootstrap(body["token"])
    response = _response(
        {
            "status": "READY",
            "csrf_token": credentials.csrf_token,
            "expires_in_seconds": int(
                request.app[AUTH_KEY].idle_timeout_seconds
            ),
        }
    )
    response.set_cookie(
        CONTROL_SESSION_COOKIE,
        credentials.session_token,
        httponly=True,
        secure=request.secure,
        samesite="Strict",
        max_age=int(request.app[AUTH_KEY].idle_timeout_seconds),
        path="/control",
    )
    return response


async def logout(request: web.Request) -> web.Response:
    request.app[AUTH_KEY].logout(request.get(SESSION_TOKEN_KEY))
    response = _response({"status": "LOGGED_OUT"})
    response.del_cookie(CONTROL_SESSION_COOKIE, path="/control")
    return response


async def private_world_snapshot(request: web.Request) -> web.Response:
    return _response(request.app[PRIVATE_WORLD_API_KEY].snapshot())


async def private_world_events(request: web.Request) -> web.Response:
    return _response(request.app[PRIVATE_WORLD_API_KEY].events())


async def private_world_relationship_event(
    request: web.Request,
) -> web.Response:
    return _response(
        request.app[PRIVATE_WORLD_API_KEY].relationship_event(
            await _json_body(request)
        )
    )


async def private_world_relationship_stage(
    request: web.Request,
) -> web.Response:
    return _response(
        request.app[PRIVATE_WORLD_API_KEY].relationship_stage(
            await _json_body(request)
        )
    )


async def private_world_nickname(request: web.Request) -> web.Response:
    return _response(
        request.app[PRIVATE_WORLD_API_KEY].nickname(
            await _json_body(request)
        )
    )


async def private_world_home_access(request: web.Request) -> web.Response:
    return _response(
        request.app[PRIVATE_WORLD_API_KEY].home_access(
            await _json_body(request)
        )
    )


async def private_world_continuation(request: web.Request) -> web.Response:
    return _response(
        request.app[PRIVATE_WORLD_API_KEY].continuation(
            await _json_body(request)
        )
    )


def create_control_app(
    ledger: PrivateWorldCommandLedger,
    *,
    service: PrivateWorldCommandService | None = None,
    session_manager: ControlSessionManager | None = None,
) -> web.Application:
    command_service = service or PrivateWorldCommandService(ledger)
    app = web.Application(
        middlewares=[control_boundary_middleware],
        client_max_size=64 * 1024,
    )
    app[AUTH_KEY] = session_manager or ControlSessionManager()
    app[PRIVATE_WORLD_API_KEY] = PrivateWorldControlAPI(
        ledger,
        command_service,
    )
    app.add_routes(
        [
            web.get("/control/health", health),
            web.post("/control/api/session/bootstrap", bootstrap),
            web.post("/control/api/session/logout", logout),
            web.get(
                "/control/api/private-world/snapshot",
                private_world_snapshot,
            ),
            web.get(
                "/control/api/private-world/events",
                private_world_events,
            ),
            web.post(
                "/control/api/private-world/relationship-events",
                private_world_relationship_event,
            ),
            web.post(
                "/control/api/private-world/relationship-stage",
                private_world_relationship_stage,
            ),
            web.post(
                "/control/api/private-world/nicknames",
                private_world_nickname,
            ),
            web.post(
                "/control/api/private-world/home-access",
                private_world_home_access,
            ),
            web.post(
                "/control/api/private-world/continuations",
                private_world_continuation,
            ),
        ]
    )
    return app


__all__ = [
    "AUTH_KEY",
    "PRIVATE_WORLD_API_KEY",
    "SESSION_TOKEN_KEY",
    "ControlRequestError",
    "create_control_app",
]
