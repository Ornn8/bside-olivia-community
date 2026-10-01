import asyncio
from datetime import datetime, timedelta

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.meal_lifecycle import LOCAL, advance


def at(hour, minute=0):
    return datetime(2026,9,28,hour,minute,tzinfo=LOCAL)


class Port:
    def __init__(self, choose=None, fail=False):
        self.choose, self.fail, self.calls = choose, fail, []
        self.raw_calls = []
    async def ask(self, state, questions, **kwargs):
        if kwargs.get('purpose')=='world-life-episode':
            return {key:next(iter(value['criteria'])) for key,value in questions.items()}
        self.raw_calls.append((state, questions))
        defaults = state.get('meal_candidate_defaults', {})
        questions = {'meal': {**questions['meal'], 'criteria': {
            key: {**defaults, **value} for key, value in questions['meal']['criteria'].items()}}}
        self.calls.append((state,questions))
        if self.fail: raise ValueError('JEV_UNAVAILABLE')
        options=questions['meal']['criteria']
        return {'meal':self.choose(state,options) if self.choose else next(iter(options))}


def meals(store, now):
    return {m['slot']:m for m in store.snapshot(now)['world']['meals'] if m['date']==now.date().isoformat()}


def test_clock_authors_breakfast_and_finishes_without_user_confirmation(tmp_path):
    store, port = DailyLifeStore(tmp_path/'world.db'), Port()
    asyncio.run(advance(store,port,at(7)))
    assert not port.calls and not meals(store,at(7))
    asyncio.run(advance(store,port,at(8,5)))
    first=meals(store,at(8,5))['breakfast']
    assert first['status']=='eating' and first['started_at']==at(8,5).astimezone(first_zone()).isoformat()
    asyncio.run(advance(store,port,at(8,10)))
    assert len(port.calls)==1
    asyncio.run(advance(store,port,at(8,35)))
    finished=meals(store,at(8,35))['breakfast']
    assert finished['status']=='eaten' and finished['food']==first['food']
    assert finished['started_at']==first['started_at'] and finished['finished_at']
    asyncio.run(advance(store,port,at(9)))
    assert meals(store,at(9))['breakfast']==finished
    assert len(port.calls)==2


def first_zone():
    from datetime import timezone
    return timezone.utc


@pytest.mark.parametrize('finish', [False, True])
def test_approved_exchange_can_finish_existing_meal_before_clock_duration(tmp_path, monkeypatch, finish):
    store=DailyLifeStore(tmp_path/'world.db')
    asyncio.run(advance(store,Port(),at(8)))
    original=meals(store,at(8))['breakfast']
    action=dict(source_id='reply:completion:1',occurred_at=at(8,12).isoformat(),
                evidence_kind='character_statement',reply_text='吃完了，碗也洗了',current=None,updates=[])
    monkeypatch.setattr(store,'pending_exchange_actions',lambda now,after=None:[action])
    port=Port(lambda s,o:'finish_now' if finish else 'continue_eating')
    asyncio.run(advance(store,port,at(8,12)))
    result=meals(store,at(8,12))['breakfast']
    assert result['status']==('eaten' if finish else 'eating')
    assert result['started_at']==original['started_at']
    assert result['food']==original['food']
    if finish:
        assert datetime.fromisoformat(result['finished_at'])==at(8,12)
    assert meals(store,at(8))['breakfast']==original
    asyncio.run(advance(store,port,at(8,13)))
    assert len(port.calls)==1


@pytest.mark.parametrize('reopen', [False, True])
def test_new_exchange_can_reconsider_skipped_lunch_without_inventing_completion(tmp_path, monkeypatch, reopen):
    store = DailyLifeStore(tmp_path/'world.db')
    first = Port(lambda s,o: 'skipped' if s['slot']=='lunch' else next(iter(o)))
    asyncio.run(advance(store,first,at(12,10)))
    original = meals(store,at(12,10))['lunch']
    action = dict(source_id='reply:new:1', occurred_at=at(13).isoformat(),
                  evidence_kind='character_statement', current={'note':'现在去准备午饭'}, updates=[])
    monkeypatch.setattr(store,'pending_exchange_actions',lambda now,after=None: [action], raising=False)
    port = Port(lambda s,o: 'start_0' if reopen else 'keep_skipped')
    asyncio.run(advance(store,port,at(13,1)))
    updated = meals(store,at(13,1))['lunch']
    assert updated['status'] == ('eating' if reopen else 'skipped')
    assert updated.get('finished_at') is None
    assert meals(store,at(12,10))['lunch'] == original
    assert port.calls[0][0]['exchange_actions']==[action]
    assert not any(v['status']=='eaten' for v in port.calls[0][1]['meal']['criteria'].values())
    asyncio.run(advance(store,port,at(13,2)))
    assert len(port.calls)==1


