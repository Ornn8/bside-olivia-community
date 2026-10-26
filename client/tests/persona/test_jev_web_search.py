import asyncio
import json

import pytest

from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter
from original_client_relay_api import RELAY_BASE
from runtime.reply import web_search

PAGES = [{'title': '上海明天天气预报', 'site': '中国天气网', 'url': 'https://www.weather.com.cn/a', 'date': '2026-10-25',
          'summary': '明天多云转小雨，最高气温19度。'}]


class Choices:
    def __init__(self, decision='q0', fail=False):
        self.decision, self.fail, self.calls = decision, fail, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        if self.fail:
            raise ValueError('JEV_UNAVAILABLE')
        from runtime.reply.jev_semantic_service import decide
        from types import SimpleNamespace
        native = SimpleNamespace(ask=lambda state, questions: {'query': {'choice': self.decision}})
        return decide(native, {'state': state, 'questions': questions, 'purpose': purpose})['decisions']


@pytest.fixture
def relay(monkeypatch):
    searched = []

    async def request(base, key, method, path, payload=None):
        searched.append((method, path, payload))
        if payload['query'] == 'broken':
            raise RuntimeError('502')
        return {'results': PAGES if '天气' in payload['query'] else [], 'charged_yuan': '0'}
    monkeypatch.setattr('original_client_relay_api.relay_request', request)
    return searched


def _gateway(monkeypatch, model='qwen3.7-flash', base_url=RELAY_BASE):
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', model=model, base_url=base_url))
    monkeypatch.setattr(gateway, '_key', lambda: 'olivia-synthetic')
    calls = []

    async def post(body, request, **kwargs):
        calls.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '回复正文'}}]}
    monkeypatch.setattr(gateway, '_post_json', post)
    return gateway, calls


@pytest.mark.parametrize('decision,fail', [('q0', False), ('none', False), ('q0', True)])
def test_jev_picks_a_public_sentence_and_only_that_reaches_the_search(monkeypatch, relay, decision, fail):
    decisions = Choices(decision, fail)
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: decisions)
    gateway, calls = _gateway(monkeypatch)
    messages = [{'role': 'system', 'content': '私人历史和秘密，不得发往搜索'},
                {'role': 'user', 'content': '上海明天天气怎么样？'}]
    result = asyncio.run(gateway.complete_scoped(messages, scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING))
    assert result.text == '回复正文' and len(decisions.calls) == 1 and len(calls) == 1
    writer = json.dumps(calls[-1], ensure_ascii=False)
    assert calls[-1]['enable_search'] is False and '自行判断是否搜索' not in writer
    if decision != 'none' and not fail:
        assert relay == [('POST', '/tools/search', {'query': '上海明天天气怎么样？', 'count': 5,
                                                    'question': '上海明天天气怎么样？'})]
        assert '私人历史和秘密' not in json.dumps(relay, ensure_ascii=False)
        assert '外部参考JSON' in writer and '明天多云转小雨' in writer and 'weather.com.cn' not in writer
    else:
        assert relay == []
        assert ('没有查询互联网' if not fail else '查询未完成') in writer


def test_any_model_on_the_relay_and_no_other_endpoint(monkeypatch, relay):
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Choices())
    gateway, calls = _gateway(monkeypatch, model='deepseek-flash')
    asyncio.run(gateway.complete_scoped([{'role': 'user', 'content': '上海明天天气怎么样？'}],
                                        scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING))
    assert len(relay) == 1 and '外部参考JSON' in json.dumps(calls[-1], ensure_ascii=False)
    assert 'enable_search' not in calls[-1]
    gateway, calls = _gateway(monkeypatch, model='deepseek-flash', base_url='https://example.org/v1')
    asyncio.run(gateway.complete_scoped([{'role': 'user', 'content': '上海明天天气怎么样？'}],
                                        scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING))
    assert len(relay) == 1 and '互联网' not in json.dumps(calls[-1], ensure_ascii=False)


def test_a_failed_or_empty_search_is_said_and_never_claimed(monkeypatch, relay):
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Choices())
    gateway, calls = _gateway(monkeypatch)
    for text, said in (('broken', '查询未完成'), ('今天有什么新闻', '没有找到相关结果')):
        asyncio.run(gateway.complete_scoped([{'role': 'user', 'content': text}],
                                            scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING))
        assert said in json.dumps(calls[-1], ensure_ascii=False)


def test_structured_repair_reuses_search_result(monkeypatch, relay):
    decisions = Choices()
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: decisions)
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible', model='qwen3.7-flash', base_url=RELAY_BASE))
    monkeypatch.setattr(gateway, '_key', lambda: 'olivia-synthetic')
    calls = []

    async def post(body, request, **kwargs):
        calls.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'invalid' if len(calls) == 1 else '{"reply":"晴"}'}}]}
    monkeypatch.setattr(gateway, '_post_json', post)
    asyncio.run(gateway.complete_structured_scoped([{'role': 'user', 'content': '上海明天天气怎么样？'}],
        scope=GatewayRequestScope.PERSONAL_CHAT_JSON,
        response_format={'type': 'json_schema', 'schema': {'type': 'object', 'required': ['reply'], 'properties': {'reply': {'type': 'string'}}}}))
    assert len(decisions.calls) == 1 and len(relay) == 1


def test_a_qq_turn_starts_the_search_with_the_turn_and_its_writer_reuses_it(monkeypatch, relay):
    order = []

    class Early(Choices):
        async def ask(self, state, questions, *, purpose):
            order.append('judged')
            return await super().ask(state, questions, purpose=purpose)
    decisions = Early()
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: decisions)
    monkeypatch.setattr('runtime.reply.jev_billing._account_key', lambda: 'olivia-synthetic', raising=False)
    gateway, calls = _gateway(monkeypatch, model='claude-haiku-5-5')

    async def turn():
        with web_search.searching('上海明天天气怎么样？'):
            await asyncio.sleep(0)
            order.append('writer')          # the turn's other preparation ran meanwhile
            for _ in range(2):              # her writer and a retry of it
                await gateway.complete_scoped([{'role': 'user', 'content': '上海明天天气怎么样？'}],
                                              scope=GatewayRequestScope.PERSONAL_CHAT_JSON)
    asyncio.run(turn())
    assert order == ['judged', 'writer'] and len(decisions.calls) == 1 and len(relay) == 1
    assert all('明天多云转小雨' in json.dumps(body, ensure_ascii=False) for body in calls)


def test_no_key_or_no_judgment_starts_nothing(monkeypatch, relay):
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: Choices())
    monkeypatch.setattr('runtime.reply.jev_billing._account_key', lambda: None, raising=False)

    async def turn():
        with web_search.searching('上海明天天气怎么样？'):
            return web_search._TURN.get()
    assert asyncio.run(turn()) is None
    monkeypatch.setattr('runtime.reply.jev_billing._account_key', lambda: 'olivia-synthetic', raising=False)
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: None)
    assert asyncio.run(turn()) is None and relay == []
