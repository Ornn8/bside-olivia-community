import asyncio
import json
import pytest
from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter, ProviderProtocolError

SCHEMA = {'type': 'object', 'properties': {'mode': {'type': 'string', 'enum': ['voice']}},
          'required': ['mode'], 'additionalProperties': False}
FORMAT = {'type': 'json_schema', 'name': 'route', 'strict': True, 'schema': SCHEMA}
TOOL = {'type': 'function', 'function': {'name': 'route', 'parameters': SCHEMA}}
MESSAGES = [{'role': 'user', 'content': 'synthetic frozen letter'}]

def adapter(monkeypatch, replies, *, model='synthetic', capabilities=None, style='chat_completions'):
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://example.test/v1', model=model, api_style=style,
        provider_options={'capabilities': capabilities or {}}))
    seen = []
    async def post(body, request, **kwargs):
        seen.append(body)
        return replies[min(len(seen)-1, len(replies)-1)]
    monkeypatch.setattr(gateway, '_post_json', post)
    return gateway, seen

def response(text, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': text}}]}

@pytest.mark.parametrize('model,caps', [('qwen3.8-flash-2026-09-01', {}), ('vendor-alias', {'thinking':'qwen'})])
def test_tool_policy_uses_capabilities_not_exact_names(monkeypatch, model, caps):
    reply = {'choices': [{'message': {'tool_calls': [{'function': {'name': 'route', 'arguments': '{"mode":"voice"}'}}]}}]}
    gateway, seen = adapter(monkeypatch, [reply], model=model, capabilities=caps)
    asyncio.run(gateway.complete_with_tools(messages=MESSAGES, tools=[TOOL], tool_choice='required'))
    assert seen[0]['enable_thinking'] is False

@pytest.mark.parametrize('caps,expected', [({'json_schema': True}, 'json_schema'), ({}, 'json_object'),
                                       ({'json_mode':False}, None)])
def test_structured_capabilities_and_one_validation_repair(monkeypatch, caps, expected):
    gateway, seen = adapter(monkeypatch, [response('{"mode":"bad"}'), response('```json\n{"mode":"voice"}\n```')], capabilities=caps)
    result = asyncio.run(gateway.complete_structured_scoped(MESSAGES, response_format=FORMAT,
        scope=GatewayRequestScope.PERSONAL_CHAT_JSON))
    assert json.loads(result.text) == {'mode':'voice'}
    assert len(seen) == 2
    assert seen[0].get('response_format', {}).get('type') == expected
    assert 'synthetic frozen letter' in json.dumps(seen[1])

def test_single_tool_can_use_json_when_tools_are_unavailable(monkeypatch):
    gateway, seen = adapter(monkeypatch, [response('{"mode":"voice"}')], capabilities={'tools':False})
    calls = asyncio.run(gateway.complete_with_tools(messages=MESSAGES, tools=[TOOL], tool_choice='required'))
    assert calls[0].name == 'route' and calls[0].arguments == {'mode':'voice'}
    assert 'tools' not in seen[0]

def test_missing_tool_uses_one_validated_json_fallback(monkeypatch):
    gateway, seen = adapter(monkeypatch, [response('not a tool call'), response('{"mode":"voice"}')])
    calls = asyncio.run(gateway.complete_with_tools(messages=MESSAGES, tools=[TOOL], tool_choice='required'))
    assert calls[0].arguments == {'mode':'voice'} and len(seen) == 2

def test_truncated_tool_call_is_never_accepted_or_repaired(monkeypatch):
    reply = {'choices': [{'finish_reason':'length', 'message': {'tool_calls': [{'function': {'name':'route','arguments':'{"mode":"voice"}'}}]}}]}
    gateway, seen = adapter(monkeypatch, [reply])
    with pytest.raises(ProviderProtocolError):
        asyncio.run(gateway.complete_with_tools(messages=MESSAGES, tools=[TOOL], tool_choice='required'))
    assert len(seen) == 1

def test_invalid_structured_output_stops_after_one_repair(monkeypatch):
    gateway, seen = adapter(monkeypatch, [response('{"mode":"bad"}')])
    with pytest.raises(ProviderProtocolError):
        asyncio.run(gateway.complete_structured_scoped(MESSAGES, response_format=FORMAT,
            scope=GatewayRequestScope.PERSONAL_CHAT_JSON))
    assert len(seen) == 2


@pytest.mark.parametrize('parameter', ['tools', 'tool_choice', 'response_format'])
def test_explicit_unsupported_parameter_has_one_http_fallback(parameter):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    async def exercise():
        seen = []
        async def handle(request):
            body = await request.json()
            seen.append(body)
            if len(seen) == 1:
                return web.json_response({'error':{'message':parameter + ' is not supported. private-marker'}}, status=400)
            return web.json_response(response('{"mode":"voice"}'))
        app = web.Application()
        app.router.add_post('/chat/completions', handle)
        async with TestServer(app) as server:
            gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
                base_url=str(server.make_url('')).rstrip('/'), model='synthetic', requires_api_key=False))
            if parameter == 'response_format':
                result = await gateway.complete_structured_scoped(MESSAGES, response_format=FORMAT,
                    scope=GatewayRequestScope.PERSONAL_CHAT_JSON)
                assert json.loads(result.text) == {'mode':'voice'}
            else:
                calls = await gateway.complete_with_tools(messages=MESSAGES, tools=[TOOL], tool_choice='required')
                assert calls[0].arguments == {'mode':'voice'}
        assert len(seen) == 2 and parameter not in seen[1]
        assert 'private-marker' not in json.dumps(seen)
    asyncio.run(exercise())


