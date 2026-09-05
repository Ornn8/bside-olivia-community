from datetime import datetime, timedelta, timezone

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.life_rhythm import rest_timeline, rhythm
import asyncio
import json
from types import SimpleNamespace
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


def test_night_exchange_survives_restart_recovers_and_does_not_count_each_letter(tmp_path):
    path = tmp_path / 'life.sqlite3'
    life = DailyLifeStore(path)
    night = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)  # Shanghai 01:00
    assert life.snapshot(night)['rhythm']['phase'] == 'sleep'
    life.record_exchange('reply:a:1', '聊聊？', '我刚睡下。', [], occurred_at=night)
    life.record_exchange('reply:b:1', '嗯', '晚安。', [], occurred_at=night + timedelta(minutes=5))
    life = DailyLifeStore(path)
    assert life.snapshot(night + timedelta(minutes=10))['rhythm']['phase'] == 'interrupted_rest'
    morning = life.snapshot(night + timedelta(hours=8))['rhythm']
    assert morning['rest'] == 'tired'
    life.record_exchange('reply:a:1', '聊聊？', '我刚睡下。', [], occurred_at=night)
    assert life.snapshot(night + timedelta(hours=8))['rhythm'] == morning
    assert life.snapshot(night + timedelta(days=4))['rhythm']['rest'] == 'rested'


def test_actual_exchange_duration_and_resleep_are_not_flat_letter_counts():
    start = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
    def event(minutes, duration=2):
        return (start + timedelta(minutes=minutes), start + timedelta(minutes=minutes+duration))
    short = rest_timeline(start + timedelta(hours=5), [event(0)])
    continuous = rest_timeline(start + timedelta(hours=5), [event(0), event(10), event(20, 8)])
    interrupted = rest_timeline(start + timedelta(hours=5), [event(0), event(50), event(100)])
    assert short['awake_minutes'] == 32  # Actual reply duration plus 30 quiet minutes.
    assert continuous['awake_minutes'] == 58
    assert continuous['interruptions'] == 1
    assert interrupted['awake_minutes'] == 96
    assert interrupted['interruptions'] == 3
    assert interrupted['load_minutes'] > continuous['load_minutes']
    assert rest_timeline(start + timedelta(hours=5), [event(0), event(0)]) == short


def test_sleep_window_clips_awake_duration_and_daytime_does_not_count():
    start = datetime(2026, 9, 7, 14, 50, tzinfo=timezone.utc)  # 22:50
    end = start + timedelta(minutes=40)  # 23:30, continued across bedtime
    load = rest_timeline(end + timedelta(hours=1), [(start, end)])
    assert load['awake_minutes'] == 60  # 23:00 through 00:00
    assert load['interruptions'] == 0  # Stayed up; was not awakened.
    day = start - timedelta(hours=8)
    assert rest_timeline(end, [(day, day + timedelta(minutes=20))])['awake_minutes'] == 0


def test_thirty_minutes_after_reply_is_exact_resleep_boundary():
    start = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
    end = start + timedelta(minutes=2)
    boundary = end + timedelta(minutes=30)
    assert rest_timeline(boundary - timedelta(seconds=1), [(start, end)])['awake_now']
    assert not rest_timeline(boundary, [(start, end)])['awake_now']
    continuous = [(start, end), (boundary - timedelta(seconds=1), boundary + timedelta(minutes=2))]
    separate = [(start, end), (boundary, boundary + timedelta(minutes=2))]
    later = boundary + timedelta(hours=1)
    assert rest_timeline(later, continuous)['interruptions'] == 1
    assert rest_timeline(later, separate)['interruptions'] == 2


def test_runtime_preserves_receipt_time_instead_of_using_background_completion(tmp_path):
    received = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
    finished = received + timedelta(minutes=40)
    class Model:
        async def complete(self, messages, **kwargs):
            data = json.loads(messages[1]['content'])
            assert data['rhythm']['phase'] == 'sleep'
            return SimpleNamespace(text='{"updates":[],"relationship":null}')
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    runtime = DailyLifeRuntime(store, lambda: Model(), lambda: '')
    asyncio.run(runtime.consume_exchange('reply:long:1', '睡不着，想聊聊。', '我在听。',
                                         occurred_at=finished, received_at=received))
    # 40 min correspondence + 30 min settling + first-wake cost; must be tired.
    assert store.snapshot(finished + timedelta(hours=1))['rhythm']['rest'] == 'tired'


def test_routine_adapts_gradually_once_a_day_and_never_rewrites_previous_sleep(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    night = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
    for index in range(3):
        at = night + timedelta(days=index)
        store.record_exchange(f'reply:habit{index}:1', '聊聊', '晚安', [], occurred_at=at)
    now = night + timedelta(days=3, hours=-3)  # 22:00; changes upcoming night only.
    before = store.snapshot(night)['rhythm']
    store.adapt_routine(now, affinity=0.8)
    assert store.snapshot(now)['rhythm']['sleep_shift_minutes'] == 15
    store.adapt_routine(now + timedelta(hours=1), affinity=0.8)
    assert store.snapshot(now)['rhythm']['sleep_shift_minutes'] == 15
    assert store.snapshot(night)['rhythm'] == before
    store.adapt_routine(now + timedelta(days=1), affinity=0.8)
    assert store.snapshot(now + timedelta(days=1))['rhythm']['sleep_shift_minutes'] == 30
    store.adapt_routine(now + timedelta(days=2), affinity=0)
    assert store.snapshot(now + timedelta(days=2))['rhythm']['sleep_shift_minutes'] == 15


def test_delayed_bedtime_preserves_sleep_duration_across_midnight():
    shifts = {'2026-09-07': 120}
    midnight = datetime(2026, 9, 7, 16, tzinfo=timezone.utc)
    assert rhythm(midnight, [], shifts)['phase'] == 'quiet'
    assert rhythm(midnight + timedelta(hours=1), [], shifts)['phase'] == 'sleep'
    assert rhythm(midnight + timedelta(hours=8), [], shifts)['phase'] == 'sleep'
    assert rhythm(midnight + timedelta(hours=9), [], shifts)['phase'] == 'breakfast'
    event = (midnight, midnight + timedelta(minutes=2))
    assert rest_timeline(midnight + timedelta(hours=10), [event], shifts)['interruptions'] == 0


def test_explicit_user_routine_is_grounded_and_does_not_immediately_shift_sleep(tmp_path):
    import pytest
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    quote = '我在上海，一般凌晨一点睡。'
    preference = {'sleep_minute':60, 'utc_offset_minutes':480, 'quote':quote}
    with pytest.raises(ValueError, match='EVIDENCE'):
        store.record_exchange('reply:bad:1', '晚上好', '晚上好', [], occurred_at=now, routine=preference)
    store.record_exchange('reply:preference:1', quote, '知道了。', [], occurred_at=now, routine=preference)
    assert store.snapshot(now)['rhythm']['sleep_shift_minutes'] == 0
    store.adapt_routine(now + timedelta(minutes=1), affinity=.8)
    assert store.snapshot(now)['rhythm']['sleep_shift_minutes'] == 15
