from datetime import datetime, timedelta, timezone
import json

import pytest

from runtime.private_world.daily_life import DailyLifeStore
from runtime.relationship_moments import plans

NOW = datetime(2026, 10, 17, 3, tzinfo=timezone.utc)


def update(status='planned'):
    return dict(id='wallpaper', title='合成壁纸', detail='合成壁纸约定', quote='合成壁纸约定',
                status=status, kind='shared', actor='user')


def record(store, source, status, at):
    store.record_exchange('reply:' + source + ':1', '合成壁纸约定', '好的', [update(status)], occurred_at=at)


def test_closed_commitment_cannot_reopen(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite')
    record(store, 'open', 'planned', NOW)
    record(store, 'done', 'completed', NOW + timedelta(minutes=1))
    record(store, 'keyword', 'planned', NOW + timedelta(minutes=2))
    state = store.exchange_state('合成壁纸', now=NOW + timedelta(minutes=3))
    assert state['shared'][0]['status'] == 'completed'
    assert plans(store, now=NOW + timedelta(minutes=3)) == []


def test_unknown_time_commitment_expires_without_claiming_completion(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite')
    record(store, 'old', 'planned', NOW - timedelta(days=13))
    assert plans(store, now=NOW) == []
    item = store.exchange_state('合成壁纸', now=NOW)['shared'][0]
    assert item['deadline_expired'] is True
    assert item['expired_by_age'] is True
    assert not item.get('deadline_at')
    assert item['status'] == 'planned'
    context = json.loads(store.reply_context('合成壁纸', now=NOW, max_chars=12000))
    assert not any(p.get('status') == 'planned' and not p.get('deadline_expired') for p in context['threads'])


@pytest.mark.parametrize('quote', ['算账猫咪？', '哎呀~小猫咪明天再说嘛~', '明天再说', '你', '我已经收到'])
def test_bad_addressing_is_rejected_even_in_legacy_rows(tmp_path, quote):
    store = DailyLifeStore(tmp_path / 'life.sqlite')
    store.record_addressing('reply:new:1', {'linli_calls_user': quote}, occurred_at=NOW)
    with store._db() as db:
        db.execute('INSERT OR IGNORE INTO life_addressing VALUES (?,?,?,?)',
                   ('reply:legacy:1', 'linli_calls_user', quote, NOW.isoformat()))
    assert store.addressing_profile(now=NOW) == {}


def test_short_addressing_remains(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite')
    store.record_addressing('reply:new:1', {'linli_calls_user': '小猫咪'}, occurred_at=NOW)
    assert store.addressing_profile(now=NOW)['linli_calls_user'][0]['quote'] == '小猫咪'


def test_legacy_reopen_is_closed_in_all_views_without_rewriting_journal(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite')
    record(store, 'done', 'completed', NOW)
    item = {**update(), 'source_id': 'reply:legacy:1', 'updated_at': (NOW + timedelta(minutes=1)).isoformat()}
    with store._db() as db:
        db.execute('INSERT INTO life_moments VALUES (?,?,?,?)',
                   (item['source_id'], item['updated_at'], 'exchange', json.dumps({'updates': [item]})))
        db.execute('INSERT OR REPLACE INTO life_projects VALUES (?,?)', (item['id'], json.dumps(item)))
    assert store.snapshot(NOW + timedelta(minutes=2))['shared'][0]['status'] == 'completed'
    assert plans(store, now=NOW + timedelta(minutes=2)) == []
