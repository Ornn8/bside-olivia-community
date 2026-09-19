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
        assert mailbox[0]['created_at'] == payload['letters'][0]['created_at']
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
