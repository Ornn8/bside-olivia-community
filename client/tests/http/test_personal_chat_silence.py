"""User-authorized quiet turns stay distinct from delivery and model silence."""
import asyncio
import json
from datetime import datetime

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.initiative import Initiative, letter_invitation_allowed
from runtime.personal_chat.service import PersonalChatService
from runtime.private_world.life_rhythm import LOCAL


@pytest.mark.parametrize('timing,reason', [
    ('wait_user', 'USER_REQUESTED_WAIT'),
    ('no_reply', 'USER_REQUESTED_NO_REPLY'),
])
def test_authorized_quiet_turn_records_reason_without_delivery_or_replay(timing, reason):
    async def scenario():
        rows, generated = [], []
        async def generate(event, row):
            generated.append(True)
            row.update(companion_timing=timing, silence_reason=reason)
            return None
        async def forbidden(*args):
            raise AssertionError('quiet turn must not send or commit')
        service = PersonalChatService(rows, lambda: None, generate, forbidden, {'qq': ('bot', 'owner')})
        event = PersonalMessage('qq', 'bot', 'owner', 'quiet', 'synthetic current request')
        await service.handle(event, forbidden)
        assert rows[0]['delivery_status'] == 'SKIPPED'
        assert rows[0]['skip_reason'] == reason
        assert 'user_controls_applied' not in rows[0]  # A quiet-only request need not change preferences.
        restarted = PersonalChatService(json.loads(json.dumps(rows)), lambda: None, generate, forbidden, service.bindings)
        await restarted.recover()
        await restarted.handle(event, forbidden)
        assert len(generated) == 1 and not restarted.pending('qq')
    asyncio.run(scenario())


@pytest.mark.parametrize('timing,reason', [
    ('wait_user', None), ('no_reply', None), ('defer', None),
    ('wait_user', 'USER_REQUESTED_NO_REPLY'), ('no_reply', 'USER_REQUESTED_WAIT'),
    ('wait_user', True),
])
def test_unapproved_model_silence_cannot_finish_a_user_request(timing, reason):
    async def scenario():
        rows = []
        async def generate(event, row):
            row.update(companion_timing=timing, user_controls_applied=True)
            if reason is not None:
                row['silence_reason'] = reason
            return None
        async def forbidden(*args):
            raise AssertionError('invalid empty reply must not deliver')
        service = PersonalChatService(rows, lambda: None, generate, forbidden, {'qq': ('bot', 'owner')})
        with pytest.raises(ValueError, match='PERSONAL_CHAT_REPLY_INVALID'):
            await service.handle(PersonalMessage('qq', 'bot', 'owner', 'question', 'synthetic complete question'), forbidden)
        assert rows[0]['delivery_status'] == 'FAILED'
        assert rows[0]['error_code'] == 'PERSONAL_CHAT_REPLY_INVALID'
        assert 'skip_reason' not in rows[0]
    asyncio.run(scenario())


def test_input_revision_clears_quiet_and_control_authority():
    async def scenario():
        rows, generated, sent = [], [], []
        later = PersonalMessage('qq', 'bot', 'owner', 'completion', 'synthetic complete question')
        async def generate(event, row):
            generated.append(True)
            if len(generated) == 1:
                row.update(companion_timing='wait_user', silence_reason='USER_REQUESTED_WAIT',
                    skip_reason='USER_REQUESTED_WAIT', user_controls_applied=True, initiative_preference='pause')
                await service.ingest(later)
                return None
            assert not any(key in row for key in ('companion_timing', 'silence_reason', 'skip_reason',
                                                  'user_controls_applied', 'initiative_preference'))
            return 'synthetic answer'
        async def send(text):
            sent.append(text)
            return 'synthetic-receipt'
        async def commit(row):
            pass
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        await service.handle(PersonalMessage('qq', 'bot', 'owner', 'fragment', 'synthetic first fragment'), send)
        assert sent == ['synthetic answer'] and rows[0]['input_revision'] == 1
        assert rows[1]['skip_reason'] == 'MERGED_RECEIPT'
        assert rows[1]['superseded_by'] == rows[0]['letter_id']
    asyncio.run(scenario())


def test_same_revision_retry_preserves_validated_pause_and_cancellation_after_restart():
    async def scenario():
        rows, generated = [], []
        async def generate(event, row):
            generated.append(True)
            if len(generated) == 1:
                row.update(user_controls_applied=True, initiative_preference='pause', pause_until=None,
                           letter_preference='pause', letter_until=None, followup_at=None,
                           followup_quote='synthetic cancellation', listening_preference='text_only')
                raise RuntimeError('PERSONAL_CHAT_TTS_UNAVAILABLE')
            assert row['user_controls_applied'] is True
            assert row['initiative_preference'] == row['letter_preference'] == 'pause'
            assert row['pause_until'] is None and row['letter_until'] is None and row['followup_at'] is None
            assert row['followup_quote'] == 'synthetic cancellation'
            assert row['listening_preference'] == 'text_only'
            return 'synthetic checked answer'
        async def send(text):
            return 'synthetic-receipt'
        async def commit(row):
            pass
        event = PersonalMessage('qq', 'bot', 'owner', 'control', 'synthetic control request')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        with pytest.raises(RuntimeError, match='PERSONAL_CHAT_TTS_UNAVAILABLE'):
            await service.handle(event, send)
        restarted = PersonalChatService(json.loads(json.dumps(rows)), lambda: None, generate, commit, service.bindings)
        await restarted.handle(event, send)
        assert restarted.rows[0]['delivery_status'] == 'DELIVERED'
        assert restarted.rows[0]['generation_attempts'] == 2
    asyncio.run(scenario())


