"""World selection offers a bounded recent set and does not repeat an identical question."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from runtime.reply import world_context_selection as selection
from runtime.reply.world_context_selection import MAX_CANDIDATES, WorldSelectionError, select_world_context


NOW = datetime(2026, 10, 10, 6, tzinfo=timezone.utc)


class Port:
    def __init__(self, level='useful'):
        self.level, self.calls = level, []

    async def ask(self, state, questions, *, purpose):
        wire = json.dumps(dict(state=state, questions=questions, purpose=purpose), ensure_ascii=False, separators=(',', ':'))
        self.calls.append((state, questions, len(wire.encode())))
        return {key: self.level for key in questions}


def packet(threads, *, at=NOW, scope=None, as_of=None):
    records = [{'field': 'schedule', 'value': {'date': '2026-10-10', 'phase': 'day', 'classes': [], 'current_class': None,
                                                'next_class': None}},
               {'field': 'weather', 'value': {'city': '上海', 'status': 'ok'}},
               {'field': 'character_development', 'value': {'items': []}}]
    for index in range(threads):
        finished = index % 3 == 0
        records.append({'field': 'threads', 'many': True, 'value': {
            'id': f'p{index}', 'title': f'事项{index}', 'status': 'completed' if finished else 'ongoing', 'kind': 'linli',
            'updated_at': (at - timedelta(minutes=threads - index)).isoformat(), 'source_id': f'reply:{index}:1'}})
    base = {'kind': 'character_life_reference', 'as_of': (as_of or at).isoformat(), 'stale': False, 'current': None, 'threads': [],
            'meaning': '仅为有时间和来源的角色视角。'}
    value = {'base': base, 'records': records, 'rhythm': {'local_time': '14:00', 'phase': 'day'}, 'recent_dialogue': {}}
    if scope is not None:
        value['scope'] = scope
    return value


def select(port, value, text='你今天在做什么'):
    return asyncio.run(select_world_context(port, value, text))


def test_offered_records_are_capped_newest_unfinished_first_and_request_does_not_grow():
    sizes = {}
    for threads in (30, 3000):
        port = Port()
        started = time.perf_counter()
        result = json.loads(select(port, packet(threads)))
        elapsed = time.perf_counter() - started
        (state, questions, size), = port.calls
        assert len(state['records']) <= MAX_CANDIDATES and len(questions) == len(state['records'])
        assert state['candidate_count'] == len(state['records'])
        sizes[threads] = size
        assert elapsed < 1.0  # building and trimming a catalogue of three thousand takes ~0.05 s
        assert result['kind'] == 'character_life_reference'
    assert sizes[3000] <= sizes[30] * 3 and sizes[3000] < 30000
    # What is dropped first: finished records, oldest first; then unfinished ones, oldest first.
    port = Port()
    select(port, packet(3000))
    offered = [entry['id'] for entry in port.calls[0][0]['records'].values() if entry['field'] == 'threads']
    assert len(offered) == MAX_CANDIDATES - 3
    unfinished = [index for index in range(3000) if index % 3 != 0]
    assert len(set(offered)) == len(offered)
    assert all(entry['status'] != 'completed' for entry in port.calls[0][0]['records'].values() if entry['field'] == 'threads')
    updated = sorted(entry['updated_at'] for entry in port.calls[0][0]['records'].values() if entry['field'] == 'threads')
    newest = sorted((NOW - timedelta(minutes=3000 - index)).isoformat() for index in unfinished)[-(MAX_CANDIDATES - 3):]
    assert updated == newest


def test_required_records_are_never_dropped_by_the_cap():
    port = Port()
    select(port, packet(500), '现在在做什么')
    fields = [entry['field'] for entry in port.calls[0][0]['records'].values()]
    assert {'schedule', 'weather', 'character_development'} <= set(fields)


def test_an_identical_question_in_the_same_bucket_reuses_the_answer_but_a_different_one_does_not():
    selection._ANSWERS.clear()
    value = packet(20, scope='life-a')
    port = Port()
    first = json.loads(select(port, value))
    later = NOW + timedelta(minutes=2)
    second = json.loads(select(port, packet(20, scope='life-a', as_of=later)))
    assert len(port.calls) == 1  # no second request
    assert second['as_of'] == later.isoformat() and second['threads'] == first['threads']  # assembled from the new state
    select(port, packet(20, scope='life-a'), '换个问题')                               # another message
    select(port, packet(21, scope='life-a'))                                             # the catalogue changed
    select(port, packet(20, scope='life-a', as_of=NOW + timedelta(minutes=10)))          # next time bucket
    select(port, packet(20, scope='life-b'))                                             # another store
    assert len(port.calls) == 5
    select(port, packet(20), '没有范围')
    select(port, packet(20), '没有范围')
    assert len(port.calls) == 7  # no scope, no reuse


def test_failed_questions_are_not_remembered():
    selection._ANSWERS.clear()

    class Failing(Port):
        async def ask(self, state, questions, *, purpose):
            self.calls.append(1)
            raise ValueError('JEV_UNAVAILABLE')
    port = Failing()
    for _ in range(2):
        with pytest.raises(WorldSelectionError):
            select(port, packet(5, scope='life-c'))
    assert len(port.calls) == 2


def test_adapter_builds_only_the_recent_dialogue_for_selection(monkeypatch):
    import local_server
    from types import SimpleNamespace
    from runtime.reply import conversation_context as context_module
    seen = {}
    monkeypatch.setattr(context_module, 'recent_correspondence', lambda *a, **k: seen.setdefault('archive', True) and '')
    adapter = object.__new__(local_server.LetterAdapter)
    adapter._now = lambda: NOW
    adapter.recent_letters = lambda: [{'letter_id': 'a', 'content': '早', 'reply_text': '早安', 'letter_status': 'COMPLETED',
                                       'life_received_at': NOW.isoformat(), 'created_at': NOW.timestamp()}]
    adapter.memory_prompt_builder = SimpleNamespace(relationship_context=lambda **k: pytest.fail('notes built for selection'))
    adapter.diary = SimpleNamespace(context=lambda now: pytest.fail('diary built for selection'))
    fragments = adapter.recent_letter_fragments('你好', now=NOW, selection_only=True)
    assert [fragment.fragment_id for fragment in fragments] == ['chat.recent'] and 'archive' not in seen
    # The ordinary reply still searches the archive.
    context_module.conversation_context(adapter.recent_letters(), query='你好', now=NOW)
    assert seen.get('archive') is True
