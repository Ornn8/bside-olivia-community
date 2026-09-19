import json

from runtime.memory.recall_check import _project, _validate


def _history_source(user_text: str, linli_text: str):
    return {
        'source': 's0',
        'scope': 'historical_exchange',
        'text': json.dumps([
            {'citation': 'history:user', 'speaker': 'user', 'text': user_text},
            {'citation': 'history:linli', 'speaker': 'linli', 'text': linli_text},
        ], ensure_ascii=False),
    }


def _value(*, status='confirmed', stage='completed', quote, topic='戒指'):
    return {
        'reply_intent': 'recall_question',
        'direct_questions': [],
        'findings': [{
            'topic': topic,
            'status': status,
            'event_stage': stage,
            'finding': '历史原文支持这一最小范围。',
            'citations': [{'source': 's0', 'quote': quote}],
        }],
    }


def test_character_acknowledgement_is_distinct_from_external_world_truth():
    source = _history_source('我把戒指戴到你手上。', '那枚戒指我已经戴上了。')
    value = _validate(_value(quote='那枚戒指我已经戴上了。'), [source])

    finding = value['findings'][0]
    assert finding['continuity_scope'] == 'character_acknowledged'
    assert finding['citations'][0]['matched_originals'][0]['speaker'] == 'linli'

    messages = (
        {'role': 'system', 'content': '<evidence_use>{}</evidence_use>'},
        {'role': 'user', 'content': '你还记得我们的戒指吗？'},
    )
    projected = _project(messages, value, [source], max_input_chars=50000)
    system = projected[0]['content']
    assert '"continuity_scope":"character_acknowledged"' in system
    assert '角色叙事连续性' in system
    assert '不证明现实世界客观事实' in system
    assert '不自动授予当前身体接触' in system


def test_user_report_does_not_become_shared_history_without_character_quote():
    source = _history_source('我们已经戴过戒指了。', '我听见你这么说了。')
    value = _validate(_value(quote='我们已经戴过戒指了。'), [source])
    assert value['findings'][0]['continuity_scope'] == 'user_report'


def test_character_plan_does_not_become_completed_history():
    source = _history_source('我们已经登记了。', '如果以后去登记，我会认真准备。')
    value = _validate(_value(stage='planned', quote='如果以后去登记，我会认真准备。', topic='登记'), [source])
    assert value['findings'][0]['continuity_scope'] == 'plan_only'


def test_conflicting_history_is_not_promoted_to_acknowledged_fact():
    source = _history_source('那天我们去了海边。', '我后来明确说过那次没有去成。')
    value = _validate(_value(status='conflicting', stage='unknown', quote='我后来明确说过那次没有去成。', topic='海边'), [source])
    assert value['findings'][0]['continuity_scope'] == 'contradicted'
