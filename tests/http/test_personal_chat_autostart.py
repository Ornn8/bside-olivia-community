import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from runtime.personal_chat import backend, qq, wechat


@pytest.mark.parametrize('choice', ['qq', 'wechat', 'declined', 'later', None])
def test_saved_choice_controls_listener_on_each_app_start(tmp_path, monkeypatch, choice):
    import original_client_setup_api
    import local_server
    monkeypatch.delenv('OLIVIA_PERSONAL_CHAT_CONFIG', raising=False)
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    monkeypatch.setattr(original_client_setup_api, '_dpapi_unprotect', lambda x: x)
    folder = tmp_path / 'personal-chat'
    folder.mkdir()
    secret = folder / 'wechat.dpapi'
    secret.write_text(json.dumps({'account': 'bot', 'owner': 'owner'}), encoding='utf-8')
    # Unselected credentials may be missing/broken without blocking selected QQ.
    if choice == 'qq':
        secret.unlink()
    (folder / 'config.json').write_text(json.dumps({
        'wechat': {'credentials_file': str(secret)},
        'qq': {'url': 'ws://127.0.0.1:3001', 'account': '100', 'owner': '200'},
    }), encoding='utf-8')
    (folder / 'existing-access.json').write_text(json.dumps({'channels': ['qq', 'wechat']}), encoding='utf-8')
    letters = [{'letter_id': 'invite', 'origin': 'proactive', 'proactive_kind': 'contact_invitation',
                'letter_status': 'COMPLETED', 'created_at': 1}]
    if choice:
        letters.append({'letter_id': 'choice', 'letter_status': 'COMPLETED', 'created_at': 2,
                        'content': choice, 'contact_invitation_id': 'invite',
                        'contact_choice': {'choice': choice, 'quote': choice}})
    # Roundtrip through disk, like persisted application state.
    saved = json.dumps(letters)

    async def run_once():
        connected = []
        async def listen_qq(url, token, account, owner, handler, stop):
            connected.append('qq')
            await stop.wait()
        async def listen_wechat(credentials, handler, stop, **kwargs):
            connected.append('wechat')
            await stop.wait()
        monkeypatch.setattr(qq, 'run_qq', listen_qq)
        monkeypatch.setattr(wechat, 'run_wechat', listen_wechat)
        store = local_server.Store()
        store.letters = json.loads(saved)
        server = SimpleNamespace(store=store, _state_root=lambda: tmp_path,
            _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
            _safe_log=lambda *a, **k: None,
            _atomic_write_store_file=local_server._atomic_write_store_file)
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        await asyncio.sleep(.02)
        await runner.cleanup()
        assert connected == ([choice] if choice in {'qq', 'wechat'} else [])
    asyncio.run(run_once())
    asyncio.run(run_once())


def test_credentials_do_not_unlock_contacts_and_missing_selected_channel_never_falls_back(tmp_path):
    store = SimpleNamespace(letters=[])
    server = SimpleNamespace(store=store, _state_root=lambda: tmp_path)
    assert backend.selected_channels(server) == set()
    store.letters = [dict(letter_id='invite', origin='proactive', proactive_kind='contact_invitation',
                          letter_status='COMPLETED', created_at=1),
                    dict(letter_id='choice', letter_status='COMPLETED', created_at=2,
                         contact_invitation_id='invite', content='微信',
                         contact_choice={'choice': 'wechat', 'quote': '微信'})]
    assert backend.selected_channels(server) == {'wechat'}
