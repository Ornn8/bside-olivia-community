import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.memory.history_selection import _block, select_history_messages


def _messages():
    groups = [[{'citation': f'old:{i}', 'speaker': 'user', 'occurred_at': '2026-09-22',
                'text': text}] for i, text in enumerate(['食堂吃面', '推荐七里香', '落款写你的老姜'])]
    return ({'role': 'system', 'content': '人设\n' + _block(groups)},
            {'role': 'user', 'content': '[历史消息 {}]\n我准备出门'},
            {'role': 'assistant', 'content': '[历史消息 {}]\n路上小心'},
            {'role': 'user', 'content': '我准备去接娃，晚上微信聊'})


class Gateway:
    def __init__(self, answer=None, failure=None):
        self.answer = answer if answer is not None else {'selected_ids': ['h1']}
        self.failure = failure
        self.calls = []

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.failure:
            raise self.failure
        return SimpleNamespace(text=json.dumps(self.answer))


def run(gateway, messages=None, budget=20000):
    return asyncio.run(select_history_messages(messages or _messages(), gateway,
                                               max_input_chars=budget, request_id='letter-1'))


def test_one_selection_call_returns_only_selected_original_with_provenance():
    gateway = Gateway()
    result = run(gateway)
    assert len(gateway.calls) == 1
    packet = json.loads(gateway.calls[0][0][-1]['content'])
    assert len(packet['candidates']) == 3
    assert packet['current_message'] == _messages()[-1]['content']
    assert tuple(m for m in result[1:] if m['role'] != 'system') == _messages()[1:]
    assert '推荐七里香' in result[0]['content']
    assert 'old:1' in result[0]['content'] and '2026-09-22' in result[0]['content']
    assert '食堂吃面' not in result[0]['content']
    assert '落款写你的老姜' not in result[0]['content']


@pytest.mark.parametrize('answer', [{'selected_ids': []}, {'selected_ids': ['unknown']},
                                    {'selected_ids': ['h0', 'h0']}, {'selected_ids': 'h0'},
                                    {'selected_ids': ['h0'], 'rewrite': 'invented'}])
def test_empty_or_invalid_selection_never_restores_all_candidates(answer):
    result = run(Gateway(answer))
    assert result[0]['content'] == '人设\n'
    assert tuple(m for m in result[1:] if m['role'] != 'system') == _messages()[1:]


@pytest.mark.parametrize('error', [TimeoutError(), RuntimeError('offline')])
def test_failure_keeps_current_and_recent_without_retry(error):
    gateway = Gateway(failure=error)
    result = run(gateway)
    assert len(gateway.calls) == 1
    assert tuple(m for m in result[1:] if m['role'] != 'system') == _messages()[1:]
    assert '食堂吃面' not in result[0]['content']


def test_candidate_and_final_context_are_bounded_without_cutting_records():
    groups = [[{'citation':str(i), 'text':str(i) + '内容' * 600}] for i in range(60)]
    messages = ({'role':'system','content':_block(groups)}, {'role':'user','content':'你好'})
    gateway = Gateway({'selected_ids': ['h0', 'h1', 'h2', 'h3', 'h4', 'h5']})
    result = run(gateway, messages, budget=10000)
    packet = json.loads(gateway.calls[0][0][-1]['content'])
    assert len(packet['candidates']) < 24
    assert sum(len(m['content']) for m in gateway.calls[0][0]) <= 10000
    assert sum(len(m['content']) for m in result) <= 10000
    assert result[-1] == messages[-1]


def test_no_candidates_does_not_call_model():
    gateway = Gateway()
    messages = ({'role':'system','content':'人设'}, {'role':'user','content':'你好'})
    assert run(gateway, messages) == messages
    assert not gateway.calls


def test_selected_context_is_not_selected_again_at_gateway_boundary():
    gateway = Gateway()
    selected = run(gateway)
    assert run(gateway, selected) == selected
    assert len(gateway.calls) == 1
