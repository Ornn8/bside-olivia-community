"""Local endpoints of the call page. The Olivia key never leaves this backend.

    GET  /toy/call/config   whether calls are open and the current per-minute price
    POST /toy/call/start    (X-Olivia-Companion-Action: confirmed) → missed, or a one-time cloud ticket
    GET  /toy/call/world    her world now, for the page to forward when it changes during a call
    POST /toy/call/end      the call's end report → kept, remembered, visible to QQ replies
    POST /toy/call/recover  {call_id}: fetch the end report from the cloud when the page never got it
    POST /toy/call/phone/pair     (confirmed) → a one-time link for the phone call page (QR code)
    GET  /toy/call/phones         the paired phones; opens the phone line (runtime/call/desk.py) once one is
    POST /toy/call/phone/unpair   (confirmed) {id}: unpair one phone ("all": every phone)
    GET  /toy/call/avatar         whether this account owns her call avatar (runtime/call_avatar_ui.py)
    GET  /toy/call/avatar/files/<name>   the avatar's rig, atlas and effects, fetched with the key
    POST /toy/call/avatar/buy     {"max_cents": n}: buy it once with this account's key (confirmed action)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from runtime.cloud_service import endpoint
from runtime.tls import client_tls_context

# A call holds money in blocks while it runs; this is only the per-call ceiling the user consents to.
MAX_CHARGE_CENTS = 6000
# Some users' networks to the call service are slow at times (2026-10-14: the call button vanished
# and pairing failed after 6 s); these reads may take longer, and pairing tries twice.
CONFIG_TIMEOUT = ClientTimeout(total=12)
PAIR_TIMEOUT = ClientTimeout(total=15)
TICKET_TIMEOUT = ClientTimeout(total=12)
AVATAR_TIMEOUT = ClientTimeout(total=60)
AVATAR_FILE = re.compile(r'rig\.json|atlas\.(?:webp|png)|fx_[a-z_]{1,20}\.png')
AVATAR_TYPES = {'json': 'application/json', 'webp': 'image/webp', 'png': 'image/png'}
AVATAR_MAX_BYTES = 16 * 1024 * 1024
# The avatar files of the current version, kept in memory: each call page opening reads them again.
_avatar = {'version': None, 'files': {}}


def _cloud():
    base, key = os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', '')
    return (endpoint(base), key) if base and key else (None, None)


def _ws_url(base):
    parts = urlsplit(base)
    return ('wss' if parts.scheme == 'https' else 'ws') + '://' + parts.netloc + '/v1/call'


async def _cloud_json(method, path, *, timeout, headers=None, body=None):
    base, _ = _cloud()
    async with ClientSession(timeout=timeout, trust_env=False, connector=TCPConnector(ssl=client_tls_context())) as session:
        async with session.request(method, base + path, headers=headers or {}, json=body) as response:
            if response.status != 200:
                return response.status, None
            return 200, await response.json(content_type=None)


async def config(server):
    base, _ = _cloud()
    if base is None:
        return {'available': False, 'reason': 'account'}
    try:
        status, value = await _cloud_json('GET', '/v1/call/config', timeout=CONFIG_TIMEOUT)
    except (ClientError, asyncio.TimeoutError, ValueError):
        return {'available': False, 'reason': 'network'}
    if status != 200 or not isinstance(value, dict):
        return {'available': False, 'reason': 'service'}
    price = value.get('cents_per_minute')
    models = [{key: m.get(key) for key in ('id', 'cents_per_minute', 'base_cents_per_minute', 'busy_cents_per_minute')}
              for m in value.get('models') or [] if isinstance(m, dict) and isinstance(m.get('id'), str)
              and type(m.get('cents_per_minute')) is int]
    return {'available': bool(value.get('available')) and type(price) is int, 'phone': value.get('phone') is True,
            'cents_per_minute': price if type(price) is int else None,
            'base_cents_per_minute': value.get('base_cents_per_minute'),
            'busy_cents_per_minute': value.get('busy_cents_per_minute'),
            'models': models, 'default_model': value.get('default_model')}


async def start(server, request):
    from runtime.call.context import build
    from runtime.call.store import record_missed
    base, key = _cloud()
    if base is None:
        raise web.HTTPServiceUnavailable(reason='CALL_ACCOUNT_REQUIRED')
    try:
        from original_client_companion_api import _backend
        backend = _backend(request)
    except Exception:
        backend = None
    try:
        body = json.loads(await request.text() or '{}')
    except ValueError:
        body = {}
    model = body.get('model') if isinstance(body, dict) else None
    if model is not None and (not isinstance(model, str) or not model.isalnum() or len(model) > 20):
        raise ValueError('CALL_REQUEST_INVALID')
    context, pickup = await asyncio.to_thread(build, server, backend)
    if pickup == 'missed':
        await asyncio.to_thread(record_missed, server, 'bathing')
        return {'pickup': 'missed'}
    from runtime.call.store import mark_active
    mark_active(server)
    headers = {'Authorization': 'Bearer ' + key, 'X-Olivia-Max-Charge-Cents': str(MAX_CHARGE_CENTS)}
    try:
        status, value = await _cloud_json('POST', '/v1/call/ticket', timeout=TICKET_TIMEOUT, headers=headers,
                                          body={'v': 1, 'context': context, 'pickup': pickup,
                                                **({'model': model} if model else {})})
    except (ClientError, asyncio.TimeoutError, ValueError):
        raise web.HTTPServiceUnavailable(reason='CALL_NETWORK')
    if status != 200 or not isinstance(value, dict) or not isinstance(value.get('ticket'), str):
        raise web.HTTPServiceUnavailable(reason={401: 'CALL_AUTH_FAILED', 402: 'CALL_INSUFFICIENT_BALANCE',
                                                 503: 'CALL_BUSY', 400: 'CALL_MODEL_UNKNOWN'}.get(status, 'CALL_UNAVAILABLE'))
    return {'pickup': pickup, 'ticket': value['ticket'], 'ws_url': _ws_url(base),
            'cents_per_minute': value.get('cents_per_minute')}


async def recover(server, body):
    """The end report kept by the cloud for a call whose page lost its socket (or was closed)."""
    from runtime.call.store import record_call
    call_id = body.get('call_id') if isinstance(body, dict) else None
    if not isinstance(call_id, str) or not 8 <= len(call_id) <= 64 or not call_id.isalnum():
        raise ValueError('CALL_REQUEST_INVALID')
    base, key = _cloud()
    if base is None:
        raise web.HTTPServiceUnavailable(reason='CALL_ACCOUNT_REQUIRED')
    try:
        status, value = await _cloud_json('GET', '/v1/call/report?call_id=' + call_id, timeout=CONFIG_TIMEOUT,
                                          headers={'Authorization': 'Bearer ' + key})
    except (ClientError, asyncio.TimeoutError, ValueError):
        raise web.HTTPServiceUnavailable(reason='CALL_NETWORK')
    if status != 200 or not isinstance(value, dict) or value.get('call_id') != call_id:
        raise web.HTTPServiceUnavailable(reason='CALL_REPORT_PENDING')
    row = await asyncio.to_thread(record_call, server, value)
    from runtime.call.life import absorb_later
    absorb_later(server, row)
    return {'saved': True}


async def pair_phone(server):
    base, key = _cloud()
    if base is None:
        raise web.HTTPServiceUnavailable(reason='CALL_ACCOUNT_REQUIRED')
    for attempt in (1, 2):
        try:
            status, value = await _cloud_json('POST', '/v1/call/pair', timeout=PAIR_TIMEOUT,
                                              headers={'Authorization': 'Bearer ' + key})
            break
        except (ClientError, asyncio.TimeoutError, ValueError):
            if attempt == 2:
                raise web.HTTPServiceUnavailable(reason='CALL_NETWORK')
            await asyncio.sleep(1)
    if status != 200 or not isinstance(value, dict) or not str(value.get('url', '')).startswith('https://'):
        raise web.HTTPServiceUnavailable(reason='CALL_AUTH_FAILED' if status == 401 else 'CALL_UNAVAILABLE')
    return {'url': value['url'], 'expires_in': value.get('expires_in')}


async def phones(server, app):
    from runtime.call import desk
    base, key = _cloud()
    if base is None:
        return {'phones': [], 'line': False}
    try:
        status, value = await _cloud_json('GET', '/v1/call/phones', timeout=CONFIG_TIMEOUT,
                                          headers={'Authorization': 'Bearer ' + key})
    except (ClientError, asyncio.TimeoutError, ValueError):
        raise web.HTTPServiceUnavailable(reason='CALL_NETWORK')
    if status != 200 or not isinstance(value, dict):
        raise web.HTTPServiceUnavailable(reason='CALL_UNAVAILABLE')
    listed = [{k: p.get(k) for k in ('id', 'label', 'created', 'used')} for p in value.get('phones') or []
              if isinstance(p, dict) and isinstance(p.get('id'), str)]
    if listed and not desk.running():
        desk.start(server, app)
    return {'phones': listed, 'line': bool(listed) and desk.running()}


async def unpair_phone(server, body):
    phone_id = body.get('id') if isinstance(body, dict) else None
    if not isinstance(phone_id, str) or not (phone_id == 'all' or (len(phone_id) == 12 and phone_id.isalnum())):
        raise ValueError('CALL_REQUEST_INVALID')
    base, key = _cloud()
    if base is None:
        raise web.HTTPServiceUnavailable(reason='CALL_ACCOUNT_REQUIRED')
    try:
        status, _ = await _cloud_json('DELETE', '/v1/call/phones?id=' + phone_id, timeout=CONFIG_TIMEOUT,
                                      headers={'Authorization': 'Bearer ' + key})
    except (ClientError, asyncio.TimeoutError, ValueError):
        raise web.HTTPServiceUnavailable(reason='CALL_NETWORK')
    if status != 200:
        raise web.HTTPServiceUnavailable(reason='CALL_UNAVAILABLE')
    return {'removed': True}


async def avatar_state(server):
    base, key = _cloud()
    if base is None:
        return {'available': False, 'owned': False}
    try:
        status, value = await _cloud_json('GET', '/v1/call/avatar', timeout=CONFIG_TIMEOUT,
                                          headers={'Authorization': 'Bearer ' + key})
    except (ClientError, asyncio.TimeoutError, ValueError):
        return {'available': False, 'owned': False, 'reason': 'network'}
    if status != 200 or not isinstance(value, dict):
        return {'available': False, 'owned': False}
    state = {name: value.get(name) for name in ('available', 'owned', 'for_sale', 'cents', 'version')}
    if state['version'] != _avatar['version']:
        _avatar.update(version=state['version'], files={})
    return state


async def avatar_buy(body):
    """One purchase with this account's key; the cloud never charges an owned avatar twice."""
    base, key = _cloud()
    if base is None:
        return {'owned': False, 'error': 'account'}
    if not isinstance(body, dict) or type(body.get('max_cents')) is not int or not 0 < body['max_cents'] <= 100000:
        raise ValueError('CALL_AVATAR_BUY_INVALID')
    try:
        status, value = await _cloud_json('POST', '/v1/call/avatar', timeout=CONFIG_TIMEOUT,
                                          headers={'Authorization': 'Bearer ' + key},
                                          body={'action': 'buy', 'max_cents': body['max_cents']})
    except (ClientError, asyncio.TimeoutError, ValueError):
        return {'owned': False, 'error': 'network'}
    if status == 402:
        return {'owned': False, 'error': 'balance'}
    if status != 200 or not isinstance(value, dict):
        return {'owned': False, 'error': 'service'}
    state = {name: value.get(name) for name in ('available', 'owned', 'for_sale', 'cents', 'version', 'charged_cents')}
    if state['version'] != _avatar['version']:
        _avatar.update(version=state['version'], files={})
    return state


