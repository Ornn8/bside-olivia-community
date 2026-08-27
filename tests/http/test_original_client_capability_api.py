from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator

from original_client_capability_api import (
    ACTION_PATH,
    ERROR_HTTP_STATUSES,
    PUBLIC_ROUTE_CONTRACT,
    STATUS_PATH,
    mount_original_client_capability_api,
)


TRUSTED_ORIGIN = "https://client.example"
ROOT = Path(__file__).parents[2]


class _Status:
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "olivia.capability-status.v2",
            "status": "READY",
            "capability": "long_term_memory",
            "state": "missing",
            "phase": "idle",
            "downloaded_bytes": 0,
            "total_bytes": 100,
            "remaining_bytes": 100,
            "installed_bytes": 0,
            "install_locations": [
                "runtime/mem0-site-packages",
                "data/memory/model-cache",
            ],
            "version": "fixture-v1",
            "license_summary": "fixture",
            "requires_gpu": False,
        }


class _Installer:
    def __init__(self, *, start_result: str = "APPLIED") -> None:
        self.starts: list[tuple[str, Path | None, bool]] = []
        self.start_result = start_result
        self.pauses = 0
        self.resumes: list[str] = []
        self.uninstalls: list[bool] = []

    def status(self) -> _Status:
        return _Status()

    def start(self, *, source_mode, offline_root=None, cleanup_offline=False) -> str:
        self.starts.append((source_mode, offline_root, cleanup_offline))
        return self.start_result

    def pause(self) -> str:
        self.pauses += 1
        return "APPLIED"

    def resume(self, *, source_mode) -> str:
        self.resumes.append(source_mode)
        return "APPLIED"

    def uninstall(self, *, remove_model) -> str:
        self.uninstalls.append(remove_model)
        return "APPLIED"


def test_capability_public_contract_matches_routes_authorization_and_schema() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "mem0_capability_api_contract.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT / "contracts" / "mem0_capability_api_contract.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(schema).validate(contract)
    assert set(contract["routes"]) == {STATUS_PATH, ACTION_PATH}
    assert contract["routes"] == PUBLIC_ROUTE_CONTRACT
    assert {
        code: details["http_statuses"]
        for code, details in contract["error_codes"].items()
    } == ERROR_HTTP_STATUSES
    assert contract["authorization"] == {
        "origins": "explicit-trusted-https-or-loopback-only",
        "login_required_for_mutations": True,
        "session_header": "X-Olivia-Setup-Session",
        "confirmation_header": {
            "name": "X-Olivia-Capability-Action",
            "value": "confirmed",
        },
    }


