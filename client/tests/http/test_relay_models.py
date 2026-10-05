import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from original_client_setup_api import LLMSetupService, mount_original_client_setup_api


@pytest.mark.parametrize('baseline_input,baseline_output,expected_default,expected_opus', [
    ('4', '8', ('0.25000', '0.12500'), ('5.84000', '4.32500')),
    ('2', '5', ('0.50000', '0.20000'), ('11.68000', '6.92000')),
    ('0', '8', None, None),
    (None, None, None, None),
])
def test_select_model_keeps_encrypted_key_and_runtime_choice_across_reconnect(
        tmp_path, monkeypatch, baseline_input, baseline_output, expected_default, expected_opus):
    import original_client_relay_api as relay
    calls, applied = [], []
    async def remote(base, key, method, path, payload=None):
        calls.append((method, path))
        assert key == 'olivia-synthetic-private-key'
        rows = [
            {'id': 'qwen3.7-flash', 'display_name': 'Qwen3.8 Flash', 'actual_model': 'qwen3.7-flash', 'input_multiplier': '1', 'output_multiplier': '1'},
            {'id': 'claude-opus-5-5', 'display_name': 'Claude Opus 5.5', 'input_multiplier': '23.36', 'output_multiplier': '34.60'}]
        if baseline_input is not None:
            rows.append({'id': 'gemini-3.8-flash', 'display_name': 'Gemini 3.8 Flash',
                'input_multiplier': baseline_input, 'output_multiplier': baseline_output})
        return {'data': rows}
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
            if expected_default is None:
                assert response.status == 503
                assert service._config().model == 'gemini-3.8-flash'
                return
            assert response.status == 200
            catalog = await response.json()
            assert catalog['selected_model'] == 'gemini-3.8-flash'
            assert catalog['baseline_model'] == 'gemini-3.8-flash'
            rows = {row['id']: row for row in catalog['models']}
            assert 'actual_model' not in rows['qwen3.7-flash']
            for model, expected in [('qwen3.7-flash', expected_default),
                    ('claude-opus-5-5', expected_opus), ('gemini-3.8-flash', ('1.00000', '1.00000'))]:
                assert (rows[model]['input_multiplier'], rows[model]['output_multiplier']) == expected
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'connect'})
            assert response.status == 200
            assert service._config().model == 'gemini-3.8-flash'
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'select_model', 'model': 'qwen3.7-flash'})
            assert response.status == 200
            response = await client.post('/toy/relay/action', headers=headers, json={'action': 'connect'})
            assert response.status == 200
            assert service._config().model == 'qwen3.7-flash'
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
