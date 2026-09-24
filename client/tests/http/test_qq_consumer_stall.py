"""A stalled post-delivery consumer must not hold the QQ conversation forever."""
import asyncio
from types import SimpleNamespace

from runtime.personal_chat import backend
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def test_stalled_consumer_releases_next_reply_and_recovers_without_resend(monkeypatch):
    async def scenario():
        rows, sent = [], []
        stalled = asyncio.Event()
        release = asyncio.Event()

        async def commit(server, row):
            if row['content'] == 'first':
                stalled.set()
                await release.wait()
            row['consumer_done'] = True

        monkeypatch.setattr(backend, 'commit', commit)
        monkeypatch.setattr(backend, '_CONSUMER_TIMEOUT_SECONDS', .02, raising=False)
        server = SimpleNamespace(_persist_store_state=lambda: None, _safe_log=lambda *a, **k: None)

        async def generate(event, row):
            return event.text + ' reply'

        async def send(text):
            sent.append(text)
            return str(len(sent))

        service = PersonalChatService(rows, lambda: None, generate,
            lambda row: backend.recoverable_commit(server, row), {'qq': ('100', '200')})
        first = asyncio.create_task(service.handle(PersonalMessage('qq', '100', '200', '1', 'first'), send))
        await stalled.wait()
        try:
            await asyncio.wait_for(service.handle(PersonalMessage('qq', '100', '200', '2', 'second'), send), .3)
            assert sent == ['first reply', 'second reply']
            assert rows[0]['delivery_status'] == 'DELIVERED'
            assert rows[0]['consumer_error_code'] == 'PERSONAL_CHAT_CONSUMER_TIMEOUT'
            release.set()
            rows[0]['consumer_retry_at'] = 0
            await service.recover()
            assert rows[0]['consumer_done']
            assert 'consumer_error_code' not in rows[0]
            assert len(sent) == 2
        finally:
            release.set()
            await first

    asyncio.run(scenario())


def test_daily_life_task_survives_consumer_timeout(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        row = {'letter_id': 'synthetic', 'delivery_status': 'DELIVERED'}
        server = SimpleNamespace(daily_life_tasks={'reply:synthetic:1': task},
            _schedule_daily_life_exchange=lambda row: None,
            _persist_store_state=lambda: None, _safe_log=lambda *a, **k: None)
        monkeypatch.setattr(backend, 'commit', backend._commit_life)
        monkeypatch.setattr(backend, '_CONSUMER_TIMEOUT_SECONDS', .01)
        try:
            await backend.recoverable_commit(server, row)
            assert row['consumer_error_code'] == 'PERSONAL_CHAT_CONSUMER_TIMEOUT'
            assert not task.done()
            release.set()
            await task
        finally:
            release.set()
            await task
    asyncio.run(scenario())


def test_recovery_releases_lock_between_old_rows():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        rows = [{'letter_id': str(i), 'delivery_status': 'DELIVERED'} for i in range(4)]
        async def commit(row):
            order.append(row['letter_id'])
            if row['letter_id'] == '0':
                entered.set()
                await release.wait()
        service = PersonalChatService(rows, lambda: None, None, commit, {})
        async def waiting_message():
            async with service.lock:
                order.append('new-message')
        recovering = asyncio.create_task(service.recover())
        await entered.wait()
        waiting = asyncio.create_task(waiting_message())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(recovering, waiting)
        assert order.index('new-message') < order.index('1')
    asyncio.run(scenario())


def test_bundle_keeps_chat_channel_and_delivery_without_content():
    import io
    import json
    import zipfile
    from runtime.diagnostics.support_bundle import build_diagnostic_bundle
    from tests.http.test_diagnostic_support_bundle import _source
    source = _source()
    source['tasks'] = {'status': 'active', 'pending': 1, 'items': [{
        'status': 'processing', 'stage': 'reply_generation', 'elapsed_bucket': 'under_1m',
        'channel': 'qq', 'delivery_status': 'GENERATED',
        'consumer_error_code': 'PERSONAL_CHAT_CONSUMER_TIMEOUT',
        'content': 'private-message', 'owner_id': 'private-account', 'token': 'private-secret'}]}
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as z:
        raw = z.read('tasks.json')
        item = json.loads(raw)['items'][0]
    assert item['channel'] == 'qq'
    assert item['delivery_status'] == 'GENERATED'
    assert item['consumer_error_code'] == 'PERSONAL_CHAT_CONSUMER_TIMEOUT'
    assert b'private-' not in raw


def test_onebot_probe_and_next_reply_survive_stalled_consumer(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from runtime.personal_chat.qq import run_qq
    from tests.http.test_personal_chat_qq import TOKEN, event, login

    async def scenario():
        stop, stalled = asyncio.Event(), asyncio.Event()
        sent = []
        async def commit(server, row):
            if row['content'] == 'first':
                stalled.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(backend, 'commit', commit)
        monkeypatch.setattr(backend, '_CONSUMER_TIMEOUT_SECONDS', .15)
        server_stub = SimpleNamespace(_persist_store_state=lambda: None, _safe_log=lambda *a, **k: None)
        async def generate(message, row):
            return message.text + ' reply'
        service = PersonalChatService([], lambda: None, generate,
            lambda row: backend.recoverable_commit(server_stub, row), {'qq': ('100', '200')})
        async def handle(message, send):
            await service.handle(message, send)
            if message.text == 'second':
                stop.set()
        async def control(message, send):
            await send('1611')
        handle.is_control_message = lambda message: message.text == '/连接测试'
        handle.handle_control = control
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1, message=[{'type': 'text', 'data': {'text': 'first'}}]))
            for index in range(3):
                raw = await asyncio.wait_for(ws.receive_json(), 1)
                sent.append(raw['params']['message'][-1]['data']['text'])
                await ws.send_json({'echo': raw['echo'], 'status': 'ok', 'retcode': 0,
                                    'data': {'message_id': 301 + index}})
                if index == 0:
                    await stalled.wait()
                    for identifier, text in [(2, 'second'), (3, '/连接测试')]:
                        await ws.send_json(event(identifier, message=[{'type': 'text', 'data': {'text': text}}]))
            await stop.wait()
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get('/', socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url('/')), TOKEN, '100', '200',
                handle, stop, merge_seconds=0), 3)
        assert sent == ['first reply', '1611', 'second reply']
    asyncio.run(scenario())
