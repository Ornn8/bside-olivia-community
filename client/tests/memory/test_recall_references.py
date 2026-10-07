"""Named days, diary days and exchange vectors lead recall before keyword neighbours."""
from datetime import date, datetime, timedelta, timezone

import pytest

from runtime.memory.date_reference import day_ranges
from runtime.memory.source_retrieval import SourceRetrieval

ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 12, tzinfo=ZONE)


@pytest.mark.parametrize('text,expected', [
    ('8月26号写的信那晚发生的事', [(date(2026, 8, 26), date(2026, 8, 26))]),
    ('八月底那次', [(date(2026, 8, 21), date(2026, 8, 31))]),
    ('上上个月26号', [(date(2026, 8, 26), date(2026, 8, 26))]),
    ('上个月初', [(date(2026, 9, 1), date(2026, 9, 10))]),
    ('上周三我们聊了什么', [(date(2026, 9, 30), date(2026, 9, 30))]),
    ('前天你说的', [(date(2026, 10, 6), date(2026, 10, 6))]),
    ('还记得26号那天吗', [(date(2026, 9, 26), date(2026, 9, 26))]),
    ('2025年12月31日', [(date(2025, 12, 31), date(2025, 12, 31))]),
    ('十二月三号', [(date(2025, 12, 3), date(2025, 12, 3))]),  # a month/day not yet reached this year is last year
    ('我们一起过生日那次', []),
    ('明年3月', []),
])
def test_named_days(text, expected):
    assert day_ranges(text, NOW) == expected


def test_sources_in_range_reads_complete_exchanges_best_match_first(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.sqlite3')
    day = datetime(2026, 8, 26, 20, tzinfo=ZONE)
    index.put('u', 'reply:a:1', '今天天气很好', '是呀', day)
    index.put('u', 'reply:b:1', '今晚一起过生日，你唱了两首生日歌', '嗯，生日快乐', day + timedelta(minutes=30))
    index.put('u', 'reply:c:1', '第二天的事', '好', day + timedelta(days=1))
    start = datetime(2026, 8, 26, tzinfo=ZONE)
    records = index.sources_in_range('u', start, start + timedelta(days=1), '还记得过生日那天吗')
    sources = list(dict.fromkeys(record.source_id for record in records))
    assert sources == ['reply:b:1', 'reply:a:1']
    assert {r.metadata['speaker'] for r in records if r.source_id == 'reply:b:1'} == {'user', 'linli'}
    index.forget('u', 'reply:b:1')
    assert 'reply:b:1' not in {r.source_id for r in index.sources_in_range('u', start, start + timedelta(days=1))}


def test_exchange_vectors_are_derived_and_follow_edits_and_forgetting(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.sqlite3')
    index.put('u', 'reply:a:1', '我们去看海', '好呀', NOW - timedelta(days=3))
    index.put('u', 'reply:b:1', '今天考试', '加油', NOW - timedelta(days=2))
    missing = index.vectors_missing('u', 'm')
    assert [source for source, _, _ in missing] == ['reply:b:1', 'reply:a:1']
    index.put_vectors('u', 'm', [(source, digest, [1.0, 0.0] if source == 'reply:a:1' else [0.0, 1.0])
                                 for source, digest, _ in missing])
    assert index.vectors_missing('u', 'm') == []
    assert [hit[0] for hit in index.nearest_sources('u', 'm', [0.9, 0.1])] == ['reply:a:1', 'reply:b:1']
    assert index.nearest_sources('u', 'm', [0.9, 0.1])[0][2] == '我们去看海'
    index.put('u', 'reply:a:1', '我们去看海，改了', '好呀', NOW - timedelta(days=3))
    assert [source for source, _, _ in index.vectors_missing('u', 'm')] == ['reply:a:1']
    index.forget('u', 'reply:b:1')
    assert 'reply:b:1' not in [hit[0] for hit in index.nearest_sources('u', 'm', [0.0, 1.0])]


def test_named_day_and_diary_day_lead_recall(tmp_path):
    from tests.memory.test_mem0_memory import FakeMem0, _config
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
    from runtime.memory.recall import source_id
    from runtime.diary.diary import DiaryStore
    adapter = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    index = adapter._originals
    for i in range(30):
        index.put('local-user', f'reply:n{i}:1', f'生日蛋糕好吃吗 {i}', '好吃', NOW - timedelta(days=40 - i))
    birthday = datetime(2026, 8, 26, 21, tzinfo=ZONE)
    index.put('local-user', 'reply:bd:1', '今晚你唱了两首歌给我听，我们说好这天是纪念日', '嗯，我记住了', birthday)
    index.put('local-user', 'reply:sea:1', '周末想去海边', '好', datetime(2026, 7, 3, 20, tzinfo=ZONE))
    builder = CompanionMemoryPromptBuilder(LocalMemoryAdapter(tmp_path / 'archive.sqlite3'), adapter)
    recall = builder.collect_at('还记得8月26号那天吗', as_of=NOW)
    assert source_id(recall.records[0]) == 'reply:bd:1'
    assert recall.records[0].provenance['requested_reference'] == 'date'
    diary = DiaryStore(tmp_path / 'diary.sqlite3')
    diary.save('2026-07-03', {'title': '想去看海', 'mood': '期待', 'body': '你说周末想去海边，我偷偷查了海边的潮汐表。', 'facts': []},
               now=NOW, model='m', short=False)
    builder.attach_diary(diary)
    recall = builder.collect_at('你查了海边潮汐表那次还记得吗', as_of=NOW)
    assert source_id(recall.records[0]) == 'reply:sea:1'
    assert recall.records[0].metadata['retrieval_route'] == 'diary'
