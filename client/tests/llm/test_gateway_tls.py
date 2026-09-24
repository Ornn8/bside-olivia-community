import asyncio
import ssl

import pytest

from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, ProviderTimeout


@pytest.mark.parametrize('stream', [False, True])
def test_gateway_supplements_empty_system_roots_without_disabling_verification(monkeypatch, stream):
    empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(ssl, 'create_default_context', lambda: empty)
    calls = []

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            context = kwargs.get('ssl')
            assert isinstance(context, ssl.SSLContext)
            assert context.cert_store_stats()['x509_ca'] > 100
            assert context.check_hostname
            assert context.verify_mode == ssl.CERT_REQUIRED
            calls.append(url)
            raise asyncio.TimeoutError()

    monkeypatch.setattr('llm_gateway.aiohttp.ClientSession', Session)
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://example.invalid/v1', model='synthetic', requires_api_key=False,
        stream=stream, max_retries=0))

    async def run():
        with pytest.raises(ProviderTimeout):
            if stream:
                _ = [delta async for delta in adapter.stream([{'role': 'user', 'content': 'test'}])]
            else:
                await adapter.complete([{'role': 'user', 'content': 'test'}])
    asyncio.run(run())
    assert len(calls) == 1