@pytest.mark.parametrize('skip',[False,True])
def test_overdue_day_recovery_decides_eaten_or_skipped_once(tmp_path,skip):
    store=DailyLifeStore(tmp_path/'world.db')
    port=Port(lambda s,o:'skipped' if skip else next(iter(o)))
    asyncio.run(advance(store,port,at(15)))
    records=meals(store,at(15))
    assert set(records)=={'breakfast','lunch'}
    for meal in records.values():
        assert meal['status']==('skipped' if skip else 'eaten') and meal['recovered']
        assert datetime.fromisoformat(meal['occurred_at']) < datetime.fromisoformat(meal['recorded_at'])
        assert meal['date']=='2026-09-28'
    asyncio.run(advance(store,port,at(16)))
    assert len(port.calls)==2
    # Never expose a later offline reconstruction in a historical time slice.
    assert not meals(store,at(14))


def test_failed_due_decision_is_visible_and_backoff_survives_restart(tmp_path):
    store, port=DailyLifeStore(tmp_path/'world.db'),Port(fail=True)
    asyncio.run(advance(store,port,at(8)))
    state=store.snapshot(at(8))['world']
    assert not state['meals']
    assert state['meal_schedule'][0]['status']=='error'
    assert state['meal_schedule'][0]['error_code']=='MEAL_DECISION_UNAVAILABLE'
    reopened=DailyLifeStore(store.path)
    asyncio.run(advance(reopened,port,at(8,1)))
    assert len(port.calls)==1
    port.fail=False
    asyncio.run(advance(reopened,port,at(8,5)))
    assert len(port.calls)==2 and meals(reopened,at(8,5))['breakfast']['status']=='eating'


def test_existing_terminal_daily_record_never_rewritten(tmp_path):
    store,port=DailyLifeStore(tmp_path/'world.db'),Port()
    store.publish_day('old',dict(location='宿舍',activity='吃饭',note='早餐吃完了。'),[],occurred_at=at(8),
                      meals=[dict(slot='breakfast',food='包子',status='eaten')])
    old=meals(store,at(8))['breakfast']
    asyncio.run(advance(store,port,at(11)))
    assert not port.calls and meals(store,at(11))['breakfast']==old
    store.publish_day('later',dict(location='宿舍',activity='整理',note='整理桌面。'),[],occurred_at=at(11),
                      meals=[dict(slot='breakfast',food='包子',status='eaten')])
    assert meals(store,at(11))['breakfast']==old


def test_plan_waits_until_its_own_time_and_then_closes(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    port=Port(lambda s,o:'plan_0' if not s['previous_meal'] else next(iter(o)))
    asyncio.run(advance(store,port,at(8)))
    record=meals(store,at(8))['breakfast']
    assert record['status']=='planned' and record['scheduled_for']
    asyncio.run(advance(store,port,at(8,15)))
    assert len(port.calls)==1
    asyncio.run(advance(store,port,at(8,30)))
    assert meals(store,at(8,30))['breakfast']['status']=='eating'
    asyncio.run(advance(store,port,at(8,55)))
    assert meals(store,at(8,55))['breakfast']['status']=='eaten'


def test_shanghai_date_and_schedule_are_server_authored(tmp_path):
    store=DailyLifeStore(tmp_path/'world.db')
    world=store.snapshot(at(1).astimezone(first_zone()))['world']
    assert world['schedule']['date']=='2026-09-28'
    assert [m['scheduled_for'] for m in world['meal_schedule']]==[at(h).isoformat() for h in (8,12,18)]
    assert all(m['status']=='not_due' for m in world['meal_schedule'])


def test_recovered_eating_times_do_not_overlap_class(tmp_path):
    store,port=DailyLifeStore(tmp_path/'world.db'),Port()
    asyncio.run(advance(store,port,at(11)))
    state,questions=port.calls[0]
    for candidate in questions['meal']['criteria'].values():
        if not candidate['started_at']: continue
        start=datetime.fromisoformat(candidate['started_at'])
        finish=datetime.fromisoformat(candidate['finished_at'])
        assert not any(start<datetime.fromisoformat(c['end']) and finish>datetime.fromisoformat(c['start'])
                       for c in state['world']['schedule']['classes'])


def test_meal_transport_is_lossless_and_smaller_with_recovery_and_continuation(tmp_path):
    import json
    from runtime.private_world.meal_lifecycle import options

    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())
    totals = [0, 0]
    for name, start, finish in [('recovery', at(11), None), ('continuation', at(8), at(8,35))]:
        store, port = DailyLifeStore(tmp_path / (name + '.db')), Port()
        asyncio.run(advance(store, port, start))
        old = None
        if finish:
            old = meals(store, start)['breakfast']
            asyncio.run(advance(store, port, finish))
        state, question = port.raw_calls[-1]
        restored = {key: {**state['meal_candidate_defaults'], **value}
                    for key, value in question['meal']['criteria'].items()}
        expected = options('breakfast', old, (finish or start).astimezone(first_zone()))
        # The recovery gate may remove candidates overlapping a class.
        assert restored == {key: expected[key] for key in restored}
        assert state['previous_meal'] == old
        assert set(state['world']) == {'schedule', 'meals'}
        if old:
            assert all(c['started_at'] == old['started_at'] and c['food'] == old['food']
                       for c in restored.values())
        assert all(datetime.fromisoformat(c['finished_at']) <= (finish or start)
                   for c in restored.values() if c['finished_at'])
        original_state = {k: v for k, v in state.items()
                          if k not in {'meal_candidate_defaults', 'meal_candidate_rule'}}
        original_question = {**question, 'meal': {**question['meal'], 'criteria': restored}}
        totals[0] += size([original_state, original_question])
        totals[1] += size([state, question])
    assert totals[1] < totals[0]
    print('meal lifecycle bytes before/after:', totals)


