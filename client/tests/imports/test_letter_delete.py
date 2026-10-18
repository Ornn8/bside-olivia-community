import sqlite3

import pytest

from runtime.imports.letter_delete import Sources, apply_sql, history_plan, imported_identity, imported_plan, prune, sql_plan


def test_exact_sources_and_shared_contributions():
    sources = Sources({'letter_id': 'a'}, 'user')
    assert sources.matches('reply:a:1')
    assert not sources.matches('reply:ab:1')
    assert prune({'source_id': 'reply:a:1', 'quote': 'secret'}, sources) is None
    assert prune({'source_ids': ['reply:a:1', 'reply:b:1'], 'fact': 'shared'}, sources) == {
        'source_ids': ['reply:b:1'], 'fact': 'shared'}
    assert prune({'source_id': 'reply:b:1', 'quote': 'reply:a:1'}, sources) == {
        'source_id': 'reply:b:1', 'quote': 'reply:a:1'}


def test_sql_plan_backup_restore_and_conflicting_restore(tmp_path):
    path = tmp_path / 'life.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE life_moments(source_id TEXT PRIMARY KEY, payload TEXT)')
        db.executemany('INSERT INTO life_moments VALUES (?,?)', [('reply:a:1', '{"note":"a"}'), ('reply:b:1', '{"note":"b"}')])
    plan = sql_plan(path, Sources({'letter_id': 'a'}, 'user'))
    assert len(plan) == 1
    apply_sql(path, plan)
    apply_sql(path, plan)  # crash recovery is idempotent
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT source_id FROM life_moments').fetchall() == [('reply:b:1',)]
    apply_sql(path, plan, restore=True)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM life_moments').fetchone()[0] == 2
        db.execute('UPDATE life_moments SET payload=? WHERE source_id=?', ('new', 'reply:a:1'))
    with pytest.raises(ValueError, match='CONFLICT'):
        apply_sql(path, plan)


def test_originals_preserve_other_speaker_and_other_user(tmp_path):
    from runtime.memory.source_retrieval import SourceRetrieval
    from datetime import datetime, timezone
    index = SourceRetrieval(tmp_path / 'originals.db')
    index.put('local-user', 'reply:a:1', 'mine', 'hers', datetime.now(timezone.utc))
    index.put('other', 'reply:a:1', 'other mine', 'other hers', datetime.now(timezone.utc))
    plan = sql_plan(index.path, Sources({'letter_id': 'a'}, 'user'))
    apply_sql(index.path, plan)
    with index.connect() as db:
        assert db.execute('SELECT actor,text FROM originals WHERE user=?', ('local-user',)).fetchall() == [('linli', 'hers')]
        assert db.execute('SELECT COUNT(*) FROM originals WHERE user=?', ('other',)).fetchone()[0] == 2
    apply_sql(index.path, plan, restore=True)
    with index.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM originals').fetchone()[0] == 4


def test_deleted_source_cannot_be_reindexed(tmp_path):
    from runtime.memory.source_retrieval import SourceRetrieval
    from datetime import datetime, timezone
    index = SourceRetrieval(tmp_path / 'originals.db')
    now = datetime.now(timezone.utc)
    index.put('local-user', 'reply:a:1', 'mine', 'hers', now)
    plan = sql_plan(index.path, Sources({'letter_id': 'a'}, 'user'))
    index.mark_letter_deleted('local-user', {'letter_id': 'a'}, 'user')
    apply_sql(index.path, plan)
    index.put('local-user', 'reply:a:1', 'mine', 'hers', now)
    with index.connect() as db:
        assert db.execute('SELECT actor,text FROM originals').fetchall() == [('linli', 'hers')]
    index.mark_letter_deleted('local-user', {'letter_id': 'a'}, 'user', restore=True)
    apply_sql(index.path, plan, restore=True)
    assert not index.letter_deleted('local-user', 'reply:a:1')


