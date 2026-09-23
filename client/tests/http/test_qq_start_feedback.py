import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.http.test_personal_chat_setup import _Server


def test_login_timeout_is_visible_and_survives_status_refresh(tmp_path, monkeypatch):
    from runtime.personal_chat import setup, napcat_installer as installer
    monkeypatch.setattr(installer, 'find_shell', lambda root: tmp_path)
    monkeypatch.setattr(installer, 'onebot_available', lambda: False)
    monkeypatch.setattr(installer, 'webui_available', lambda root: False)
    monkeypatch.setattr(installer, 'open_login_page', lambda root: False)
    monkeypatch.setattr(setup, '_selected_channels', lambda server: {'qq'})
    async def no_sleep(_):
        pass
    monkeypatch.setattr(setup.asyncio, 'sleep', no_sleep)
    app = web.Application()
    server = _Server(tmp_path)
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]
    runtime['napcat_state'] = 'STARTING'
    async def scenario():
        async with TestClient(TestServer(app)) as client:
            await setup._open_napcat_login(server, runtime)
            response = await client.get(setup.STATUS_PATH, headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE})
            status = (await response.json())['napcat']
            assert status['state'] == 'FAILED'
            assert status['error'] == 'NAPCAT_START_TIMEOUT'
    asyncio.run(scenario())


def test_live_service_with_exited_old_handle_is_available_without_browser(tmp_path, monkeypatch):
    from runtime.personal_chat import setup, napcat_installer as installer
    monkeypatch.setattr(installer, 'open_login_page', lambda root: (_ for _ in ()).throw(AssertionError('browser not needed')))
    monkeypatch.setattr(installer, 'webui_available', lambda root: True)
    async def no_sleep(_):
        pass
    monkeypatch.setattr(setup.asyncio, 'sleep', no_sleep)
    runtime = {'napcat_state': 'STARTING', 'napcat_shell_process': SimpleNamespace(poll=lambda: 1)}
    asyncio.run(setup._open_napcat_login(_Server(tmp_path), runtime))
    assert runtime['napcat_state'] == 'AWAITING_QQ_LOGIN'
    assert 'napcat_error' not in runtime


def test_inline_login_is_guarded_and_hides_raw_credentials(tmp_path, monkeypatch):
    from runtime.personal_chat import setup, napcat_installer as installer
    monkeypatch.setattr(setup, '_selected_channels', lambda server: {'qq'})
    calls = []
    def login(root, *, refresh=False):
        calls.append(refresh)
        return dict(logged_in=False, scanned=False, verification_required=False,
                    qr='https://qq.com/synthetic-login', token='must-not-expose')
    monkeypatch.setattr(installer, 'login_status', login)
    app = web.Application()
    setup.install_setup_routes(app, _Server(tmp_path))
    async def scenario():
        async with TestClient(TestServer(app)) as client:
            denied = await client.get(setup.NAPCAT_LOGIN_PATH)
            assert denied.status == 403
            assert calls == []
            for method in ('get', 'post'):
                response = await getattr(client, method)(setup.NAPCAT_LOGIN_PATH,
                    headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE})
                assert response.status == 200
                data = await response.json()
                assert data['qr_data'].startswith('data:image/svg+xml;base64,')
                assert 'token' not in data and 'qr' not in data
                assert response.headers['Cache-Control'] == 'no-store'
            assert calls == [False, True]
    asyncio.run(scenario())


def test_existing_webui_does_not_spawn_another_process(tmp_path, monkeypatch):
    from runtime.personal_chat import napcat_installer as installer
    monkeypatch.setattr(installer, 'prepare_onebot', lambda root: None)
    monkeypatch.setattr(installer, '_onebot_port_open', lambda: False)
    monkeypatch.setattr(installer, 'webui_available', lambda root: True)
    monkeypatch.setattr(installer, 'launch_shell', lambda root: (_ for _ in ()).throw(AssertionError('duplicate launch')))
    assert installer.ensure_shell(tmp_path) is None


def test_browser_open_failure_is_actionable(tmp_path, monkeypatch):
    from runtime.personal_chat import setup, napcat_installer as installer
    monkeypatch.setattr(setup, '_selected_channels', lambda server: {'qq'})
    monkeypatch.setattr(installer, 'open_login_page', lambda root: False)
    app = web.Application()
    setup.install_setup_routes(app, _Server(tmp_path))
    async def scenario():
        async with TestClient(TestServer(app)) as client:
            response = await client.post(setup.NAPCAT_BROWSER_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE})
            assert response.status == 503
            assert (await response.json())['error'] == 'NAPCAT_LOGIN_OPEN_FAILED'
    asyncio.run(scenario())


def test_reenter_with_live_service_clears_exited_process_handle(tmp_path, monkeypatch):
    from runtime.personal_chat import setup, napcat_installer as installer
    async def login_ready(server, runtime):
        return None
    monkeypatch.setattr(setup, '_open_napcat_login', login_ready)
    monkeypatch.setattr(setup, '_selected_channels', lambda server: {'qq'})
    monkeypatch.setattr(installer, 'onebot_available', lambda: False)
    monkeypatch.setattr(installer, 'webui_available', lambda root: True)
    monkeypatch.setattr(installer, 'ensure_shell', lambda root: None)
    app = web.Application()
    setup.install_setup_routes(app, _Server(tmp_path))
    runtime = app[setup._SETUP]
    runtime['napcat_shell_process'] = SimpleNamespace(poll=lambda: 1)
    async def scenario():
        async with TestClient(TestServer(app)) as client:
            response = await client.post(setup.NAPCAT_START_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE})
            assert response.status == 202
            assert (await response.json())['status'] == 'STARTING'
            await runtime['napcat_login_task']
            assert runtime['napcat_shell_process'] is None
            assert runtime['napcat_state'] == 'AWAITING_QQ_LOGIN'
    asyncio.run(scenario())


def test_repeated_start_while_login_pending_does_not_spawn_again(tmp_path, monkeypatch):
    from runtime.personal_chat import setup, napcat_installer as installer
    monkeypatch.setattr(setup, '_selected_channels', lambda server: {'qq'})
    monkeypatch.setattr(installer, 'onebot_available', lambda: False)
    monkeypatch.setattr(installer, 'webui_available', lambda root: False)
    calls = []
    monkeypatch.setattr(installer, 'ensure_shell', lambda root: calls.append(1) or SimpleNamespace(poll=lambda: None))
    async def waiting_login(server, runtime):
        await asyncio.Event().wait()
    monkeypatch.setattr(setup, '_open_napcat_login', waiting_login)
    app = web.Application()
    setup.install_setup_routes(app, _Server(tmp_path))
    async def scenario():
        async with TestClient(TestServer(app)) as client:
            responses = await asyncio.gather(*[client.post(setup.NAPCAT_START_PATH,
                headers={setup.CONFIRM_HEADER: setup.CONFIRM_VALUE}, json={}) for _ in range(2)])
            assert all(response.status == 202 for response in responses)
            assert calls == [1]
    asyncio.run(scenario())
