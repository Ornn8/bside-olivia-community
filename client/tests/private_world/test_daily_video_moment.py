"""Her current arrived place is a daily-video candidate, offered like a photo."""
import time
from datetime import datetime, timezone

from runtime.private_world.daily_life import DailyLifeStore

NOW = datetime(2026, 10, 11, 7, tzinfo=timezone.utc)


def place(name, place_id):
    return dict(place_id=place_id, name=name, setting=name+'，沿用已有布局与陈设。', stage='arrived')


def worker_for(store, *, moments=True):
    from runtime.personal_chat.daily_video import DailyVideoWorker
    worker = DailyVideoWorker.__new__(DailyVideoWorker)
    worker.scenes = [dict(scene_id='daily_place', event_kinds=['housework', 'meal', 'shopping', 'walk'], locations=[])]
    worker.moments = moments
    worker.capabilities_at = time.monotonic()
    worker.event_store = store
    worker.get = lambda _: None
    worker.preparation = lambda _: None
    return worker


def test_current_place_is_a_moment_only_when_the_server_announces_it(tmp_path):
    from runtime.personal_chat.daily_video import select_candidate
    store = DailyLifeStore(tmp_path/'world.db')
    store.publish_day('synthetic:practice', dict(location='学校琴房', activity='练琴', note='把下周要回课的曲子过一遍。',
        place=place('学校琴房', 'campus')), [], activity_kind='practice', occurred_at=NOW)
    assert worker_for(store, moments=False).candidates(now=NOW) == []
    candidates = worker_for(store).candidates(now=NOW)
    assert [(c['event_kind'], c['scene_id']) for c in candidates] == [('moment', 'daily_place')]
    request = select_candidate(dict(event_id='synthetic:practice', spoken_text='刚练完一遍，手有点酸。'), candidates)
    assert request['place']['name'] == '学校琴房'
    assert store.daily_video_can_prepare(request, now=NOW)
    assert store.daily_video_event_matches(request, now=NOW)
    assert not store.daily_video_can_prepare({**request, 'place': place('商场', 'shop')}, now=NOW)
    assert not store.daily_video_event_matches(request, now=NOW.replace(hour=6))


def test_bath_and_specific_events_never_become_generic_moments(tmp_path):
    store = DailyLifeStore(tmp_path/'world.db')
    store.publish_day('synthetic:walk', dict(location='公园湖畔', activity='散步', note='绕着湖边走走。',
        place=place('公园湖畔', 'neighborhood')), [], activity_kind='walk', occurred_at=NOW)
    assert [c['event_kind'] for c in worker_for(store).candidates(now=NOW)] == ['walk']
    assert [s['event_kind'] for s in store.daily_video_sources(now=NOW, moment=True)] == ['walk']


def test_a_moment_without_an_arrived_place_is_not_offered(tmp_path):
    store = DailyLifeStore(tmp_path/'world.db')
    store.publish_day('synthetic:read', dict(location='家里', activity='看书', note='窝在沙发上看小说。'),
                      [], activity_kind='reading', occurred_at=NOW)
    assert store.daily_video_sources(now=NOW, moment=True) == []
