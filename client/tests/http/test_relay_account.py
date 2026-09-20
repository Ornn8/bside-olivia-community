import asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from original_client_setup_api import LLMSetupService, mount_original_client_setup_api, LLMSetupError


def test_claim_retry_reuses_encrypted_key_and_connects(tmp_path, monkeypatch):
    import original_client_relay_api as relay
    calls = []
    async def remote(base, key, method, path, payload=None):
        calls.append(key)
        if len(calls) == 1:
            raise LLMSetupError('RELAY_UNAVAILABLE', status=503)
        return {'registered':True}
    async def probe(*args):
        pass
    monkeypatch.setattr(relay,'relay_request',remote)
    service = LLMSetupService(tmp_path, protect=lambda key:'encrypted:'+key[::-1], unprotect=lambda value:value.removeprefix('encrypted:')[::-1], probe=probe)
    service.observe_login(success=True)
    async def scenario():
        app=web.Application();mount_original_client_setup_api(app,service,trusted_origins=('https://client.example',))
        headers={'Origin':'https://client.example','X-Olivia-Setup-Action':'confirmed','X-Olivia-Setup-Session':service._session_token}
        async with TestClient(TestServer(app)) as client:
            first=await client.post('/toy/relay/action',headers=headers,json={'action':'claim'})
            assert first.status == 503
            pending=await client.post('/toy/relay/action',headers=headers,json={'action':'account'})
            assert (await pending.json())['configured'] is False
            second=await client.post('/toy/relay/action',headers=headers,json={'action':'claim'})
            assert second.status == 200
            assert calls[0] == calls[1]
            assert calls[0] not in await second.text()
            assert calls[0] not in (tmp_path/'config/olivia_relay_key.dpapi').read_text()
            account=await client.post('/toy/relay/action',headers=headers,json={'action':'account'})
            assert (await account.json())['connected'] is False
            connected=await client.post('/toy/relay/action',headers=headers,json={'action':'connect'})
            assert connected.status == 200
            exported=await client.post('/toy/relay/action',headers=headers,json={'action':'export_key'})
            assert (await exported.json())['key'] == calls[0]
    asyncio.run(scenario())
