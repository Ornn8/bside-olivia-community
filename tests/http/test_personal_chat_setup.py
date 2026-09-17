from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest


class _Store:
    def __init__(self) -> None:
        self.letters = []


class _Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = _Store()
        self.private_world_port = None

    def _state_root(self) -> Path:
        return self.root


def test_wechat_qr_is_rendered_locally_and_rejects_foreign_targets() -> None:
    from runtime.personal_chat.setup import _qr_data_url

    value = _qr_data_url("https://liteapp.weixin.qq.com/q/synthetic-login")
    assert value.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(value.split(",", 1)[1])
    assert b"<svg" in svg
    assert b"synthetic-login" not in svg

    with pytest.raises(RuntimeError, match="WECHAT_QR_UNAVAILABLE"):
        _qr_data_url("https://example.invalid/steal-login")


def test_setup_status_requires_explicit_local_action_and_redacts_login_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import setup

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_selected_channels", lambda _server: {"wechat"})
    app = web.Application()
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]
    runtime["wechat"] = {
        "state": "SCAN_REQUIRED",
        "qr_data": "data:image/svg+xml;base64,c3Zn",
        "qrcode": "polling-secret",
        "verify_code": "123456",
    }

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            denied = await client.get(setup.STATUS_PATH)
            assert denied.status == 403

            response = await client.get(
                setup.STATUS_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE},
            )
            assert response.status == 200
            body = await response.json()
            assert body["selected_channels"] == ["wechat"]
            assert body["configured"]["wechat"] is False
            assert body["wechat"]["state"] == "SCAN_REQUIRED"
            assert body["wechat"]["qr_data"].startswith("data:image/svg+xml;base64,")
            assert "qrcode" not in body["wechat"]
            assert "verify_code" not in body["wechat"]

    asyncio.run(scenario())


def test_qq_setup_tests_loopback_and_never_persists_plaintext_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import original_client_setup_api
    from runtime.personal_chat import setup

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_selected_channels", lambda _server: {"qq"})

    async def fake_probe(url: str, token: str, account: str) -> None:
        assert url == "ws://127.0.0.1:3001"
        assert token == "synthetic-token-123456"
        assert account == "123456789"

    monkeypatch.setattr(setup, "_qq_probe", fake_probe)
    monkeypatch.setattr(original_client_setup_api, "_dpapi_protect", lambda _value: "synthetic-ciphertext")

    app = web.Application()
    setup.install_setup_routes(app, server)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                setup.QQ_CONFIGURE_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE},
                json={
                    "account": "123456789",
                    "owner": "987654321",
                    "url": "ws://127.0.0.1:3001",
                    "token": "synthetic-token-123456",
                },
            )
            assert response.status == 200
            assert (await response.json())["status"] == "READY_RESTART"

    asyncio.run(scenario())

    secret = (tmp_path / "personal-chat" / "qq.dpapi").read_text(encoding="utf-8")
    config_text = (tmp_path / "personal-chat" / "config.json").read_text(encoding="utf-8")
    config = json.loads(config_text)
    assert secret == "synthetic-ciphertext"
    assert "synthetic-token-123456" not in secret
    assert "synthetic-token-123456" not in config_text
    assert config["qq"]["account"] == "123456789"
    assert config["qq"]["owner"] == "987654321"
    assert config["qq"]["credentials_file"].endswith("qq.dpapi")


def test_setup_rejects_channels_that_user_has_not_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime.personal_chat import setup

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_selected_channels", lambda _server: set())
    app = web.Application()
    setup.install_setup_routes(app, server)
    headers = {setup.CONFIRM_HEADER: setup.CONFIRM_VALUE}

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            wechat = await client.post(setup.WECHAT_START_PATH, headers=headers, json={})
            assert wechat.status == 409
            assert (await wechat.json())["error"] == "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"

            qq = await client.post(
                setup.QQ_CONFIGURE_PATH,
                headers=headers,
                json={"account": "12345", "owner": "67890", "token": "x" * 16},
            )
            assert qq.status == 409
            assert (await qq.json())["error"] == "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"

    asyncio.run(scenario())


def test_personal_chat_setup_ui_stays_inside_existing_settings_surface() -> None:
    from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT

    script = PERSONAL_CHAT_SETUP_JAVASCRIPT
    for required in (
        'document.querySelector("[data-olivia-proactive-settings]")',
        'root.dataset.oliviaPersonalChatSetup = "true"',
        '"/toy/personal-chat/setup/status"',
        '"/toy/personal-chat/setup/wechat/start"',
        '"/toy/personal-chat/setup/wechat/verify"',
        '"/toy/personal-chat/setup/qq/configure"',
        'status.wechat?.qr_data',
        '"QQ / 微信聊天"',
        '"QQ（实验功能）"',
        '"X-Olivia-Companion-Action"',
    ):
        assert required in script
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "window.open", "eval(", "new Function"):
        assert forbidden not in script
