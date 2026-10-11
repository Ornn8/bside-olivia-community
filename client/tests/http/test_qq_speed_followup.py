import asyncio
from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.initiative import Initiative
from runtime.personal_chat.initiative_profile import profile_from_rows
from runtime.personal_chat.qq import run_qq
from runtime.private_world.life_rhythm import LOCAL
from tests.http.test_personal_chat_qq import login, event, TOKEN


def test_due_survives_receipts_restart_and_single_overdue_catchup(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 11, 12, tzinfo=LOCAL).timestamp()]
    monkeypatch.setattr('runtime.personal_chat.initiative.random.uniform', lambda a, b: a)
    path = tmp_path / 'initiative.sqlite3'
    message = PersonalMessage('qq', '100', '200', 'one', 'hello')
    policy = Initiative([], clock=lambda: now[0], interval=lambda: 7200, state_path=path)
    policy.received(message, None)
    due = policy.due
    now[0] += 100
    policy.received(message, None)
    assert policy.due == due
    restored = Initiative([], clock=lambda: now[0], interval=lambda: 7200, state_path=path)
    restored.restore(message, None)
    assert restored.due == due and not restored.catch_up
    now[0] = due + 1
    restored = Initiative([], clock=lambda: now[0], interval=lambda: 7200, state_path=path)
    restored.restore(message, None)
    assert restored.catch_up and restored.due == now[0] + 60
    now[0] += 61
    assert restored.ready()
    assert restored.reserve()
    # A crash after reservation cannot accumulate/replay the missed interval.
    restarted = Initiative([], clock=lambda: now[0], interval=lambda: 7200, state_path=path)
    restarted.restore(message, None)
    assert not restarted.ready() and not restarted.catch_up
    assert restarted.due == now[0] + 7200


def test_shallow_intervals_keep_attempt_limits():
    for tier, low, high, limit in [('reserved', 2, 6, 2), ('familiar', 1, 3, 4)]:
        p = profile_from_rows([{'initiative_tier': tier}])
        assert (p.im_interval_min, p.im_interval_max, p.im_attempt_limit) == (low*3600, high*3600, limit)


def test_typing_has_independent_ack_and_stops_on_new_input(monkeypatch):
    async def scenario():
        stop, release = asyncio.Event(), asyncio.Event()
        actions = []
        async def handler(message, send):
            if message.message_id != '1':
                return
            cancel = send.start_typing()
            try:
                await release.wait()
                assert await send('reply') == '301'
                stop.set()
            finally:
                cancel()
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event())
            for _ in range(2):
                typing = await ws.receive_json()
                actions.append(typing['action'])
                assert typing['action'] == 'set_input_status'
                assert typing['params'] == {'user_id': '200', 'event_type': 1}
                await ws.send_json(dict(echo=typing['echo'], status='ok', retcode=0, data=None))
            await ws.send_json(event(2))
            await asyncio.sleep(.08)
            release.set()
            reply = await ws.receive_json()
            assert reply['action'] == 'send_private_msg'
            # A typing ACK without a message_id is never a reply confirmation.
            await ws.send_json(dict(echo=typing['echo'], status='ok', retcode=0, data=None))
            await ws.send_json(dict(echo=reply['echo'], status='ok', retcode=0, data={'message_id': 301}))
            await stop.wait()
            await ws.close()
            return ws
        monkeypatch.setattr('runtime.personal_chat.qq._typing_interval', lambda: .02)
        app = web.Application()
        app.router.add_get('/', socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url('/')), TOKEN, '100', '200', handler,
                stop, merge_seconds=0), 3)
        assert actions == ['set_input_status', 'set_input_status']
    asyncio.run(scenario())


def test_initiative_telemetry_negotiation_filtering_and_downgrade(tmp_path):
    from runtime.diagnostics.reply_telemetry import Collector
    collector = Collector(tmp_path)
    collector.account('synthetic-account')
    fields = dict(tier='reserved', due_in_seconds=-1.5, reason='ready', catch_up=True)
    for schema in (1, 2):
        collector.schema = schema
        collector.emit('initiative', 'ok', initiative=fields)
        assert not any(e['stage'] == 'initiative' for e in collector.batch(schema))
    collector.schema = 3
    collector.emit('initiative', 'ok', initiative=dict(fields, secret='must-not-leave'))
    events = [e for e in collector.batch(3) if e['stage'] == 'initiative']
    assert len(events) == 1 and events[0]['initiative'] == fields
    for field, value in [('due_in_seconds', float('nan')), ('due_in_seconds', True),
                         ('due_in_seconds', 86401), ('due_in_seconds', 10**1000),
                         ('reason', 'private-content'), ('tier', []), ('catch_up', 1)]:
        collector.emit('initiative', 'ok', initiative=dict(fields, **{field: value}))
    assert len([e for e in collector.batch(3) if e['stage'] == 'initiative']) == 1
    assert not any(e['stage'] == 'initiative' or 'initiative' in e for e in collector.batch(2))
    assert not any(e['stage'] == 'initiative' for e in collector.batch(3))


