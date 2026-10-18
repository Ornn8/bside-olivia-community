from datetime import datetime, timezone

import pytest

from runtime.personal_chat.initiative_profile import profile_from_rows
from runtime.reply.proactive_letters import make_context
from runtime.reply.proactive_runtime import contact_gates

NOW = datetime(2026, 10, 11, 4, tzinfo=timezone.utc)


@pytest.mark.parametrize('channel,status,limit', [
    ('qq', 'RECEIVED', 1800), ('qq', 'GENERATING', 1800), ('qq', 'GENERATED', 1800),
    ('qq', 'MEDIA_PENDING', 1800), ('qq', 'SENDING', 1800), ('qq', 'DELIVERY_UNCONFIRMED', 1800),
    ('letter', 'PENDING', 7200), ('letter', 'PROCESSING', 7200), ('qq', 'FAILED', 1800),
])
def test_old_pending_rows_expire_in_both_letter_projections(channel, status, limit):
    row = dict(letter_id='synthetic', channel=channel, origin='user',
               created_at=NOW.timestamp()-limit+1,
               **{'delivery_status' if channel == 'qq' else 'letter_status': status})
    profile = profile_from_rows([])
    assert 'pending_reply' in contact_gates([row], now=NOW, profile=profile, channel='letter')['blocked_reasons']
    row['created_at'] -= 2
    assert not make_context([row], now=NOW.timestamp())['blocked']
    assert 'pending_reply' not in contact_gates([row], now=NOW, profile=profile, channel='letter')['blocked_reasons']


def test_two_recent_unread_letters_block_but_old_unread_does_not():
    now = NOW.timestamp()
    row = dict(origin='proactive', letter_status='COMPLETED', published_at=now-100, is_read=0)
    profile = profile_from_rows([{'initiative_tier': 'committed'}])
    assert not make_context([row], now=now, profile=profile)['unread']
    assert make_context([row, dict(row)], now=now, profile=profile)['unread']
    old = dict(row, published_at=now-48*3600-1)
    assert not make_context([row, old, old], now=now, profile=profile)['unread']
    assert make_context([row], now=now, profile=profile)['remaining'] == 5


def test_authorized_third_round_profile():
    expected = [('reserved', 3, 1, 43200, 259200), ('familiar', 6, 2, 14400, 172800),
                ('trusted', 12, 2, 10800, 86400)]
    for tier, attempts, letters, followup, silence in expected:
        profile = profile_from_rows([{'initiative_tier': tier}])
        assert (profile.im_attempt_limit, profile.letter_daily_limit, profile.letter_followup_delay,
                profile.letter_silence_delay) == (attempts, letters, followup, silence)


def test_old_qq_rows_no_longer_hold_initiative_but_active_work_does():
    from runtime.personal_chat.initiative import Initiative
    from runtime.personal_chat.events import PersonalMessage
    now = NOW.timestamp()
    rows = [dict(letter_id='old', channel='qq', origin='user', created_at=now-86400,
                 delivery_status='DELIVERY_UNCONFIRMED')]
    active = [False]
    policy = Initiative(rows, clock=lambda: now, interval=lambda: 0, active_provider=lambda: active[0])
    policy.received(PersonalMessage('qq', '100', '200', '1', 'hi'), None)
    assert policy.ready() and policy.free()
    active[0] = True
    assert not policy.ready() and not policy.free()
    active[0] = False
    rows.append(dict(letter_id='new', channel='qq', origin='user', created_at=now, delivery_status='RECEIVED'))
    assert not policy.ready() and not policy.free()
    assert rows[0]['delivery_status'] == 'DELIVERY_UNCONFIRMED'


def test_initiative_new_medium_reasons_and_hourly_throttle(tmp_path, monkeypatch):
    from runtime.diagnostics import reply_telemetry as telemetry
    collector = telemetry.Collector(tmp_path, clock=lambda: NOW.timestamp())
    collector.account('synthetic')
    monkeypatch.setattr(telemetry, '_collector', collector)
    observer = telemetry.InitiativeObserver()
    for schema in (1, 2):
        collector.schema = schema
        collector.emit('initiative', 'skipped', initiative=dict(tier='reserved', due_in_seconds=0,
            reason='disabled', catch_up=False, medium='letter'))
        assert not any(e['stage'] == 'initiative' for e in collector.batch(schema))
    collector.schema = 3
    for medium in ('letter', 'qq'):
        for reason in ('disabled', 'memory_not_ready', 'in_flight', 'unread', 'daily_limit', 'no_opportunity', 'deferred', 'ready'):
            fields = dict(tier='familiar', now=NOW.timestamp(), due=NOW.timestamp()+10**6, medium=medium)
            observer.record(reason, **fields)
            observer.record(reason, **fields)
    events = [e for e in collector.batch(3) if e['stage'] == 'initiative']
    assert len(events) == 16
    assert all(e['initiative']['due_in_seconds'] == 86400 and e['initiative']['catch_up'] is False for e in events)
    assert all(e['status'] == ('ok' if e['initiative']['reason'] == 'ready' else 'skipped') for e in events)
    observer.record('ready', tier='familiar', now=NOW.timestamp()+3600, due=0, medium='qq')
    assert len([e for e in collector.batch(3) if e['stage'] == 'initiative']) == 17
    for medium in ('private-content', [], None):
        collector.emit('initiative', 'skipped', initiative=dict(tier='reserved', due_in_seconds=0,
            reason='deferred', catch_up=False, medium=medium))
    assert len([e for e in collector.batch(3) if e['stage'] == 'initiative']) == 17
    assert not any(e['stage'] == 'initiative' for e in collector.batch(2))


