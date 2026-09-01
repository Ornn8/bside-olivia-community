from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from llm_gateway import ManagedLLMConfig

from original_client_setup_api import (
    ERROR_HTTP_STATUSES,
    LLM_DELETE_PATH,
    LLM_SAVE_PATH,
    LLM_TEST_PATH,
    LLMSetupService,
    LLMSetupError,
    PUBLIC_ROUTE_CONTRACT,
    SETUP_COMPLETE_PATH,
    SETUP_STATUS_PATH,
    _dpapi_protect,
    _dpapi_unprotect,
    mount_original_client_setup_api,
)


TRUSTED_ORIGIN = "https://client.example"
SESSION_HEADER = "X-Olivia-Setup-Session"
ROOT = Path(__file__).parents[2]


def test_initial_setup_public_contract_matches_routes_and_schema() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "initial_setup_api_contract.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT / "contracts" / "initial_setup_api_contract.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(schema).validate(contract)
    assert set(contract["routes"]) == {
        SETUP_STATUS_PATH,
        LLM_TEST_PATH,
        LLM_SAVE_PATH,
        LLM_DELETE_PATH,
        SETUP_COMPLETE_PATH,
    }
    assert contract["routes"] == PUBLIC_ROUTE_CONTRACT
    assert {
        code: details["http_statuses"]
        for code, details in contract["error_codes"].items()
    } == ERROR_HTTP_STATUSES
    assert {
        details["status"] for details in contract["error_codes"].values()
    } == {"FAILED"}


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


def test_setup_opens_before_sign_in_and_never_returns_secret(tmp_path: Path) -> None:
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
            assert before_payload["show_initial_setup"] is True
            assert before_payload["llm"]["key_configured"] is False
            assert isinstance(before_payload["session_token"], str)
            assert len(before_payload["session_token"]) >= 32

            blocked = await client.post(
                "/toy/setup/complete",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Content-Type": "application/json",
                    "X-Olivia-Setup-Action": "confirmed",
                },
                json={"skipped": True},
            )
            assert blocked.status == 403
            assert (await blocked.json())["error_code"] == "LLM_SETUP_LOGIN_REQUIRED"

            service.observe_login(success=False)
            assert (await (await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )).json())["show_initial_setup"] is True

            service.observe_login(success=True)
            after_payload = await (await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )).json()
            assert after_payload["show_initial_setup"] is True
            assert isinstance(after_payload["session_token"], str)
            assert len(after_payload["session_token"]) >= 32
            assert "api_key" not in json.dumps(after_payload)
            assert "secret" not in json.dumps(after_payload)

    asyncio.run(scenario())


