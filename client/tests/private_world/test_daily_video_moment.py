"""Her current arrived place is a daily-video candidate, offered like a photo."""
import time
from datetime import datetime, timezone, timedelta
import re

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.personal_chat.daily_video import LOCAL

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
    request = select_candidate(dict(event_id=candidates[0]['event_id'], spoken_text='刚练完一遍，手有点酸。'), candidates)
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


def moment_store(tmp_path, now=NOW):
    store = DailyLifeStore(tmp_path/'world.db')
    store.publish_day('synthetic:reading', dict(location='home', activity='reading', note='A book.',
        place=place('home', 'home')), [], activity_kind='reading', occurred_at=now)
    return store


def request_for(store, now):
    from runtime.personal_chat.daily_video import select_candidate
    candidates = worker_for(store).candidates(now=now)
    return select_candidate(dict(event_id=candidates[0]['event_id'], spoken_text='A quiet moment.'), candidates)


@pytest.mark.real_user_idle
def test_repeat_interval_daily_limit_and_stable_offer(tmp_path):
    start = NOW.replace(hour=11)
    store = moment_store(tmp_path, start)
    identities = []
    for index in range(4):
        now = start + timedelta(minutes=30*index)
        request = request_for(store, now)
        assert request_for(store, now)['event_id'] == request['event_id']
        assert re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request['event_id'])
        assert request['event_id'] != 'synthetic:reading'
        identities.append(request['event_id'])
        assert store.reserve_daily_video_share(request, now=now)
        assert not store.reserve_daily_video_share(request, now=now)
        assert worker_for(store).candidates(now=now+timedelta(minutes=29, seconds=59)) == []
        assert store.daily_video_event_facts(request)['activity'] == 'reading'
        assert store.reserve_daily_video_delivery(request, task_id=f'task-{index}', now=now)
        assert not store.reserve_daily_video_delivery(request, task_id=f'task-{index}', now=now)
    assert len(set(identities)) == 4
    assert worker_for(store).candidates(now=start+timedelta(hours=2)) == []


def test_midnight_keeps_current_source_and_cooldown(tmp_path):
    before = NOW.replace(hour=15, minute=50)
    store = moment_store(tmp_path, before)
    first = request_for(store, before)
    store.reserve_daily_video_share(first, now=before)
    midnight = before + timedelta(minutes=10)
    assert worker_for(store).candidates(now=midnight) == []
    later = midnight + timedelta(minutes=20)
    second = request_for(store, later)
    assert second['target_date'] == later.astimezone(LOCAL).date().isoformat()
    assert second['event_id'] != first['event_id']
    assert store.daily_video_can_prepare(second, now=later)
    assert store.reserve_daily_video_share(second, now=later)


def test_mapped_share_cannot_use_a_different_or_stale_source(tmp_path):
    store = moment_store(tmp_path)
    request = request_for(store, NOW)
    store.publish_day('synthetic:moved', dict(location='studio', activity='reading', note='Another room.',
        place=place('studio', 'campus')), [], activity_kind='reading', occurred_at=NOW+timedelta(minutes=1))
    assert not store.daily_video_can_prepare(request, now=NOW+timedelta(minutes=1))
    with pytest.raises(ValueError, match='DAILY_VIDEO_SOURCE_UNAVAILABLE'):
        store.reserve_daily_video_delivery(request, task_id='task', now=NOW+timedelta(minutes=1))
    assert worker_for(store).candidates(now=NOW+timedelta(days=1)) == []


def test_legacy_reservation_counts_towards_repeat_budget(tmp_path):
    store = moment_store(tmp_path)
    legacy = dict(version=1, event_id='synthetic:reading', target_date='2026-10-11',
        event_at=NOW.isoformat(), event_kind='moment', scene_id='daily_place', certainty='live',
        spoken_text='A previous clip.', place=place('home', 'home'))
    assert store.reserve_daily_video_delivery(legacy, task_id='old-task', now=NOW)
    assert worker_for(store).candidates(now=NOW+timedelta(minutes=29)) == []
    new = request_for(store, NOW+timedelta(minutes=30))
    assert new['event_id'] != legacy['event_id']
    assert store.daily_video_source_id(new['event_id']) == legacy['event_id']
    with store._db() as db:
        assert db.execute('SELECT share_index FROM life_video_moments WHERE event_id=?', (new['event_id'],)).fetchone()[0] == 2


@pytest.mark.real_user_idle
def test_midnight_resets_full_daily_budget_for_a_still_live_source(tmp_path):
    start = NOW.replace(hour=14)
    store = moment_store(tmp_path, start)
    ids = []
    for index in range(4):
        now = start+timedelta(minutes=30*index)
        request = request_for(store, now)
        ids.append(request['event_id'])
        store.reserve_daily_video_share(request, now=now)
    assert worker_for(store).candidates(now=start+timedelta(minutes=119)) == []
    midnight = start+timedelta(hours=2)
    next_day = request_for(store, midnight)
    assert next_day['event_id'] not in ids
    assert store.reserve_daily_video_share(next_day, now=midnight)
    with store._db() as db:
        assert db.execute('SELECT share_index FROM life_video_moments WHERE event_id=?', (next_day['event_id'],)).fetchone()[0] == 1


def test_parallel_candidate_reads_and_reservations_use_one_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = moment_store(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        offers = list(pool.map(lambda _: request_for(store, NOW), range(2)))
        assert offers[0] == offers[1]
        claims = list(pool.map(lambda request: store.reserve_daily_video_share(request, now=NOW), offers))
    assert sorted(claims) == [False, True]
