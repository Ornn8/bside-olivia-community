import json
from datetime import datetime, timedelta, timezone

from runtime.private_world.daily_life import DailyLifeStore

NOW = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)


def test_self_reports_do_not_replace_published_world_even_after_legacy_restart(tmp_path):
    path = tmp_path / 'life.sqlite3'
    store = DailyLifeStore(path)
    store.publish_day('day:meal', dict(location='餐桌', activity='吃饭', note='青菜腐竹配饭'), [], occurred_at=NOW)
    store.record_exchange('reply:meal:1', '吃什么', '刚吃完小馄饨', [],
                          occurred_at=NOW + timedelta(minutes=1), current_quote='刚吃完小馄饨')
    # A preceding application version may have promoted this quote already.
    with store._db() as db:
        payload = json.loads(db.execute("SELECT payload FROM life_moments WHERE source_id='reply:meal:1'").fetchone()[0])
        db.execute('INSERT OR REPLACE INTO life_current VALUES (1,?)', (json.dumps(payload['current']),))
    reopened = DailyLifeStore(path)
    state = reopened.snapshot(NOW + timedelta(minutes=2))
    assert state['current']['source_id'] == 'day:meal'
    context = json.loads(reopened.reply_context('到底吃什么', now=NOW + timedelta(minutes=2)))
    assert context['current'] is None  # old observation is not a live claim
    assert context['last_observation']['source_id'] == 'day:meal'
    assert context['previous_observations'][0]['evidence_kind'] == 'character_statement'
    assert context['previous_observations'][0]['note'] == '刚吃完小馄饨'


def test_statements_alone_cannot_create_current_world(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.record_exchange('reply:meal:1', '吃什么', '刚吃完小馄饨', [], occurred_at=NOW, current_quote='刚吃完小馄饨')
    assert store.snapshot(NOW)['current'] is None
    assert json.loads(store.reply_context('吃什么', now=NOW))['current'] is None
    assert store.has_source('reply:meal:1')


def test_as_of_state_replays_projects_instead_of_reading_future_pointer(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    for index, (status, quote) in enumerate((('planned', '我准备寄书'), ('cancelled', '我不寄书了'))):
        store.record_exchange(f'reply:book:{index+1}', quote, '好', [dict(id='book', title='寄书',
            detail=quote, status=status, kind='shared', actor='user', quote=quote)],
            occurred_at=NOW + timedelta(hours=index))
    assert store.snapshot(NOW)['shared'][0]['status'] == 'planned'
    assert store.exchange_state('寄书', now=NOW)['shared'][0]['status'] == 'planned'
    context = json.loads(store.reply_context('寄书', now=NOW))
    assert context['threads'][0]['status'] == 'planned'
    assert context['threads'][0]['evidence_kind'] == 'user_statement'
    assert '我不寄书了' not in json.dumps(context, ensure_ascii=False)


def test_context_reads_one_snapshot_without_mutating_legacy_data(tmp_path, monkeypatch):
    from contextlib import contextmanager
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    store.publish_day('day:fixture', dict(location='琴房', activity='整理', note='整理曲谱'), [], occurred_at=NOW)
    store.record_exchange('reply:fixture', '好', '我正在吃饭', [], occurred_at=NOW, current_quote='我正在吃饭')
    with store._db() as db:
        before = list(db.iterdump())
    original = store._db
    reads = []
    @contextmanager
    def monitored():
        with original() as db:
            reads.append(db)
            yield db
    monkeypatch.setattr(store, '_db', monitored)
    assert store.reply_context('你在做什么', now=NOW)
    assert len(reads) == 1
    with original() as db:
        assert list(db.iterdump()) == before
