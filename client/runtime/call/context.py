"""What the cloud needs to know when a call starts, and whether she picks up.

The pack holds facts only (persona, relationship, memories, recent chat, her current life); all
wording of the call lives on the server. Every part is optional except the persona, and a part
that cannot be read is left out rather than failing the call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

LOCAL_WEEKDAYS = '一二三四五六日'
STAGES = {'unknown': '还在慢慢建立', 'acquaintance': '刚认识不久，还有些拘谨', 'familiar': '渐渐熟了',
          'friend': '朋友，在一起很自在', 'trusted_friend': '彼此信赖的朋友，愿意说心里话',
          'close': '很亲近，会想对方', 'committed': '已经确认了彼此的关系'}
MEMORY_COUNT = 40
RECENT_LINES = 20
RECENT_CALLS = 3
CALLS_WITHIN = timedelta(hours=48)
MEAL_SLOTS = {'breakfast': '早饭', 'lunch': '午饭', 'dinner': '晚饭', 'snack': '加餐'}
MEAL_STATUS = {'eaten': '吃过了', 'eating': '正在吃', 'planned': '打算吃', 'skipped': '没吃'}
# METAR cloud cover and weather codes, as plain words.
CLOUDS = (('OVC', '阴天'), ('BKN', '多云'), ('SCT', '少云'), ('FEW', '少云'), ('CAVOK', '晴'), ('SKC', '晴'),
          ('CLR', '晴'), ('NSC', '晴'))
WEATHER = (('TS', '有雷雨'), ('SN', '在下雪'), ('RA', '在下雨'), ('DZ', '下毛毛雨'), ('FG', '有雾'), ('BR', '有点雾'),
           ('HZ', '有霾'))
STYLE_SAMPLES = 6
# A call is spoken chat: the letter and music rules only crowd out how she talks.
CALL_MODES = (None, 'spoken_video', 'future_im')

log = logging.getLogger(__name__)


def _optional(read, default=None):
    try:
        return read()
    except Exception as exc:  # one missing part never blocks a call
        log.info('call context part unavailable: %s', type(exc).__name__)
        return default


def persona_text(server) -> str:
    from persona_loader import load_persona
    snapshot = load_persona(server.letters_adapter.persona_v2_path).snapshot
    if snapshot.status != 'READY':
        raise RuntimeError('CALL_PERSONA_UNAVAILABLE')
    lines = []
    if snapshot.profile is not None:
        lines.append(f'{snapshot.profile.display_name}。{snapshot.profile.summary}')
    lines += [d.statement for d in snapshot.declarations
              if d.inclusion == 'core' and d.statement and d.mode in CALL_MODES]
    samples = [e.assistant_text for e in snapshot.style_exemplars
               if e.assistant_text and e.mode == 'spoken_video'][:STYLE_SAMPLES]
    if samples:
        lines.append('她说话的样子（只学语气，不当事实）：' + ' / '.join(samples))
    return '\n'.join(lines)


def relationship_text(server) -> str:
    from runtime.memory.effective_relationship import effective_relationship_stage, relationship_history
    stage = effective_relationship_stage(server.private_world_port.snapshot(), relationship_history(server.store))
    return '关系阶段：' + STAGES.get(stage, STAGES['unknown']) + '。语气和亲密程度只按这个阶段来，不越界。'


def nicknames(server) -> list[str]:
    from runtime.memory.private_world_projection import project_private_world
    from runtime.memory.effective_relationship import relationship_history
    projected = project_private_world(server.private_world_port.snapshot(), relationship_history(server.store))
    return list(projected.authorized_nicknames)


def memories(backend) -> list[str]:
    result = backend.browse_memories(query=None, page=1, limit=MEMORY_COUNT, days=0, sort='new')
    return [m.text for m in result['memories'] if getattr(m, 'text', '')]


def recent_chat(server) -> list[dict]:
    rows = [r for r in getattr(server.store, 'personal_chats', [])
            if r.get('delivery_status') == 'DELIVERED' and r.get('channel') in ('qq', 'wechat')]
    rows.sort(key=lambda r: r.get('life_received_at') or '')
    lines = []
    for row in rows[-RECENT_LINES:]:
        if row.get('content') and row.get('origin') != 'proactive':
            lines.append({'role': 'user', 'text': str(row['content'])[:200], 'at': row.get('life_received_at')})
        if row.get('reply_text'):
            lines.append({'role': 'her', 'text': str(row['reply_text'])[:200], 'at': row.get('life_received_at')})
    return lines[-RECENT_LINES:]


def local_now_text(now: datetime) -> str:
    from runtime.private_world.life_rhythm import LOCAL
    local = now.astimezone(LOCAL)
    hour = local.hour
    part = ('凌晨' if hour < 5 else '早上' if hour < 9 else '上午' if hour < 12 else '中午' if hour < 14
            else '下午' if hour < 18 else '晚上')
    return f'星期{LOCAL_WEEKDAYS[local.weekday()]}{part}{local:%H:%M}'


def _local(value):
    from runtime.private_world.life_rhythm import LOCAL
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(LOCAL)


def meals_today(meals, now: datetime) -> list[str]:
    """Today's meals as recorded in her life, e.g. "午饭：米饭和番茄炒蛋（12:00，吃过了）"."""
    from runtime.private_world.life_rhythm import LOCAL
    today = now.astimezone(LOCAL).date().isoformat()
    lines = []
    order = list(MEAL_SLOTS)
    for meal in sorted((m for m in meals or [] if m.get('date') == today and m.get('food')),
                       key=lambda m: order.index(m['slot']) if m.get('slot') in order else 9):
        status = MEAL_STATUS.get(meal.get('status'), '')
        if meal.get('status') == 'eating' and meal.get('stale'):
            status = '之前在吃，现在不确定吃完没有'
        at = _local(meal.get('started_at') or meal.get('scheduled_for') or meal.get('occurred_at'))
        when = f'{at:%H:%M}，' if at else ''
        lines.append(f"{MEAL_SLOTS.get(meal.get('slot'), '一顿饭')}：{meal['food']}（{when}{status}）")
    return lines