def test_world_relationship_evidence_removed_and_restored(tmp_path):
    from datetime import datetime, timedelta, timezone
    import hashlib
    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
    from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
    from runtime.imports.letter_delete import world_plan
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for i, identity in enumerate(('a:1', 'b:1')):
        at = now + timedelta(days=i)
        PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(delivery_id=identity,
            occurred_at=at, semantic_key=identity, canonical_reply_sha256=hashlib.sha256(b'reply').hexdigest()))
        assert PrivateWorldRelationshipCommitter(ledger).commit_exchange(identity, identity, 'reply',
            {'kind': 'support_received', 'user_quote': identity, 'reply_quote': 'reply'}, occurred_at=at).value == 'COMMITTED'
    before = ledger.snapshot()
    patches = world_plan(ledger, Sources({'letter_id': 'a'}, 'reply'))
    apply_sql(ledger._database_path, patches)
    assert ledger.snapshot().trust < before.trust
    assert any(e.delivery_id == 'b:1' for e in ledger.events())
    assert all(e.event_type == 'letter_source_removed' and e.payload == {} for e in ledger.events() if e.delivery_id == 'a:1')
    with sqlite3.connect(ledger._database_path) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    apply_sql(ledger._database_path, patches, restore=True)
    assert ledger.snapshot() == before


def test_a_live_memory_belongs_to_the_user_side_only():
    # A memory made from a live letter holds the user's own words and names its speaker local_user.
    memory = {'source_id': 'reply:a:1', 'actor': 'local_user', 'data': '用户当时的原话：…'}
    assert prune(memory, Sources({'letter_id': 'a'}, 'user')) is None
    assert prune(memory, Sources({'letter_id': 'a'}, 'reply')) == memory
    proactive = {'source_id': 'reply:a:1', 'actor': 'linli', 'data': '她主动写的信'}
    assert prune(proactive, Sources({'letter_id': 'a'}, 'reply')) is None
    assert prune(proactive, Sources({'letter_id': 'a'}, 'user')) == proactive


def test_mem0_history_of_a_removed_memory_goes_with_it_and_comes_back(tmp_path):
    path = tmp_path / 'history.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, new_memory TEXT, event TEXT)')
        db.executemany('INSERT INTO history VALUES (?,?,?,?,?)', [
            ('h1', 'm1', None, '用户当时的原话：删除我', 'ADD'), ('h2', 'm1', '用户当时的原话：删除我', '用户当时的原话：改过', 'UPDATE'),
            ('h3', 'm2', None, '另一条记忆', 'ADD')])
    plan = history_plan(path, {'m1'})
    assert [p['before']['id'] for p in plan] == ['h1', 'h2']
    apply_sql(path, plan)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id FROM history').fetchall() == [('h3',)]
    apply_sql(path, plan, restore=True)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id,memory_id,new_memory FROM history ORDER BY id').fetchall() == [
            ('h1', 'm1', '用户当时的原话：删除我'), ('h2', 'm1', '用户当时的原话：改过'), ('h3', 'm2', '另一条记忆')]
    assert history_plan(path, set()) == [] and history_plan(tmp_path / 'missing.db', {'m1'}) == []


def test_relationship_moments_and_waiting_turns_of_the_letter_are_removed_with_it(tmp_path):
    path = tmp_path / 'world.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE relationship_moments (id TEXT PRIMARY KEY, occurred_at TEXT, kind TEXT, user_quote TEXT, '
                   'reply_quote TEXT, source_id TEXT UNIQUE, sensitive INTEGER)')
        db.execute('CREATE TABLE relationship_queue (id TEXT PRIMARY KEY, message_id TEXT, payload TEXT, queued_at TEXT, status TEXT)')
        db.execute('CREATE TABLE relationship_batch_state (key TEXT PRIMARY KEY, value TEXT)')
        db.executemany('INSERT INTO relationship_moments VALUES (?,?,?,?,?,?,?)', [
            ('m1', 't', 'kept', '我的话', '她的话', 'a:1', 0), ('m2', 't', 'plan', '别的话', '别的回信', 'b:1', 0)])
        db.executemany('INSERT INTO relationship_queue VALUES (?,?,?,?,?)', [
            ('a:1', 'a', '{"id":"a:1","user":"我的话","reply":"她的话"}', 't', 'pending'),
            ('b:1', 'b', '{"id":"b:1","user":"别的话","reply":"别的回信"}', 't', 'pending'),
            ('c:1', 'c', '{}', 't', 'done')])
        db.executemany('INSERT INTO relationship_batch_state VALUES (?,?)', [
            ('relationship-batch:x', '{"turns":[{"user_quote":"我的话"}]}'), ('retry_at', 't')])
    for part in ('user', 'reply'):
        plan = sql_plan(path, Sources({'letter_id': 'a'}, part))
        assert sorted((p['table'], p['before'].get('id') or p['before'].get('key')) for p in plan) == [
            ('relationship_batch_state', 'relationship-batch:x'), ('relationship_moments', 'm1'), ('relationship_queue', 'a:1')]
        assert all(p['after'] is None for p in plan)
        apply_sql(path, plan)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT id FROM relationship_moments').fetchall() == [('m2',)]
            assert sorted(r[0] for r in db.execute('SELECT id FROM relationship_queue')) == ['b:1', 'c:1']
            assert db.execute('SELECT key FROM relationship_batch_state').fetchall() == [('retry_at',)]
        apply_sql(path, plan, restore=True)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM relationship_moments').fetchone()[0] == 2
            assert db.execute('SELECT COUNT(*) FROM relationship_queue').fetchone()[0] == 3


