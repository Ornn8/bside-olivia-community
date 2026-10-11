"""Local opportunity projection only: no transport, provider or world writes."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.personal_chat.initiative import Initiative
from runtime.personal_chat.initiative_profile import profile_from_snapshot, unanswered_wait
from runtime.reply.proactive_letters import make_context, scan_pending, settings, write_json


NOW = datetime(2026, 9, 27, 4, 10, tzinfo=timezone.utc).timestamp()


def profile(tier='committed', **changes):
    state = {'relationship_stage': tier, 'familiarity': 0, 'trust': 0, 'comfort': 0, 'closeness': 0, 'tension': 0}
    state.update(changes)
    return profile_from_snapshot(SimpleNamespace(**state))


def world(now=NOW, *, stale=False, availability='open', source='day:current'):
    return {'current': {'source_id': source, 'occurred_at': datetime.fromtimestamp(now-60, timezone.utc).isoformat(),
                        'activity_kind': 'creative', 'activity': '练习构图', 'note': '在窗边练习构图。'},
            'stale': stale, 'world': {'schedule': {'current_class': None}},
            'rhythm': {'phase': 'free', 'availability': availability}, 'shared': []}


def kinds(context):
    return {item['kind']: item for item in context['candidates']}


@pytest.mark.parametrize('tier,kwargs,expected', [
    ('committed', {}, (300, 720, 72, 6, 1200, 14400)),
    ('close', {}, (600, 1500, 24, 2, 5400, 172800)),
    ('familiar', {'familiarity': 75, 'trust': 75}, (1800, 5400, 12, 2, 10800, 86400)),
    ('unknown', {}, (7200, 21600, 3, 1, 43200, 259200)),
    ('familiar', {}, (3600, 10800, 6, 2, 14400, 172800)),
])
def test_relationship_cadence_matches_authorized_policy(tier, kwargs, expected):
    actual = profile(tier, **kwargs)
    assert (actual.im_interval_min, actual.im_interval_max, actual.im_attempt_limit,
            actual.letter_daily_limit, actual.letter_followup_delay, actual.letter_silence_delay) == expected


def test_high_numeric_affinity_changes_behavior_not_relationship_identity():
    snapshot = SimpleNamespace(relationship_stage='familiar', familiarity=70, trust=80, comfort=80, closeness=90, tension=0)
    before = vars(snapshot).copy()
    result = profile_from_snapshot(snapshot)
    assert result.tier == 'committed'
    assert vars(snapshot) == before
    for key, value in [('familiarity', 69), ('trust', 79), ('comfort', 79), ('closeness', 89)]:
        assert profile_from_snapshot(SimpleNamespace(**{**before, key: value})).tier != 'committed'


def test_top_unanswered_starts_small_then_increases_and_caution_still_applies():
    top = profile()
    assert unanswered_wait(top, 2*86400, 1) == 900
    assert unanswered_wait(top, None, 2) == 1800
    waits = [unanswered_wait(top, None, count) for count in range(1, 25)]
    assert waits == sorted(waits) and waits[-1] == 4*3600
    tense = profile(tension=80)
    assert unanswered_wait(tense, None, 1) > unanswered_wait(top, None, 1)
    assert tense.im_interval_min > top.im_interval_min and tense.im_attempt_limit < top.im_attempt_limit


def test_live_profile_provider_rearms_once_without_backlog(monkeypatch):
    clock = [NOW]
    current = [profile('unknown')]
    monkeypatch.setattr('runtime.personal_chat.initiative.random.uniform', lambda low, high: low)
    policy = Initiative([], clock=lambda: clock[0], profile_provider=lambda: current[0])
    assert not policy.ready()
    policy.received(object(), None)
    assert policy.due == NOW + 2*3600
    current[0] = profile()
    clock[0] += 1
    assert not policy.ready() and policy.due == clock[0]+300
    clock[0] = policy.due
    assert policy.ready()
    assert policy.due == clock[0]


def test_life_share_uses_current_source_without_native_letter_and_is_once_per_source():
    state = world()
    original = deepcopy(state)
    first = make_context([], now=NOW, world=state, profile=profile())
    life = kinds(first)['life_share']
    assert life['source_id'] == 'day:current'
    assert life['not_before'] <= NOW < life['expires_at']
    assert state == original
    next_window = make_context([], now=NOW+1200, world=state, profile=profile())
    assert kinds(next_window)['life_share']['id'] == life['id']
    delivered = {'origin': 'proactive', 'letter_status': 'COMPLETED', 'is_read': 1,
                 'published_at': NOW, 'proactive_candidate_id': life['id']}
    assert 'life_share' not in kinds(make_context([delivered], now=NOW+1200, world=state, profile=profile()))
    state['current']['source_id'] = 'day:new'
    assert kinds(make_context([delivered], now=NOW+1200, world=state, profile=profile()))['life_share']['id'] != life['id']


@pytest.mark.parametrize('change', ['stale', 'future', 'missing'])
def test_old_future_or_missing_observation_is_not_a_fresh_life_share(change):
    state = world()
    if change == 'stale':
        state['stale'] = True
    elif change == 'future':
        state['current']['occurred_at'] = datetime.fromtimestamp(NOW+1, timezone.utc).isoformat()
    else:
        state['current'] = None
    context = make_context([], now=NOW, world=state, profile=profile())
    assert 'life_share' not in kinds(context)
    affection = kinds(context)['affection_checkin']
    assert affection['source_id'].startswith('initiative:affection:')


def test_affection_opportunity_is_current_window_not_fake_world_event(tmp_path):
    state = world(stale=True)
    state['current'] = None
    original = deepcopy(state)
    first = make_context([], now=NOW, world=state, profile=profile())
    opportunity = kinds(first)['affection_checkin']
    same = kinds(make_context([], now=NOW+1, world=state, profile=profile()))['affection_checkin']
    assert opportunity['id'] == same['id']
    write_json(tmp_path/'proactive/settings.json', {**settings({}), 'enabled': True})
    write_json(tmp_path/'proactive/context.json', first)
    assert scan_pending(tmp_path, now=NOW)['id'] == opportunity['id']
    assert scan_pending(tmp_path, now=opportunity['expires_at']) == {}
    after = make_context([], now=NOW+3*86400, world=state, profile=profile())
    assert len(after['candidates']) == 1
    assert kinds(after)['affection_checkin']['id'] != opportunity['id']
    assert kinds(after)['affection_checkin']['not_before'] <= NOW+3*86400 < kinds(after)['affection_checkin']['expires_at']
    assert state == original


def test_affection_close_window_and_explicit_live_profile_ignore_stale_row_tier():
    row = {'letter_id': 'im-user', 'channel': 'qq', 'letter_status': 'COMPLETED', 'delivery_status': 'DELIVERED',
           'content': '聊两句', 'created_at': NOW-600, 'initiative_tier': 'reserved'}
    context = make_context([row], now=NOW, world=world(stale=True), profile=profile('close'))
    opportunity = kinds(context)['affection_checkin']
    assert context['initiative_profile']['tier'] == 'close'
    assert opportunity['expires_at'] - opportunity['not_before'] == 90*60
    assert opportunity.get('previous_source_id') == 'reply:im-user:1'


@pytest.mark.parametrize('tier,extra,expected', [
    ('unknown', {}, set()), ('familiar', {}, set()),
    ('familiar', {'familiarity': 75, 'trust': 75}, {'life_share'}),
    ('close', {}, {'life_share', 'affection_checkin'}),
])
def test_new_opportunities_require_relationship_basis(tier, extra, expected):
    assert set(kinds(make_context([], now=NOW, world=world(), profile=profile(tier, **extra)))) == expected


@pytest.mark.parametrize('availability', ['busy', 'rest', None])
def test_current_window_opportunities_require_known_free_time(availability):
    assert not make_context([], now=NOW, world=world(availability=availability), profile=profile())['candidates']


def test_class_boundary_overrides_open_rhythm_for_new_opportunities():
    state = world()
    state['world']['schedule']['current_class'] = {'title': '课程'}
    assert not make_context([], now=NOW, world=state, profile=profile())['candidates']


def test_combined_im_rows_do_not_consume_letter_quota_or_unread_gate():
    rows = [{'letter_id': str(i), 'origin': 'proactive', 'channel': 'qq', 'reply_mode': 'future_im',
             'letter_status': 'COMPLETED', 'delivery_status': 'DELIVERED', 'created_at': NOW-1, 'is_read': 0}
            for i in range(8)]
    context = make_context(rows, now=NOW, world=world(), profile=profile())
    assert context['remaining'] == 6 and not context['unread']
    rows.append({'origin': 'proactive', 'letter_status': 'COMPLETED', 'published_at': NOW-1, 'is_read': 0})
    context = make_context(rows, now=NOW, world=world(), profile=profile())
    assert context['remaining'] == 5 and not context['unread']
    rows.append(dict(rows[-1]))
    context = make_context(rows, now=NOW, world=world(), profile=profile())
    assert context['remaining'] == 4 and context['unread']
    rows.append({'letter_status': 'PENDING', 'created_at': NOW})
    assert make_context(rows, now=NOW, world=world(), profile=profile())['blocked']


def test_affection_same_window_has_one_identity_even_if_life_or_anchor_changes():
    first = kinds(make_context([], now=NOW, world=world(), profile=profile()))['affection_checkin']
    later = world(source='day:new-observation')
    rows = [{'letter_id': 'new-im-input', 'channel': 'qq', 'content': '来了', 'delivery_status': 'DELIVERED',
             'created_at': NOW, 'reply_revision': 1}]
    second = kinds(make_context(rows, now=NOW+1, world=later, profile=profile()))['affection_checkin']
    assert second['id'] == first['id']
    rows.append({'channel': 'wechat', 'origin': 'proactive', 'delivery_status': 'DELIVERED',
                 'created_at': NOW, 'proactive_candidate_id': first['id']})
    context = make_context(rows, now=NOW+1, world=later, profile=profile())
    assert 'affection_checkin' not in kinds(context)
    assert context['remaining'] == 6 and not context['unread']


def test_unsuccessful_im_attempt_does_not_consume_opportunity_and_pending_input_still_blocks():
    first = kinds(make_context([], now=NOW, world=world(), profile=profile()))['affection_checkin']
    rows = [{'channel': 'qq', 'origin': 'proactive', 'delivery_status': 'SKIPPED',
             'proactive_candidate_id': first['id'], 'created_at': NOW}]
    assert kinds(make_context(rows, now=NOW, world=world(), profile=profile()))['affection_checkin']['id'] == first['id']
    rows.append({'channel': 'qq', 'origin': 'user', 'delivery_status': 'RECEIVED', 'created_at': NOW})
    assert make_context(rows, now=NOW, world=world(), profile=profile())['blocked']