def weather_text(weather) -> str:
    if not isinstance(weather, dict) or weather.get('status') != 'fresh':
        return ''
    codes, cover = str(weather.get('weather_codes') or ''), str(weather.get('cloud_cover') or '')
    sky = next((word for code, word in WEATHER if code in codes), '') or next((word for code, word in CLOUDS if code in cover), '')
    temperature = weather.get('temperature_c')
    parts = [weather.get('city') or '上海'] + ([f'{round(temperature)}度'] if type(temperature) in (int, float) else []) + ([sky] if sky else [])
    return ' '.join(parts) + '（气象站实测，不代表窗外此刻一定如此）'


def episodes_today(episodes, now: datetime) -> list[str]:
    """What happened to her today, in a few words each (obstacle → outcome)."""
    from runtime.private_world.life_rhythm import LOCAL
    today, lines = now.astimezone(LOCAL).date(), []
    for episode in reversed(episodes or []):
        at = _local(episode.get('occurred_at'))
        if not at or at.date() != today:
            continue
        steps = [step for step in episode.get('process') or [] if isinstance(step, dict)]
        obstacle = '；'.join(step['obstacle'] for step in steps if step.get('obstacle'))
        result = (episode.get('result') or {}).get('detail') or ''
        text = '，'.join(t for t in (obstacle, result) if t)
        if text:
            lines.append(f'{at:%H:%M} {text}')
    return lines[-4:]


def classes_today(schedule, now: datetime) -> list[str]:
    lines = []
    for item in (schedule or {}).get('classes') or []:
        start, end = _local(item.get('start')), _local(item.get('end'))
        if not start or not end:
            continue
        state = '已经下课' if now >= end else '正在上' if now >= start else '还没开始'
        lines.append(f"{item.get('title') or item.get('name') or '课'} {start:%H:%M}-{end:%H:%M}（按课表{state}）")
    return lines


