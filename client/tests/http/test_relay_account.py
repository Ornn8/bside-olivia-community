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
            pending_data=await pending.json()
            assert pending_data['configured'] is False
            assert pending_data['registration_pending'] is True
            assert pending_data['key_prefix'] == ''
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


def test_relay_loads_bundled_roots_when_system_store_is_empty(monkeypatch):
    import ssl
    import original_client_relay_api as relay
    empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(ssl, 'create_default_context', lambda: empty)
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, *args, **kwargs):
            context = kwargs.get('ssl')
            assert isinstance(context, ssl.SSLContext)
            assert context.cert_store_stats()['x509_ca'] > 100
            assert context.check_hostname
            assert context.verify_mode == ssl.CERT_REQUIRED
            raise TimeoutError()
    monkeypatch.setattr(relay, 'ClientSession', Session)
    async def scenario():
        try:
            await relay.relay_request(relay.RELAY_BASE, 'olivia-synthetic', 'POST', '/accounts', {})
        except LLMSetupError as error:
            assert error.code == 'RELAY_TIMEOUT'
    asyncio.run(scenario())


def test_relay_transport_failures_are_distinguishable_without_secret_leaks(monkeypatch):
    import original_client_relay_api as relay
    from aiohttp import ClientSSLError, ClientConnectionError
    failures = [(TimeoutError('private endpoint'), 'RELAY_TIMEOUT'),
                (ClientSSLError(None, OSError('private certificate')), 'RELAY_TLS_FAILED'),
                (ClientConnectionError('private endpoint'), 'RELAY_CONNECTION_FAILED'),
                (ValueError('private response'), 'RELAY_RESPONSE_INVALID')]
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, *args, **kwargs): raise failure
    monkeypatch.setattr(relay, 'ClientSession', Session)
    async def scenario():
        nonlocal failure
        for failure, expected in failures:
            try:
                await relay.relay_request(relay.RELAY_BASE,'olivia-synthetic','POST','/accounts',{})
            except LLMSetupError as error:
                assert error.code == expected
                assert 'private' not in str(error)
            else:
                raise AssertionError('failure was swallowed')
    failure = None
    asyncio.run(scenario())
