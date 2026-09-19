from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord
from runtime.memory.recall import RecallResult
from runtime.memory.recall_trace import deepen_recall
from dataclasses import replace
import json


def record(source, ident=None, text='戒指仍戴在手上。'):
    return MemoryRecord(memory_id=ident or source, domain=CONVERSATION_MEMORY,
                        text=text, source='mem0', created_at=0,
                        provenance={'source_record_id': source},
                        metadata={'verbatim': True, 'complete_original': True})


class TraceBuilder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def trace_sources(self, sources, **kwargs):
        self.calls.append((tuple(sources), kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_known_source_can_recover_original_when_initial_recall_is_empty():
    original = record('reply:ring:1')
    builder = TraceBuilder((original,))
    result = deepen_recall(builder, RecallResult(topics=('戒指',)), query='戒指', source_ids=('reply:ring:1',))
    assert result.records == (original,)
    assert result.rounds == 1
    assert builder.calls[0][1]['expand'] is False


def test_explicit_source_is_read_before_related_neighbour():
    original, correction = record('reply:ring:1'), record('reply:correction:1', text='更正：那时只是计划。')
    builder = TraceBuilder((original,), (correction,))
    result = deepen_recall(builder, RecallResult(), query='还记得戒指吗', source_ids=('reply:ring:1',))
    assert result.records == (original, correction)
    assert [options['expand'] for _, options in builder.calls] == [False, True]
    assert result.rounds == 2


def test_existing_explicit_original_still_traces_its_later_correction():
    original = record('reply:ring:1')
    correction = record('reply:correction:1', text='更正：后来取消了那个计划。')
    builder = TraceBuilder((original,), (correction,))
    result = deepen_recall(builder, RecallResult(records=(original,)),
                           query='还记得戒指吗', source_ids=('reply:ring:1',))
    assert correction in result.records
    assert [options['expand'] for _, options in builder.calls] == [False, True]


def test_neighbour_expansion_never_exceeds_two_rounds():
    original, next_one, next_two = record('reply:a:1'), record('reply:b:1'), record('reply:c:1')
    builder = TraceBuilder((next_one,), (next_two,), (record('reply:d:1'),))
    result = deepen_recall(builder, RecallResult(records=(original,)), query='还记得那时候吗')
    assert result.records == (original, next_one, next_two)
    assert len(builder.calls) == result.rounds == 2
    assert builder.calls[1][0] == ('reply:b:1',)
    assert result.stop_reason == 'depth_limit'


def test_no_new_source_stops_without_repeating_the_query():
    original = record('reply:a:1')
    builder = TraceBuilder((original,), (record('reply:b:1'),))
    result = deepen_recall(builder, RecallResult(records=(original,)), query='还记得吗')
    assert result.records == (original,)
    assert len(builder.calls) == 1
    assert result.stop_reason == 'no_new_evidence'


def test_error_retains_existing_evidence_and_reports_unavailable():
    original = record('reply:a:1')
    builder = TraceBuilder(OSError('private local path must not escape'))
    result = deepen_recall(builder, RecallResult(records=(original,), source_status=(('archive', 'available'),)), query='还记得吗')
    assert result.records == (original,)
    assert dict(result.source_status)['trace'] == 'unavailable'
    assert result.status == 'degraded'
    assert 'private' not in result.state_text(result.records)


def test_failure_after_exact_read_keeps_new_original():
    original = record('reply:a:1')
    builder = TraceBuilder((original,), RuntimeError('MEMORY_TRACE_UNAVAILABLE'))
    result = deepen_recall(builder, RecallResult(), query='还记得吗', source_ids=('reply:a:1',))
    assert result.records == (original,)
    assert dict(result.source_status)['trace'] == 'unavailable'
    assert result.rounds == 2


def test_day_events_and_excluded_sources_cannot_seed_or_enter_trace():
    original = record('reply:a:1')
    builder = TraceBuilder((record('reply:current:1'), record('day:20260916'), original), ())
    result = deepen_recall(builder, RecallResult(records=(record('reply:current:1'),)),
                           query='还记得吗', source_ids=('day:20260916', 'reply:current:1', 'reply:a:1'),
                           exclude_source_ids=('reply:current:1',))
    assert result.records == (original,)
    assert builder.calls[0][0] == ('reply:a:1',)
    assert all(options['exclude_source_ids'] == ('reply:current:1',) for _, options in builder.calls)


def test_no_seed_does_not_scan_any_store():
    builder = TraceBuilder()
    result = deepen_recall(builder, RecallResult(topics=('第一次',)), query='还记得第一次吗', source_ids=('day:20260916',))
    assert not builder.calls
    assert result.rounds == 0
    assert result.stop_reason == 'no_source'


def test_paused_or_disabled_trace_does_not_read_more():
    original = record('reply:a:1')
    builder = TraceBuilder(RuntimeError('MEMORY_TRACE_DISABLED'), (record('reply:b:1'),))
    result = deepen_recall(builder, RecallResult(records=(original,)), query='还记得吗')
    assert result.records == (original,)
    assert dict(result.source_status)['trace'] == 'disabled'
    assert len(builder.calls) == 1


def test_parts_share_source_but_are_deduplicated_by_memory_id():
    part_a, part_b = record('reply:a:1', 'part:a'), record('reply:a:1', 'part:b')
    builder = TraceBuilder((part_a, part_b), (part_a, part_b))
    result = deepen_recall(builder, RecallResult(records=(part_a,)), query='还记得吗', source_ids=('reply:a:1',))
    assert result.records == (part_a, part_b)
    assert len({r.memory_id for r in result.records}) == 2


def test_ordinary_covered_message_does_not_expand_history():
    original = record('reply:a:1')
    recall = RecallResult(records=(original,), topics=('戒指',))
    builder = TraceBuilder()
    assert deepen_recall(builder, recall, query='戒指') == recall
    assert not builder.calls


def test_exact_trace_accepts_a_resolved_archive_alias():
    original = replace(record('history:original'), metadata={
        'complete_original': True, 'requested_source_id': 'relationship-letter:pair',
        'source_aliases': json.dumps(['history:original', 'relationship-letter:pair']),
    })
    result = deepen_recall(TraceBuilder((original,)), RecallResult(), query='戒指',
                           source_ids=('relationship-letter:pair',))
    assert result.records == (original,)


def test_explicit_world_source_has_capacity_priority_over_generic_search_hits():
    from runtime.memory.memory_port import NullMemoryPort
    from runtime.memory.memory_prompt import MemoryPromptBuilder
    original = record('reply:world-source', text='当时只是计划，后来已经取消。')
    noise = tuple(record(f'reply:noise:{i}', text='我们聊过许多普通的事情。') for i in range(8))
    recall = deepen_recall(TraceBuilder((original,), ()), RecallResult(records=noise),
                           query='后来已经取消', source_ids=('reply:world-source',))
    renderer = MemoryPromptBuilder(NullMemoryPort(), max_tokens=10000)
    rendered = renderer.render(recall, max_chars=1000)
    assert '当时只是计划，后来已经取消。' in rendered.text
    assert original in rendered.references


def test_excluded_alias_removes_existing_and_new_trace_records():
    original = replace(record('history:original'), metadata={
        'complete_original': True, 'requested_source_id': 'relationship-letter:pair',
        'source_aliases': json.dumps(['history:original', 'relationship-letter:pair', 'forgotten:raw']),
    })
    result = deepen_recall(TraceBuilder((original,)), RecallResult(records=(original,)),
                           query='戒指', source_ids=('relationship-letter:pair',),
                           exclude_source_ids=('forgotten:raw',))
    assert not result.records


def test_structured_trace_preserves_partial_source_failure():
    original = record('reply:ring:1')
    class StructuredBuilder:
        def trace_sources_result(self, *args, **kwargs):
            return RecallResult(records=(original,), source_status=(
                ('original_trace', 'unavailable'), ('archive_trace', 'available')))
        def trace_sources(self, *args, **kwargs):
            raise AssertionError('structured trace state must survive')
    result = deepen_recall(StructuredBuilder(), RecallResult(), query='戒指',
                           source_ids=('reply:ring:1',))
    assert result.records == (original,)
    assert result.status == 'degraded'
    assert dict(result.source_status)['original_trace'] == 'unavailable'


def test_later_trace_of_other_sources_does_not_erase_an_earlier_failure():
    class StructuredBuilder:
        def __init__(self):
            self.results = iter((
                RecallResult(records=(record('reply:first'),), source_status=(
                    ('original_trace', 'unavailable'), ('archive_trace', 'available'))),
                RecallResult(records=(record('reply:next'),), source_status=(
                    ('original_trace', 'available'), ('archive_trace', 'available'))),
            ))
        def trace_sources_result(self, *args, **kwargs):
            return next(self.results)
    result = deepen_recall(StructuredBuilder(), RecallResult(), query='还记得戒指吗',
                           source_ids=('reply:first',))
    assert len(result.records) == 2
    assert result.status == 'degraded'
    assert dict(result.source_status)['original_trace'] == 'unavailable'


def test_existing_depth_limit_cannot_restart_more_local_reads():
    original = record('reply:a:1')
    builder = TraceBuilder()
    result = deepen_recall(builder, RecallResult(records=(original,), rounds=2), query='还记得吗')
    assert not builder.calls
    assert result.rounds == 2
    assert result.stop_reason == 'depth_limit'


def test_real_builder_trace_is_local_and_respects_tombstones_and_pause(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from runtime.memory.memory_port import NullMemoryPort
    from tests.memory.test_mem0_memory import FakeMem0, _config
    backend = FakeMem0()
    memory = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    memory._originals.put('local-user', 'reply:ring:1', '戒指当时戴着。', '戒指我记得。', now)
    memory._originals.put('local-user', 'reply:forgotten:1', '秘密戒指', '秘密回应', now)
    memory._originals.forget('local-user', 'reply:forgotten:1')
    lifecycle = SimpleNamespace(is_paused=lambda: False)
    builder = CompanionMemoryPromptBuilder(NullMemoryPort(), memory, memory_lifecycle=lifecycle)
    def forbidden_status():
        raise AssertionError('local trace must not probe provider status')
    memory.status = forbidden_status
    result = deepen_recall(builder, RecallResult(topics=('戒指',)), query='戒指',
                           source_ids=('reply:ring:1', 'reply:forgotten:1'))
    assert {r.provenance['source_record_id'] for r in result.records} == {'reply:ring:1'}
    assert not backend.calls
    lifecycle.is_paused = lambda: True
    paused = deepen_recall(builder, RecallResult(topics=('戒指',)), query='戒指', source_ids=('reply:ring:1',))
    assert not paused.records
    assert dict(paused.source_status)['trace'] == 'disabled'
    assert not backend.calls
