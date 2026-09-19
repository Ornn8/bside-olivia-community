from datetime import datetime, timedelta, timezone

from runtime.memory.conversation_memory_port import ConversationMemoryRecord
from runtime.memory.source_retrieval import SourceRetrieval


NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def fact(source, user='alice'):
    return ConversationMemoryRecord(memory_id=f'fact:{source}', text='这段经历与问题有关',
                                    source_id=source, user_id=user, score=0.9)


def test_semantic_original_survives_many_fts_topics_and_preserves_both_sides(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    for topic in range(6):
        for item in range(12):
            index.put('alice', f'noise:{topic}:{item}', f'topic{topic}', '普通回应', NOW)
    index.put('alice', 'ring', '那天把银圈交给你', '仍戴在无名指上', NOW)
    records = index.search('。'.join(f'topic{i}' for i in range(6)), 'alice',
                           (fact('ring'),), limit=100, expanded=True)
    assert {r.metadata['speaker'] for r in records if r.source_id == 'ring'} == {'user', 'linli'}
    assert len(records) <= 100
    assert {int(r.source_id.split(':')[1]) for r in records if r.source_id.startswith('noise:')} == set(range(6))
    assert all(r.metadata['retrieval_route'] == 'semantic' for r in records if r.source_id == 'ring')


def test_hybrid_sources_are_unique_and_report_each_matching_topic(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    index.put('alice', 'shared', 'alpha beta', '双方共同回应', NOW)
    records = index.search('alpha。beta', 'alice', (fact('shared'), fact('shared')), limit=10, expanded=True)
    assert len(records) == 2
    assert all(r.metadata['retrieval_route'] == 'hybrid' for r in records)
    assert all(r.metadata['topic_indexes'] == '0,1' for r in records)


def test_duplicate_query_clauses_cannot_report_an_omitted_topic_as_covered(tmp_path):
    from runtime.memory.recall import RecallResult, query_topics
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    index.put('alice', 'alpha-source', 'alpha', 'first response', NOW)
    index.put('alice', 'beta-source', 'beta', 'second response', NOW)
    query = 'alpha，alpha；beta'
    records = index.search(query, 'alice', limit=10, expanded=True)
    recall = RecallResult(records=records, topics=query_topics(query))
    retained = tuple(r for r in records if r.source_id == 'alpha-source')
    assert [item['state'] for item in recall.coverage(retained)] == ['evidence_found', 'omitted']


def test_long_original_preserves_all_parts_and_trailing_correction(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    user = '登记计划。' + '当时只是讨论。' * 550
    reply = '我记得这个计划。' + '我们也说过其他事。' * 350 + '更正：最终没有登记。'
    index.put('alice', 'long', user, reply, NOW)
    records = index.search('登记计划', 'alice', limit=100, expanded=True)
    for speaker, original in [('user', user), ('linli', reply)]:
        parts = [r for r in records if r.metadata['speaker'] == speaker]
        assert ''.join(r.text for r in parts) == original
        assert all(len(r.text) <= 2000 and r.metadata['part_count'] == len(parts) for r in parts)
        assert all(original[r.metadata['start']:r.metadata['end']] == r.text for r in parts)
    assert not index.search('登记计划', 'alice', limit=2, expanded=True)


def test_empty_proactive_user_side_is_not_fabricated(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    index.put('alice', 'proactive', '', '今天见到一架钢琴，想起你了。', NOW)
    records = index.search('钢琴', 'alice', limit=10, expanded=True)
    assert len(records) == 1
    assert records[0].metadata['speaker'] == 'linli'


def test_get_sources_and_expansion_respect_user_forgetting_and_exclusions(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    for user in ('alice', 'bob'):
        for position, source in enumerate(('before', 'seed', 'after', 'distant')):
            index.put(user, source, f'{user}:{source}', '回应', NOW + timedelta(days=position))
    index.forget('alice', 'before')
    direct = index.get_sources('alice', ('seed', 'seed', 'before', 'after'), exclude_source_ids=('after',))
    assert [r.source_id for r in direct] == ['seed', 'seed']
    assert all(r.user_id == 'alice' for r in direct)
    expanded = index.expand_sources('alice', ('seed',), limit=10)
    assert {r.source_id for r in expanded} == {'after'}
    assert all(r.metadata['retrieval_route'] == 'context' for r in expanded)
    assert all(r.metadata['expansion_seed'] == 'seed' for r in expanded)
    assert not index.expand_sources('alice', ('seed',), exclude_source_ids=('seed',), limit=10)
    assert not index.expand_sources('alice', ('seed',), exclude_source_ids=('after',), limit=10)


def test_semantic_fallback_is_not_hidden_behind_fts_and_deleted_sources_do_not_return(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    for position in range(12):
        index.put('alice', f'noise:{position}', 'hello', '回应', NOW)
    index.forget('alice', 'forgotten')
    records = index.search('hello', 'alice', (fact('missing-original'), fact('forgotten'), fact('excluded'), fact('foreign', 'bob')),
                           limit=10, expanded=True, exclude_source_ids=('excluded',))
    assert 'missing-original' in {r.source_id for r in records}
    assert not {'forgotten', 'excluded', 'foreign'} & {r.source_id for r in records}


def test_excluded_chunks_do_not_consume_source_candidate_slots(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    for position in range(15):
        index.put('alice', f'noise:{position}', 'piano', '', NOW)
    index.put('alice', 'wanted', 'piano', '保留回应', NOW)
    records = index.search('piano', 'alice', limit=10, expanded=True,
                           exclude_source_ids=tuple(f'noise:{i}' for i in range(15)))
    assert {r.source_id for r in records} == {'wanted'}


def test_oversized_exchange_does_not_block_other_complete_sources(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    index.put('alice', 'large', 'a' * 9000, '回应', NOW)
    index.put('alice', 'small', 'piano', '这个来源可以完整读取', NOW)
    records = index.search('piano', 'alice', (fact('large'),), limit=2, expanded=True)
    assert {r.source_id for r in records} == {'small'}


def test_expansion_is_atomic_bounded_and_does_not_jump_over_an_excluded_neighbour(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    for position, source in enumerate(('before', 'seed', 'after', 'distant')):
        index.put('alice', source, source, '回应', NOW + timedelta(days=position))
    assert not index.expand_sources('alice', ('seed',), limit=1)
    found = index.expand_sources('alice', ('seed',), exclude_source_ids=('after',), limit=100)
    assert {r.source_id for r in found} == {'before'}
    assert index.forgotten_sources('alice') == frozenset()
    index.forget('alice', 'before')
    assert index.forgotten_sources('alice') == frozenset({'before'})
    assert index.forgotten_sources('bob') == frozenset()


def test_original_fragment_offsets_account_for_whitespace_and_empty_parts(tmp_path):
    index = SourceRetrieval(tmp_path / 'sources.sqlite3')
    original = ' ' * 2000 + '  原文：不是那一天。\n' + '回忆。' * 750 + '\n 更正还在末尾。  '
    index.put('alice', 'whitespace', original, '', NOW)
    records = index.get_sources('alice', ('whitespace',))
    assert records and all(original[r.metadata['start']:r.metadata['end']] == r.text for r in records)
    assert records[0].metadata['start'] == 2002
    assert records[-1].text.endswith('更正还在末尾。')
    assert all(r.metadata['part_count'] == len(records) for r in records)
