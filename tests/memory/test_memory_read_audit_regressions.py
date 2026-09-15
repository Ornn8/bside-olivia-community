import asyncio
import sqlite3
import pytest
from tests.memory.test_mem0_memory import FakeMem0, _config
from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
from runtime.memory.conversation_memory_admin import ConversationMemoryAdminService, ConversationMemoryAdminError


def test_failed_management_search_is_not_a_successful_empty_result(tmp_path):
    provider = FakeMem0()
    provider.fail.add('search')
    memory = Mem0ConversationMemoryAdapter(provider, _config(tmp_path))
    admin = ConversationMemoryAdminService(memory, tmp_path/'audit.sqlite3', user_id='local-user')
    with pytest.raises(ConversationMemoryAdminError, match='MEMORY_ADMIN_READ_FAILED'):
        admin.list_memories(query='piano', limit=50)


def test_fallback_still_excludes_recent_sources(tmp_path):
    provider = FakeMem0()
    provider.rows=[{'id':'memory.test', 'memory':'synthetic piano', 'user_id':'local-user', 'agent_id':'linli',
                    'metadata':{'domain':'conversation_memory','source_id':'reply:excluded:1'}}]
    memory = Mem0ConversationMemoryAdapter(provider, _config(tmp_path))
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic index failure')
    memory._originals.search=unavailable
    assert memory.search_evidence_context('piano',user_id='local-user',limit=5,
        exclude_source_ids=('reply:excluded:1',)) == ()


def test_84_imported_originals_are_searchable_with_zero_extracted_facts(tmp_path):
    from runtime.memory.local_memory import LocalMemoryAdapter
    from runtime.memory.memory_port import LegacyLetter
    from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
    from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
    provider=FakeMem0()
    memory=Mem0ConversationMemoryAdapter(provider, _config(tmp_path))
    with LocalMemoryAdapter(tmp_path/'archive.sqlite3') as archive:
        rows=[LegacyLetter(content=f'original {i}',source='offline-letter-pairs',source_record_id=f'offline-letter-pairs:{i}',
              metadata={'import_kind':'offline_recovered_text_reply','user_content':f'piano detail{i}', 'reply_text':'remembered'}) for i in range(84)]
        assert archive.import_legacy_records(rows,atomic=True).inserted == 84
        assert archive.import_legacy_records(rows,atomic=True).duplicates == 84
        outbox=CanonicalMemoryOutbox(tmp_path/'state.json',tmp_path/'outbox.sqlite3',
            ConversationMemoryDeliveryCommitter(memory),archive_memory=archive)
        assert memory.browse_originals(user_id='local-user',query='',limit=20)['archive_total'] is None
        for step in range(5):
            asyncio.run(outbox.scan_once())
            progress=memory.browse_originals(user_id='local-user',query='',limit=20)
            assert progress['archive_total']==84
            assert progress['archive_indexed']==min(84,(step+1)*20)
        assert memory.status().memory_count == 0
        with sqlite3.connect(memory._originals.path) as db:
            assert db.execute('SELECT COUNT(DISTINCT source) FROM originals').fetchone()[0] == 84
        found=memory.search_evidence_context('detail83',user_id='local-user',limit=5)
        assert any('detail83' in row.text for row in found)
        admin=ConversationMemoryAdminService(memory,tmp_path/'admin.sqlite3',user_id='local-user')
        view=admin.browse_originals(query='detail83',limit=20)
        assert any('detail83' in row['text'] for row in view['originals'])
        assert admin.list_memories(query='detail83',limit=20)==()
        assert memory.browse_originals(user_id='another-user',query='detail83',limit=20)['originals']==[]
        source=view['originals'][0]['source_id']
        memory._originals.forget('local-user',source)
        after=admin.browse_originals(query='detail83',limit=20)
        assert after['originals']==[] and after['archive_removed']==1 and after['archive_indexed']==83
        memory=Mem0ConversationMemoryAdapter(provider,_config(tmp_path))
        assert memory.browse_originals(user_id='local-user',query='',limit=20)['archive_removed']==1
        assert not any(method=='add' for method,_ in provider.calls)
