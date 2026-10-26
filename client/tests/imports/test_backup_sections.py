"""The backup carries her diary with his comments, the stored facts and calls, and restores them without a model."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.diary.diary import DiaryStore
from runtime.imports import backup_sections
from runtime.imports.letter_backup import export_letters, import_letters
from runtime.memory.local_memory import LocalMemoryAdapter

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
FACT = {'kind': 'promise', 'text': '周六一起看电影', 'date': '2026-10-31', 'quote': '周六去看电影吧'}


def diary(tmp_path, name='diary.sqlite3'):
    store = DiaryStore(tmp_path / name)
    store.save('2026-10-24', {'title': '下雨', 'mood': '安静', 'body': '他说周六去看电影吧。', 'facts': [FACT]},
               now=NOW, model='m', short=False, style='prose')
    store.save('2026-10', {'title': '十月', 'mood': '想念', 'body': '这个月的回忆。', 'facts': []},
               now=NOW, model='m', short=False, style='prose')
    store.mark_seen('2026-10-24')
    store.add_comment('2026-10-24', '我也记着呢', NOW)
    return store


def call(**changes):
    return {'call_id': 'c0ffee12345678', 'started_at': '2026-10-25T12:00:00+00:00',
            'ended_at': '2026-10-25T12:05:00+00:00', 'seconds': 300, 'pickup': 'answer', 'reason': '',
            'charge_cents': 30, 'transcript': [{'role': 'user', 'text': '今天好累'}, {'role': 'her', 'text': '抱抱你'}],
            'notes': {'summary': '他加班很累', 'user_shared': [], 'her_promises': []}, 'memory_format': 2,
            'life_status': 'DONE', **changes}


def test_diary_roundtrip_keeps_entries_memoirs_facts_and_comments(tmp_path):
    payload = backup_sections.export([], diary=diary(tmp_path).backup(), memories=[])
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    entries, comments, facts = backup_sections.read(json.dumps(payload, ensure_ascii=False))
    fresh = DiaryStore(tmp_path / 'fresh.sqlite3')
    assert fresh.restore(entries, comments) == (2, 1)
    assert fresh.restore(entries, comments) == (0, 0)
    restored = fresh.entry('2026-10-24')
    assert (restored['title'], restored['body'], restored['seen']) == ('下雨', '他说周六去看电影吧。', True)
    assert [c['text'] for c in restored['comments']] == ['我也记着呢']
    assert fresh.entry('2026-10')['body'] == '这个月的回忆。'
    assert '周六一起看电影' in fresh.context(NOW)
    assert not fresh.may_attempt('2026-10-24')
    assert facts == []


def test_restore_keeps_what_this_install_wrote_or_deleted(tmp_path):
    entries, comments, _ = backup_sections.validate_sections({'diary': diary(tmp_path).backup()})
    here = DiaryStore(tmp_path / 'here.sqlite3')
    here.save('2026-10-24', {'title': '这边写的', 'mood': '平静', 'body': '这台电脑上写的那天。', 'facts': []},
              now=NOW, model='m', short=False)
    here.save('2026-10', {'title': '删掉', 'mood': '平静', 'body': '删掉的回忆。', 'facts': []},
              now=NOW, model='m', short=False)
    here.delete('2026-10')
    assert here.restore(entries, comments) == (0, 1)
    assert here.entry('2026-10-24')['body'] == '这台电脑上写的那天。'
    assert here.entry('2026-10') is None


@pytest.mark.parametrize('broken', [
    {'diary': {'entries': [{'day': '昨天'}], 'comments': []}},
    {'diary': {'entries': [], 'comments': [{'day': '2026-10-24', 'written_at': 'x', 'text': '留言'}]}},
    {'diary': {'entries': []}},
    {'memories': [{'text': 'fact', 'source_id': 'has space'}]},
    {'memories': [{'text': 'fact', 'source_id': 'history:1', 'metadata': {'api_key': 'never'}}]},
    {'memories': [{'text': '', 'source_id': 'history:1'}]},
])
def test_a_broken_section_stops_the_import_before_any_write(tmp_path, broken):
    payload = {**export_letters([{'letter_id': 'one', 'content': '原信'}]), **broken}
    with pytest.raises(ValueError):
        backup_sections.read(json.dumps(payload, ensure_ascii=False))


def test_files_without_sections_still_import():
    assert backup_sections.read(json.dumps(export_letters([{'content': '旧版本的备份'}]))) == ([], [], [])
    assert backup_sections.read([{'content': 'letter_pairs', 'reply': 'reply'}]) == ([], [], [])
    assert backup_sections.read({'format': 'soul', 'manifest': {}}) == ([], [], [])


def test_an_unreadable_memory_store_is_written_down_not_hidden():
    class Broken:
        enabled = True

        def all_memories(self, *, user_id):
            raise RuntimeError('MEM0_LIST_FAILED')
    assert backup_sections.memory_section(Broken(), 'local-user') is None
    payload = backup_sections.export([], diary=None, memories=None)
    assert payload['memories_unavailable'] is True and 'memories' not in payload and 'diary' not in payload
    assert backup_sections.memory_section(SimpleNamespace(enabled=False), 'local-user') == []


def test_calls_become_text_records_that_restore_once(tmp_path):
    rows = backup_sections.call_letters([call(), {'call_id': 'missed1234', 'missed': True}])
    assert len(rows) == 1 and rows[0]['letter_id'].startswith('call-') and rows[0]['title'] == '语音通话'
    payload = export_letters(rows)
    record = payload['letters'][0]
    assert '今天好累' in record['content'] and '抱抱你' in record['reply_text'] and 'charge' not in str(payload)
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as adapter:
        same = import_letters(payload, adapter=adapter, existing=backup_sections.call_letters([call()]))
        assert (same['inserted'], same['duplicates']) == (0, 1)
        assert import_letters(payload, adapter=adapter)['inserted'] == 1
        assert export_letters(adapter.list_legacy())['letters'] == payload['letters']


def test_stored_facts_come_back_as_they_were(tmp_path):
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from tests.memory.test_mem0_memory import FakeMem0, _config
    source = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path / 'a'))
    source.backend.add('他养了一只叫团子的猫。', user_id='local-user', agent_id='linli', infer=False,
                       metadata={'source_id': 'history:1', 'domain': 'conversation_memory', 'canonical': True,
                                 'history_actor': 'user', 'occurred_at': '2026-09-01T08:00:00+00:00'})
    records = backup_sections.memory_section(source, 'local-user')
    assert records == [{'text': '他养了一只叫团子的猫。', 'source_id': 'history:1',
                        'occurred_at': '2026-09-01T08:00:00+00:00',
                        'metadata': {'canonical': True, 'history_actor': 'user'}}]
    _, _, facts = backup_sections.validate_sections({'memories': records})
    target = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path / 'b'))
    assert target.restore_memories(facts, user_id='local-user') == 1
    assert target.restore_memories(facts, user_id='local-user') == 0
    [restored] = target.list_memories(user_id='local-user', limit=10)
    assert (restored.text, restored.source_id) == ('他养了一只叫团子的猫。', 'history:1')
    assert restored.occurred_at.isoformat() == '2026-09-01T08:00:00+00:00'
    assert restored.metadata['history_actor'] == 'user' and restored.metadata['canonical'] is True
    assert all(call[1].get('infer') is False for call in target.backend.calls if call[0] == 'add')


def test_backup_routes_carry_diary_facts_and_calls_to_a_fresh_install(tmp_path, monkeypatch):
    import local_server as server
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from tests.memory.test_mem0_memory import FakeMem0, _config
    archive = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path / 'a'))
    memory.backend.add('他喜欢喝冰美式。', user_id='local-user', agent_id='linli', infer=False,
                       metadata={'source_id': 'history:2', 'domain': 'conversation_memory'})
    state = SimpleNamespace(letters=[{'letter_id': 'one', 'content': '信件原文', 'reply_text': '信件回复'}],
                            personal_chats=[], legacy_letters=[], calls=[call()])
    monkeypatch.setattr(server, 'store', state)
    monkeypatch.setattr(server, 'memory_adapter', archive)
    monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
    monkeypatch.setattr(server, '_mark_superseded_failed_retries', lambda: None)
    monkeypatch.setattr(server, '_start_history_relationships', lambda **kw: None)
    monkeypatch.setattr(server, '_store_state_error_code', None)
    monkeypatch.setattr(server, 'diary_store', diary(tmp_path))
    monkeypatch.setattr(server.letters_adapter, 'conversation_memory', memory)
    monkeypatch.setattr(server.letters_adapter, 'memory_prompt_builder',
                        SimpleNamespace(conversation_memory_user_id='local-user'))

    async def run():
        exported = await server.route('POST', '/toy/letter/backup/export', {}, {}, companion_confirmed=True)
        backup = exported['data']['backup']
        assert len(backup['letters']) == 2 and len(backup['diary']['entries']) == 2
        assert [row['text'] for row in backup['memories']] == ['他喜欢喝冰美式。']
        same = await server.route('POST', '/toy/letter/backup/import', {'backup': json.dumps(backup)}, {},
                                  companion_confirmed=True)
        assert (same['data']['inserted'], same['data']['duplicates'], same['data']['diary_entries']) == (0, 2, 0)
        await asyncio.gather(*server._backup_memory_tasks)
        assert len(memory.list_memories(user_id='local-user', limit=10)) == 1
        fresh = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path / 'b'))
        state.letters, state.calls = [], []
        monkeypatch.setattr(server, 'diary_store', DiaryStore(tmp_path / 'fresh.sqlite3'))
        monkeypatch.setattr(server.letters_adapter, 'conversation_memory', fresh)
        moved = await server.route('POST', '/toy/letter/backup/import', {'backup': json.dumps(backup)}, {},
                                   companion_confirmed=True)
        data = moved['data']
        assert (data['inserted'], data['diary_entries'], data['diary_comments']) == (2, 2, 1)
        assert data['memories'] == {'status': 'RESTORING', 'count': 1} and data['provider_calls'] == 0
        await asyncio.gather(*server._backup_memory_tasks)
        assert [r.text for r in fresh.list_memories(user_id='local-user', limit=10)] == ['他喜欢喝冰美式。']
        assert server.diary_store.entry('2026-10-24')['comments'][0]['text'] == '我也记着呢'
        again = await server.route('POST', '/toy/letter/backup/export', {}, {}, companion_confirmed=True)
        assert sorted(again['data']['backup']['letters'], key=lambda row: row['source_id']) == sorted(
            backup['letters'], key=lambda row: row['source_id'])
    try:
        asyncio.run(run())
    finally:
        archive.close()


def test_memories_are_not_promised_when_long_term_memory_is_off():
    import local_server as server

    class Off:
        enabled = True

        def status(self):
            return SimpleNamespace(status='disabled')

        def restore_memories(self, records, *, user_id):
            raise AssertionError('nothing is written while memory is off')
    records = [{'text': 'fact', 'source_id': 'history:1', 'metadata': {}, 'occurred_at': None}]
    assert asyncio.run(server._restore_backup_memories(Off(), 'local-user', records)) == {
        'status': 'UNAVAILABLE', 'count': 1}
    assert asyncio.run(server._restore_backup_memories(None, 'local-user', records))['status'] == 'UNAVAILABLE'
    assert asyncio.run(server._restore_backup_memories(Off(), 'local-user', [])) == {'status': 'NONE', 'count': 0}
