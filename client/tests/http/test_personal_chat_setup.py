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
        self.persist_calls = 0

    def _state_root(self) -> Path:
        return self.root

    def _persist_store_state(self) -> None:
        self.persist_calls += 1


def test_qq_start_returns_while_dependencies_are_preparing(tmp_path, monkeypatch):
    import threading
    from runtime.personal_chat import setup, napcat_installer
    release = threading.Event()
    calls = []
    def prepare(_root):
        calls.append(1)
        release.wait(5)
        raise napcat_installer.NapCatSetupError('NAPCAT_DEPENDENCY_REPAIR_FAILED')
    monkeypatch.setattr(setup, '_selected_channels', lambda _server: {'qq'})
    monkeypatch.setattr(napcat_installer, 'ensure_shell', prepare)
    async def scenario():
        app = web.Application()
        setup.install_setup_routes(app, _Server(tmp_path))
        async with TestClient(TestServer(app)) as client:
            headers = {setup.CONFIRM_HEADER: setup.CONFIRM_VALUE}
            try:
                response = await asyncio.wait_for(client.post(setup.NAPCAT_START_PATH, headers=headers, json={}), 1)
                assert response.status == 202
                again = await client.post(setup.NAPCAT_START_PATH, headers=headers, json={})
                assert again.status == 202
                assert len(calls) == 1
            finally:
                release.set()
                task = app[setup._SETUP].get('napcat_login_task')
                if task:
                    await task
            assert app[setup._SETUP]['napcat_error'] == 'NAPCAT_DEPENDENCY_REPAIR_FAILED'
    asyncio.run(scenario())


@pytest.mark.parametrize('initial,additional', [('wechat', 'qq'), ('qq', 'wechat')])
def test_selected_channel_can_add_other_channel_without_losing_binding(tmp_path, initial, additional):
    from runtime.personal_chat import setup
    server = _Server(tmp_path)
    server.store.letters = [dict(letter_id='invite', origin='proactive', proactive_kind='contact_invitation',
        letter_status='COMPLETED', published_at=1, contact_setup_choice=initial),
        dict(letter_id='reply', letter_status='COMPLETED', published_at=2,
             contact_invitation_id='invite', content=initial, contact_choice={'choice': initial, 'quote': initial})]
    assert setup._selected_channels(server) == {initial}
    assert setup._store_setup_choice(server, additional) == ['qq', 'wechat']
    assert setup._selected_channels(server) == {'qq', 'wechat'}
    assert setup._selected_channels(_Server(tmp_path)) == {'qq', 'wechat'}
    assert setup._store_setup_choice(server, additional) == ['qq', 'wechat']
    # Narrative/model-derived choices cannot roll back explicit settings.
    server.store.letters.append(dict(letter_id='later-reply', letter_status='COMPLETED',
        published_at=9999999999,
        contact_invitation_id='invite', content='later',
        contact_choice={'choice': 'later', 'quote': 'later'}))
    assert setup._selected_channels(server) == {'qq', 'wechat'}


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
            assert body["napcat"]["managed"] is True

    asyncio.run(scenario())


def test_completed_invitation_can_choose_channel_directly_in_settings(tmp_path: Path) -> None:
    from runtime.personal_chat import setup

    server = _Server(tmp_path)
    server.store.letters.append(
        {
            "letter_id": "invite-1",
            "origin": "proactive",
            "proactive_kind": "contact_invitation",
            "letter_status": "COMPLETED",
            "content": "要不要交换联系方式？",
            "created_at": 1,
            "published_at": 1,
        }
    )
    app = web.Application()
    setup.install_setup_routes(app, server)
    headers = {setup.CONFIRM_HEADER: setup.CONFIRM_VALUE}

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            before = await client.get(setup.STATUS_PATH, headers=headers)
            assert before.status == 200
            assert (await before.json())["contact_state"] == "available"

            selected = await client.post(
                setup.CHANNEL_CHOICE_PATH,
                headers=headers,
                json={"choice": "wechat"},
            )
            assert selected.status == 200
            assert (await selected.json())["channels"] == ["wechat"]

            after = await client.get(setup.STATUS_PATH, headers=headers)
            body = await after.json()
            assert body["contact_state"] == "wechat"
            assert body["selected_channels"] == ["wechat"]

    asyncio.run(scenario())
    assert setup._selected_channels(_Server(tmp_path)) == {'wechat'}


