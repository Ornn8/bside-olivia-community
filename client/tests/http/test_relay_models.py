import asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from original_client_setup_api import LLMSetupService, mount_original_client_setup_api


def test_select_model_keeps_encrypted_key_and_runtime_choice_across_reconnect(tmp_path, monkeypatch):
    import original_client_relay_api as relay
    calls, applied = [], []
    async def remote(base, key, method, path, payload=None):
        calls.append((method, path))
        assert key == 'olivia-synthetic-private-key'
        return {'data': [
            {'id': 'qwen3.7-flash', 'display_name': 'Qwen3.8 Flash', 'actual_model': 'qwen3.7-flash', 'input_multiplier': '1', 'output_multiplier': '1'},
            {'id': 'claude-opus-5-5', 'display_name': 'Claude Opus 5.5', 'input_multiplier': '23.36', 'output_multiplier': '34.60'}]}
    monkeypatch.setattr(relay, 'relay_request', remote)
    service = LLMSetupService(tmp_path, protect=lambda key: 'cipher:'+key,
        unprotect=lambda cipher: cipher.removeprefix('cipher:'), apply_runtime=lambda config, key: applied.append((config.model, key)))
    service.observe_login(success=True)
    service._config_root.mkdir(parents=True)
    (service._config_root/'olivia_relay_key.dpapi').write_text('cipher:olivia-synthetic-private-key')
    async def scenario():
        app = web.Application()
        mount_original_client_setup_api(app, service, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': 'https://client.example', 'X-Olivia-Setup-Action': 'confirmed', 'X-Olivia-Setup-Session': service._session_token}
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'models'})
            assert response.status == 200
            assert (await response.json())['selected_model'] == 'qwen3.7-flash'
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'select_model', 'model': 'claude-opus-5-5'})
            assert response.status == 200
            assert (await response.json())['selected_model'] == 'claude-opus-5-5'
            assert service._config().model == 'claude-opus-5-5'
            assert applied[-1] == ('claude-opus-5-5', 'olivia-synthetic-private-key')
            assert 'private-key' not in await response.text()
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'connect'})
            assert response.status == 200
            assert service._config().model == 'claude-opus-5-5'
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'account'})
            assert (await response.json())['connected'] is True
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'select_model', 'model': 'unlisted-expensive-model'})
            assert response.status == 400
            assert service._config().model == 'claude-opus-5-5'
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'select_model', 'model': 'claude-sonnet-5-5'})
            assert response.status == 503
            assert service._config().model == 'claude-opus-5-5'
    asyncio.run(scenario())
    assert not any(path == '/chat/completions' for method, path in calls)
