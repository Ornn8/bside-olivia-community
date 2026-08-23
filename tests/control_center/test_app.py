from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Lock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.app import AUTH_KEY, create_control_app
from control_center.auth import CONTROL_CSRF_HEADER
from private_world_ledger import LedgerEvent
from private_world_port import PrivateWorldSnapshot


ROOT = Path(__file__).resolve().parents[2]


class FakeLedger:
    def __init__(self) -> None:
        self.current = PrivateWorldSnapshot()
        self.items: list[LedgerEvent] = []
        self._lock = Lock()

    def snapshot(self) -> PrivateWorldSnapshot:
        return self.current

    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self.items)

    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
    ) -> bool:
        with self._lock:
            if any(
                row.event_id == event.event_id
                or row.delivery_id == event.delivery_id
                for row in self.items
            ):
                return False
            self.items.append(event)
            self.current = snapshot
            return True


def test_control_shell_bootstrap_protected_status_and_logout() -> None:
    async def scenario() -> None:
        app = create_control_app(FakeLedger())
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

            denied = await client.get("/control/api/private-world/snapshot")
            assert denied.status == 401
            assert (await denied.json())["error"]["code"] == "CONTROL_SESSION_REQUIRED"

            token = app[AUTH_KEY].issue_bootstrap_token()
            bootstrapped = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert bootstrapped.status == 200
            payload = (await bootstrapped.json())["data"]
            csrf = payload["csrf_token"]
            assert payload["status"] == "READY"
            assert "HttpOnly" in bootstrapped.headers["Set-Cookie"]
            assert "SameSite=Strict" in bootstrapped.headers["Set-Cookie"]

            snapshot = await client.get("/control/api/private-world/snapshot")
            assert snapshot.status == 200
            assert (await snapshot.json())["data"]["schema_version"] == (
                "p03.private-world-control.v1"
            )

            missing_csrf = await client.post(
                "/control/api/session/logout",
                json={},
                headers={"Origin": origin},
            )
            assert missing_csrf.status == 403
            assert (await missing_csrf.json())["error"]["code"] == (
                "CONTROL_CSRF_REQUIRED"
            )

            logged_out = await client.post(
                "/control/api/session/logout",
                json={},
                headers={"Origin": origin, CONTROL_CSRF_HEADER: csrf},
            )
            assert logged_out.status == 200
            assert (await logged_out.json())["data"]["status"] == "LOGGED_OUT"

            after_logout = await client.get(
                "/control/api/private-world/snapshot"
            )
            assert after_logout.status == 401

    asyncio.run(scenario())


def test_control_shell_rejects_non_loopback_host_and_cross_origin() -> None:
    async def scenario() -> None:
        app = create_control_app(FakeLedger())
        async with TestClient(TestServer(app)) as client:
            invalid_host = await client.get(
                "/control/health",
                headers={"Host": "example.invalid"},
            )
            assert invalid_host.status == 403
            assert (await invalid_host.json())["error"]["code"] == (
                "CONTROL_HOST_FORBIDDEN"
            )

            invalid_origin = await client.get(
                "/control/health",
                headers={"Origin": "https://example.invalid"},
            )
            assert invalid_origin.status == 403
            assert (await invalid_origin.json())["error"]["code"] == (
                "CONTROL_ORIGIN_FORBIDDEN"
            )

            missing_origin = await client.post(
                "/control/api/session/bootstrap",
                json={"token": "not-a-real-token"},
            )
            assert missing_origin.status == 403
            assert (await missing_origin.json())["error"]["code"] == (
                "CONTROL_ORIGIN_FORBIDDEN"
            )

    asyncio.run(scenario())


def test_control_assets_are_self_contained() -> None:
    static_root = ROOT / "control_center" / "static"
    for name in ("index.html", "api.js", "app.js", "app.css"):
        content = (static_root / name).read_text(encoding="utf-8").casefold()
        assert "http://" not in content
        assert "https://" not in content
        assert "//cdn" not in content
        assert "googleapis" not in content
