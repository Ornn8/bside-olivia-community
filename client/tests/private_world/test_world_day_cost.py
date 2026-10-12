"""Her whole day costs one judgment and one small check per exchange; time passing costs nothing."""
from datetime import timedelta

from runtime.diary.diary import WRITE_AFTER_HOUR
from tests.private_world.day_world import MONDAY, Port, at, exchange, make_world, moments, tick

# (time, what the user wrote, her reply, what the check should find)
EXCHANGES = [
    ((8, 50), '早安，睡得好吗', '早安，刚起来。', 'none'),
    ((10, 5), '课上还顺利吗', '和声课有点难，不过还好。', 'none'),
    ((11, 20), '中午吃什么呀', '还没想好。', 'none'),
    ((12, 20), '饭吃了吗', '正在吃，味道不错。', 'none'),
    ((12, 40), '吃完了吗', '吃完了，碗也洗了。', 'meal_finished'),
    ((14, 30), '下午有空吗', '下午有钢琴课，课后有空。', 'none'),
    ((16, 5), '要不要出来走走', '好啊，那我现在就出门散步。', 'replace_now'),
    ((17, 10), '今晚想吃什么', '今晚不想自己做了，在外面随便买点吧。', 'dinner_change'),
    ((18, 40), '好吃吗', '嗯，还可以。', 'none'),
    ((20, 0), '周末一起去看展吧', '好呀，约在明天下午。', 'tomorrow'),
    ((21, 30), '在做什么', '在看书。', 'none'),
    ((22, 45), '早点睡', '嗯，我先去收拾一下。', 'none'),
]


def scripted(script):
    def policy(state, questions, purpose):
        if purpose != 'exchange-world-check':
            return None
        action = script['now']
        answers = {key: 'none' for key in questions}
        criteria = questions['change']['criteria']
        if action == 'replace_now' and 'replace' in criteria:
            target = next(k for k, v in questions['target']['criteria'].items() if '正在进行' in v)
            answers.update(change='replace', target=target,
                           activity=next(k for k in questions['activity']['criteria'] if k.startswith('walk')))
        elif action == 'meal_finished' and any(k.startswith('finished_') for k in questions.get('meal', {}).get('criteria', {})):
            answers.update(change='meal', meal=next(k for k in questions['meal']['criteria'] if k.startswith('finished_')))
        elif action == 'dinner_change' and 'switch_dinner_1' in questions.get('meal', {}).get('criteria', {}):
            answers.update(change='meal', meal='switch_dinner_1')
        return answers

    return policy


def run_day(tmp_path, monkeypatch, exchanges):
    script = {'now': None}
    store, runtime, port = make_world(tmp_path, monkeypatch, Port(scripted(script)))
    pending = {at(*when): (user, reply, action) for when, user, reply, action in exchanges}
    now, end, counter, laid = at(0, 30), at(0, 30) + timedelta(hours=24), 0, False
    stale_minutes = lit_minutes = 0
    while now < end:
        due = [when for when in sorted(pending) if when <= now]
        before = len(port.calls)
        tick(runtime, now)
        if not laid and now >= at(WRITE_AFTER_HOUR, 0):
            laid = True
            assert len(port.calls) == before + 1  # the one judgment of the day
        else:
            assert len(port.calls) == before, f'time passing asked something at {now}'
        for when in due:
            user, reply, action = pending.pop(when)
            script['now'] = action
            counter += 1
            exchange(runtime, counter, when, user, reply)
            script['now'] = None
        if at(8, 30) <= now < at(23, 30):
            lit_minutes += 1
            stale_minutes += store.snapshot(now)['stale']
        now += timedelta(minutes=5)
    return store, runtime, port, stale_minutes / lit_minutes


def test_a_whole_day_is_one_judgment_plus_one_check_per_exchange_and_nothing_for_time_passing(tmp_path, monkeypatch):
    store, runtime, port, stale = run_day(tmp_path, monkeypatch, EXCHANGES)
    assert port.count('world-day-schedule') == 1
    assert port.count('exchange-world-check') == len(EXCHANGES)
    assert set(port.purposes()) == {'world-day-schedule', 'exchange-world-check'}  # no per-moment decision, episode or meal request
    assert len(port.calls) == 1 + len(EXCHANGES)
    assert runtime.error_code is None and runtime.schedule_error_code is None and runtime.world_check_error_code is None
    # She still lives the day: classes, meals, walking, bath and the activities between.
    kinds = [m.get('activity_kind') for m in moments(store)]
    assert {'class', 'meal', 'walk', 'bath_started', 'bath_finished'} <= set(kinds)
    meals = {m['slot']: m['status'] for m in store.snapshot(at(23, 59))['world']['meals'] if m['date'] == MONDAY}
    assert meals == {'breakfast': 'eaten', 'lunch': 'eaten', 'dinner': 'eaten'}
    assert stale < 0.1  # almost always a current activity she is really in


def test_a_day_without_a_single_message_costs_one_request(tmp_path, monkeypatch):
    store, runtime, port, _ = run_day(tmp_path, monkeypatch, [])
    assert port.purposes() == ['world-day-schedule']
