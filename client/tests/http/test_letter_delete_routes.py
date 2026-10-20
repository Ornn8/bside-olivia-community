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


@pytest.fixture
def diary(server, tmp_path, monkeypatch):
    """Her diary for the day of the letter (1970-01-01), a comment under it, an unfinished draft, and the memoir of the month."""
    from runtime.diary.diary import DiaryStore
    store = DiaryStore(tmp_path / 'diary.db')
    monkeypatch.setattr(server, 'diary_store', store)
    with store._db() as db:
        for key, title in (('1970-01-01', '那一天'), ('1970-01', '那个月')):
            db.execute('INSERT INTO diary_entries(day,written_at,title,mood,body,facts) VALUES (?,?,?,?,?,?)',
                       (key, key, title, '平静', '我的信里写到了她的信', '[]'))
        db.execute('INSERT INTO diary_comments VALUES (?,?,?)', ('1970-01-01', 'x', '评论'))
        db.execute('INSERT INTO diary_work VALUES (?,?,?)', ('1970-01-01', 'draft', '{"messages":["我的信"]}'))
    return store


def diary_rows(store):
    import sqlite3
    with sqlite3.connect(store.path) as db:
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                for table in ('diary_entries', 'diary_comments', 'diary_work', 'diary_rewrite', 'diary_deleted')}


def test_the_confirmation_asks_about_the_diary_only_when_there_is_one(server, diary, monkeypatch):
    assert call(server, 'diary', {'letter_ids': ['a']})['data'] == {'status': 'READY', 'days': ['1970-01-01']}
    assert call(server, 'diary', {'letter_ids': ['unknown']})['data']['days'] == []
    with diary._db() as db:
        db.execute("DELETE FROM diary_entries WHERE day='1970-01-01'")  # the memoir of the month alone is not offered
    assert call(server, 'diary', {'letter_ids': ['a']})['data']['days'] == []
    server.store.letters[0].pop('created_at')  # a letter without a time belongs to no day
    assert call(server, 'diary', {'letter_ids': ['a']})['data']['days'] == []
    assert call(server, 'diary', {'letter_ids': 'a'})['data']['error_code'] == 'LETTER_DELETE_INVALID'
    monkeypatch.setattr(server, 'diary_store', None)
    assert call(server, 'diary', {'letter_ids': ['a']})['data']['days'] == []


def test_her_diary_is_not_touched_unless_the_user_also_asks(server, diary):
    before = diary_rows(diary)
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user'})['code'] == 0
    assert diary_rows(diary) == before
    assert server.store.letters[0]['content'] == ''
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'reply', 'with_diary': 'yes'})['data']['error_code'] == 'LETTER_DELETE_INVALID'


def test_the_diary_of_that_day_goes_with_the_letter_to_be_written_again_and_the_original_returns_with_the_restore(server, diary):
    before = diary_rows(diary)
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user', 'with_diary': True})['code'] == 0
    # the day's entry, comment and draft are gone and the day is to be written again; the month's memoir stays
    assert diary_rows(diary) == {'diary_entries': [row for row in before['diary_entries'] if row[0] == '1970-01'],
                                 'diary_comments': [], 'diary_work': [], 'diary_rewrite': [('1970-01-01',)], 'diary_deleted': []}
    # the second side of the same letter finds nothing more to remove
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'reply', 'with_diary': True})['code'] == 0
    assert diary_rows(diary)['diary_rewrite'] == [('1970-01-01',)]
    trash = call(server, 'list')['data']['items']
    assert call(server, 'restore', {'id': trash[0]['id']})['code'] == 0
    assert diary_rows(diary) == before and call(server, 'list')['data']['items'] == []
    assert server.store.letters[0]['content'] == '我的信' and server.store.letters[0]['reply_text'] == '她的信'


def test_the_restore_puts_the_original_entry_in_place_of_the_one_written_since(server, diary):
    from datetime import datetime, timezone
    before = diary_rows(diary)
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user', 'with_diary': True})['code'] == 0
    assert diary.rewrite_days() == ['1970-01-01']
    # the diary tick writes the day again from what is left, and the user comments on the new entry
    assert diary.save('1970-01-01', {'title': '重写的', 'mood': '平静', 'body': '她的信里的话', 'facts': []},
                      now=datetime(1970, 1, 3, tzinfo=timezone.utc), model='m', short=False)
    diary.add_comment('1970-01-01', '新评论', datetime(1970, 1, 3, tzinfo=timezone.utc))
    diary.failed('1970-01-01', 'Leftover')
    assert diary.rewrite_days() == [] and diary.entry('1970-01-01')['title'] == '重写的'
    assert call(server, 'restore', {'id': call(server, 'list')['data']['items'][0]['id']})['code'] == 0
    assert diary_rows(diary) == before, 'the original entry replaces the new one and nothing is written again'
    assert diary.rewrite_days() == [] and not diary.may_attempt('1970-01-01')


