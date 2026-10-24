"""Model calls on the server's loop share one kept-alive connection; anywhere else each call has its own session."""
import asyncio
import ssl

import aiohttp
from aiohttp import web

from runtime import relay_session


async def _relay():
    peers = []

    async def answer(request):
        peers.append(request.transport.get_extra_info('peername')[1])
        return web.json_response({'ok': await request.json()})

    app = web.Application()
    app.router.add_post('/v1/chat/completions', answer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f'http://127.0.0.1:{port}/v1/chat/completions', peers


async def _call(url, value):
    async with relay_session.session(aiohttp.ClientTimeout(total=5)) as http:
        async with http.post(url, json=value, ssl=ssl.create_default_context()) as response:
            return (await response.json())['ok']


def test_calls_on_the_server_loop_reuse_one_connection():
    async def run():
        runner, url, peers = await _relay()
        await relay_session.start()
        try:
            assert [await _call(url, n) for n in range(3)] == [0, 1, 2]
        finally:
            await relay_session.stop()
            await runner.cleanup()
        return peers
    peers = asyncio.run(run())
    assert len(peers) == 3 and len(set(peers)) == 1


def test_without_the_shared_session_each_call_has_its_own():
    async def run():
        runner, url, peers = await _relay()
        try:
            assert [await _call(url, n) for n in range(2)] == [0, 1]
        finally:
            await runner.cleanup()
        return peers
    peers = asyncio.run(run())
    assert len(set(peers)) == 2


def test_another_loop_never_borrows_the_server_session():
    async def run():
        runner, url, peers = await _relay()
        await relay_session.start()
        try:
            await _call(url, 'server')
            # A worker thread with its own loop gets a session of its own for the call.
            assert await asyncio.to_thread(asyncio.run, _call(url, 'worker')) == 'worker'
            await _call(url, 'server again')
        finally:
            await relay_session.stop()
            await runner.cleanup()
        return peers
    peers = asyncio.run(run())
    assert len(peers) == 3 and peers[0] == peers[2] != peers[1]


def test_stop_closes_the_session_and_a_later_start_opens_a_new_one():
    async def run():
        await relay_session.start()
        first = relay_session._state['session']
        await relay_session.stop()
        assert first.closed and relay_session._state['session'] is None
        await relay_session.start()
        second = relay_session._state['session']
        await relay_session.stop()
        return first is not second
    assert asyncio.run(run())
