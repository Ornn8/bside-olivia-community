import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.reply.world_context_selection import select_world_context, WorldSelectionError


def test_selection_dialogue_keeps_recent_pair_provenance_without_repeating_full_prompt():
    from types import SimpleNamespace
    from runtime.reply.world_context_selection import selection_dialogue
    rows = [{'source_id': f'r{i}', 'user_letter': f'u{i}', 'linli_reply': f'a{i}',
             'sent_at': f't{i}', 'truncated': False} for i in range(4)]
    result = selection_dialogue([SimpleNamespace(fragment_id='chat.recent',
        text=json.dumps({'meaning': 'LONG_REPEATED_CONTRACT', 'letters': rows})),
        SimpleNamespace(fragment_id='chat.historical', text='OLDER_RETRIEVAL')])
    assert result['exchanges'] == rows[-2:]
    assert result['coverage'] == 'last_two_exchanges_only'
    assert 'LONG_REPEATED_CONTRACT' not in json.dumps(result)
    assert 'OLDER_RETRIEVAL' not in json.dumps(result)


def test_jev_can_select_complete_timetable_without_current_class(tmp_path):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=now)
    class Port:
        async def ask(self, state, questions, *, purpose):
            assert purpose == 'reply-world-selection'
            return {key: 'must' if item['field'] == 'schedule' else 'skip'
                    for key, item in state['records'].items()}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '你下午不用去学校吗？')))
    assert result['schedule']['current_class'] is None
    assert result['schedule']['next_class']['start'].startswith('2026-09-28T14:00')
    assert len(result['schedule']['classes']) == 2
    assert result['as_of'] == now.isoformat()
    assert 'character_development' not in result


def test_selection_budget_does_not_choose_manual_priority_or_truncate(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    class Port:
        async def ask(self, state, questions, **kwargs):
            return {key: 'useful' for key in questions}
    with pytest.raises(WorldSelectionError, match='JEV_WORLD_SELECTION_BUDGET'):
        asyncio.run(select_world_context(Port(), packet, '说说今天', max_chars=100))


@pytest.mark.parametrize('historical', [False, True])
def test_required_activity_keeps_complete_facts_and_staleness_within_budget(tmp_path, historical):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('day:read', {'location': '家里', 'activity': '读书', 'note': '读完一篇散文。'}, [],
                      occurred_at=now - timedelta(minutes=5))
    if historical:
        store.record_exchange('reply:new', '你好', '你好', [], occurred_at=now)
    packet = store.reply_candidates(now=now)
    packet['records'].append({'field': 'threads', 'many': True, 'value': {'note': '可选经历' * 2000}})
    field = 'last_observation' if historical else 'current'
    expected = {r['field']: r['value'] for r in packet['records'] if r['field'] in (field, 'schedule')}
    class Port:
        async def ask(self, state, questions, **kwargs):
            return {key: 'must' if item['field'] == 'threads' else 'skip'
                    for key, item in state['records'].items()}
    result = asyncio.run(select_world_context(Port(), packet, '',
        required_fields=('current', 'last_observation', 'schedule')))
    value = json.loads(result)
    assert len(result) <= 3500
    assert value['stale'] is historical
    assert value[field] == expected[field]
    assert value['schedule'] == expected['schedule']
    assert value['threads'] == []
    if historical:
        assert value['current'] is None


def test_required_facts_over_budget_fail_before_provider_instead_of_truncating(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    packet['records'].append({'field': 'current', 'value': {'note': '完整事实' * 2000}})
    class Port:
        async def ask(self, *args, **kwargs):
            pytest.fail('An impossible required-state budget must be detected before billing')
    with pytest.raises(WorldSelectionError, match='^JEV_WORLD_SELECTION_BUDGET$'):
        asyncio.run(select_world_context(Port(), packet, '', required_fields=('current', 'schedule')))


def test_ordinary_reply_can_still_skip_current_activity_and_schedule(tmp_path):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('day:read', {'location': '家里', 'activity': '读书', 'note': '读完一篇散文。'}, [],
                      occurred_at=now)
    packet = store.reply_candidates(now=now)
    class Port:
        async def ask(self, state, questions, **kwargs):
            return {key: 'skip' for key in questions}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '你好')))
    assert result['current'] is None
    assert 'schedule' not in result


