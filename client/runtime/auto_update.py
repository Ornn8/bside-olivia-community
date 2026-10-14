"""Automatic updates (owner 2026-10-14: 可以做个自动更新; 点击不是直接更新，先显示更新内容).

In the background: read the signed release manifest, download the patch, check it and install it with
the same installer as a hand-picked patch. Nothing restarts on its own while the user is around:
the page (runtime/update_ui.py) shows "新版本已准备好"; a click shows what changed first, then
"现在重启更新" restarts. Otherwise the update takes effect at the next start, or at night when the
app is idle (no call, no reply in progress, no message for a while). After an update the page shows
what changed once.

Trust: https://bside-moon.cn/update/latest.json and latest.json.sig, an Ed25519 signature over the exact
bytes with PUBLIC_KEY below (the private key stays on the release machine). The patch zip must match
the signed size and sha256, and its manifest digest the signed manifest_sha256, before the installer
(which checks every file against that manifest) applies it.

The first start of an installed update is on probation (.olivia-update-probation.json in the install
root): if it fails, the launcher goes back to the previous version and marks the version rejected so it
is not installed again; once it has run PROBATION_SECONDS the mark is removed.

Local endpoints:
    GET  /toy/autoupdate/state     {state: idle|ready|updated, current, version, notes}
    POST /toy/autoupdate/restart   (confirmed) restart into the installed update now
    POST /toy/autoupdate/seen      (confirmed) the notes after an update were shown
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import zipfile
from pathlib import Path

PUBLIC_KEY = bytes.fromhex('9dd7cb44967b4ba21df761029b76ee50104855e702d3de36b8daafa7617839bb')
BASE = 'https://bside-moon.cn/'
FIRST_CHECK_SECONDS = 120
CHECK_EVERY_SECONDS = 6 * 3600
TICK_SECONDS = 600
NIGHT_HOURS = {2, 3, 4, 5}         # local time when an idle app may restart on its own
QUIET_SECONDS = 20 * 60            # no chat activity for this long counts as idle
CALL_SECONDS = 7 * 60              # the call page re-reads her world every 5 minutes while a call runs
PROBATION_SECONDS = 90
MAX_ZIP_BYTES = 256 * 1024 * 1024
BUSY = {'RECEIVED', 'PENDING', 'SENDING', 'MEDIA_PENDING'}
PROBATION_NAME = '.olivia-update-probation.json'
_VERSION = re.compile(r'\d+(?:\.\d+){1,3}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
log = logging.getLogger('olivia.auto_update')

_state = {'state': 'idle', 'version': None, 'notes': None, 'checked_at': 0.0, 'failures': {}, 'restarting': False}


def version_key(text):
    """(2, 3, 9) for a release; None for anything else (dev builds such as 2.3.4+call.10 never auto-update)."""
    return tuple(int(part) for part in text.split('.')) if isinstance(text, str) and _VERSION.fullmatch(text) else None


def running_version():
    from original_client_update_api import running_component_version
    return running_component_version().get('version')


def parse_manifest(raw, signature):
    """The verified manifest, or ValueError."""
    from runtime import ed25519
    try:
        signed = base64.b64decode(signature.strip(), validate=True)
    except ValueError:
        raise ValueError('UPDATE_SIGNATURE_INVALID') from None
    if not ed25519.verify(PUBLIC_KEY, raw, signed):
        raise ValueError('UPDATE_SIGNATURE_INVALID')
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('schema') != 1 or version_key(value.get('version')) is None
            or value.get('zip') != f"Olivia-{value['version']}.zip"
            or type(value.get('bytes')) is not int or not 0 < value['bytes'] <= MAX_ZIP_BYTES
            or not _SHA256.fullmatch(str(value.get('sha256'))) or not _SHA256.fullmatch(str(value.get('manifest_sha256')))):
        raise ValueError('UPDATE_MANIFEST_INVALID')
    value['notes'] = clean_notes(value.get('notes'))
    return value


def clean_notes(notes):
    """{title, items: [{title, text}]} with bounded plain text; anything else becomes empty."""
    if not isinstance(notes, dict):
        return {'title': '', 'items': []}
    text = lambda v, n: v.strip()[:n] if isinstance(v, str) else ''
    items = [{'title': text(item.get('title'), 60), 'text': text(item.get('text'), 400)}
             for item in (notes.get('items') or [])[:12] if isinstance(item, dict)]
    return {'title': text(notes.get('title'), 120), 'items': [i for i in items if i['title'] or i['text']]}


def _root():
    raw = os.environ.get('OLIVIA_INSTALL_ROOT', '').strip()
    root = Path(raw) if raw else None
    return root.resolve() if root and root.is_absolute() and (root / '.olivia-update-state.json').is_file() else None


def _base():
    # Tests point at a local server; the manifest must still carry the release signature.
    value = os.environ.get('OLIVIA_UPDATE_BASE', BASE)
    return value if re.fullmatch(r'https://[a-z0-9.-]+/|http://127\.0\.0\.1:\d{1,5}/', value) else BASE


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


async def _fetch(session, url, limit):
    async with session.get(url, allow_redirects=False) as response:
        if response.status != 200:
            raise RuntimeError('UPDATE_FETCH_FAILED')
        body = await response.content.read(limit + 1)
        if len(body) > limit:
            raise RuntimeError('UPDATE_FETCH_TOO_LARGE')
        return body


async def _download(session, url, target, size, sha256):
    partial = target.with_name(target.name + '.part')
    digest, count = hashlib.sha256(), 0
    async with session.get(url, allow_redirects=False) as response:
        if response.status != 200:
            raise RuntimeError('UPDATE_FETCH_FAILED')
        with partial.open('wb') as output:
            async for chunk in response.content.iter_chunked(1 << 20):
                count += len(chunk)
                if count > size:
                    raise RuntimeError('UPDATE_DOWNLOAD_INVALID')
                digest.update(chunk)
                output.write(chunk)
    if count != size or digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError('UPDATE_DOWNLOAD_INVALID')
    partial.replace(target)


def bundle_manifest_digest(bundle):
    with zipfile.ZipFile(bundle) as archive:
        names = [n for n in archive.namelist() if n.endswith('.oliviapatch.manifest.sha256')]
        if len(names) != 1:
            raise RuntimeError('UPDATE_BUNDLE_INVALID')
        return archive.read(names[0])[:256].decode('ascii', errors='replace').strip()


def _install(root, bundle, manifest):
    from original_client_update_api import LocalComponentUpdater, _apply_update_bundle
    if bundle_manifest_digest(bundle) != manifest['manifest_sha256']:
        raise RuntimeError('UPDATE_BUNDLE_INVALID')
    result = _apply_update_bundle(LocalComponentUpdater(root), bundle)
    if result.get('status') != 'APPLIED' or result.get('version') != manifest['version']:
        raise RuntimeError('UPDATE_APPLY_FAILED')
    _write_json(root / PROBATION_NAME, {'version': manifest['version'], 'applied_at': time.time()})


async def check(root, *, session_factory=None):
    """One check: install a newer signed release in the background. Returns the state."""
    current = running_version()
    if version_key(current) is None:
        return _state
    from aiohttp import ClientSession, ClientTimeout, TCPConnector
    from runtime.tls import client_tls_context
    base = _base()
    factory = session_factory or (lambda: ClientSession(timeout=ClientTimeout(total=600, sock_read=60), trust_env=False,
                                                        connector=TCPConnector(ssl=client_tls_context())))
    async with factory() as session:
        raw = await _fetch(session, base + 'update/latest.json', 64 * 1024)
        signature = (await _fetch(session, base + 'update/latest.json.sig', 1024)).decode('ascii', errors='replace')
        manifest = parse_manifest(raw, signature)
        version = manifest['version']
        if version_key(version) <= version_key(current) or _state['state'] == 'ready' and _state['version'] == version:
            return _state
        mark = _read_json(root / PROBATION_NAME) or {}
        if mark.get('version') == version and mark.get('rejected'):
            return _state  # this version failed to start here once; wait for the next one
        if _state['failures'].get(version, 0) >= 3:
            return _state
        updates = root / 'data' / 'updates'
        updates.mkdir(parents=True, exist_ok=True)
        bundle = updates / manifest['zip']
        try:
            await _download(session, base + manifest['zip'], bundle, manifest['bytes'], manifest['sha256'])
            await asyncio.to_thread(_install, root, bundle, manifest)
        except Exception:
            _state['failures'][version] = _state['failures'].get(version, 0) + 1
            raise
        finally:
            bundle.unlink(missing_ok=True)
    _write_json(updates / 'notes.json', {'version': version, 'notes': manifest['notes'], 'seen': False})
    _state.update(state='ready', version=version, notes=manifest['notes'])
    log.info('auto update %s installed; restart pending', version)
    return _state


def idle(server, now):
    """No call, nothing being answered and no chat for a while."""
    if now - getattr(server, '_call_seen_at', 0) < CALL_SECONDS:
        return False
    rows = getattr(getattr(server, 'store', None), 'personal_chats', None) or []
    for row in rows[-300:]:
        try:
            created = float(row.get('created_at') or 0)
        except (TypeError, ValueError):
            continue
        if now - created < QUIET_SECONDS or (row.get('delivery_status') in BUSY and now - created < 2 * 3600):
            return False
    return True


async def restart(root):
    if _state['restarting']:
        return
    _state['restarting'] = True
    from original_client_update_api import LocalComponentUpdater, _exit_for_restart
    try:
        await asyncio.to_thread(LocalComponentUpdater(root).restart)
    except Exception:
        _state['restarting'] = False
        raise
    asyncio.get_running_loop().call_later(1, _exit_for_restart)


def _settle_probation(root, current):
    """The installed update started here: show its notes once, and lift the probation after a while."""
    notes = _read_json(root / 'data' / 'updates' / 'notes.json')
    if notes and notes.get('version') == current and not notes.get('seen'):
        _state.update(state='updated', version=current, notes=clean_notes(notes.get('notes')))
    mark = _read_json(root / PROBATION_NAME)
    if mark and mark.get('version') == current and not mark.get('rejected'):
        async def lift():
            await asyncio.sleep(PROBATION_SECONDS)
            if (_read_json(root / PROBATION_NAME) or {}).get('version') == current:
                (root / PROBATION_NAME).unlink(missing_ok=True)
        return lift()
    return None


async def _run(server, root):
    lift = _settle_probation(root, running_version())
    if lift is not None:
        asyncio.ensure_future(lift)
    await asyncio.sleep(FIRST_CHECK_SECONDS)
    while True:
        now = time.time()
        if now - _state['checked_at'] >= CHECK_EVERY_SECONDS and _state['state'] != 'ready':
            _state['checked_at'] = now
            try:
                await check(root)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('auto update check failed: %s', type(exc).__name__ if not str(exc).startswith('UPDATE_') else exc)
        if _state['state'] == 'ready' and time.localtime(now).tm_hour in NIGHT_HOURS and idle(server, now):
            log.info('auto update: idle at night, restarting into %s', _state['version'])
            try:
                await restart(root)
            except Exception:
                log.warning('auto update restart failed')
        await asyncio.sleep(TICK_SECONDS)


_task = None


def start(server, app):
    """Background task for an installed client (never in a source checkout); it ends with the loop."""
    global _task
    root = _root()
    if root is None or _task is not None and not _task.done():
        return
    _task = asyncio.get_running_loop().create_task(_run(server, root))


async def autoupdate_http(server, request):
    from aiohttp import web
    headers = server.CORS_HEADERS(request)
    path, method = request.path.rstrip('/'), request.method

    def reply(data, status=200):
        return web.json_response(server.ok(data) if status == 200 else server.err(status, data, {'error_code': data}),
                                 status=status, headers=headers)
    root = _root()
    if path == '/toy/autoupdate/state' and method == 'GET':
        return reply({'state': _state['state'], 'version': _state['version'], 'notes': _state['notes'],
                      'current': running_version(), 'enabled': root is not None})
    if method == 'POST' and request.headers.get('X-Olivia-Companion-Action') != 'confirmed':
        return reply('COMPANION_CONFIRMATION_REQUIRED', 403)
    if path == '/toy/autoupdate/restart' and method == 'POST':
        if root is None or _state['state'] != 'ready':
            return reply('UPDATE_RESTART_NOT_PENDING', 409)
        try:
            await restart(root)
        except Exception:
            return reply('UPDATE_RESTART_UNAVAILABLE', 503)
        return reply({'restarting': True})
    if path == '/toy/autoupdate/seen' and method == 'POST':
        if root is not None and _state['state'] == 'updated':
            notes = _read_json(root / 'data' / 'updates' / 'notes.json') or {}
            _write_json(root / 'data' / 'updates' / 'notes.json', {**notes, 'seen': True})
            _state.update(state='idle', version=None, notes=None)
        return reply({'seen': True})
    return reply('NOT_FOUND', 404)
