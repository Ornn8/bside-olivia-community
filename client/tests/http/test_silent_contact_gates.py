"""Validated silent user boundaries reach the enabled shared contact path."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat.initiative_profile import profile_from_snapshot
from runtime.reply.proactive_runtime import contact_gates, contact_summary, live_state, packet
from runtime.reply.companion_proactive import JevProactivePort


NOW = datetime(2026, 10, 3, 11, tzinfo=timezone.utc)
PROFILE = profile_from_snapshot(SimpleNamespace(relationship_stage='committed', tension=0))


def quiet_user(**extra):
    return dict(letter_id='quiet-user', channel='qq', delivery_status='SKIPPED',
                letter_status='SKIPPED', created_at=NOW.timestamp()-100,
                companion_timing='no_reply', silence_reason='USER_REQUESTED_NO_REPLY',
                user_controls_applied=True, **extra)


@pytest.mark.parametrize('channel', ['qq', 'letter'])
def test_validated_silent_pause_blocks_shared_contact_without_becoming_contact(channel):
    rows = [quiet_user(initiative_preference='pause', pause_until=None,
                       letter_preference='pause', letter_until=None)]
    assert contact_gates(rows, now=NOW, profile=PROFILE, channel=channel)['paused']
    assert contact_summary(rows, NOW) == {'seconds_since_last_contact': None, 'unanswered_count': 0}


@pytest.mark.parametrize('extra', [
    {'user_controls_applied': False}, {'origin': 'proactive'}, {'superseded_by': 'canonical'},
])
def test_unvalidated_proactive_or_superseded_silence_has_no_control_authority(extra):
    row = quiet_user(initiative_preference='pause', pause_until=None,
                     letter_preference='pause', letter_until=None)
    row.update(extra)
    assert not contact_gates([row], now=NOW, profile=PROFILE, channel='letter')['paused']


def test_completed_letter_pause_and_expired_silent_pause_keep_existing_meaning():
    letter = dict(letter_id='letter', letter_status='COMPLETED', letter_preference='pause',
                  letter_until=None, created_at=NOW.timestamp()-1000)
    assert contact_gates([letter], now=NOW, profile=PROFILE, channel='letter')['paused']
    row = quiet_user(initiative_preference='pause', pause_until=NOW.timestamp()-1)
    assert not contact_gates([row], now=NOW, profile=PROFILE, channel='qq')['paused']


def test_explicit_wait_survives_proactive_or_superseded_rows_until_fresh_user_input():
    wait = quiet_user()
    wait.update(companion_timing='wait_user', silence_reason='USER_REQUESTED_WAIT',
                user_controls_applied=False)
    rows = [wait,
            dict(letter_id='proactive', channel='qq', origin='proactive', delivery_status='SKIPPED',
                 created_at=NOW.timestamp()-10),
            dict(letter_id='merged', channel='qq', delivery_status='SKIPPED', superseded_by='quiet-user',
                 created_at=NOW.timestamp()-5)]
    assert contact_gates(rows, now=NOW, profile=PROFILE, channel='letter')['blocked_reasons'] == ['user_waiting']
    rows.append(dict(letter_id='fresh', channel='qq', delivery_status='SKIPPED',
                     created_at=NOW.timestamp()-1))
    assert contact_gates(rows, now=NOW, profile=PROFILE, channel='letter')['blocked_reasons'] == []


def server_for(rows):
    snapshot = {'rhythm': {'phase': 'sleep', 'availability': 'rest'}, 'stale': False}
    return SimpleNamespace(
        private_world_port=SimpleNamespace(snapshot=lambda: SimpleNamespace(relationship_stage='committed', tension=0)),
        daily_life_runtime=SimpleNamespace(store=SimpleNamespace(snapshot=lambda now: snapshot)),
        store=SimpleNamespace(letters=[], personal_chats=rows))


def test_live_state_passes_due_appointment_exception_only_to_wait_and_world_gate():
    wait = quiet_user()
    wait.update(companion_timing='wait_user', silence_reason='USER_REQUESTED_WAIT')
    server = server_for([wait])
    assert live_state(server, channel='qq', now=NOW)['gates']['blocked_reasons'] == ['sleeping', 'user_waiting']
    assert live_state(server, channel='qq', now=NOW, appointment_due=True)['gates']['blocked_reasons'] == []
    server.store.personal_chats.append(dict(letter_id='recent-proactive', channel='qq', origin='proactive',
        delivery_status='DELIVERED', created_at=NOW.timestamp()-30))
    server.store.personal_chats.append(dict(letter_id='pending-proactive', channel='qq', origin='proactive',
        delivery_status='GENERATING', created_at=NOW.timestamp()-10))
    reasons = live_state(server, channel='qq', now=NOW, appointment_due=True)['gates']['blocked_reasons']
    assert reasons == ['pending_reply', 'cooldown', 'unanswered_limit']


def test_user_waiting_shared_packet_is_local_defer_without_vendor_request(monkeypatch):
    wait = quiet_user()
    wait.update(companion_timing='wait_user', silence_reason='USER_REQUESTED_WAIT')
    gates = contact_gates([wait], now=NOW, profile=PROFILE, channel='letter')
    assert gates['blocked_reasons'] == ['user_waiting']
    value = packet(channel='letter', now=NOW, profile=PROFILE, world={}, rhythm={},
                   emotion={}, messages=[], opportunities=[], rows=[wait],
                   available_media=['text'], hard_gates=gates)
    port = JevProactivePort()

    def forbidden_request(_input):
        pytest.fail('an explicit user wait must stop before any vendor request')

    monkeypatch.setattr(port, '_request', forbidden_request)
    result = asyncio.run(port.evaluate(value))
    assert result.error_code is None
    assert result.decision == dict(action='defer', opportunity_id=None,
                                   reason='hard_gate', intent=None, medium=None)
