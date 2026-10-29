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
CRITERIA = {'rel_disclosure': {'none': '用户只是寒暄、回应她、提问、发指令或开玩笑，没有讲自己的事。例：在干嘛、哈哈哈、好呀',
                    'everyday': '用户顺口说了自己此刻在做什么或今天的安排，信息很浅。例：刚下班、在吃饭、准备睡了',
                    'personal': '用户主动讲了自己的经历、喜好、家人朋友、工作学业细节或长期打算，让她更了解自己；此刻在做什么不算',
                    'vulnerable': '用户说出心事、压力、孤独、失落、身体或情绪上的难受，或不常对人说的事；玩笑式夸张、只是说累了不算'},
 'rel_care': {'none': '没有具体的照顾。客套的辛苦了、早点睡、多喝水，普通问候、撒娇、调情都不算',
              'user_to_her': '她提到了具体的困难或状态，用户针对这件事给了认真的关心、建议或帮助，且她接受了',
              'her_to_user': '用户提到了具体的困难或情绪，她针对这件事认真接住并给了支持，用户有回应；她的日常叮嘱不算',
              'mutual': '本轮两者都有'},
 'rel_shared': {'none': '没有共同事项。空泛的下次一起、她单方面描述的回忆或打算、只是聊到某件事都不算',
                'plan': '双方在本轮明确约定了一件具体的事（做什么，最好有时间），并且用户同意了',
                'kept': '本轮在兑现之前约好的具体事，或报告约定的进展，例如之前约好拍猫、这轮发来了照片',
                'created': '两人在本轮一起完成了一个东西：给东西起名、一起编完一段故事、一起做了决定'},
 'rel_affection': {'none': '没有表达亲近',
                   'warm': '用户轻轻地表达亲近、想念、在意',
                   'clear': '用户明确表达喜欢、爱或你很重要。挚友和家人路线下是亲情和友情的亲近，不是恋爱'},
 'rel_friction': {'none': '没有真实的不愉快。调侃、斗嘴、撒娇式抱怨（你好坏、哼）、开玩笑的嫌弃、她吐槽别人都不算',
                  'tension': '用户认真地不高兴、不耐烦、失望或误会了她（不是玩笑），但没有伤人',
                  'conflict': '用户明确说了伤人的话，她表示抵触或受伤；不能从她的责备倒推用户伤害',
                  'repair': '之前有真实的不愉快，本轮双方道歉或说开了'},
 'rel_boundary': {'none': '没有明确的边界事件。开玩笑的不要、撒娇式拒绝、普通的不想吃都不算', 'respected': '一方认真表达了不想要、不想聊或不能做的事，另一方明确尊重并照做'}}
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


def history_day_updates(state, days, changes=None):
    """The days of imported letters count as days they talked, like the days of live chats.

    The days are merged into ``relationship_days`` (nothing is removed) and the
    story volume follows from the merged span through ``volume_updates``,
    which never lowers a volume already reached."""
    changes = dict(changes or {})
    merged = tuple(sorted(set(state.relationship_days) | set(days)))
    changes['relationship_days'] = merged
    changes.update(volume_updates(state, changes, len(merged)))
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
