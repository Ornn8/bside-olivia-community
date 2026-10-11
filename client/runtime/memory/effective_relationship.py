"""Read-only friendship projection; never grants identity or action permissions."""


def _value(snapshot, name, default='unknown'):
    value = snapshot.get(name, default) if isinstance(snapshot, dict) else getattr(snapshot, name, default)
    return getattr(value, 'value', value)


def level(snapshot, name):
    value = _value(snapshot, name)
    if type(value) is int and 0 <= value <= 100:
        return 'unknown' if value == 0 else 'low' if value < 35 else 'medium' if value < 70 else 'high'
    return value if isinstance(value, str) and value in {'unknown', 'low', 'medium', 'high'} else 'unknown'


def effective_relationship_stage(snapshot, history):
    stage = _value(snapshot, 'relationship_stage')
    if stage not in {'unknown', 'acquaintance', 'familiar'}:
        return stage
    levels = [level(snapshot, name) for name in ('familiarity', 'trust', 'comfort', 'closeness')]
    tension = level(snapshot, 'tension')
    def days(window):
        value = history.get(window, 0)
        return value if type(value) is int and 0 <= value <= window else 0
    if all(value == 'high' for value in levels) and tension == 'low' and days(21) >= 8:
        return 'trusted_friend'
    if all(value in {'medium', 'high'} for value in levels) and tension != 'high' and days(14) >= 5:
        return 'friend'
    return stage


def relationship_history(store, now=None):
    import time
    from datetime import datetime, timedelta
    from runtime.personal_chat.engagement import BEIJING, Engagement
    now = time.time() if now is None else now
    events = Engagement(window=21 * 86400)._read_events(
        getattr(store, 'letters', []), getattr(store, 'personal_chats', []), now)
    today = datetime.fromtimestamp(now, BEIJING).date()
    dates = {datetime.fromtimestamp(at, BEIJING).date() for at in events.values()}
    return {days: sum(today - timedelta(days=days - 1) <= day <= today for day in dates)
            for days in (14, 21)}
