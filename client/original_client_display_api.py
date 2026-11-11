"""Local endpoint for the compatibility display mode shown under "Updates and help" in settings.

``GET /api/display-compat`` answers ``{"status": "READY", "mode": "auto" | "on" | "off"}``;
``POST /api/display-compat {"mode": ...}`` saves the choice to ``config/display-compat.json``. The launcher
reads it before the next client start (``installer/display_compat.py``), so a change applies then. The route is
limited to the trusted page origin on the loopback interface, like the other local settings routes.
"""

from __future__ import annotations

from aiohttp import web

from installer import display_compat
from original_client_setup_api import LLMSetupError, _authorize, _body, _headers

DISPLAY_COMPAT_PATH = "/api/display-compat"


def mount_display_compat_api(app: web.Application, setup: object) -> None:
    """Mount the route on the setup service's config directory."""

    config_root = setup._config_root  # type: ignore[attr-defined]

    async def options(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))

    async def status(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=False)
        return web.json_response(
            {"status": "READY", "mode": display_compat.read_mode(config_root)}, headers=_headers(origin)
        )

    async def save(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=True)
        payload = await _body(request)
        if set(payload) != {"mode"} or display_compat.normalize_mode(payload["mode"]) is None:
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
        try:
            mode = display_compat.save_mode(config_root, payload["mode"])
        except OSError as exc:
            raise LLMSetupError("DISPLAY_COMPAT_SAVE_FAILED", status=500) from exc
        return web.json_response({"status": "SAVED", "mode": mode}, headers=_headers(origin))

    app.router.add_get(DISPLAY_COMPAT_PATH, status)
    app.router.add_post(DISPLAY_COMPAT_PATH, save)
    app.router.add_options(DISPLAY_COMPAT_PATH, options)


__all__ = ["DISPLAY_COMPAT_PATH", "mount_display_compat_api"]
