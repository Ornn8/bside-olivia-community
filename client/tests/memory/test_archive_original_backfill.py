import asyncio
from datetime import datetime, timezone

from runtime.memory.source_retrieval import SourceRetrieval
from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox


def test_archive_backfills_in_batches_without_extraction_and_retries(tmp_path):
    from types import SimpleNamespace
    rows = [dict(source_record_id=f'old:{i}', occurred_at='2024-01-02T10:00:00+08:00',
                 imported_at=1704200000, metadata={'import_kind': 'official_text_reply',
                 'user_content': f'piano detail {i}', 'reply_text': 'remembered'}) for i in range(23)]
    archive = SimpleNamespace(list_legacy=lambda: rows)
    index = SourceRetrieval(tmp_path / 'original.sqlite3')
    writes = []
    class Memory:
        ready = False
        def index_original_exchange(self, **kw):
            if not self.ready:
                return False
            writes.append(kw)
            index.put(kw['user_id'], kw['source_id'], kw['user_message'],
                      kw['assistant_message'], kw['occurred_at'])
            return True
    memory = Memory()
    committer = ConversationMemoryDeliveryCommitter(memory)
    outbox = CanonicalMemoryOutbox(tmp_path / 'state.json', tmp_path / 'outbox.sqlite3',
                                  committer, archive_memory=archive)
    asyncio.run(outbox.scan_once())
    assert not writes
    memory.ready = True
    asyncio.run(outbox.scan_once())
    assert len(writes) == 20
    asyncio.run(outbox.scan_once())
    assert len(writes) == 23
    asyncio.run(outbox.scan_once())
    assert len(writes) == 23
    found = index.search('piano', 'local-user')
    assert found[0].occurred_at.isoformat() == '2024-01-02T10:00:00+08:00'
    assert found[0].metadata['history_actor'] == 'user'


def test_missing_timestamp_remains_unknown_and_timestamp_correction_is_indexed(tmp_path):
    index = SourceRetrieval(tmp_path / 'original.sqlite3')
    index.put('alice', 'history:1', 'piano today', '', None)
    assert index.search('piano', 'alice')[0].occurred_at is None
    stamp = datetime(2024, 1, 2, tzinfo=timezone.utc)
    index.put('alice', 'history:1', 'piano today', '', stamp)
    assert index.search('piano', 'alice')[0].occurred_at == stamp


def test_prompt_explains_source_time_not_import_time(tmp_path):
    from runtime.memory.memory_prompt import MemoryPromptBuilder
    from runtime.memory.memory_port import MemoryRecord, LEGACY_LETTERS
    from types import SimpleNamespace
    record = MemoryRecord(memory_id='old', domain=LEGACY_LETTERS, text='today piano',
                          source='archive', created_at=0,
                          provenance={'occurred_at': '2024-01-02T10:00:00+08:00'})
    archive = SimpleNamespace(search=lambda *a, **kw: [record], status=lambda: {'status': 'available'})
    prompt = MemoryPromptBuilder(archive, conversation_memory=None).build('piano')
    assert '2024-01-02T10:00:00+08:00' in prompt.text
    assert 'relative to the source timestamp' in prompt.text


def test_existing_backup_reaches_real_retrieval_without_reimport_or_model_write(tmp_path):
    from runtime.imports.letter_backup import export_letters, import_letters
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
    from tests.memory.test_mem0_memory import FakeMem0, _config
    backend = FakeMem0()
    memory = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as archive:
        import_letters(export_letters([{'letter_id': 'old', 'content': 'piano has an orange snail sticker',
                       'reply_text': 'remember the sticker', 'created_at': '2024-01-02T10:00:00+08:00'}]),
                       adapter=archive)
        committer = ConversationMemoryDeliveryCommitter(memory)
        outbox = CanonicalMemoryOutbox(tmp_path / 'state.json', tmp_path / 'outbox.sqlite3',
                                      committer, archive_memory=archive)
        asyncio.run(outbox.scan_once())
        found = memory.search_evidence_context('snail', user_id='local-user', limit=5)
        assert 'orange snail' in found[0].text
        assert found[0].occurred_at.isoformat() == '2024-01-02T10:00:00+08:00'
        prompt = CompanionMemoryPromptBuilder(archive, memory).build('snail')
        assert 'orange snail' in prompt.text
        assert '2024-01-02T10:00:00+08:00' in prompt.text
        assert not any(method == 'add' for method, _ in backend.calls)
        # Forgetting survives a worker restart; backfill cannot restore the source.
        memory._originals.forget('local-user', found[0].source_id)
        restarted = CanonicalMemoryOutbox(tmp_path / 'state.json', tmp_path / 'outbox.sqlite3',
                                         ConversationMemoryDeliveryCommitter(memory), archive_memory=archive)
        asyncio.run(restarted.scan_once())
        assert not memory._originals.search('snail', 'local-user')


def test_offline_unknown_date_backfill_obeys_pause(tmp_path):
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.memory_port import LegacyLetter
    from tests.memory.test_mem0_memory import FakeMem0, _config
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    class Lifecycle:
        paused = True
        def run_write(self, operation, **kwargs):
            return None if self.paused else operation()
    lifecycle = Lifecycle()
    memory = Mem0ConversationMemoryAdapter(FakeMem0(), _config(tmp_path))
    with LocalMemoryAdapter(tmp_path / 'archive.sqlite3') as archive:
        archive.import_legacy_records([LegacyLetter(content='old pair', source_record_id='offline-letter-pairs:old:1',
            source='offline-letter-pairs', metadata={'import_kind': 'offline_recovered_text_reply',
            'user_content': 'snail sticker', 'reply_text': 'yes'})])
        outbox = CanonicalMemoryOutbox(tmp_path / 'state.json', tmp_path / 'outbox.sqlite3',
            ConversationMemoryDeliveryCommitter(memory, memory_lifecycle=lifecycle), archive_memory=archive)
        asyncio.run(outbox.scan_once())
        assert not memory._originals.search('snail', 'local-user')
        lifecycle.paused = False
        asyncio.run(outbox.scan_once())
        found = memory._originals.search('snail', 'local-user')
        assert found[0].occurred_at is None