def test_fresh_user_can_choose_both_without_relationship_or_invitation(tmp_path):
    from runtime.personal_chat import setup
    server = _Server(tmp_path)
    assert setup._contact_access(server)['state'] == 'available'
    assert setup._store_setup_choice(server, 'both') == ['qq', 'wechat']
    restarted = _Server(tmp_path)
    assert setup._selected_channels(restarted) == {'qq', 'wechat'}
    assert setup._contact_access(restarted)['state'] == 'both'
    assert not restarted.store.letters


def test_existing_bindings_survive_missing_invitation_after_upgrade(tmp_path):
    from runtime.personal_chat import setup
    folder = tmp_path / 'personal-chat'
    folder.mkdir()
    (folder / 'config.json').write_text(json.dumps({'qq': {}, 'wechat': {}}))
    assert setup._selected_channels(_Server(tmp_path)) == {'qq', 'wechat'}


def test_qq_setup_tests_loopback_and_never_persists_plaintext_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import original_client_setup_api
    from runtime.personal_chat import setup

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_selected_channels", lambda _server: {"qq"})

    async def fake_probe(url: str, token: str, account: str | None = None) -> str:
        assert url == "ws://127.0.0.1:3001"
        assert token == "synthetic-token-123456"
        assert account == "123456789"
        return "123456789"

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
    assert config["qq"]["managed"] is False
    assert config["qq"]["credentials_file"].endswith("qq.dpapi")


@pytest.mark.parametrize('cached_state', ['ONEBOT_READY', 'ONEBOT_PROBING', 'IDLE'])
def test_managed_qq_only_needs_owner_after_napcat_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached_state
) -> None:
    import original_client_setup_api
    from runtime.personal_chat import napcat_installer, setup

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_selected_channels", lambda _server: {"qq"})
    monkeypatch.setattr(
        napcat_installer,
        "managed_connection",
        lambda _root: ("ws://127.0.0.1:3001", "managed-synthetic-token-123456"),
    )

    async def fake_probe(url: str, token: str, account: str | None = None) -> str:
        assert url == "ws://127.0.0.1:3001"
        assert token == "managed-synthetic-token-123456"
        assert account == (None if cached_state == 'IDLE' else "123456789")
        return "123456789"

    monkeypatch.setattr(setup, "_qq_probe", fake_probe)
    monkeypatch.setattr(napcat_installer, "account_config_ready", lambda _root, _account: True)
    monkeypatch.setattr(original_client_setup_api, "_dpapi_protect", lambda _value: "managed-ciphertext")
    app = web.Application()
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]
    runtime["napcat_state"] = cached_state
    runtime["napcat_account"] = None if cached_state == 'IDLE' else "123456789"

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                setup.QQ_CONFIGURE_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE},
                json={"managed": True, "owner": "987654321"},
            )
            assert response.status == 200
            assert (await response.json())["status"] == "READY_RESTART"

    asyncio.run(scenario())
    config = json.loads((tmp_path / "personal-chat" / "config.json").read_text(encoding="utf-8"))
    assert config["qq"]["managed"] is True
    assert config["qq"]["account"] == "123456789"
    assert config["qq"]["url"] == "ws://127.0.0.1:3001"
    assert "managed-synthetic-token-123456" not in json.dumps(config)