def test_failed_jev_does_not_fall_back_to_keyword_selection(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    class Port:
        async def ask(self, *args, **kwargs):
            raise RuntimeError('PRIVATE_PROVIDER_DETAILS')
    with pytest.raises(WorldSelectionError, match='^JEV_WORLD_SELECTION_UNAVAILABLE$'):
        asyncio.run(select_world_context(Port(), packet, '学校'))


@pytest.mark.parametrize('code', ['JEV_HTTP_503', 'JEV_BILLING_UNAVAILABLE',
    'JEV_BILLING_RECEIPT_INVALID', 'JEV_RESPONSE_INVALID'])
def test_selection_preserves_safe_cause_through_chat_mapping(tmp_path, code):
    from runtime.personal_chat.backend import _generation_failure_code
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    class Port:
        async def ask(self, *args, **kwargs):
            raise ValueError(code)
    with pytest.raises(WorldSelectionError, match='^' + code + '$'):
        asyncio.run(select_world_context(Port(), packet, '晚饭呢'))
    assert _generation_failure_code(code) == code
    assert _generation_failure_code('JEV_WORLD_SELECTION_UNAVAILABLE') == 'JEV_WORLD_SELECTION_UNAVAILABLE'


def test_candidate_projection_uses_as_of_not_future_world(tmp_path):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('future', {'location': '家里', 'activity': '练琴', 'note': 'FUTURE_SECRET'}, [],
                      occurred_at=datetime(2026, 9, 29, 5, tzinfo=timezone.utc))
    assert 'FUTURE_SECRET' not in json.dumps(store.reply_candidates(now=now))


def test_jev_priorities_keep_whole_timetable_when_related_records_exceed_budget(tmp_path):
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=now)
    packet['records'] = [
        {'field': 'recent_episodes', 'many': True, 'value': {'note': '练习经过' * 900}},
        *packet['records'],
    ]
    calls = []
    class Port:
        async def ask(self, state, questions, **kwargs):
            calls.append(state)
            assert all('incremental_chars' not in r and 'value' not in r for r in state['records'].values())
            return {key: 'must' if r['field'] == 'schedule' else 'useful'
                    for key, r in state['records'].items()}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '下午的课呢？', max_chars=1600)))
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 1600
    assert len(result['schedule']['classes']) == 2
    assert result['schedule']['next_class']['start'].startswith('2026-09-28T14:00')
    assert 'recent_episodes' not in result
    assert len(calls) == 1


def test_later_exchange_marks_old_published_activity_historical(tmp_path):
    from datetime import timedelta
    now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('day:past', {'location': '家里', 'activity': '休息', 'note': '在家休息。'}, [],
                      occurred_at=now-timedelta(minutes=5))
    store.record_exchange('reply:new', '你好', '你好', [], occurred_at=now)
    packet = store.reply_candidates(now=now)
    assert packet['base']['stale'] is True
    assert not any(r['field'] == 'current' for r in packet['records'])
    last = next(r['value'] for r in packet['records'] if r['field'] == 'last_observation')
    assert last['evidence_kind'] == 'published_life' and last['actor'] == 'linli'


def test_batches_measure_questions_and_state_together_against_transport_cap(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    packet['records'] = [{'field': 'threads', 'many': True, 'value': {'note': '内容' * 400}}
                         for _ in range(25)]
    calls = []
    packets = []
    class Port:
        async def ask(self, state, questions, *, purpose):
            envelope = {'state': state, 'questions': questions, 'purpose': purpose}
            assert len(json.dumps(envelope, ensure_ascii=False, separators=(',', ':')).encode()) <= 30000
            calls.extend(questions)
            packets.append(envelope)
            return {key: 'skip' for key in questions}
    asyncio.run(select_world_context(Port(), packet, '你好'))
    assert len(calls) == 25 and len(set(calls)) == 25
    assert len(packets) == 1


def test_directory_preview_never_replaces_selected_complete_evidence(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    original = {'note': '完整生活经过。' * 100, 'status': 'completed', 'title': '练习'}
    packet['records'] = [{'field': 'threads', 'many': True, 'value': original}]
    class Port:
        async def ask(self, state, questions, **kwargs):
            assert state['records']['r0'] == {'field': 'threads', 'status': 'completed', 'title': '练习'}
            return {'r0': 'must'}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '练习怎么样？')))
    assert result['threads'] == [original]


def test_short_and_long_records_use_same_metadata_directory_and_restore_full_sources(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    originals = [dict(id='p1', title='明天的课', actor='linli', status='cancelled',
                      updated_at='2026-09-28T01:00:00+00:00', detail='SHORT_PRIVATE_DETAIL'),
                 dict(id='p2', title='明天的课', actor='user', status='planned',
                      updated_at='2026-09-28T02:00:00+00:00', detail='LONG_PRIVATE_DETAIL' * 200)]
    packet['records'] = [{'field': 'threads', 'many': True, 'value': item} for item in originals]
    packet['rhythm']['internal_history'] = 'RHYTHM_PRIVATE_DETAIL' * 500
    class Port:
        async def ask(self, state, questions, **kwargs):
            encoded = json.dumps(state)
            assert 'PRIVATE_DETAIL' not in encoded and 'preview' not in encoded
            assert state['records']['r0']['status'] == 'cancelled'
            assert state['records']['r1']['actor'] == 'user'
            assert state['records']['r0']['updated_at'] != state['records']['r1']['updated_at']
            return {'r0': 'must', 'r1': 'useful'}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '刚才说的课程怎么回事？', max_chars=10000)))
    assert result['threads'] == originals


