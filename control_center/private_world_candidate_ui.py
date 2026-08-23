"""Protected graphical review surface for PrivateWorld candidates."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .private_world_candidate_api import (
    CandidateReviewBackend,
    mount_candidate_review_api,
)


_STATIC_ROOT = Path(__file__).with_name("static")


def _asset(name: str) -> web.FileResponse:
    path = _STATIC_ROOT / name
    if not path.is_file():
        raise RuntimeError("CONTROL_CANDIDATE_STATIC_UNAVAILABLE")
    return web.FileResponse(path)


async def candidate_index(request: web.Request) -> web.FileResponse:
    del request
    return _asset("candidates.html")


async def candidate_css(request: web.Request) -> web.FileResponse:
    del request
    return _asset("candidates.css")


async def candidate_script(request: web.Request) -> web.FileResponse:
    del request
    return _asset("candidates.js")


def mount_candidate_control(
    app: web.Application,
    backend: CandidateReviewBackend,
) -> None:
    """Mount the authenticated API and its protected graphical page."""

    mount_candidate_review_api(app, backend)
    app.add_routes(
        [
            web.get("/control/candidates", candidate_index),
            web.get("/control/candidates/", candidate_index),
            web.get(
                "/control/static/candidates.css",
                candidate_css,
            ),
            web.get(
                "/control/static/candidates.js",
                candidate_script,
            ),
        ]
    )


__all__ = ["mount_candidate_control"]
