"""What the cloud needs to know when a call starts, and whether she picks up.

The pack holds facts only (persona, relationship, memories, recent chat, her current life); all
wording of the call lives on the server. Every part is optional except the persona, and a part
that cannot be read is left out rather than failing the call.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

LOCAL_WEEKDAYS = '一二三四五六日'
STAGES = {'unknown': '还在慢慢建立', 'acquaintance': '刚认识不久，还有些拘谨', 'familiar': '渐渐熟了',
          'friend': '朋友，在一起很自在', 'trusted_friend': '彼此信赖的朋友，愿意说心里话',
          'close': '很亲近，会想对方', 'committed': '已经确认了彼此的关系'}
MEMORY_COUNT = 40
RECENT_LINES = 20
STYLE_SAMPLES = 6

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
    lines += [d.statement for d in snapshot.declarations if d.inclusion == 'core' and d.statement]
    samples = [e.assistant_text for e in snapshot.style_exemplars if e.assistant_text][:STYLE_SAMPLES]
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


def world(server, now: datetime) -> tuple[dict, dict]:
    """(what she is doing now, as plain facts; the rhythm it came from)."""
    snapshot = server.daily_life_runtime.store.snapshot(now)
    current = snapshot.get('current') or {}
    rhythm = snapshot.get('rhythm') or {}
    facts = {'now': local_now_text(now)}
    if current.get('activity'):
        facts['activity'] = current['activity']
    if current.get('location'):
        facts['location'] = current['location']
    note = rhythm.get('activity') or ''
    if current.get('note'):
        note = (current['note'] + ('；' + note if note else ''))
    if note:
        facts['note'] = note
    today = [a.get('note') or a.get('activity') for a in (snapshot.get('world') or {}).get('today_activities', [])]
    facts['today'] = [t for t in today if t][-8:]
    plans = []
    schedule = (snapshot.get('world') or {}).get('schedule') or {}
    if schedule.get('next_class'):
        plans.append('下一节课：' + str(schedule['next_class'].get('name') or schedule['next_class'].get('title') or '课程'))
    for project in snapshot.get('projects') or []:
        if project.get('next_activity') and project.get('status') not in ('completed', 'cancelled'):
            plans.append(f"{project.get('title', '')}：{project['next_activity']}")
    facts['plans'] = plans[:6]
    return facts, {'rhythm': rhythm, 'current': current, 'schedule': schedule}


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
        'world': facts,
    }
    return {k: v for k, v in context.items() if v}, pickup(state)
