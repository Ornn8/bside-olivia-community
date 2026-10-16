"""Synthetic source-time budgets; expiry is attention, never factual resolution."""
from copy import deepcopy
from datetime import timedelta
import pytest

from runtime.private_world.jev_emotion import prepare
from tests.private_world.test_character_emotion import NOW, appraisal, commit
from runtime.private_world.character_emotion import CharacterEmotionStore


def concern(identity, age=0, **extra):
    return dict(id=identity, summary='Still pending.', status='open',
                occurred_at=(NOW - timedelta(hours=age)).isoformat(), **extra)


def packet(concerns, count=1):
    sources = [dict(source_id=f'new-{i}', text='A new message.', occurred_at=NOW.isoformat())
               for i in range(count)]
    return dict(persona='', assessment=dict(sources=sources, source_contexts={
        s['source_id']: dict(concerns=concerns, prior_appraisals=[]) for s in sources}))


def selected(plan):
    state = plan[0]
    return [state['shared_context'][ref['shared_context']]['id']
            for ref in state['sources']['s0']['concerns'].values()]


def test_only_recent_open_concerns_are_asked_without_mutating_history():
    values = [concern('old', 25), concern('boundary', 24), concern('recent', 1),
              {**concern('resolved'), 'status': 'resolve'}]
    request = packet(values)
    original = deepcopy(request)
    plan = prepare(request)
    assert selected(plan) == ['recent', 'boundary']
    assert len(plan[1]) == 10
    assert request == original


def test_four_newest_concerns_bound_each_source_and_use_stable_ties():
    values = [concern(f'c-{i}', i // 2, source_order=i) for i in range(24)]
    plan = prepare(packet(values, 8))
    assert selected(plan) == ['c-1', 'c-0', 'c-3', 'c-2']
    assert len(plan[1]) == 96  # 8 * (7 base + 1 lifecycle + 4 concerns)
    assert plan == prepare(packet(list(reversed(values)), 8))


def test_unknown_or_future_dates_are_omitted_without_resolving_them():
    values = [concern('future', -1), {**concern('missing'), 'occurred_at': None},
              {**concern('bad'), 'occurred_at': 'bad'},
              {**concern('naive'), 'occurred_at': '2026-09-27T02:00:00'}]
    original = deepcopy(values)
    assert selected(prepare(packet(values))) == []
    assert values == original
    assert all(c['status'] == 'open' for c in values)


def test_late_source_uses_its_own_time_not_packet_now():
    request = packet([concern('then-active', 23)])
    request['assessment']['as_of'] = (NOW + timedelta(days=10)).isoformat()
    assert selected(prepare(request)) == ['then-active']


@pytest.mark.parametrize('stamp', [None, 'bad', '2026-09-27T02:00:00'])
def test_unknown_source_time_does_not_infer_concern_recency(stamp):
    request = packet([concern('open')])
    request['assessment']['sources'][0]['occurred_at'] = stamp
    assert selected(prepare(request)) == []
    assert request['assessment']['source_contexts']['new-0']['concerns'][0]['status'] == 'open'


def test_view_expiry_keeps_history_and_allows_reopening(tmp_path):
    store = CharacterEmotionStore(tmp_path / 'synthetic.db')
    store.receive('old', 'Still pending.', occurred_at=NOW)
    commit(store, appraisal('old', 'Still pending.', concern=dict(
        id='emotion:old', action='open', summary='Still pending.')))
    before = store.assessment(['old'], now=NOW)
    assert len(store.view(now=NOW + timedelta(hours=24))['concerns']) == 1
    later = NOW + timedelta(hours=25)
    assert store.view(now=later)['concerns'] == []
    assert store.assessment(['old'], now=NOW) == before
    assert store.assessment(['old'], now=later)['concerns'][0]['status'] == 'open'
    store.receive('again', 'Still pending.', occurred_at=later)
    commit(store, appraisal('again', 'Still pending.', concern=dict(
        id='emotion:old', action='open', summary='Still pending.')))
    assert [c['id'] for c in store.view(now=later)['concerns']] == ['emotion:old']
