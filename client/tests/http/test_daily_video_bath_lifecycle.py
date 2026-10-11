"""Actual bath starts prepare once; elapsed time never authorizes publication."""
import asyncio
from datetime import timedelta
import time

import pytest

from runtime.personal_chat.daily_video import DailyVideoWorker, capture_caption, select_candidate, validate_input
from runtime.private_world.life_episode import create
from tests.http.test_daily_video import API, NOW, payload, service, world


class EpisodePort:
    async def ask(self, state, questions, **kwargs):
        return dict(trigger='own_activity', experience='0:ordinary:none')


def publish_bath(store, kind, source, when):
    episode = asyncio.run(create(EpisodePort(), source, when, kind, {}))
    store.publish_day(source, dict(location='住处', activity='开始洗澡' if kind == 'bath_started' else '刚洗完澡',
        note=episode['result']['detail']), [], occurred_at=when, activity_kind=kind, episode=episode)


def configured_worker(tmp_path, store, chat, api, clock, author):
    job = DailyVideoWorker(tmp_path / 'jobs', chat, ('100', '200'), lambda: api, store,
        validator=lambda path: None, quiet_seconds=0, clock=clock, author_candidate=author)
    job.scenes = [dict(scene_id='bathroom', event_kinds=['bath_finished'], locations=['住处'])]
    job.capabilities_at = time.monotonic()
    return job


