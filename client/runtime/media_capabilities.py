"""One cached read of the cloud media service's capabilities for the reply path.

Long speech and piano used to read /capabilities separately, each with 3 s, and a failed read counted as "not
available" for 60 s. Users far from the server failed often at busy hours: on 2026-10-20, 64% of QQ decisions went
out with neither, so ASMR and piano could not be chosen even after she offered them. Now a good answer is kept: it is
refreshed in the background once a minute and stays in use for up to 30 minutes while refreshes fail. Only the very
first read waits.
"""
import asyncio
import hashlib
import time

FRESH_SECONDS = 60
STALE_SECONDS = 1800
TIMEOUT_SECONDS = 3
_state = {}


async def _fetch(url, token):
    from runtime.remote_generation import RemoteGeneration
    try:
        value = await asyncio.wait_for(RemoteGeneration(url, token).request('capabilities', {}), TIMEOUT_SECONDS)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


async def _refresh(key, url, token):
    value = await _fetch(url, token)
    entry = _state.get(key)
    if value is not None:
        _state[key] = {'value': value, 'at': time.monotonic(), 'task': None}
    elif entry is not None:
        entry.update(task=None, failed_at=time.monotonic())
    return value


async def current(environment):
    """The service's capabilities, or None when none has been read in the last STALE_SECONDS."""
    url, token = environment.get('OLIVIA_GPU_API_URL', ''), environment.get('OLIVIA_GPU_API_KEY', '')
    if not url or not token:
        return None
    key = (url, hashlib.sha256(token.encode()).hexdigest())
    for other in [k for k in _state if k != key]:
        _state.pop(other, None)  # another account or endpoint: never reuse its answer
    entry = _state.get(key)
    age = time.monotonic() - entry['at'] if entry and entry.get('value') is not None else None
    if age is not None and age < FRESH_SECONDS:
        return entry['value']
    if age is not None and age < STALE_SECONDS:
        if not entry.get('task'):
            entry['task'] = asyncio.get_running_loop().create_task(_refresh(key, url, token))
        return entry['value']
    task = entry.get('task') if entry else None
    if task is None and entry and time.monotonic() - entry.get('failed_at', -FRESH_SECONDS) < FRESH_SECONDS:
        return None  # nothing good yet and the last read failed: do not hold every reply for another 3 s
    if task is None:
        task = asyncio.get_running_loop().create_task(_refresh(key, url, token))
        _state[key] = {'value': None, 'at': 0.0, 'task': task}
    try:
        return await asyncio.shield(task)
    except Exception:
        return None