async def avatar_file(name, headers):
    if not AVATAR_FILE.fullmatch(name):
        raise web.HTTPNotFound(reason='CALL_AVATAR_FILE')
    base, key = _cloud()
    if base is None:
        raise web.HTTPServiceUnavailable(reason='CALL_ACCOUNT_REQUIRED')
    body = _avatar['files'].get(name)
    if body is None:
        try:
            async with ClientSession(timeout=AVATAR_TIMEOUT, trust_env=False,
                                     connector=TCPConnector(ssl=client_tls_context())) as session:
                async with session.get(base + '/v1/call/avatar/files/' + name,
                                       headers={'Authorization': 'Bearer ' + key}) as response:
                    if response.status == 403:
                        raise web.HTTPForbidden(reason='CALL_AVATAR_NOT_OWNED')
                    if response.status != 200:
                        raise web.HTTPServiceUnavailable(reason='CALL_AVATAR_UNAVAILABLE')
                    body = await response.content.read(AVATAR_MAX_BYTES + 1)
        except (ClientError, asyncio.TimeoutError):
            raise web.HTTPServiceUnavailable(reason='CALL_NETWORK')
        if len(body) > AVATAR_MAX_BYTES:
            raise web.HTTPServiceUnavailable(reason='CALL_AVATAR_UNAVAILABLE')
        _avatar['files'][name] = body
    return web.Response(body=body, content_type=AVATAR_TYPES[name.rsplit('.', 1)[1]],
                        headers={**headers, 'Cache-Control': 'no-store'})


