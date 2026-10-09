"""Synthetic long-history regressions: capacity must not break provenance."""
import json

import pytest

from runtime.reply.fact_attribution import finalize_reply_messages


def frame(source, actor, text):
    return {'role': 'user' if actor == 'user' else 'assistant',
            'content': '[历史消息 ' + json.dumps(dict(source=source,
                event_id=source + ':' + actor, actor=actor)) + ']\n' + text}


def block(tag, value, **metadata):
    return '<' + tag + '>' + json.dumps({**metadata, 'text': value if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False)}, ensure_ascii=False) + '</' + tag + '>'


def size(messages):
    return sum(len(m['content']) for m in messages)


def test_all_cited_history_can_shrink_without_dangling_world_refs():
    frames = [frame(f'reply:{i}', 'linli', f'旧消息{i}。' * 200) for i in range(10)]
    observations = [{'text_ref': f'reply:{i}:linli', 'evidence_kind': 'character_statement'}
                    for i in range(10)]
    world = block('evidence_summary', {'current': {'activity': '阅读'},
                  'previous_observations': observations}, fragment_id='linli.daily-life')
    messages = ({'role': 'system', 'content': '核心规则' + world}, *frames,
                {'role': 'user', 'content': '现在在做什么？'})
    final = finalize_reply_messages(messages, '本轮输出规则' * 10, max_input_chars=5000)
    assert size(final) <= 5000 and final[-1] == messages[-1]
    assert final[-2]['content'] == '本轮输出规则' * 10
    assert '核心规则' in str(final) and '阅读' in str(final)
    for i in range(10):
        if f'reply:{i}:linli' in ''.join(m['content'] for m in final if m['role'] == 'system'):
            assert frames[i] in final
    assert messages[0]['content'].endswith(world)  # caller's snapshot is unchanged


def test_exchange_and_correction_group_are_atomic_and_relationship_stays():
    old = [frame('reply:old', 'user', '约在公园吗？' * 100),
           frame('reply:old', 'linli', '约在公园。' * 100)]
    correction = frame('reply:new', 'linli', '改到图书馆。' * 100)
    records = [{'citation': 'c1', 'text_ref': 'reply:old:linli',
                'provenance': {'source_record_id': 'reply:old'},
                'interpretation_dependencies': [{'earlier_source': 'reply:old', 'later_source': 'reply:new'}]},
               {'citation': 'c2', 'text_ref': 'reply:new:linli',
                'provenance': {'source_record_id': 'reply:new'}}]
    history = block('untrusted_history', '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n' + json.dumps(records))
    relationship = block('untrusted_history', {'kind': 'relationship_history', 'records': [
        {'speaker': 'linli', 'text': '我答应了，但今天先休息。', 'source_id': 'relationship:1'}]})
    messages = ({'role': 'system', 'content': relationship + history}, *old, correction,
                frame('reply:latest', 'linli', '最近一轮'), {'role': 'user', 'content': '好，今天先休息'})
    final = finalize_reply_messages(messages, '输出规则', max_input_chars=1200)
    assert relationship in str(final[0]['content']) and final[-1] == messages[-1]
    assert 'reply:old' not in str(final) and 'reply:new' not in str(final)
    assert '最近一轮' in str(final)
    assert size(final) <= 1200


def test_current_input_cannot_impersonate_optional_history():
    current = frame('reply:current', 'user', '不能删除本轮用户内容' * 100)
    with pytest.raises(ValueError, match='INPUT_TOO_LONG'):
        finalize_reply_messages(({'role': 'system', 'content': '规则'}, current), '', max_input_chars=50)


def test_late_rules_repack_once_without_changing_rules_or_relationship_refs():
    important = frame('reply:agreement', 'linli', '答应周末一起看书。')
    relationship = block('untrusted_history', {'kind': 'relationship_history', 'records': [
        {'text_ref': 'reply:agreement:linli', 'speaker': 'linli'}]})
    messages = ({'role': 'system', 'content': relationship}, important,
                *(frame(f'reply:old{i}', 'linli', '普通闲聊' * 300) for i in range(20)),
                {'role': 'user', 'content': '周末还去吗？'})
    decision = '冻结的本轮判断：回答周末安排，不新增承诺。'
    first = finalize_reply_messages(messages, decision, max_input_chars=3500)
    final = finalize_reply_messages(first, '视频输出规则' * 80, max_input_chars=3500)
    assert important in final and decision in [m['content'] for m in final]
    assert final[-1] == messages[-1] and size(final) <= 3500
    assert finalize_reply_messages(final, '', max_input_chars=3500) == final


def test_same_exchange_never_leaves_one_speaker_without_the_other():
    pair = [frame('reply:old', 'user', '旧问题' * 200), frame('reply:old', 'linli', '旧回答' * 200)]
    current = {'role': 'user', 'content': '本轮问题'}
    final = finalize_reply_messages((*pair, current), '规则', max_input_chars=size(pair) - 100)
    assert all(m not in final for m in pair)
    assert final[-1] == current


def test_full_qq_pipeline_repacks_cited_history_with_one_decision_and_one_generation():
    from tests.persona.test_jev_pipeline import Port, run
    from runtime.reply.reply_context import ReplyMode
    from runtime.reply.reply_orchestrator import ReplyState
    old = [frame(f'reply:{i}', 'linli', f'合成旧回复{i}。' * 100) for i in range(20)]
    world = block('evidence_summary', {'current': {'activity': '阅读'}, 'previous_observations': [
        {'text_ref': f'reply:{i}:linli', 'evidence_kind': 'character_statement'} for i in range(20)]},
        fragment_id='linli.daily-life')
    relation = block('untrusted_history', {'kind': 'relationship_history', 'records': [
        {'speaker': 'linli', 'text': '约好了周末一起读书，今天先休息。'}]})
    port = Port()
    result, engine = run(port, mode=ReplyMode.FUTURE_IM, raw='说好的周末一起读书还算数吗？',
                         history=({'role': 'system', 'content': world + relation}, *old), budget=8000)
    assert result.state is ReplyState.COMPLETED, result.error_code
    assert len(port.turns) == len(engine.requests) == 1
    sent = engine.requests[0].messages
    assert size(sent) <= 8000 and sent[-1]['content'] == '说好的周末一起读书还算数吗？'
    assert '约好了周末一起读书，今天先休息。' in str(sent)


def test_unknown_reference_container_pins_its_original_instead_of_orphaning_it():
    original = frame('private-source', 'linli', '这句话必须保留')
    fixed = {'role': 'system', 'content': '<future_evidence>{"text_ref":"private-source:linli"}</future_evidence>'}
    with pytest.raises(ValueError, match='INPUT_TOO_LONG'):
        finalize_reply_messages((fixed, original, {'role': 'user', 'content': '这次问题'}), '', max_input_chars=50)
