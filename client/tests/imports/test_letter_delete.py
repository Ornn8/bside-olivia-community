import sqlite3

import pytest

from runtime.imports.letter_delete import Sources, prune, sql_plan, apply_sql


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
