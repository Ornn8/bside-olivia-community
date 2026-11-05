"""The history re-read rides out passing refusals, and the days of an imported history count.

A user who moves with weeks of letters must not be left in the first volume: the
assessed exchanges' calendar days join the days they talked. Synthetic data only.
"""
import asyncio
from dataclasses import replace
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService
from runtime.imports import historical_memory, relationship_batches
from runtime.imports.relationship_batches import RelationshipBatches, add_history_days, archive_exchanges
from runtime.private_world.commands import (
    ApplyHistoricalRelationshipEvidence, PrivateWorldActor, PrivateWorldCommandError,
    PrivateWorldCommandSource, command_from_record,
)
from runtime.private_world.ledger import LedgerEvent
from runtime.reply import jev_questions
from runtime.reply.jev_semantic_service import decide
from tests.imports.test_relationship_batches import rows

NOW = datetime(2026, 10, 29, 4, tzinfo=timezone.utc)
FIRST = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def dated_rows(count, *, per_day=2, start=FIRST):
    """`count` exchanges, `per_day` of them on each day from `start` on."""
    data = rows(count)
    for index, row in enumerate(data):
        row['occurred_at'] = (start + timedelta(days=index // per_day, minutes=index)).isoformat()
    return data


def days_of(count, *, per_day=2, start=FIRST):
    return tuple(sorted({(start + timedelta(days=index // per_day)).date().isoformat() for index in range(count)}))


class Steady:
    """A gateway judging every batch the same: a familiar, comfortable relationship with some trust."""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, **_kw):
        self.calls.append(json.loads(messages[1]['content']))
        return SimpleNamespace(text=json.dumps({'relationship_stage': 'unknown', 'familiarity': 40, 'trust': 40,
                                                'comfort': 40, 'closeness': 40, 'tension': 0,
                                                'evidence_indexes': [1]}))


def world(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
    return ledger, PrivateWorldCommandService(ledger)


def run(queue, service, ledger, gateway=None, **kw):
    asyncio.run(queue.run(gateway=gateway or Steady(), persona_policy='Synthetic policy.',
                          command_service=service, snapshot=ledger.snapshot, **kw))


@pytest.fixture
def pauses(monkeypatch):
    """Record every pause of the re-read instead of waiting it out."""
    recorded = []
    real = asyncio.sleep

    async def pause(seconds, *args, **kwargs):
        recorded.append(seconds)
        await real(0)

    monkeypatch.setattr(relationship_batches.asyncio, 'sleep', pause)
    return recorded


@pytest.fixture
def jev(monkeypatch):
    """A synthetic JEV endpoint that answers each request with the next scripted HTTP status, then succeeds."""
    script, calls = [], []

    class Native:
        def ask(self, state, questions):
            return {key: {'type': 'choice', 'choice': 'unknown' if key == 'stage' else 'yes' if key.startswith('e')
                          else '20' if '20' in question['criteria'] else 'up_small'}
                    for key, question in questions.items()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(packet)
            status = script.pop(0) if script else 200
            value = dict(decide(Native(), packet), backend='jev') if status == 200 else {}
            if status == 422:
                value = {'error': 'provider_http_422'}
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(value).encode('utf-8'))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = jev_questions.JevQuestionsPort(f'http://127.0.0.1:{server.server_port}/v1/companion/decide',
                                          token='synthetic')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    try:
        yield script, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_default_pauses_grow_over_five_tries():
    assert RelationshipBatches.retry_delays == (15, 40, 120, 300)


def test_jev_busy_refusal_is_tried_again_after_pauses_and_the_reread_completes(tmp_path, jev, pauses):
    script, calls = jev
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(rows(7)))
    script.extend([429, 503])
    run(queue, service, ledger)
    assert pauses == [15, 40]
    assert len(calls) == 4  # two refused, then both batches
    assert queue.status()['status'] == 'APPLIED' and queue.status()['processed'] == 7


def test_a_refusal_that_outlasts_five_tries_fails_and_is_taken_up_at_the_next_start(tmp_path, jev, pauses):
    script, calls = jev
    ledger, service = world(tmp_path)
    path = tmp_path / 'queue.sqlite3'
    queue = RelationshipBatches(path)
    queue.enqueue(archive_exchanges(rows(7)))
    script.extend([429] * 5)
    run(queue, service, ledger)
    assert pauses == [15, 40, 120, 300] and len(calls) == 5
    status = queue.status()
    assert status['status'] == 'FAILED' and status['error_code'] == 'PRIVATE_WORLD_HISTORY_JEV_HTTP_429'
    assert 'transient' not in status['failure_context'] and status['processed'] == 0
    queue = RelationshipBatches(path)
    run(queue, service, ledger)
    assert len(calls) == 5  # one run never loops on a failed batch
    assert queue.retry_transient() == 1
    run(queue, service, ledger)
    assert queue.status()['status'] == 'APPLIED' and len(calls) == 7


def test_a_plain_server_error_is_passing_but_a_rejection_is_not(tmp_path, jev, pauses):
    script, calls = jev
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(rows(5)))
    script.extend([500] * 5)
    run(queue, service, ledger)
    assert queue.status()['error_code'] == 'PRIVATE_WORLD_HISTORY_JEV_HTTP_ERROR' and len(pauses) == 4
    assert queue.retry_transient() == 1
    script.append(422)
    pauses.clear()
    run(queue, service, ledger)
    assert queue.status()['error_code'] == 'PRIVATE_WORLD_HISTORY_JEV_PROVIDER_HTTP_422'
    assert pauses == [] and len(calls) == 6
    assert queue.retry_transient() == 0  # waits for the explicit retry
    assert queue.status()['status'] == 'FAILED'


def test_other_failures_stay_failed_at_the_next_start(tmp_path):
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(rows(15)))
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='failed',error='PRIVATE_WORLD_HISTORY_LLM_REJECTED' WHERE id=1")
        db.execute("UPDATE batches SET state='failed',error='PRIVATE_WORLD_HISTORY_LLM_RATE_LIMITED' WHERE id=2")
        db.execute("UPDATE batches SET state='failed',error='PRIVATE_WORLD_HISTORY_JEV_TIMEOUT',"
                   "failure_context='not json' WHERE id=3")
    assert queue.retry_transient() == 2
    with queue.connect() as db:
        assert db.execute('SELECT id,state FROM batches ORDER BY id').fetchall() == [
            (1, 'failed'), (2, 'pending'), (3, 'pending')]


def test_the_reread_waits_while_a_live_reply_is_being_written(tmp_path, pauses):
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(rows(5)))
    replying = [True, True, True]
    order = []
    gateway = Steady()
    original = gateway.complete

    async def complete(messages, **kw):
        order.append(('call', len(replying)))
        return await original(messages, **kw)

    gateway.complete = complete
    run(queue, service, ledger, gateway, busy=lambda: bool(replying) and replying.pop())
    assert pauses == [2, 2, 2] and order == [('call', 0)]


def test_a_reply_that_never_reports_done_or_a_broken_source_does_not_stop_the_reread(tmp_path, pauses):
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(rows(10)))
    queue.live_wait_seconds = 6
    run(queue, service, ledger, busy=lambda: True)
    assert pauses == [2, 2, 2] * 2 and queue.status()['status'] == 'APPLIED'
    queue.enqueue(archive_exchanges(rows(15)))
    pauses.clear()

    def broken():
        raise RuntimeError('synthetic')

    run(queue, service, ledger, busy=broken)
    assert pauses == [] and queue.status()['processed'] == 15


def test_assessed_history_counts_its_days_and_leaves_the_first_volume(tmp_path, monkeypatch):
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    undated = rows(3)
    for index, row in enumerate(undated):
        row['metadata']['user_content'] = f'undated {index}'
    queue.enqueue(archive_exchanges(dated_rows(20) + undated))
    run(queue, service, ledger)
    state = ledger.snapshot()
    assert state.relationship_days == days_of(20)  # 10 days; an exchange without a time adds none
    assert (state.familiarity, state.comfort) == (40, 40) and state.relationship_stage == 'unknown'
    # 8+ days with familiarity and comfort at 35+: friends, the third volume.
    assert state.relationship_volume == 3 and state.volume_started_days <= len(state.relationship_days)
    from runtime.memory.effective_relationship import effective_relationship_stage
    assert effective_relationship_stage(state, {}) == 'friend'


def test_a_short_history_stays_where_its_days_put_it(tmp_path):
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(dated_rows(10, per_day=5)))
    run(queue, service, ledger)
    state = ledger.snapshot()
    assert state.relationship_days == days_of(10, per_day=5)
    assert state.relationship_volume == 1  # two days: not yet familiar


def test_history_days_join_live_days_without_removing_any(tmp_path):
    from runtime.relationship_growth import DEFAULTS, growth_updates
    ledger, service = world(tmp_path)
    state = ledger.snapshot()
    live = growth_updates(state, {'rel_disclosure': 'everyday'}, NOW, DEFAULTS)
    ledger.apply_once(LedgerEvent('live-day', 'live-day', 'synthetic_live', {}, NOW.isoformat()),
                      replace(state, version=state.version + 1, **live), expected_snapshot_version=state.version)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(dated_rows(20)))
    run(queue, service, ledger)
    assert ledger.snapshot().relationship_days == (*days_of(20), '2026-10-29')


