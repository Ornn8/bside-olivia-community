"""Character simulation, not a medical model. Derive rest from actual exchanges.

Shanghai civil time; no offline backfill, random sickness or absence penalties.
Only bounded recent exchange timestamps are needed, never letter contents.
"""
from datetime import datetime, timedelta, timezone

LOCAL = timezone(timedelta(hours=8))
SETTLE = timedelta(minutes=30)
# Fictional state threshold: a brief exchange is activity, not yet fatigue.
FATIGUE_LOAD_MINUTES = 30
RHYTHM_FACT_AUTHORITY = (
    "phase 来自计划时段与通信记录；sleep 是计划休息时段，interrupted_rest 是夜间通信后的休息阶段。"
    "它们不证明她此前已经睡着，也不证明用户把她叫醒；wake_cause 未知时不编造叫醒原因或据此责备发信人。"
    "planned_rest_window 只支持她目前给自己定的休息安排，不证明长期习惯、实际入睡起床时刻、睡前活动或过去如何调整作息。"
)


def _shift(day, shifts):
    return next((shifts[key] for key in sorted(shifts, reverse=True) if key <= day.isoformat()), 0)


def _rest_window(day, shifts):
    """One simulated night plan, not a verified character fact or recorded sleep."""
    offset = timedelta(minutes=_shift(day, shifts))
    begin = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL) + timedelta(days=1) + offset
    tomorrow = day + timedelta(days=1)
    finish = datetime.combine(tomorrow, datetime.min.time(), tzinfo=LOCAL) + timedelta(hours=8, minutes=30) + offset
    return begin, finish


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
        begin, finish = _rest_window(day, shifts or {})
        tomorrow = day + timedelta(days=1)
        awake, wakes = 0.0, 0
        for start, end in merged:
            overlap = (min(end, finish, now) - max(start, begin)).total_seconds() / 60
            if overlap > 0:
                awake += overlap
                wakes += int(start > begin)
        if begin <= now:
            nights.append({'begin': begin, 'end': finish, 'awake': awake, 'wakes': wakes})
        day = tomorrow
    debt = peak_debt = 0.0
    for night in nights:
        loss = night['awake'] + 10 * night['wakes'] * (night['wakes'] + 1) / 2
        # Recovery only after later sleep, never from clock time while awake.
        if not loss and night['end'] <= now:
            debt = max(0, debt - 90)
        debt += loss
        peak_debt = max(peak_debt, debt)
    return {'awake_minutes': sum(n['awake'] for n in nights),
            'interruptions': sum(n['wakes'] for n in nights),
            'load_minutes': debt,
            'recovering': 0 < debt < peak_debt,
            'strained_nights': sum(n['awake'] >= 90 and n['begin'] >= now - timedelta(days=7) for n in nights),
            'previous_strain': any(n['awake'] >= 90 for n in nights),
            'awake_now': bool(merged and merged[-1][0] <= now < merged[-1][1])}


def rhythm(now: datetime, exchanges: list[tuple[datetime, datetime]], shifts: dict | None = None) -> dict:
    local = now.astimezone(LOCAL)
    shifts = shifts or {}
    wake_hour = 8.5
    prior_shift = _shift(local.date() - timedelta(days=1), shifts)
    shift = _shift(local.date(), shifts)
    wake_hour += prior_shift / 60
    hour = local.hour + local.minute / 60
    windows = [_rest_window(local.date() + timedelta(days=i), shifts) for i in (-2, -1, 0, 1)]
    begin, finish = next((start, end) for start, end in windows if end > local)
    sleeping = begin <= local < finish
    bathing = begin - timedelta(minutes=30) <= local < begin
    phase = ('bathing' if bathing else 'sleep' if sleeping else
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
    rest = 'depleted' if debt >= 120 else 'tired' if debt >= FATIGUE_LOAD_MINUTES else 'rested'
    # These thresholds drive fictional continuity, not a disease probability.
    unwell = debt >= 240 and timeline['strained_nights'] >= 2
    consult = unwell and debt >= 360 and timeline['strained_nights'] >= 3
    recovering = timeline['previous_strain'] and timeline['recovering']
    wellbeing = {
        'state': 'unwell' if unwell else 'recovering' if recovering else 'well',
        'care': 'consider_consultation' if consult else 'rest' if rest != 'rested' else 'none',
        'summary': ('连续休息不足，身体不太舒服，减少练习；若休息后仍不适，安排门诊咨询。' if consult else
                    '连续休息不足，身体不太舒服，今天先减少活动、好好休息。' if unwell else
                    '正在慢慢恢复，先不把日程排满。' if recovering else
                    '没有持续身体不适的记录。'),
    }
    labels = {'bathing': '林离洗澡中', 'sleep': '计划休息的时段', 'interrupted_rest': '夜间通信后，准备继续休息',
              'breakfast': '早餐时间', 'lunch': '午饭时间', 'dinner': '晚饭时间',
              'quiet': '准备收工休息', 'focus': '留给练习和创作的时间', 'free': '自己的闲暇时间'}
    return {'phase': phase, 'rest': rest, 'local_time': local.isoformat(),
            'phase_basis': 'schedule_and_correspondence', 'wake_cause': 'unknown',
            'planned_rest_window': {'kind': 'current_plan', 'start': begin.isoformat(), 'end': finish.isoformat()},
            'bath_end_at': begin.timestamp() if bathing else None,
            'wellbeing': wellbeing,
            'sleep_shift_minutes': shift,
            'activity': labels[phase],
            'note': ('休息不足，今天减少安排，把休息放在前面。' if rest == 'depleted' else
                     '最近休息受到影响，放慢一点，留时间补觉。' if rest == 'tired' else
                     '按自己的节奏生活。'),
            'availability': 'rest' if phase in {'bathing', 'sleep', 'interrupted_rest', 'quiet'} else
                            'busy' if phase in {'focus', 'breakfast', 'lunch', 'dinner'} else 'open'}
