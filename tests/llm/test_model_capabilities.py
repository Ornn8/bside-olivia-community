import pytest
from runtime.reply.model_capabilities import model_capabilities


def test_unknown_model_uses_no_vendor_extensions():
    caps = model_capabilities('https://example.test/v1', 'vendor/model-new')
    assert caps.reasoning_parameters(True) == {}
    assert caps.tool_choice and caps.json_mode and not caps.stream_usage


@pytest.mark.parametrize('name', ['qwen-flash', 'qwen3.5-plus', 'qwen3.7-flash-2026-09-01'])
def test_qwen_family_uses_its_own_thinking_switch(name):
    caps = model_capabilities('https://example.test/v1', name)
    assert caps.reasoning_parameters(True) == {'enable_thinking': True}
    assert caps.reasoning_parameters(False) == {'enable_thinking': False}


def test_proxy_can_disable_vendor_extensions_and_json_mode():
    caps = model_capabilities('https://proxy.test/v1', 'deepseek-v4-pro',
        {'capabilities': {'thinking': 'none', 'json_mode': False, 'tool_choice': True}})
    assert caps.reasoning_parameters(True) == {}
    assert not caps.json_mode and caps.tool_choice


@pytest.mark.parametrize('overrides', [{'stream_usage':'yes'}, {'extra_body':{}}, {'thinking':'invented'}])
def test_invalid_capabilities_are_rejected(overrides):
    with pytest.raises(ValueError):
        model_capabilities('https://example.test/v1','model',{'capabilities':overrides})


def test_memory_uses_qwen_switch_without_deepseek_parameters():
    from types import SimpleNamespace
    from runtime.memory.mem0_memory import _guard_extraction_client
    seen = []
    completions = SimpleNamespace(create=lambda **kw: seen.append(kw) or SimpleNamespace(choices=[],usage=None))
    provider = SimpleNamespace(client=SimpleNamespace(base_url='https://example.test/v1',chat=SimpleNamespace(completions=completions)))
    _guard_extraction_client(provider, model='qwen-flash')
    completions.create(messages=[],response_format={'type':'json_object'})
    assert seen[0]['extra_body'] == {'enable_thinking':False}
    assert seen[0]['response_format'] == {'type':'json_object'}


def test_custom_capabilities_apply_to_actual_gateway_body():
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, GatewayRequestScope
    config = GatewayConfig(provider='openai_compatible',base_url='https://example.test/v1',model='unlisted-model',
        provider_options={'capabilities':{'json_mode':False}})
    body=OpenAICompatibleAdapter(config)._body([{'role':'user','content':'Return JSON.'}],stream=False,scope=GatewayRequestScope.SONG_CONTENT)
    assert body == {'model':'unlisted-model','messages':[{'role':'user','content':'Return JSON.'}],'stream':False}


@pytest.mark.parametrize('model,expected', [
    ('qwen-flash', {'enable_thinking':False}),
    ('qwen3.5-plus', {'enable_thinking':False}),
    ('deepseek-v4-pro', {'thinking':{'type':'disabled'}}),
    ('custom/unlisted-v1', {}),
])
def test_compatible_http_endpoints_receive_only_matching_parameters(model, expected):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, GatewayRequestScope
    async def exercise():
        seen=[]
        async def complete(request):
            body=await request.json(); seen.append(body)
            for key in ('thinking','enable_thinking'):
                if key in expected: assert body[key]==expected[key]
                else: assert key not in body
            return web.json_response({'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
        app=web.Application(); app.router.add_post('/v1/chat/completions',complete)
        async with TestServer(app) as server:
            adapter=OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',base_url=str(server.make_url('/v1')),model=model))
            response=await adapter.complete_scoped([{'role':'user','content':'Return JSON.'}],scope=GatewayRequestScope.SONG_CONTENT)
            assert response.text=='{"ok":true}' and len(seen)==1
    asyncio.run(exercise())