def test_a_future_day_from_a_wrong_clock_is_not_counted():
    exchanges = archive_exchanges(dated_rows(4, per_day=1, start=NOW - timedelta(days=1)))
    assert historical_memory.exchange_days(exchanges, now=NOW) == ('2026-10-28', '2026-10-29')
    # Days are calendar days in UTC+8, as live days are.
    late = archive_exchanges([{**rows(1)[0], 'occurred_at': '2026-10-28T17:30:00+00:00'}])
    assert historical_memory.exchange_days(late, now=NOW) == ('2026-10-29',)


def test_history_days_travel_with_the_stored_command_and_old_records_stay_canonical():
    base = dict(command_id='history.evidence.v1.synthetic', idempotency_key='history.evidence.v1.synthetic',
                actor=PrivateWorldActor.MIGRATION, source=PrivateWorldCommandSource.IMPORT, occurred_at=NOW,
                reason='synthetic', evidence_refs=('history:synthetic',), familiarity=10, closeness=5, trust=5,
                comfort=5, tension=0)
    dated = ApplyHistoricalRelationshipEvidence(**base, history_days=('2026-09-01', '2026-09-02'))
    record = dated.to_dict()
    assert record['payload']['history_days'] == ['2026-09-01', '2026-09-02']
    assert command_from_record(json.loads(json.dumps(record))) == dated
    plain = ApplyHistoricalRelationshipEvidence(**base)
    assert 'history_days' not in plain.to_dict()['payload']
    assert command_from_record(plain.to_dict()) == plain
    for days in (('2026-09-02', '2026-09-01'), ('2026-09-01', '2026-09-01'), ('2026-9-1',), ('2026-02-30',),
                 '2026-09-01', (20260901,), ('2026-09-01', 5)):
        with pytest.raises(PrivateWorldCommandError):
            ApplyHistoricalRelationshipEvidence(**base, history_days=days)