@pytest.mark.parametrize('live_error', [None, 'QQ_ACCOUNT_MISMATCH', 'QQ_LOGIN_UNAVAILABLE', 'NAPCAT_ONEBOT_CONFIG_PENDING'])
def test_manual_binding_rechecks_after_background_timeout(tmp_path, monkeypatch, live_error):
    import original_client_setup_api
    from runtime.personal_chat import setup, napcat_installer
    calls = []
    monkeypatch.setattr(setup, '_selected_channels', lambda server: {'qq'})
    monkeypatch.setattr(napcat_installer, 'find_shell', lambda root: Path('synthetic'))
    monkeypatch.setattr(napcat_installer, 'onebot_available', lambda: True)
    monkeypatch.setattr(napcat_installer, 'managed_connection', lambda root: ('ws://127.0.0.1:3001', 'synthetic-token'))
    monkeypatch.setattr(napcat_installer, 'account_config_ready',
                        lambda *args: len(calls) < 3 or live_error != 'NAPCAT_ONEBOT_CONFIG_PENDING')
    monkeypatch.setattr(original_client_setup_api, '_dpapi_protect', lambda value: 'ciphertext')
    async def probe(url, token, expected=None):
        calls.append(expected)
        if len(calls) == 2:
            raise asyncio.TimeoutError()
        if len(calls) == 3:
            assert expected == '123456789'
            if live_error in {'QQ_ACCOUNT_MISMATCH', 'QQ_LOGIN_UNAVAILABLE'}:
                raise RuntimeError(live_error)
        return '123456789'
    monkeypatch.setattr(setup, '_qq_probe', probe)
    async def scenario():
        app = web.Application()
        setup.install_setup_routes(app, _Server(tmp_path))
        headers = {setup.CONFIRM_HEADER: setup.CONFIRM_VALUE}
        async with TestClient(TestServer(app)) as client:
            await client.get(setup.STATUS_PATH, headers=headers)
            assert app[setup._SETUP]['napcat_state'] == 'ONEBOT_READY'
            await client.get(setup.STATUS_PATH, headers=headers)
            assert app[setup._SETUP]['napcat_state'] == 'ONEBOT_PROBING'
            response = await client.post(setup.QQ_CONFIGURE_PATH, headers=headers,
                                         json={'managed': True, 'owner': '987654321'})
            body = await response.json()
            assert len(calls) == 3
            if live_error:
                assert body['error'] == live_error
                assert not (tmp_path / 'personal-chat/config.json').exists()
            else:
                assert response.status == 200
                assert app[setup._SETUP]['napcat_state'] == 'ONEBOT_READY'
                assert (tmp_path / 'personal-chat/config.json').is_file()
    asyncio.run(scenario())


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

            napcat = await client.post(setup.NAPCAT_INSTALL_PATH, headers=headers, json={})
            assert napcat.status == 409
            assert (await napcat.json())["error"] == "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"

    asyncio.run(scenario())


def test_setup_allows_original_client_cors_preflight_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import setup

    origin = "https://olivia.local"
    server = _Server(tmp_path)
    server.TRUSTED_FRONTEND_ORIGINS = frozenset({origin})
    monkeypatch.setattr(setup, "_selected_channels", lambda _server: {"wechat"})
    app = web.Application()
    setup.install_setup_routes(app, server)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            preflight = await client.options(
                setup.STATUS_PATH,
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": setup.CONFIRM_HEADER,
                },
            )
            assert preflight.status == 204
            assert preflight.headers["Access-Control-Allow-Origin"] == origin
            assert setup.CONFIRM_HEADER in preflight.headers["Access-Control-Allow-Headers"]

            response = await client.get(
                setup.STATUS_PATH,
                headers={"Origin": origin, setup.CONFIRM_HEADER: setup.CONFIRM_VALUE},
            )
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == origin
            assert (await response.json())["selected_channels"] == ["wechat"]

            denied = await client.get(
                setup.STATUS_PATH,
                headers={
                    "Origin": "https://example.invalid",
                    setup.CONFIRM_HEADER: setup.CONFIRM_VALUE,
                },
            )
            assert denied.status == 403
            assert "Access-Control-Allow-Origin" not in denied.headers

    asyncio.run(scenario())


def test_setup_middleware_precedes_existing_original_client_catch_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import setup

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_selected_channels", lambda _server: {"wechat"})
    app = web.Application()

    async def fallback(_request: web.Request) -> web.Response:
        return web.json_response({"fallback": True}, status=418)

    app.router.add_route("*", "/{tail:.*}", fallback)
    setup.install_setup_routes(app, server)

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            response = await client.get(
                setup.STATUS_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE},
            )
            assert response.status == 200
            body = await response.json()
            assert body["selected_channels"] == ["wechat"]
            assert "fallback" not in body

    asyncio.run(scenario())


def test_personal_chat_setup_ui_stays_inside_existing_settings_surface() -> None:
    from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT

    script = PERSONAL_CHAT_SETUP_JAVASCRIPT
    for required in (
        'document.querySelector("[data-olivia-proactive-settings]")',
        'root.dataset.oliviaPersonalChatSetup = "true"',
        '"/toy/personal-chat/setup/status"',
        '"/toy/personal-chat/setup/channel-choice"',
        '"/toy/personal-chat/setup/wechat/start"',
        '"/toy/personal-chat/setup/wechat/verify"',
        '"/toy/personal-chat/setup/qq/configure"',
        '"/toy/personal-chat/setup/qq/napcat/install"',
        '"/toy/personal-chat/setup/qq/napcat/start"',
        'status.wechat?.qr_data',
        '"QQ / 微信聊天"',
        '"QQ（实验功能）"',
        '"一键安装"',
        '"X-Olivia-Companion-Action"',
    ):
        assert required in script
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "window.open", "eval(", "new Function"):
        assert forbidden not in script
