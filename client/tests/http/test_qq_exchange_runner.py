import asyncio

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.exchange_runner import ExchangeRunner
from runtime.personal_chat.service import PersonalChatService


def sender(sent):
    async def send(text):
        sent.append(text)
        return 'receipt'
    send.is_available = lambda: True
    send.for_exchange = lambda event: send
    return send


@pytest.mark.parametrize('finish_offline', [False, True])
def test_disconnect_keeps_generation_and_resumes_once(finish_offline):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls, first, second = [], [], []

        async def generate(event, row):
            calls.append(event.message_id)
            started.set()
            await release.wait()
            return 'finished'

        async def commit(row):
            pass

        service = PersonalChatService([], lambda: None, generate, commit, {'qq': ('100', '200')})

        async def handle(event, send):
            await service.handle(event, send)

        runner = ExchangeRunner(handle)
        old, new = sender(first), sender(second)
        runner.ready('qq', old)
        event = PersonalMessage('qq', '100', '200', '1', 'hello')
        waiting = asyncio.create_task(runner(event, old))
        await started.wait()
        runner.disconnected(old)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert service.rows[0]['delivery_status'] == 'GENERATING'
        core = tuple(runner.tasks.values())
        if not finish_offline:
            runner.ready('qq', new)
        release.set()
        await asyncio.gather(*core, return_exceptions=True)
        if finish_offline:
            assert service.rows[0]['delivery_status'] == 'GENERATED'
            runner.ready('qq', new)
            await runner(service.pending('qq')[0], new)
        assert calls == ['1'] and first == [] and second == ['finished']
        assert service.rows[0]['delivery_status'] == 'DELIVERED'
        await runner.close()
        await asyncio.gather(*service.consumer_tasks.values())

    asyncio.run(scenario())


def test_shutdown_cancels_owned_generation_and_old_disconnect_does_not_clear_new_lease():
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handle(event, send):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runner = ExchangeRunner(handle)
        old, new = sender([]), sender([])
        runner.ready('qq', old)
        runner.ready('qq', new)
        runner.disconnected(old)
        assert runner.sender is new
        waiting = asyncio.create_task(runner(PersonalMessage('qq', '100', '200', '1', 'hello'), new))
        await started.wait()
        await runner.close()
        await asyncio.gather(waiting, return_exceptions=True)
        assert cancelled.is_set() and not runner.send.is_available()

    asyncio.run(scenario())


def test_disconnect_after_send_reservation_never_resends():
    async def scenario():
        entered, fail = asyncio.Event(), asyncio.Event()
        calls = []

        async def send(text):
            calls.append(text)
            entered.set()
            await fail.wait()
            raise ConnectionError('synthetic')

        send.is_available = lambda: True
        send.for_exchange = lambda event: send

        async def generate(event, row):
            return 'finished'

        service = PersonalChatService([], lambda: None, generate, None, {'qq': ('100', '200')})
        runner = ExchangeRunner(service.handle)
        runner.ready('qq', send)
        waiting = asyncio.create_task(runner(PersonalMessage('qq', '100', '200', '1', 'hello'), send))
        await entered.wait()
        runner.disconnected(send)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        core = tuple(runner.tasks.values())
        fail.set()
        await asyncio.gather(*core, return_exceptions=True)
        runner.ready('qq', sender(calls))
        assert service.rows[0]['delivery_status'] == 'SENDING'
        assert not service.pending('qq') and calls == ['finished']
        await runner.close()

    asyncio.run(scenario())


def test_real_socket_replacement_keeps_one_paid_generation():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from runtime.personal_chat.qq import run_qq

    async def scenario():
        started, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls, sent, sockets = [], [], []

        async def generate(event, row):
            calls.append(event.message_id)
            started.set()
            await release.wait()
            return 'finished'

        async def commit(row):
            pass

        service = PersonalChatService([], lambda: None, generate, commit, {'qq': ('100', '200')})

        async def handle(event, send):
            await service.handle(event, send)
            stop.set()

        handle.ingest, handle.pending = service.ingest, service.pending
        runner = ExchangeRunner(handle)

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            login = await ws.receive_json()
            await ws.send_json(dict(echo=login['echo'], status='ok', retcode=0, data={'user_id': 100}))
            if len(sockets) == 1:
                await ws.send_json(dict(post_type='message', message_type='private', self_id=100, user_id=200,
                                        message_id=1, message=[dict(type='text', data={'text': 'hello'})]))
                await started.wait()
                await ws.close()
            else:
                release.set()
                outgoing = await ws.receive_json()
                sent.append(outgoing)
                await ws.send_json(dict(echo=outgoing['echo'], status='ok', retcode=0, data={'message_id': 300}))
                await stop.wait()
                await ws.close()
            return ws

        app = web.Application()
        app.router.add_get('/', socket)
        async with TestServer(app) as server:
            async def listen():
                while not stop.is_set():
                    try:
                        await run_qq(str(server.make_url('/')), 'synthetic-token-123456', '100', '200',
                                     runner, stop, merge_seconds=0, reconnect_delay=.01)
                    except RuntimeError as exc:
                        assert str(exc) == 'QQ_CONNECTION_LOST_DURING_EXCHANGE'
            try:
                await asyncio.wait_for(listen(), 3)
            finally:
                await runner.close()
        assert calls == ['1'] and len(sent) == 1 and len(sockets) == 2
        assert service.rows[0]['delivery_status'] == 'DELIVERED'
        await asyncio.gather(*service.consumer_tasks.values())

    asyncio.run(scenario())
