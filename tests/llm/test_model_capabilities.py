import pytest
from runtime.reply.model_capabilities import model_capabilities


def test_unknown_model_uses_no_vendor_extensions():
    caps = model_capabilities('https://example.test/v1', 'vendor/model-new')
    assert caps.reasoning_parameters(True) == {}
    assert caps.tool_choice and caps.json_mode and not caps.stream_usage


def test_existing_pro_does_not_silently_switch_to_maximum_reasoning():
    assert not model_capabilities('https://api.deepseek.com', 'deepseek-v4-pro').scoped_reasoning
    assert model_capabilities('https://api.deepseek.com', 'deepseek-v4-pro',
        {'capabilities':{'reasoning_effort':'max'}}).scoped_reasoning


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


def test_capabilities_do_not_embed_task_token_budget():
    caps = model_capabilities('https://example.test/v1', 'qwen3.8-flash')
    assert 'max_completion_tokens' not in caps.reasoning_parameters(True)


@pytest.mark.parametrize('model', ['qwen-flash', 'deepseek-v4-flash'])
def test_memory_capability_overrides_reach_sdk(model):
    from types import SimpleNamespace
    from runtime.memory.mem0_memory import _guard_extraction_client
    seen = []
    completions = SimpleNamespace(create=lambda **kw: seen.append(kw) or SimpleNamespace(choices=[], usage=None))
    provider = SimpleNamespace(client=SimpleNamespace(base_url='https://api.deepseek.com', chat=SimpleNamespace(completions=completions)))
    _guard_extraction_client(provider, model=model,
        provider_options={'capabilities': {'thinking': 'none', 'json_mode': False}})
    completions.create(messages=[], response_format={'type': 'json_object'})
    assert seen == [{'messages': []}]


def test_official_review_respects_json_and_effort_overrides(monkeypatch):
    import asyncio
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, GatewayRequestScope
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://api.deepseek.com', model='deepseek-flash',
        provider_options={'capabilities': {'json_mode': False, 'reasoning_effort': 'low'}}))
    seen = []
    async def post(body, request_id, **kwargs):
        seen.append(body)
        return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}
    monkeypatch.setattr(adapter, '_post_json', post)
    asyncio.run(adapter.complete_structured_scoped([{'role':'user','content':'Return JSON.'}],
        response_format={'type':'json_object'}, scope=GatewayRequestScope.JSON_MAX_REASONING))
    assert seen[0]['reasoning'] == {'effort': 'low'}
    assert 'text' not in seen[0]


@pytest.mark.parametrize('deferred', [False, True])
def test_shared_memory_initialization_preserves_capabilities(tmp_path, monkeypatch, deferred):
    from runtime.memory import local_memory
    captured = []
    monkeypatch.setattr(local_memory, 'create_mem0_adapter', lambda **kw: captured.append(kw) or object())
    monkeypatch.setattr(local_memory, 'DeferredConversationMemoryAdapter', lambda config, factory: factory())
    options = {'capabilities': {'thinking': 'none', 'json_mode': False}}
    local_memory.create_conversation_memory_adapter(
        local_memory.MemoryConfig(enabled=True, provider='mem0', data_root=tmp_path / 'memory'),
        environ={}, llm_fallback={'base_url':'https://proxy.test/v1', 'model':'qwen-flash',
            'provider_options': options}, defer_initialization=deferred)
    assert captured[0]['config'].llm_provider_options == options


