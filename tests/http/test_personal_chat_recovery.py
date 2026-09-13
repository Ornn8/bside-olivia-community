"""A transient generation failure must not stop the real transport loop."""
import asyncio
import json
from types import SimpleNamespace

from aiohttp import web


def test_generation_timeout_retries_then_receives_next_wechat_message(tmp_path, monkeypatch):
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
    calls = []
    async def read(*args):
        if calls.count('poll') >= 2:
            await args[-1].wait()
            return None
        calls.append('poll')
        return {'msgs': [{'message_id': str(calls.count('poll')), 'from_user_id': 'owner', 'to_user_id': 'bot',
            'message_type': 1, 'message_state': 2, 'context_token': 'synthetic',
            'item_list': [{'type': 1, 'text_item': {'text': '今天有点累'}}]}], 'get_updates_buf': 'next'}
    async def generate(*args):
        calls.append('generate')
        if calls.count('generate') == 1:
            raise asyncio.TimeoutError()
        return '慢慢说，我听着呢。'
    async def request(*args, **kwargs):
        calls.append('send')
        return {}
    async def commit(*args):
        calls.append('commit')
    monkeypatch.setattr(wechat, 'wechat_request', request)
    monkeypatch.setattr(backend, 'commit', commit)
    monkeypatch.setattr(wechat, '_read_until_stopped', read)
    monkeypatch.setattr(backend, 'generate', generate)
    # The real transport is used, with only the burst delay disabled.
    run_wechat = wechat.run_wechat
    async def transport(credentials, handler, stop, **kwargs):
        return await run_wechat(credentials, handler, stop, merge_seconds=0, **kwargs)
    monkeypatch.setattr(wechat, 'run_wechat', transport)
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda *args, **kwargs: None)
    async def run():
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        for _ in range(200):
            await asyncio.sleep(.01)
            if calls.count('send') == 2:
                break
        assert app[backend._RUNTIME]['status']['wechat'] == 'LISTENING'
        assert calls.count('generate') == 3 and calls.count('send') == 2
        assert [r['delivery_status'] for r in server.store.personal_chats] == ['DELIVERED', 'DELIVERED']
        assert [r['generation_attempts'] for r in server.store.personal_chats] == [2, 1]
        await runner.cleanup()
    asyncio.run(run())
