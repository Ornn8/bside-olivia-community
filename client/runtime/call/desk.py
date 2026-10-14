"""The computer's line for calls from a paired phone (the cloud's /v1/call/desk).

Her memory, her day and the past chats live here, so a call from the user's phone is still built
on this computer: while at least one phone is paired, the backend keeps one WebSocket to the call
service. When the phone dials, the service rings here and this computer answers with the same
context pack and pickup as a call from the desktop page (or "missed" while she is in the bath).
During the call it looks up memories for what the user says and sends her world when it changes;
afterwards it keeps the end report exactly as the desktop page does (memory, diary, her life).
The service closes the line with 4404 when no phone is paired; the line then rests until the call
page pairs one again (it reads the phone list while showing the QR code).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, WSMsgType, WSServerHandshakeError

log = logging.getLogger(__name__)

RETRY_SECONDS = (5, 15, 30, 60, 120)
WORLD_SECONDS = 300          # as the desktop call page: re-read her world every 5 minutes
STOP_CODES = (4404, 4409)    # no phone paired / another computer with this account took the line
_task: asyncio.Task | None = None


def _desk_url(base):
    parts = urlsplit(base)
    return ('wss' if parts.scheme == 'https' else 'ws') + '://' + parts.netloc + '/v1/call/desk'


def _backend(app):
    try:
        from original_client_companion_api import _BACKEND_KEY
        return app.get(_BACKEND_KEY) if app is not None else None
    except Exception:
        return None


def running() -> bool:
    return _task is not None and not _task.done()


def start(server, app):
    """Open the line (once at a time). Called at startup and when the call page sees a paired phone."""
    global _task
    if running():
        return _task
    _task = asyncio.get_running_loop().create_task(_run(server, app))
    return _task


async def _run(server, app):
    from runtime.call.http import _cloud
    from runtime.tls import client_tls_context
    failures = 0
    while True:
        base, key = _cloud()
        if base is None:
            return
        try:
            async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=10), trust_env=False,
                                     connector=TCPConnector(ssl=client_tls_context())) as session:
                async with session.ws_connect(_desk_url(base), headers={'Authorization': 'Bearer ' + key},
                                              heartbeat=30, max_msg_size=4 * 1024 * 1024) as ws:
                    failures = 0
                    await Line(server, app, ws).serve()
                    if ws.close_code in STOP_CODES:
                        return
        except WSServerHandshakeError as exc:
            if exc.status < 500:   # no account, or a call service without phone calls (redirect / not found)
                return
        except (ClientError, asyncio.TimeoutError, OSError, ValueError):
            pass
        except Exception:
            log.exception('call desk line failed')
        await asyncio.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
        failures += 1


class Line:
    def __init__(self, server, app, ws):
        self.server, self.app, self.ws = server, app, ws
        self.pickups, self.calls, self.tasks = {}, {}, set()   # ring -> pickup; call_id -> {pickup, world task, recalled}

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def send(self, value):
        if not self.ws.closed:
            await self.ws.send_json(value)

    async def serve(self):
        try:
            async for msg in self.ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    message = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await self.handle(message)
        finally:
            for call in self.calls.values():
                call['world'].cancel()
            for task in list(self.tasks):
                task.cancel()

    async def handle(self, m):
        kind = m.get('type')
        if kind == 'ring' and isinstance(m.get('ring'), str):
            self.spawn(self.answer(m['ring']))
        elif kind == 'started' and isinstance(m.get('call_id'), str):
            from runtime.call.store import mark_active
            mark_active(self.server)
            self.calls[m['call_id']] = {'pickup': self.pickups.pop(m.get('ring'), 'answer'), 'recalled': set(),
                                        'world': asyncio.get_running_loop().create_task(self.world(m['call_id']))}
        elif kind == 'user' and m.get('call_id') in self.calls:
            self.spawn(self.recall(m['call_id'], m.get('text')))
        elif kind == 'end' and isinstance(m.get('report'), dict):
            await self.keep(m['report'])

    async def answer(self, ring):
        from runtime.call.context import build
        from runtime.call.store import record_missed
        try:
            context, pickup = await asyncio.to_thread(build, self.server, _backend(self.app))
        except Exception:
            log.exception('phone call context failed')   # the phone hears "no answer from the computer"
            return
        if pickup == 'missed':
            await asyncio.to_thread(record_missed, self.server, 'bathing')
            await self.send({'type': 'missed', 'ring': ring})
            return
        self.pickups[ring] = pickup
        await self.send({'type': 'answer', 'ring': ring, 'context': context, 'pickup': pickup})

    async def world(self, call_id):
        from runtime.call.context import world
        from runtime.call.store import mark_active
        last = None
        while True:
            await asyncio.sleep(WORLD_SECONDS)
            mark_active(self.server)
            try:
                facts, _ = await asyncio.to_thread(world, self.server, datetime.now(timezone.utc))
            except Exception:
                continue
            text = json.dumps(facts, ensure_ascii=False, sort_keys=True)
            if text != last:
                last = text
                await self.send({'type': 'world', 'call_id': call_id, 'world': facts})

    # What the user just said may touch something she remembers: look it up here and pass it on.
    async def recall(self, call_id, text):
        from runtime.call.context import recall
        backend = _backend(self.app)
        if backend is None or not isinstance(text, str) or len(text.strip()) < 4:
            return
        try:
            found = await asyncio.to_thread(recall, backend, text)
        except Exception:
            return
        call = self.calls.get(call_id)
        if call is None:
            return
        fresh = [m for m in found if m not in call['recalled']][:3]
        call['recalled'].update(fresh)
        if fresh:
            await self.send({'type': 'recall', 'call_id': call_id, 'memories': fresh})

    async def keep(self, report):
        from runtime.call.life import absorb_later
        from runtime.call.store import mark_ended, record_call
        call_id = report.get('call_id')
        call = self.calls.pop(call_id, None)
        if call is not None:
            call['world'].cancel()
        if not self.calls:
            mark_ended(self.server)
        if report.get('transcript'):
            body = {key: report.get(key) for key in ('call_id', 'seconds', 'charge_cents', 'reason', 'transcript', 'notes')}
            body['pickup'] = (call or {}).get('pickup', 'answer')
            try:
                row = await asyncio.to_thread(record_call, self.server, body)
            except ValueError:
                row = None
            if row is not None:
                absorb_later(self.server, row)
        if isinstance(call_id, str):
            await self.send({'type': 'kept', 'call_id': call_id})
