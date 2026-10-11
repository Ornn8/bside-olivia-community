import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web


@pytest.mark.parametrize('failed_turn', [False, True])
def test_reconnect_clears_transport_error_and_preserves_failed_turn(tmp_path, monkeypatch, failed_turn):
    import local_server
    from runtime.personal_chat import backend, qq

    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    config = tmp_path / 'channels.json'
    config.write_text(json.dumps({'qq': {'url': 'ws://127.0.0.1:3001', 'account': '10000', 'owner': '20000'}}))
    monkeypatch.setenv('OLIVIA_PERSONAL_CHAT_CONFIG', str(config))
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    logs = []
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda event, **fields: logs.append(dict(event=event, **fields)))
    if failed_turn:
        server.store.personal_chats.append(dict(channel='qq', delivery_status='FAILED',
            error_code='PERSONAL_CHAT_GENERATION_INTERRUPTED', life_received_at='2026-10-04T15:30:48Z'))

    async def forbidden_generate(*args):
        raise AssertionError('No provider calls permitted')
    monkeypatch.setattr(backend, 'generate', forbidden_generate)
    async def no_recovery(*args):
        pass
    monkeypatch.setattr(backend, '_recover_chat_loop', no_recovery)

    async def scenario():
        attempts = []
        reconnected = asyncio.Event()
        async def listen(*args, **kwargs):
            attempts.append(1)
            kwargs['state_callback']('CONNECTED')
            kwargs['state_callback']('CONNECTED')  # Heartbeats are not new transitions.
            if len(attempts) == 1:
                raise RuntimeError('QQ_CONNECTION_LOST_DURING_EXCHANGE')
            reconnected.set()
            await args[-1].wait()
        monkeypatch.setattr(qq, 'run_qq', listen)
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await asyncio.wait_for(reconnected.wait(), 7)
            runtime = app[backend._RUNTIME]
            assert len(attempts) == 2
            assert runtime['status']['qq'] == 'CONNECTED'
            assert 'qq' not in runtime['errors']
            expected = ({'qq': 'PERSONAL_CHAT_GENERATION_INTERRUPTED'}, {'qq': '2026-10-04T15:30:48Z'}) if failed_turn else ({}, {})
            assert backend.reply_failures(server, runtime) == expected
            states = [item['status'] for item in logs if item['event'] == 'personal_chat_transport_state']
            assert states == ['connecting', 'connected', 'reconnecting', 'connecting', 'connected']
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_generation_cancellation_is_durable_and_does_not_send_or_commit():
    from runtime.personal_chat.events import PersonalMessage
    from runtime.personal_chat.service import PersonalChatService
    from runtime.diagnostics.support_bundle import project_chat_task

    async def scenario():
        rows, snapshots, sends, commits = [], [], [], []
        started = asyncio.Event()
        async def generate(event, row):
            started.set()
            await asyncio.Event().wait()
        async def send(text):
            sends.append(text)
        async def commit(row):
            commits.append(row)
        service = PersonalChatService(rows, lambda: snapshots.append(json.loads(json.dumps(rows))),
                                      generate, commit, {'qq': ('10000', '20000')})
        task = asyncio.create_task(service.handle(PersonalMessage('qq', '10000', '20000', '1', 'synthetic'), send))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel('private cancellation detail must never be exported')
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rows[0]['delivery_status'] == 'FAILED'
        assert rows[0]['error_code'] == 'PERSONAL_CHAT_GENERATION_INTERRUPTED'
        assert snapshots[-1][0]['error_code'] == rows[0]['error_code']
        assert rows[0]['generation_attempts'] == 1
        assert not sends and not commits
        projected = project_chat_task(rows[0])
        assert projected['generation_interrupted'] is True
        assert 'private cancellation' not in json.dumps(projected)
        async def restored_generate(event, row):
            return 'synthetic reply'
        service.generate = restored_generate
        await service.handle(PersonalMessage('qq', '10000', '20000', '1', 'synthetic'), send)
        assert rows[0]['delivery_status'] == 'DELIVERED'
        assert rows[0]['generation_attempts'] == 2
        assert 'generation_interrupted' not in project_chat_task(rows[0])
        assert sends == ['synthetic reply'] and len(commits) == 1
    asyncio.run(scenario())


