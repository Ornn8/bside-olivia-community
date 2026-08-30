"""Loopback-only download endpoint for the privacy-safe diagnostic bundle."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import asyncio
import re
from urllib.parse import urlsplit

from aiohttp import web

from runtime.diagnostics.support_bundle import DiagnosticBundleError, build_diagnostic_bundle


DIAGNOSTIC_EXPORT_PATH = "/toy/diagnostics/export"
DIAGNOSTIC_EXPORT_SCHEMA = "olivia.diagnostic-export.v1"
_MOUNTED_KEY = web.AppKey("original_client.diagnostics_mounted", bool)
_ORIGINS_KEY = web.AppKey("original_client.diagnostics_origins", frozenset)
_SOURCE_KEY = web.AppKey("original_client.diagnostics_source", object)
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}$")


class DiagnosticExportAPIError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _trusted_origin(value: object) -> str:
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("trusted diagnostic origin is invalid")
    try:
        parsed = urlsplit(value.rstrip("/"))
    except ValueError as exc:
        raise ValueError("trusted diagnostic origin is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("trusted diagnostic origin is invalid")
    return f"https://{parsed.netloc}"


def _host_is_loopback(request: web.Request) -> bool:
    try:
        host = urlsplit(f"//{request.host}").hostname
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _origin_allowed(request: web.Request, origin: str) -> bool:
    if origin in request.app[_ORIGINS_KEY]:
        return True
    if not _LOCAL_ORIGIN_RE.fullmatch(origin):
        return False
    try:
        port = urlsplit(origin).port
    except ValueError:
        return False
    return port is not None and 1 <= port <= 65535


def _headers(origin: str | None = None) -> dict[str, str]:
    result = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if origin:
        result.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
    return result


def _error(code: str, status: int, *, origin: str | None = None) -> web.Response:
    return web.json_response(
        {
            "schema_version": DIAGNOSTIC_EXPORT_SCHEMA,
            "status": "UNAVAILABLE" if status == 503 else "FAILED",
            "error_code": code,
        },
        status=status,
        headers=_headers(origin),
    )


def _authorize(request: web.Request) -> str:
    if not _host_is_loopback(request):
        raise DiagnosticExportAPIError("DIAGNOSTIC_HOST_FORBIDDEN", 403)
    origin = request.headers.get("Origin", "")
    if not _origin_allowed(request, origin):
        raise DiagnosticExportAPIError("DIAGNOSTIC_ORIGIN_FORBIDDEN", 403)
    return origin


async def _export(request: web.Request) -> web.Response:
    origin: str | None = None
    try:
        origin = _authorize(request)
        source = request.app[_SOURCE_KEY]
        if not callable(source):
            raise DiagnosticExportAPIError("DIAGNOSTIC_EXPORT_UNAVAILABLE", 503)
        values = await asyncio.to_thread(source)
        if not isinstance(values, Mapping):
            raise DiagnosticExportAPIError("DIAGNOSTIC_EXPORT_UNAVAILABLE", 503)
        bundle = build_diagnostic_bundle(values)
        return web.Response(
            body=bundle,
            content_type="application/zip",
            headers={
                **_headers(origin),
                "Content-Disposition": 'attachment; filename="olivia-diagnostic-bundle.zip"',
            },
        )
    except DiagnosticExportAPIError as exc:
        return _error(exc.code, exc.status, origin=origin)
    except (DiagnosticBundleError, OSError, RuntimeError, TypeError, ValueError):
        return _error("DIAGNOSTIC_EXPORT_UNAVAILABLE", 503, origin=origin)


def mount_original_client_diagnostics_api(
    app: web.Application,
    source: Callable[[], Mapping[str, object]],
    *,
    trusted_origins: Sequence[str] = (),
) -> None:
    """Mount the download endpoint once before the legacy catch-all route."""

    if not isinstance(app, web.Application) or not callable(source):
        raise TypeError("an application and diagnostic source are required")
    if len(trusted_origins) > 8:
        raise ValueError("too many trusted diagnostic origins")
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("DIAGNOSTIC_EXPORT_ALREADY_MOUNTED")
    app[_ORIGINS_KEY] = frozenset(_trusted_origin(value) for value in trusted_origins)
    app[_SOURCE_KEY] = source
    app[_MOUNTED_KEY] = True
    app.router.add_get(DIAGNOSTIC_EXPORT_PATH, _export)


__all__ = [
    "DIAGNOSTIC_EXPORT_PATH",
    "DIAGNOSTIC_EXPORT_SCHEMA",
    "DiagnosticExportAPIError",
    "mount_original_client_diagnostics_api",
]
