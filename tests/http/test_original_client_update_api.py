from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator

from original_client_update_api import (
    ACTION_PATH,
    CONFIRM_HEADER,
    SESSION_HEADER,
    mount_original_client_update_api,
)


TRUSTED_ORIGIN = "https://client.example"
ROOT = Path(__file__).parents[2]


class _Updater:
    def __init__(self) -> None:
        self.applied: list[tuple[Path, str]] = []
        self.rollbacks = 0

    def apply(self, package: Path, manifest_sha256: str) -> dict[str, object]:
        self.applied.append((package, manifest_sha256))
        return {"status": "APPLIED", "component": "local_backend", "version": "1.2.3"}

    def rollback(self) -> dict[str, object]:
        self.rollbacks += 1
        return {"status": "ROLLED_BACK", "component": "local_backend", "version": "1.2.2"}


def test_update_api_requires_login_confirmation_and_applies_or_rolls_back(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        package = tmp_path / "olivia-1.2.3.oliviapatch"
        package.write_bytes(b"fixture")
        updater = _Updater()
        app = web.Application()
        mount_original_client_update_api(
            app,
            updater,
            trusted_origins=(TRUSTED_ORIGIN,),
            select_patch=lambda: package,
            authorize_session=lambda value: (
                None
                if value == "signed-in-session"
                else (_ for _ in ()).throw(PermissionError())
            ),
        )
        async with TestClient(TestServer(app)) as client:
            body = {
                "action": "apply",
                "package_path": str(package),
                "manifest_sha256": "a" * 64,
            }
            rejected = await client.post(
                ACTION_PATH,
                headers={"Origin": TRUSTED_ORIGIN},
                json=body,
            )
            assert rejected.status == 403
            assert updater.applied == []

            headers = {
                "Origin": TRUSTED_ORIGIN,
                CONFIRM_HEADER: "confirmed",
                SESSION_HEADER: "signed-in-session",
            }
            selected = await client.post(
                ACTION_PATH,
                headers=headers,
                json={"action": "select"},
            )
            assert selected.status == 200
            assert await selected.json() == {
                "status": "SELECTED",
                "package_path": str(package.resolve()),
                "restart_required": False,
            }
            applied = await client.post(ACTION_PATH, headers=headers, json=body)
            assert applied.status == 200
            assert await applied.json() == {
                "status": "APPLIED",
                "component": "local_backend",
                "version": "1.2.3",
                "restart_required": True,
            }
            assert updater.applied == [(package.resolve(), "a" * 64)]

            rolled_back = await client.post(
                ACTION_PATH,
                headers=headers,
                json={"action": "rollback"},
            )
            assert rolled_back.status == 200
            assert (await rolled_back.json())["status"] == "ROLLED_BACK"
            assert updater.rollbacks == 1

    asyncio.run(scenario())


def test_update_api_contract_matches_its_schema() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "local_update_api_contract.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT / "contracts" / "local_update_api_contract.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert not list(Draft202012Validator(schema).iter_errors(contract))
    route = contract["routes"][ACTION_PATH]
    assert route["actions"]["select"]["response_fields"] == [
        "status",
        "package_path",
        "restart_required",
    ]
    assert route["actions"]["apply"]["restart_required"] is True
    assert route["actions"]["rollback"]["restart_required"] is True