def test_real_onebot_reconnect_finishes_owned_turn_and_answers_new_message(tmp_path, monkeypatch):
    import local_server
    from aiohttp.test_utils import TestServer
    from runtime.personal_chat import backend, qq
    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    logs, generated, sends = [], [], []
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda event, **fields: logs.append(dict(event=event, **fields)))
    original_run = qq.run_qq
    async def quick_qq(*args, **kwargs):
        return await original_run(*args, **kwargs, merge_seconds=0)
    monkeypatch.setattr(qq, 'run_qq', quick_qq)
    async def commit(*args):
        pass
    monkeypatch.setattr(backend, 'commit', commit)

    async def scenario():
        started, new_reply, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def generate(_server, event, row):
            generated.append(event.message_id)
            if event.message_id == '1':
                started.set()
                await release.wait()
            return 'synthetic new reply'
        monkeypatch.setattr(backend, 'generate', generate)
        connections = []
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            login = await ws.receive_json()
            await ws.send_json(dict(echo=login['echo'], status='ok', retcode=0, data={'user_id': 10000}))
            connections.append(1)
            message_id = len(connections)
            if message_id == 1:
                await ws.send_json(dict(post_type='message', message_type='private', self_id=10000, user_id=20000,
                                        message_id=1, message=[{'type': 'text', 'data': {'text': 'synthetic'}}]))
                await asyncio.wait_for(started.wait(), 2)
                await ws.close(code=4001)
            else:
                release.set()
                for index in range(2):
                    outgoing = await ws.receive_json()
                    sends.append(outgoing['params']['message'])
                    await ws.send_json(dict(echo=outgoing['echo'], status='ok', retcode=0, data={'message_id': 300 + index}))
                    if index == 0:
                        await ws.send_json(dict(post_type='message', message_type='private', self_id=10000, user_id=20000,
                                                message_id=2, message=[{'type': 'text', 'data': {'text': 'synthetic'}}]))
                new_reply.set()
                async for _ in ws:
                    pass
            return ws
        onebot = web.Application()
        onebot.router.add_get('/', socket)
        async with TestServer(onebot) as transport:
            config = tmp_path / 'channels.json'
            config.write_text(json.dumps({'qq': {'url': str(transport.make_url('/')).replace('http:', 'ws:'),
                                                'account': '10000', 'owner': '20000'}}))
            monkeypatch.setenv('OLIVIA_PERSONAL_CHAT_CONFIG', str(config))
            app = web.Application()
            backend.install_personal_chat(app, server)
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                await asyncio.wait_for(new_reply.wait(), 9)
                async with asyncio.timeout(1):
                    while server.store.personal_chats[-1]['delivery_status'] != 'DELIVERED':
                        await asyncio.sleep(.01)
                old, new = server.store.personal_chats
                assert old['delivery_status'] == 'DELIVERED' and old['generation_attempts'] == 1
                assert new['delivery_status'] == 'DELIVERED' and new['generation_attempts'] == 1
                assert generated == ['1', '2'] and len(sends) == 2 and len(connections) == 2
                assert backend.reply_errors(server, app[backend._RUNTIME]) == {}
                closed = [row for row in logs if row['event'] == 'personal_chat_transport_closed']
                assert len(closed) == 1 and closed[0]['processing'] is True and closed[0]['close_code'] == 4001
                assert not any(row['event'] == 'personal_chat_exchange_cancelled' for row in logs)
            finally:
                await runner.cleanup()
    asyncio.run(scenario())
