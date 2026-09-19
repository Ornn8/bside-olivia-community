import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from installer.start_local import _load_llm_environment
from original_client_setup_api import LLMSetupService, LLMSetupError
from llm_gateway import create_gateway, load_gateway_config


def test_keyless_custom_test_save_restart_and_completion(tmp_path):
    async def scenario():
        requests = []

        async def completion(request):
            requests.append((dict(request.headers), await request.json()))
            return web.json_response({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

        app = web.Application()
        app.router.add_post('/v1/chat/completions', completion)
        async with TestServer(app) as server:
            applied = []
            service = LLMSetupService(tmp_path, apply_runtime=lambda c, k: applied.append((c, k)))
            payload = {"base_url": str(server.make_url('/v1')), "model": "local-model", "api_key": ""}
            with pytest.raises(LLMSetupError, match='LLM_SETUP_TEST_REQUIRED'):
                service.save(payload)
            await service.test(payload)
            assert 'Authorization' not in requests[0][0]
            assert service.save(payload)
            assert applied[-1][0].requires_api_key is False
            assert applied[-1][1] == ''
            assert service.complete(skipped=False) is False
            assert not list((tmp_path / 'config').glob('*.dpapi'))
            saved = json.loads((tmp_path / 'config/llm.json').read_text())
            assert saved['requires_api_key'] is False
            env = _load_llm_environment({'OLIVIA_LLM_API_KEY': 'old-secret', 'OPENAI_API_KEY': 'old-secret'}, tmp_path, include_secret=True)
            assert env['OLIVIA_LLM_PROVIDER'] == 'openai_compatible'
            assert env['OLIVIA_LLM_REQUIRES_API_KEY'] == '0'
            assert not env.get('OLIVIA_LLM_API_KEY')
            assert not env.get('OPENAI_API_KEY')
            env['OLIVIA_LLM_STREAM'] = 'false'
            gateway = create_gateway(load_gateway_config(environ=env))
            await gateway.complete([{'role': 'user', 'content': 'A synthetic local letter.'}], request_id='keyless-letter')
            assert 'Authorization' not in requests[-1][0]
            reloaded = LLMSetupService(tmp_path)
            assert reloaded.status()['llm']['key_configured'] is False
            await reloaded.test(payload)

    asyncio.run(scenario())


def test_mem0_sdk_omits_auth_and_ignores_ambient_cloud_key(monkeypatch):
    from types import SimpleNamespace
    import httpx
    from openai import OpenAI
    from runtime.memory import mem0_memory

    monkeypatch.setenv('OPENAI_API_KEY', 'ambient-secret')
    received = []
    class Memory:
        @staticmethod
        def from_config(config):
            llm = config['llm']['config']
            assert llm['api_key'] != ''  # Prevent Mem0's `key or getenv` fallback.
            return SimpleNamespace(llm=SimpleNamespace(client=OpenAI(api_key=llm['api_key'])))
    monkeypatch.setattr(mem0_memory, '_load_product_mem0_module', lambda: SimpleNamespace(Memory=Memory))
    backend = mem0_memory._default_factory({'llm': {'provider': 'openai', 'config': {'api_key': '', 'model': 'local', 'openai_base_url': 'http://127.0.0.1:19001/v1'}}})
    client = backend.llm.client
    hooks = client._client.event_hooks
    client._client.close()
    def respond(request):
        received.append(request)
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': 'local', 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'OK'}}]})
    client._client = httpx.Client(transport=httpx.MockTransport(respond), event_hooks=hooks)
    try:
        client.chat.completions.create(model='local', messages=[{'role': 'user', 'content': 'synthetic memory'}])
        assert received[0].url.host == '127.0.0.1'
        assert 'authorization' not in received[0].headers
    finally:
        client.close()


def test_custom_empty_key_never_reuses_saved_secret(tmp_path):
    async def scenario():
        probes = []
        async def probe(url, model, key):
            probes.append(key)
        service = LLMSetupService(tmp_path, probe=probe, protect=lambda key: 'ciphertext', unprotect=lambda key: 'old-secret')
        payload = {'base_url': 'https://custom.example/v1', 'model': 'local', 'api_key': 'old-secret'}
        await service.test(payload)
        service.save(payload)
        payload['api_key'] = ''
        await service.test(payload)
        service.save(payload)
        assert probes == ['old-secret', '']
        assert not list((tmp_path / 'config').glob('*.dpapi'))
        with pytest.raises(LLMSetupError, match='LLM_SETUP_KEY_REQUIRED'):
            await service.test({'base_url': 'https://api.deepseek.com', 'model': 'deepseek-v4-flash', 'api_key': ''})
    asyncio.run(scenario())
