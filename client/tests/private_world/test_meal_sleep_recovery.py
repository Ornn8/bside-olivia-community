import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.life_episode import create
from runtime.private_world.life_rhythm import rhythm, with_recovery
from runtime.private_world.meal_lifecycle import advance
from runtime.private_world.student_world import student_schedule
from runtime.private_world.world_decision import decision_context


LOCAL = timezone(timedelta(hours=8))


def at(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=LOCAL)


class Port:
    def __init__(self, *, meal=None, path='settled'):
        self.meal, self.path, self.calls = meal, path, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((purpose, state, questions))
        if purpose == 'world-meal-lifecycle':
            values = {key: {**state['meal_candidate_defaults'], **value}
                      for key, value in questions['meal']['criteria'].items()}
            return {'meal': self.meal(state, values) if self.meal else next(iter(values))}
        path = self.path if state['new_activity'] == 'rest' else next(iter(state['paths']))
        assert path in state['paths']
        return {'trigger': next(iter(questions['trigger']['criteria'])),
                'experience': f'{path}:recovery:none'}

    @property
    def meal_calls(self):
        return [call for call in self.calls if call[0] == 'world-meal-lifecycle']


def meal(store, now, slot='breakfast'):
    return next(item for item in store.snapshot(now)['world']['meals']
                if item['date'] == now.date().isoformat() and item['slot'] == slot)