def test_runtime_advances_due_meal_even_when_main_activity_is_fresh(tmp_path,monkeypatch):
    from runtime.private_world import daily_life_runtime as module
    from runtime.reply import jev_questions
    store,port=DailyLifeStore(tmp_path/'world.db'),Port()
    store.publish_day('fresh',dict(location='宿舍',activity='练习',note='练习新片段。'),[],occurred_at=at(8,1),activity_kind='practice')
    runtime=module.DailyLifeRuntime(store,lambda:object(),lambda:'音乐专业大学生')
    async def no_emotion(now): pass
    async def forbidden(*args,**kwargs): pytest.fail('fresh main activity must not regenerate')
    monkeypatch.setattr(runtime,'_complete',forbidden)
    monkeypatch.setattr(module,'configured_duties',lambda:None)
    monkeypatch.setattr(jev_questions,'configured_questions',lambda:port)
    assert store.snapshot(at(8,5))['stale'] is False
    asyncio.run(runtime.refresh(at(8,5)))
    assert meals(store,at(8,5))['breakfast']['status']=='eating'
    assert len(port.calls)==1


def test_contrastive_action_picks_the_candidate_family():
    from runtime.private_world.meal_lifecycle import _ACTION_CRITERIA, _chosen_for_action, _family
    candidates = {'eat_0': {}, 'eat_1': {}, 'plan_0': {}, 'plan_1': {}, 'skipped': {}}
    assert {_family(k) for k in candidates} == {'eat_now', 'later', 'skip'}
    # A skip answer to the event list no longer wins over an explicit "eat now".
    assert _chosen_for_action(candidates, 'skipped', 'eat_now') == 'eat_0'
    assert _chosen_for_action(candidates, 'plan_1', 'eat_now') == 'eat_1'
    assert _chosen_for_action(candidates, 'eat_1', None) == 'eat_1'
    assert _chosen_for_action({'keep_skipped': {}, 'start_0': {}}, 'keep_skipped', 'eat_now') == 'start_0'
    for meaning in _ACTION_CRITERIA.values():
        assert set(meaning) == {'what', 'not_for', 'examples'}
    assert '不是默认' in _ACTION_CRITERIA['skip']['not_for']


def test_class_covering_every_meal_option_waits_instead_of_recording_a_skip(tmp_path, monkeypatch):
    store, port = DailyLifeStore(tmp_path/'world.db'), Port()
    real = store.snapshot
    def busy(now):
        snapshot = real(now)
        snapshot['world']['schedule'] = {**snapshot['world'].get('schedule', {}), 'classes': [
            {'start': at(11, 50).isoformat(), 'end': at(18, 30).isoformat()}]}
        return snapshot
    monkeypatch.setattr(store, 'snapshot', busy)
    asyncio.run(advance(store, port, at(12, 5)))
    # Only "skipped" survived the class filter: no decision, no recorded skip.
    assert 'lunch' not in meals(store, at(12, 5))
    assert not [state for state, _ in port.calls if state['slot'] == 'lunch']


def test_user_words_reach_the_meal_decision_before_any_meal_record(tmp_path):
    from datetime import timezone
    store, port = DailyLifeStore(tmp_path/'world.db'), Port()
    store.record_exchange('reply:dinner-call', '开饭啦，给你擦擦嘴角，快吃饭乖', '快擦干净过来吃饭', [],
                          occurred_at=at(11, 59), current_quote='快擦干净过来吃饭')
    with store._db() as db:
        db.execute('INSERT INTO life_exchange_world_gate VALUES (?,?,?)', ('reply:dinner-call', 'reconsider', '快擦干净过来吃饭'))
        db.execute('INSERT INTO life_exchange_user_text VALUES (?,?)', ('reply:dinner-call', '开饭啦，给你擦擦嘴角，快吃饭乖'))
    asyncio.run(advance(store, port, at(12, 1)))
    lunch_call = next(state for state, questions in port.calls if state['slot'] == 'lunch')
    assert [a.get('user_text') for a in lunch_call['exchange_actions']] == ['开饭啦，给你擦擦嘴角，快吃饭乖']
    assert 'user_text是对方的话' in lunch_call['exchange_rule']
