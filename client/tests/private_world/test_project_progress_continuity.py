import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.private_world.world_decision import compile_decision, decision_context


NOW = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)
CURRENT = {'location': '家里', 'activity': '练琴', 'note': '在家里练琴。'}


def project(key='practice', *, detail='还在练习', status='ongoing'):
    return {'id': key, 'title': '左手衔接练习', 'detail': detail, 'status': status}


def decision(*, progress=None, status='ongoing', key='practice', following=None):
    return {'activity': {'kind': 'practice', 'place_id': 'home', 'focus': '左手衔接'},
            'meal': None, 'project': {'id': key, 'title': '左手衔接练习', 'status': status,
                                     'progress': progress, 'next_activity': following}}


def data_for(store, now=NOW):
    state = store.snapshot(now)
    return decision_context({'time': now.isoformat(), 'persona': '[]', 'world': state['world'],
                             'rhythm': state['rhythm'],
                             'projects': store.exchange_state(now=now)['projects']})


def test_seventh_active_project_remains_selectable_in_real_refresh(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    for index in range(7):
        store.publish_day('seed:' + str(index), CURRENT, [project('p' + str(index))],
                          occurred_at=NOW - timedelta(days=1, minutes=7-index))
    assert 'p0' not in {p['id'] for p in store.snapshot(NOW)['projects']}
    calls = []
    class Gateway:
        async def complete(self, messages, **kwargs):
            packet = json.loads(messages[-1]['content'])
            calls.append(packet)
            # Only continue an identity actually offered to the decision model.
            selected = 'p0' if any(p['id'] == 'p0' for p in packet['projects']) else 'p6'
            return SimpleNamespace(text=json.dumps(decision(key=selected, progress='慢练时第二处换指仍卡住')))
    runtime = DailyLifeRuntime(store, Gateway, lambda: '[]')
    asyncio.run(runtime.refresh(NOW))
    assert {p['id'] for p in calls[0]['projects']} == {'p' + str(i) for i in range(7)}
    assert runtime.error_code is None
    result = store.snapshot(NOW)['current']['progress']
    assert result[0]['id'] == 'p0' and '换指仍卡住' in result[0]['detail']


def test_project_history_uses_three_last_events_with_original_sources_and_cutoff(tmp_path):
    path = tmp_path / 'life.sqlite3'
    store = DailyLifeStore(path)
    store.publish_day('old', CURRENT, [project(detail='第一次试奏')], occurred_at=NOW-timedelta(days=3))
    store.publish_day('second', CURRENT, [project(detail='慢练一段')], occurred_at=NOW-timedelta(days=2))
    quote = '刚才换指还是会停一下。'
    store.record_exchange('reply:report:1', '', quote,
        [{**project(detail='EXTRACTOR_PARAPHRASE'), 'kind': 'linli', 'actor': 'linli', 'quote': quote}],
        occurred_at=NOW-timedelta(hours=1))
    store.publish_day('same-time-later', CURRENT, [project(detail='把第二处拆成两个小节')],
                      occurred_at=NOW-timedelta(hours=1))
    store.publish_day('future', CURRENT, [project(detail='FUTURE_RESULT')], occurred_at=NOW+timedelta(hours=1))
    user_quote = '我还在等老师回信。'
    character_quote = '我的短练习做完了。'
    store.record_exchange('reply:other:1', user_quote, character_quote, [
        {**project('shared'), 'kind': 'shared', 'actor': 'user', 'quote': user_quote},
        {**project('closed', detail='EXTRACTED_RESULT', status='completed'),
         'kind': 'linli', 'actor': 'linli', 'quote': character_quote}], occurred_at=NOW)

    # Restart must read history from the journal, not a process-local cache.
    state = DailyLifeStore(path).exchange_state(now=NOW, include_history=True)
    live = next(p for p in state['projects'] if p['id'] == 'practice')
    history = live['history']
    assert [p['source_id'] for p in history] == ['second', 'reply:report:1', 'same-time-later']
    assert [p['updated_at'] for p in history] == sorted(p['updated_at'] for p in history)
    assert [p['evidence_kind'] for p in history] == ['published_life', 'character_statement', 'published_life']
    assert history[1]['detail'] == history[1]['quote'] == quote
    assert 'EXTRACTOR_PARAPHRASE' not in json.dumps(history)
    assert 'FUTURE_RESULT' not in json.dumps(state)
    assert all(p['kind'] == 'linli' for p in history)
    closed = next(p for p in state['projects'] if p['id'] == 'closed')
    assert closed['evidence_kind'] == 'character_statement'
    assert 'history' not in closed and 'detail' not in closed and 'quote' not in closed
    assert state['shared'][0]['evidence_kind'] == 'user_statement'
    assert 'history' not in state['shared'][0]


def test_three_days_same_project_progress_reaches_next_choice_and_emotion(tmp_path):
    path = tmp_path / 'life.sqlite3'
    changes = ['第二处换指还会停，先拆成两个小节', '拆开后连上了两个小节，接回原速仍卡住', '慢速能连过四小节，原速的衔接还没稳定']
    following = {'kind': 'practice', 'place_id': 'home', 'focus': '四小节慢速连接'}
    life_inputs, emotion_sources = [], []
    class Gateway:
        async def complete_structured_scoped(self, messages, **kwargs):
            packet = json.loads(messages[-1]['content'])
            if 'assessment' in packet:
                sources = packet['assessment']['sources']
                emotion_sources.extend(sources)
                payload = {'appraisals': [dict(source_id=s['source_id'], quote='', reported_affect=None,
                    reaction='none', goal_or_need=None, action_tendency='none', concern=None, revises=None)
                    for s in sources]}
            else:
                life_inputs.append(packet)
                payload = decision(progress=changes[len(life_inputs)-1], following=following)
            return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))
    gateway = Gateway()
    async def run():
        for day in range(3):
            # Reconstruct the runtime each day to prove durable continuity.
            store = DailyLifeStore(path)
            runtime = DailyLifeRuntime(store, lambda: gateway, lambda: '[]')
            await runtime.refresh(NOW + timedelta(days=day))
            # Her reading of the day's progress is paid for with the user's next message.
            from runtime.memory.received_user_originals import ReceivedOriginal
            when = NOW + timedelta(days=day, minutes=1)
            await runtime.emotion.evaluate_received([ReceivedOriginal(
                f'received-user:qq:d{day}', '今天练得怎么样？', when, (f'reply:d{day}:1',), 'qq', f'd{day}')], now=when)
            assert runtime.error_code is None and runtime.emotion.error_code is None
    asyncio.run(run())
    assert len(life_inputs) == 3
    for day in (1, 2):
        old = next(p for p in life_inputs[day]['projects'] if p['id'] == 'practice')
        assert len(old['history']) == day
        assert changes[day-1] in old['detail']
    for change in changes:
        assert any(change in source['text'] and source['source_kind'] == 'published_world'
                   for source in emotion_sources)
    store = DailyLifeStore(path)
    later = store.exchange_state(now=NOW+timedelta(days=7), include_history=True)['projects'][0]
    assert later['id'] == 'practice' and later['status'] == 'ongoing'
    assert len(later['history']) == 3
    assert len({p['source_id'] for p in later['history']}) == 3
    assert '四小节慢速连接' in later['detail']