def _imported_by_an_earlier_version(tmp_path, monkeypatch, count=20):
    """A finished re-read whose commands carried no days, as earlier versions wrote them."""
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'queue.sqlite3')
    queue.enqueue(archive_exchanges(dated_rows(count)))
    with monkeypatch.context() as patch:
        patch.setattr(historical_memory, 'exchange_days', lambda *_args, **_kwargs: ())
        run(queue, service, ledger)
    assert ledger.snapshot().relationship_days == () and ledger.snapshot().relationship_volume == 1
    return ledger, service, queue


def _events(ledger):
    with sqlite3.connect(ledger._database_path) as db:
        return db.execute('SELECT COUNT(*) FROM private_world_events').fetchone()[0]


def test_repair_counts_the_days_of_a_history_imported_before_and_runs_once(tmp_path, monkeypatch):
    ledger, service, queue = _imported_by_an_earlier_version(tmp_path, monkeypatch)
    before = ledger.snapshot()
    assert queue.merge_history_days(ledger, now=NOW) == 10
    after = ledger.snapshot()
    assert after.relationship_days == days_of(20) and after.relationship_volume == 3
    assert {k: getattr(after, k) for k in ('familiarity', 'trust', 'comfort', 'closeness', 'tension',
                                           'relationship_stage')} == {
        k: getattr(before, k) for k in ('familiarity', 'trust', 'comfort', 'closeness', 'tension',
                                        'relationship_stage')}
    events = _events(ledger)
    assert queue.merge_history_days(ledger, now=NOW) == 0 and _events(ledger) == events
    # Reopened (a restart): the state key holds, nothing is written again.
    assert RelationshipBatches(tmp_path / 'queue.sqlite3').merge_history_days(ledger, now=NOW) == 0
    assert _events(ledger) == events and ledger.snapshot() == after
    # A later write on the relationship keeps the merged days (they are the new projection base).
    run(queue, service, ledger)
    assert ledger.snapshot().relationship_days == days_of(20)


