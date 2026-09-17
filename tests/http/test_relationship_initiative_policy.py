from types import SimpleNamespace
from datetime import datetime

from runtime.personal_chat.initiative import Initiative
from runtime.private_world.life_rhythm import LOCAL
from runtime.reply.initiative_policy import (
    initiative_context,
    interaction_rhythm,
    profile_from_snapshot,
)
from runtime.reply.proactive_letters import make_context


def snap(stage='unknown', familiarity=0, trust=0, comfort=0, closeness=0):
    return SimpleNamespace(
        relationship_stage=stage,
        familiarity=familiarity,
        trust=trust,
        comfort=comfort,
        closeness=closeness,
    )


def test_relationship_changes_what_counts_as_natural_initiative():
    assert profile_from_snapshot(snap()).band == 'reserved'
    trusted = profile_from_snapshot(snap(familiarity=70, trust=70, comfort=70))
    assert trusted.band == 'trusted'
    assert trusted.allow_low_stakes
    assert profile_from_snapshot(snap(stage='committed')).band == 'committed'


def test_unanswered_messages_quiet_down_then_reopen_in_close_relationship():
    now = [datetime(2026, 9, 17, 12, tzinfo=LOCAL).timestamp()]
    rows = [
        {'origin': 'proactive', 'delivery_status': 'DELIVERED', 'created_at': now[0] - 1800},
        {'origin': 'proactive', 'delivery_status': 'DELIVERED', 'created_at': now[0] - 900},
    ]
    policy = Initiative(
        rows,
        clock=lambda: now[0],
        interval=lambda: 0,
        snapshot=lambda: snap(stage='close', familiarity=80, trust=80, comfort=80, closeness=80),
    )
    policy.received(SimpleNamespace(), None)
    assert not policy.ready()
    now[0] += 4 * 3600 + 1
    assert policy.ready()


def test_reserved_relationship_stays_more_conservative_after_one_unanswered_message():
    now = [datetime(2026, 9, 17, 12, tzinfo=LOCAL).timestamp()]
    rows = [{'origin': 'proactive', 'delivery_status': 'DELIVERED', 'created_at': now[0] - 3600}]
    policy = Initiative(rows, clock=lambda: now[0], interval=lambda: 0, snapshot=lambda: snap())
    policy.received(SimpleNamespace(), None)
    assert not policy.ready()
    now[0] += 24 * 3600 + 1
    assert policy.ready()


def test_silence_is_relative_to_the_relationships_observed_chat_rhythm():
    now = datetime(2026, 9, 17, 20, tzinfo=LOCAL).timestamp()
    rows = [
        {'delivery_status': 'DELIVERED', 'created_at': now - 7 * 3600},
        {'delivery_status': 'DELIVERED', 'created_at': now - 6 * 3600},
        {'delivery_status': 'DELIVERED', 'created_at': now - 5 * 3600},
    ]
    rhythm = interaction_rhythm(rows, now)
    assert rhythm['typical_user_gap_seconds'] == 3600
    assert rhythm['silence_state'] == 'longer_than_usual'
    close = initiative_context(rows, snap(stage='close', familiarity=80, trust=80, comfort=80, closeness=80), now)
    reserved = initiative_context(rows, snap(), now)
    assert close['emotional_cue'] == 'missing_or_mild_concern'
    assert reserved['emotional_cue'] == 'neutral'


def test_proactive_letters_gain_low_stakes_checkin_only_after_relationship_allows_it():
    trusted_profile = profile_from_snapshot(snap(familiarity=70, trust=70, comfort=70)).public()
    rows = [{
        'letter_id': 'u1', 'letter_status': 'COMPLETED', 'content': '最近还好。',
        'reply_text': '嗯。', 'created_at': 1000, 'reply_revision': 1,
        'initiative_profile': trusted_profile,
    }]
    early = make_context(rows, now=2000)
    assert early['candidates'][0]['kind'] == 'correspondence_followup'
    later = make_context(rows, now=1000 + 19 * 3600)
    assert later['candidates'][0]['kind'] == 'relationship_checkin'
    assert later['candidates'][0]['relationship_initiative']['band'] == 'trusted'

    reserved = [{**rows[0], 'initiative_profile': profile_from_snapshot(snap()).public()}]
    assert make_context(reserved, now=1000 + 2 * 86400)['candidates'][0]['kind'] == 'correspondence_followup'


def test_relationship_metadata_does_not_change_existing_candidate_identity():
    base = [{
        'letter_id': 'u1', 'letter_status': 'COMPLETED', 'content': '明天面试。',
        'reply_text': '好。', 'created_at': 1000, 'reply_revision': 1,
    }]
    old_shape = make_context(base, now=2000)['candidates'][0]['id']
    annotated = [{**base[0], 'initiative_profile': profile_from_snapshot(snap()).public()}]
    assert make_context(annotated, now=2000)['candidates'][0]['id'] == old_shape