def test_initiative_observation_throttle_and_gates(monkeypatch):
    now = [datetime(2026, 10, 11, 12, tzinfo=LOCAL).timestamp()]
    rows, observed = [], []
    monkeypatch.setattr('runtime.diagnostics.reply_telemetry.emit', lambda *a, **kw: observed.append(kw['initiative']))
    policy = Initiative(rows, clock=lambda: now[0], interval=lambda: 0)
    assert not policy.ready() and observed[-1]['reason'] == 'no_target'
    policy.received(PersonalMessage('qq', '100', '200', '1', 'hello'), None)
    assert policy.ready()
    for _ in range(100):
        policy.ready()
    assert len(observed) == 2
    now[0] += 3600
    policy.ready()
    assert len(observed) == 3
    cases = [
        ([dict(delivery_status='RECEIVED')], 'user_turn_open'),
        ([dict(delivery_status='DELIVERED', companion_timing='wait_user', silence_reason='USER_REQUESTED_WAIT')], 'wait_user'),
        ([dict(delivery_status='DELIVERED', initiative_preference='pause')], 'paused'),
        ([dict(origin='proactive', delivery_status='DELIVERED', created_at=now[0])], 'unanswered_wait'),
        ([dict(origin='proactive', delivery_status='DELIVERY_UNCONFIRMED', created_at=now[0])], 'sending_uncertain'),
        ([dict(origin='proactive', delivery_status='SKIPPED', created_at=now[0])] * 2, 'attempt_limit'),
    ]
    for values, reason in cases:
        rows[:] = values
        policy.catch_up = True
        assert not policy.ready() and observed[-1]['reason'] == reason and observed[-1]['catch_up']
    rows.clear()
    now[0] = datetime(2026, 10, 12, 7, tzinfo=LOCAL).timestamp()
    assert not policy.ready() and observed[-1]['reason'] == 'quiet_hours'
    policy.due = now[0] + 100
    assert not policy.ready() and observed[-1]['reason'] == 'not_due'


@pytest.mark.parametrize('proactive', [False, True])
@pytest.mark.parametrize('failure', [None, 'generation', 'send'])
def test_typing_service_lifecycle(proactive, failure):
    from runtime.personal_chat.service import PersonalChatService
    async def scenario():
        state = []
        async def generate(event, row):
            assert state == ['start']
            if failure == 'generation':
                raise RuntimeError('SYNTHETIC_FAILURE')
            return 'hello'
        async def send(text):
            assert state == ['start']
            if failure == 'send':
                raise RuntimeError('SYNTHETIC_FAILURE')
            return 'receipt'
        def start():
            state.append('start')
            return lambda: state.append('stop')
        send.start_typing = start
        async def commit(row):
            assert state == ['start', 'stop']
        service = PersonalChatService([], lambda: None, generate, commit, {'qq': ('100', '200')})
        message = PersonalMessage('qq', '100', '200', '1', '' if proactive else 'hello')
        try:
            await (service.proactive(message, send) if proactive else service.handle(message, send))
        except RuntimeError:
            assert failure
        assert state == ['start', 'stop'] and not service.typing
    asyncio.run(scenario())


@pytest.mark.parametrize('enabled', [False, True])
def test_typing_timeout_never_blocks_reply_and_shutdown_cleans_tasks(monkeypatch, enabled):
    async def scenario():
        stop = asyncio.Event()
        async def handler(message, send):
            send.start_typing()
            await asyncio.sleep(.06)
            assert await send('reply') == '302'
            stop.set()  # Intentionally do not cancel presence: transport must clean up.
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event())
            saw_typing = False
            while True:
                action = await ws.receive_json()
                if action['action'] == 'set_input_status':
                    saw_typing = True  # Deliberately never ACK, exercising short deadline.
                    continue
                assert action['action'] == 'send_private_msg' and saw_typing == enabled
                await ws.send_json(dict(echo=action['echo'], status='ok', retcode=0, data={'message_id': 302}))
                break
            await stop.wait()
            await ws.close()
            return ws
        monkeypatch.setenv('OLIVIA_QQ_TYPING_ENABLED', '1' if enabled else '0')
        monkeypatch.setattr('runtime.personal_chat.qq._TYPING_ACK_TIMEOUT', .01)
        monkeypatch.setattr('runtime.personal_chat.qq._typing_interval', lambda: .02)
        app = web.Application()
        app.router.add_get('/', socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url('/')), TOKEN, '100', '200', handler,
                stop, merge_seconds=0), 3)
        assert not any('typing_loop' in str(t.get_coro()) for t in asyncio.all_tasks())
    asyncio.run(scenario())


def test_due_is_per_binding_and_storage_failure_blocks_attempt(tmp_path):
    now = [datetime(2026, 10, 11, 12, tzinfo=LOCAL).timestamp()]
    path = tmp_path / 'due.sqlite3'
    policy = Initiative([], clock=lambda: now[0], interval=lambda: 7200, state_path=path)
    first = PersonalMessage('qq', '100', '200', '1', 'hi')
    second = PersonalMessage('qq', '100', '201', '2', 'hi')
    policy.received(first, None)
    due = policy.due
    now[0] += 100
    policy.received(second, None)
    assert policy.due == due + 100
    policy.received(first, None)
    assert policy.due == due
    broken = tmp_path / 'not-a-directory'
    broken.write_text('synthetic')
    policy = Initiative([], clock=lambda: now[0], interval=lambda: 0, state_path=broken / 'due.sqlite3')
    policy.received(first, None)  # A persistence problem cannot fail owner intake.
    assert not policy.ready() and not policy.reserve()
