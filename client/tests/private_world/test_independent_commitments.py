from datetime import datetime, timedelta, timezone

import pytest

from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 9, 9, 9, tzinfo=timezone.utc)


def _items(count):
    return [dict(id=f'promise-{i}', title=f'分享{i}', detail=f'我会给你分享第{i}篇文章。',
                 quote=f'我会给你分享第{i}篇文章。', status='planned', kind='shared', actor='linli')
            for i in range(count)]


def test_four_commitments_can_change_independently(tmp_path):
    store = DailyLifeStore(tmp_path/'world.sqlite')
    items = _items(4)
    store.record_exchange('reply:multi:1', '好。', ''.join(i['quote'] for i in items), items, occurred_at=NOW)
    cancellation = '第二篇不用分享了。'
    store.record_exchange('reply:cancel:1', cancellation, '好，其他照旧。',
        [{**items[1], 'actor': 'user', 'quote': cancellation, 'detail': cancellation, 'status': 'cancelled'}],
        occurred_at=NOW+timedelta(minutes=5))
    state = {p['id']: p for p in store.exchange_state()['shared']}
    assert state['promise-1']['status'] == 'cancelled'
    for i in (0, 2, 3):
        assert state[f'promise-{i}']['status'] == 'planned'
        assert state[f'promise-{i}']['quote'] == items[i]['quote']


def test_oversized_exchange_is_rejected_without_partial_writes(tmp_path):
    store = DailyLifeStore(tmp_path/'world.sqlite')
    items = _items(13)
    with pytest.raises(ValueError, match='DAILY_LIFE_UPDATES_INVALID'):
        store.record_exchange('reply:oversized:1', '好。', ''.join(i['quote'] for i in items), items, occurred_at=NOW)
    assert not store.has_source('reply:oversized:1')
    assert store.exchange_state()['shared'] == []
