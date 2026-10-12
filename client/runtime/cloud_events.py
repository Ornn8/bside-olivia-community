"""Cloud-reported visits: only the local time of each one is kept, for her diary.

The cloud tells the client when the owner came by. Nothing is sent in answer and
nothing but the moment is stored: no amount, no wording, no reason. The diary
writer is given a day's visits as times only.
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
    """Merge fetched events; the file keeps ids and moments only."""
    known = _known(read_json(_path(root)))
    for item in events:
        known.setdefault(item['id'], item['at'])
    known = {key: value for key, value in known.items() if value > now - KEEP_SECONDS}
    write_json(_path(root), {'visits': known})
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
