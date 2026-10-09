from datetime import datetime, timedelta, timezone

from runtime.memory.source_retrieval import SourceRetrieval


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def fill(index, count=80):
    for i in range(count):
        index.put('owner', f'reply:{i}:1', f'合成来信{i}', '合成回信', NOW + timedelta(seconds=i))
    pending = index.vectors_missing('owner', 'model-a', limit=count)
    index.put_vectors('owner', 'model-a', [(s, d, [1., 0.]) for s, d, _ in pending])


def test_ready_index_does_not_rehash_history_and_only_changed_exchange_is_pending(tmp_path, monkeypatch):
    path = tmp_path / 'source.sqlite3'
    index = SourceRetrieval(path)
    fill(index)
    # A fresh object/process must reuse durable progress, not an in-memory singleton.
    index = SourceRetrieval(path)
    original = index._exchange_text
    calls = []
    def counted(rows):
        calls.append(rows)
        return original(rows)
    monkeypatch.setattr(SourceRetrieval, '_exchange_text', staticmethod(counted))
    assert index.vectors_missing('owner', 'model-a') == []
    assert calls == []
    index.put('owner', 'reply:4:1', '改过的合成来信', '合成回信', NOW + timedelta(seconds=4))
    calls.clear()
    assert [s for s, _, _ in index.vectors_missing('owner', 'model-a')] == ['reply:4:1']
    assert len(calls) == 1
    assert 'reply:4:1' not in [s for s, _, _ in index.nearest_sources('owner', 'model-a', [1., 0.], limit=100)]


def test_late_embedding_cannot_restore_changed_or_forgotten_source(tmp_path):
    index = SourceRetrieval(tmp_path / 'source.sqlite3')
    index.put('owner', 'reply:1:1', '原文', '回复', NOW)
    stale = index.vectors_missing('owner', 'model-a')
    index.put('owner', 'reply:1:1', '更正原文', '回复', NOW)
    index.put_vectors('owner', 'model-a', [(s, d, [1., 0.]) for s, d, _ in stale])
    assert index.nearest_sources('owner', 'model-a', [1., 0.]) == []
    pending = index.vectors_missing('owner', 'model-a')
    assert len(pending) == 1 and '更正原文' in pending[0][2]
    index.forget('owner', 'reply:1:1')
    index.put_vectors('owner', 'model-a', [(s, d, [1., 0.]) for s, d, _ in pending])
    assert index.vectors_missing('owner', 'model-a') == []
    assert index.vector_coverage('owner', 'model-a') == 0


def test_pending_queue_is_bounded_and_separate_for_each_owner_and_model(tmp_path):
    index = SourceRetrieval(tmp_path / 'source.sqlite3')
    fill(index, 12)
    index.put('other', 'reply:11:1', '另一个用户', '他的回复', NOW)
    assert len(index.vectors_missing('owner', 'model-b', limit=3)) == 3
    assert index.vectors_missing('owner', 'model-a') == []
    assert index.vectors_missing('owner', 'model-b', limit=0) == []
    assert len(index.vectors_missing('other', 'model-a')) == 1
    index.put_received('owner', 'received-user:new', '只收到用户原话', NOW)
    for model in ('model-a', 'model-b'):
        assert 'received-user:new' in [s for s, _, _ in index.vectors_missing('owner', model)]
    index.retract_received('owner', ['received-user:new'])
    assert 'received-user:new' not in [s for s, _, _ in index.vectors_missing('owner', 'model-b')]


def test_chunked_nearest_keeps_global_topk_across_batches(tmp_path):
    from contextlib import closing
    from array import array
    index = SourceRetrieval(tmp_path / 'source.sqlite3')
    with closing(index.connect()) as db, db:
        for i in range(1100):
            source = f'reply:{i:04}:1'
            db.execute('INSERT INTO originals VALUES (?,?,?,?,?)', ('u', source, 'user', NOW.isoformat(), '合成原文'))
            db.execute('INSERT INTO source_vectors VALUES (?,?,?,?,?)', ('u', source, 'm', 'legacy', array('f', [i / 1100, 0]).tobytes()))
    hits = index.nearest_sources('u', 'm', [1., 0.], limit=3, exclude_source_ids=['reply:1099:1'])
    assert [s for s, _, _ in hits] == ['reply:1098:1', 'reply:1097:1', 'reply:1096:1']


def test_first_upgrade_keeps_valid_vectors_and_only_queues_stale_legacy_rows(tmp_path):
    from contextlib import closing
    index = SourceRetrieval(tmp_path / 'source.sqlite3')
    fill(index, 4)
    with closing(index.connect()) as db, db:
        db.execute('DELETE FROM vector_models')
        db.execute("UPDATE originals SET text='升级前已改动' WHERE source='reply:1:1' AND actor='user'")
    assert [s for s, _, _ in index.vectors_missing('owner', 'model-a')] == ['reply:1:1']
    assert len(index.nearest_sources('owner', 'model-a', [1., 0.])) == 3
    index.put('owner', 'reply:5:1', '新增', '答复', NOW)
    assert {s for s, _, _ in index.vectors_missing('owner', 'model-a')} == {'reply:1:1', 'reply:5:1'}


def test_timestamp_only_update_reuses_the_same_paid_embedding(tmp_path):
    index = SourceRetrieval(tmp_path / 'source.sqlite3')
    fill(index, 1)
    index.put('owner', 'reply:0:1', '合成来信0', '合成回信', NOW + timedelta(days=1))
    assert index.vectors_missing('owner', 'model-a') == []
    assert len(index.nearest_sources('owner', 'model-a', [1., 0.])) == 1
