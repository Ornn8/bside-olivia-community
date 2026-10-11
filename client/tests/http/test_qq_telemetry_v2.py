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


@pytest.mark.parametrize('detail,expected', [
    ('required_source_missing', 'JEV_CONTEXT_SOURCE_MISSING'),
    ('required_original_missing', 'JEV_CONTEXT_ORIGINAL_MISSING'),
    ('required_source_invalid', 'JEV_CONTEXT_SOURCE_INVALID'),
    ('required_excerpt_incomplete', 'JEV_CONTEXT_EXCERPT_INCOMPLETE'),
    ('current_input_invalid', 'JEV_CONTEXT_CURRENT_INPUT_INVALID'),
    ('decision_input_invalid', 'JEV_CONTEXT_DECISION_INPUT_INVALID'),
    ('private arbitrary text', 'JEV_CONTEXT_UNAVAILABLE'),
])
def test_context_detail_becomes_finite_wire_code(collector_factory, detail, expected):
    c = collector_factory()
    c.account('synthetic')
    c.observe([dict(letter_id='synthetic', channel='qq', created_at=c.clock(),
        delivery_status='FAILED', generation_attempts=1, error_code='JEV_CONTEXT_UNAVAILABLE',
        generation_failure_context={'failure_stage': 'decision_context', 'failure_detail': detail})])
    event = next(e for e in c.batch(2) if e['stage'] == 'reply')
    assert event['error_code'] == expected
    assert detail not in json.dumps(event)


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


def test_daily_video_results_are_content_free_whitelisted_and_deduplicated(collector_factory):
    from runtime.diagnostics.reply_telemetry import DAILY_VIDEO_RESULTS
    c = collector_factory()
    c.account('synthetic')
    for result in (*sorted(DAILY_VIDEO_RESULTS), 'private content', {'private': 'content'}):
        c.emit('media', 'ok', daily_video_result=result)
    batch = c.batch(2)
    results = [e['daily_video_result'] for e in batch if 'daily_video_result' in e]
    assert set(results) == DAILY_VIDEO_RESULTS and results[-2:] == ['other', 'other']
    assert 'private' not in json.dumps(batch)
    root = Path(__file__).parents[2] / 'contracts'
    jsonschema.validate({'schema': 2, 'events': batch}, json.loads((root/'reply_telemetry_v2.schema.json').read_text()))
    assert all('daily_video_result' not in e for e in c.batch(1))
    c.ack([e['event_id'] for e in batch])
    row = dict(letter_id='private-turn', created_at=c.clock(), channel='qq', delivery_status='DELIVERED',
               share_video_event='private-source', share_video_result='source_unavailable',
               share_video_photo_fallback=True, image_delivery_status='DELIVERED')
    c.observe([row])
    c.observe([row])
    assert [e['daily_video_result'] for e in c.batch(2) if 'daily_video_result' in e] == ['source_unavailable', 'photo_fallback']
    assert 'private' not in json.dumps(c.batch(2))
