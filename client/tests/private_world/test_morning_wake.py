"""A morning video needs an authored night and a newly authored waking process."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.life_episode import create

LOCAL = timezone(timedelta(hours=8))
START = datetime(2026, 10, 5, 23, tzinfo=LOCAL)
END = datetime(2026, 10, 6, 7, 20, tzinfo=LOCAL)


class Port:
    def __init__(self, path):
        self.path, self.calls = path, []

    async def ask(self, state, questions, *, purpose):
        self.calls.append(state)
        assert purpose == 'world-life-episode'
        assert self.path in state['paths']
        return {'trigger': next(iter(questions['trigger']['criteria'])),
                'experience': f'{self.path}:ordinary:none'}


class Queue:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def enqueue(self, value):
        self.calls.append(value)
        if self.fail:
            raise OSError('synthetic transport failure')
        return value


def night(store):
    from runtime.private_world.morning_wake import author_night_sleep
    return asyncio.run(author_night_sleep(store, Port('night_sleep'), source_id='night:1',
                                        now=START, end_at=END))


def wake(store, port=None, queue=None, **kwargs):
    from runtime.private_world.morning_wake import author_morning_wake
    return asyncio.run(author_morning_wake(store, port or Port('morning_wake'), queue or Queue(),
        source_id='wake:1', now=END, spoken_text='早呀，我刚醒，还想再缓一下。', **kwargs))


def test_explicit_night_then_new_wake_exports_same_canonical_identity_and_queue(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    due = store.snapshot(END)
    assert due['rhythm']['authored_sleep']['kind'] == 'overnight'
    assert due['rhythm']['authored_sleep']['status'] == 'due'
    assert not any(m['content'].get('event_kind') == 'wake_up' for m in store.history()['moments'])
    queue = Queue()
    value = wake(store, queue=queue)
    assert value['event_id'] == 'wake:1' and value['event_kind'] == 'wake_up'
    assert value['event_at'] == END.astimezone(timezone.utc).isoformat()
    assert value['scene_id'] == 'bedroom' and value['certainty'] == 'live'
    assert store.daily_video_event_matches(value, now=END)
    assert queue.calls == [value]
    assert not any(m['kind'] == 'media' for m in store.history()['moments'])


def test_clock_and_ordinary_rest_do_not_certify_morning_wake(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    with pytest.raises(ValueError, match='MORNING_WAKE_SOURCE_UNAVAILABLE'):
        wake(store)
    night(store)
    queue = Queue()
    assert wake(store, Port('ongoing'), queue) is None
    assert not queue.calls
    assert not any(m['content'].get('event_kind') for m in store.history()['moments'])
    assert store.snapshot(END)['rhythm']['authored_sleep']['status'] == 'due'


def test_nap_does_not_become_early_morning_selfie(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    now = END.replace(hour=8, minute=0)
    episode = asyncio.run(create(Port('nap_60'), 'nap:1', now, 'rest', store.snapshot(now)))
    store.publish_day('nap:1', dict(location='住处', activity='休息', note=episode['result']['detail']),
                      [], occurred_at=now, activity_kind='rest', episode=episode)
    with pytest.raises(ValueError, match='MORNING_WAKE_SOURCE_UNAVAILABLE'):
        from runtime.private_world.morning_wake import author_morning_wake
        asyncio.run(author_morning_wake(store, Port('morning_wake'), Queue(), source_id='wake:1',
                    now=now + timedelta(hours=1), spoken_text='合成早安'))


@pytest.mark.parametrize('when', [END - timedelta(minutes=1), END + timedelta(hours=6)])
def test_premature_or_late_morning_is_not_backfilled(tmp_path, when):
    from runtime.private_world.morning_wake import author_morning_wake
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    with pytest.raises(ValueError, match='MORNING_WAKE_SOURCE_UNAVAILABLE'):
        asyncio.run(author_morning_wake(store, Port('morning_wake'), Queue(), source_id='wake:1',
                    now=when, spoken_text='合成早安'))
    assert len(store.history()['moments']) == 1


def test_night_correspondence_does_not_become_clean_overnight_wake(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    store.record_exchange('reply:night', '合成问候', '合成回复', [],
        received_at=START + timedelta(hours=2), occurred_at=START + timedelta(hours=2, minutes=1))
    with pytest.raises(ValueError, match='MORNING_WAKE_SOURCE_UNAVAILABLE'):
        wake(store)


def test_queue_failure_retry_reuses_authored_source_without_paid_world_reauthor(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    with pytest.raises(OSError):
        wake(store, queue=Queue(fail=True))
    port, queue = Port('morning_wake'), Queue()
    value = wake(DailyLifeStore(store.path), port, queue)
    assert not port.calls and queue.calls == [value]
    assert len(store.history()['moments']) == 2


def test_same_wake_cannot_be_authored_under_second_id(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    wake(store)
    from runtime.private_world.morning_wake import author_morning_wake
    with pytest.raises(ValueError, match='MORNING_WAKE_SOURCE_UNAVAILABLE'):
        asyncio.run(author_morning_wake(store, Port('morning_wake'), Queue(), source_id='wake:2',
                    now=END, spoken_text='合成早安'))


@pytest.mark.parametrize('start,end', [(END, END + timedelta(hours=2)),
    (START, START + timedelta(minutes=60)), (START, START + timedelta(hours=14))])
def test_night_plan_is_explicit_bounded_and_not_daytime_nap(tmp_path, start, end):
    from runtime.private_world.morning_wake import author_night_sleep
    store = DailyLifeStore(tmp_path / 'world.db')
    port = Port('night_sleep')
    with pytest.raises(ValueError, match='LIFE_EPISODE_SLEEP_INVALID'):
        asyncio.run(author_night_sleep(store, port, source_id='night:1', now=start, end_at=end))
    assert not port.calls and not store.history()['moments']


def test_forged_morning_resolution_requires_valid_overnight_source(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    episode = asyncio.run(create(Port('settled'), 'wake:1', END, 'rest', store.snapshot(END)))
    episode['effects']['morning_wake'] = {'sleep_source_id': 'missing'}
    episode['effects']['sleep_resolution'] = {'source_id': 'missing'}
    with pytest.raises(ValueError, match='LIFE_EPISODE_SLEEP_INVALID'):
        store.publish_day('wake:1', dict(location='住处', activity='醒来', note='合成醒来'), [],
                          occurred_at=END, activity_kind='rest', episode=episode)
    assert not store.history()['moments']


def test_night_candidate_is_not_started_when_writer_keeps_resting(tmp_path):
    from runtime.private_world.morning_wake import author_night_sleep
    store = DailyLifeStore(tmp_path / 'world.db')
    port = Port('ongoing')
    asyncio.run(author_night_sleep(store, port, source_id='night:1', now=START, end_at=END))
    assert len(port.calls) == 1
    assert port.calls[0]['context']['overnight_sleep_candidate']['meaning'] == '待选休息计划，不证明已经睡着。'
    assert not store.snapshot(END)['rhythm'].get('authored_sleep')
    asyncio.run(author_night_sleep(store, port, source_id='night:1', now=START, end_at=END))
    assert len(port.calls) == 1
    with pytest.raises(ValueError, match='MORNING_WAKE_SOURCE_UNAVAILABLE'):
        wake(store)


def test_new_correspondence_during_writer_call_aborts_canonical_wake(tmp_path):
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    class InterruptedPort(Port):
        async def ask(self, state, questions, *, purpose):
            answer = await super().ask(state, questions, purpose=purpose)
            store.record_exchange('reply:concurrent', '合成问候', '合成回复', [],
                received_at=START + timedelta(hours=2), occurred_at=START + timedelta(hours=2, minutes=1))
            return answer
    queue = Queue()
    with pytest.raises(ValueError, match='LIFE_EPISODE_SLEEP_INVALID'):
        wake(store, InterruptedPort('morning_wake'), queue)
    assert not store.has_source('wake:1') and not queue.calls


def test_same_night_identity_rejects_changed_plan_without_reauthoring(tmp_path):
    from runtime.private_world.morning_wake import author_night_sleep
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    port = Port('night_sleep')
    asyncio.run(author_night_sleep(store, port, source_id='night:1', now=START, end_at=END))
    assert not port.calls
    with pytest.raises(ValueError, match='DAILY_LIFE_SOURCE_CONFLICT'):
        asyncio.run(author_night_sleep(store, port, source_id='night:1', now=START,
                                      end_at=END + timedelta(minutes=20)))
    assert not port.calls


def test_wake_naive_time_and_invalid_spoken_text_never_author_world(tmp_path):
    from runtime.private_world.morning_wake import author_morning_wake
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    port = Port('morning_wake')
    for now, text in ((END.replace(tzinfo=None), '合成早安'), (END, ''), (END, 'x' * 501)):
        with pytest.raises(ValueError, match='DAILY_VIDEO_INPUT_INVALID'):
            asyncio.run(author_morning_wake(store, port, Queue(), source_id='wake:1', now=now, spoken_text=text))
    assert not port.calls and not store.has_source('wake:1')


def test_callable_joins_native_worker_qq_ack_and_original_canonical_media_journal(tmp_path):
    from runtime.private_world.morning_wake import author_morning_wake
    from runtime.personal_chat.daily_video import DailyVideoWorker
    from runtime.personal_chat.service import PersonalChatService
    store = DailyLifeStore(tmp_path / 'world.db')
    night(store)
    async def scenario():
        class API:
            token, url = 'synthetic', 'https://synthetic.invalid'
            calls = 0
            async def generate(self, kind, value, output, **kwargs):
                assert kind == 'daily_video' and value['event_kind'] == 'wake_up'
                self.calls += 1
                output.write_bytes(b'synthetic-qualified-mp4')
                return {'task_id': 'wake-task'}
            async def request(self, action, value):
                assert action == 'ack'
        chat = PersonalChatService([], lambda: None, None, None, {'qq': ('100', '200')})
        api, sent = API(), []
        worker = DailyVideoWorker(tmp_path / 'jobs', chat, ('100', '200'), lambda: api, store,
            validator=lambda path: None, quiet_seconds=0, clock=lambda: END)
        async def send(text):
            raise AssertionError('video only')
        async def video(path, **kwargs):
            assert chat.lock.locked()
            assert len(store.history()['moments']) == 2
            sent.append(path)
            return 'qq-wake-ack'
        send.video, send.is_available = video, lambda: True
        worker.bind('qq', send)
        port = Port('morning_wake')
        for _ in range(2):
            await author_morning_wake(store, port, worker, source_id='wake:1', now=END, spoken_text='合成早安')
            await worker.wait_idle()
        assert len(port.calls) == api.calls == len(sent) == 1
        row = worker.get('wake:1')
        assert row['world_status'] == 'COMMITTED' and row['message_id'] == 'qq-wake-ack'
        moments = store.history()['moments']
        media = [m['content']['delivery'] for m in moments if m['kind'] == 'media']
        assert len(moments) == 3 and len(media) == 1 and media[0]['daily_event']['event_id'] == 'wake:1'
        await worker.close()
    asyncio.run(scenario())


def test_explicit_rounded_wake_plan_accepts_real_start_seconds_without_changing_plan(tmp_path):
    from runtime.private_world.morning_wake import author_night_sleep
    store = DailyLifeStore(tmp_path / 'world.db')
    start = START + timedelta(seconds=17, microseconds=123)
    asyncio.run(author_night_sleep(store, Port('night_sleep'), source_id='night:1', now=start, end_at=END))
    sleep = store.snapshot(start + timedelta(minutes=1))['rhythm']['authored_sleep']
    assert datetime.fromisoformat(sleep['started_at']) == start
    assert datetime.fromisoformat(sleep['end_at']) == END
    assert sleep['kind'] == 'overnight' and sleep['status'] == 'sleeping'
    assert store.snapshot(start + timedelta(minutes=1))['rhythm']['activity'] == '夜间睡眠中'


@pytest.mark.parametrize('same_source', [False, True])
def test_parallel_night_authors_commit_only_one_active_sleep_plan(tmp_path, same_source):
    from runtime.private_world.morning_wake import author_night_sleep
    store = DailyLifeStore(tmp_path/'world.db')
    async def scenario():
        class ConcurrentPort(Port):
            def __init__(self):
                super().__init__('night_sleep')
                self.ready = asyncio.Event()
            async def ask(self, state, questions, *, purpose):
                result = await super().ask(state, questions, purpose=purpose)
                if len(self.calls) == 2:
                    self.ready.set()
                await self.ready.wait()
                return result
        port = ConcurrentPort()
        second_id = 'night:a' if same_source else 'night:b'
        second_end = END if same_source else END + timedelta(hours=1)
        results = await asyncio.wait_for(asyncio.gather(
            author_night_sleep(store, port, source_id='night:a', now=START, end_at=END),
            author_night_sleep(store, port, source_id=second_id, now=START, end_at=second_end),
            return_exceptions=True), 2)
        failures = [value for value in results if isinstance(value, Exception)]
        assert len(failures) == (0 if same_source else 1)
        if failures:
            assert isinstance(failures[0], ValueError) and str(failures[0]) == 'LIFE_EPISODE_SLEEP_INVALID'
        with store._db() as db:
            plans = db.execute("SELECT source_id FROM life_episodes WHERE json_extract(payload,'$.effects.sleep_plan.kind')='overnight'").fetchall()
        assert len(plans) == len(store.history()['moments']) == 1
        assert store.snapshot(START)['rhythm']['authored_sleep']['source_id'] == plans[0][0]
    asyncio.run(scenario())


@pytest.mark.parametrize('resolved', [False, True])
def test_storage_accepts_nonoverlapping_or_already_resolved_sleep_plan(tmp_path, resolved):
    from copy import deepcopy
    import json
    from runtime.private_world.life_episode import overnight_plan
    store = DailyLifeStore(tmp_path/'world.db')
    night(store)
    if resolved:
        # An explicit earlier author ended the night; a new sleep may begin afterwards.
        ended = START + timedelta(minutes=30)
        resolution = asyncio.run(create(Port('settled'), 'night:ended', ended, 'rest', store.snapshot(ended)))
        resolution['effects']['sleep_resolution'] = {'source_id': 'night:1'}
        store.publish_day('night:ended', dict(location='住处', activity='休息', note=resolution['result']['detail']),
                          [], occurred_at=ended, activity_kind='rest', episode=resolution)
        later = START + timedelta(minutes=45)
        later_end = END + timedelta(hours=1)
    else:
        later, later_end = START + timedelta(days=1), END + timedelta(days=1)
    with store._db() as db:
        episode = deepcopy(json.loads(db.execute('SELECT payload FROM life_episodes WHERE source_id=?', ('night:1',)).fetchone()[0]))
    episode.update(source_id='night:later', occurred_at=later.astimezone(timezone.utc).isoformat())
    episode['effects']['sleep_plan'] = overnight_plan(later, later_end)
    assert store.publish_day('night:later', dict(location='住处', activity='休息', note=episode['result']['detail']),
                             [], occurred_at=later, activity_kind='rest', episode=episode)
    assert store.snapshot(later)['rhythm']['authored_sleep']['source_id'] == 'night:later'


@pytest.mark.parametrize('interrupted,ended_at', [
    (True, START + timedelta(hours=2, minutes=2)), (True, END + timedelta(minutes=1)),
    (False, END.replace(hour=12, minute=0)), (False, END + timedelta(days=1))])
def test_old_night_can_end_without_wake_video_or_recovery_and_next_night_can_start(tmp_path, interrupted, ended_at):
    from runtime.private_world.morning_wake import author_night_sleep
    store = DailyLifeStore(tmp_path/'world.db')
    night(store)
    if interrupted:
        store.record_exchange('reply:interruption', '合成问候', '合成回复', [],
            received_at=START + timedelta(hours=2), occurred_at=START + timedelta(hours=2, minutes=1))
    pending = store.snapshot(ended_at)
    assert pending['rhythm']['authored_sleep']['interrupted'] is interrupted
    assert pending['rhythm']['authored_sleep']['status'] == 'due'
    # Ordinary rest cannot silently turn interrupted sleep into a completed night.
    ordinary = asyncio.run(create(Port('ongoing'), 'night:still-resting', ended_at, 'rest', pending))
    assert 'sleep_resolution' not in ordinary['effects']
    store.publish_day('night:still-resting', dict(location='住处', activity='休息', note=ordinary['result']['detail']),
                      [], occurred_at=ended_at, activity_kind='rest', episode=ordinary)
    days = 2 if ended_at >= START + timedelta(days=1) else 1
    next_start, next_end = START + timedelta(days=days), END + timedelta(days=days)
    with pytest.raises(ValueError, match='LIFE_EPISODE_SLEEP_INVALID'):
        asyncio.run(author_night_sleep(store, Port('night_sleep'), source_id='night:next',
                                      now=next_start, end_at=next_end))
    port = Port('night_ended')
    episode = asyncio.run(create(port, 'night:ended', ended_at, 'rest', store.snapshot(ended_at)))
    assert 'morning_wake' not in port.calls[0]['paths']
    assert 'nap_refreshed' not in port.calls[0]['paths']
    assert episode['effects'] == {'next_action': None, 'open_loop': None,
                                 'sleep_resolution': {'source_id': 'night:1'}}
    assert episode['result']['status'] == 'paused'
    store.publish_day('night:ended', dict(location='住处', activity='休息', note=episode['result']['detail']),
                      [], occurred_at=ended_at, activity_kind='rest', episode=episode)
    assert 'authored_sleep' not in DailyLifeStore(store.path).snapshot(ended_at)['rhythm']
    assert not any(m['content'].get('event_kind') == 'wake_up' for m in store.history()['moments'])
    asyncio.run(author_night_sleep(store, Port('night_sleep'), source_id='night:next',
                                  now=next_start, end_at=next_end))
    assert store.snapshot(next_start)['rhythm']['authored_sleep']['source_id'] == 'night:next'
