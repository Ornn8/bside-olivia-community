"""QQ chat, letters and diary may each use their own model on the Olivia account."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway import GatewayConfig, OpenAICompatibleAdapter
from original_client_relay_api import RELAY_BASE
from runtime import model_routes


@pytest.fixture
def routes(tmp_path):
    model_routes.configure(tmp_path)
    yield tmp_path
    model_routes._path, model_routes._cache = None, (None, {})


def test_saved_routes_apply_only_inside_their_use(routes):
    assert model_routes.load() == {'qq': None, 'letter': None, 'diary': None}
    model_routes.save({'qq': 'qwen3.7-flash', 'letter': None, 'diary': 'claude-opus-5-5'})
    assert model_routes.routed_model(RELAY_BASE, 'claude-sonnet-5-5') == 'claude-sonnet-5-5'  # no use marked
    with model_routes.using('qq'):
        assert model_routes.routed_model(RELAY_BASE, 'claude-sonnet-5-5') == 'qwen3.7-flash'
        assert model_routes.routed_model('https://api.example/v1', 'own-model') == 'own-model'  # own provider untouched
    with model_routes.using('letter'):
        assert model_routes.routed_model(RELAY_BASE, 'claude-sonnet-5-5') == 'claude-sonnet-5-5'  # follows the main model
    with model_routes.using('diary'):
        assert model_routes.routed_model(RELAY_BASE, 'claude-sonnet-5-5') == 'claude-opus-5-5'


@pytest.mark.parametrize('bad', [{'qq': 'unknown-model', 'letter': None, 'diary': None}, {'qq': None}, 'qq'])
def test_invalid_routes_are_rejected(routes, bad):
    with pytest.raises(ValueError):
        model_routes.save(bad)


def test_damaged_file_follows_the_main_model(routes):
    (routes / 'olivia_model_routes.json').write_text('{broken', encoding='utf-8')
    assert model_routes.load() == {'qq': None, 'letter': None, 'diary': None}


def test_request_body_names_the_routed_model_and_threads_inherit_it(routes):
    model_routes.save({'qq': 'claude-haiku-5-5', 'letter': None, 'diary': None})
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', base_url=RELAY_BASE,
                                                    model='claude-sonnet-5-5', feature_enabled=True))
    messages = [{'role': 'user', 'content': '在吗'}]
    assert adapter._body(messages, stream=False)['model'] == 'claude-sonnet-5-5'

    async def scenario():
        with model_routes.using('qq'):
            inline = adapter._body(messages, stream=False)['model']
            threaded = await asyncio.to_thread(lambda: adapter._body(messages, stream=False)['model'])
            task = await asyncio.create_task(asyncio.sleep(0, adapter.config.model))
        return inline, threaded, task
    assert asyncio.run(scenario()) == ('claude-haiku-5-5',) * 3
    assert adapter.config.model == 'claude-sonnet-5-5'


def test_settings_action_saves_and_lists_routes(tmp_path, monkeypatch):
    import original_client_relay_api as relay
    from original_client_setup_api import LLMSetupService, mount_original_client_setup_api

    async def remote(base, key, method, path, payload=None):
        return {'data': [{'id': 'claude-sonnet-5-5', 'display_name': 'Claude Sonnet 5.5', 'input_multiplier': '1', 'output_multiplier': '1'},
                         {'id': 'qwen3.7-flash', 'display_name': 'Qwen', 'input_multiplier': '1', 'output_multiplier': '1'}]}
    monkeypatch.setattr(relay, 'relay_request', remote)
    service = LLMSetupService(tmp_path, protect=lambda key: 'cipher:' + key,
                              unprotect=lambda cipher: cipher.removeprefix('cipher:'), apply_runtime=lambda config, key: None)
    service.observe_login(success=True)
    service._config_root.mkdir(parents=True)
    (service._config_root / 'olivia_relay_key.dpapi').write_text('cipher:olivia-synthetic-private-key')

    async def scenario():
        app = web.Application()
        mount_original_client_setup_api(app, service, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': 'https://client.example', 'X-Olivia-Setup-Action': 'confirmed',
                       'X-Olivia-Setup-Session': service._session_token}
            bad = await client.post('/toy/relay/action', headers=headers,
                                    json={'action': 'select_routes', 'routes': {'qq': 'nope', 'letter': None, 'diary': None}})
            assert bad.status == 400
            saved = await client.post('/toy/relay/action', headers=headers,
                                      json={'action': 'select_routes', 'routes': {'qq': 'qwen3.7-flash', 'letter': None, 'diary': None}})
            assert saved.status == 200
            listed = await (await client.post('/toy/relay/action', headers=headers, json={'action': 'models'})).json()
            assert listed['routes'] == {'qq': 'qwen3.7-flash', 'letter': None, 'diary': None}
    try:
        asyncio.run(scenario())
    finally:
        model_routes._path, model_routes._cache = None, (None, {})
