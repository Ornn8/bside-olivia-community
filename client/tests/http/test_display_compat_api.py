"""The compatibility display mode route the settings page uses, and the page-ready marker the launcher reads."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from installer import display_compat
from original_client_display_api import DISPLAY_COMPAT_PATH, mount_display_compat_api
from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
from original_client_ui_language_api import UI_LANGUAGE_PATH, mount_ui_language_api

TRUSTED_ORIGIN = "https://client.example"
CONFIRM = {"X-Olivia-Setup-Action": "confirmed"}


def _app(tmp_path: Path) -> web.Application:
    service = LLMSetupService(tmp_path, protect=lambda value: value, unprotect=lambda value: value)
    app = web.Application()
    mount_original_client_setup_api(app, service, trusted_origins=(TRUSTED_ORIGIN,))
    mount_ui_language_api(app, service)
    mount_display_compat_api(app, service)
    return app


def test_the_mode_is_read_saved_and_read_back(tmp_path):
    async def scenario() -> None:
        async with TestClient(TestServer(_app(tmp_path))) as client:
            read = await client.get(DISPLAY_COMPAT_PATH, headers={"Origin": TRUSTED_ORIGIN})
            assert read.status == 200 and await read.json() == {"status": "READY", "mode": "auto"}
            for mode in ("on", "off", "auto"):
                saved = await client.post(DISPLAY_COMPAT_PATH, headers={"Origin": TRUSTED_ORIGIN, **CONFIRM},
                                          json={"mode": mode})
                assert saved.status == 200 and await saved.json() == {"status": "SAVED", "mode": mode}
                assert display_compat.read_mode(tmp_path / "config") == mode
                read = await client.get(DISPLAY_COMPAT_PATH, headers={"Origin": TRUSTED_ORIGIN})
                assert (await read.json())["mode"] == mode
            stored = json.loads((tmp_path / "config" / display_compat.CONFIG_NAME).read_text(encoding="utf-8"))
            assert stored == {"mode": "auto", "schema_version": 1}

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [{"mode": "fast"}, {"mode": 1}, {"mode": None}, {}, {"mode": "on", "extra": 1},
                                  {"value": "on"}])
def test_other_values_and_extra_fields_are_rejected(tmp_path, body):
    async def scenario() -> None:
        async with TestClient(TestServer(_app(tmp_path))) as client:
            response = await client.post(DISPLAY_COMPAT_PATH, headers={"Origin": TRUSTED_ORIGIN, **CONFIRM}, json=body)
            assert response.status == 400 and (await response.json())["error_code"] == "LLM_SETUP_FIELDS_INVALID"
            assert not (tmp_path / "config" / display_compat.CONFIG_NAME).exists()

    asyncio.run(scenario())


def test_only_the_trusted_page_with_the_confirmation_header_can_change_it(tmp_path):
    async def scenario() -> None:
        async with TestClient(TestServer(_app(tmp_path))) as client:
            foreign = await client.post(DISPLAY_COMPAT_PATH, headers={"Origin": "https://elsewhere.example", **CONFIRM},
                                        json={"mode": "on"})
            assert foreign.status == 403 and (await foreign.json())["error_code"] == "LLM_SETUP_ORIGIN_FORBIDDEN"
            unconfirmed = await client.post(DISPLAY_COMPAT_PATH, headers={"Origin": TRUSTED_ORIGIN}, json={"mode": "on"})
            assert unconfirmed.status == 403
            assert (await unconfirmed.json())["error_code"] == "LLM_SETUP_CONFIRMATION_REQUIRED"
            foreign_read = await client.get(DISPLAY_COMPAT_PATH, headers={"Origin": "https://elsewhere.example"})
            assert foreign_read.status == 403
            preflight = await client.options(DISPLAY_COMPAT_PATH, headers={"Origin": TRUSTED_ORIGIN})
            assert preflight.status == 204
            assert "X-Olivia-Setup-Action" in preflight.headers["Access-Control-Allow-Headers"]
            assert display_compat.read_mode(tmp_path / "config") == "auto"

    asyncio.run(scenario())


def test_the_page_report_writes_the_ready_marker(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(data_root))

    async def scenario() -> None:
        async with TestClient(TestServer(_app(tmp_path))) as client:
            foreign = await client.post(UI_LANGUAGE_PATH, headers={"Origin": "https://elsewhere.example", **CONFIRM},
                                        json={"language": "zh-cn"})
            assert foreign.status == 403
            assert not (data_root / "logs" / display_compat.PAGE_READY_NAME).exists()
            reported = await client.post(UI_LANGUAGE_PATH, headers={"Origin": TRUSTED_ORIGIN, **CONFIRM},
                                         json={"language": "zh-cn"})
            assert reported.status == 200

    asyncio.run(scenario())
    assert display_compat.page_ready_since(data_root, 0.0)