def test_breakfast_can_be_planned_after_class_and_starts_at_its_actual_time(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')

    def choose(state, options):
        if state['time'] == at(30, 9, 59).astimezone(timezone.utc).isoformat():
            plans = [(key, value) for key, value in options.items() if value['status'] == 'planned']
            assert plans, 'class conflict must leave a future meal choice'
            assert all(datetime.fromisoformat(value['scheduled_for']) == at(30, 11, 40) for _, value in plans)
            return plans[0][0]
        return next(key for key, value in options.items() if value['status'] == 'eating')

    port = Port(meal=choose)
    asyncio.run(advance(store, port, at(30, 9, 59)))
    assert meal(store, at(30, 9, 59))['status'] == 'planned'
    asyncio.run(advance(store, port, at(30, 10, 30)))
    assert len(port.meal_calls) == 1
    asyncio.run(advance(store, port, at(30, 11, 40)))
    current = meal(store, at(30, 11, 40))
    assert current['status'] == 'eating' and datetime.fromisoformat(current['started_at']) == at(30, 11, 40)
    assert meal(store, at(30, 10))['status'] == 'planned'


@pytest.mark.parametrize('slot,hour,next_meal_hour', [('breakfast', 8, 12), ('lunch', 12, 18)])
def test_old_unstarted_plan_does_not_start_at_the_next_main_meal(slot, hour, next_meal_hour):
    from runtime.private_world.meal_lifecycle import options
    planned = {'status': 'planned', 'scheduled_for': at(30, hour, 30).isoformat(), 'food': '合成餐食'}
    choices = options(slot, planned, at(30, next_meal_hour))
    assert choices and all(value['status'] == 'skipped' for value in choices.values())


def test_skipped_meal_reconsidered_after_class_without_a_message_and_once_after_restart(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    initial = Port(meal=lambda state, options: 'skipped', path='skip')
    asyncio.run(advance(store, initial, at(30, 9, 59)))
    old = meal(store, at(30, 9, 59))
    keep = Port(meal=lambda state, options: 'keep_skipped')
    asyncio.run(advance(store, keep, at(30, 11, 40)))
    assert len(keep.meal_calls) == 1
    assert any(value.get('status') == 'eating' for value in keep.meal_calls[0][2]['meal']['criteria'].values())
    restarted = DailyLifeStore(store.path)
    asyncio.run(advance(restarted, keep, at(30, 11, 41)))
    assert len(keep.meal_calls) == 1
    assert meal(restarted, at(30, 11, 41)) == old


def test_new_recovery_can_start_skipped_lunch_without_rewriting_old_skip(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    initial = Port(meal=lambda state, options: 'skipped', path='skip')
    asyncio.run(advance(store, initial, at(30, 12)))
    old = meal(store, at(30, 12), 'lunch')
    now = at(30, 12, 45)
    state = store.snapshot(now)
    episode = asyncio.run(create(Port(path='refreshed'), 'recovery:1', now, 'rest', state))
    store.publish_day('recovery:1', {'location': '住处', 'activity': '休息', 'note': episode['result']['detail']},
                      [], occurred_at=now, activity_kind='rest', episode=episode)
    start = Port(meal=lambda state, options: next(key for key, value in options.items() if value['status'] == 'eating'))
    asyncio.run(advance(store, start, now + timedelta(minutes=1)))
    current = meal(store, now + timedelta(minutes=1), 'lunch')
    assert current['status'] == 'eating' and current['finished_at'] is None
    assert meal(store, at(30, 12), 'lunch') == old
    assert not any(item['status'] == 'eaten' for item in store.snapshot(now)['world']['meals'])


def publish_rest(store, source, now, path):
    state = store.snapshot(now)
    episode = asyncio.run(create(Port(path=path), source, now, 'rest', state))
    store.publish_day(source, {'location': '住处', 'activity': '休息', 'note': episode['result']['detail']},
                      [], occurred_at=now, activity_kind='rest', episode=episode)
    return episode


def test_nap_starts_then_finishes_through_jev_and_recovery_survives_midnight_and_restart(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    for day in (28, 29):
        store.record_exchange(f'reply:night:{day}', '合成问候', '合成回复', [],
                              received_at=at(day, 0), occurred_at=at(day, 3))
    started = at(29, 11)
    before = store.snapshot(started)['rhythm']
    history = store.snapshot(started - timedelta(minutes=1))
    assert before['rest'] == 'depleted'
    event = publish_rest(store, 'nap:start', started, 'nap_60')
    assert 'body_recovery' not in event['effects']
    sleeping = store.snapshot(started + timedelta(minutes=30))
    assert sleeping['rhythm']['authored_sleep']['status'] == 'sleeping'
    assert sleeping['rhythm']['rest'] == 'depleted'
    awake = at(29, 12)
    due = DailyLifeStore(store.path).snapshot(awake)
    assert due['rhythm']['authored_sleep']['status'] == 'due' and due['stale']
    finished = publish_rest(store, 'nap:finish', awake, 'nap_refreshed')
    assert finished['effects']['sleep_resolution']['source_id'] == 'nap:start'
    assert store.snapshot(awake)['rhythm']['rest'] == 'rested'
    next_day = DailyLifeStore(store.path).snapshot(at(30, 9))
    assert next_day['rhythm']['rest'] == 'rested'
    assert next_day['rhythm']['recovery']['source_id'] == 'nap:finish'
    assert 'authored_sleep' not in next_day['rhythm']
    data = decision_context({'time': at(30, 9).isoformat(), 'persona': '[]', 'projects': [],
                             'world': next_day['world'], 'rhythm': next_day['rhythm']})
    assert {'meal', 'reading', 'walk'} <= set(data['allowed_activity_kinds'])
    assert store.snapshot(started - timedelta(minutes=1)) == history


def test_new_night_after_recovery_adds_new_load_not_the_entire_old_load(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    store.record_exchange('reply:old:night', '合成问候', '合成回复', [],
                          received_at=at(28, 0), occurred_at=at(28, 3))
    publish_rest(store, 'rest:recovered', at(28, 12), 'refreshed')
    store.record_exchange('reply:new:night', '合成问候', '合成回复', [],
                          received_at=at(29, 1), occurred_at=at(29, 1, 2))
    body = DailyLifeStore(store.path).snapshot(at(29, 2))['rhythm']
    assert body['rest'] == 'tired'
    assert body['current_load_minutes'] == 42
    assert body['historical_rest']['load_minutes'] > body['current_load_minutes']


def test_fatigue_cannot_remove_meals_and_light_activities_from_jev_choices():
    now = at(29, 11)
    body = rhythm(now, [(at(28, 0), at(28, 3)), (at(29, 0), at(29, 3))])
    data = decision_context({'time': now.isoformat(), 'persona': '[]', 'projects': [],
                             'world': {'schedule': student_schedule(now)}, 'rhythm': body})
    assert {'rest', 'meal', 'reading', 'walk'} <= set(data['allowed_activity_kinds'])


def test_energy_recovery_does_not_clear_independent_illness():
    now = at(29, 11)
    state = rhythm(now, [])
    state['wellbeing'] = {'state': 'unwell', 'care': 'rest', 'summary': '独立记录的不适'}
    event = {'source_id': 'energy:1', 'occurred_at': now.isoformat(), 'activity_kind': 'rest',
             'result': {'status': 'completed'}, 'effects': {'body_recovery': {'rest': 'rested', 'baseline_load_minutes': 0}}}
    recovered = with_recovery(state, [event], now)
    assert recovered['rest'] == 'rested'
    assert recovered['wellbeing'] == state['wellbeing']
    data = decision_context({'time': now.isoformat(), 'persona': '[]', 'projects': [],
                             'world': {'schedule': student_schedule(now)}, 'rhythm': recovered})
    assert 'meal' in data['allowed_activity_kinds'] and 'practice' not in data['allowed_activity_kinds']


def test_fatigue_during_class_allows_rest_without_claiming_class_was_cancelled():
    now = at(30, 10)
    body = rhythm(now, [(at(29, 0), at(29, 3)), (at(30, 0), at(30, 3))])
    schedule = student_schedule(now)
    data = decision_context({'time': now.isoformat(), 'persona': '[]', 'projects': [],
                             'world': {'schedule': schedule}, 'rhythm': body})
    assert data['allowed_activity_kinds'] == ['class', 'rest']
    assert data['world']['schedule']['current_class'] == schedule['current_class']



def test_due_nap_is_ended_and_not_renewed(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    publish_rest(store, 'nap:start', at(29, 14), 'nap_30')
    state = store.snapshot(at(29, 14, 31))
    assert state['rhythm']['authored_sleep']['status'] == 'due'
    port = Port(path='nap_tired')
    value = asyncio.run(create(port, 'nap:end', at(29, 14, 31), 'rest', state))
    paths = port.calls[0][1]['paths']
    assert {'nap_refreshed', 'nap_tired'} <= set(paths)
    assert not {'nap_30', 'nap_60', 'nap_90'} & set(paths)
    assert value['effects']['sleep_resolution'] == {'source_id': 'nap:start'}
    assert 'sleep_plan' not in value['effects']

def test_correspondence_overlapping_nap_start_interrupts_it_and_does_not_offer_completed_nap(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    publish_rest(store, 'nap:start', at(29, 11), 'nap_60')
    store.record_exchange('reply:overlap', '合成问候', '合成回复', [],
                          received_at=at(29, 10, 59), occurred_at=at(29, 11, 1))
    state = store.snapshot(at(29, 11, 2))
    assert state['rhythm']['authored_sleep']['interrupted'] and state['stale']
    port = Port(path='nap_interrupted')
    value = asyncio.run(create(port, 'nap:interrupted', at(29, 11, 2), 'rest', state))
    assert 'nap_refreshed' not in port.calls[0][1]['paths']
    assert 'body_recovery' not in value['effects']
    assert value['effects']['sleep_resolution'] == {'source_id': 'nap:start'}


@pytest.mark.parametrize('failure', [None, 'decision', 'episode'])
def test_refresh_resolves_due_nap_before_meals_and_failure_preserves_pending_state(tmp_path, monkeypatch, failure):
    from runtime.private_world import daily_life_runtime, day_plan
    from runtime.reply import jev_questions

    store = DailyLifeStore(tmp_path / 'world.db')
    asyncio.run(advance(store, Port(meal=lambda s, o: 'skipped', path='skip'), at(29, 11)))
    publish_rest(store, 'nap:start', at(29, 11), 'nap_60')

    def choose(state, choices):
        assert state['rhythm']['rest'] == 'rested'
        assert 'authored_sleep' not in state['rhythm']
        return next(key for key, value in choices.items() if value['status'] == 'eating')

    class RefreshPort(Port):
        async def ask(self, state, questions, *, purpose):
            if failure == 'episode' and purpose == 'world-life-episode':
                self.calls.append((purpose, state, questions))
                raise RuntimeError('JEV_RESPONSE_INVALID')
            return await super().ask(state, questions, purpose=purpose)

    port = RefreshPort(meal=choose, path='nap_refreshed')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    monkeypatch.setattr(daily_life_runtime, 'configured_duties', lambda: None)

    async def no_plan(*args, **kwargs):
        return None

    monkeypatch.setattr(day_plan, 'ensure', no_plan)
    runtime = daily_life_runtime.DailyLifeRuntime(store, lambda: None, lambda: '[]')
    decisions = []

    async def decide(prompt, data, source, **kwargs):
        decisions.append(data)
        assert data['allowed_activity_kinds'] == ['rest']
        if failure == 'decision':
            raise RuntimeError('JEV_RESPONSE_INVALID')
        return {'activity': {'kind': 'rest', 'place_id': 'home', 'focus': ''},
                'meal': None, 'project': None, 'development': []}

    runtime._complete = decide
    asyncio.run(runtime.refresh(at(29, 12)))
    state = store.snapshot(at(29, 12))
    assert len(decisions) == 1
    if failure:
        assert not port.meal_calls
        assert state['rhythm']['authored_sleep']['status'] == 'due'
        assert state['rhythm']['rest'] != 'rested' or 'recovery' not in state['rhythm']
        assert not any(item['slot'] == 'lunch' for item in state['world']['meals'])
        assert runtime.error_code == 'DAILY_LIFE_GENERATION_UNAVAILABLE'
    else:
        assert [call[0] for call in port.calls] == ['world-life-episode', 'world-meal-lifecycle', 'world-life-episode']
        assert state['current']['activity_kind'] == 'meal'
        assert meal(store, at(29, 12), 'lunch')['status'] == 'eating'
        assert runtime.error_code is None
    restarted = daily_life_runtime.DailyLifeRuntime(DailyLifeStore(store.path), lambda: None, lambda: '[]')
    restarted._complete = decide
    asyncio.run(restarted.refresh(at(29, 12, 1)))
    assert len(decisions) == 1


def test_interrupted_nap_is_resolved_even_inside_planned_night_rest(tmp_path, monkeypatch):
    from runtime.private_world import daily_life_runtime, day_plan
    from runtime.reply import jev_questions

    store = DailyLifeStore(tmp_path / 'world.db')
    publish_rest(store, 'nap:start', at(29, 8), 'nap_30')
    store.record_exchange('reply:interruption', '合成问候', '合成回复', [],
                          received_at=at(29, 8, 5), occurred_at=at(29, 8, 6))
    before = store.snapshot(at(29, 8, 7))
    assert before['rhythm']['phase'] == 'interrupted_rest'
    port = Port(meal=lambda state, options: 'skipped', path='nap_interrupted')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    monkeypatch.setattr(daily_life_runtime, 'configured_duties', lambda: None)

    async def no_plan(*args, **kwargs):
        return None

    monkeypatch.setattr(day_plan, 'ensure', no_plan)
    runtime = daily_life_runtime.DailyLifeRuntime(store, lambda: None, lambda: '[]')

    async def decide(*args, **kwargs):
        return {'activity': {'kind': 'rest', 'place_id': 'home', 'focus': ''},
                'meal': None, 'project': None, 'development': []}

    runtime._complete = decide
    asyncio.run(runtime.refresh(at(29, 8, 7)))
    assert runtime.error_code is None
    assert 'authored_sleep' not in store.snapshot(at(29, 8, 7))['rhythm']
    assert port.calls[0][0] == 'world-life-episode'


def test_unrelated_life_history_does_not_evict_energy_recovery(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    store.record_exchange('reply:old', '合成问候', '合成回复', [],
                          received_at=at(29, 0), occurred_at=at(29, 3))
    publish_rest(store, 'rest:recovered', at(29, 12), 'refreshed')
    for index in range(130):
        now = at(29, 12) + timedelta(seconds=index+1)
        source = f'reading:{index}'
        episode = asyncio.run(create(Port(), source, now, 'reading', store.snapshot(now)))
        store.publish_day(source, {'location': '住处', 'activity': '阅读', 'note': episode['result']['detail']},
                          [], occurred_at=now, activity_kind='reading', episode=episode)
    state = DailyLifeStore(store.path).snapshot(at(29, 13))
    assert state['rhythm']['recovery']['source_id'] == 'rest:recovered'
    assert state['rhythm']['rest'] == 'rested'


@pytest.mark.parametrize('invalid', ['duration', 'future_credit', 'unknown_resolution'])
def test_invalid_sleep_effects_rollback_publication(tmp_path, invalid):
    store = DailyLifeStore(tmp_path / 'world.db')
    now = at(29, 11)
    episode = asyncio.run(create(Port(path='nap_60'), 'invalid:nap', now, 'rest', store.snapshot(now)))
    if invalid == 'duration':
        episode['effects']['sleep_plan']['duration_minutes'] = 0
    elif invalid == 'future_credit':
        episode['effects']['body_recovery'] = {'rest': 'rested', 'baseline_load_minutes': 0}
    else:
        episode['effects']['sleep_resolution'] = {'source_id': 'missing:nap'}
    with pytest.raises(ValueError, match='LIFE_EPISODE_(SLEEP|RECOVERY)_INVALID'):
        store.publish_day('invalid:nap', {'location': '住处', 'activity': '休息', 'note': '合成补觉'}, [],
                          occurred_at=now, activity_kind='rest', episode=episode)
    assert not store.has_source('invalid:nap')
