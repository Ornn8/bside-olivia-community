"""Cloud-owned moments she answers at once; a credited top-up is a 'gift'.

The cloud decides what counts and how she takes it (``brief``). This module only
polls, validates and remembers which events were answered: QQ first, a letter
when QQ cannot take it. Both are billed like any other contact.
"""
import os
import re
import time
from pathlib import Path

from runtime.reply.proactive_letters import read_json, write_json

POLL_SECONDS = 120
WINDOW = 48 * 3600
FIRST_RUN_GRACE = 2 * 3600   # after an upgrade only the last two hours' top-ups are answered
QQ_HEAD_START = 180          # a letter answers only what QQ did not take by then
_ID = re.compile(r'gift:[0-9a-f-]{36}')


def instruction(gift, *, photo):
    """The cloud's brief plus what this client will actually send with it."""
    return gift['brief'] + ('回应之后会附上一张她此刻的照片，可以自然地提一句。' if photo else '这次只用文字回应。')


def _path(root):
    return Path(root) / 'proactive' / 'cloud-events.json'


def validate(payload):
    events = payload.get('events') if isinstance(payload, dict) else None
    if not isinstance(events, list) or len(events) > 20:
        raise ValueError('CLOUD_EVENTS_INVALID')
    result = []
    for item in events:
        # Kinds this client does not know (from a newer cloud) are ignored.
        if (isinstance(item, dict) and item.get('kind') == 'gift' and isinstance(item.get('id'), str)
                and _ID.fullmatch(item['id']) and type(item.get('at')) is int
                and isinstance(item.get('brief'), str) and 1 <= len(item['brief']) <= 600
                and type(item.get('photo')) is bool):
            result.append({key: item[key] for key in ('id', 'kind', 'at', 'brief', 'photo')})
    return result


def record(root, events, now):
    """Merge fetched events. The first run treats older top-ups as already answered."""
    state = read_json(_path(root))
    first = not isinstance(state.get('events'), dict)
    known = {} if first else state['events']
    for item in events:
        if item['id'] not in known:
            stale = first and item['at'] < now - FIRST_RUN_GRACE
            known[item['id']] = {**item, 'seen_at': now, 'status': 'expired' if stale else 'pending'}
    known = {key: value for key, value in known.items()
             if isinstance(value, dict) and value.get('at', 0) > now - 7 * 86400}
    write_json(_path(root), {'events': known})
    return known


def pending(root, now, *, settled=0):
    """Unanswered gifts, oldest first; ``settled`` skips ones seen here less than that many seconds ago."""
    if root is None:
        return []
    events = read_json(_path(root)).get('events')
    if not isinstance(events, dict):
        return []
    return sorted((value for value in events.values() if isinstance(value, dict)
                   and value.get('status') == 'pending' and type(value.get('at')) is int
                   and value['at'] > now - WINDOW and value.get('seen_at', now) <= now - settled),
                  key=lambda value: value['at'])


def mark(root, event_id, status):
    state = read_json(_path(root))
    events = state.get('events')
    if isinstance(events, dict) and isinstance(events.get(event_id), dict):
        events[event_id]['status'] = status
        write_json(_path(root), {'events': events})


async def poll(root):
    """One check; silent when the Olivia account is not configured or the cloud is unreachable."""
    key = os.environ.get('OLIVIA_GPU_API_KEY', '')
    if root is None or not key.startswith('olivia-'):
        return
    from original_client_relay_api import RELAY_BASE, relay_request
    from runtime.official_endpoints import canonical_api_origin
    if canonical_api_origin(os.environ.get('OLIVIA_GPU_API_URL', '').rstrip('/')) + '/v1' != RELAY_BASE:
        return
    try:
        payload = await relay_request(RELAY_BASE, key, 'GET', '/events')
    except Exception:
        return
    record(root, validate(payload), time.time())
