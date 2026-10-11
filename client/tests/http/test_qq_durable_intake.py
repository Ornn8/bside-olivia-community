import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.qq import run_qq
from runtime.personal_chat.service import PersonalChatService


TOKEN = 'synthetic-onebot-durable-token'


def event(number, text='hello'):
    return {'post_type': 'message', 'message_type': 'private', 'self_id': 100,
            'user_id': 200, 'message_id': number,
            'message': [{'type': 'text', 'data': {'text': text}}]}


async def login(ws):
    request = await ws.receive_json()
    await ws.send_json({'echo': request['echo'], 'status': 'ok', 'retcode': 0,
                        'data': {'user_id': 100}})


async def acknowledge(ws):
    request = await receive_reply(ws)
    await ws.send_json({'echo': request['echo'], 'status': 'ok', 'retcode': 0,
                        'data': {'message_id': 300}})
    return request


async def receive_reply(ws):
    while True:
        request = await ws.receive_json()
        if request['action'] != 'set_input_status':
            return request
        assert request['params'] == {'user_id': '200', 'event_type': 1}
        await ws.send_json({'echo': request['echo'], 'status': 'ok', 'retcode': 0, 'data': None})


async def run(socket, handler, stop, *, merge_seconds=0):
    app = web.Application()
    app.router.add_get('/', socket)
    async with TestServer(app) as server:
        await asyncio.wait_for(run_qq(str(server.make_url('/')), TOKEN, '100', '200',
                                     handler, stop, merge_seconds=merge_seconds, reconnect_delay=.01), 3)


def test_slow_generation_allows_durable_intake_and_control_ack():
    async def scenario():
        stop, started, recorded, probed = (asyncio.Event() for _ in range(4))
        durable, handled = [], []

        async def ingest(message):
            durable.append(message.message_id)
            if message.message_id == '2':
                recorded.set()

        async def handler(message, send):
            handled.append(message.message_id)
            if message.message_id == '1':
                started.set()
                await recorded.wait()
                await probed.wait()
            await send('reply')
            if message.message_id == '2':
                stop.set()

        async def control(message, send):
            await send('probe')
            probed.set()

        handler.ingest = ingest
        handler.is_control_message = lambda message: message.text == '/连接测试'
        handler.handle_control = control

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1))
            await started.wait()
            await ws.send_json(event(2))
            await ws.send_json(event(3, '/连接测试'))
            for _ in range(3):
                await acknowledge(ws)
            await stop.wait()
            await ws.close()
            return ws

        await run(socket, handler, stop)
        assert durable == ['1', '2']
        assert handled == ['1', '2']
        assert probed.is_set()
    asyncio.run(scenario())


def test_slow_persistence_does_not_block_socket_ack_and_rejection_is_not_dispatched():
    async def scenario():
        stop, saving, acked = (asyncio.Event() for _ in range(3))
        handled = []

        async def ingest(message):
            if message.message_id == '2':
                saving.set()
                await acked.wait()
                return False

        async def handler(message, send):
            handled.append(message.message_id)
            await saving.wait()
            await send('reply')
            acked.set()
            await asyncio.sleep(.02)
            stop.set()

        handler.ingest = ingest

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1))
            await ws.send_json(event(2))
            await acknowledge(ws)
            await stop.wait()
            await ws.close()
            return ws

        await run(socket, handler, stop)
        assert handled == ['1']
        assert acked.is_set()
    asyncio.run(scenario())


def test_reconnect_scans_durable_pending_only_after_verified_login():
    async def scenario():
        stop = asyncio.Event()
        durable = [PersonalMessage('qq', '100', '200', '1', 'persisted before disconnect')]
        connections, scans, handled = [], [], []

        async def ingest(message):
            raise AssertionError('recovered input must not be re-ingested')

        def pending(channel):
            scans.append(channel)
            return tuple(durable)

        async def handler(message, send):
            handled.append(message.message_id)
            await send('recovered')
            durable.clear()
            stop.set()

        handler.ingest, handler.pending = ingest, pending

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            connections.append(ws)
            if len(connections) == 1:
                await ws.receive_json()
                assert not scans
                await ws.close()
                return ws
            await login(ws)
            await acknowledge(ws)
            await stop.wait()
            await ws.close()
            return ws

        await run(socket, handler, stop)
        assert len(connections) == 2
        assert handled == ['1']
        assert scans and set(scans) == {'qq'}
    asyncio.run(scenario())