def test_test_then_save_persists_only_dpapi_key_and_non_secret_config(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        probes: list[tuple[str, str, str]] = []
        applied: list[tuple[ManagedLLMConfig, str | None]] = []
        async def probe(base_url: str, model: str, api_key: str) -> None:
            probes.append((base_url, model, api_key))

        service = LLMSetupService(
            tmp_path,
            protect=lambda value: f"protected:{len(value)}",
            unprotect=lambda value: "x" * int(value.split(":", 1)[1]),
            probe=probe,
            apply_runtime=lambda config, api_key: applied.append((config, api_key)),
        )
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
            SESSION_HEADER: str(service.status()["session_token"]),
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
            assert premature.headers["Access-Control-Allow-Origin"] == TRUSTED_ORIGIN

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
            assert await saved.json() == {
                "status": "SAVED",
                "reload_applied": True,
                "restart_required": True,
            }
            assert applied == [
                (
                    ManagedLLMConfig(
                        provider="openai_compatible",
                        base_url="https://opencode.ai/zen/go/v1",
                        model="deepseek-v4-flash",
                        max_retries=2,
                    ),
                    "fixture-private-key",
                )
            ]

            status_payload = await (await client.get(
                "/toy/setup/status", headers={"Origin": TRUSTED_ORIGIN}
            )).json()
            assert status_payload["llm"] == {
                "base_url": "https://opencode.ai/zen/go/v1",
                "key_configured": True,
                "max_retries": 2,
                "model": "deepseek-v4-flash",
                "provider": "openai_compatible",
            }
            assert "fixture-private-key" not in json.dumps(status_payload)

        config = json.loads((tmp_path / "config" / "llm.json").read_text(encoding="utf-8"))
        assert config["base_url"] == "https://opencode.ai/zen/go/v1"
        assert config["model"] == "deepseek-v4-flash"
        assert config["schema_version"] == 3
        assert config["provider"] == "openai_compatible"
        assert config["max_retries"] == 2
        assert config["key_file"].startswith("deepseek_api_key.")
        assert config["key_file"].endswith(".dpapi")
        protected_path = tmp_path / "config" / config["key_file"]
        protected = protected_path.read_text(encoding="utf-8")
        assert protected.strip() == "protected:19"
        assert config["key_sha256"] == hashlib.sha256(
            protected_path.read_bytes()
        ).hexdigest()
        assert "fixture-private-key" not in protected

    asyncio.run(scenario())


def test_setup_connection_probe_identifies_the_client_to_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        seen: dict[str, str | None] = {}

        async def completion(request: web.Request) -> web.Response:
            seen["user_agent"] = request.headers.get("User-Agent")
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completion)
        async with TestClient(TestServer(app)) as client:
            service = LLMSetupService(tmp_path)
            await service.test(
                {
                    "base_url": str(client.make_url("/v1")),
                    "model": "deepseek-v4-flash",
                    "api_key": "fixture-private-key",
                }
            )

        assert seen["user_agent"] == "Olivia-Community/0.1"

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

            loopback = await client.get(
                "/toy/setup/status",
                headers={"Origin": "http://127.0.0.1:45678"},
            )
            assert loopback.status == 403

            complete = await client.post(
                "/toy/setup/complete",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Content-Type": "application/json",
                    "X-Olivia-Setup-Action": "confirmed",
                    SESSION_HEADER: str(service.status()["session_token"]),
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


def test_setup_cannot_complete_without_saved_key_unless_explicitly_skipped(
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
            response = await client.post(
                "/toy/setup/complete",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Content-Type": "application/json",
                    "X-Olivia-Setup-Action": "confirmed",
                    SESSION_HEADER: str(service.status()["session_token"]),
                },
                json={"skipped": False},
            )
            assert response.status == 409
            assert (await response.json())["error_code"] == "LLM_SETUP_KEY_REQUIRED"

    asyncio.run(scenario())


def test_setup_rejects_extra_fields_and_invalid_trusted_origins(tmp_path: Path) -> None:
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
            response = await client.post(
                "/toy/setup/llm/test",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Content-Type": "application/json",
                    "X-Olivia-Setup-Action": "confirmed",
                    SESSION_HEADER: str(service.status()["session_token"]),
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


def test_setup_stored_key_cannot_be_probed_against_changed_endpoint(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        probes: list[tuple[str, str, str]] = []
        service = _service(tmp_path, probes)
        service.observe_login(success=True)
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / "deepseek_api_key.dpapi").write_text(
            "protected:fixture\n", encoding="utf-8"
        )
        (tmp_path / "config" / "llm.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-flash",
                }
            ),
            encoding="utf-8",
        )
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
                    SESSION_HEADER: str(service.status()["session_token"]),
                },
                json={
                    "base_url": "https://collector.example/v1",
                    "model": "deepseek-v4-flash",
                    "api_key": "",
                },
            )
            assert response.status == 400
            assert (await response.json())["error_code"] == "LLM_SETUP_KEY_REQUIRED"
            assert probes == []

    asyncio.run(scenario())


def test_setup_delete_removes_only_managed_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, [])
        service.observe_login(success=True)
        config_root = tmp_path / "config"
        config_root.mkdir(parents=True)
        key_path = config_root / "deepseek_api_key.dpapi"
        key_path.write_text("protected:fixture\n", encoding="utf-8")
        retained = config_root / "retained.txt"
        retained.write_text("keep", encoding="utf-8")
        app = web.Application()
        mount_original_client_setup_api(
            app,
            service,
            trusted_origins=(TRUSTED_ORIGIN,),
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/setup/llm/delete",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "Content-Type": "application/json",
                    "X-Olivia-Setup-Action": "confirmed",
                    SESSION_HEADER: str(service.status()["session_token"]),
                },
                json={},
            )
            assert response.status == 200
            assert await response.json() == {
                "status": "DELETED",
                "reload_applied": False,
                "restart_required": True,
            }
        assert not key_path.exists()
        assert retained.read_text(encoding="utf-8") == "keep"

    asyncio.run(scenario())