def world(server, now: datetime) -> tuple[dict, dict]:
    """(what she is doing now and what her day has held, as plain facts; the rhythm it came from)."""
    snapshot = server.daily_life_runtime.store.snapshot(now)
    current = snapshot.get('current') or {}
    rhythm = snapshot.get('rhythm') or {}
    life = snapshot.get('world') or {}
    facts = {'now': local_now_text(now)}
    if snapshot.get('stale') and current:
        # An old observation, not what she is doing now (her day has moved on since).
        at = _local(current.get('occurred_at'))
        what = '，'.join(t for t in (current.get('activity'), current.get('location'), current.get('note')) if t)
        if what:
            facts['last'] = (f'{at:%H:%M} ' if at else '') + what
    else:
        if current.get('activity'):
            facts['activity'] = current['activity']
        if current.get('location'):
            facts['location'] = current['location']
    note = rhythm.get('activity') or ''
    if current.get('note') and not snapshot.get('stale'):
        note = (current['note'] + ('；' + note if note else ''))
    if note:
        facts['note'] = note
    today = []
    for item in life.get('today_activities', []):
        text = item.get('note') or item.get('activity')
        if text and text not in today[-1:]:
            today.append(text)
    facts['today'] = today[-8:]
    facts['meals'] = meals_today(life.get('meals'), now)
    facts['weather'] = weather_text(life.get('weather'))
    facts['events'] = episodes_today(life.get('recent_episodes'), now)
    facts['classes'] = classes_today(life.get('schedule'), now)
    plans = []
    schedule = (snapshot.get('world') or {}).get('schedule') or {}
    if schedule.get('next_class'):
        plans.append('下一节课：' + str(schedule['next_class'].get('name') or schedule['next_class'].get('title') or '课程'))
    for project in snapshot.get('projects') or []:
        if project.get('next_activity') and project.get('status') not in ('completed', 'cancelled'):
            plans.append(f"{project.get('title', '')}：{project['next_activity']}")
    facts['plans'] = plans[:6]
    return {k: v for k, v in facts.items() if v}, {'rhythm': rhythm, 'current': current, 'schedule': schedule}


def concerns(server, now: datetime) -> list[str]:
    """What is on her mind from recent chats (her emotion state's open concerns), as QQ replies see it."""
    emotion = getattr(server.daily_life_runtime, 'emotion', None)
    if emotion is None:
        return []
    view = emotion.view(now, read_only=True)
    return [c['summary'] for c in view.get('concerns') or [] if isinstance(c, dict) and c.get('summary')][:3]


def recent_calls(server, now: datetime) -> list[dict]:
    """The last few finished calls (when, how long, what was said), so a new call does not contradict them."""
    rows = []
    for row in getattr(server.store, 'calls', None) or []:
        ended = _local(row.get('ended_at'))
        summary = ((row.get('notes') or {}).get('summary') or '').strip()
        if row.get('missed') or not ended or not summary or now - ended > CALLS_WITHIN:
            continue
        rows.append({'at': ended.astimezone(timezone.utc).isoformat(), 'minutes': max(1, round((row.get('seconds') or 0) / 60)),
                     'summary': summary[:400]})
    return rows[-RECENT_CALLS:]


def pickup(state: dict) -> str:
    """Owner's rule (2026-10-14): bath → no pickup; class → a whispered line; asleep → sleepy."""
    rhythm, current, schedule = state.get('rhythm') or {}, state.get('current') or {}, state.get('schedule') or {}
    if rhythm.get('phase') == 'bathing' or rhythm.get('authored_bath'):
        return 'missed'
    if schedule.get('current_class') and current.get('activity_kind') == 'class':
        return 'brief'
    if rhythm.get('phase') in ('sleep', 'interrupted_rest'):
        return 'sleepy'
    return 'answer'


def build(server, backend=None, now: datetime | None = None) -> tuple[dict, str]:
    """(context pack, pickup) for a call starting now."""
    now = now or datetime.now(timezone.utc)
    facts, state = world(server, now)
    names = _optional(lambda: nicknames(server), [])
    context = {
        'persona': persona_text(server),
        'user': ('她可以这样称呼对方：' + '、'.join(names)) if names else '',
        'relationship': _optional(lambda: relationship_text(server), ''),
        'memories': _optional(lambda: memories(backend), []) if backend is not None else [],
        'recent': _optional(lambda: recent_chat(server), []),
        'calls': _optional(lambda: recent_calls(server, now), []),
        'concerns': _optional(lambda: concerns(server, now), []),
        'world': facts,
    }
    return {k: v for k, v in context.items() if v}, pickup(state)
