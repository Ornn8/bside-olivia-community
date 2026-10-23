"""Her own contact that goes unsent reports which step stopped it (2026-10-23: about 430 of 530 daily attempts ended
SKIPPED with no reason, so the cause could not be ranked). Local collector only; no network or provider calls."""
from runtime.diagnostics.reply_telemetry import Collector
from runtime.diagnostics.telemetry_codes import ERROR_CODES

STEPS = ('NO_MEDIUM', 'DEFERRED', 'WRITER', 'TEXT', 'VOICE_FAILED', 'VOICE_ONLY')


def ready(tmp_path):
    c = Collector(tmp_path, version='2.4.20', clock=lambda: 1000)
    c.owner = 'local-collector-fixture'
    c.schema = 3
    return c


def skipped(letter_id, **extra):
    return dict(letter_id=letter_id, created_at=1000, channel='qq', origin='proactive', delivery_status='SKIPPED',
                generation_attempts=1, skip_reason='PROACTIVE_NO_REPLY', **extra)


def test_skipped_contact_reports_its_step(tmp_path):
    c = ready(tmp_path)
    c.observe([skipped('local-row', proactive_skip='PERSONAL_CHAT_PROACTIVE_SKIP_WRITER')])
    reply = [e for e in c.batch(3) if e['stage'] == 'reply']
    assert [(e['channel'], e['status'], e['error_code']) for e in reply] == [
        ('proactive', 'skipped', 'PERSONAL_CHAT_PROACTIVE_SKIP_WRITER')]


def test_missing_or_unknown_step_stays_none(tmp_path):
    c = ready(tmp_path)
    c.observe([skipped('a'), skipped('b', proactive_skip='free text')])
    assert {e['error_code'] for e in c.batch(3) if e['stage'] == 'reply'} == {'NONE'}


def test_a_failure_code_is_not_replaced(tmp_path):
    c = ready(tmp_path)
    c.observe([dict(skipped('a', proactive_skip='PERSONAL_CHAT_PROACTIVE_SKIP_TEXT'),
                    error_code='PERSONAL_CHAT_USER_PRIORITY')])
    assert {e['error_code'] for e in c.batch(3) if e['stage'] == 'reply'} == {'PERSONAL_CHAT_USER_PRIORITY'}


def test_every_step_is_in_the_wire_catalog():
    assert {'PERSONAL_CHAT_PROACTIVE_SKIP_' + step for step in STEPS} <= ERROR_CODES
