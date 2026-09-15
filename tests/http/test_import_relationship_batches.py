import asyncio
import pytest
from types import SimpleNamespace
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService
from runtime.imports.letter_backup import export_letters
from runtime.imports.relationship_batches import RelationshipBatches
from runtime.memory.local_memory import LocalMemoryAdapter
from tests.imports.test_relationship_batches import Gateway


@pytest.mark.parametrize('start_mode',['import','startup'])
def test_backup_route_queues_ordered_relationships_and_reports_progress(tmp_path,monkeypatch,start_mode):
    import local_server as server
    archive=LocalMemoryAdapter(tmp_path/'archive.sqlite3')
    ledger=SQLitePrivateWorldLedger(tmp_path/'world.sqlite3')
    gateway=Gateway()
    monkeypatch.setattr(server,'_history_relationship_queue',
        RelationshipBatches(tmp_path/'history-relationship.sqlite3') if start_mode=='import' else None)
    monkeypatch.setattr(server,'_state_root',lambda:tmp_path)
    monkeypatch.setattr(server,'_start_conversation_memory_initialization',lambda _loop:True)
    monkeypatch.setattr(server,'_history_relationship_task',None)
    monkeypatch.setattr(server,'_legacy_import_adapter',lambda:archive)
    monkeypatch.setattr(server,'memory_adapter',archive)
    monkeypatch.setattr(server,'private_world_command_service',PrivateWorldCommandService(ledger))
    monkeypatch.setattr(server,'private_world_port',ledger)
    monkeypatch.setattr(server,'letters_adapter',SimpleNamespace(gateway=gateway,get_persona_policy=lambda:'policy'))
    monkeypatch.setattr(server,'_llm_runtime_ready',lambda:True)
    monkeypatch.setattr(server,'store',SimpleNamespace(letters=[],legacy_letters=[]))
    monkeypatch.setattr(server,'_mark_superseded_failed_retries',lambda:None)
    backup=export_letters([{'content':f'第{i}封','reply_text':f'收到第{i}封','letter_id':str(i)} for i in range(12)])
    async def run():
        for _ in range(2):
            result=await server.route('POST','/toy/letter/backup/import',{'backup':backup},{},companion_confirmed=True)
            assert result['data']['status']=='APPLIED'
            if server._history_relationship_task is None:
                await server._start_conversation_memory(None)
            await server._history_relationship_task
        response=await server.route('GET','/toy/letter/legacy/local-import',{}, {'relationship':'1'})
        assert response['data']['processed']==12 and response['data']['status']=='APPLIED'
        assert [len(x['ordered_exchanges']) for x in gateway.calls]==[5,5,2]
        assert [x['user_letter'] for batch in gateway.calls for x in batch['ordered_exchanges']]==[f'第{i}封' for i in range(12)]
    try:asyncio.run(run())
    finally:archive.close()
