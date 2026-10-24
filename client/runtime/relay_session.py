"""One keep-alive HTTPS session for model calls made on the server's own event loop.

A new session per call paid a TCP and TLS handshake every time, about 0.3 s to the relay before a byte of the
request went out. The server opens this session when it starts and closes it when it stops; a call made on any
other loop (tests, a worker's own loop) keeps the old one-session-per-call behaviour.
"""
import asyncio
import ssl

import aiohttp

from runtime.tls import client_tls_context

_state = {'loop': None, 'session': None, 'tls': None}


async def start():
    loop = asyncio.get_running_loop()
    if _state['session'] is not None and not _state['session'].closed and _state['loop'] is loop:
        return
    tls = client_tls_context()
    connector = aiohttp.TCPConnector(ssl=tls, limit=32, keepalive_timeout=30)
    _state.update(loop=loop, tls=tls, session=aiohttp.ClientSession(connector=connector))


async def stop():
    session = _state['session']
    _state.update(loop=None, session=None, tls=None)
    if session is not None and not session.closed:
        await session.close()


class session:
    """``async with session(timeout) as http: http.post(url, ...)``: the shared session when this is the
    server's loop, otherwise a session of its own for this one call."""

    def __init__(self, timeout):
        self._timeout = timeout
        self._own = None

    async def __aenter__(self):
        shared = _state['session']
        if shared is not None and not shared.closed and _state['loop'] is asyncio.get_running_loop():
            return _Bound(shared, self._timeout, _state['tls'])
        self._own = aiohttp.ClientSession(timeout=self._timeout)
        return _Bound(await self._own.__aenter__(), None, None)

    async def __aexit__(self, *exc):
        if self._own is not None:
            return await self._own.__aexit__(*exc)
        return False


class _Bound:
    def __init__(self, http, timeout, tls):
        self._http = http
        self._timeout = timeout
        self._tls = tls

    def post(self, url, **kwargs):
        if self._timeout is not None:
            kwargs['timeout'] = self._timeout
        # A pooled connection is found by its TLS context: a context made for this call would never match one.
        if self._tls is not None and isinstance(kwargs.get('ssl'), ssl.SSLContext):
            kwargs['ssl'] = self._tls
        return self._http.post(url, **kwargs)