def _quiet(read, default):
    try:
        return read()
    except Exception:
        return default


async def call_http(server, request):
    headers = server.CORS_HEADERS(request)
    path, method = request.path.rstrip('/'), request.method

    def reply(data, status=200):
        return web.json_response(server.ok(data) if status == 200 else server.err(status, data, {'error_code': data}),
                                 status=status, headers=headers)
    try:
        if path == '/toy/call/config' and method == 'GET':
            from runtime.call.life import retry_pending
            retry_pending(server)
            return reply(await config(server))
        if path == '/toy/call/world' and method == 'GET':
            from runtime.call.context import world
            from runtime.call.store import mark_active
            mark_active(server)
            facts, _ = await asyncio.to_thread(world, server, datetime.now(timezone.utc))
            return reply({'world': facts})
        if path == '/toy/call/start' and method == 'POST':
            if request.headers.get('X-Olivia-Companion-Action') != 'confirmed':
                return reply('COMPANION_CONFIRMATION_REQUIRED', 403)
            return reply(await start(server, request))
        if path == '/toy/call/end' and method == 'POST':
            body = json.loads(await request.text() or '{}')
            if not isinstance(body, dict):
                return reply('CALL_END_INVALID', 400)
            from runtime.call.store import mark_ended, record_call
            row = await asyncio.to_thread(record_call, server, body)
            mark_ended(server)
            from runtime.call.life import absorb_later
            absorb_later(server, row)
            return reply({'saved': True})
        if path == '/toy/call/recall' and method == 'POST':
            body = json.loads(await request.text() or '{}')
            from runtime.call.context import recall
            try:
                from original_client_companion_api import _backend
                backend = _backend(request)
            except Exception:
                return reply({'memories': []})
            text = body.get('text') if isinstance(body, dict) else None
            found = await asyncio.to_thread(lambda: _quiet(lambda: recall(backend, text), []))
            return reply({'memories': found})
        if path == '/toy/call/phones' and method == 'GET':
            return reply(await phones(server, request.app))
        if path in ('/toy/call/phone/pair', '/toy/call/phone/unpair') and method == 'POST':
            if request.headers.get('X-Olivia-Companion-Action') != 'confirmed':
                return reply('COMPANION_CONFIRMATION_REQUIRED', 403)
            if path.endswith('/pair'):
                return reply(await pair_phone(server))
            return reply(await unpair_phone(server, json.loads(await request.text() or '{}')))
        if path == '/toy/call/avatar' and method == 'GET':
            return reply(await avatar_state(server))
        if path == '/toy/call/avatar/buy' and method == 'POST':
            if request.headers.get('X-Olivia-Companion-Action') != 'confirmed':
                return reply('COMPANION_CONFIRMATION_REQUIRED', 403)
            try:
                return reply(await avatar_buy(json.loads(await request.text() or '{}')))
            except ValueError:
                return reply('CALL_AVATAR_BUY_INVALID', 400)
        if path.startswith('/toy/call/avatar/files/') and method == 'GET':
            return await avatar_file(path.rsplit('/', 1)[1], headers)
        if path == '/toy/call/recover' and method == 'POST':
            if request.headers.get('X-Olivia-Companion-Action') != 'confirmed':
                return reply('COMPANION_CONFIRMATION_REQUIRED', 403)
            return reply(await recover(server, json.loads(await request.text() or '{}')))
        return reply('NOT_FOUND', 404)
    except web.HTTPException as exc:
        return reply(exc.reason, exc.status)
    except (ValueError, KeyError) as exc:
        return reply(str(exc) if str(exc).startswith('CALL_') else 'CALL_REQUEST_INVALID', 400)
    except RuntimeError as exc:
        return reply(str(exc) if str(exc).startswith('CALL_') else 'CALL_UNAVAILABLE', 503)
