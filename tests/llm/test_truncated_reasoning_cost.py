import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, ProviderProtocolError


@pytest.mark.parametrize('content', ['', 'partial reply'])
def test_stream_length_never_retries_or_publishes_even_after_reasoning(content):
    async def exercise():
        calls = []
        async def handler(request):
            calls.append(await request.json())
            payload = {'choices': [{'delta': {'reasoning_content': 'synthetic thinking', 'content': content},
                                    'finish_reason': 'length'}]}
            return web.Response(text='data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n',
                                content_type='text/event-stream')
        app = web.Application()
        app.router.add_post('/chat/completions', handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(GatewayConfig(
                provider='openai_compatible', base_url=str(client.make_url('')).rstrip('/'),
                model='synthetic', stream=True, max_retries=2, retry_backoff_seconds=0,
            ), key_resolver=lambda: 'synthetic-local-only')
            published = []
            with pytest.raises(ProviderProtocolError) as error:
                async for delta in adapter.stream([{'role': 'user', 'content': 'synthetic video letter'}]):
                    published.append(delta.text)
            assert error.value.retryable is False
            assert not published
            assert len(calls) == 1
    asyncio.run(exercise())
