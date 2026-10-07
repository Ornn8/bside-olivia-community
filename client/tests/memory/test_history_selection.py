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


def test_selection_bounds_recent_context_and_deduplicates_candidates():
    group = [{'citation': 'same', 'speaker': 'user', 'text': '相关旧事'}]
    messages = ({'role': 'system', 'content': _block([group] * 30)},
                {'role': 'user', 'content': '很长的上一轮' * 5000},
                {'role': 'assistant', 'content': '最近回复'},
                {'role': 'user', 'content': '那件事后来怎样？'})
    gateway = Gateway({'selected_ids': ['h0']})
    result = run(gateway, messages, budget=100000)
    assert len(gateway.calls) == 1
    packet = json.loads(gateway.calls[0][0][-1]['content'])
    assert len(packet['candidates']) == 1
    assert packet['recent_dialogue'] == [messages[-2]]
    assert sum(len(m['content']) for m in gateway.calls[0][0]) <= 12000
    assert tuple(m for m in result if m['role'] != 'system') == messages[1:]


def test_dependency_store_failure_keeps_a_valid_selection(monkeypatch):
    import runtime.memory.history_selection as selection
    import runtime.diagnostics.recall_trace as trace
    checks = []
    monkeypatch.setattr(trace, 'finish', lambda before, after, check: checks.append(check))
    def broken(*args, **kwargs):
        raise OSError('disk')
    monkeypatch.setattr(selection, 'save_dependencies', broken)
    result = run(Gateway())
    assert '推荐七里香' in result[0]['content']
    assert checks[-1] == {'status': 'checked', 'reason': 'dependency_store', 'findings': []}


def test_unexpected_failure_names_the_step_and_exception(monkeypatch):
    import runtime.diagnostics.recall_trace as trace
    checks = []
    monkeypatch.setattr(trace, 'finish', lambda before, after, check: checks.append(check))
    run(Gateway(failure=RuntimeError('offline')))
    assert checks[-1]['status'] == 'unavailable' and checks[-1]['reason'] == 'error_select_RuntimeError'
    assert trace.project({'event': 'history_recall', 'reason': 'error_select_RuntimeError'})['reason'] == 'error_select_RuntimeError'
    assert 'reason' not in trace.project({'event': 'history_recall', 'reason': 'error_select_C:/secret'})


def test_a_day_the_user_named_survives_an_empty_selection():
    groups = [[{'citation': 'old:0', 'speaker': 'user', 'occurred_at': '2026-08-26', 'text': '今晚一起过生日',
                'provenance': {'requested_reference': 'date'}}],
              [{'citation': 'old:1', 'speaker': 'user', 'occurred_at': '2026-09-01', 'text': '食堂吃面'}]]
    messages = ({'role': 'system', 'content': '人设\n' + _block(groups)},
                {'role': 'user', 'content': '还记得8月26号那天吗'})
    result = run(Gateway({'selected_ids': []}), messages)
    assert '今晚一起过生日' in result[0]['content'] and '食堂吃面' not in result[0]['content']