def test_the_diary_choice_survives_restoring_an_older_deletion(server, diary):
    server.store.letters.append(dict(letter_id='b', content='第二封', reply_text='第二封回信', letter_status='COMPLETED', created_at=200))
    before = diary_rows(diary)
    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user'})['code'] == 0
    assert call(server, 'delete', {'letter_id': 'b', 'part': 'user', 'with_diary': True})['code'] == 0
    assert diary_rows(diary)['diary_rewrite'] == [('1970-01-01',)]
    older = min(call(server, 'list')['data']['items'], key=lambda item: item['deleted_at'])
    assert call(server, 'restore', {'id': older['id']})['code'] == 0
    # letter a is back; letter b was deleted again with its diary choice, as chosen when it was deleted
    assert server.store.letters[0]['content'] == '我的信' and server.store.letters[1]['content'] == ''
    assert diary_rows(diary)['diary_rewrite'] == [('1970-01-01',)] and len(diary_rows(diary)['diary_entries']) == 1
    (remaining,) = call(server, 'list')['data']['items']
    assert call(server, 'restore', {'id': remaining['id']})['code'] == 0
    assert diary_rows(diary) == before


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
    # Left alone, an interrupted deletion stops holding letters after a while; one being written still does.
    entry['deleted_at'] -= server.INCOMPLETE_LETTER_TRASH_HOLD_SECONDS + 1
    assert not server._letter_trash_holds_replies()
    with server._history_memory_admin_gate:
        assert server._letter_trash_holds_replies()
    entry['deleted_at'] += server.INCOMPLETE_LETTER_TRASH_HOLD_SECONDS + 1
    assert server._letter_trash_holds_replies()
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


def test_restoring_the_older_deletion_first_keeps_the_newer_one_deleted(server, tmp_path, monkeypatch):
    """Two deletions rewrote the same record. Restoring in any order must give what the remaining deletion leaves."""
    import json
    import sqlite3
    from types import SimpleNamespace
    path = tmp_path / 'life.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE life_notes(id TEXT PRIMARY KEY, payload TEXT)')
        db.execute('INSERT INTO life_notes VALUES (?,?)', ('shared', json.dumps(
            {'sources': ['reply:a:1', 'reply:b:1', 'reply:c:1'], 'text': 'shared'})))
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=SimpleNamespace(path=path)))
    server.store.letters.append(dict(letter_id='b', content='第二封', reply_text='第二封回信', letter_status='COMPLETED', created_at=200))

    def sources():
        with sqlite3.connect(path) as db:
            return json.loads(db.execute('SELECT payload FROM life_notes').fetchone()[0])['sources']

    assert call(server, 'delete', {'letter_id': 'a', 'part': 'user'})['code'] == 0
    assert call(server, 'delete', {'letter_id': 'b', 'part': 'user'})['code'] == 0
    assert sources() == ['reply:c:1']
    older, newer = sorted(call(server, 'list')['data']['items'], key=lambda item: item['deleted_at'])
    assert call(server, 'restore', {'id': older['id']})['data']['status'] == 'RESTORED'
    # letter a is back with its share of the record; letter b stays deleted, with its original deletion time
    assert sources() == ['reply:a:1', 'reply:c:1']
    assert server.store.letters[0]['content'] == '我的信' and server.store.letters[1]['content'] == ''
    (remaining,) = call(server, 'list')['data']['items']
    assert (remaining['letter_id'], remaining['status'], remaining['deleted_at']) == ('b', 'deleted', newer['deleted_at'])
    assert call(server, 'restore', {'id': remaining['id']})['data']['status'] == 'RESTORED'
    assert sources() == ['reply:a:1', 'reply:b:1', 'reply:c:1'] and call(server, 'list')['data']['items'] == []
    assert server.store.letters[1]['content'] == '第二封'
