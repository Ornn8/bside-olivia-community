import asyncio
from copy import deepcopy

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    import local_server as s
    from runtime.memory.conversation_memory_port import NullConversationMemoryPort
    from runtime.memory.memory_port import NullMemoryPort
    from runtime.private_world.port import NullPrivateWorldPort
    monkeypatch.setattr(s, 'store', s.Store())
    monkeypatch.setattr(s, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(s, '_store_state_error_code', None)
    monkeypatch.setattr(s, 'conversation_memory_adapter', NullConversationMemoryPort())
    monkeypatch.setattr(s, 'memory_adapter', NullMemoryPort())
    monkeypatch.setattr(s, 'daily_life_runtime', None)
    monkeypatch.setattr(s, 'private_world_port', NullPrivateWorldPort())
    monkeypatch.setattr(s, 'diary_store', None)
    monkeypatch.setattr(s, 'resolve_private_world_database', lambda *a, **kw: (None, None, False))
    monkeypatch.setattr(s, '_official_history_mailbox_projection', lambda **kw: [])
    monkeypatch.setattr(s, '_mark_superseded_failed_retries', lambda: None)
    monkeypatch.setattr(s, '_schedule_pending_reply_jobs', lambda: 0)
    s.store.letters = [dict(letter_id='a', content='我的信', reply_text='她的信',
                           letter_status='COMPLETED', created_at=100)]
    return s


def call(s, action, body=None, confirmed=True):
    return asyncio.run(s.route('POST', '/toy/letter/deletion/' + action, body or {}, {}, companion_confirmed=confirmed))


@pytest.mark.parametrize('part', ['user', 'reply'])
def test_delete_half_restore_restart(server, part):
    s = server
    original = deepcopy(s.store.letters[0])
    result = call(s, 'delete', {'letter_id': 'a', 'part': part})
    assert result['code'] == 0, result
    row = s.store.letters[0]
    field, other = ('content', 'reply_text') if part == 'user' else ('reply_text', 'content')
    assert row[field] == '' and row[other] == original[other]
    assert len(s._letter_collection('current')) == 1
    s.store = s.Store()
    s._load_store_state()
    trash = call(s, 'list')['data']['items']
    assert len(trash) == 1
    assert call(s, 'restore', {'id': trash[0]['id']})['code'] == 0
    assert s.store.letters[0][field] == original[field]
    assert call(s, 'list')['data']['items'] == []


def test_before_reply_deleted_not_queued_and_retention_purges(server):
    from runtime.imports.letter_delete_runtime import purge
    from runtime.imports.letter_delete import RETENTION_SECONDS
    from runtime.memory.conversation_memory_outbox import _delivery_from_row
    row = server.store.letters[0]
    row.update(reply_text='', letter_status='PENDING')
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user'})['code'] == 0
    assert row['letter_status'] == 'CANCELED'
    assert server._letter_collection('current') == []
    assert _delivery_from_row(row, user_id='local-user') is None
    entry = next(iter(server.store.letter_trash.values()))
    assert purge(server, entry['deleted_at'] + RETENTION_SECONDS - 1) == 0
    assert purge(server, entry['deleted_at'] + RETENTION_SECONDS) == 1
    assert row['content'] == '' and not server.store.letter_trash


def test_confirm_validation_and_write_failure(server, monkeypatch):
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user'}, False)['code'] == 403
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'both'})['code'] == 400
    original = deepcopy(server.store.letters)
    def fail():
        raise server.StoreStateUnavailable()
    monkeypatch.setattr(server, '_persist_store_state', fail)
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user'})['code'] == 503
    assert server.store.letters == original
    assert server.store.letter_trash == {}


