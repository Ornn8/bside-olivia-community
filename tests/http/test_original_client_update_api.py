from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

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


@pytest.mark.skipif(os.name != "nt", reason="Windows native picker")
@pytest.mark.parametrize("selected", [True, False])
def test_patch_picker_uses_visible_topmost_owner_and_preserves_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: bool,
) -> None:
    import original_client_update_api as api

    patch = tmp_path / "local.oliviapatch"
    patch.write_bytes(b"synthetic patch")

    def run(command, **kwargs):
        script = command[-1]
        assert "$owner.TopMost = $true;" in script
        assert "$owner.ShowInTaskbar = $false;" in script
        assert script.index("$owner.Show();") < script.index("$dialog.ShowDialog($owner)")
        assert "$owner.Activate();" in script
        assert "finally" in script and "$owner.Dispose();" in script and "$dialog.Dispose();" in script
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        return subprocess.CompletedProcess(command, 0, str(patch) if selected else "", "")

    monkeypatch.setattr(api.subprocess, "run", run)
    assert api._select_windows_patch() == (patch.resolve() if selected else None)


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


def test_update_api_selects_applies_and_rolls_back_local_patch(
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
            authorize_session=lambda value: (
                None
                if value == "signed-in-session"
                else (_ for _ in ()).throw(PermissionError())
            ),
            select_patch=lambda: package,
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
            assert (await applied.json())["status"] == "APPLIED"
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


def test_update_api_rejects_non_string_action_values_with_the_public_contract() -> None:
    async def scenario() -> None:
        updater = _Updater()
        app = web.Application()
        mount_original_client_update_api(
            app,
            updater,
            trusted_origins=(TRUSTED_ORIGIN,),
            authorize_session=lambda _value: None,
        )
        headers = {
            "Origin": TRUSTED_ORIGIN,
            CONFIRM_HEADER: "confirmed",
            SESSION_HEADER: "signed-in-session",
        }
        async with TestClient(TestServer(app)) as client:
            for invalid_action in (["apply"], {"name": "apply"}):
                response = await client.post(
                    ACTION_PATH,
                    headers=headers,
                    json={"action": invalid_action},
                )
                assert response.status == 400
                assert await response.json() == {
                    "status": "FAILED",
                    "error_code": "UPDATE_FIELDS_INVALID",
                }
        assert updater.applied == []
        assert updater.rollbacks == 0

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
    assert contract["execution"] == "serialized-apply-rollback-off-event-loop"
    route = contract["routes"][ACTION_PATH]
    assert route["actions"]["select"]["status_values"] == ["SELECTED", "CANCELLED"]
    assert route["actions"]["apply"]["status_values"] == ["APPLIED"]
    assert route["actions"]["rollback"]["restart_required"] is True


def test_release_docs_describe_local_patch_updates() -> None:
    documentation = (ROOT / "docs" / "WINDOWS_FULL_PATCH.md").read_text(
        encoding="utf-8"
    )

    assert "手动下载 `.oliviapatch`" in documentation
    assert "python -m installer apply-update" in documentation
    assert "Manifest SHA-256" in documentation
