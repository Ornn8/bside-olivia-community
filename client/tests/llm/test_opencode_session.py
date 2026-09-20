import asyncio
import json
from uuid import UUID

import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter


@pytest.mark.parametrize('style', ['chat_completions', 'responses'])
def test_session_survives_turns_scopes_streaming_and_retries(monkeypatch, style):
    async def exercise():
        seen = []
        async def handle(request):
            seen.append(dict(request.headers))
            if len(seen) == 1:
                return web.json_response({}, status=503)
            body = await request.json()
            if body.get('stream'):
                event = ({'type': 'response.output_text.delta', 'delta': 'OK'} if style == 'responses'
                         else {'choices': [{'delta': {'content': 'OK'}}]})
                stop = ({'type': 'response.completed'} if style == 'responses'
                        else {'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
                return web.Response(text='data: ' + json.dumps(event) + '\n\ndata: ' + json.dumps(stop) + '\n\ndata: [DONE]\n\n',
                                    content_type='text/event-stream')
            return web.json_response({'output_text': 'OK'} if style == 'responses' else
                                     {'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]})
        app = web.Application()
        app.router.add_post('/', handle)
        async with TestServer(app) as server:
            config = GatewayConfig(provider='openai_compatible', base_url='https://opencode.ai/zen/go/v1',
                                   model='synthetic', api_style=style, stream=True,
                                   max_retries=1, retry_backoff_seconds=0)
            gateway = OpenAICompatibleAdapter(config, key_resolver=lambda: 'synthetic-key')
            monkeypatch.setattr(gateway, '_url', lambda: str(server.make_url('/')))
            messages = ({'role': 'user', 'content': 'synthetic'},)
            assert (await gateway.complete(messages, request_id='letter-reply:first')).text == 'OK'
            assert (await gateway.complete_scoped(messages, request_id='recall:second',
                     scope=GatewayRequestScope.RECALL_CHECK)).text == 'OK'
            assert ''.join([d.text async for d in gateway.stream(messages, request_id='chat:third')]) == 'OK'
        sessions = [item.get('x-opencode-session') for item in seen]
        assert len(seen) == 4 and None not in sessions and len(set(sessions)) == 1
        UUID(sessions[0])
        assert sessions[0] not in {'synthetic-key', 'letter-reply:first', 'chat:third'}
        other = OpenAICompatibleAdapter(config)
        assert other._headers(None, 'different')['x-opencode-session'] != sessions[0]
        assert seen[0]['Idempotency-Key'] == seen[1]['Idempotency-Key'] == 'letter-reply:first'
        assert all(item['User-Agent'].startswith('Olivia-') for item in seen)
    asyncio.run(exercise())


@pytest.mark.parametrize('base', ['https://example.test/v1', 'https://opencode.ai.example.test/zen/go/v1',
                                 'https://opencode.ai/zen/v1', 'https://opencode.ai/zen/go/v10'])
def test_session_header_is_not_added_to_other_providers(base):
    gateway = OpenAICompatibleAdapter(GatewayConfig(base_url=base))
    assert 'x-opencode-session' not in gateway._headers(None, 'synthetic')


def test_connection_probe_sends_session_header(monkeypatch):
    import original_client_setup_api as setup
    async def exercise():
        seen = []
        async def handle(request):
            seen.append(dict(request.headers))
            return web.json_response({'choices': []})
        app = web.Application()
        app.router.add_post('/', handle)
        async with TestServer(app) as server:
            async with ClientSession() as local:
                class LocalSession:
                    def __init__(self, **kwargs): pass
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): pass
                    def post(self, url, **kwargs):
                        return local.post(str(server.make_url('/')), **kwargs)
                monkeypatch.setattr(setup, 'ClientSession', LocalSession)
                await setup._probe_openai_compatible('https://opencode.ai/zen/go/v1', 'synthetic', 'synthetic-key')
                await setup._probe_openai_compatible('https://example.test/v1', 'synthetic', 'synthetic-key')
        UUID(seen[0]['x-opencode-session'])
        assert 'x-opencode-session' not in seen[1]
    asyncio.run(exercise())
