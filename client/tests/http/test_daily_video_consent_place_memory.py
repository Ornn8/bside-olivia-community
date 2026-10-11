"""Daily videos: only for the video tier, only as her real current moment, and remembered once sent."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.memory.conversation_memory_delivery import CanonicalMemoryDeliveryResult, CanonicalMemoryDeliveryStatus
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from runtime.personal_chat import daily_video_memory
from runtime.personal_chat.daily_video import DailyVideoWorker, is_current_activity, tier_allows_video
from runtime.personal_chat.events import PersonalMessage
from runtime.video_reply_settings import VideoReplySettingsStore
from tests.http.test_daily_video import API, HOME, NOW, payload, publish, service, world, worker

SCENES = [dict(scene_id='daily_place', event_kinds=['housework', 'walk', 'meal', 'shopping'], locations=[])]
BINDING = PersonalMessage('qq', '100', '200', '', '').binding_id


def place(name, place_id='home'):
    return dict(place_id=place_id, name=name, setting=name + '，沿用已有布局与陈设。', stage='arrived')


KITCHEN, PARK = place('家里厨房'), place('公园湖畔', 'neighborhood')


def occur(store, source_id, at, kind, where, activity='做事', note='在做这件事。'):
    store.publish_day(source_id, {'location': where['name'], 'activity': activity, 'note': note, 'place': where},
                      [], occurred_at=at, activity_kind=kind)


def offering(tmp_path, store, *, moments=False, consent=None):
    job = worker(tmp_path, store, service(), API())
    job.scenes, job.capabilities_at, job.moments, job.consent = SCENES, time.monotonic(), moments, consent
    return job


def conversation(chat):
    row = dict(letter_id='chat-1', channel='qq', binding_id=BINDING, delivery_status='DELIVERED',
               letter_status='COMPLETED', reply_revision=1, content='在干嘛', reply_text='刚收拾完桌边。', created_at=1)
    chat.rows.append(row)
    return row


# --- consent: the video tier and nothing else ---------------------------------------------------

def test_only_the_video_tier_allows_videos(tmp_path):
    settings = VideoReplySettingsStore.initialize(tmp_path)
    assert tier_allows_video(settings) is False  # A new install starts as text.
    for index, (tier, allowed) in enumerate((('audio', False), ('text', False), ('video', True), ('audio', False))):
        settings.mutate_tier(f'video_reply_setting:tier-{index}', tier)
        assert tier_allows_video(settings) is allowed, tier
    assert tier_allows_video(VideoReplySettingsStore.unavailable()) is False
    assert tier_allows_video(None) is False  # No readable setting is never consent.


class Offering:
    """Stands in for the worker the backend asks; counts how often it was asked."""
    def __init__(self):
        self.calls = 0

    def candidates(self, **kwargs):
        self.calls += 1
        base = {key: item for key, item in payload().items() if key != 'spoken_text'}
        return [{**base, 'event_id': 'day:moment', 'event_kind': 'moment', 'scene_id': 'daily_place',
                 'detail': '在家。', 'location': '住处', 'place': HOME},
                {**base, 'detail': '整理好了一小块。', 'location': '住处', 'place': HOME}]


@pytest.mark.parametrize('tier,photo,expected', [
    ('text', True, []), ('audio', True, []), ('audio', False, []),
    ('video', True, ['day:moment', 'day:synthetic-housework']),
    ('video', False, ['day:synthetic-housework'])])
def test_chat_offers_videos_of_every_kind_only_to_the_video_tier(monkeypatch, tmp_path, tier, photo, expected):
    from runtime.personal_chat import backend
    from tests.http.test_chat_jev_decision import server_fixture, pipeline_result
    from tests.http.test_personal_chat_decision import envelope
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://gpu.example')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic')
    server, row, *_ = server_fixture(monkeypatch, tmp_path, pipeline_result(text=envelope(text='你好呀。')))
    server.video_reply_settings_store = SimpleNamespace(
        image_snapshot=lambda: {'enabled': photo, 'resolution': '1K'}, tier_snapshot=lambda: tier)
    server._daily_video_worker = asked = Offering()
    row['image_available'] = True
    asyncio.run(backend.generate(server, PersonalMessage('qq', '100', '200', '1', '在干嘛'), row))
    assert [item['event_id'] for item in row['daily_video_candidates']] == expected
    assert asked.calls == bool(expected)  # Nobody even looks for a video without the tier.


def test_worker_offers_nothing_of_any_kind_without_the_tier(tmp_path):
    store = world(tmp_path)
    occur(store, 'day:kitchen', NOW, 'housework', KITCHEN)
    tier = ['audio']
    job = offering(tmp_path, store, moments=True, consent=lambda: tier[0] == 'video')
    assert job.candidates() == [] and job.candidates(current_only=False) == []
    tier[0] = 'video'
    assert [item['event_kind'] for item in job.candidates()] == ['housework']


def test_a_queued_video_is_dropped_once_the_tier_no_longer_allows_video(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish(store, payload(), HOME)
    tier, sent = ['video'], []

    async def scenario():
        job = worker(tmp_path, store, chat, api)
        job.consent = lambda: tier[0] == 'video'
        await job.enqueue(payload())
        await job.wait_idle()
        assert job.get(payload()['event_id'])['status'] == 'READY' and len(api.calls) == 1
        tier[0] = 'audio'                       # The user switches video off before it is sent.
        async def send(text):
            raise AssertionError('no text')
        async def video(path, **kwargs):
            sent.append(path)
            return 'qq-1'
        send.video, send.is_available = video, lambda: True
        assert not job._can_send(job.get(payload()['event_id']))
        job.bind('qq', send)
        await job.wait_idle()
        row = job.get(payload()['event_id'])
        assert not sent and row['status'] == 'FAILED' and row['error_code'] == 'DAILY_VIDEO_CONSENT_REQUIRED'
        assert row['delivery_status'] == 'PENDING'
        tier[0] = 'video'                       # Switching it back on never revives a dropped clip.
        job.resume()
        await job.wait_idle()
        assert not sent
        await job.close()
    asyncio.run(scenario())


def test_an_ungenerated_job_costs_nothing_once_the_tier_is_gone(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish(store, payload(), HOME)

    async def scenario():
        job = worker(tmp_path, store, chat, api)
        job.consent = lambda: False
        with pytest.raises(ValueError, match='DAILY_VIDEO_CONSENT_REQUIRED'):
            await job.enqueue(payload())
        job._save(dict(event_id=payload()['event_id'], input=payload(), binding=job.binding,
                       status='PENDING', delivery_status='PENDING', world_status='PENDING'))
        job.resume()
        await job.wait_idle()
        assert api.calls == []                  # No paid generation for a user without the tier.
        assert job.get(payload()['event_id'])['error_code'] == 'DAILY_VIDEO_CONSENT_REQUIRED'
        await job.close()
    asyncio.run(scenario())


def test_the_tier_is_checked_again_at_the_moment_of_sending(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish(store, payload(), HOME)
    tier, attempts = ['video'], []

    async def scenario():
        from runtime.personal_chat.qq import QQVideoDeferred
        job = worker(tmp_path, store, chat, api)
        job.consent = lambda: tier[0] == 'video'
        async def send(text):
            raise AssertionError('no text')
        async def video(path, *, eligible, **kwargs):
            tier[0] = 'audio'                   # The setting changes while the clip is handed over.
            attempts.append(eligible())
            if not eligible():
                raise QQVideoDeferred('QQ_VIDEO_DEFERRED')
            return 'qq-1'
        send.video, send.is_available = video, lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        row = job.get(payload()['event_id'])
        assert attempts == [False] and row['delivery_status'] == 'PENDING'
        job.resume()
        await job.wait_idle()
        assert job.get(payload()['event_id'])['status'] == 'FAILED' and attempts == [False]
        await job.close()
    asyncio.run(scenario())


def test_sharing_a_moment_does_not_pay_for_a_writer_without_the_tier(tmp_path):
    store = world(tmp_path)
    occur(store, 'day:walk', NOW, 'walk', PARK)
    job = offering(tmp_path, store, consent=lambda: True)
    candidate = job.candidates()[0]
    job.consent = lambda: False

    async def author(value):
        raise AssertionError('no writer call without the tier')

    with pytest.raises(ValueError, match='DAILY_VIDEO_CONSENT_REQUIRED'):
        asyncio.run(job.share(candidate, author))


def test_the_production_worker_follows_the_users_tier(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from runtime.personal_chat import backend
    import local_server
    config = tmp_path / 'synthetic-channels.json'
    config.write_text(json.dumps({'qq': {'url': 'ws://127.0.0.1:3001', 'account': '100', 'owner': '200'}}))
    monkeypatch.setenv('OLIVIA_PERSONAL_CHAT_CONFIG', str(config))
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    tier = ['audio']
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda *args, **kwargs: None, daily_life_runtime=SimpleNamespace(store=world(tmp_path)),
        video_reply_settings_store=SimpleNamespace(tier_snapshot=lambda: tier[0]))

    async def scenario():
        app = web.Application()
        backend.install_personal_chat(app, server)
        async with TestClient(TestServer(app)):
            job = app[backend._RUNTIME]['daily_video']
            assert job._consented() is False
            tier[0] = 'video'
            assert job._consented() is True
            tier[0] = 'text'
            assert job._consented() is False
    asyncio.run(scenario())


# --- one place for "now" --------------------------------------------------------------------------

def test_a_video_of_another_place_or_an_earlier_event_is_not_her_moment(tmp_path):
    """The report: a kitchen video at home, then a photo in the canteen five minutes later."""
    store = world(tmp_path)
    occur(store, 'day:kitchen', NOW - timedelta(minutes=30), 'housework', KITCHEN)
    occur(store, 'day:walk', NOW, 'walk', PARK)
    job = offering(tmp_path, store, moments=True)
    assert {item['event_id'] for item in job.candidates(current_only=False)} == {'day:kitchen', 'day:walk'}
    offered = job.candidates()
    assert [(item['event_id'], item['place']['name']) for item in offered] == [('day:walk', '公园湖畔')]


def test_the_matching_current_event_is_offered(tmp_path):
    store = world(tmp_path)
    occur(store, 'day:kitchen', NOW - timedelta(minutes=30), 'housework', KITCHEN)
    occur(store, 'day:practice', NOW, 'practice', place('学校琴房', 'campus'))
    job = offering(tmp_path, store, moments=True)
    offered = job.candidates()
    assert [(item['source_id'], item['event_kind']) for item in offered] == [('day:practice', 'moment')]
    assert offered[0]['place']['name'] == '学校琴房'
    occur(store, 'day:later-walk', NOW + timedelta(minutes=5), 'walk', PARK)
    job.clock = lambda: NOW + timedelta(minutes=6)
    assert [item['event_id'] for item in job.candidates()] == ['day:later-walk']


def test_an_earlier_event_at_the_same_place_is_still_not_now(tmp_path):
    store = world(tmp_path)
    occur(store, 'day:tidy', NOW - timedelta(minutes=20), 'housework', KITCHEN)
    occur(store, 'day:read', NOW, 'reading', KITCHEN)  # What she is doing now is not a video kind.
    job = offering(tmp_path, store)
    assert [item['event_id'] for item in job.candidates(current_only=False)] == ['day:tidy']
    assert job.candidates() == []


def test_an_observation_that_has_lapsed_is_not_her_moment(tmp_path):
    store = world(tmp_path)
    occur(store, 'day:walk', NOW, 'walk', PARK)
    job = offering(tmp_path, store)
    assert len(job.candidates(now=NOW + timedelta(minutes=5))) == 1
    assert job.candidates(now=NOW + timedelta(hours=3)) == []


def test_without_structured_place_identity_nothing_is_offered(tmp_path):
    store = world(tmp_path)
    publish(store, payload())  # Only the free-text location 住处, no place record.
    job = offering(tmp_path, store)
    job.scenes = [dict(scene_id='bedroom', event_kinds=['housework'], locations=['住处'])]
    assert len(job.candidates(current_only=False)) == 1
    assert job.candidates() == []


GOOD = dict(event_id='day:a', place=HOME)
LIVE = dict(source_id='day:a', place=HOME, live=True)


@pytest.mark.parametrize('candidate,observed,expected', [
    (GOOD, LIVE, True),
    (GOOD, None, False),
    (GOOD, {**LIVE, 'live': False}, False),
    (GOOD, {**LIVE, 'source_id': 'day:b'}, False),
    (GOOD, {**LIVE, 'place': None}, False),
    ({'event_id': 'day:a'}, LIVE, False),
    ({**GOOD, 'place': place('卧室')}, LIVE, False),
    ({**GOOD, 'place': place('住处', 'campus')}, LIVE, False),
    ({**GOOD, 'place': {'name': '住处'}}, LIVE, False)])
def test_current_activity_is_matched_by_ids_and_place_identity(candidate, observed, expected):
    assert is_current_activity(candidate, observed) is expected


def test_a_moment_she_has_moved_on_from_is_not_authored_or_queued(tmp_path):
    store = world(tmp_path)
    occur(store, 'day:kitchen', NOW - timedelta(minutes=30), 'housework', KITCHEN)
    job = offering(tmp_path, store)
    kitchen = job.candidates()[0]
    occur(store, 'day:walk', NOW, 'walk', PARK)  # By the time the cloud planner picks it, she is elsewhere.

    async def author(value):
        raise AssertionError('no writer call for a moment that is over')

    with pytest.raises(ValueError, match='DAILY_VIDEO_SOURCE_UNAVAILABLE'):
        asyncio.run(job.share(kitchen, author))
    assert job.get('day:kitchen') is None


def test_a_chosen_video_that_is_no_longer_current_is_rejected_when_queued(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    occur(store, 'day:kitchen', NOW - timedelta(minutes=30), 'housework', KITCHEN)

    async def scenario():
        job = worker(tmp_path, store, chat, api)
        job.scenes, job.capabilities_at = SCENES, time.monotonic()
        from runtime.personal_chat.daily_video import select_candidate
        offered = job.candidates()
        request = select_candidate(dict(event_id='day:kitchen', spoken_text='刚收拾好。'), offered)
        chat.rows.append(dict(channel='qq', binding_id=BINDING, delivery_status='DELIVERED',
                              daily_video_request=request, daily_video_candidates=offered))
        occur(store, 'day:walk', NOW, 'walk', PARK)
        await job.intake_replies()
        assert chat.rows[-1]['daily_video_status'] == 'REJECTED'
        assert chat.rows[-1]['daily_video_error_code'] == 'DAILY_VIDEO_SOURCE_UNAVAILABLE'
        assert job.get('day:kitchen') is None and not api.calls
        await job.close()
    asyncio.run(scenario())


def test_background_bath_preparation_is_not_filtered_by_what_she_does_now(tmp_path):
    from tests.private_world.test_places_and_meals import Port
    from runtime.private_world.life_episode import create
    store = world(tmp_path)
    episode = asyncio.run(create(Port(), 'bath:start', NOW, 'bath_started', {}))
    store.publish_day('bath:start', dict(location='浴室', activity='开始洗澡', note='开始洗澡。', place=place('浴室')),
                      [], activity_kind='bath_started', occurred_at=NOW, episode=episode)
    job = offering(tmp_path, store)
    job.scenes = [dict(scene_id='daily_place', event_kinds=['bath_finished'], locations=[])]
    assert [(c['event_kind'], c['event_status']) for c in job.candidates(current_only=False)] == [('bath_finished', 'preparing')]


# --- memory: one record per delivered video ------------------------------------------------------

def deliver(tmp_path, *, extra=None, send_video=None):
    """Run one housework video through the worker; returns (worker, chat, anchor row, sends)."""
    store, chat, api = world(tmp_path), service(), API()
    request = payload(place=HOME, share_text='给你看看我刚收拾好的桌边。', **(extra or {}))
    publish(store, request, HOME)
    anchor = conversation(chat)
    sends = []

    async def run():
        job = worker(tmp_path, store, chat, api)
        async def send(text):
            raise AssertionError('no text')
        async def video(path, **kwargs):
            sends.append(kwargs.get('caption'))
            return 'qq-301'
        send.video, send.is_available = send_video or video, lambda: True
        job.bind('qq', send)
        await job.enqueue(request)
        await job.wait_idle()
        return job
    return asyncio.run(run()), chat, anchor, sends, request, (store, api)


def test_a_delivered_video_enters_memory_once_with_what_was_shown_and_said(tmp_path):
    job, chat, anchor, sends, request, (store, api) = deliver(tmp_path)
    assert len(sends) == 1 and job.get(request['event_id'])['memory_status'] == 'COMMITTED'
    records = anchor[daily_video_memory.KEY]
    assert len(records) == 1 and records[0]['event_id'] == request['event_id']
    text = records[0]['text']
    for expected in ('短视频', '自拍', '住处', '做家务', '整理好了一小块。', '我整理了桌边这一小块。',
                     '给你看看我刚收拾好的桌边。', '10月6日10:00', '不证明对方已观看'):
        assert expected in text, expected
    assert [call[0] for call in api.calls] == ['daily_video']  # No model or vision call looked at the clip.

    async def again():
        # A replayed commit, a second pass and a restarted worker all find the same one record.
        row = job.get(request['event_id'])
        row['memory_status'] = 'PENDING'
        await job._remember(row)
        job.resume()
        await job.wait_idle()
        await job.close()
        restored = worker(tmp_path, store, chat, api)
        restored.resume()
        await restored.wait_idle()
        await restored.close()
    asyncio.run(again())
    assert len(anchor[daily_video_memory.KEY]) == 1 and len(chat.rows) == 1


def test_the_memory_outbox_receives_one_source_for_the_video(tmp_path):
    job, chat, anchor, *_ = deliver(tmp_path)
    source = tmp_path / 'state.json'
    source.write_text(json.dumps({'letters': [], 'personal_chats': chat.rows}), encoding='utf8')
    calls = []

    class Committer:
        async def commit(self, delivery):
            calls.append(delivery)
            return CanonicalMemoryDeliveryResult(CanonicalMemoryDeliveryStatus.WRITTEN, delivery.source_id)

    async def scan():
        outbox = CanonicalMemoryOutbox(source, tmp_path / 'outbox.sqlite3', Committer())
        await outbox.scan_once()
        await outbox.scan_once()
    asyncio.run(scan())
    assert len(calls) == 2 and calls[0].source_id != calls[1].source_id  # her text reply, then the video
    video = calls[1]
    assert video.origin == 'proactive' and video.user_message == ''
    assert '短视频' in video.assistant_message and '我整理了桌边这一小块。' in video.assistant_message
    assert video.occurred_at.tzinfo is not None


def test_no_memory_for_a_video_that_was_not_delivered(tmp_path):
    from runtime.personal_chat.qq import QQVideoDeferred
    async def unconfirmed(path, **kwargs):
        raise RuntimeError('QQ_SEND_UNCONFIRMED')
    async def deferred(path, **kwargs):
        raise QQVideoDeferred('QQ_VIDEO_DEFERRED')
    for index, send_video in enumerate((unconfirmed, deferred)):
        job, chat, anchor, sends, request, _ = deliver(tmp_path / str(index), send_video=send_video)
        row = job.get(request['event_id'])
        assert row['delivery_status'] in {'UNKNOWN', 'PENDING'} and 'memory_status' not in row
        assert daily_video_memory.KEY not in anchor
        assert list(daily_video_memory.memory_rows(anchor)) == []
    # Generated but not yet sent: nothing to remember either.
    store, chat, api = world(tmp_path / 'ready'), service(), API()
    publish(store, payload(), HOME)
    conversation(chat)

    async def ready():
        job = worker(tmp_path / 'ready', store, chat, api)
        await job.enqueue(payload())
        await job.wait_idle()
        assert job.get(payload()['event_id'])['status'] == 'READY'
        await job.close()
    asyncio.run(ready())
    assert daily_video_memory.KEY not in chat.rows[0]


def test_videos_delivered_before_this_change_are_not_backfilled(tmp_path):
    job, chat, anchor, sends, request, (store, api) = deliver(tmp_path)
    row = job.get(request['event_id'])
    for key in ('memory_status', 'memory_attempts'):
        row.pop(key, None)
    job._save(row)
    del anchor[daily_video_memory.KEY]

    async def restart():
        await job.close()
        restored = worker(tmp_path, store, chat, api)
        restored.resume()
        await restored.wait_idle()
        await restored.close()
    asyncio.run(restart())
    assert daily_video_memory.KEY not in anchor


def test_a_video_with_no_conversation_to_hold_it_settles_without_retrying(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish(store, payload(), HOME)

    async def scenario():
        job = worker(tmp_path, store, chat, api)
        async def send(text):
            raise AssertionError('no text')
        async def video(path, **kwargs):
            return 'qq-301'
        send.video, send.is_available = video, lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        assert job.get(payload()['event_id'])['memory_status'] == 'NO_CONVERSATION'
        job.resume()
        assert not job.tasks                     # Settled: the monitor loop has nothing to redo.
        await job.close()
    asyncio.run(scenario())


def test_malformed_memory_records_are_ignored_by_the_outbox_projection():
    good = dict(event_id='day:a', delivered_at=NOW.isoformat(), text='林离发了一段视频。')
    row = {daily_video_memory.KEY: [good, good, dict(good, event_id='day:b', delivered_at='nope'),
                                    dict(good, event_id='day:c', delivered_at='2026-10-06T02:00:00'),
                                    dict(good, event_id='day:d', text=' '), 'x', {}]}
    rows = list(daily_video_memory.memory_rows(row))
    assert len(rows) == 1 and rows[0]['origin'] == 'proactive' and rows[0]['content'] == ''
    assert rows[0]['letter_id'] == daily_video_memory.letter_id('day:a')
    assert list(daily_video_memory.memory_rows({daily_video_memory.KEY: 'oops'})) == []


def test_capability_requests_say_the_client_takes_moment_videos():
    """Older clients fail a whole QQ turn on a moment offer; the service announces moments only on this header."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from runtime.remote_generation import RemoteGeneration

    async def scenario():
        seen = []

        async def capabilities(request):
            seen.append(request.headers.get('X-Olivia-Daily-Video-Moment'))
            return web.json_response({'kinds': [], 'shared_assets': []})

        app = web.Application()
        app.router.add_get('/v1/capabilities', capabilities)
        server = TestServer(app)
        await server.start_server()
        try:
            await RemoteGeneration(str(server.make_url('')).rstrip('/'), 'synthetic-token').request('capabilities', {})
        finally:
            await server.close()
        return seen

    assert asyncio.run(scenario()) == ['1']