def letter_server(monkeypatch, tmp_path):
    import threading
    from types import SimpleNamespace
    import local_server as server
    from runtime.diagnostics.reply_telemetry import InitiativeObserver
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    monkeypatch.setattr(server, 'store', SimpleNamespace(letters=[], personal_chats=[]))
    monkeypatch.setattr(server, 'time', SimpleNamespace(time=lambda: NOW.timestamp()))
    # scan_pending reads its own clock; keep it on NOW so the 7-day opportunity window does not expire.
    import runtime.reply.proactive_letters as proactive_letters
    monkeypatch.setattr(proactive_letters, 'time', SimpleNamespace(time=lambda: NOW.timestamp()))
    monkeypatch.setattr(server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(server, '_proactive_settings', lambda: {'enabled': True})
    monkeypatch.setattr(server, '_conversation_memory_ready_for_reply', lambda: True)
    monkeypatch.setattr(server, '_history_memory_admin_gate', threading.Lock())
    monkeypatch.setattr(server, '_personal_chat_service', None, raising=False)
    monkeypatch.setattr(server, 'reply_jobs', {})
    monkeypatch.setattr(server, 'daily_life_runtime', None)
    monkeypatch.setattr(server, 'diary_store', None)
    monkeypatch.setattr(server, '_current_life_rhythm', lambda: {'availability': 'open'})
    monkeypatch.setattr(server, '_letter_initiative_observer', InitiativeObserver())
    return server


def test_letter_gates_refresh_old_context_and_preserve_live_task_priority(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from runtime.reply.proactive_letters import write_json, read_json, scan_pending
    server = letter_server(monkeypatch, tmp_path)
    source = dict(letter_id='source', letter_status='COMPLETED', origin='user', content='synthetic',
                  created_at=NOW.timestamp()-13*3600)
    old = dict(letter_id='old', letter_status='PROCESSING', created_at=NOW.timestamp()-7201)
    server.store.letters[:] = [source, old]
    write_json(tmp_path / 'proactive/context.json', {'updated_at': NOW.timestamp()-10*86400, 'blocked': True})
    assert server._proactive_ready()
    assert read_json(tmp_path / 'proactive/context.json')['updated_at'] == NOW.timestamp()
    assert scan_pending(tmp_path, now=NOW.timestamp())['source_id'] == 'reply:source:1'
    assert server._active_undelivered_letter() is None
    server.reply_jobs['old'] = SimpleNamespace(done=lambda: False)
    assert server._active_undelivered_letter() is old
    assert server._proactive_gate_reason() == 'in_flight'
    server.reply_jobs.clear()
    old['created_at'] = NOW.timestamp()
    assert server._proactive_gate_reason() == 'in_flight'
    old['created_at'] -= 86400
    monkeypatch.setattr(server, '_personal_chat_service', SimpleNamespace(active_user_turns={'old-qq'}))
    assert server._proactive_gate_reason() == 'in_flight'


def test_letter_first_blocking_gate_and_cooldown_telemetry(monkeypatch, tmp_path):
    from runtime.reply.proactive_letters import write_json
    from runtime.diagnostics import reply_telemetry as telemetry
    import asyncio
    server = letter_server(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(telemetry, 'emit', lambda *args, **kwargs: events.append(kwargs['initiative']))
    monkeypatch.setattr(server, '_proactive_settings', lambda: {'enabled': False})
    monkeypatch.setattr(server, '_conversation_memory_ready_for_reply', lambda: False)
    asyncio.run(server._proactive_tick())
    assert events[-1]['reason'] == 'disabled' and events[-1]['medium'] == 'letter'
    monkeypatch.setattr(server, '_proactive_settings', lambda: {'enabled': True})
    asyncio.run(server._proactive_tick())
    assert events[-1]['reason'] == 'memory_not_ready'
    monkeypatch.setattr(server, '_conversation_memory_ready_for_reply', lambda: True)
    asyncio.run(server._proactive_tick())
    assert events[-1]['reason'] == 'no_opportunity'
    write_json(tmp_path / 'proactive/schedule.json', {'next_check_at': NOW.timestamp()+600})
    asyncio.run(server._proactive_tick())
    assert events[-1]['reason'] == 'not_due' and events[-1]['due_in_seconds'] == 600
    count = len(events)
    for _ in range(3):
        asyncio.run(server._proactive_tick())
    assert len(events) == count


def test_letter_legacy_model_defer_is_observed(monkeypatch, tmp_path):
    import asyncio
    from runtime.diagnostics import reply_telemetry as telemetry
    server = letter_server(monkeypatch, tmp_path)
    server.store.letters.append(dict(letter_id='source', letter_status='COMPLETED', content='synthetic',
                                    created_at=NOW.timestamp()-13*3600))
    events = []
    monkeypatch.setattr(telemetry, 'emit', lambda *args, **kwargs: events.append(kwargs['initiative']))
    async def prepare(*args, **kwargs):
        return {}
    async def complete(*args, **kwargs):
        return '{"decision":"defer","format":"text","title":"synthetic"}'
    monkeypatch.setattr(server, '_prepare_proactive_turn', prepare)
    monkeypatch.setattr(server, '_proactive_complete', complete)
    asyncio.run(server._proactive_tick())
    assert [e['reason'] for e in events] == ['ready', 'deferred']
    assert all(e['medium'] == 'letter' and not e['catch_up'] for e in events)


@pytest.mark.parametrize('decision', ['jev', 'legacy'])
def test_qq_defer_uses_same_initiative_observer(monkeypatch, decision):
    import asyncio
    from types import SimpleNamespace
    from runtime.personal_chat import backend
    from runtime.personal_chat.events import PersonalMessage
    from runtime.personal_chat.initiative import Initiative
    from runtime.diagnostics import reply_telemetry as telemetry
    monkeypatch.delenv('OLIVIA_JEV_DECISION_URL', raising=False)
    events, rows = [], []
    monkeypatch.setattr(telemetry, 'emit', lambda *a, **kw: events.append(kw['initiative']))
    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    policy = Initiative(rows, clock=lambda: NOW.timestamp(), interval=lambda: 0)
    policy.received(PersonalMessage('qq', '100', '200', 'one', 'hi'), None)
    async def proactive(event, send, eligible, followup=None):
        assert eligible()
        rows.append(dict(letter_id=event.exchange_id, origin='proactive', created_at=NOW.timestamp(),
            delivery_status='SKIPPED', **({'proactive_decision': {'decision': {'action': 'defer'}}}
                if decision == 'jev' else {'skip_reason': 'PROACTIVE_NO_REPLY'})))
    server = SimpleNamespace(store=SimpleNamespace(personal_chats=rows))
    asyncio.run(backend._proactive_contact(server,
        {'service': SimpleNamespace(proactive=proactive), 'status': {'qq': 'CONNECTED'}}, policy))
    assert [e['reason'] for e in events] == ['ready', 'deferred']
    assert all('medium' not in e for e in events)  # Original QQ wire shape remains valid.


def test_letter_unread_daily_and_admin_gate_reasons(monkeypatch, tmp_path):
    server = letter_server(monkeypatch, tmp_path)
    row = dict(origin='proactive', letter_status='COMPLETED', published_at=NOW.timestamp()-60,
               initiative_tier='committed', is_read=0)
    server.store.letters[:] = [dict(row), dict(row)]
    assert server._proactive_gate_reason() == 'unread'
    server._history_memory_admin_gate.acquire()
    try:
        assert server._proactive_gate_reason() == 'memory_not_ready'
    finally:
        server._history_memory_admin_gate.release()
    server.store.letters.extend(dict(row) for _ in range(4))
    assert server._proactive_gate_reason() == 'daily_limit'


def test_memory_readiness_accepts_healthy_degraded_and_explicit_disabled(monkeypatch):
    from types import SimpleNamespace
    import local_server as server
    bootstrap = {'status': 'available'}
    monkeypatch.setattr(server, 'letters_adapter', SimpleNamespace(memory_prompt_builder=SimpleNamespace(
        conversation_runtime_status=bootstrap)))
    runtime = SimpleNamespace(enabled=True, worker_running=True, status='available', reply_ready=True, reason_code=None)
    monkeypatch.setattr(server, 'conversation_memory_reply_readiness_status', lambda: runtime)
    assert server._conversation_memory_ready_for_reply()
    bootstrap['status'] = 'degraded'
    runtime.status, runtime.reply_ready = 'degraded', True
    assert server._conversation_memory_ready_for_reply()
    runtime.worker_running = False
    assert not server._conversation_memory_ready_for_reply()
    bootstrap.update(status='disabled', provider='none')
    assert server._conversation_memory_ready_for_reply()


@pytest.mark.parametrize('world', ['busy', 'bathing', 'class'])
def test_letter_gate_reports_her_world_reason_not_in_flight(monkeypatch, tmp_path, world):
    import runtime.reply.proactive_runtime as runtime
    from runtime.diagnostics.reply_telemetry import INITIATIVE_REASONS
    server = letter_server(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, 'enabled', lambda: True)
    monkeypatch.setattr(runtime, 'live_state', lambda *a, **k: {'gates': {'paused': False, 'blocked_reasons': [world]}})
    assert server._proactive_gate_reason() == world
    assert world in INITIATIVE_REASONS