@pytest.mark.parametrize('format', ['backup', 'offline', 'legacy'])
def test_old_archive_formats_restore_exact_bytes(server, monkeypatch, tmp_path, format):
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.memory_port import LegacyLetter
    from runtime.imports.letter_backup import export_letters, import_letters
    archive = LocalMemoryAdapter(tmp_path / 'archive.db')
    try:
        if format == 'backup':
            import_letters(export_letters([{'letter_id': 'old', 'content': '旧去信', 'reply_text': '旧回信'}]), adapter=archive)
        else:
            archive.import_legacy_records([LegacyLetter(content='旧去信\n旧回信', source_record_id='offline-letter-pairs:one' if format == 'offline' else 'official:one',
                source='fixture', metadata={'user_content': '旧去信', 'reply_text': '旧回信'})])
        original = archive.list_legacy()[0]
        server.store.letters = []
        monkeypatch.setattr(server, 'memory_adapter', archive)
        monkeypatch.setattr(server, '_official_history_mailbox_projection', lambda **kw: archive.list_legacy())
        result = call(server, 'delete', {'letter_id': original['letter_id'], 'part': 'user'})
        assert result['code'] == 0, result
        row = archive.list_legacy()[0]
        assert row['content'] == '' and row['reply_text'] == '旧回信'
        trash = call(server, 'list')['data']['items']
        assert call(server, 'restore', {'id': trash[0]['id']})['code'] == 0
        assert archive.list_legacy()[0] == original
        with pytest.raises(Exception, match='read-only'):
            archive.connection.execute('UPDATE legacy_letters SET content=?', ('forbidden',))
    finally:
        archive.close()


def test_unattributed_diary_blocks_before_mutating_anything(server, tmp_path, monkeypatch):
    from runtime.diary.diary import DiaryStore, local_day
    from datetime import datetime, timezone
    diary = DiaryStore(tmp_path / 'diary.db')
    monkeypatch.setattr(server, 'diary_store', diary)
    day = local_day(datetime.fromtimestamp(100, timezone.utc))
    with diary._db() as db:
        db.execute('INSERT INTO diary_entries(day,written_at,title,mood,body,facts) VALUES (?,?,?,?,?,?)',
                   (day, day, 'synthetic', 'calm', 'mixed sources', '[]'))
    original = deepcopy(server.store.letters)
    result = call(server, 'delete', {'letter_id': 'a', 'part': 'user'})
    assert result['data']['error_code'] == 'LETTER_DELETE_UNATTRIBUTED_DIARY'
    assert server.store.letters == original and not server.store.letter_trash


def test_partial_cleanup_is_recoverable_and_not_purged(server, tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from runtime.imports import letter_delete_runtime as runtime
    path = tmp_path / 'life.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE life_moments(source_id TEXT PRIMARY KEY, payload TEXT)')
        db.execute('INSERT INTO life_moments VALUES (?,?)', ('reply:a:1', '{}'))
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=SimpleNamespace(path=path)))
    apply_points = runtime._apply_points
    def fail(*a, **kw):
        raise OSError('synthetic failure')
    monkeypatch.setattr(runtime, '_apply_points', fail)
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user'})['code'] == 503
    entry = next(iter(server.store.letter_trash.values()))
    assert entry['status'] == 'prepared'
    assert runtime.purge(server, entry['deleted_at'] + 90 * 86400) == 0
    assert not server._conversation_memory_ready_for_reply()
    monkeypatch.setattr(runtime, '_apply_points', apply_points)
    identity = next(iter(server.store.letter_trash))
    assert call(server, 'restore', {'id': identity})['code'] == 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT source_id FROM life_moments').fetchall() == [('reply:a:1',)]


def test_delete_both_sides_and_restore_exchange(server):
    for part in ('user', 'reply'):
        assert call(server, 'delete', {'letter_id': 'a', 'part': part})['code'] == 0
    assert server._letter_collection('current') == []
    items = call(server, 'list')['data']['items']
    assert len(items) == 2
    assert call(server, 'restore', {'id': items[0]['id']})['code'] == 0
    assert server.store.letters[0]['content'] == '我的信'
    assert server.store.letters[0]['reply_text'] == '她的信'
    assert not server.store.letter_trash
