from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from original_client_setup_api import (
    LLMSetupService,
    _dpapi_protect,
    _dpapi_unprotect,
    mount_original_client_setup_api,
)


TRUSTED_ORIGIN = "https://client.example"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")
def test_setup_dpapi_round_trip_does_not_store_plaintext() -> None:
    fixture_value = "synthetic-setup-key"
    protected = _dpapi_protect(fixture_value)
    assert fixture_value not in protected
    assert _dpapi_unprotect(protected) == fixture_value


def _service(tmp_path: Path, probes: list[tuple[str, str, str]]) -> LLMSetupService:
    async def probe(base_url: str, model: str, api_key: str) -> None:
        probes.append((base_url, model, api_key))

    return LLMSetupService(
        tmp_path,
        protect=lambda value: f"protected:{len(value)}",
        unprotect=lambda _value: "stored-secret",
        probe=probe,
    )


def test_setup_requires_successful_login_and_never_returns_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, [])
        app = web.Application()
        mount_original_client_setup_api(
            app,
            service,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(app)) as client:
            before = await client.get(
                "/toy/setup/status",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert before.status == 200
            before_payload = await before.json()
            assert before_payload["login_observed"] is False
            assert before_payload["show_initial_setup"] is False
            assert before_payload["llm"]["key_configured"] is False

            service.observe_login(success=False)
            assert (await (await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )).json())["show_initial_setup"] is False

            service.observe_login(success=True)
            after_payload = await (await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )).json()
            assert after_payload["show_initial_setup"] is True
            assert "api_key" not in json.dumps(after_payload)
            assert "secret" not in json.dumps(after_payload)

    asyncio.run(scenario())


def test_test_then_save_persists_only_dpapi_key_and_non_secret_config(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        probes: list[tuple[str, str, str]] = []
        service = _service(tmp_path, probes)
        service.observe_login(success=True)
        app = web.Application()
        mount_original_client_setup_api(
            app,
            service,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        headers = {
            "Origin": TRUSTED_ORIGIN,
            "Content-Type": "application/json",
            "X-Olivia-Setup-Action": "confirmed",
        }
        body = {
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "deepseek-v4-flash",
            "api_key": "fixture-private-key",
        }
        async with TestClient(TestServer(app)) as client:
            premature = await client.post("/toy/setup/llm/save", headers=headers, json=body)
            assert premature.status == 409
            assert (await premature.json())["error_code"] == "LLM_SETUP_TEST_REQUIRED"

            tested = await client.post("/toy/setup/llm/test", headers=headers, json=body)
            assert tested.status == 200
            assert await tested.json() == {"status": "AVAILABLE"}
            assert probes == [
                (
                    "https://opencode.ai/zen/go/v1",
                    "deepseek-v4-flash",
                    "fixture-private-key",
                )
            ]

            saved = await client.post("/toy/setup/llm/save", headers=headers, json=body)
            assert saved.status == 200
            assert await saved.json() == {"status": "SAVED", "restart_required": True}

            status_payload = await (await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )).json()
            assert status_payload["llm"] == {
                "base_url": "https://opencode.ai/zen/go/v1",
                "key_configured": True,
                "model": "deepseek-v4-flash",
            }
            assert "fixture-private-key" not in json.dumps(status_payload)

        config = json.loads((tmp_path / "config" / "llm.json").read_text(encoding="utf-8"))
        assert config == {
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "deepseek-v4-flash",
            "schema_version": 1,
        }
        protected = (tmp_path / "config" / "deepseek_api_key.dpapi").read_text(
            encoding="utf-8"
        )
        assert protected.strip() == "protected:19"
        assert "fixture-private-key" not in protected

    asyncio.run(scenario())


def test_setup_rejects_untrusted_origin_and_marks_skipped_setup_complete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, [])
        service.observe_login(success=True)
        app = web.Application()
        mount_original_client_setup_api(
            app,
            service,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(app)) as client:
            forbidden = await client.get(
                "/toy/setup/status",
                headers={"Origin": "https://attacker.example"},
            )
            assert forbidden.status == 403

            complete = await client.post(
                "/toy/setup/complete",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Content-Type": "application/json",
                    "X-Olivia-Setup-Action": "confirmed",
                },
                json={"skipped": True},
            )
            assert complete.status == 200
            assert await complete.json() == {"status": "COMPLETED", "skipped": True}
            status_payload = await (await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )).json()
            assert status_payload["show_initial_setup"] is False
            assert status_payload["setup_completed"] is True
            assert status_payload["skipped"] is True

    asyncio.run(scenario())


def test_setup_rejects_extra_fields_and_invalid_trusted_origins(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, [])
        app = web.Application()
        mount_original_client_setup_api(
            app,
            service,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/setup/llm/test",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Content-Type": "application/json",
                    "X-Olivia-Setup-Action": "confirmed",
                },
                json={
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-flash",
                    "api_key": "fixture-key",
                    "unexpected": True,
                },
            )
            assert response.status == 400
            assert (await response.json())["error_code"] == "LLM_SETUP_FIELDS_INVALID"

    asyncio.run(scenario())

    invalid_app = web.Application()
    try:
        mount_original_client_setup_api(
            invalid_app,
            _service(tmp_path, []),
            trusted_origins=("http://remote.example",),
        )
    except ValueError as error:
        assert str(error) == "trusted origins are invalid"
    else:
        raise AssertionError("non-HTTPS trusted origin was accepted")