@pytest.mark.parametrize('scope_name', ['TEXT_LETTER_MAX_REASONING', 'BACKGROUND_REASONING', 'JSON_MAX_REASONING'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('base_url,model,official', [
    ('https://api.deepseek.com', 'deepseek-v4-flash', True),
    ('https://api.deepseek.com/v1/', 'deepseek-flash', True),
    ('https://api.deepseek.com.example/v1', 'deepseek-flash', False),
    ('https://api.deepseek.com/proxy', 'deepseek-flash', False),
    ('https://opencode.ai/zen/go/v1', 'deepseek-v4-flash', False),
])
def test_flash_task_budget_is_scoped_to_official_letter_and_background(scope_name, stream, base_url, model, official):
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, GatewayRequestScope
    scope = GatewayRequestScope[scope_name]
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', base_url=base_url, model=model))
    body = adapter._body([{'role':'user','content':'Hello'}], stream=stream,
        max_reasoning=adapter._uses_max_reasoning(scope), scope=scope)
    bounded = official and scope_name != 'JSON_MAX_REASONING'
    assert body['reasoning_effort'] == ('high' if bounded else 'max')
    assert body.get('max_tokens') == (10000 if bounded else None)


@pytest.mark.parametrize('scope_name', ['TEXT_LETTER_MAX_REASONING', 'BACKGROUND_REASONING'])
def test_bounded_flash_still_rejects_truncated_reply(monkeypatch, scope_name):
    import asyncio
    from llm_gateway import GatewayConfig, OpenAICompatibleAdapter, GatewayRequestScope, ProviderProtocolError
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://api.deepseek.com', model='deepseek-flash'))
    async def post(body, request_id, **kwargs):
        assert body['reasoning_effort'] == 'high' and body['max_tokens'] == 10000
        return {'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}
    monkeypatch.setattr(adapter, '_post_json', post)
    with pytest.raises(ProviderProtocolError):
        asyncio.run(adapter.complete_scoped([{'role':'user','content':'Hello'}], scope=GatewayRequestScope[scope_name]))


def test_memory_factory_consumes_options_without_leaking_them_to_mem0_sdk(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from runtime.memory import mem0_memory
    from runtime.memory import mem0_observation_time, mem0_history_attribution
    seen = []
    completions = SimpleNamespace(create=lambda **kw: seen.append(kw) or SimpleNamespace(choices=[],usage=None))
    provider = SimpleNamespace(client=SimpleNamespace(base_url='https://api.deepseek.com',chat=SimpleNamespace(completions=completions)),
        generate_response=lambda **kw: None)
    def from_config(config):
        assert '_olivia_provider_options' not in config
        return SimpleNamespace(llm=provider)
    monkeypatch.setattr(mem0_memory, '_load_product_mem0_module', lambda: SimpleNamespace(Memory=SimpleNamespace(from_config=from_config)))
    monkeypatch.setattr(mem0_observation_time, 'bind_observation_time', lambda value: value)
    monkeypatch.setattr(mem0_history_attribution, 'bind_history_attribution', lambda value: value)
    config = mem0_memory.Mem0Config(enabled=True, data_root=tmp_path, llm_base_url='https://api.deepseek.com',
        llm_model='deepseek-v4-flash', llm_provider_options={'capabilities':{'thinking':'none','json_mode':False}})
    sdk_config = config.provider_config({})
    assert 'max_tokens' not in sdk_config['llm']['config']
    mem0_memory._default_factory(sdk_config)
    completions.create(messages=[], response_format={'type':'json_object'})
    assert seen == [{'messages':[]}]


def test_memory_different_endpoint_does_not_inherit_shared_overrides(tmp_path, monkeypatch):
    from runtime.memory import local_memory
    captured = []
    monkeypatch.setattr(local_memory, 'create_mem0_adapter', lambda **kw: captured.append(kw) or object())
    local_memory.create_conversation_memory_adapter(
        local_memory.MemoryConfig(enabled=True, provider='mem0', data_root=tmp_path / 'memory'),
        environ={'OLIVIA_MEMORY_LLM_BASE_URL':'https://memory.test/v1', 'OLIVIA_MEMORY_LLM_MODEL':'qwen-flash'},
        llm_fallback={'base_url':'https://reply.test/v1', 'model':'qwen-flash',
            'provider_options':{'capabilities':{'thinking':'none'}}})
    assert captured[0]['config'].llm_provider_options == {}
