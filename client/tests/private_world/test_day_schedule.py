"""The day is laid out once, then followed by the clock: no request while time passes."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from runtime.diary.diary import WRITE_AFTER_HOUR
from runtime.private_world import day_schedule, daily_life_runtime
from runtime.private_world.world_decision import compile_decision
from tests.private_world.day_world import MONDAY, Port, at, make_world, moments, tick

TUESDAY = '2026-10-13'


def schedule(store, day=MONDAY):
    return day_schedule.load(store, day)


def starts(items):
    return [(item['type'], day_schedule.hhmm(day_schedule.at(item['start'])), day_schedule.hhmm(day_schedule.at(item['end']))) for item in items]


def test_the_day_is_laid_out_once_when_the_diary_is_due_with_one_request(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    tick(runtime, at(WRITE_AFTER_HOUR - 1, 59))
    assert schedule(store) is None and port.calls == []
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    assert port.purposes() == ['world-day-schedule'] and schedule(store) is not None
    for when in (at(WRITE_AFTER_HOUR, 5), at(9, 0), at(15, 30), at(23, 0)):
        tick(runtime, when)
    assert port.purposes() == ['world-day-schedule']
    assert runtime.error_code is None and runtime.schedule_error_code is None


def test_the_day_is_laid_out_at_start_when_it_has_no_schedule_yet(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    tick(runtime, at(14, 20))
    assert port.purposes() == ['world-day-schedule']
    # Only what is still ahead is asked about: the classes and meals already past are not offered as choices.
    asked = {key for key in port.calls[0]['questions'] if key.startswith('block_')}
    assert asked and all(day_schedule.at(item['end']) > at(14, 20)
                         for item in schedule(store)['items'] if item['type'] == 'free' and f"block_{item['id']}" in asked)


def test_blocks_follow_the_rhythm_and_the_timetable_not_the_model(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    items = schedule(store)['items']
    fixed = [(kind, begin, end) for kind, begin, end in starts(items) if kind != 'free']
    assert fixed == [('meal', '08:30', '09:30'), ('class', '09:00', '10:40'), ('meal', '12:00', '13:00'),
                     ('class', '14:00', '15:00'), ('meal', '18:00', '19:00'),
                     ('bath_started', '23:30', '23:50'), ('bath_finished', '23:50', '00:00')]
    free = [item for item in items if item['type'] == 'free']
    assert free and all(timedelta(minutes=15) <= day_schedule.at(i['end']) - day_schedule.at(i['start']) <= timedelta(hours=2) for i in free)
    others = sorted((i for i in items if i['type'] != 'meal'), key=lambda i: i['start'])
    assert all(day_schedule.at(a['end']) <= day_schedule.at(b['start']) for a, b in zip(others, others[1:]))
    covered = sorted((day_schedule.at(i['start']), day_schedule.at(i['end'])) for i in items)
    reach = covered[0][1]
    for begin, end in covered[1:]:
        assert begin - reach <= timedelta(minutes=15)
        reach = max(reach, end)
    # The model was asked only about the free blocks, meals and projects, never about times.
    assert all(key.split('_')[0] in {'block', 'meal', 'project'} for key in port.calls[0]['questions'])


def test_every_stored_block_passes_the_decision_contract_at_its_own_time(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    checked = 0
    for item in schedule(store)['items']:
        if item['type'] == 'meal':
            continue
        assert item['state'] == 'ready'
        overlay = {'authored_bath': {'source_id': 'x', 'started_at': item['start']}} if item['type'] == 'bath_finished' else None
        data = day_schedule.contract_data(store, day_schedule.at(item['start']), runtime.persona(), rhythm=overlay)
        compile_decision(deepcopy(item['result']), data)
        checked += 1
    assert checked >= 10


def test_the_same_specific_activity_is_not_chosen_twice_while_another_is_offered(tmp_path, monkeypatch):
    def always_the_same(state, questions, purpose):
        if purpose == 'world-day-schedule':
            return {key: (next(k for k in question['criteria'] if k.startswith('practice_0')) if key.startswith('block_') else
                          'none' if key.startswith('project_') else next(iter(question['criteria'])))
                    for key, question in questions.items()}

    store, runtime, port = make_world(tmp_path, monkeypatch, Port(always_the_same))
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    chosen = [(i['result']['activity']['kind'], i['result']['activity']['focus']) for i in schedule(store)['items'] if i['type'] == 'free']
    assert len(set(chosen)) == min(len(chosen), 3)  # three practice foci exist, then repeats are all that is left


def test_a_block_that_fails_the_contract_is_left_to_a_live_decision(tmp_path, monkeypatch):
    real = daily_life_runtime.compile_decision

    def reject_reading(value, data):
        if value['activity']['kind'] == 'reading':
            raise ValueError('DAILY_LIFE_DECISION_CLASS_CONFLICT')
        return real(value, data)

    def policy(state, questions, purpose):
        if purpose == 'world-day-schedule':
            return {key: (next(k for k, v in question['criteria'].items() if v.startswith('阅读')) if key == 'block_i2' else
                          'none' if key.startswith('project_') else next(iter(question['criteria'])))
                    for key, question in questions.items()}
        if purpose == 'world-decision':
            rest = next(k for k, v in questions['activity']['criteria'].items() if v['kind'] == 'rest')
            return {key: (rest if key == 'activity' else 'none' if key in {'project', 'next_activity'} else next(iter(q['criteria'])))
                    for key, q in questions.items()}

    store, runtime, port = make_world(tmp_path, monkeypatch, Port(policy))
    monkeypatch.setattr(daily_life_runtime, 'compile_decision', reject_reading)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    tick(runtime, at(10, 40))
    assert port.count('world-decision') == 1  # one live decision stands in for the block
    first = moments(store)[-1]
    assert first['activity_kind'] == 'rest' and not first['source_id'].startswith('sched:')
    tick(runtime, at(10, 50))
    tick(runtime, at(11, 30))
    assert port.count('world-decision') == 1  # the block is settled, not retried every minute


def test_a_failed_judgment_falls_back_to_live_decisions_and_the_next_day_recovers(tmp_path, monkeypatch):
    broken = {'on': True}

    def policy(state, questions, purpose):
        if purpose == 'world-day-schedule' and broken['on']:
            raise RuntimeError('JEV_UNAVAILABLE')
        if purpose == 'world-decision':
            rest = next(k for k, v in questions['activity']['criteria'].items() if v['kind'] == 'rest')
            return {key: (rest if key == 'activity' else 'none' if key in {'project', 'next_activity'} else next(iter(q['criteria'])))
                    for key, q in questions.items()}

    store, runtime, port = make_world(tmp_path, monkeypatch, Port(policy))
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    assert schedule(store) is None and runtime.schedule_error_code == 'JEV_UNAVAILABLE'
    assert day_schedule.attempts(store, MONDAY)[0] == 1
    tick(runtime, at(WRITE_AFTER_HOUR, 3))  # inside the back-off: no new request
    assert port.count('world-day-schedule') == 1
    tick(runtime, at(WRITE_AFTER_HOUR, 10))
    tick(runtime, at(WRITE_AFTER_HOUR + 1, 0))
    tick(runtime, at(WRITE_AFTER_HOUR + 2, 0))
    tick(runtime, at(WRITE_AFTER_HOUR + 3, 0))
    assert port.count('world-day-schedule') == day_schedule.ATTEMPT_LIMIT  # then it stops trying for the day
    # Her life does not freeze: the usual per-moment decision carries the day.
    tick(runtime, at(11, 0))
    assert port.count('world-decision') == 1 and moments(store)[-1]['activity_kind'] == 'rest'
    broken['on'] = False
    tick(runtime, at(WRITE_AFTER_HOUR, 0, TUESDAY))
    assert schedule(store, TUESDAY) is not None
    assert port.count('world-day-schedule') == day_schedule.ATTEMPT_LIMIT + 1


def test_following_the_clock_publishes_each_block_whole_and_makes_no_request(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    before = len(port.calls)
    tick(runtime, at(9, 0))
    assert moments(store)[-1]['activity_kind'] == 'class'
    tick(runtime, at(10, 41))
    block = moments(store)[-1]
    assert block['source_id'].startswith(f'sched:{MONDAY}:') and block['valid_until']
    # The block lasts until its own end, not for a fixed cadence.
    assert not store.snapshot(at(11, 59))['stale'] and store.snapshot(at(12, 0))['stale']
    for minute in range(0, 600, 7):
        tick(runtime, at(9, 0) + timedelta(minutes=minute))
    assert len(port.calls) == before


def test_a_block_is_published_only_once_and_never_for_a_stretch_already_over(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    tick(runtime, at(15, 10))
    tick(runtime, at(15, 11))
    tick(runtime, at(15, 12))
    # Only the stretch she is in is published; nothing is invented for the hours before it.
    scheduled = [m for m in moments(store) if m['source_id'].startswith('sched:')]
    assert [m['source_id'] for m in scheduled] == [f'sched:{MONDAY}:i6:0']
    assert day_schedule.at(scheduled[0]['at']) == at(15, 10)


def test_bath_and_shopping_leave_the_record_the_store_requires_without_asking(tmp_path, monkeypatch):
    def shopping(state, questions, purpose):
        if purpose == 'world-day-schedule':
            return {key: (next((k for k, v in question['criteria'].items() if v.startswith('买日常用品')), next(iter(question['criteria'])))
                          if key == 'block_i4' else 'none' if key.startswith('project_') else next(iter(question['criteria'])))
                    for key, question in questions.items()}

    store, runtime, port = make_world(tmp_path, monkeypatch, Port(shopping))
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    plan = {item['id']: item for item in schedule(store)['items']}
    assert plan['i4']['result']['activity']['kind'] == 'shopping'
    tick(runtime, at(12, 0))
    tick(runtime, at(12, 50))
    tick(runtime, day_schedule.at(plan['i4']['start']) + timedelta(minutes=1))
    tick(runtime, at(23, 31))
    tick(runtime, at(23, 51))
    with store._db() as db:
        episodes = {kind: status for kind, status in db.execute(
            "SELECT json_extract(payload,'$.activity_kind'), json_extract(payload,'$.result.status') FROM life_episodes")}
    assert episodes == {'shopping': 'completed', 'bath_started': 'partial', 'bath_finished': 'completed'}
    assert port.purposes() == ['world-day-schedule']


def test_a_meal_with_preparation_moves_along_the_clock_without_asking(tmp_path, monkeypatch):
    def cook_lunch(state, questions, purpose):
        if purpose == 'world-day-schedule':
            return {key: (next(k for k, v in question['criteria'].items() if '自己做' in v) if key == 'meal_lunch' else
                          'none' if key.startswith('project_') else next(iter(question['criteria'])))
                    for key, question in questions.items()}

    store, runtime, port = make_world(tmp_path, monkeypatch, Port(cook_lunch))
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    tick(runtime, at(12, 0))
    assert moments(store)[-1]['activity'] == '正在准备餐食午餐'
    tick(runtime, at(12, 24))
    assert moments(store)[-1]['activity'] == '正在准备餐食午餐'
    tick(runtime, at(12, 26))
    assert moments(store)[-1]['activity'] == '正在吃午餐' and '自己炒的青菜肉丝面' in moments(store)[-1]['note']
    tick(runtime, at(12, 50))
    assert [m['status'] for m in store.snapshot(at(12, 50))['world']['meals'] if m['slot'] == 'lunch'] == ['eating']
    tick(runtime, at(12, 56))
    assert [m['status'] for m in store.snapshot(at(12, 56))['world']['meals'] if m['slot'] == 'lunch'] == ['eaten']
    assert port.purposes() == ['world-day-schedule']


def test_a_meal_that_cannot_be_ready_before_a_lesson_is_not_offered(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    # 8:30 with a lesson at 9:00: only a meal that needs no preparation fits, so there is nothing to choose.
    assert 'meal_breakfast' not in port.calls[0]['questions']
    breakfast = next(i for i in schedule(store)['items'] if i['type'] == 'meal' and i['slot'] == 'breakfast')
    assert breakfast['entry']['food'] == '二食堂的豆浆油条' and breakfast['entry']['mode'] == 'canteen'
    assert 'meal_lunch' in port.calls[0]['questions']


def _yesterdays_project(store):
    store.publish_day('day:yesterday', {'location': '家里', 'activity': '练琴：左手两处衔接', 'note': '在家里练琴。'},
                      [dict(id='life:left-hand', title='左手练习', detail='本次：练琴。', status='ongoing')],
                      occurred_at=at(16, 0, '2026-10-11'), activity_kind='practice')


def test_yesterdays_project_progress_is_settled_in_the_same_judgment(tmp_path, monkeypatch):
    def policy(state, questions, purpose):
        if purpose == 'world-day-schedule':
            return {key: ('completed' if key.startswith('review_') else 'none' if key.startswith('project_') and 'none' in q['criteria']
                          else next(iter(q['criteria']))) for key, q in questions.items()}

    store, runtime, port = make_world(tmp_path, monkeypatch, Port(policy))
    _yesterdays_project(store)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    assert port.purposes() == ['world-day-schedule']
    assert [key for key in port.calls[0]['questions'] if key.startswith('review_')] == ['review_0']
    assert schedule(store)['reviews'][0]['status'] == 'completed'
    tick(runtime, at(9, 0))  # the first block of the day carries the settlement
    project = next(p for p in store.snapshot(at(9, 1))['projects'] if p['id'] == 'life:left-hand')
    assert project['status'] == 'completed' and '昨天：练琴：左手两处衔接' in project['detail']
    assert schedule(store)['reviews'] == []
    assert len(port.calls) == 1


def test_a_project_nothing_settled_just_continues_and_open_time_scopes_are_read_once_a_day(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    _yesterdays_project(store)
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    assert 'project_time_0_scope' in port.calls[0]['questions'] and 'project_time_contexts' in port.calls[0]['state']
    tick(runtime, at(9, 0))
    project = next(p for p in store.snapshot(at(9, 1))['projects'] if p['id'] == 'life:left-hand')
    assert project['status'] == 'ongoing' and project.get('time_scope') == 'open'


def test_a_late_bath_after_midnight_is_followed_from_yesterdays_schedule(tmp_path, monkeypatch):
    store, runtime, port = make_world(tmp_path, monkeypatch)
    with store._db() as db:
        db.execute('INSERT INTO life_routine_days VALUES (?,?)', (MONDAY, 60))  # bedtime an hour later than usual
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    late = [i for i in schedule(store)['items'] if i['type'] == 'bath_started'][0]
    assert day_schedule.at(late['start']) == at(0, 30, TUESDAY)
    tick(runtime, at(0, 31, TUESDAY))  # the new day is not laid out until the diary hour
    assert schedule(store, TUESDAY) is None
    assert moments(store)[-1]['activity_kind'] == 'bath_started'
    tick(runtime, at(0, 51, TUESDAY))
    assert moments(store)[-1]['activity_kind'] == 'bath_finished'
    assert port.purposes() == ['world-day-schedule']


@pytest.mark.parametrize('sources', [0, 2])
def test_yesterdays_experience_appraisal_is_one_batch_call_or_none_at_all(tmp_path, monkeypatch, sources):
    from runtime.private_world.character_development import digest
    from runtime.private_world.daily_life import DailyLifeStore
    kinds = []

    class Duty:
        async def evaluate(self, kind, packet):
            kinds.append(kind)
            return SimpleNamespace(decision={'candidates': []}, error_code=None, input_digest=digest(packet))

    store, runtime, port = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(daily_life_runtime, 'configured_duties', lambda: Duty())
    monkeypatch.setattr(DailyLifeStore, 'development_world_assessment', lambda self, now: {
        'as_of': now.isoformat(), 'sources': [{'source_id': f'day:{i}', 'occurred_at': at(16, 0, '2026-10-11').isoformat()} for i in range(sources)]})
    tick(runtime, at(WRITE_AFTER_HOUR, 0))
    for when in (at(9, 0), at(10, 41), at(15, 10), at(23, 31)):
        tick(runtime, when)
    # Once for the day when there is something to appraise, however many moments were published.
    assert kinds == (['world'] if sources else [])
    assert port.purposes() == ['world-day-schedule']
