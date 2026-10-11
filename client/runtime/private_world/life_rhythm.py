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
    "作息只是计划，可以因为当下的事调整：她此刻正在回复就说明醒着，不说自己在睡、还没醒或被吵醒，"
    "可以自然表达困了、该睡了，但不借作息敷衍或打发对方。"
    "authored_sleep另指已发布的角色睡眠过程（nap补觉或overnight夜间睡眠）：sleeping是已开始，due是到续接时刻或通信打断后待判断；"
    "到时不自动证明睡完或恢复。wellbeing.basis=night_correspondence只来自通信负荷，不证明疾病或请假。"
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


def rest_timeline(now: datetime, exchanges: list[tuple[datetime, datetime]], shifts: dict | None = None, *, since=None, initial_load=0) -> dict:
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
            overlap = (min(end, finish, now) - max(start, begin, since or begin)).total_seconds() / 60
            if overlap > 0:
                awake += overlap
                wakes += int(start > begin and (since is None or start >= since))
        if begin <= now and (since is None or finish > since):
            nights.append({'begin': begin, 'end': finish, 'awake': awake, 'wakes': wakes})
        day = tomorrow
    debt = peak_debt = float(initial_load)
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


def _rest(load):
    return 'depleted' if load >= 120 else 'tired' if load >= FATIGUE_LOAD_MINUTES else 'rested'


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
    rest = _rest(debt)
    # These thresholds drive fictional continuity, not a disease probability.
    unwell = debt >= 240 and timeline['strained_nights'] >= 2
    consult = unwell and debt >= 360 and timeline['strained_nights'] >= 3
    recovering = timeline['previous_strain'] and timeline['recovering']
    wellbeing = {
        'basis': 'night_correspondence',
        'state': 'unwell' if unwell else 'recovering' if recovering else 'well',
        'care': 'consider_consultation' if consult else 'rest' if rest != 'rested' else 'none',
        'summary': ('连续休息不足，身体不太舒服，减少练习；若休息后仍不适，安排门诊咨询。' if consult else
                    '连续休息不足，身体不太舒服，今天先减少活动、好好休息。' if unwell else
                    '正在慢慢恢复，先不把日程排满。' if recovering else
                    '没有持续身体不适的记录。'),
    }
    labels = {'bathing': '林离洗澡中', 'sleep': '计划休息的时段', 'interrupted_rest': '夜里还醒着，在和对方聊天',
              'breakfast': '早餐时间', 'lunch': '午饭时间', 'dinner': '晚饭时间',
              'quiet': '准备收工休息', 'focus': '留给练习和创作的时间', 'free': '自己的闲暇时间'}
    return {'phase': phase, 'rest': rest, 'current_load_minutes': debt, 'local_time': local.isoformat(),
            'historical_rest': {'load_minutes': debt, 'rest': rest,
                'meaning': '夜间通信历史负荷，不是当前精力测量；不证明已睡着或当前仍需一直休息。'},
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


def with_recovery(state, episodes, now, *, exchanges=None, shifts=None):
    """Persist authored energy recovery; later correspondence adds only new load.

    Past correspondence load remains intact. Energy recovery cannot clear independently
    evidenced illness. A nap needs an authored start and a later JEV resolution.
    """
    result = state
    visible = sorted((e for e in episodes if datetime.fromisoformat(e['occurred_at']) <= now),
                     key=lambda e: (e['occurred_at'], e['source_id']), reverse=True)
    for episode in visible:
        recovery = episode.get('effects', {}).get('body_recovery')
        if not isinstance(recovery, dict) or episode.get('activity_kind') != 'rest':
            continue
        stamp = datetime.fromisoformat(episode['occurred_at'])
        if now - stamp > timedelta(days=14):
            continue
        if exchanges is None and state['historical_rest']['load_minutes'] > recovery.get('baseline_load_minutes', -1):
            continue  # New night-time load invalidates an earlier recovery.
        rest = recovery.get('rest')
        if rest not in {'tired', 'rested'} or episode.get('result', {}).get('status') != 'completed':
            continue
        remaining = recovery.get('remaining_load_minutes', 0 if rest == 'rested' else min(recovery['baseline_load_minutes'], 119))
        load = (rest_timeline(now, exchanges, shifts, since=stamp, initial_load=remaining)['load_minutes']
                if exchanges is not None else remaining)
        rest = _rest(load)
        wellbeing = state['wellbeing']
        if wellbeing.get('basis') == 'night_correspondence':
            wellbeing = {**wellbeing, 'state': 'well' if rest == 'rested' else 'recovering',
                         'care': 'none' if rest == 'rested' else 'rest',
                         'summary': '这段休息后精力有所恢复。' if rest == 'rested' else '恢复后又有新的消耗，放慢节奏安排生活。'}
        result = {**state, 'rest': rest, 'current_load_minutes': load,
            'recovery': {'source_id': episode['source_id'], 'occurred_at': episode['occurred_at'],
                         'meaning': '本次已发布休息过程的精力恢复，不代表睡眠或疾病痊愈。'},
            'wellbeing': wellbeing,
            'note': '这段休息后精力有所恢复，可以按意愿接回轻活动。' if rest == 'rested' else '这段休息后缓过来一些，仍适合放慢节奏。'}
        break
    latest = next((e for e in visible if e.get('effects', {}).get('sleep_plan')), None)
    resolved = {e['effects']['sleep_resolution']['source_id'] for e in visible if e.get('effects', {}).get('sleep_resolution')}
    if latest and latest['source_id'] not in resolved:
        start = datetime.fromisoformat(latest['occurred_at'])
        end = datetime.fromisoformat(latest['effects']['sleep_plan']['end_at'])
        interrupted = any(received <= now and received < end and completed > start
                          for received, completed in exchanges or [])
        due = now >= end or interrupted
        result = {**result, 'authored_sleep': {'source_id': latest['source_id'], 'started_at': latest['occurred_at'],
                   'end_at': end.isoformat(), 'kind': latest['effects']['sleep_plan'].get('kind', 'nap'),
                   'status': 'due' if due else 'sleeping', 'interrupted': interrupted}}
        if not due:
            result.update(phase='sleep', activity='夜间睡眠中' if latest['effects']['sleep_plan'].get('kind') == 'overnight' else '补觉中',
                          availability='rest', phase_basis='authored_sleep')
    return result
