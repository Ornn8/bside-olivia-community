"""Read-only friendship projection; never grants identity or action permissions."""


def _value(snapshot, name, default='unknown'):
    value = snapshot.get(name, default) if isinstance(snapshot, dict) else getattr(snapshot, name, default)
    return getattr(value, 'value', value)


def level(snapshot, name):
    value = _value(snapshot, name)
    if type(value) is int and 0 <= value <= 100:
        return 'unknown' if value == 0 else 'low' if value < 35 else 'medium' if value < 70 else 'high'
    return value if isinstance(value, str) and value in {'unknown', 'low', 'medium', 'high'} else 'unknown'


# Every meaningful exchange raises familiarity and comfort; trust and closeness
# move only on rarer support/repair/shared-experience evidence (reducer.py), so a
# month of daily contact typically leaves them "low". Requiring them at
# "medium" kept long-term users at "unknown" forever.
_EVERYDAY = ('familiarity', 'comfort')
_DEEP = ('trust', 'closeness')


def effective_relationship_stage(snapshot, history, *, policy=None):
    stage = _value(snapshot, 'relationship_stage')
    if _value(snapshot, 'relationship_route', '') in {'friend', 'family'} and stage in {'close', 'committed'}:
        stage = 'trusted_friend'
    if _value(snapshot, 'relationship_days', ()):
        from runtime.cloud_policy import get_policy
        policy = policy or get_policy('relationship_stages')
        days = len(_value(snapshot, 'relationship_days', ()))
        if stage in {'close', 'committed'}:
            return stage
        floor = {1: 'unknown', 2: 'familiar', 3: 'friend', 4: 'trusted_friend', 5: 'trusted_friend'}.get(
            min(5, _value(snapshot, 'story_volume', 1)), 'unknown')
        proposed = 'unknown'
        familiarity, comfort, trust, closeness = (_value(snapshot, key, 0) for key in ('familiarity', 'comfort', 'trust', 'closeness'))
        if days >= policy['familiar_days'] and familiarity >= policy['familiar_score']:
            proposed = 'familiar'
        if days >= policy['friend_days'] and min(familiarity, comfort) >= policy['friend_score'] and min(trust, closeness) > 0:
            proposed = 'friend'
        if (days >= policy['trusted_days'] and min(familiarity, comfort) >= policy['trusted_score']
                and min(trust, closeness) >= policy['trusted_deep']):
            proposed = 'trusted_friend'
        stages = ('unknown', 'acquaintance', 'familiar', 'friend', 'trusted_friend')
        return max((stage, floor, proposed), key=stages.index)
    if stage not in {'unknown', 'acquaintance', 'familiar'}:
        return stage
    everyday = [level(snapshot, name) for name in _EVERYDAY]
    deep = [level(snapshot, name) for name in _DEEP]
    tension = level(snapshot, 'tension')
    # A score of 0 maps to "unknown": no tension at all is calm, not missing data.
    calm = tension in {'unknown', 'low'}
    def days(window):
        value = history.get(window, 0)
        return value if type(value) is int and 0 <= value <= window else 0
    if (all(value == 'high' for value in everyday) and all(value in {'medium', 'high'} for value in deep)
            and calm and days(21) >= 8):
        return 'trusted_friend'
    if (all(value in {'medium', 'high'} for value in everyday) and all(value != 'unknown' for value in deep)
            and tension != 'high' and days(14) >= 5):
        return 'friend'
    if stage == 'unknown':
        # Display/tone only: an unconfirmed stage still reflects how well they know each other.
        return {'high': 'familiar', 'medium': 'familiar', 'low': 'acquaintance'}.get(everyday[0], stage)
    return stage


def relationship_history(store, now=None):
    import time
    from datetime import datetime, timedelta
    from runtime.personal_chat.engagement import BEIJING, Engagement
    now = time.time() if now is None else now
    events = Engagement(window=21 * 86400)._read_events(
        getattr(store, 'letters', []), getattr(store, 'personal_chats', []), now)
    today = datetime.fromtimestamp(now, BEIJING).date()
    stamps = list(events.values())
    # A real conversation on the phone counts as a day they talked, like letters and QQ.
    for call in getattr(store, 'calls', None) or []:
        if not isinstance(call, dict) or call.get('missed') or (call.get('seconds') or 0) < 30:
            continue
        try:
            at = datetime.fromisoformat(call['started_at']).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if now - 21 * 86400 < at <= now:
            stamps.append(at)
    dates = {datetime.fromtimestamp(at, BEIJING).date() for at in stamps}
    return {days: sum(today - timedelta(days=days - 1) <= day <= today for day in dates)
            for days in (14, 21)}
