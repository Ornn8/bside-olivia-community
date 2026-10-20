"""Shadow measurements ride with a finished reply's timings, only once the service accepts them."""
import asyncio

import pytest

from runtime.diagnostics import reply_telemetry
from runtime.diagnostics.reply_telemetry import Collector, shadow, take_shadow


def ready(tmp_path, supported=True):
    c = Collector(tmp_path, version='2.4.12', clock=lambda: 1000)
    c.owner = 'local-collector-fixture'
    c.schema = 3
    c.policy_version_supported = True
    c.shadow_timings_supported = supported
    return c


def delivered():
    return dict(letter_id='local-row', created_at=1000, channel='qq', delivery_status='DELIVERED',
                generation_attempts=1, reply_timing_seconds={'writer': 2.5})


def reply_timings(events):
    return [e.get('timing_seconds') for e in events if e['stage'] == 'reply' and e.get('timing_seconds')]


def test_pending_shadow_values_go_out_with_the_next_finished_reply(tmp_path):
    take_shadow()
    shadow('shadow_reply_repeat', 0.93)
    shadow('shadow_memory_overlap', 1.7)  # clamped as a count, never negative
    shadow('not_a_shadow_name', 0.5)
    shadow('shadow_reply_echo', float('nan'))
    c = ready(tmp_path)
    c.observe([delivered()])
    timings = reply_timings(c.batch(3))
    assert timings == [{'writer': 2.5, 'shadow_reply_repeat': 0.93, 'shadow_memory_overlap': 1.7}]
    assert take_shadow() == {}  # taken once


def test_old_service_never_receives_shadow_names(tmp_path):
    take_shadow()
    shadow('shadow_reply_repeat', 0.5)
    c = ready(tmp_path, supported=False)
    c.observe([delivered()])
    assert reply_timings(c.batch(3)) == [{'writer': 2.5}]
    c.shadow_timings_supported = True
    assert reply_timings(c.batch(3)) == [{'writer': 2.5, 'shadow_reply_repeat': 0.5}]


@pytest.mark.parametrize('supported', [True, False, 'text'])
def test_capability_ack(tmp_path, supported):
    c = ready(tmp_path, supported=False)
    c.observe([delivered()])

    async def send(events):
        return dict(accepted=[e['event_id'] for e in events], supported_schemas=[1, 2, 3],
                    policy_version_supported=True, decision_rejection_reason_supported=True,
                    shadow_timings_supported=supported)
    asyncio.run(c.upload(send))
    assert c.shadow_timings_supported is (supported is True)
    assert (c.last_error is None) is (type(supported) is bool)


def test_shadow_never_raises():
    reply_telemetry.shadow(None, object())
    reply_telemetry.shadow('shadow_reply_echo', 'x')
