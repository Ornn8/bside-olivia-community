import asyncio
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from original_client_setup_api import LLMSetupService, mount_original_client_setup_api, _probe_openai_compatible


def test_recharge_route_uses_saved_credentials_and_requires_session(tmp_path, monkeypatch):
    import original_client_relay_api as relay
    monkeypatch.setattr(relay, 'RELAY_BASE', 'https://relay.example/v1')
    calls = []
    async def remote(base, key, method, path, payload=None):
        calls.append((base,key,method,path,payload))
        return {'order':{'amount_cents':999,'credit_yuan':'10.00000000','state':'pending'}}
    monkeypatch.setattr(relay,'relay_request',remote)
    service = LLMSetupService(tmp_path,unprotect=lambda value:'olivia-synthetic-private-key')
    cipher=tmp_path/'cipher';cipher.write_text('encrypted')
    monkeypatch.setattr(service,'_config',lambda:SimpleNamespace(model='qwen3.7-flash',base_url='https://relay.example/v1'))
    monkeypatch.setattr(service,'_active_key_path',lambda:cipher)
    service.observe_login(success=True)
    async def scenario():
        app=web.Application();mount_original_client_setup_api(app,service,trusted_origins=('https://client.example',))
        async with TestClient(TestServer(app)) as client:
            headers={'Origin':'https://client.example','X-Olivia-Setup-Action':'confirmed'}
            response=await client.post('/toy/relay/action',headers=headers,json={'action':'create_order','amount_cents':1000})
            assert response.status==403 and not calls
            headers['X-Olivia-Setup-Session']=service._session_token
            response=await client.post('/toy/relay/action',headers=headers,json={'action':'create_order','amount_cents':1000})
            assert response.status==200
            assert (await response.json())['order']['credit_yuan']=='10.00000000'
            assert 'private-key' not in await response.text()
            assert calls==[('https://relay.example/v1','olivia-synthetic-private-key','POST','/payments/orders',{'amount_cents':1000})]
            response=await client.post('/toy/relay/action',headers=headers,json={'action':'create_order','amount_cents':1000,'key':'attacker'})
            assert response.status==400 and len(calls)==1
    asyncio.run(scenario())


def test_zero_balance_connection_probe_does_not_generate_text(monkeypatch):
    import original_client_relay_api as relay
    calls=[]
    async def remote(*args):
        calls.append(args)
        return {'data':[{'id':'qwen3.7-flash'}]}
    monkeypatch.setattr(relay,'relay_request',remote)
    asyncio.run(_probe_openai_compatible('https://relay.example/v1','qwen3.7-flash','olivia-synthetic'))
    assert calls==[('https://relay.example/v1','olivia-synthetic','GET','/models')]