def test_runtime_apply_failure_rolls_back_saved_config_and_key(tmp_path: Path) -> None:
    async def probe(_base_url: str, _model: str, _api_key: str) -> None:
        return None

    config_root = tmp_path / "config"
    config_root.mkdir(parents=True)
    config_path = config_root / "llm.json"
    previous_config = (
        '{"schema_version":1,"base_url":"https://old.example/v1",'
        '"model":"old-model"}\n'
    ).encode("utf-8")
    config_path.write_bytes(previous_config)
    old_key = config_root / "deepseek_api_key.dpapi"
    old_key.write_text("protected:old\n", encoding="utf-8")

    def fail_apply(_base_url: str, _model: str, _api_key: str | None) -> None:
        raise RuntimeError("synthetic apply failure")

    service = LLMSetupService(
        tmp_path,
        protect=lambda value: f"protected:{len(value)}",
        unprotect=lambda _value: "stored-secret",
        probe=probe,
        apply_runtime=fail_apply,
    )
    asyncio.run(
        service.test(
            {
                "base_url": "https://new.example/v1",
                "model": "new-model",
                "api_key": "replacement-key",
            }
        )
    )

    with pytest.raises(LLMSetupError, match="LLM_SETUP_SAVE_FAILED"):
        service.save(
            {
                "base_url": "https://new.example/v1",
                "model": "new-model",
                "api_key": "replacement-key",
            }
        )

    assert config_path.read_bytes() == previous_config
    assert old_key.read_text(encoding="utf-8") == "protected:old\n"
    assert list(config_root.glob("deepseek_api_key.*.dpapi")) == []


def test_runtime_apply_failure_rolls_back_key_deletion(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True)
    config_path = config_root / "llm.json"
    previous_config = (
        '{"schema_version":1,"base_url":"https://old.example/v1",'
        '"model":"old-model"}\n'
    ).encode("utf-8")
    config_path.write_bytes(previous_config)
    old_key = config_root / "deepseek_api_key.dpapi"
    old_key.write_text("protected:old\n", encoding="utf-8")

    service = LLMSetupService(
        tmp_path,
        protect=lambda value: value,
        unprotect=lambda value: value,
        probe=lambda *_args: None,
        apply_runtime=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("synthetic apply failure")
        ),
    )

    with pytest.raises(LLMSetupError, match="LLM_SETUP_SAVE_FAILED"):
        service.delete()

    assert config_path.read_bytes() == previous_config
    assert old_key.read_text(encoding="utf-8") == "protected:old\n"


def test_failed_rollback_keeps_the_new_key_referenced_by_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def probe(_base_url: str, _model: str, _api_key: str) -> None:
        return None

    config_root = tmp_path / "config"
    config_root.mkdir(parents=True)
    (config_root / "llm.json").write_text(
        '{"schema_version":1,"base_url":"https://old.example/v1",'
        '"model":"old-model"}\n',
        encoding="utf-8",
    )
    original_write_bytes = Path.write_bytes

    def fail_rollback(path: Path, payload: bytes) -> int:
        if path.name == "llm.json.rollback":
            raise OSError("synthetic rollback failure")
        return original_write_bytes(path, payload)

    monkeypatch.setattr(Path, "write_bytes", fail_rollback)
    service = LLMSetupService(
        tmp_path,
        protect=lambda value: f"protected:{len(value)}",
        unprotect=lambda value: value,
        probe=probe,
        apply_runtime=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("synthetic apply failure")
        ),
    )
    body = {
        "base_url": "https://new.example/v1",
        "model": "new-model",
        "api_key": "replacement-key",
    }
    asyncio.run(service.test(body))

    with pytest.raises(LLMSetupError, match="LLM_SETUP_SAVE_FAILED"):
        service.save(body)

    config = json.loads((config_root / "llm.json").read_text(encoding="utf-8"))
    assert config["schema_version"] == 3
    assert (config_root / config["key_file"]).is_file()


def test_interrupted_save_keeps_previous_provider_key_generation_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True)
    old_key = config_root / ("deepseek_api_key." + "a" * 32 + ".dpapi")
    old_key.write_text("old-ciphertext\n", encoding="utf-8")
    old_config = {
        "schema_version": 2,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "key_file": old_key.name,
        "key_sha256": hashlib.sha256(old_key.read_bytes()).hexdigest(),
    }
    config_path = config_root / "llm.json"
    config_path.write_text(json.dumps(old_config), encoding="utf-8")
    service = _service(tmp_path, [])
    service._tested_digest = service._digest(
        "https://opencode.ai/zen/go/v1",
        "deepseek-v4-flash",
        "new-fixture-key",
    )

    def interrupted(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("synthetic interrupted config commit")

    monkeypatch.setattr("original_client_setup_api._atomic_json", interrupted)
    with pytest.raises(LLMSetupError, match="LLM_SETUP_SAVE_FAILED"):
        service.save(
            {
                "base_url": "https://opencode.ai/zen/go/v1",
                "model": "deepseek-v4-flash",
                "api_key": "new-fixture-key",
            }
        )

    assert json.loads(config_path.read_text(encoding="utf-8")) == old_config
    assert old_key.read_text(encoding="utf-8") == "old-ciphertext\n"
