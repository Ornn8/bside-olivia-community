"""Local collector tests; no account registration, network or provider calls."""
import asyncio
import json

import pytest

from runtime.diagnostics.reply_telemetry import Collector


def ready(tmp_path):
    c = Collector(tmp_path, version='2.4.5', clock=lambda: 1000)
    c.owner = 'local-collector-fixture'
    c.schema = 3
    c.decision_rejection_reason_supported = True
    return c


def row(reason):
    return dict(letter_id='local-row', created_at=1000, channel='qq',
                delivery_status='FAILED', generation_attempts=1,
                error_code='PERSONAL_CHAT_DECISION_INVALID', decision_rejection_reason=reason)


@pytest.mark.parametrize('reason,expected', [
    ('JSON_SYNTAX', 'JSON_SYNTAX'), ('CONTROL_BODY_INVALID', 'CONTROL_BODY_INVALID'),
    ('RECOVERY_ACTION_WITHOUT_PLAN', 'RECOVERY_ACTION_WITHOUT_PLAN'),
    ('SILENCE_NOT_AUTHORIZED', 'SILENCE_NOT_AUTHORIZED'), ('REPEATED_REPLY', 'REPEATED_REPLY'),
    ('private model text', 'UNKNOWN'), ({'private': 'text'}, 'UNKNOWN'), (None, 'UNKNOWN')])
def test_observe_only_fixed_reason_on_failed_reply(tmp_path, reason, expected):
    c = ready(tmp_path)
    current = row(reason)
    c.observe([current])
    events = c.batch(3)
    assert [e['decision_rejection_reason'] for e in events if e['stage'] == 'reply'] == [expected]
    assert all('decision_rejection_reason' not in e for e in events if e['stage'] != 'reply')
    assert 'private' not in json.dumps(events)
    c.ack([e['event_id'] for e in events])
    current.update(delivery_status='DELIVERED', error_code=None, generation_attempts=2)
    c.observe([current])
    assert all('decision_rejection_reason' not in e for e in c.batch(3))


def test_old_server_and_old_schemas_strip_reason(tmp_path):
    c = ready(tmp_path); c.observe([row('JSON_SYNTAX')])
    for schema in (1, 2):
        assert all('decision_rejection_reason' not in e for e in c.batch(schema))
    c.decision_rejection_reason_supported = False
    assert all('decision_rejection_reason' not in e for e in c.batch(3))
    c.decision_rejection_reason_supported = True
    assert any(e.get('decision_rejection_reason') == 'JSON_SYNTAX' for e in c.batch(3))


@pytest.mark.parametrize('supported', [True, False, 'private text'])
def test_capability_ack(tmp_path, supported):
    c = ready(tmp_path); c.observe([row('JSON_SYNTAX')])
    async def send(events):
        return dict(accepted=[e['event_id'] for e in events], supported_schemas=[1, 2, 3],
                    policy_version_supported=True, decision_rejection_reason_supported=supported)
    asyncio.run(c.upload(send))
    assert c.decision_rejection_reason_supported is (supported is True)
    assert (c.last_error is None) is (type(supported) is bool)
    if type(supported) is not bool:
        assert c.batch(), 'Malformed ACK must not discard pending observations'


def test_support_projection_keeps_decoder_reasons(tmp_path):
    from runtime.diagnostics.support_bundle import project_chat_task
    assert project_chat_task({'channel': 'qq', 'decision_rejection_reason': 'CONTROL_BODY_DEPTH'})[
        'decision_rejection_reason'] == 'CONTROL_BODY_DEPTH'
