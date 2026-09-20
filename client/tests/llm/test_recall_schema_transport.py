import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter


@pytest.mark.parametrize('style', ['chat_completions', 'responses'])
@pytest.mark.parametrize('caps', [{'json_schema': True}, {'json_schema': False},
                                  {'json_schema': False, 'json_mode': False}])
def test_recall_source_schema_reaches_http_without_an_extra_generation(style, caps):
    async def exercise():
        schema = {'type': 'object', 'properties': {'source': {'enum': ['s0', 'current']}},
                  'required': ['source'], 'additionalProperties': False}
        format_ = {'type': 'json_schema', 'name': 'recall_check', 'strict': False, 'schema': schema}
        messages = [{'role': 'system', 'content': 'Interpret evidence once.'},
                    {'role': 'user', 'content': 'synthetic current question'}]
        # Invalid source is intentionally returned: the recall caller owns partial
        # evidence validation. Transport must not start another semantic generation.
        returned = '{"source":"bad-source"}'
        seen = []
        async def handle(request):
            seen.append(await request.json())
            if style == 'responses':
                return web.json_response({'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': returned}]}]})
            return web.json_response({'choices': [{'finish_reason': 'stop',
                                                   'message': {'content': returned}}]})
        app = web.Application()
        app.router.add_post('/chat/completions', handle)
        app.router.add_post('/responses', handle)
        async with TestServer(app) as server:
            gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
                base_url=str(server.make_url('')).rstrip('/'), model='synthetic',
                api_style=style, requires_api_key=False, max_retries=0,
                provider_options={'capabilities': caps}))
            result = await gateway.complete_structured_scoped(messages, response_format=format_,
                scope=GatewayRequestScope.RECALL_CHECK)
        assert result.text == returned
        assert len(seen) == 1
        body = seen[0]
        prompt = body['input' if style == 'responses' else 'messages']
        assert prompt[0] == messages[0] and prompt[-1] == messages[-1]
        wire = body.get('text', {}).get('format') if style == 'responses' else body.get('response_format')
        if caps.get('json_schema'):
            spec = wire if style == 'responses' else wire['json_schema']
            assert spec['schema'] == schema
            assert prompt == messages
        else:
            assert json.dumps(schema, ensure_ascii=False, separators=(',', ':')) in prompt[-2]['content']
            assert wire == ({'type': 'json_object'} if caps.get('json_mode', True) else None)
        assert messages[-1]['content'] == 'synthetic current question'
    asyncio.run(exercise())


@pytest.mark.parametrize('status', ['incomplete', 'failed'])
def test_incomplete_structured_response_is_not_returned_as_usable_evidence(monkeypatch, status):
    from llm_gateway import ProviderProtocolError
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://example.test/v1', model='synthetic', api_style='responses'))
    seen = []
    async def post(body, request, **kwargs):
        seen.append(body)
        return {'status': status, 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': '{"findings":[]}'}]}]}
    monkeypatch.setattr(gateway, '_post_json', post)
    with pytest.raises(ProviderProtocolError):
        asyncio.run(gateway.complete_structured_scoped([{'role': 'user', 'content': 'Evidence'}],
            response_format={'type': 'json_object'}, scope=GatewayRequestScope.RECALL_CHECK))
    assert len(seen) == 1