def test_actual_start_authors_without_chat_then_waits_for_actual_finish_and_reuses_order(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish_bath(store, 'bath_started', 'bath:actual-start', NOW)
    now, authored, captions = [NOW], [], []
    share = '刚才洗完澡顺手拍的，给你看看头发还湿着的样子。'

    async def author(candidate):
        assert not chat.lock.locked()
        assert candidate['event_status'] == 'preparing' and candidate['certainty'] == 'live'
        authored.append(candidate)
        return select_candidate(dict(event_id=candidate['event_id'], spoken_text='洗好啦，头发还湿着。',
            share_text=share), [candidate])

    async def scenario():
        job = configured_worker(tmp_path, store, chat, api, lambda: now[0], author)
        async def send(text):
            raise AssertionError('caption must share the video ACK')
        async def video(path, *, caption, eligible):
            assert eligible() and chat.lock.locked()
            captions.append(caption)
            return 'qq-bath-1'
        send.video, send.is_available = video, lambda: True
        job.bind('qq', send)
        await job.intake_preparations()
        await job.wait_idle()
        frozen = job.get('bath:actual-start')['input']
        assert frozen['event_at'] == NOW.isoformat() and frozen['share_text'] == share
        assert job.get('bath:actual-start')['status'] == 'READY'
        assert not captions and len(api.calls) == len(authored) == 1
        await job.close()

        now[0] += timedelta(minutes=21)
        restored = configured_worker(tmp_path, store, chat, api, lambda: now[0], author)
        restored.bind('qq', send)
        await restored.intake_preparations()
        await restored.wait_idle()
        assert store.snapshot(now[0])['rhythm']['authored_bath']['source_id'] == 'bath:actual-start'
        assert not store.daily_video_event_matches(frozen, now=now[0])
        assert store.daily_video_can_prepare(frozen, now=now[0])
        assert not captions and len(api.calls) == len(authored) == 1

        episode = await create(EpisodePort(), 'bath:actual-finish', now[0], 'bath_finished', {})
        store.publish_day('bath:actual-finish', dict(location='住处', activity='刚洗完澡', note=episode['result']['detail']),
            [], occurred_at=now[0], activity_kind='bath_finished', episode=episode)
        restored.resume()
        await restored.wait_idle()
        row = restored.get('bath:actual-start')
        assert row['delivery_status'] == 'DELIVERED' and row['world_status'] == 'COMMITTED'
        assert row['input'] == frozen and api.calls == [('daily_video', frozen)]
        assert captions == [share + '\n这是今天 10:00那次洗澡后的样子。']
        assert not store.snapshot(now[0])['rhythm'].get('authored_bath')
        assert len(store.history()['moments']) == 3  # Start, actual finish, acknowledged share.
        await restored.intake_preparations()
        assert len(authored) == 1
        await restored.close()
    asyncio.run(scenario())


def test_interrupted_paid_author_is_unknown_and_not_repeated_after_restart(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish_bath(store, 'bath_started', 'bath:interrupted', NOW)
    calls = []
    async def scenario():
        started, wait = asyncio.Event(), asyncio.Event()
        async def author(candidate):
            calls.append(candidate)
            started.set()
            await wait.wait()
        job = configured_worker(tmp_path, store, chat, api, lambda: NOW, author)
        await job.intake_preparations()
        await started.wait()
        await job.close()
        restored = configured_worker(tmp_path, store, chat, api, lambda: NOW, author)
        await restored.intake_preparations()
        assert restored.preparation('bath:interrupted')['status'] == 'UNKNOWN'
        assert len(calls) == 1 and not api.calls and not restored.tasks
        await restored.close()
    asyncio.run(scenario())


def test_start_requires_authored_episode_and_cannot_start_twice_or_finish_by_clock(tmp_path):
    store = world(tmp_path)
    with pytest.raises(ValueError, match='DAILY_LIFE_BATH_EPISODE_REQUIRED'):
        store.publish_day('bath:clock-only', dict(location='住处', activity='开始洗澡', note='只是时段。'),
            [], occurred_at=NOW, activity_kind='bath_started')
    publish_bath(store, 'bath_started', 'bath:one', NOW)
    with pytest.raises(ValueError, match='DAILY_LIFE_BATH_ALREADY_STARTED'):
        publish_bath(store, 'bath_started', 'bath:two', NOW + timedelta(minutes=1))
    source = store.daily_video_sources(now=NOW)[0]
    request = select_candidate(dict(event_id=source['event_id'], spoken_text='洗好啦。'),
        [dict(version=1, certainty='planned', scene_id='bathroom', target_date='2026-10-06', **source)])
    assert not store.daily_video_event_matches(request, now=NOW + timedelta(days=1))
    assert store.daily_video_can_prepare(request, now=NOW + timedelta(days=1))
    assert store.snapshot(NOW + timedelta(minutes=21))['stale']


def test_existing_world_refresh_can_author_actual_bath_finish_when_observation_expires(tmp_path, monkeypatch):
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = world(tmp_path)
    publish_bath(store, 'bath_started', 'bath:runtime-start', NOW)
    due, calls = NOW + timedelta(minutes=20), []
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: EpisodePort())
    async def no_other_duty(*args, **kwargs):
        return None
    monkeypatch.setattr('runtime.private_world.meal_lifecycle.advance', no_other_duty)
    monkeypatch.setattr('runtime.private_world.day_plan.ensure', no_other_duty)
    runtime = DailyLifeRuntime(store, lambda: object(), lambda: '[]')
    async def authored_finish(prompt, data, source, **kwargs):
        calls.append(data)
        assert data['rhythm']['authored_bath']['source_id'] == 'bath:runtime-start'
        assert data['allowed_activity_kinds'] == ['bath_finished', 'rest']
        return dict(activity=dict(kind='bath_finished', place_id='home', focus=''), meal=None, project=None)
    runtime._complete = authored_finish
    asyncio.run(runtime.refresh(due))
    assert runtime.error_code is None and len(calls) == 1
    with store._db() as db:
        linked = db.execute('SELECT completed_source_id,completed_at FROM life_baths WHERE source_id=?',
                            ('bath:runtime-start',)).fetchone()
        assert linked['completed_source_id'] and linked['completed_at'] == due.isoformat()


def test_evening_world_reconsideration_can_start_a_real_bath_instead_of_only_clock_phase(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime
    store = world(tmp_path)
    earlier = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)  # 22:00 Beijing.
    now = earlier + timedelta(minutes=90)  # Existing schedule offers bathing at 23:30.
    store.publish_day('evening:rest', dict(location='住处', activity='休息', note='安静休息。'),
        [], occurred_at=earlier, activity_kind='rest')
    assert store.snapshot(now)['rhythm']['phase'] == 'bathing'
    assert not store.snapshot(now)['rhythm'].get('authored_bath')
    monkeypatch.setattr('runtime.reply.jev_questions.configured_questions', lambda: EpisodePort())
    async def no_other_duty(*args, **kwargs):
        return None
    monkeypatch.setattr('runtime.private_world.meal_lifecycle.advance', no_other_duty)
    monkeypatch.setattr('runtime.private_world.day_plan.ensure', no_other_duty)
    runtime = DailyLifeRuntime(store, lambda: object(), lambda: '[]')
    async def authored_start(prompt, data, source, **kwargs):
        assert 'bath_started' in data['allowed_activity_kinds']
        return dict(activity=dict(kind='bath_started', place_id='home', focus=''), meal=None, project=None)
    runtime._complete = authored_start
    asyncio.run(runtime.refresh(now))
    assert runtime.error_code is None
    assert store.snapshot(now)['rhythm']['authored_bath']['started_at'] == now.isoformat()
    assert store.daily_video_sources(now=now)[0]['event_status'] == 'preparing'


def test_preparation_writer_cannot_replace_canonical_event_facts(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish_bath(store, 'bath_started', 'bath:source-locked', NOW)
    async def author(candidate):
        data = select_candidate(dict(event_id=candidate['event_id'], spoken_text='洗好啦。'), [candidate])
        return {**data, 'event_id': 'bath:invented'}
    async def scenario():
        job = configured_worker(tmp_path, store, chat, api, lambda: NOW, author)
        await job.intake_preparations()
        with pytest.raises(ValueError, match='DAILY_VIDEO_SOURCE_UNAVAILABLE'):
            await job.wait_idle()
        assert job.preparation('bath:source-locked')['status'] == 'UNKNOWN'
        assert not api.calls and not job.get('bath:invented')
        await job.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['housework', 'walk', 'bath_finished', 'shopping', 'meal', 'wake_up'])
def test_all_scene_share_copy_keeps_capture_date_and_does_not_change_spoken_text(kind):
    data = {**payload(), 'event_kind': kind, 'share_text': '给你看看我刚才的样子。'}
    assert validate_input(data)['spoken_text'] == payload()['spoken_text']
    caption = capture_caption(data, NOW + timedelta(days=1))
    assert caption.startswith(data['share_text'] + '\n这是2026-10-06 10:00')


@pytest.mark.parametrize('copy', ['', ' ', 'x' * 201, 'hello\x00world', None])
def test_optional_invalid_share_copy_falls_back_without_reauthor(copy):
    data = payload()
    assert select_candidate(dict(event_id=data['event_id'], spoken_text=data['spoken_text'], share_text=copy), [data]) == data
    with pytest.raises(ValueError, match='DAILY_VIDEO_INPUT_INVALID'):
        validate_input({**data, 'share_text': copy})
