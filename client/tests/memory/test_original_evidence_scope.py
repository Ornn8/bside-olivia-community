from datetime import datetime, timezone
import json

import pytest

from runtime.memory.companion_memory_context import _ConversationMemoryView
from runtime.memory.conversation_memory_port import ConversationMemoryRecord
from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord, NullMemoryPort
from runtime.memory.memory_prompt import MemoryPromptBuilder, _unescape_reserved
from runtime.memory.recall import RecallResult


def render_records(records, *, max_chars=20000):
    builder = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None, max_tokens=300000)
    return builder.render(RecallResult(tuple(records), source_status=(('original_index', 'available'),)),
                          max_chars=max_chars)


def original_rows(prompt):
    return [row for line in _unescape_reserved(prompt.text).splitlines()
            if line.startswith('[{') for row in json.loads(line)]


def test_converted_original_pair_discloses_speakers_original_time_and_evidence_scope():
    stamp = datetime(2026, 8, 10, 18, 25, tzinfo=timezone.utc)
    records = tuple(_ConversationMemoryView._convert(ConversationMemoryRecord(
        memory_id='original:' + actor, text=text, user_id='alice', source_id='history:ring',
        occurred_at=stamp, metadata={'verbatim': True, 'complete_original': True, 'speaker': actor}))
        for actor, text in (('user', '下周一起去挑戒指好吗'), ('linli', '可以，先看看，下周还没到')))

    prompt = render_records(records)
    rows = original_rows(prompt)

    assert [row['speaker'] for row in rows] == ['user', 'linli']
    assert {row['occurred_at'] for row in rows} == {stamp.isoformat()}
    assert {row['evidence_scope'] for row in rows} == {'recorded_utterance'}
    assert [row['text'] for row in rows] == [record.text for record in records]
    assert {row['citation'] for row in rows} == {record.memory_id for record in prompt.references}
    assert all(row['provenance']['current_conversation'] is False for row in rows)
    assert 'Source identity does not itself confirm the events described' in prompt.text
    assert all('event_status' not in row and 'confirmed' not in row for row in rows)


@pytest.mark.parametrize('occurred_at,metadata', [
    (None, {'speaker': 'linli'}),
    ('2026-09-16T12:00:00+00:00', {'speaker': 'linli', 'timestamp_known': False}),
])
def test_missing_original_time_stays_unknown_instead_of_using_import_time(occurred_at, metadata):
    record = MemoryRecord('original:missing-time', CONVERSATION_MEMORY, '昨晚练了半小时',
        'archive_original_text', created_at=1789552800, occurred_at=occurred_at,
        provenance={'source_record_id': 'history:missing-time'},
        metadata={'complete_original': True, 'verbatim': True, **metadata})

    prompt = render_records((record,))
    row, = original_rows(prompt)

    assert row['speaker'] == 'linli'
    assert row['occurred_at'] is None
    assert 'null means unknown' in prompt.text
    assert '1789552800' not in prompt.text


def test_mixed_summary_is_not_presented_as_an_original_utterance():
    records = (
        MemoryRecord('original:known', CONVERSATION_MEMORY, '原文中的细节', 'original_text', 0,
            provenance={'source_record_id': 'history:known', 'speaker': 'user'},
            metadata={'complete_original': True, 'verbatim': True}),
        MemoryRecord('summary:old', CONVERSATION_MEMORY, '关系记忆摘要', 'mem0', 0),
    )

    rows = original_rows(render_records(records))

    assert rows[0]['speaker'] == 'user'
    assert rows[0]['evidence_scope'] == 'recorded_utterance'
    assert rows[1]['speaker'] == 'unknown'
    assert rows[1]['evidence_scope'] == 'retrieved_summary'


@pytest.mark.parametrize('source,is_current', [('history:old-letter', False), ('letter:local', True)])
def test_conversation_conversion_does_not_mark_imported_history_as_current(source, is_current):
    record = _ConversationMemoryView._convert(ConversationMemoryRecord(
        memory_id='source:1', text='此前的来往', user_id='alice', source_id=source))

    assert record.provenance['current_conversation'] is is_current