def test_capability_api_requires_explicit_action_and_exposes_bounded_status(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        installer = _Installer()
        app = web.Application()
        mount_original_client_capability_api(
            app,
            installer,
            trusted_origins=(TRUSTED_ORIGIN,),
            authorize_session=lambda value: (
                None if value == "signed-in-session" else (_ for _ in ()).throw(PermissionError())
            ),
        )
        async with TestClient(TestServer(app)) as client:
            status = await client.get(
                "/toy/capabilities/mem0",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert status.status == 200
            payload = await status.json()
            assert payload["state"] == "missing"
            assert "path" not in str(payload).casefold()
            assert installer.starts == []

            forbidden = await client.post(
                "/toy/capabilities/mem0/action",
                headers={"Origin": TRUSTED_ORIGIN, "Content-Type": "application/json"},
                json={"action": "install", "source": "auto"},
            )
            assert forbidden.status == 403
            assert installer.starts == []

            confirmed = {
                "Origin": TRUSTED_ORIGIN,
                "Content-Type": "application/json",
                "X-Olivia-Capability-Action": "confirmed",
                "X-Olivia-Setup-Session": "signed-in-session",
            }
            install = await client.post(
                "/toy/capabilities/mem0/action",
                headers=confirmed,
                json={"action": "install", "source": "auto"},
            )
            assert install.status == 200
            assert await install.json() == {"status": "APPLIED"}
            assert installer.starts == [("auto", None, False)]

            await client.post(
                "/toy/capabilities/mem0/action",
                headers=confirmed,
                json={"action": "pause"},
            )
            await client.post(
                "/toy/capabilities/mem0/action",
                headers=confirmed,
                json={"action": "resume", "source": "official"},
            )
            await client.post(
                "/toy/capabilities/mem0/action",
                headers=confirmed,
                json={"action": "uninstall", "remove_model": False},
            )
            assert installer.pauses == 1
            assert installer.resumes == ["official"]
            assert installer.uninstalls == [False]

            attack = await client.get(
                "/toy/capabilities/mem0",
                headers={"Origin": "https://attacker.example"},
            )
            assert attack.status == 403

    asyncio.run(scenario())


def test_capability_api_requires_login_session_and_has_no_offline_import_route(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        installer = _Installer()
        app = web.Application()
        mount_original_client_capability_api(
            app,
            installer,
            trusted_origins=(TRUSTED_ORIGIN,),
            authorize_session=lambda value: (
                None if value == "signed-in-session" else (_ for _ in ()).throw(PermissionError())
            ),
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/capabilities/mem0/action",
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "X-Olivia-Capability-Action": "confirmed",
                    "Content-Type": "application/json",
                },
                json={"action": "install", "source": "auto"},
            )
            assert response.status == 403
            assert installer.starts == []
            offline = await client.post(
                "/toy/capabilities/mem0/import",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert offline.status == 404

    asyncio.run(scenario())


def test_capability_status_work_does_not_block_sibling_routes() -> None:
    class BlockingInstaller(_Installer):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def status(self) -> _Status:
            self.entered.set()
            assert self.release.wait(timeout=2)
            return _Status()

    async def scenario() -> None:
        installer = BlockingInstaller()
        app = web.Application()
        async def ping(_request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        app.router.add_get("/ping", ping)
        mount_original_client_capability_api(
            app,
            installer,
            trusted_origins=(TRUSTED_ORIGIN,),
            authorize_session=lambda _value: None,
        )
        async with TestClient(TestServer(app)) as client:
            timer = threading.Timer(0.5, installer.release.set)
            timer.start()
            started = time.perf_counter()
            pending = asyncio.create_task(
                client.get(STATUS_PATH, headers={"Origin": TRUSTED_ORIGIN})
            )
            try:
                assert await asyncio.to_thread(installer.entered.wait, 1)
                ping = await client.get("/ping")
                assert ping.status == 200
                assert time.perf_counter() - started < 0.3
            finally:
                installer.release.set()
                timer.cancel()
            response = await pending
            assert response.status == 200

    asyncio.run(scenario())


def test_capability_installer_exceptions_use_stable_contract_codes() -> None:
    class FailingInstaller(_Installer):
        def status(self) -> _Status:
            raise RuntimeError("private status detail")

        def start(self, *, source_mode, offline_root=None, cleanup_offline=False) -> str:
            raise RuntimeError("private action detail")

    async def scenario() -> None:
        app = web.Application()
        mount_original_client_capability_api(
            app,
            FailingInstaller(),
            trusted_origins=(TRUSTED_ORIGIN,),
            authorize_session=lambda _value: None,
        )
        async with TestClient(TestServer(app)) as client:
            failed_status = await client.get(
                STATUS_PATH, headers={"Origin": TRUSTED_ORIGIN}
            )
            assert failed_status.status == 503
            assert await failed_status.json() == {
                "status": "FAILED",
                "error_code": "CAPABILITY_STATUS_UNAVAILABLE",
            }
            failed_action = await client.post(
                ACTION_PATH,
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    "X-Olivia-Capability-Action": "confirmed",
                    "X-Olivia-Setup-Session": "signed-in-session",
                },
                json={"action": "install", "source": "auto"},
            )
            assert failed_action.status == 503
            assert await failed_action.json() == {
                "status": "FAILED",
                "error_code": "CAPABILITY_ACTION_UNAVAILABLE",
            }

    asyncio.run(scenario())
