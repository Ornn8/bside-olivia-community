import asyncio

import pytest

from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter


MESSAGES = ({'role': 'user', 'content': 'Return the recall check as JSON.'},)


@pytest.mark.parametrize('base_url,model,expected', [
    ('https://api.deepseek.com/v1', 'deepseek-v4-flash',
     {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low', 'max_tokens': 24000}),
    ('https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3.8-flash',
     {'enable_thinking': True, 'reasoning_effort': 'low', 'max_completion_tokens': 24000}),
    ('https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3.7-flash', {'enable_thinking': True}),
    ('https://synthetic.example/v1', 'unknown-model', {}),
])
def test_recall_check_is_low_reasoning_json_on_normal_endpoint(monkeypatch, base_url, model, expected):
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', base_url=base_url,
        model=model, requires_api_key=False))
    seen = []
    async def respond(body, request_id, **kwargs):
        seen.append(body)
        assert 'endpoint' not in kwargs
        assert body['response_format'] == {'type': 'json_object'}
        for key in ('thinking', 'enable_thinking', 'reasoning_effort', 'max_tokens', 'max_completion_tokens'):
            assert body.get(key) == expected.get(key)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"complete":true}'}}]}
    monkeypatch.setattr(adapter, '_post_json', respond)

    result = asyncio.run(adapter.complete_structured_scoped(MESSAGES,
        response_format={'type': 'json_object'}, scope=GatewayRequestScope.RECALL_CHECK))

    assert result.text == '{"complete":true}'
    assert len(seen) == 1
    assert not adapter._uses_official_review_responses(GatewayRequestScope.RECALL_CHECK)


def test_recall_check_respects_disabled_provider_capabilities():
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://api.deepseek.com/v1', model='deepseek-v4-flash', requires_api_key=False,
        provider_options={'capabilities': {'thinking': 'none', 'reasoning_effort': None, 'json_mode': False}}))
    scope = GatewayRequestScope.RECALL_CHECK

    body = adapter._body(MESSAGES, stream=False, max_reasoning=adapter._uses_max_reasoning(scope), scope=scope)

    assert not ({'response_format', 'thinking', 'reasoning_effort', 'max_tokens'} & body.keys())


def test_recall_check_responses_uses_standard_json_format_only():
    adapter = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://synthetic.example/v1', model='unknown-model', api_style='responses', requires_api_key=False))
    scope = GatewayRequestScope.RECALL_CHECK

    body = adapter._body(MESSAGES, stream=False, max_reasoning=adapter._uses_max_reasoning(scope), scope=scope)

    assert body['text'] == {'format': {'type': 'json_object'}}
    assert 'reasoning' not in body and 'thinking' not in body
