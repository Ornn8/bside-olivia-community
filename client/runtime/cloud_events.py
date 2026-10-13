"""What the cloud tells the client, polled from one events service.

Visits: only the local time of each one is kept, for her diary. Nothing is sent in
answer and nothing but the moment is stored: no amount, no wording, no reason. The
diary writer is given a day's visits as times only.

Contacts: requests for her to reach out, kept in the same file and carried out by
runtime.cloud_contact. Kinds this client does not know are ignored.
"""
import os
import re
import time
from datetime import datetime
from pathlib import Path

from runtime.reply.proactive_letters import read_json, write_json

POLL_SECONDS = 120
KEEP_SECONDS = 400 * 86400
_ID = re.compile(r'gift:[0-9a-f-]{36}')


def _path(root):
    return Path(root) / 'proactive' / 'cloud-events.json'


def validate(payload):
    events = payload.get('events') if isinstance(payload, dict) else None
    if not isinstance(events, list) or len(events) > 20:
        raise ValueError('CLOUD_EVENTS_INVALID')
    # Kinds this client does not know (from a newer cloud) are ignored. Of a known one
    # only the id (to count it once) and the moment are read; every other field is dropped here.
    return [{'id': item['id'], 'at': item['at']} for item in events
            if isinstance(item, dict) and item.get('kind') == 'gift' and isinstance(item.get('id'), str)
            and _ID.fullmatch(item['id']) and type(item.get('at')) is int and item['at'] > 0]


def _known(state):
    """Visit moments by id, from the current file or the one an earlier version left."""
    visits = state.get('visits')
    older = state.get('events')
    found = {key: value for key, value in (visits.items() if isinstance(visits, dict) else ())
             if type(value) is int}
    for key, value in (older.items() if isinstance(older, dict) else ()):
        if isinstance(value, dict) and type(value.get('at')) is int:
            found.setdefault(key, value['at'])
    return found


def record(root, events, now):
    """Merge fetched events; the visits part keeps ids and moments only."""
    state = read_json(_path(root))
    known = _known(state)
    for item in events:
        known.setdefault(item['id'], item['at'])
    known = {key: value for key, value in known.items() if value > now - KEEP_SECONDS}
    write_json(_path(root), {'visits': known, **({'contacts': state['contacts']}
                                               if isinstance(state.get('contacts'), dict) else {})})
    return known


def visits(root, period):
    """Local times of the visits in one day ('YYYY-MM-DD') or month ('YYYY-MM'), oldest first.

    A day gives 'HH:MM', a month 'MM-DD HH:MM'. Times and nothing else.
    """
    if root is None:
        return []
    from runtime.diary.diary import SHANGHAI
    moments = sorted(datetime.fromtimestamp(at, SHANGHAI) for at in _known(read_json(_path(root))).values())
    if len(period) == 10:
        return [moment.strftime('%H:%M') for moment in moments if moment.strftime('%Y-%m-%d') == period]
    return [moment.strftime('%m-%d %H:%M') for moment in moments if moment.strftime('%Y-%m') == period]


async def request(method, path, payload=None):
    """One call to the cloud with the Olivia account; None when no such account is set up."""
    key = os.environ.get('OLIVIA_GPU_API_KEY', '')
    if not key.startswith('olivia-'):
        return None
    from original_client_relay_api import RELAY_BASE, relay_request
    from runtime.official_endpoints import canonical_api_origin
    if canonical_api_origin(os.environ.get('OLIVIA_GPU_API_URL', '').rstrip('/')) + '/v1' != RELAY_BASE:
        return None
    return await relay_request(RELAY_BASE, key, method, path, payload)


async def poll(root):
    """One check; silent when the Olivia account is not configured or the cloud is unreachable."""
    if root is None:
        return
    try:
        payload = await request('GET', '/events')
    except Exception:
        return
    if payload is None:
        return
    now = time.time()
    record(root, validate(payload), now)
    from runtime import cloud_contact
    cloud_contact.record(root, cloud_contact.parse(payload), now)