@pytest.mark.parametrize('progress', [None, '', '   '])
def test_completion_requires_current_result_not_only_a_next_plan(tmp_path, progress):
    data = data_for(DailyLifeStore(tmp_path / 'life.sqlite3'))
    with pytest.raises(ValueError, match='RESULT_REQUIRED' if progress is None else 'DAILY_LIFE_'):
        compile_decision(decision(status='completed', progress=progress,
            following={'kind': 'practice', 'place_id': 'home', 'focus': '以后再练'}), data)


def test_new_short_completed_step_is_valid_and_progress_never_overwrites_current(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    current, projects, meals = compile_decision(decision(status='completed', progress='两小节慢练已连续弹过'), data_for(store))
    assert current == {'location': '住处', 'activity': '练琴：左手衔接', 'note': '在住处练琴：左手衔接。'}
    assert '两小节慢练已连续弹过' in projects[0]['detail']
    assert set(projects[0]) == {'id', 'title', 'detail', 'status'}
    store.publish_day('short:complete', current, projects, occurred_at=NOW, meals=meals)
    assert store.exchange_state(now=NOW)['projects'][0]['status'] == 'completed'


def test_pause_or_no_change_does_not_require_fabricated_progress(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.publish_day('seed', CURRENT, [project()], occurred_at=NOW-timedelta(days=1))
    data = data_for(store)
    value = decision(status='paused')
    value['activity'] = {'kind': 'rest', 'place_id': 'home', 'focus': ''}
    current, projects, meals = compile_decision(value, data)
    store.publish_day('pause', current, projects, occurred_at=NOW, meals=meals)
    value['project'] = None
    _, no_change, _ = compile_decision(value, data)
    assert no_change == []
    assert store.exchange_state(now=NOW)['projects'][0]['status'] == 'paused'


def test_all_project_identities_survive_explicit_input_budget_failure(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    for index in range(30):
        store.publish_day('seed:' + str(index), CURRENT,
            [project('p' + str(index), detail='慢练进展记录' * 35)],
            occurred_at=NOW-timedelta(days=1, minutes=30-index))
    before = store.exchange_state(now=NOW)
    calls = []
    class Gateway:
        config = SimpleNamespace(max_input_chars=7000)
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(decision(progress='新进展')))
    runtime = DailyLifeRuntime(store, Gateway, lambda: '[]')
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code == 'DAILY_LIFE_GENERATION_UNAVAILABLE'
    assert calls == []
    assert store.exchange_state(now=NOW) == before
    assert len(before['projects']) == 30


def test_many_closed_projects_stay_bounded_without_stopping_world(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    for index in range(143):
        store.publish_day('closed-source:' + str(index), CURRENT,
            [project('closed:' + str(index), detail='实际归还并核对借阅记录。', status='completed')],
            occurred_at=NOW-timedelta(days=1, minutes=index))
    store.publish_day('active-source', CURRENT, [project('active')], occurred_at=NOW-timedelta(hours=12))
    calls = []
    class Gateway:
        config = SimpleNamespace(max_input_chars=30000)
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(decision(key='active', progress='第一处慢练衔接稳定了一些')))
    runtime = DailyLifeRuntime(store, Gateway, lambda: '[]')
    asyncio.run(runtime.refresh(NOW))
    assert runtime.error_code is None and len(calls) == 1
    request = json.loads(calls[0][-1]['content'])
    rows = request['projects']
    # A decision lists her open projects and only the latest finished identities;
    # the rest stay in the journal and the request says so instead of growing.
    assert {p['id'] for p in rows} == {'active'} | {'closed:' + str(i) for i in range(6)}
    assert request['project_coverage']['finished_omitted'] == 137 and request['project_coverage']['open_omitted'] == 0
    assert len(next(p for p in rows if p['id'] == 'active')['history']) == 1
    for item in rows:
        if item['status'] == 'completed':
            assert set(item) == {'id', 'title', 'status', 'evidence_kind'}
            assert item['evidence_kind'] == 'published_life'
    # Compact identity projection does not remove stored source or event text.
    evidence = next(p for p in store.exchange_state('左手衔接练习', now=NOW)['projects'] if p['id'] == 'closed:0')
    assert evidence['source_id'] == 'closed-source:0'
    assert evidence['detail'] == '实际归还并核对借阅记录。'