def policy(rows, *, now=None):
    now = now or datetime(2026, 10, 3, 12, tzinfo=LOCAL).timestamp()
    value = Initiative(rows, clock=lambda: now, interval=lambda: 0)
    value.received(PersonalMessage('qq', 'bot', 'owner', 'current', 'synthetic input'), None)
    return value


def test_wait_blocks_initiative_until_a_new_canonical_user_input():
    rows = [dict(letter_id='wait', delivery_status='SKIPPED', companion_timing='wait_user',
                 silence_reason='USER_REQUESTED_WAIT', created_at=1)]
    value = policy(rows)
    assert not value.ready()
    rows.append(dict(letter_id='merged', delivery_status='SKIPPED', superseded_by='wait', created_at=2))
    assert not value.ready()  # A merged receipt cannot clear the wait.
    rows.append(dict(letter_id='fresh', delivery_status='SKIPPED', companion_timing='no_reply',
                     silence_reason='USER_REQUESTED_NO_REPLY', created_at=3))
    assert value.ready()  # A new user input ends the previous wait, without claiming delivery.


def test_wait_still_honors_a_validated_user_appointment_and_its_cancellation():
    now = [datetime(2026, 10, 3, 12, tzinfo=LOCAL).timestamp()]
    rows = [dict(letter_id='wait', delivery_status='SKIPPED', companion_timing='wait_user',
                 silence_reason='USER_REQUESTED_WAIT', user_controls_applied=True,
                 followup_at=now[0] + 60, created_at=now[0])]
    value = Initiative(rows, clock=lambda: now[0], interval=lambda: 0)
    value.received(PersonalMessage('qq', 'bot', 'owner', 'wait', 'synthetic wait and appointment'), None)
    assert not value.ready()
    now[0] += 60
    assert value.ready()  # User-requested appointment overrides ordinary waiting at its due time.
    rows.append(dict(letter_id='cancel', delivery_status='SKIPPED', companion_timing='wait_user',
                     silence_reason='USER_REQUESTED_WAIT', user_controls_applied=True,
                     followup_at=None, created_at=now[0]))
    assert value.pending_followup() is None
    assert not value.ready()


@pytest.mark.parametrize('status', ['SKIPPED', 'FAILED'])
def test_independently_validated_user_controls_apply_without_delivery(status):
    now = datetime(2026, 10, 3, 12, tzinfo=LOCAL).timestamp()
    rows = [dict(letter_id='old', delivery_status='DELIVERED', followup_at=now - 1, created_at=now - 60),
            dict(letter_id='control', delivery_status=status, user_controls_applied=True,
                 initiative_preference='pause', letter_preference='pause', followup_at=None, created_at=now)]
    value = policy(rows, now=now)
    assert value.pending_followup() is None
    assert not letter_invitation_allowed(rows, [], now)
    assert not value.ready()
    assert rows[-1]['delivery_status'] == status


@pytest.mark.parametrize('marker', [False, 1, 'true', {'value': True}])
def test_control_marker_must_be_literal_true(marker):
    now = datetime(2026, 10, 3, 12, tzinfo=LOCAL).timestamp()
    rows = [dict(letter_id='old', delivery_status='DELIVERED', followup_at=now - 1, created_at=now - 60),
            dict(letter_id='untrusted', delivery_status='SKIPPED', user_controls_applied=marker,
                 initiative_preference='pause', letter_preference='pause', followup_at=None, created_at=now)]
    value = policy(rows, now=now)
    assert value.pending_followup() is rows[0]
    assert letter_invitation_allowed(rows, [], now)
    assert value.ready()


@pytest.mark.parametrize('untrusted', [{'superseded_by': 'canonical'}, {'origin': 'proactive'}])
def test_superseded_or_proactive_rows_cannot_supply_unsent_user_controls(untrusted):
    now = datetime(2026, 10, 3, 12, tzinfo=LOCAL).timestamp()
    rows = [dict(letter_id='old', delivery_status='DELIVERED', followup_at=now - 1, created_at=now - 60),
            dict(letter_id='untrusted', delivery_status='SKIPPED', user_controls_applied=True,
                 initiative_preference='pause', letter_preference='pause', followup_at=None, created_at=now, **untrusted)]
    value = policy(rows, now=now)
    assert value.pending_followup() is rows[0]
    assert letter_invitation_allowed(rows, [], now)
    assert value.ready()


@pytest.mark.parametrize('untrusted', [{'superseded_by': 'canonical'}, {'origin': 'proactive'}])
def test_delivered_proactive_or_superseded_preferences_cannot_override_current_user_controls(untrusted):
    now = datetime(2026, 10, 3, 12, tzinfo=LOCAL).timestamp()
    rows = [dict(letter_id='current-user', delivery_status='DELIVERED', created_at=now - 60,
                 initiative_preference='pause', letter_preference='pause', followup_at=None),
            dict(letter_id='untrusted', delivery_status='DELIVERED', created_at=now,
                 initiative_preference='open', letter_preference='open', followup_at=now - 1, **untrusted)]
    value = policy(rows, now=now)
    assert value.pending_followup() is None
    assert not letter_invitation_allowed(rows, [], now)
    assert not value.ready()


def test_validated_unsent_open_preference_does_not_reset_unanswered_contact_history():
    now = datetime(2026, 10, 3, 12, tzinfo=LOCAL).timestamp()
    rows = [dict(letter_id='owner', delivery_status='DELIVERED', created_at=now - 120,
                 initiative_preference='pause'),
            dict(letter_id='contact', origin='proactive', delivery_status='DELIVERED', created_at=now - 60),
            dict(letter_id='control', delivery_status='SKIPPED', user_controls_applied=True,
                 initiative_preference='open', created_at=now)]
    assert not policy(rows, now=now).ready()  # The last delivered contact still awaits a reply.
