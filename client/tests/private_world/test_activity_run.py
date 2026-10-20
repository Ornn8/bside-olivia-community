from datetime import timedelta

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.world_decision import current_activity_run, decision_context
from tests.private_world.test_meal_lifecycle import at

BACH = '练琴：巴赫《平均律》第一册第五首前奏曲'


def publish(store, name, minute, activity, kind='practice'):
    store.publish_day(name, dict(activity=activity, location='家里', note='继续。'), [],
                      occurred_at=at(10) + timedelta(minutes=minute), activity_kind=kind)


def test_the_decision_sees_how_long_the_current_activity_has_lasted(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    for index, minute in enumerate((40, 60, 89, 120, 125, 150, 179)):
        publish(store, f'day:p{index}', minute, BACH)
    world = store.snapshot(at(13))['world']
    run = current_activity_run({'time': at(12, 59).isoformat(), 'world': world})
    assert run['activity'] == BACH and run['minutes'] == 139
    context = decision_context(dict(time=at(12, 59).isoformat(), persona='[]', projects=[], world=world,
                                    rhythm={'phase': 'day', 'rest': 'rested'}))
    assert context['current_activity_run']['minutes'] == 139 and 'today_activities' not in context['world']


def test_another_activity_or_a_long_gap_starts_a_new_run(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    publish(store, 'day:a', 0, BACH)
    publish(store, 'day:b', 30, '阅读：和声学教材第五章', 'reading')
    publish(store, 'day:c', 60, BACH)
    world = store.snapshot(at(13))['world']
    assert current_activity_run({'time': (at(11, 30)).isoformat(), 'world': world})['minutes'] == 30
    store2 = DailyLifeStore(tmp_path / 'world2.db')
    publish(store2, 'day:a', 0, BACH)
    publish(store2, 'day:b', 120, BACH)
    world2 = store2.snapshot(at(13))['world']
    assert current_activity_run({'time': (at(12, 30)).isoformat(), 'world': world2})['minutes'] == 30
    # Nothing recorded for a long while: no current run.
    assert current_activity_run({'time': (at(15)).isoformat(), 'world': world2}) is None
    assert current_activity_run({'time': at(12).isoformat(), 'world': {}}) is None
