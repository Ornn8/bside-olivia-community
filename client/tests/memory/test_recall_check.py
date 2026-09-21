"""Evidence verification must retain source identity and explicit uncertainty."""
import asyncio
from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest

from runtime.memory.recall_check import prepare_recall_messages


def history_messages():
    groups = (
        [{'citation': 'first:user', 'speaker': 'user', 'text': '周五一起去登记好吗'},
         {'citation': 'first:linli', 'speaker': 'linli', 'text': '周五只是计划，还没有登记。'}],
        [{'citation': 'second:user', 'speaker': 'user', 'text': '那个蓝色杯子收到了吗'},
         {'citation': 'second:linli', 'speaker': 'linli', 'text': '蓝色杯子已经收到了。'}],
    )
    original = '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n' + '\n'.join(
        json.dumps(group, ensure_ascii=False, separators=(',', ':')) for group in groups)
    system = ('original-persona\n<evidence_use>{"preserve_event_scope":true}</evidence_use>\n'
              '<untrusted_history>' + json.dumps({'text': original}, ensure_ascii=False) + '</untrusted_history>')
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': '周五的登记办好了吗'}]


def finding(*, source='s0', quote='周五只是计划，还没有登记。'):
    return {'reply_intent': 'recall_question', 'direct_questions': ['周五的登记办好了吗'],
        'findings': [{'topic': '登记', 'status': 'confirmed', 'event_stage': 'planned',
        'event': {'actor': 'linli', 'action': '登记', 'when': '周五'},
        'finding': '双方曾计划周五去登记，记录没有证明已完成。',
        'citations': [{'source': source, 'quote': quote}]}]}


