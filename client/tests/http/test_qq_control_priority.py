import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from runtime.personal_chat.qq import run_qq
from tests.http.test_personal_chat_qq import TOKEN, event, login


def test_pending_code_is_not_merged_and_probe_bypasses_busy_generation():
    async def scenario():
        stop, busy, release, verified, probe_done = (asyncio.Event() for _ in range(5))
        seen = []
        async def handle(message, send):
            seen.append(message.text)
            if message.text == '1611':
                verified.set()
            elif message.text == '/连接测试':
                assert await send('probe') == '301'
                probe_done.set()
            else:
                busy.set()
                await release.wait()
        handle.is_control_message = lambda message: message.text in {'1611', '/连接测试'}
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            for i, text in enumerate(['1611', 'hello'], 1):
                await ws.send_json(event(i, message=[{'type': 'text', 'data': {'text': text}}]))
            await asyncio.wait_for(busy.wait(), 1)
            await ws.send_json(event(3, message=[{'type': 'text', 'data': {'text': '/连接测试'}}]))
            try:
                request = await asyncio.wait_for(ws.receive_json(), .5)
                await ws.send_json({'echo': request['echo'], 'status': 'ok', 'retcode': 0,
                                    'data': {'message_id': 301}})
                await asyncio.wait_for(probe_done.wait(), .5)
            finally:
                release.set()
                stop.set()
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get('/', socket)
        async with TestServer(app) as server:
            try:
                await asyncio.wait_for(run_qq(str(server.make_url('/')), TOKEN, '100', '200',
                    handle, stop, merge_seconds=.05), 3)
            finally:
                release.set()
                stop.set()
        assert verified.is_set(), seen
        assert probe_done.is_set(), seen
        assert seen == ['1611', 'hello', '/连接测试']
    asyncio.run(scenario())
