import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, ProviderProtocolError


@pytest.mark.parametrize('shape', ['choices', 'delta', 'blocks'])
@pytest.mark.parametrize('parts', [
    ['第一段。', '\n', '\n', '第二段。'],
    ['Hello', ' ', 'world.', '\n\n  ', '缩进段。'],
    ['第一段。\n', '\n第二段。'],
])
def test_stream_preserves_whitespace_independent_of_provider_chunk_boundaries(shape, parts):
    assert asyncio.run(_stream(parts, shape)) == ''.join(parts)


def test_whitespace_only_stream_still_fails_closed():
    with pytest.raises(ProviderProtocolError):
        asyncio.run(_stream([' ', '\n\n'], 'choices'))


async def _stream(parts, shape):
    async def handler(request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        for part in parts:
            delta = {'content': [{'type': 'text', 'text': part}]} if shape == 'blocks' else {'content': part}
            event = {'delta': delta} if shape == 'delta' else {'choices': [{'delta': delta}]}
            await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
        await response.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        await response.write_eof()
        return response
    app = web.Application()
    app.router.add_post('/v1/chat/completions', handler)
    async with TestClient(TestServer(app)) as client:
        gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
            base_url=str(client.make_url('/v1')), model='synthetic', stream=True, max_retries=0))
        return ''.join([delta.text async for delta in gateway.stream([{'role':'user','content':'synthetic'}])])
