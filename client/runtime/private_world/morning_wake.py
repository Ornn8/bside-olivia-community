"""Callable composition of the existing world author and daily video queue.

No clock scheduler: a night plan, sleeping, and waking are separate states.
The cloud owns the selfie recipe; only the spoken copy crosses this bridge.
"""
from datetime import datetime, timedelta, timezone
import json

from .life_episode import create, overnight_plan

LOCAL = timezone(timedelta(hours=8))


def _published(store, source_id):
    with store._db() as db:
        row = db.execute("SELECT payload FROM life_moments WHERE source_id=? AND kind='daily'",
                         (source_id,)).fetchone()
    return json.loads(row[0]) if row else None


async def author_night_sleep(store, port, *, source_id, now, end_at):
    """Offer an explicit plan; only the selected night_sleep path starts sleep."""
    plan = overnight_plan(now, end_at)
    existing = _published(store, source_id)
    if existing:
        with store._db() as db:
            row = db.execute('SELECT payload FROM life_episodes WHERE source_id=?', (source_id,)).fetchone()
        saved_plan = json.loads(row[0])['effects'].get('sleep_plan') if row else None
        if (datetime.fromisoformat(existing['occurred_at']) != now or not row
                or (saved_plan is not None and saved_plan != plan)):
            raise ValueError('DAILY_LIFE_SOURCE_CONFLICT')
        return existing
    state = store.snapshot(now)
    if state['rhythm'].get('authored_sleep'):
        raise ValueError('LIFE_EPISODE_SLEEP_INVALID')
    episode = await create(port, source_id, now, 'rest', {**state, 'overnight_sleep_candidate': plan})
    store.publish_day(source_id, dict(location='住处', activity='休息', note=episode['result']['detail']),
                      [], occurred_at=now, activity_kind='rest', episode=episode)
    return _published(store, source_id)


async def author_morning_wake(store, port, worker, *, source_id, now, spoken_text):
    """Author once, then enqueue the same source; retries never author again."""
    from runtime.personal_chat.daily_video import validate_input
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('DAILY_VIDEO_INPUT_INVALID')
    value = validate_input(dict(version=1, event_id=source_id,
        target_date=now.astimezone(LOCAL).date().isoformat(), event_at=now.astimezone(timezone.utc).isoformat(),
        event_kind='wake_up', scene_id='bedroom', certainty='live', spoken_text=spoken_text))
    existing = _published(store, source_id)
    if existing:
        if datetime.fromisoformat(existing['occurred_at']) != now:
            raise ValueError('DAILY_LIFE_SOURCE_CONFLICT')
        if existing.get('event_kind') != 'wake_up':
            return None
    else:
        state = store.snapshot(now)
        sleep = state['rhythm'].get('authored_sleep') or {}
        if (sleep.get('kind') != 'overnight' or sleep.get('status') != 'due' or sleep.get('interrupted')
                or not 4 <= now.astimezone(LOCAL).hour < 12
                or datetime.fromisoformat(sleep['end_at']).astimezone(LOCAL).date() != now.astimezone(LOCAL).date()):
            raise ValueError('MORNING_WAKE_SOURCE_UNAVAILABLE')
        episode = await create(port, source_id, now, 'rest', state)
        store.publish_day(source_id, dict(location='住处', activity='醒来' if episode['effects'].get('morning_wake') else '休息',
                                         note=episode['result']['detail']),
                          [], occurred_at=now, activity_kind='rest', episode=episode)
        if not episode.get('effects', {}).get('morning_wake'):
            return None
    if not store.daily_video_event_matches(value, now=now):
        raise ValueError('MORNING_WAKE_SOURCE_UNAVAILABLE')
    return await worker.enqueue(value)