def test_repair_waits_for_the_whole_reread_and_never_lowers_anything(tmp_path, monkeypatch):
    ledger, service, queue = _imported_by_an_earlier_version(tmp_path, monkeypatch)
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='failed',error='PRIVATE_WORLD_HISTORY_LLM_REJECTED' WHERE id=4")
    assert queue.merge_history_days(ledger, now=NOW) == 0 and ledger.snapshot().relationship_days == ()
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='pending',error=NULL WHERE id=4")
    assert queue.merge_history_days(ledger, now=NOW) == 0
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='done' WHERE id=4")
    # A reached volume and a live day stay as they are.
    state = ledger.snapshot()
    ledger.apply_once(LedgerEvent('reached', 'reached', 'synthetic_live', {}, NOW.isoformat()),
                      replace(state, version=state.version + 1, relationship_volume=4, relationship_route='undecided',
                              relationship_days=('2026-10-29',), volume_started_days=1),
                      expected_snapshot_version=state.version)
    assert queue.merge_history_days(ledger, now=NOW) == 10
    after = ledger.snapshot()
    assert after.relationship_volume == 4 and after.relationship_days == (*days_of(20), '2026-10-29')


def test_repair_splits_a_long_history_into_bounded_records(tmp_path):
    ledger, _service = world(tmp_path)
    days = tuple((FIRST + timedelta(days=offset)).date().isoformat() for offset in range(900))
    assert add_history_days(ledger, days, at=NOW) == 900
    assert ledger.snapshot().relationship_days == days
    with sqlite3.connect(ledger._database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM private_world_events WHERE event_type='relationship_history_days'"
                          ).fetchone()[0] == 3
    assert add_history_days(ledger, days, at=NOW) == 0


def _wire_server(monkeypatch, server, tmp_path, queue, ledger, service, gateway):
    from runtime.memory.local_memory import LocalMemoryAdapter
    archive = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
    monkeypatch.setattr(server, '_history_relationship_queue', None)
    monkeypatch.setattr(server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(server, '_start_conversation_memory_initialization', lambda _loop: True)
    monkeypatch.setattr(server, '_history_relationship_task', None)
    monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
    monkeypatch.setattr(server, 'private_world_command_service', service)
    monkeypatch.setattr(server, 'private_world_port', ledger)
    monkeypatch.setattr(server, 'letters_adapter', SimpleNamespace(gateway=gateway, get_persona_policy=lambda: 'policy'))
    monkeypatch.setattr(server, '_llm_runtime_ready', lambda: True)
    return archive


def test_startup_takes_up_a_passing_failure_and_repairs_the_days(tmp_path, monkeypatch):
    import local_server as server
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'history-relationship.sqlite3')
    queue.enqueue(archive_exchanges(dated_rows(20)))
    with monkeypatch.context() as patch:
        patch.setattr(historical_memory, 'exchange_days', lambda *_args, **_kwargs: ())
        run(queue, service, ledger)
    # Batch 3 met a busy relay in an earlier version and stopped the re-read there.
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='failed',error='PRIVATE_WORLD_HISTORY_JEV_HTTP_429',assessment=NULL "
                   "WHERE id=3")
        db.execute("UPDATE batches SET state='pending',assessment=NULL WHERE id>3")
    gateway = Steady()
    archive = _wire_server(monkeypatch, server, tmp_path, queue, ledger, service, gateway)
    try:
        async def start():
            await server._start_conversation_memory(None)
            await server._history_relationship_task
        asyncio.run(start())
    finally:
        archive.close()
    status = server._history_relationship_queue.status()
    assert status['status'] == 'APPLIED' and status['processed'] == 20
    assert gateway.calls == []  # their commands were applied already: no model call again
    state = ledger.snapshot()
    assert state.relationship_days == days_of(20) and state.relationship_volume == 3


def test_startup_leaves_a_rejected_batch_failed(tmp_path, monkeypatch):
    import local_server as server
    ledger, service = world(tmp_path)
    queue = RelationshipBatches(tmp_path / 'history-relationship.sqlite3')
    queue.enqueue(archive_exchanges(dated_rows(10)))
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='failed',error='PRIVATE_WORLD_HISTORY_JEV_PROVIDER_HTTP_422' WHERE id=1")
    gateway = Steady()
    archive = _wire_server(monkeypatch, server, tmp_path, queue, ledger, service, gateway)
    try:
        async def start():
            await server._start_conversation_memory(None)
            await server._history_relationship_task
        asyncio.run(start())
    finally:
        archive.close()
    status = server._history_relationship_queue.status()
    assert status['status'] == 'FAILED' and status['processed'] == 0 and gateway.calls == []
    assert ledger.snapshot().relationship_days == ()
