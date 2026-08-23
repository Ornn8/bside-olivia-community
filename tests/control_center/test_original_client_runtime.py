from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer
import pytest

from control_center.original_client_runtime import (
    OriginalClientRuntimeCompositionError,
    mount_original_client_companion_runtime,
)
from original_client_companion_api import STATUS_PATH


def test_runtime_mounts_disabled_capabilities_on_existing_loopback_app() -> None:
    async def scenario() -> None:
        from aiohttp import web

        app = web.Application()
        runtime = mount_original_client_companion_runtime(app)
        assert runtime.backend is not None
        assert runtime.trusted_origins == ()

        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url("/")).rstrip("/")
            response = await client.get(STATUS_PATH, headers={"Origin": origin})
            assert response.status == 200
            payload = await response.json()
            assert payload["status"] == "READY"
            assert payload["capabilities"]["memory"]["state"] == "disabled"
            assert payload["capabilities"]["private_world"]["state"] == "disabled"
            assert payload["capabilities"]["candidates"]["state"] == "disabled"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Access-Control-Allow-Origin"] == origin

    asyncio.run(scenario())


def test_runtime_keeps_external_origin_outside_original_client_boundary() -> None:
    async def scenario() -> None:
        from aiohttp import web

        app = web.Application()
        mount_original_client_companion_runtime(app)
        async with TestClient(TestServer(app)) as client:
            response = await client.get(
                STATUS_PATH,
                headers={"Origin": "https://example.invalid"},
            )
            assert response.status == 403
            payload = await response.json()
            assert payload["error_code"] == "COMPANION_ORIGIN_FORBIDDEN"
            assert "Access-Control-Allow-Origin" not in response.headers

    asyncio.run(scenario())


def test_runtime_accepts_explicit_original_client_origin() -> None:
    async def scenario() -> None:
        from aiohttp import web

        app = web.Application()
        origin = "https://original-client.invalid"
        mount_original_client_companion_runtime(
            app,
            trusted_origins=(origin,),
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.get(STATUS_PATH, headers={"Origin": origin})
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == origin

    asyncio.run(scenario())


def test_runtime_rejects_duplicate_origins_and_duplicate_mount() -> None:
    from aiohttp import web

    app = web.Application()
    with pytest.raises(ValueError):
        mount_original_client_companion_runtime(
            app,
            trusted_origins=("https://client.invalid", "https://client.invalid"),
        )

    mount_original_client_companion_runtime(app)
    with pytest.raises(OriginalClientRuntimeCompositionError) as duplicate:
        mount_original_client_companion_runtime(app)
    assert duplicate.value.code == "ORIGINAL_COMPANION_MOUNT_FAILED"


def test_runtime_opens_no_socket_and_creates_no_second_application(monkeypatch) -> None:
    from aiohttp import web

    app = web.Application()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("composition must not open a listener")

    monkeypatch.setattr(web, "run_app", forbidden)
    runtime = mount_original_client_companion_runtime(app)
    assert runtime.backend is not None
