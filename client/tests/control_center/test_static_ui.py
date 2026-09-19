from __future__ import annotations

import asyncio
from importlib import resources
from threading import Lock

from aiohttp.test_utils import TestClient, TestServer

from control_center.app import create_control_app
from private_world_ledger import LedgerEvent
from private_world_port import PrivateWorldSnapshot


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
        expected_snapshot_version: int | None = None,
    ) -> bool:
        with self._lock:
            self.items.append(event)
            self.current = snapshot
        return True


def test_static_assets_are_packaged_and_external_resource_free() -> None:
    root = resources.files("control_center").joinpath("static")
    index = root.joinpath("index.html").read_text(encoding="utf-8")
    css = root.joinpath("app.css").read_text(encoding="utf-8")
    api = root.joinpath("api.js").read_text(encoding="utf-8")
    app = root.joinpath("app.js").read_text(encoding="utf-8")

    for document in (index, css, api, app):
        assert "https://" not in document
        assert "http://" not in document
    assert '<html lang="zh-CN">' in index
    assert "<main" in index
    assert 'aria-live="polite"' in index
    assert 'type="number"' not in index
    assert '<script type="module" src="/control/static/app.js">' in index
    assert "window.location.hash" in api
    assert "history.replaceState" in api
    assert "sessionStorage" in api
    assert "innerHTML" not in app
    assert "document.write" not in app
    assert "eval(" not in app


def test_static_shell_is_public_but_private_api_remains_authenticated() -> None:
    async def scenario() -> None:
        app = create_control_app(FakeLedger())
        async with TestClient(TestServer(app)) as client:
            expected_types = {
                "/control": {"text/html"},
                "/control/": {"text/html"},
                "/control/static/app.css": {"text/css"},
                "/control/static/api.js": {
                    "text/javascript",
                    "application/javascript",
                },
                "/control/static/app.js": {
                    "text/javascript",
                    "application/javascript",
                },
            }
            for path, content_types in expected_types.items():
                response = await client.get(path)
                assert response.status == 200
                assert response.content_type in content_types
                assert response.headers["Cache-Control"] == "no-store"
                assert "Access-Control-Allow-Origin" not in response.headers
                assert await response.text()

            private = await client.get(
                "/control/api/private-world/snapshot"
            )
            assert private.status == 401
            assert (await private.json())["error"]["code"] == (
                "CONTROL_SESSION_REQUIRED"
            )

    asyncio.run(scenario())