def _imported_fixture(tmp_path):
    import hashlib
    import json
    pairs = [('我的话甲', '她的话甲'), ('我的话乙', '她的话乙'), ('我的话丙', '她的话丙')]
    identity = lambda pair: 'relationship-letter:' + hashlib.sha256(json.dumps(list(pair), ensure_ascii=False).encode('utf-8')).hexdigest()
    exchange = lambda pair: {'source_record_id': identity(pair), 'occurred_at': '1970-01-01T00:00:00+00:00',
                             'user_message': pair[0], 'assistant_message': pair[1], 'timestamp_known': False}
    archive, queue = tmp_path / 'archive.db', tmp_path / 'history-relationship.sqlite3'
    with sqlite3.connect(archive) as db:
        db.execute('CREATE TABLE legacy_letters (memory_id TEXT, metadata_json TEXT)')
        db.execute('INSERT INTO legacy_letters VALUES (?,?)', ('L1', json.dumps({'user_content': pairs[0][0], 'reply_text': pairs[0][1]})))
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE batches (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, assessment TEXT, error TEXT)')
        db.execute('INSERT INTO batches(payload,state) VALUES (?,?)', (json.dumps([exchange(pairs[0]), exchange(pairs[1])], ensure_ascii=False), 'pending'))
        db.execute('INSERT INTO batches(payload,state,assessment) VALUES (?,?,?)', (json.dumps([exchange(pairs[0]), exchange(pairs[2])], ensure_ascii=False), 'done', '{"evidence_indexes":[1]}'))
        db.execute('INSERT INTO batches(payload,state) VALUES (?,?)', (json.dumps([exchange(pairs[0])], ensure_ascii=False), 'failed'))
    return archive, queue, identity(pairs[0])


def test_an_imported_pair_leaves_the_batches_waiting_for_the_relationship_assessment(tmp_path):
    import json
    archive, queue, identity = _imported_fixture(tmp_path)
    assert imported_identity(archive, 'L1') == identity and imported_identity(archive, 'missing') is None
    plan = imported_plan(queue, identity, 'user')
    apply_sql(queue, plan)
    with sqlite3.connect(queue) as db:
        rows = db.execute('SELECT id,state,payload FROM batches ORDER BY id').fetchall()
    # not yet assessed: the pair leaves the batch (a batch left empty goes); assessed: the pair keeps its place
    assert [(row[0], row[1]) for row in rows] == [(1, 'pending'), (2, 'done')]
    assert [item['user_message'] for item in json.loads(rows[0][2])] == ['我的话乙']
    assert [(item['user_message'], item['assistant_message']) for item in json.loads(rows[1][2])] == [('（已删除）', '她的话甲'), ('我的话丙', '她的话丙')]
    apply_sql(queue, plan, restore=True)
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT COUNT(*) FROM batches').fetchone()[0] == 3
        assert '我的话甲' in db.execute('SELECT payload FROM batches WHERE id=2').fetchone()[0]


def test_a_restore_goes_on_when_a_batch_was_assessed_after_the_deletion(tmp_path):
    _, queue, identity = _imported_fixture(tmp_path)
    plan = imported_plan(queue, identity, 'reply')
    apply_sql(queue, plan)
    with sqlite3.connect(queue) as db:
        db.execute("UPDATE batches SET state='done', assessment='{}' WHERE id=1")
    apply_sql(queue, plan, restore=True)  # the first batch cannot take the pair back; the rest of the restore is not stopped
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT state FROM batches WHERE id=1').fetchone()[0] == 'done'
        assert '她的话甲' in db.execute('SELECT payload FROM batches WHERE id=2').fetchone()[0]
