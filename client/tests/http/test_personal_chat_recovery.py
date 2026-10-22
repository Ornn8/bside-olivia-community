"""WeChat is retired (2026-10-22): a binding saved before stays on disk but its transport never starts."""
import asyncio
import json
from types import SimpleNamespace

from aiohttp import web


def test_a_wechat_only_binding_starts_no_transport(tmp_path, monkeypatch):
    import local_server, original_client_setup_api
    from runtime.personal_chat import backend, wechat
    folder = tmp_path / 'personal-chat'
    folder.mkdir()
    secret = folder / 'credentials.dpapi'
    secret.write_text(json.dumps(dict(account='bot', owner='owner', token='synthetic', base='https://ilinkai.weixin.qq.com')))
    (folder / 'config.json').write_text(json.dumps({'wechat': {'credentials_file': str(secret)}}))
    (folder / 'existing-access.json').write_text(json.dumps({'channels': ['wechat']}))
    monkeypatch.delenv('OLIVIA_PERSONAL_CHAT_CONFIG', raising=False)
    monkeypatch.setattr(original_client_setup_api, '_dpapi_unprotect', lambda value: value)
    started = []

    async def transport(credentials, handler, stop, **kwargs):
        started.append('wechat')
        await stop.wait()
    monkeypatch.setattr(wechat, 'run_wechat', transport)
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda *args, **kwargs: None)

    async def run():
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        await asyncio.sleep(.05)
        assert started == [] and backend.selected_channels(server) == set()
        assert app.get(backend._RUNTIME) is None
        await runner.cleanup()
    asyncio.run(run())
    assert (folder / 'config.json').is_file() and secret.is_file()  # nothing the user saved is deleted
