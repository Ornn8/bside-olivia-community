import asyncio
import json
from types import SimpleNamespace

from runtime.imports.letter_backup import export_letters
from runtime.memory.local_memory import LocalMemoryAdapter


def test_restore_export_mailbox_roundtrip_without_model(tmp_path, monkeypatch):
    import local_server as server
    archive = LocalMemoryAdapter(tmp_path/'archive.sqlite3')
    monkeypatch.setattr(server, 'store', SimpleNamespace(letters=[], legacy_letters=[]))
    monkeypatch.setattr(server, 'memory_adapter', archive)
    monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
    monkeypatch.setattr(server, '_mark_superseded_failed_retries', lambda: None)
    payload = export_letters([{'letter_id':'original-1', 'content':'杯底的小蜗牛\n围巾是橙色的。',
                              'reply_text':'这真有意思。', 'created_at':1788000000,
                              'origin':'user', 'reply_mode':'voice'}])
    async def run():
        denied = await server.route('POST', '/toy/letter/backup/import', {'backup':payload}, {})
        assert denied['code'] == 403
        result = await server.route('POST', '/toy/letter/backup/import', {'backup':payload}, {}, companion_confirmed=True)
        assert result['data']['inserted'] == 1
        assert result['data']['provider_calls'] == 0
        mailbox = server._letter_collection('current')
        assert len(mailbox) == 1
        assert mailbox[0]['read_only'] is True
        assert mailbox[0]['created_at'] == 1788000000
        assert mailbox[0]['reply_mode'] == 'text_letter'
        exported = await server.route('POST', '/toy/letter/backup/export', {}, {}, companion_confirmed=True)
        assert exported['data']['backup']['letters'] == payload['letters']
        again = await server.route('POST', '/toy/letter/backup/import', {'backup':payload}, {}, companion_confirmed=True)
        assert again['data']['duplicates'] == 1
        assert archive.search('蜗牛')
        from runtime.memory.memory_prompt import MemoryPromptBuilder
        prompt = MemoryPromptBuilder(archive, conversation_memory=None).build('蜗牛')
        assert '橙色' in prompt.text
    try:
        asyncio.run(run())
    finally:
        archive.close()


def test_old_file_can_import_originals_with_unavailable_mem0(tmp_path, monkeypatch):
    import local_server as server
    archive = LocalMemoryAdapter(tmp_path/'archive.sqlite3')
    source = tmp_path/'letter_pairs.json'
    source.write_text(json.dumps([{'content':'那只蜗牛','reply':'橙色围巾'}]),encoding='utf-8')
    monkeypatch.setattr(server, '_default_offline_letter_pair_source', lambda:source)
    monkeypatch.setattr(server, '_legacy_import_adapter', lambda:archive)
    def forbidden():
        raise AssertionError('original import must not require a model')
    monkeypatch.setattr(server, '_official_history_preflight_error', forbidden)
    try:
        result = asyncio.run(server.route('POST', '/toy/letter/legacy/local-import',
            {'originals_only':True}, {}, companion_confirmed=True))
        assert result['data']['status'] == 'APPLIED'
        assert result['data']['inserted'] == 1
        assert result['data']['provider_calls'] == 0
    finally:
        archive.close()


def test_export_restore_chat_history_without_transport_or_generation(tmp_path, monkeypatch):
    from copy import deepcopy
    import local_server as server
    from tests.imports.test_personal_chat_backup import chat
    live = [chat(), chat('im-received', content='收到的消息仍保留', delivery_status='FAILED',
                        letter_status='FAILED', reply_text='never-export-draft')]
    original = deepcopy(live)
    archive = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
    state = SimpleNamespace(letters=[{'letter_id': 'one', 'content': '信件原文',
                                     'reply_text': '信件回复'}], personal_chats=live, legacy_letters=[])
    monkeypatch.setattr(server, 'store', state)
    monkeypatch.setattr(server, 'memory_adapter', archive)
    monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
    monkeypatch.setattr(server, '_mark_superseded_failed_retries', lambda: None)
    monkeypatch.setattr(server, '_start_history_relationships', lambda **kw: None)
    monkeypatch.setattr(server, '_store_state_error_code', None)
    async def run():
        exported = await server.route('POST', '/toy/letter/backup/export', {}, {}, companion_confirmed=True)
        backup = exported['data']['backup']
        assert len(backup['letters']) == 3
        assert sum(row.get('channel') == 'qq' for row in backup['letters']) == 2
        assert state.personal_chats == original
        assert 'never-export' not in str(backup)
        same = await server.route('POST', '/toy/letter/backup/import', {'backup': backup}, {}, companion_confirmed=True)
        assert (same['data']['inserted'], same['data']['duplicates']) == (0, 3)
        state.letters = []
        state.personal_chats = []
        imported = await server.route('POST', '/toy/letter/backup/import', {'backup': backup}, {}, companion_confirmed=True)
        assert imported['data']['inserted'] == 3 and imported['data']['provider_calls'] == 0
        assert state.letters == [] and state.personal_chats == []
        assert len(server._letter_collection('current')) == 3
        assert all(row['read_only'] for row in server._letter_collection('current'))
        restored = await server.route('POST', '/toy/letter/backup/export', {}, {}, companion_confirmed=True)
        assert sorted(restored['data']['backup']['letters'], key=lambda row: row['source_id']) == sorted(
            backup['letters'], key=lambda row: row['source_id'])
        again = await server.route('POST', '/toy/letter/backup/import', {'backup': backup}, {}, companion_confirmed=True)
        assert again['data']['duplicates'] == 3
        from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
        from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
        from tests.memory.test_mem0_memory import FakeMem0, _config
        backend = FakeMem0()
        memory = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
        prompt = CompanionMemoryPromptBuilder(archive, memory).build('蜗牛')
        assert '橙色围巾' in prompt.text
        assert not any(method == 'add' for method, _ in backend.calls)
    try:
        asyncio.run(run())
    finally:
        archive.close()


def test_backup_restore_rejects_unloaded_live_chat_state(tmp_path, monkeypatch):
    import local_server as server
    archive = LocalMemoryAdapter(tmp_path / 'archive.sqlite3')
    monkeypatch.setattr(server, '_store_state_error_code', 'STORE_STATE_INVALID')
    monkeypatch.setattr(server, '_legacy_import_adapter', lambda: archive)
    monkeypatch.setattr(server, '_start_history_relationships', lambda **kw: None)
    try:
        result = asyncio.run(server.route('POST', '/toy/letter/backup/import',
            {'backup': export_letters([{'letter_id': 'one', 'content': 'synthetic'}])},
            {}, companion_confirmed=True))
        assert result['code'] == 503
        assert result['message'] == 'LETTER_BACKUP_STORAGE_UNAVAILABLE'
        assert not archive.list_legacy()
    finally:
        archive.close()
