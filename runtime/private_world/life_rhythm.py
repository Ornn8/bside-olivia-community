"""Character simulation, not a medical model. Derive rest from actual exchanges.

Shanghai civil time; no offline backfill, random sickness or absence penalties.
Only bounded recent exchange timestamps are needed, never letter contents.
"""
from datetime import datetime, timedelta, timezone

LOCAL = timezone(timedelta(hours=8))
SETTLE = timedelta(minutes=30)


def _shift(day, shifts):
    return next((shifts[key] for key in sorted(shifts, reverse=True) if key <= day.isoformat()), 0)


def rest_timeline(now: datetime, exchanges: list[tuple[datetime, datetime]], shifts: dict | None = None) -> dict:
    """Union real correspondence intervals; sleep resumes after quiet settling.

    An interval begins at receipt and ends at canonical reply completion.
    Thirty quiet minutes is the agreed character simulation parameter, not a measurement
    of a human falling asleep. Overlaps and repeated reads never add time twice.
    """
    merged = []
    for start, end in sorted(set(exchanges)):
        if start > now or end < start:
            continue
        end = end + SETTLE
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    local = now.astimezone(LOCAL)
    day = local.date() - timedelta(days=14)
    nights = []
    while day <= local.date():
        offset = timedelta(minutes=_shift(day, shifts or {}))
        begin = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL) + timedelta(hours=23) + offset
        tomorrow = day + timedelta(days=1)
        wake = 8 if tomorrow.weekday() >= 5 else 7
        finish = datetime.combine(tomorrow, datetime.min.time(), tzinfo=LOCAL) + timedelta(hours=wake) + offset
        awake, wakes = 0.0, 0
        for start, end in merged:
            overlap = (min(end, finish, now) - max(start, begin)).total_seconds() / 60
            if overlap > 0:
                awake += overlap
                wakes += int(start > begin)
        if begin <= now:
            nights.append({'end': finish, 'awake': awake, 'wakes': wakes})
        day = tomorrow
    debt = 0.0
    for night in nights:
        loss = night['awake'] + 10 * night['wakes'] * (night['wakes'] + 1) / 2
        # Recovery only after later sleep, never from clock time while awake.
        if not loss and night['end'] <= now:
            debt = max(0, debt - 90)
        debt += loss
    return {'awake_minutes': sum(n['awake'] for n in nights),
            'interruptions': sum(n['wakes'] for n in nights),
            'load_minutes': debt,
            'awake_now': bool(merged and merged[-1][0] <= now < merged[-1][1])}


def rhythm(now: datetime, exchanges: list[tuple[datetime, datetime]], shifts: dict | None = None) -> dict:
    local = now.astimezone(LOCAL)
    shifts = shifts or {}
    wake_hour = 8 if local.weekday() >= 5 else 7
    prior_shift = _shift(local.date() - timedelta(days=1), shifts)
    shift = _shift(local.date(), shifts)
    wake_hour += prior_shift / 60
    hour = local.hour + local.minute / 60
    sleeping = hour >= 23 + shift / 60 or (hour < wake_hour and hour >= -1 + prior_shift / 60)
    phase = ('sleep' if sleeping else
             'quiet' if hour < 5 else
             'breakfast' if hour < wake_hour + 1 else
             'lunch' if 12 <= hour < 13 else
             'dinner' if 18 <= hour < 19 else
             'quiet' if hour >= 22 else
             'focus' if 9 <= hour < 12 or 14 <= hour < 17 else 'free')
    timeline = rest_timeline(now, exchanges, shifts)
    debt, awake = timeline['load_minutes'], timeline['awake_now']
    if phase == 'sleep' and awake:
        phase = 'interrupted_rest'
    rest = 'depleted' if debt >= 120 else 'tired' if debt > 0 else 'rested'
    labels = {'sleep': '正在休息', 'interrupted_rest': '夜里醒来，准备继续休息',
              'breakfast': '早餐时间', 'lunch': '午饭时间', 'dinner': '晚饭时间',
              'quiet': '准备收工休息', 'focus': '留给练习和创作的时间', 'free': '自己的闲暇时间'}
    return {'phase': phase, 'rest': rest, 'local_time': local.isoformat(),
            'sleep_shift_minutes': shift,
            'activity': labels[phase],
            'note': ('休息不足，今天减少安排，把休息放在前面。' if rest == 'depleted' else
                     '最近休息受到影响，放慢一点，留时间补觉。' if rest == 'tired' else
                     '按自己的节奏生活。'),
            'availability': 'rest' if phase in {'sleep', 'interrupted_rest', 'quiet'} else
                            'busy' if phase in {'focus', 'breakfast', 'lunch', 'dinner'} else 'open'}
