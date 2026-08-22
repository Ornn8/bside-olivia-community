from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.app import (
    CSRF_HEADER,
    create_control_app,
    issue_bootstrap_token,
)


ROOT = Path(__file__).resolve().parents[2]


def test_control_shell_bootstrap_status_csrf_and_logout() -> None:
    async def scenario() -> None:
        app = create_control_app()
        token = issue_bootstrap_token(app)
        async with TestClient(
            TestServer(app),
            cookie_jar=CookieJar(unsafe=True),
        ) as client:
            origin = str(client.make_url("/")).rstrip("/")
            shell = await client.get("/control/")
            assert shell.status == 200
            assert shell.headers["Cache-Control"] == "no-store"
            assert "default-src 'self'" in shell.headers["Content-Security-Policy"]
            assert shell.headers["X-Frame-Options"] == "DENY"

            bootstrapped = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert bootstrapped.status == 200
            bootstrap_payload = await bootstrapped.json()
            csrf = bootstrap_payload["csrf_token"]
            assert csrf
            assert "HttpOnly" in bootstrapped.headers["Set-Cookie"]
            assert "SameSite=Strict" in bootstrapped.headers["Set-Cookie"]

            reused = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert reused.status == 403
            assert (await reused.json())["error_code"] == "CONTROL_BOOTSTRAP_INVALID"

            status = await client.get("/control/api/status")
            assert status.status == 200
            status_payload = await status.json()
            assert status_payload["capabilities"]["control.shell"] == "available"

            missing_csrf = await client.post(
                "/control/api/session/logout",
                headers={"Origin": origin},
            )
            assert missing_csrf.status == 403
            assert (await missing_csrf.json())["error_code"] == "CONTROL_CSRF_INVALID"

            logged_out = await client.post(
                "/control/api/session/logout",
                headers={"Origin": origin, CSRF_HEADER: csrf},
            )
            assert logged_out.status == 200
            assert (await logged_out.json())["status"] == "LOGGED_OUT"

            denied = await client.get("/control/api/status")
            assert denied.status == 403
            assert (await denied.json())["error_code"] == "CONTROL_SESSION_REQUIRED"

    asyncio.run(scenario())


def test_control_shell_rejects_non_loopback_host_and_cross_origin_mutation() -> None:
    async def scenario() -> None:
        app = create_control_app()
        token = issue_bootstrap_token(app)
        async with TestClient(TestServer(app)) as client:
            invalid_host = await client.get(
                "/control/",
                headers={"Host": "example.invalid"},
            )
            assert invalid_host.status == 403
            assert (await invalid_host.json())["error_code"] == "CONTROL_HOST_FORBIDDEN"

            invalid_origin = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": "https://example.invalid"},
            )
            assert invalid_origin.status == 403
            assert (await invalid_origin.json())["error_code"] == "CONTROL_ORIGIN_FORBIDDEN"

            missing_origin = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
            )
            assert missing_origin.status == 403
            assert (await missing_origin.json())["error_code"] == "CONTROL_ORIGIN_FORBIDDEN"

    asyncio.run(scenario())


def test_control_assets_are_self_contained_and_do_not_embed_external_urls() -> None:
    static_root = ROOT / "control_center" / "static"
    for name in ("index.html", "app.js", "app.css"):
        content = (static_root / name).read_text(encoding="utf-8").casefold()
        assert "http://" not in content
        assert "https://" not in content
        assert "//cdn" not in content
        assert "googleapis" not in content