@pytest.mark.parametrize('status', [400, 401, 403])
def test_rejections_without_capability_evidence_are_not_retried(status):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from llm_gateway import ProviderRejected
    async def exercise():
        seen = []
        async def handle(request):
            seen.append(await request.json())
            return web.json_response({'error':{'message':'invalid request private-marker'}}, status=status)
        app = web.Application()
        app.router.add_post('/chat/completions', handle)
        async with TestServer(app) as server:
            gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
                base_url=str(server.make_url('')).rstrip('/'), model='synthetic', requires_api_key=False))
            with pytest.raises(ProviderRejected) as failure:
                await gateway.complete_with_tools(messages=MESSAGES, tools=[TOOL], tool_choice='required')
            from runtime.diagnostics.failure_context import exception_context
            assert exception_context(failure.value)['failure_stage'] == 'http_response'
            assert 'private-marker' not in repr(vars(failure.value))
        assert len(seen) == 1
    asyncio.run(exercise())


def test_schema_wire_format_for_responses_api(monkeypatch):
    gateway, seen = adapter(monkeypatch, [{'output':[{'type':'message','content':[{'type':'output_text','text':'{"mode":"voice"}'}]}]}],
        capabilities={'json_schema':True}, style='responses')
    result = asyncio.run(gateway.complete_structured_scoped(MESSAGES, response_format=FORMAT,
        scope=GatewayRequestScope.PERSONAL_CHAT_JSON))
    assert json.loads(result.text) == {'mode':'voice'}
    assert seen[0]['text']['format'] == FORMAT


def test_schema_failure_has_content_free_diagnostic_stage(monkeypatch):
    from runtime.diagnostics.failure_context import exception_context
    gateway, seen = adapter(monkeypatch, [response('{"mode":"private-marker"}')])
    with pytest.raises(ProviderProtocolError) as failure:
        asyncio.run(gateway.complete_structured_scoped(MESSAGES, response_format=FORMAT,
            scope=GatewayRequestScope.PERSONAL_CHAT_JSON))
    context = exception_context(failure.value)
    assert context['failure_stage'] == 'structured_validation'
    assert context['failure_detail'] == 'structured_validation_failed'
    assert 'private-marker' not in repr(context)
