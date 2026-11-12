"""One cached read of the cloud media service's capabilities for the reply path.

Long speech and piano used to read /capabilities separately, each with 3 s, and a failed read counted as "not
available" for 60 s. Users far from the server failed often at busy hours: on 2026-10-20, 64% of QQ decisions went
out with neither, so ASMR and piano could not be chosen even after she offered them. Now a good answer is kept: it is
refreshed in the background once a minute and stays in use for up to STALE_SECONDS while refreshes fail. Only the very
first read waits.

The last good answer is also kept in a small file beside the other local data (when the data folder is known), so a
restarted client starts from it instead of from nothing: a slow first read after a restart used to switch covers,
ASMR and piano off for that reply (2026-11-12, 小离 told a user the cover service could not be reached).
"""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

FRESH_SECONDS = 60
STALE_SECONDS = 6 * 3600
TIMEOUT_SECONDS = 3
RETRY_TIMEOUT_SECONDS = 20
KEPT_FILE = 'media-capabilities.json'
KEPT_BYTES = 64 * 1024
_state = {}


def _digest(url, token):
    return hashlib.sha256((url + '\0' + token).encode()).hexdigest()


def _kept_path(environment):
    root = str(environment.get('OLIVIA_LOCAL_DATA_ROOT') or os.environ.get('OLIVIA_LOCAL_DATA_ROOT') or '').strip()
    return Path(root) / KEPT_FILE if root else None


def _load(path, digest):
    """(answer, seconds since it was read) from the kept file, or None: no file, another account or endpoint, too old,
    or not readable. Never raises."""
    try:
        if path is None or not path.exists() or path.stat().st_size > KEPT_BYTES:
            return None
        saved = json.loads(path.read_text(encoding='utf-8'))
        age = time.time() - float(saved['saved_at'])
        if saved.get('key') != digest or not isinstance(saved.get('value'), dict) or not 0 <= age < STALE_SECONDS:
            return None
        return saved['value'], age
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save(path, digest, value):
    """Keep this good answer; a file that cannot be written is simply not kept."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + '.part')
        partial.write_text(json.dumps({'key': digest, 'saved_at': time.time(), 'value': value}), encoding='utf-8')
        os.replace(partial, path)
    except (OSError, ValueError, TypeError):
        pass


async def _fetch(url, token, timeout=TIMEOUT_SECONDS):
    from runtime.remote_generation import RemoteGeneration
    try:
        value = await asyncio.wait_for(RemoteGeneration(url, token).request('capabilities', {}), timeout)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


async def _refresh(key, url, token, timeout=TIMEOUT_SECONDS, path=None):
    value = await _fetch(url, token, timeout)
    entry = _state.get(key)
    if value is not None:
        _state[key] = {'value': value, 'at': time.monotonic(), 'task': None}
        _save(path, _digest(url, token), value)
    elif entry is not None:
        entry.update(task=None, failed_at=time.monotonic())
    return value


async def current(environment, retry=False):
    """The service's capabilities, or None when none has been read in the last STALE_SECONDS.

    ``retry`` reads again at once with a longer wait even if the last read just failed (a caller whose feature would
    otherwise be switched off silently by one slow answer)."""
    url, token = environment.get('OLIVIA_GPU_API_URL', ''), environment.get('OLIVIA_GPU_API_KEY', '')
    if not url or not token:
        return None
    key = (url, hashlib.sha256(token.encode()).hexdigest())
    for other in [k for k in _state if k != key]:
        _state.pop(other, None)  # another account or endpoint: never reuse its answer
    path = _kept_path(environment)
    entry = _state.get(key)
    if entry is None or entry.get('value') is None:
        kept = _load(path, _digest(url, token))
        if kept is not None:  # a restarted client: the last good answer, older by the time it has been off
            entry = _state[key] = {'value': kept[0], 'at': time.monotonic() - kept[1], 'task': entry.get('task') if entry else None,
                                   **({'failed_at': entry['failed_at']} if entry and 'failed_at' in entry else {})}
    age = time.monotonic() - entry['at'] if entry and entry.get('value') is not None else None
    if age is not None and age < FRESH_SECONDS:
        return entry['value']
    if age is not None and age < STALE_SECONDS:
        if not entry.get('task'):
            entry['task'] = asyncio.get_running_loop().create_task(_refresh(key, url, token, path=path))
        return entry['value']
    task = entry.get('task') if entry else None
    if not retry and task is None and entry and time.monotonic() - entry.get('failed_at', -FRESH_SECONDS) < FRESH_SECONDS:
        return None  # nothing good yet and the last read failed: do not hold every reply for another 3 s
    if task is None:
        task = asyncio.get_running_loop().create_task(
            _refresh(key, url, token, RETRY_TIMEOUT_SECONDS if retry else TIMEOUT_SECONDS, path))
        _state[key] = {'value': entry.get('value') if entry else None, 'at': entry.get('at', 0.0) if entry else 0.0, 'task': task}
    try:
        return await asyncio.shield(task)
    except Exception:
        return None
