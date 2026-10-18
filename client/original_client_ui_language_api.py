"""Local endpoint through which the injected UI reports the interface language to the backend.

``POST /api/ui-language {"language": "zh-cn" | "en"}`` is sent when the page starts and whenever the language is
switched. The backend keeps the value (``runtime.ui_language``), writes its own notices in that language and adds the
language header to calls to Olivia's cloud services in English only. The request is limited to the trusted page
origin on the loopback interface, like the other local settings routes.
"""

from __future__ import annotations

from aiohttp import web

from original_client_setup_api import LLMSetupError, _authorize, _body, _headers
from runtime import ui_language

UI_LANGUAGE_PATH = "/api/ui-language"


def mount_ui_language_api(app: web.Application, setup: object) -> None:
    """Mount the route and load the saved choice from the setup service's config directory."""

    ui_language.configure(setup._config_root / ui_language.FILE_NAME)  # type: ignore[attr-defined]

    async def options(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))

    async def status(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=False)
        return web.json_response(
            {"status": "READY", "language": ui_language.current()}, headers=_headers(origin)
        )

    async def save(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=True)
        payload = await _body(request)
        if set(payload) != {"language"}:
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
        try:
            language, persisted = ui_language.set_language(payload["language"])
        except ValueError as exc:
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400) from exc
        return web.json_response(
            {"status": "SAVED", "language": language, "persisted": persisted}, headers=_headers(origin)
        )

    app.router.add_get(UI_LANGUAGE_PATH, status)
    app.router.add_post(UI_LANGUAGE_PATH, save)
    app.router.add_options(UI_LANGUAGE_PATH, options)


__all__ = ["UI_LANGUAGE_PATH", "mount_ui_language_api"]