def test_durable_backlog_larger_than_response_queue_does_not_stall_intake_or_duplicate_sources():
    async def scenario():
        stop, started, all_saved = (asyncio.Event() for _ in range(3))
        saved = asyncio.Queue()
        durable, handled = {}, []

        async def ingest(message):
            durable.setdefault(message.message_id, message)
            saved.put_nowait(message.message_id)
            if message.message_id == '60':
                all_saved.set()

        async def handler(message, send):
            for source_id, _ in message.sources:
                durable.pop(source_id, None)
                handled.append(source_id)
            if message.message_id == '1':
                started.set()
                await all_saved.wait()
            await send('reply ' + message.message_id)
            if message.message_id == '60':
                stop.set()

        handler.ingest = ingest
        handler.pending = lambda channel: tuple(durable.values())

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1))
            await saved.get()
            await started.wait()
            # Pace raw ingress independently of generation; exceed the bounded
            # response queue and replay one queued source before it is handled.
            for number in [*range(2, 60), 2, 60]:
                await ws.send_json(event(number))
                assert await saved.get() == str(number)
            for _ in range(60):
                await acknowledge(ws)
            await stop.wait()
            await ws.close()
            return ws

        await run(socket, handler, stop)
        assert handled == [str(number) for number in range(1, 61)]
        assert not durable
    asyncio.run(scenario())


def test_shutdown_cancels_unfinished_intake_without_dispatch():
    async def scenario():
        stop, saving, cancelled = (asyncio.Event() for _ in range(3))

        async def ingest(message):
            saving.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def handler(message, send):
            raise AssertionError('uncommitted input was dispatched')

        handler.ingest = ingest

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1))
            await saving.wait()
            stop.set()
            await cancelled.wait()
            await ws.close()
            return ws

        await run(socket, handler, stop)
        assert cancelled.is_set()
    asyncio.run(scenario())


def service_handler(service):
    async def handler(message, send):
        await service.handle(message, send)
    handler.ingest = service.ingest
    handler.pending = service.pending
    return handler


def test_actual_service_persists_second_input_then_sends_one_merged_reply(tmp_path):
    async def scenario():
        stop, started, second_saved = (asyncio.Event() for _ in range(3))
        rows, generated, sent = [], [], []
        path = tmp_path / 'chat.json'

        def persist():
            path.write_text(json.dumps(rows), encoding='utf-8')
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if any(r.get('source_messages') == {'2': 'second'} and r['delivery_status'] == 'RECEIVED' for r in snapshot):
                second_saved.set()

        async def generate(message, row):
            generated.append(message.message_id)
            if len(generated) == 1:
                started.set()
                await second_saved.wait()
                assert generated == ['1']
            return 'reply ' + message.message_id

        async def commit(row):
            if set(row['source_messages']) == {'1', '2'}:
                stop.set()

        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('100', '200')})

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1, 'first'))
            await started.wait()
            await ws.send_json(event(2, 'second'))
            for _ in range(1):
                sent.append((await acknowledge(ws))['params']['message'])
            await stop.wait()
            await ws.close()
            return ws

        await run(socket, service_handler(service), stop)
        assert generated == ['1', '1']
        assert [message[0]['data']['id'] for message in sent] == ['1']
        assert [r['delivery_status'] for r in json.loads(path.read_text(encoding='utf-8'))] == ['DELIVERED', 'SKIPPED']
    asyncio.run(scenario())


def test_actual_service_coalesces_two_raw_receipts_into_one_reply(tmp_path):
    async def scenario():
        stop = asyncio.Event()
        rows, snapshots, generated = [], [], []
        path = tmp_path / 'chat.json'

        def persist():
            path.write_text(json.dumps(rows), encoding='utf-8')
            snapshots.append(json.loads(path.read_text(encoding='utf-8')))

        async def generate(message, row):
            generated.append(message)
            return 'one reply'

        async def commit(row):
            stop.set()

        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('100', '200')})

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1, 'first'))
            await ws.send_json(event(2, 'second'))
            await acknowledge(ws)
            await stop.wait()
            await ws.close()
            return ws

        await run(socket, service_handler(service), stop, merge_seconds=.02)
        assert any(len(snapshot) == 2 and all(r['delivery_status'] == 'RECEIVED' for r in snapshot) for snapshot in snapshots)
        assert len(generated) == 1
        assert dict(generated[0].sources) == {'1': 'first', '2': 'second'}
        assert rows[0]['delivery_status'] == 'DELIVERED'
        assert rows[1]['delivery_status'] == 'SKIPPED'
        assert rows[1]['superseded_by'] == rows[0]['letter_id']
    asyncio.run(scenario())


