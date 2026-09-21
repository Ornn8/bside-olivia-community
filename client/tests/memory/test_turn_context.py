import json
import asyncio
from types import SimpleNamespace

import pytest

from runtime.memory.recall_check import _validate, _project


def packet(value, sources):
    import re
    messages = ({'role': 'system', 'content': 'persona'}, {'role': 'user', 'content': '我吃啥了？'})
    result = _project(messages, _validate(value, sources), sources, max_input_chars=20000)
    assert result[-1] == messages[-1]
    return json.loads(re.search(r'<recall_check>(.*?)</recall_check>', result[0]['content'], re.S)[1])


def sources():
    return [{'source': 's0', 'scope': 'historical_exchange', 'text': json.dumps([
        {'citation': 'u1', 'speaker': 'user', 'occurred_at': '2026-09-21T18:00:00+08:00',
         'text': '我打包了晚饭，还没吃'},
        {'citation': 'a1', 'speaker': 'linli', 'occurred_at': '2026-09-21T18:01:00+08:00',
         'text': '我吃了面，你怎么也只吃这个'}], ensure_ascii=False)},
        {'source': 'current', 'scope': 'current_user_statement', 'text': '我吃啥了？'}]


def value():
    return {'reply_intent': 'correction', 'direct_questions': ['我吃啥了？'], 'findings': [
        {'topic': '晚饭', 'status': 'confirmed', 'event_stage': 'completed', 'finding': '打包已完成，进食未完成',
         'event': {'actor': 'user', 'action': '打包晚饭', 'when': None},
         'citations': [{'source': 's0', 'quote': '我打包了晚饭，还没吃'}]}]}


def test_event_stage_stays_bound_to_action_and_does_not_become_current_state():
    result = packet(value(), sources())
    event = result['events'][0]
    assert event['interpretation'] == {'actor': 'user', 'action': '打包晚饭', 'when': None,
                                      'stage': 'completed', 'assessment': 'confirmed', 'polarity': 'unknown'}
    assert event['interpretation_only'] is True
    assert 'event_stage' not in event and 'status' not in event
    assert result['current_state'] == []
    assert result['current_turn']['speaker'] == 'user'
    assert result['current_turn']['intent'] == 'correction'
    original = result['originals'][event['originals'][0]]
    assert original['quote'] == '我打包了晚饭，还没吃'
    assert original['records'][0]['speaker'] == 'user'
    assert original['records'][0]['occurred_at'] == '2026-09-21T18:00:00+08:00'
    assert '打包已完成，进食未完成' not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('provider', ['mock', 'openai_compatible'])
def test_world_snapshot_uses_the_same_context_when_no_history_requires_a_model(provider):
    from runtime.memory.recall_check import prepare_recall_messages
    class Gateway:
        config = SimpleNamespace(provider=provider)
        async def complete_scoped(self, *args, **kwargs):
            pytest.fail('A context projection must not introduce a model call')
    world = {'fragment_id': 'linli.daily-life', 'text': json.dumps({
        'kind': 'character_life_reference', 'stale': False, 'current': {'activity': '吃晚饭'}})}
    system = '<evidence_use>{}</evidence_use><evidence_summary>' + json.dumps(world) + '</evidence_summary>'
    messages = ({'role': 'system', 'content': system}, {'role': 'user', 'content': '你在干什么'})
    output = asyncio.run(prepare_recall_messages(messages, Gateway(), max_input_chars=20000))
    import re
    context = json.loads(re.search(r'<recall_check>(.*?)</recall_check>', output[0]['content'], re.S)[1])
    assert context['version'] == 1 and context['status'] == 'skipped'
    assert context['events'] == [] and context['current_turn']['intent'] == 'unknown'
    assert context['current_state'][0]['value'] == {'activity': '吃晚饭'}
    assert output[-1] == messages[-1]


def test_old_findings_without_a_specific_event_do_not_emit_unbound_completed_labels():
    old = value()
    del old['findings'][0]['event']
    result = packet(old, sources())
    assert result['events'][0]['interpretation'] is None
    assert 'completed' not in json.dumps(result)
    assert result['originals']


def test_only_fresh_published_world_snapshot_supplies_current_state():
    admitted = sources()
    for number, stale in enumerate((True, False), 1):
        admitted.append({'source': f's{number}', 'scope': 'evidence_summary', 'text': json.dumps({
            'fragment_id': 'linli.daily-life', 'text': json.dumps({
                'kind': 'character_life_reference', 'stale': stale,
                'current': {'activity': '吃晚饭', 'note': '青菜腐竹配饭'},
                'last_observation': {'activity': '练琴'}})})})
    result = packet(value(), admitted)
    assert result['current_state'] == [{'source': 's2', 'speaker': 'linli',
        'basis': 'published_world', 'value': {'activity': '吃晚饭', 'note': '青菜腐竹配饭'}}]


def test_partial_quote_validation_discards_the_combined_event_interpretation():
    raw = value()
    raw['findings'][0]['citations'].append({'source': 's0', 'quote': '我把晚饭吃完了'})
    result = packet(raw, sources())
    assert result['status'] == 'partial'
    assert result['events'][0]['interpretation'] is None
    assert len(result['originals']) == 1


def test_negation_and_joint_participants_survive_without_becoming_world_facts():
    raw = value()
    raw['findings'][0]['event'].update(actor='user_and_linli', action='吃晚饭', polarity='negated')
    admitted = sources()
    original = '我们两个都还没吃晚饭'
    admitted[0]['text'] = json.dumps([{'citation': 'u1', 'speaker': 'user', 'text': original}], ensure_ascii=False)
    raw['findings'][0]['citations'][0]['quote'] = original
    result = packet(raw, admitted)
    event = result['events'][0]['interpretation']
    assert event['actor'] == 'user_and_linli' and event['polarity'] == 'negated'
    assert result['current_state'] == []
