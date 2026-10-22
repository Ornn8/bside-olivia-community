"""Her own piano / call suggestion: which suggestions are open, the frozen decision, and the writer note."""
from datetime import datetime, timedelta, timezone

import pytest

from runtime import media_offers
from tests.media.test_piano import CAPS

BEIJING = timezone(timedelta(hours=8))


def at(hour, minute=0):
    return datetime(2026, 10, 22, hour, minute, tzinfo=BEIJING).timestamp()


def test_open_kinds_need_the_feature_the_hour_and_quiet_days(tmp_path):
    both = dict(available={'piano': True, 'call': True}, last_used={})
    assert media_offers.open_kinds(tmp_path, at(20), **both) == ['piano', 'call']
    assert media_offers.open_kinds(tmp_path, at(7, 30), **both) == []           # too early for either
    assert media_offers.open_kinds(tmp_path, at(23, 45), **both) == ['piano']   # too late to call
    assert media_offers.open_kinds(tmp_path, at(20), available={'piano': False, 'call': True}, last_used={}) == ['call']
    used = dict(available={'piano': True, 'call': True}, last_used={'piano': at(20) - 2 * 86400})
    assert media_offers.open_kinds(tmp_path, at(20), **used) == ['call']
    assert media_offers.open_kinds(None, at(20), **both) == []


def test_a_suggestion_waits_days_and_a_no_waits_an_hour(tmp_path):
    both = dict(available={'piano': True, 'call': True}, last_used={})
    media_offers.note_asked(tmp_path, ['piano', 'call'], now=at(20))
    media_offers.note_offered(tmp_path, 'piano', now=at(20))
    assert media_offers.open_kinds(tmp_path, at(20, 30), **both) == []
    assert media_offers.open_kinds(tmp_path, at(21, 1), **both) == ['call']
    later = at(20) + media_offers.COOLDOWN_SECONDS + 60
    assert media_offers.open_kinds(tmp_path, later, **both) == ['piano', 'call']


@pytest.mark.parametrize('value', [{}, {'kinds': []}, {'kinds': ['song']}, {'kinds': ['call', 'call']},
                                   {'kinds': ['call'], 'extra': 1}, ['call']])
def test_invalid_offer_experience_is_refused(value):
    with pytest.raises(ValueError):
        media_offers.validate(value)


def test_the_decision_freezes_the_suggestion_and_round_trips():
    from runtime.reply.companion_decision import (FrozenCompanionTurn, FrozenCompanionDecision, JevDecisionPort,
                                                  CompanionDecisionError)
    from tests.persona.test_companion_decision import input_args, envelope
    offer = {'kinds': ['piano', 'call']}
    turn = FrozenCompanionTurn.create(**input_args(), piano_experience=CAPS, offer_experience=offer)
    assert JevDecisionPort('http://127.0.0.1:1/v1/companion/decide')._packet(turn)['offer_experience'] == offer
    record = FrozenCompanionDecision.from_response(turn, {**envelope(), 'piano_request': None, 'media_offer': 'call'}).record()
    assert record['media_offer'] == 'call' and record['offer_experience'] == offer
    assert FrozenCompanionDecision.from_record(FrozenCompanionTurn.from_record(record), record).record() == record
    for bad in ({**envelope(), 'piano_request': None}, {**envelope(), 'piano_request': None, 'media_offer': 'song'}):
        with pytest.raises(CompanionDecisionError):
            FrozenCompanionDecision.from_response(turn, bad)
    with pytest.raises(CompanionDecisionError):  # a piano suggestion needs the piano itself
        FrozenCompanionTurn.create(**input_args(), offer_experience={'kinds': ['piano']})
    plain = FrozenCompanionTurn.create(**input_args())
    assert 'offer_experience' not in JevDecisionPort('http://127.0.0.1:1/v1/companion/decide')._packet(plain)
    ignored = FrozenCompanionDecision.from_response(plain, {**envelope(), 'media_offer': 'call'}).record()
    assert 'media_offer' not in ignored


def test_instructions_ask_without_promising():
    for kind in media_offers.KINDS:
        note = media_offers.INSTRUCTION[kind]
        assert note.startswith('<media_offer>') and note.rstrip().endswith('</media_offer>')
        assert '价格' in note and '询问' in note or '问句' in note
