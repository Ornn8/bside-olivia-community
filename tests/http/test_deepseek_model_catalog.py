import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import original_client_setup_api as api


@pytest.mark.parametrize('body,status,expected', [
    ({'data': [{'id': 'deepseek-v4-pro'}, {'id': 'deepseek-v4-flash'}, {'id': 'deepseek-v4-pro'}]}, 200, ['deepseek-v4-pro', 'deepseek-v4-flash']),
    ({'data': []}, 200, None),
    ({'data': [{'id': '<unsafe>'}]}, 200, None),
    ({'error': 'synthetic-private-provider-message'}, 401, None),
])
def test_models_uses_get_and_sanitizes_failure(body, status, expected):
    async def run():
        app = web.Application()
        async def listing(request):
            assert request.method == 'GET'
            assert request.headers['Authorization'] == 'Bearer synthetic-key'
            return web.json_response(body, status=status)
        app.router.add_get('/models', listing)
        async with TestServer(app) as server:
            if expected is not None:
                assert await api._list_models(str(server.make_url('')).rstrip('/'), 'synthetic-key') == expected
            else:
                with pytest.raises(api.LLMSetupError) as error:
                    await api._list_models(str(server.make_url('')).rstrip('/'), 'synthetic-key')
                assert error.value.code == 'LLM_SETUP_CONNECTION_FAILED'
                assert 'synthetic-private' not in str(error.value)
    asyncio.run(run())


def test_catalog_requires_session_reuses_key_across_models_and_never_returns_it(tmp_path, monkeypatch):
    async def run():
        async def probe(*args):
            pass
        service = api.LLMSetupService(tmp_path, probe=probe, protect=lambda _: 'cipher', unprotect=lambda _: 'synthetic-key')
        config = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-v4-pro', 'api_key': 'synthetic-key'}
        await service.test(config)
        service.save(config)
        calls = []
        async def listing(base, key):
            calls.append((base, key))
            return ['deepseek-v4-pro', 'deepseek-v4-flash']
        monkeypatch.setattr(api, '_list_models', listing)
        app = web.Application()
        api.mount_original_client_setup_api(app, service, trusted_origins=('https://client.example',))
        config.update(model='deepseek-v4-flash', api_key='')
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': 'https://client.example', api.CONFIRM_HEADER: api.CONFIRM_VALUE}
            assert (await client.post(api.LLM_MODELS_PATH, json=config, headers=headers)).status == 403
            headers[api.SESSION_HEADER] = service.status()['session_token']
            response = await client.post(api.LLM_MODELS_PATH, json=config, headers=headers)
            assert response.status == 200
            assert await response.json() == {'status': 'AVAILABLE', 'models': ['deepseek-v4-pro', 'deepseek-v4-flash']}
            assert calls == [('https://api.deepseek.com', 'synthetic-key')]
            await service.test(config)
    asyncio.run(run())
