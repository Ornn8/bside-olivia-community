from __future__ import annotations

import asyncio
import hashlib
import zipfile
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


@pytest.mark.parametrize('variant', ['valid', 'bad_hash', 'missing', 'extra', 'traversal', 'duplicate', 'symlink', 'oversized_digest'])
def test_update_bundle_applies_offline_only_after_validation(tmp_path, monkeypatch, variant):
    import original_client_update_api as api
    package = tmp_path / 'renamed.zip'
    patch = b'synthetic inner patch'
    digest = hashlib.sha256(patch).hexdigest()
    with zipfile.ZipFile(package, 'w') as z:
        entry = zipfile.ZipInfo('../test.oliviapatch' if variant == 'traversal' else 'test.oliviapatch')
        if variant == 'symlink':
            entry.external_attr = 0o120777 << 16
        z.writestr(entry, patch)
        if variant != 'missing':
            z.writestr('test.oliviapatch.manifest.sha256', 'a' * (257 if variant == 'oversized_digest' else 64) + '\n')
        z.writestr('test.oliviapatch.sha256', ('b' * 64 if variant == 'bad_hash' else digest) + '\n')
        if variant == 'extra':
            z.writestr('unexpected.txt', 'private')
        if variant == 'duplicate':
            with pytest.warns(UserWarning):
                z.writestr('test.oliviapatch', patch)
    monkeypatch.setattr(api, '_official_manifest_digest', lambda _: pytest.fail('bundle must work offline'))
    seen = []
    class Updater(_Updater):
        def apply(self, path, manifest_sha256):
            assert path.read_bytes() == patch
            assert manifest_sha256 == 'a' * 64
            seen.append(path)
            return super().apply(path, manifest_sha256)
    async def scenario():
        app = web.Application()
        mount_original_client_update_api(app, Updater(), trusted_origins=(TRUSTED_ORIGIN,),
                                        authorize_session=lambda _: None)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(ACTION_PATH, headers={'Origin': TRUSTED_ORIGIN, CONFIRM_HEADER: 'confirmed'},
                json={'action': 'apply_verified', 'package_path': str(package)})
            assert response.status == (200 if variant == 'valid' else 409)
            assert response.headers['Access-Control-Allow-Origin'] == TRUSTED_ORIGIN
            assert len(seen) == (1 if variant == 'valid' else 0)
    asyncio.run(scenario())
    assert all(not path.exists() for path in seen)


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


@pytest.mark.parametrize('origin', ['https://untrusted.example', 'null', ''])
def test_update_errors_do_not_grant_untrusted_origins_access(origin):
    async def scenario():
        updater = _Updater()
        app = web.Application()
        mount_original_client_update_api(app, updater, trusted_origins=(TRUSTED_ORIGIN,),
                                         authorize_session=lambda _: None)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(ACTION_PATH,
                headers={'Origin': origin, CONFIRM_HEADER: 'confirmed'}, json={'action': 'rollback'})
            assert response.status == 403
            assert 'Access-Control-Allow-Origin' not in response.headers
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


@pytest.mark.parametrize("failure", [False, True])
def test_verified_apply_uses_official_digest_and_fails_closed(tmp_path, monkeypatch, failure):
    import original_client_update_api as api

    package = tmp_path / "renamed.oliviapatch"
    package.write_bytes(b"fixture")
    def resolve(path):
        assert path == package.resolve()
        if failure:
            raise api.UpdateAPIError("UPDATE_CHECKSUM_UNAVAILABLE", status=503)
        return "b" * 64
    monkeypatch.setattr(api, "_official_manifest_digest", resolve)

    async def scenario():
        updater = _Updater()
        app = web.Application()
        mount_original_client_update_api(app, updater, trusted_origins=(TRUSTED_ORIGIN,),
                                         authorize_session=lambda value: None)
        headers = {"Origin": TRUSTED_ORIGIN, CONFIRM_HEADER: "confirmed"}
        async with TestClient(TestServer(app)) as client:
            response = await client.post(ACTION_PATH, headers=headers,
                json={"action": "apply_verified", "package_path": str(package)})
            assert response.status == (503 if failure else 200)
            assert response.headers.get('Access-Control-Allow-Origin') == TRUSTED_ORIGIN
            result = await response.json()
            assert result["status"] == ("FAILED" if failure else "APPLIED")
            if failure:
                assert result["error_code"] == "UPDATE_CHECKSUM_UNAVAILABLE"
        assert updater.applied == ([] if failure else [(package.resolve(), "b" * 64)])
    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["1.2.3", "../../evil", "https://evil.example", None])
def test_official_checksum_uses_only_fixed_release_origin(tmp_path, monkeypatch, version):
    import io
    import zipfile
    import original_client_update_api as api
    package = tmp_path / "renamed.oliviapatch"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"version": version}))
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            return io.BytesIO(("c" * 64 + "\n").encode())
    monkeypatch.setattr(api, "build_opener", lambda *args: Opener())
    if version == "1.2.3":
        assert api._official_manifest_digest(package) == "c" * 64
        assert calls == ["https://github.com/Ornn8/bside-olivia-community/releases/download/v1.2.3/Olivia-1.2.3.oliviapatch.manifest.sha256"]
    else:
        with pytest.raises(api.UpdateAPIError):
            api._official_manifest_digest(package)
        assert calls == []


@pytest.mark.parametrize("body", [b"<html>not found</html>", b"a" * 65, b"a" * 64 + b" " * 200, b"\xff"])
def test_official_checksum_rejects_invalid_response(tmp_path, monkeypatch, body):
    import io
    import zipfile
    import original_client_update_api as api
    package = tmp_path / "local.oliviapatch"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"version": "1.2.3"}))
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(body)
    monkeypatch.setattr(api, "build_opener", lambda *args: Opener())
    with pytest.raises(api.UpdateAPIError, match="UPDATE_CHECKSUM_UNAVAILABLE"):
        api._official_manifest_digest(package)


@pytest.mark.parametrize("url", ["http://github.com/file", "https://evil.example/file",
                                 "https://github.com@evil.example/file", "https://github.com:444/file"])
def test_checksum_redirect_rejects_untrusted_destinations(url):
    import original_client_update_api as api
    with pytest.raises(api.UpdateAPIError, match="UPDATE_CHECKSUM_UNAVAILABLE"):
        api._ReleaseRedirects().redirect_request(None, None, 302, "", {}, url)
