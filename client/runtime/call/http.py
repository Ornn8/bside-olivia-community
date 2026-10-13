"""Local endpoints of the call page. The Olivia key never leaves this backend.

    GET  /toy/call/config   whether calls are open and the current per-minute price
    POST /toy/call/start    (X-Olivia-Companion-Action: confirmed) → missed, or a one-time cloud ticket
    GET  /toy/call/world    her world now, for the page to forward when it changes during a call
    POST /toy/call/end      the call's end report → kept, remembered, visible to QQ replies
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from runtime.cloud_service import endpoint
from runtime.tls import client_tls_context

# A call holds money in blocks while it runs; this is only the per-call ceiling the user consents to.
MAX_CHARGE_CENTS = 6000
CONFIG_TIMEOUT = ClientTimeout(total=6)
TICKET_TIMEOUT = ClientTimeout(total=12)


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
    return {'available': bool(value.get('available')) and type(price) is int,
            'cents_per_minute': price if type(price) is int else None,
            'base_cents_per_minute': value.get('base_cents_per_minute'),
            'busy_cents_per_minute': value.get('busy_cents_per_minute')}


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
    context, pickup = await asyncio.to_thread(build, server, backend)
    if pickup == 'missed':
        await asyncio.to_thread(record_missed, server, 'bathing')
        return {'pickup': 'missed'}
    headers = {'Authorization': 'Bearer ' + key, 'X-Olivia-Max-Charge-Cents': str(MAX_CHARGE_CENTS)}
    try:
        status, value = await _cloud_json('POST', '/v1/call/ticket', timeout=TICKET_TIMEOUT, headers=headers,
                                          body={'v': 1, 'context': context, 'pickup': pickup})
    except (ClientError, asyncio.TimeoutError, ValueError):
        raise web.HTTPServiceUnavailable(reason='CALL_NETWORK')
    if status != 200 or not isinstance(value, dict) or not isinstance(value.get('ticket'), str):
        raise web.HTTPServiceUnavailable(reason={401: 'CALL_AUTH_FAILED', 402: 'CALL_INSUFFICIENT_BALANCE',
                                                 503: 'CALL_BUSY'}.get(status, 'CALL_UNAVAILABLE'))
    return {'pickup': pickup, 'ticket': value['ticket'], 'ws_url': _ws_url(base),
            'cents_per_minute': value.get('cents_per_minute')}


async def call_http(server, request):
    headers = server.CORS_HEADERS(request)
    path, method = request.path.rstrip('/'), request.method

    def reply(data, status=200):
        return web.json_response(server.ok(data) if status == 200 else server.err(status, data, {'error_code': data}),
                                 status=status, headers=headers)
    try:
        if path == '/toy/call/config' and method == 'GET':
            return reply(await config(server))
        if path == '/toy/call/world' and method == 'GET':
            from runtime.call.context import world
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
            from runtime.call.store import record_call
            await asyncio.to_thread(record_call, server, body)
            return reply({'saved': True})
        return reply('NOT_FOUND', 404)
    except web.HTTPException as exc:
        return reply(exc.reason, exc.status)
    except (ValueError, KeyError) as exc:
        return reply(str(exc) if str(exc).startswith('CALL_') else 'CALL_REQUEST_INVALID', 400)
    except RuntimeError as exc:
        return reply(str(exc) if str(exc).startswith('CALL_') else 'CALL_UNAVAILABLE', 503)
