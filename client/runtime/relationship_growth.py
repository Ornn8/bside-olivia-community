"""Relationship evidence and independent growth allowances."""
from datetime import datetime, timedelta, timezone

DIMENSIONS = ('familiarity', 'trust', 'comfort', 'closeness', 'tension')
CHOICES = {
    'rel_disclosure': ('none', 'everyday', 'personal', 'vulnerable'),
    'rel_care': ('none', 'user_to_her', 'her_to_user', 'mutual'),
    'rel_shared': ('none', 'plan', 'kept', 'created'),
    'rel_affection': ('none', 'warm', 'clear'),
    'rel_friction': ('none', 'tension', 'conflict', 'repair'),
    'rel_boundary': ('none', 'respected'),
}
CRITERIA = {
    'rel_disclosure': {'none': '只有寒暄、指令或对她的提问', 'everyday': '用户说了自己当下的近况、日常安排',
        'personal': '用户讲了自己的具体经历、喜好、家人朋友、计划细节，是在让她了解自己',
        'vulnerable': '用户说了心事、压力、脆弱或不常对人说的事。不算转述别人、玩笑式夸张、问她问题'},
    'rel_care': {'none': '没有具体关心；礼貌问候、单方面说辛苦了没有后续不算',
        'user_to_her': '用户具体地关心、帮助或支持她（不是一句客套），她接受了',
        'her_to_user': '她具体地接住了用户的情绪或困难，用户有回应', 'mutual': '两者都有'},
    'rel_shared': {'none': '没有共同事项；她单方面编的回忆、空泛的下次吧不算',
        'plan': '双方一起定下具体的约定或计划，时间或内容明确', 'kept': '之前说好的事这轮真的兑现或有了后续',
        'created': '两人一起完成了一个东西，例如起名、写故事、选歌、做决定'},
    'rel_affection': {'none': '没有表达亲近', 'warm': '用户轻轻地表达亲近、想念、在意',
        'clear': '用户明确表达喜欢、爱或你很重要。挚友和家人路线下是亲情和友情的亲近，不是恋爱'},
    'rel_friction': {'none': '没有摩擦', 'tension': '小摩擦、误会、不耐烦，但没有伤害',
        'conflict': '用户明确伤害她且她抵触；不能从她的责备倒推用户伤害', 'repair': '化解了已有的矛盾'},
    'rel_boundary': {'none': '没有明确边界事件', 'respected': '一方明确表达了不想要的事，另一方尊重并有回应'},
}
DEFAULTS = {
    'everyday_daily': 3, 'deep_daily': 4, 'deep_weekly': 10,
    'deltas': {
        'disclosure_everyday': {'familiarity': 1, 'comfort': 1},
        'disclosure_personal': {'familiarity': 1, 'trust': 1},
        'disclosure_vulnerable': {'trust': 2, 'closeness': 1},
        'care_user_to_her': {'comfort': 1, 'trust': 1},
        'care_her_to_user': {'comfort': 1, 'trust': 1},
        'care_mutual': {'comfort': 1, 'trust': 1},
        'shared_plan': {'closeness': 1},
        'shared_kept': {'closeness': 2, 'trust': 1},
        'shared_created': {'closeness': 2, 'trust': 1},
        'affection_warm': {'closeness': 1}, 'affection_clear': {'closeness': 2},
        'boundary_respected': {'trust': 1, 'tension': -1},
        'friction_tension': {'tension': 1},
        'friction_conflict': {'trust': -2, 'comfort': -2, 'tension': 3},
        'friction_repair': {'trust': 1, 'comfort': 1, 'tension': -2},
    },
}


def validate_signals(signals):
    if (not isinstance(signals, dict) or set(signals) - CHOICES.keys()
            or any(value not in CHOICES[key] for key, value in signals.items())):
        raise ValueError('RELATIONSHIP_SIGNALS_INVALID')
    return dict(signals)


def growth_updates(state, signals, now, policy):
    validate_signals(signals)
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    since = (today - timedelta(days=6)).isoformat()
    usage = [list(row) for row in state.relationship_growth if since <= row[0] <= today.isoformat()]
    changes = {}
    for channel in ('everyday', 'deep'):
        deltas = {dimension: 0 for dimension in DIMENSIONS}
        decreases = {dimension: 0 for dimension in DIMENSIONS}
        for key, value in signals.items():
            name = key.removeprefix('rel_') + '_' + value
            if value == 'none' or (name == 'disclosure_everyday') != (channel == 'everyday'):
                continue
            for dimension, delta in policy['deltas'][name].items():
                if delta < 0:
                    decreases[dimension] += delta
                else:
                    deltas[dimension] += delta
        for dimension, delta in deltas.items():
            if delta > 0 and dimension != 'tension':
                used_day = sum(row[3] for row in usage if row[:3] == [today.isoformat(), channel, dimension])
                remaining = policy[channel + '_daily'] - used_day
                if channel == 'deep':
                    remaining = min(remaining, policy['deep_weekly'] - sum(
                        row[3] for row in usage if row[1:3] == [channel, dimension]))
                delta = min(delta, max(0, remaining))
                if delta:
                    row = next((row for row in usage if row[:3] == [today.isoformat(), channel, dimension]), None)
                    if row is None:
                        usage.append([today.isoformat(), channel, dimension, delta])
                    else:
                        row[3] += delta
            changes[dimension] = min(100, max(0, changes.get(dimension, getattr(state, dimension)) + delta + decreases[dimension]))
    floors = {}
    if state.relationship_volume >= 2:
        floors['familiarity'] = 1
    if state.relationship_volume >= 3:
        floors.update(familiarity=35, comfort=35)
    if state.relationship_volume >= 4:
        floors['trust'] = 35
    for dimension, floor in floors.items():
        changes[dimension] = max(floor, changes[dimension])
    ceiling = max(changes['trust'], changes['comfort']) + 15
    changes['closeness'] = max(state.closeness, min(changes['closeness'], ceiling))
    changes['relationship_growth'] = tuple(tuple(row) for row in sorted(usage))
    days = tuple(sorted(set(state.relationship_days) | {today.isoformat()}))
    changes['relationship_days'] = days
    changes.update(volume_updates(state, changes, len(days), policy=policy.get('stages')))
    return changes


def route(state):
    return state.relationship_route or ('romantic' if state.relationship_stage in {'close', 'committed'} else 'undecided')


def volume_updates(state, changes, days, *, policy=None):
    from runtime.memory.effective_relationship import effective_relationship_stage
    chosen = changes.get('relationship_route', route(state))
    stage = changes.get('relationship_stage', state.relationship_stage)
    volume = state.relationship_volume
    projected = {**state.to_dict(), **changes}
    effective = effective_relationship_stage(projected, {}, policy=policy)
    volume = max(volume, {'familiar': 2, 'friend': 3, 'trusted_friend': 4, 'close': 5, 'committed': 5}.get(effective, 1))
    if volume >= 4 and chosen in {'romantic', 'friend', 'family'}:
        volume = max(volume, 5)
    if volume >= 5 and state.relationship_volume >= 5 and days - state.volume_started_days >= 60:
        volume += (days - state.volume_started_days) // 60
    result = {'relationship_route': chosen, 'relationship_volume': volume}
    if volume != state.relationship_volume:
        result['volume_started_days'] = days
    return result
