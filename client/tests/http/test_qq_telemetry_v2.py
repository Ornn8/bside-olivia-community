import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import jsonschema
import pytest

from runtime.diagnostics.reply_telemetry import Collector


@pytest.fixture
def collector_factory():
    db = sqlite3.connect(':memory:')

    class MemoryCollector(Collector):
        @contextmanager
        def _db(self):
            with db:
                yield db

    yield lambda: MemoryCollector(Path(__file__).parent)
    db.close()


def test_negotiation_downgrade_and_exact_ack_preserve_events(collector_factory):
    async def scenario():
        c = collector_factory()
        c.account('synthetic')
        sent = []

        async def send(events):
            sent.append((c.schema, events))
            return {'accepted': [e['event_id'] for e in events], 'supported_schemas': [1, 2]}

        await c.upload(send)
        assert sent[0][0] == 1 and 'error_code' not in sent[0][1][0]
        c.emit('reply', 'failed', error='JEV_CONTEXT_BUDGET_EXCEEDED')
        await c.upload(send)
        assert sent[1][0] == 2
        assert sent[1][1][0]['error_code'] == 'JEV_CONTEXT_BUDGET_EXCEEDED'
        c.emit('transport', 'closed', channel='qq', transport={'close_code': 4001, 'in_flight': 'send'})
        before = c.batch(2)

        async def reject(events):
            return {'accepted': [], 'supported_schemas': [1, 2]}

        await c.upload(reject)
        assert c.schema == 1 and c.batch(2) == before

        async def old(events):
            assert events[0]['stage'] == 'binding' and events[0]['status'] == 'unknown'
            assert 'close_code' not in events[0]
            return {'accepted': [e['event_id'] for e in events]}

        await c.upload(old)
        assert c.schema == 1 and not c.batch()

    asyncio.run(scenario())


def test_v2_schema_privacy_and_account_scope(collector_factory):
    c = collector_factory()
    c.account('synthetic')
    c.emit('reply', 'failed', error='QQ_PRIVATE_SECRET')
    c.emit('transport', 'closed', channel='qq', transport={
        'close_code': True, 'transport_error': 'private exception', 'in_flight': 'private stage',
        'napcat_state': 'AWAITING_QQ_LOGIN', 'napcat_reason': 'probe', 'token': 'private-key'})
    batch = c.batch(2)
    assert batch[-2]['error_code'] == 'UNKNOWN'
    assert batch[-1]['close_code'] is None and batch[-1]['transport_error'] is None
    assert 'private' not in json.dumps(batch).lower()
    root = Path(__file__).parents[2] / 'contracts'
    for schema in (1, 2):
        name = 'reply_telemetry.schema.json' if schema == 1 else 'reply_telemetry_v2.schema.json'
        jsonschema.validate({'schema': schema, 'events': c.batch(schema)}, json.loads((root / name).read_text()))
    restored = collector_factory()
    assert restored.schema == 1 and restored.batch(2) == batch
    restored.schema = 2
    restored.account('new-account')
    assert restored.schema == 1 and len(restored.batch()) == 1


def test_code_catalog_is_compiled_from_fixed_chat_codes():
    from scripts.generate_reply_telemetry_codes import codes
    from runtime.diagnostics.telemetry_codes import ERROR_CODES
    assert set(codes()) == ERROR_CODES
    assert 'PERSONAL_CHAT_INPUT_TOO_LONG' in ERROR_CODES
    root = Path(__file__).parents[2] / 'contracts'
    assert set(json.loads((root / 'reply_telemetry_codes.json').read_text())) == ERROR_CODES
    schema = json.loads((root / 'reply_telemetry_v2.schema.json').read_text())
    assert set(schema['properties']['events']['items']['properties']['error_code']['enum']) == ERROR_CODES | {'NONE', 'UNKNOWN'}