def test_actual_service_restart_recovers_received_not_interrupted_generation(tmp_path):
    async def scenario():
        stop, started, second_saved = (asyncio.Event() for _ in range(3))
        rows = []
        path = tmp_path / 'chat.json'

        def persist():
            path.write_text(json.dumps(rows), encoding='utf-8')
            if any(r.get('source_messages') == {'2': 'second'} and r['delivery_status'] == 'RECEIVED' for r in rows):
                second_saved.set()

        async def generate(message, row):
            started.set()
            await asyncio.Event().wait()

        async def commit(row):
            pass

        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('100', '200')})

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1, 'first'))
            await started.wait()
            await ws.send_json(event(2, 'second'))
            await second_saved.wait()
            stop.set()
            await ws.close()
            return ws

        await run(socket, service_handler(service), stop)
        rows = json.loads(path.read_text(encoding='utf-8'))
        assert rows[1]['delivery_status'] == 'RECEIVED'
        assert rows[0]['delivery_status'] in {'GENERATING', 'FAILED'}
        stop = asyncio.Event()
        generated = []

        async def resumed_generate(message, row):
            generated.append(message.message_id)
            return 'recovered second'

        async def resumed_commit(row):
            stop.set()

        resumed = PersonalChatService(rows, persist, resumed_generate, resumed_commit, {'qq': ('100', '200')})
        assert [item.message_id for item in resumed.pending('qq')] == ['2']

        async def resumed_socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            login_request = await ws.receive_json()
            await asyncio.sleep(.02)
            assert not generated
            await ws.send_json({'echo': login_request['echo'], 'status': 'ok', 'retcode': 0,
                                'data': {'user_id': 100}})
            outgoing = await acknowledge(ws)
            assert outgoing['params']['message'][0]['data']['id'] == '2'
            await stop.wait()
            await ws.close()
            return ws

        await run(resumed_socket, service_handler(resumed), stop)
        assert generated == ['2']
    asyncio.run(scenario())


def test_actual_service_input_during_network_ack_keeps_both_causal_replies():
    async def scenario():
        stop, second_saved = asyncio.Event(), asyncio.Event()
        rows, generated, sent = [], [], []

        async def persist():
            if any(r['source_messages'] == {'2': 'second'} and r['delivery_status'] == 'RECEIVED' for r in rows):
                second_saved.set()

        async def generate(message, row):
            generated.append(message.message_id)
            return 'reply ' + message.message_id

        async def commit(row):
            if row['source_messages'] == {'2': 'second'}:
                stop.set()

        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('100', '200')})

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1, 'first'))
            outgoing = await receive_reply(ws)
            sent.append(outgoing)
            assert rows[0]['delivery_status'] == 'SENDING'
            await ws.send_json(event(2, 'second'))
            # The intake lock cannot span the network ACK wait.
            await second_saved.wait()
            await ws.send_json({'echo': outgoing['echo'], 'status': 'ok', 'retcode': 0,
                                'data': {'message_id': 301}})
            sent.append(await acknowledge(ws))
            await stop.wait()
            await ws.close()
            return ws

        await run(socket, service_handler(service), stop)
        assert generated == ['1', '2']
        assert [item['params']['message'][0]['data']['id'] for item in sent] == ['1', '2']
        assert [row['delivery_status'] for row in rows] == ['DELIVERED', 'DELIVERED']
    asyncio.run(scenario())


def test_actual_service_two_invalidations_drain_queued_old_sources_once():
    async def scenario():
        stop = asyncio.Event()
        began = [asyncio.Event(), asyncio.Event()]
        recorded = [asyncio.Event(), asyncio.Event()]
        rows, generated, replies = [], [], []

        async def persist():
            for i, identifier in enumerate(('2', '3')):
                if any(identifier in r['source_messages'] for r in rows):
                    recorded[i].set()

        async def generate(message, row):
            generated.append(dict(message.sources))
            index = len(generated) - 1
            if index < 2:
                began[index].set()
                await recorded[index].wait()
                return 'old draft'
            return 'final merged reply'

        async def commit(row):
            stop.set()

        service = PersonalChatService(rows, persist, generate, commit, {'qq': ('100', '200')})

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1, 'first'))
            await began[0].wait()
            await ws.send_json(event(2, 'second'))
            await began[1].wait()
            await ws.send_json(event(3, 'third'))
            replies.append(await acknowledge(ws))
            await stop.wait()
            await asyncio.sleep(.02)
            await ws.close()
            return ws

        await run(socket, service_handler(service), stop, merge_seconds=.005)
        assert generated == [{'1': 'first'}, {'1': 'first', '2': 'second'},
                             {'1': 'first', '2': 'second', '3': 'third'}]
        assert len(replies) == 1
        assert replies[0]['params']['message'][0]['data']['id'] == '1'
        assert not service.pending('qq')
        assert sum(r['delivery_status'] == 'DELIVERED' for r in rows) == 1
    asyncio.run(scenario())