class Gateway:
    config = SimpleNamespace(provider='openai_compatible')
    def __init__(self, value=None, *, text=None):
        self.text = text if text is not None else json.dumps(finding() if value is None else value, ensure_ascii=False)
        self.calls = []
    async def complete_scoped(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        return SimpleNamespace(text=self.text)


def projected(messages):
    raw = re.search(r'<recall_check>\s*(.*?)\s*</recall_check>', messages[0]['content'], re.S)
    assert raw is not None
    return json.loads(raw.group(1))


def prepare(messages, gateway, *, budget=20000):
    return asyncio.run(prepare_recall_messages(messages, gateway,
                       max_input_chars=budget, request_id='test-request'))


def test_valid_quote_retains_originals_and_adds_interpretation_only():
    messages = history_messages()
    before = deepcopy(messages)
    gateway = Gateway()

    result = prepare(messages, gateway)

    assert messages == before
    assert result[0] is not messages[0]
    assert 'original-persona' in result[0]['content']
    assert '周五只是计划，还没有登记。' in result[0]['content']
    assert '蓝色杯子已经收到了。' in result[0]['content']
    assert result[1] == before[1]
    check = projected(result)
    assert check['status'] == 'checked'
    assert check['current_turn']['intent'] == 'recall_question'
    assert check['current_turn']['questions'] == ['周五的登记办好了吗']
    assert check['interpretation_only'] is True
    item = check['events'][0]
    assert 'finding' not in item
    assert item['originals'] == ['o0']
    assert check['originals']['o0'] == {'source':'s0', 'scope': 'historical_exchange',
        'quote':'周五只是计划，还没有登记。',
        'records':[{'citation':'first:linli','speaker':'linli','occurred_at':None}]}
    assert len(gateway.calls) == 1
    prompt, kwargs = gateway.calls[0]
    assert kwargs['request_id'] == 'recall-check:test-request'
    assert kwargs['scope'].value == 'recall_check'
    packet = json.loads(prompt[1]['content'])
    assert all('original_references' not in source for source in packet['sources'])
    assert packet['sources'][-1] == {'source': 'current', 'scope': 'current_user_statement',
                                   'text': before[-1]['content']}
    assert any(record['text'] == '蓝色杯子已经收到了。' for record in packet['sources'][1]['text'])


def test_checked_context_keeps_conflicting_quotes_recent_chat_and_selected_originals():
    messages = history_messages()
    recent = '<untrusted_history>' + json.dumps({'text': json.dumps({'letters': [
        {'user_sent_at': '2026-09-16T08:00:00+08:00', 'user_letter': '早上好',
         'linli_reply': '早，今天怎么样'}]}, ensure_ascii=False)}, ensure_ascii=False) + '</untrusted_history>'
    canon = '<public_canon>{"statement":"人物身份不变"}</public_canon>'
    messages[0]['content'] += recent + canon
    value = finding()
    value['findings'][0].update(status='conflicting', event_stage='unknown')
    value['findings'][0]['citations'].append({'source': 'current', 'quote': '周五的登记办好了吗'})
    result = prepare(messages, Gateway(value))
    check = projected(result)
    assert check['status'] == 'checked'
    assert check['events'][0]['disputed'] is True
    assert len(check['events'][0]['originals']) == 2
    assert recent in result[0]['content'] and canon in result[0]['content']
    assert '蓝色杯子已经收到了。' in result[0]['content']
    assert '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]' in result[0]['content']


def test_checked_history_keeps_extracted_memories_and_originals_in_the_same_block():
    messages = history_messages()
    match = re.search(r'<untrusted_history>(.*?)</untrusted_history>', messages[0]['content'])
    history = json.loads(match[1])
    history['text'] += '\n' + json.dumps([{'citation': 'memory:cup',
        'evidence_scope': 'retrieved_summary', 'speaker': 'unknown',
        'text': '用户的杯子是绿色'}], ensure_ascii=False, separators=(',', ':'))
    messages[0]['content'] = messages[0]['content'].replace(match[1], json.dumps(history, ensure_ascii=False))
    result = prepare(messages, Gateway())
    assert projected(result)['status'] == 'checked'
    assert '用户的杯子是绿色' in result[0]['content']
    assert 'retrieved_summary' in result[0]['content']
    assert '蓝色杯子已经收到了。' in result[0]['content']


def test_quote_from_another_source_is_rejected_even_when_present_in_window():
    messages = history_messages()
    # The exact quote exists in s0, but this citation falsely assigns it to s1.
    gateway = Gateway(finding(source='s1'))

    result = prepare(messages, gateway)

    assert projected(result)['status'] == 'unavailable'
    assert projected(result)['events'] == []
    assert result[0]['content'].startswith(messages[0]['content'])
    assert len(gateway.calls) == 1


def test_structured_gateway_uses_the_same_validation_and_dedicated_schema():
    class StructuredGateway(Gateway):
        async def complete_structured_scoped(self, messages, *, response_format, **kwargs):
            self.response_format = response_format
            return await super().complete_scoped(messages, **kwargs)
    gateway = StructuredGateway()

    result = prepare(history_messages(), gateway)

    assert projected(result)['status'] == 'checked'
    assert len(gateway.calls) == 1
    assert gateway.response_format['name'] == 'recall_check'
    assert gateway.response_format['schema']['required'] == ['reply_intent', 'direct_questions', 'findings']
    assert gateway.response_format['schema']['properties']['findings']['items']['properties']['citations']['items']['properties']['source']['enum'] == ['s0', 's1', 'current']


@pytest.mark.parametrize('text', [
    'not json', '[]', '{"findings":[]}', '{"findings":[{}]}',
    json.dumps({'findings': [{**finding()['findings'][0], 'event_stage': 'assumed_done'}]}),
    json.dumps({'findings': [{**finding()['findings'][0], 'citations': []}]}),
    json.dumps(finding(quote='原文并没有这句话')),
    json.dumps(finding(source='nonexistent')),
])
def test_invalid_check_degrades_explicitly_without_altering_originals(text):
    messages = history_messages()
    before = deepcopy(messages)
    gateway = Gateway(text=text)

    result = prepare(messages, gateway)

    assert projected(result)['status'] == 'unavailable'
    assert projected(result)['events'] == []
    assert messages == before
    assert result[0]['content'].startswith(before[0]['content'])
    assert result[1] == before[1]
    assert len(gateway.calls) == 1


def test_original_unicode_line_separator_is_not_a_group_boundary():
    messages = history_messages()
    messages[0]['content'] = messages[0]['content'].replace('周五只是计划，还没有登记。', '周五只是计划，\u2028还没有登记。')
    # Quotes are checked against decoded original text, including JSON escapes.
    gateway = Gateway(finding(quote='周五只是计划，\u2028还没有登记。'))
    result = prepare(messages, gateway)
    assert projected(result)['status'] == 'checked'
    assert len(gateway.calls) == 1


def test_timeout_is_bounded_and_retains_the_frozen_original_window(monkeypatch):
    import runtime.memory.recall_check as recall_check
    messages = history_messages()
    before = deepcopy(messages)
    timeouts = []
    async def expire(awaitable, *, timeout):
        timeouts.append(timeout)
        awaitable.close()
        raise TimeoutError
    monkeypatch.setattr(recall_check.asyncio, 'wait_for', expire)

    result = prepare(messages, Gateway())

    assert timeouts == [120]
    assert projected(result)['status'] == 'unavailable'
    assert messages == before
    assert result[0]['content'].startswith(before[0]['content'])
    assert result[1] == before[1]


@pytest.mark.parametrize('checked', [True, False])
def test_existing_success_or_failure_marker_prevents_a_second_model_call(checked):
    gateway = Gateway() if checked else Gateway(text='invalid')
    first = prepare(history_messages(), gateway)
    assert projected(first)['status'] == ('checked' if checked else 'unavailable')

    second = prepare(first, gateway)

    assert second == first
    assert len(gateway.calls) == 1


@pytest.mark.parametrize('system', [
    'ordinary persona only',
    '<evidence_use>{}</evidence_use><runtime_time>{"current":"2026-09-16"}</runtime_time>',
    '<evidence_use>{}</evidence_use><public_canon>{"name":"离"}</public_canon>',
])
def test_no_history_avoids_preflight(system):
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': '你好'}]
    gateway = Gateway()

    result = prepare(messages, gateway)

    assert result[-1] == messages[-1]
    assert result[0]['content'].startswith(system)
    if '<evidence_use>' in system:
        assert projected(result)['reason'] == 'no_history'
    assert gateway.calls == []


def test_non_live_test_gateway_does_not_gain_an_extra_generation():
    gateway = Gateway()
    gateway.config = SimpleNamespace(provider='mock')
    messages = history_messages()
    result = prepare(messages, gateway)
    assert projected(result)['reason'] == 'not_enabled'
    assert result[-1] == messages[-1]
    assert gateway.calls == []


def test_large_interpretation_is_explicitly_unavailable_when_only_marker_fits():
    messages = history_messages()
    value = finding()
    value['findings'] *= 12
    value['findings'][0]['event']['action'] = '具体计划' * 50
    gateway = Gateway(value)

    result = prepare(messages, gateway, budget=5000)

    assert len(gateway.calls) == 1
    assert projected(result)['status'] == 'unavailable'
    assert projected(result)['reason'] == 'capacity'
    assert sum(len(message['content']) for message in result) <= 5000
    assert result[0]['content'].startswith(messages[0]['content'])


def test_budget_cannot_silently_drop_failure_disclosure_or_originals():
    messages = history_messages()
    before = deepcopy(messages)
    budget = sum(len(message['content']) for message in messages)

    with pytest.raises(ValueError, match='RECALL_CHECK_CONTEXT_BUDGET_EXCEEDED'):
        prepare(messages, Gateway(), budget=budget)

    assert messages == before


def recent_chat_messages(channel):
    from datetime import datetime, timezone
    from runtime.personal_chat.context import chat_context
    user_text = '你说过"周五去登记"\n现在还算数吗'
    reply_text = '我说的是"周五计划"\n还没有办理'
    recent, _ = chat_context([{
        'letter_id': 'escaped-chat', 'channel': channel,
        'delivery_status': 'DELIVERED', 'created_at': 1789527600,
        'user_sent_at': '2026-09-16T03:00:00+00:00',
        'life_received_at': '2026-09-16T03:00:01+00:00',
        'private_world_occurred_at': '2026-09-16T03:00:05+00:00',
        'content': user_text, 'reply_text': reply_text,
    }], query='登记', now=datetime(2026, 9, 16, 3, 2, tzinfo=timezone.utc))
    other = json.dumps({'letters': [{'user_letter': '蓝色杯子到了吗',
                                     'linli_reply': '蓝色杯子已经收到'}]}, ensure_ascii=False)
    system = '<evidence_use>{"preserve_event_scope":true}</evidence_use>' + ''.join(
        '<untrusted_history>' + json.dumps({'text': text}, ensure_ascii=False) + '</untrusted_history>'
        for text in (recent, other))
    return ([{'role': 'system', 'content': system}, {'role': 'user', 'content': '你当时怎么说的'}],
            {'user_letter': user_text, 'linli_reply': reply_text})


@pytest.mark.parametrize('channel', ['qq', 'wechat'])
@pytest.mark.parametrize('speaker', ['user_letter', 'linli_reply'])
def test_recent_chat_accepts_decoded_quotes_and_newlines(channel, speaker):
    messages, originals = recent_chat_messages(channel)
    before = deepcopy(messages)
    quote = originals[speaker]
    value = finding(source='s0', quote=quote)
    value['direct_questions'] = ['你当时怎么说的']
    gateway = Gateway(value)

    result = prepare(messages, gateway)

    check = projected(result)
    assert check['status'] == 'checked'
    assert check['events'][0]['originals'] == ['o0']
    assert check['originals']['o0'] == {'source': 's0', 'quote': quote, 'scope': 'correspondence_or_memory'}
    assert messages == before
    assert result[0]['content'].startswith(before[0]['content'])
    assert len(gateway.calls) == 1


def test_sharing_is_not_projected_as_a_demand_to_verify_every_memory():
    messages = history_messages()
    messages[-1]['content'] = '我记得我们聊过周五登记，今天只是想你了'
    value = finding()
    value.update(reply_intent='sharing', direct_questions=[])
    result = prepare(messages, Gateway(value))
    assert projected(result)['status'] == 'checked'
    assert projected(result)['current_turn']['intent'] == 'sharing'
    assert projected(result)['current_turn']['questions'] == []


def test_check_cannot_invent_a_question_on_behalf_of_the_user():
    value = finding()
    value['direct_questions'] = ['请证明我们真的登记过']
    result = prepare(history_messages(), Gateway(value))
    assert projected(result)['status'] == 'unavailable'


def test_invalid_topic_does_not_discard_other_verified_topics_or_promote_bad_citations():
    value = finding()
    value['findings'].append({'topic': '杯子', 'status': 'confirmed', 'event_stage': 'completed',
        'finding': '收到蓝色杯子', 'citations': [{'source': 's159', 'quote': '蓝色杯子已经收到了。'}]})
    result = prepare(history_messages(), Gateway(value))
    check = projected(result)
    assert check['status'] == 'partial'
    assert check['events'][0]['interpretation']['assessment'] == 'confirmed'
    assert check['originals']['o0']['source'] == 's0'
    assert len(check['events']) == 1
    assert len(check['originals']) == 1
    assert 's159' not in result[0]['content']


def test_decoded_recent_chat_quote_still_cannot_cite_a_different_source():
    messages, originals = recent_chat_messages('qq')
    gateway = Gateway(finding(source='s1', quote=originals['linli_reply']))

    result = prepare(messages, gateway)

    assert projected(result)['status'] == 'unavailable'
    assert projected(result)['events'] == []
    assert result[0]['content'].startswith(messages[0]['content'])
    assert len(gateway.calls) == 1