def test_directory_uses_short_same_source_aliases_and_host_restores_identifiers(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime.now(timezone.utc))
    source_id = 'reply:' + 'a' * 64
    originals = [dict(id='opaque-' + 'b' * 64, source_id=source_id, title='下午练习', actor='linli', status=status)
                 for status in ('planned', 'cancelled')]
    packet['records'] = [{'field': 'threads', 'many': True, 'value': item} for item in originals]
    class Port:
        async def ask(self, state, questions, **kwargs):
            assert source_id not in json.dumps(state) and 'opaque-' not in json.dumps(state)
            first, second = state['records']['r0'], state['records']['r1']
            assert first['source_id'] == second['source_id'] == 's0'
            assert first['id'] == second['id'] == 'i0'
            assert first['status'] == 'planned' and second['status'] == 'cancelled'
            return {'r0': 'useful', 'r1': 'must'}
    result = json.loads(asyncio.run(select_world_context(Port(), packet, '还去练习吗？')))
    assert result['threads'] == list(reversed(originals))


def test_reply_candidates_offer_live_threads_and_only_recently_ended_ones(tmp_path):
    """Every thread ever made was offered on each reply, so cost grew until replies failed."""
    now = datetime(2026, 9, 29, 4, tzinfo=timezone.utc)
    store = DailyLifeStore(tmp_path / 'life.db')
    current = {'location': '家里', 'activity': '练琴', 'note': '继续练习。'}
    for source, days, projects in (
        ('day:a', 40, [{'id': 'old-ongoing', 'title': '长期练习', 'detail': '一直在练。', 'status': 'ongoing'}]),
        ('day:b', 30, [{'id': 'old-cancelled', 'title': '取消的计划', 'detail': '取消了。', 'status': 'cancelled'}]),
        ('day:c', 10, [{'id': 'old-done', 'title': '早就完成的曲子', 'detail': '完成了。', 'status': 'completed'}]),
        ('day:d', 2, [{'id': 'recent-done', 'title': '刚完成的调音', 'detail': '调好了。', 'status': 'completed'}]),
        ('day:e', 1, [{'id': 'waiting', 'title': '等你回复的约定', 'detail': '等你说。', 'status': 'awaiting_user'}]),
    ):
        store.publish_day(source, current, projects, occurred_at=now - timedelta(days=days))
    threads = {r['value']['id'] for r in store.reply_candidates(now=now)['records'] if r['field'] == 'threads'}
    assert threads == {'old-ongoing', 'recent-done', 'waiting'}
    # Ended threads stay in the journal; only the reply candidates leave them out.
    assert {'old-done', 'old-cancelled'} <= {p['id'] for p in store.snapshot(now)['projects']}


def test_oversized_world_catalog_offers_fewer_threads_instead_of_failing(tmp_path):
    packet = DailyLifeStore(tmp_path / 'life.db').reply_candidates(now=datetime(2026, 9, 28, 5, tzinfo=timezone.utc))
    fixed = [record['field'] for record in packet['records'] if not record.get('many')]
    threads = [{'field': 'threads', 'many': True, 'value': {
        'id': f'thread-{i}', 'title': f'一直在进行的事项{i} ' + '细节' * 30, 'status': 'ongoing',
        'updated_at': f'2026-09-{1 + i % 27:02d}T{i % 24:02d}:00:00+00:00'}} for i in range(300)]
    packet = {**packet, 'records': [*packet['records'], *threads]}
    seen = {}

    class Port:
        async def ask(self, state, questions, *, purpose):
            seen['records'] = list(state['records'].values())
            seen['bytes'] = len(json.dumps({'state': state, 'questions': questions, 'purpose': purpose},
                                           ensure_ascii=False, separators=(',', ':')).encode())
            return {key: 'useful' for key in questions}

    asyncio.run(select_world_context(Port(), packet, '最近在忙什么？'))
    assert seen['bytes'] <= 30000
    assert [r['field'] for r in seen['records'] if r['field'] in fixed] == fixed
    kept = [r['updated_at'] for r in seen['records'] if r['field'] == 'threads']
    assert 0 < len(kept) < 300 and min(kept) >= sorted(t['value']['updated_at'] for t in threads)[300 - len(kept)]
