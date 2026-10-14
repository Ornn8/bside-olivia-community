"""Activity expiry wakes the existing decision path, not a clock-based story."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from tests.private_world.decisions import life_decision


NOON = datetime(2026, 9, 6, 4, tzinfo=timezone.utc)  # Sunday, no classes.


@pytest.mark.parametrize('kind,minutes', [
    ('rest', 60), ('practice', 60), ('reading', 60), ('creative', 60),
    ('housework', 60), ('walk', 60), ('errand', 60),
])
def test_store_and_runtime_share_activity_expiry(tmp_path, kind, minutes):
    store = DailyLifeStore(tmp_path / 'life.db')
    store.publish_day('synthetic:initial',
                      dict(location='home', activity=kind, note='A recorded activity.'),
                      [], occurred_at=NOON, activity_kind=kind)
    calls = []

    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(life_decision(messages, kind='rest')))

    runtime = DailyLifeRuntime(store, Gateway, lambda: 'A student.')

    async def run():
        for minute in range(minutes):
            now = NOON + timedelta(minutes=minute)
            assert not store.snapshot(now)['stale']
            await runtime.refresh(now)
        assert not calls
        due = NOON + timedelta(minutes=minutes)
        assert store.snapshot(due)['stale']
        await runtime.refresh(due)
        assert runtime.error_code is None
        assert len(calls) == 1
        current = store.snapshot(due)['current']
        assert datetime.fromisoformat(current['occurred_at']) == due
        assert current['source_id'] != 'synthetic:initial'
        await runtime.refresh(due + timedelta(minutes=1))
        assert len(calls) == 1

    asyncio.run(run())


def test_bathing_and_sleep_defer_expired_rest_until_waking_without_backfill(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.db')
    start = datetime(2026, 9, 6, 15, tzinfo=timezone.utc)  # 23:00 Shanghai.
    store.publish_day('synthetic:evening',
                      dict(location='home', activity='rest', note='A quiet evening.'),
                      [], occurred_at=start, activity_kind='rest')
    calls = []

    class Gateway:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(life_decision(messages, kind='rest')))

    runtime = DailyLifeRuntime(store, Gateway, lambda: 'A student.')

    async def run():
        for minutes, phase in [(30, 'quiet'), (60, 'sleep'), (540, 'sleep')]:
            now = start + timedelta(minutes=minutes)
            assert store.snapshot(now)['rhythm']['phase'] == phase
            assert store.snapshot(now)['stale'] == (minutes >= 60)  # Rest expires after an hour.
            await runtime.refresh(now)
            assert not calls
        awake = start + timedelta(hours=9, minutes=30)
        await runtime.refresh(awake)
        assert len(calls) == 1
        assert datetime.fromisoformat(store.snapshot(awake)['current']['occurred_at']) == awake
        with store._db() as db:
            assert db.execute("SELECT COUNT(*) FROM life_moments WHERE kind='daily'").fetchone()[0] == 2

    asyncio.run(run())
