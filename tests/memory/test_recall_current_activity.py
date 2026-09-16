"""Only a fresh daily-life observation can support a current activity."""
import asyncio
from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest

from runtime.memory.recall_check import prepare_recall_messages


def _tag(name, value):
    return '<' + name + '>' + json.dumps(value, ensure_ascii=False) + '</' + name + '>'


def _life(*, stale=False, current=True, last_observation=False):
    observation = {
        'activity': '正在整理乐谱', 'note': '把新谱放进文件夹',
        'occurred_at': '2026-09-16T12:00:00+08:00', 'source_id': 'life:current',
    }
    value = {'kind': 'character_life_reference', 'stale': stale,
             'current': observation if current else None}
    if last_observation:
        value['last_observation'] = observation
    return value


def _evidence(value, *, fragment_id='linli.daily-life'):
    return _tag('evidence_summary', {
        'fragment_id': fragment_id, 'untrusted': True,
        'text': json.dumps(value, ensure_ascii=False),
    })


class _Gateway:
    config = SimpleNamespace(provider='openai_compatible')

    async def complete_scoped(self, messages, **kwargs):
        return SimpleNamespace(text=json.dumps({
            'reply_intent': 'recall_question', 'direct_questions': ['上次收到明信片了吗'],
            'findings': [{
                'topic': '明信片', 'status': 'confirmed', 'event_stage': 'completed',
                'finding': '旧信确认收到了明信片。',
                'citations': [{'source': 's0', 'quote': '明信片收到了。'}],
            }],
        }, ensure_ascii=False))


def _project(*fragments):
    original = '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]\n' + json.dumps([
        {'citation': 'letter:linli', 'speaker': 'linli', 'text': '明信片收到了。'},
    ], ensure_ascii=False, separators=(',', ':'))
    messages = [
        {'role': 'system', 'content': '<evidence_use>{}</evidence_use>'
         + _tag('untrusted_history', {'text': original}) + ''.join(fragments)},
        {'role': 'user', 'content': '上次收到明信片了吗'},
    ]
    before = deepcopy(messages)
    result = asyncio.run(prepare_recall_messages(
        messages, _Gateway(), max_input_chars=20000, request_id='current-activity-test',
    ))
    assert messages == before
    match = re.search(r'<recall_check>\s*(.*?)\s*</recall_check>', result[0]['content'], re.S)
    assert match is not None
    check = json.loads(match.group(1))
    assert check['status'] == 'checked'
    return check, result


def test_fresh_production_daily_life_remains_an_authorized_current_activity_source():
    fragment = _evidence(_life())

    check, result = _project(fragment)

    assert check['use_boundaries']['current_activity_sources'] == ['s1']
    # The writer still has the exact observation, not just an inaccessible ID.
    assert fragment in result[0]['content']


@pytest.mark.parametrize('fragment', [
    _evidence(_life(stale=True)),
    _evidence(_life(stale=True, current=False, last_observation=True)),
    _evidence(_life(current=False, last_observation=True)),
    _evidence(_life(current=False)),
    _evidence(_life(), fragment_id='linli.rhythm'),
    _evidence({**_life(), 'kind': 'historical_life_reference'}),
    _tag('trusted_world_fact', {
        'fact_id': 'world:old-activity', 'statement': '昨天在整理乐谱',
        'occurred_at': '2026-09-15T12:00:00+08:00',
    }),
], ids=['stale-current', 'stale-last-observation', 'last-observation-only',
        'no-current', 'rhythm', 'wrong-kind', 'historical-world-fact'])
def test_old_or_indirect_activity_evidence_cannot_authorize_a_current_activity(fragment):
    check, _ = _project(fragment)

    assert check['use_boundaries']['current_activity_sources'] == []


def test_current_activity_ids_do_not_include_neighboring_world_history_or_rhythm():
    check, _ = _project(
        _tag('trusted_world_fact', {'fact_id': 'world:old', 'statement': '昨晚练过琴'}),
        _evidence(_life()),
        _evidence(_life(), fragment_id='linli.rhythm'),
        _evidence(_life(stale=True, current=False, last_observation=True)),
    )

    assert check['use_boundaries']['current_activity_sources'] == ['s2']
