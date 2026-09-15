"""Exercise the user import route through archive backfill to the UI HTTP read."""
import asyncio
import json
from types import SimpleNamespace
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from original_client_companion_api import MEMORY_PATH,mount_original_companion_read_api
from original_client_companion_backend import OriginalClientCompanionServiceBackend
from runtime.imports.letter_backup import export_letters
from runtime.memory.local_memory import LocalMemoryAdapter
from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
from runtime.memory.conversation_memory_admin import ConversationMemoryAdminService
from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from runtime.memory.companion_memory_context import CompanionMemoryPromptBuilder
from tests.memory.test_mem0_memory import FakeMem0,_config


def test_import_backfill_restart_then_search_multiline_originals(tmp_path,monkeypatch):
    import local_server as server
    archive=LocalMemoryAdapter(tmp_path/'archive.sqlite3')
    provider=FakeMem0()
    config=_config(tmp_path)
    memory=Mem0ConversationMemoryAdapter(provider,config)
    source=tmp_path/'letter_pairs.json'
    source.write_text(json.dumps([{'content':f'小离：\r\n第 {i} 封信。\n今天散步经过了小桥。',
                                  'reply':f'收到了第 {i} 封。\n下次再聊。'} for i in range(84)],ensure_ascii=False),encoding='utf-8')
    monkeypatch.setattr(server,'_default_offline_letter_pair_source',lambda:source)
    monkeypatch.setattr(server,'_legacy_import_adapter',lambda:archive)
    monkeypatch.setattr(server,'memory_adapter',archive)
    monkeypatch.setattr(server,'store',SimpleNamespace(letters=[],legacy_letters=[]))
    monkeypatch.setattr(server,'_mark_superseded_failed_retries',lambda:None)
    bodies={
        '杯底':'小离：\r\n\r\n那只白杯子的杯底画着蓝色纸鹤。\n\t别忘了这个小图案。',
        '票根':'小离：\n我把旧票根夹在第七十三页。\r\n书里还有一片银杏叶。',
        '纽扣':'小离：\r\n外婆留下的围巾边上缝着一颗铜纽扣。\n落款',
    }
    stamps={key:1788000000+i*86400 for i,key in enumerate(bodies)}
    backup=export_letters([{'letter_id':f'detail-{i}','content':body,'reply_text':'我记得。\n谢谢你告诉我。',
                           'created_at':stamps[key],'origin':'user','reply_mode':'text_letter'} for i,(key,body) in enumerate(bodies.items())])
    async def run():
        result=await server.route('POST','/toy/letter/legacy/local-import',{'originals_only':True},{},companion_confirmed=True)
        assert result['data']['inserted']==84 and result['data']['provider_calls']==0
        duplicate=await server.route('POST','/toy/letter/legacy/local-import',{'originals_only':True},{},companion_confirmed=True)
        assert duplicate['data']['duplicates']==84
        result=await server.route('POST','/toy/letter/backup/import',{'backup':backup},{},companion_confirmed=True)
        assert result['data']['inserted']==3 and result['data']['provider_calls']==0
        outbox=CanonicalMemoryOutbox(tmp_path/'state.json',tmp_path/'outbox.sqlite3',ConversationMemoryDeliveryCommitter(memory),archive_memory=archive)
        for _ in range(5):await outbox.scan_once()
        # Reopen the index before the HTTP read, rather than accepting in-memory state.
        reopened=Mem0ConversationMemoryAdapter(provider,config)
        builder=CompanionMemoryPromptBuilder(archive,reopened)
        for question,detail in (
            ('我的杯底画着什么？','蓝色纸鹤'),
            ('我把旧票根夹在哪里了？','第七十三页'),
            ('外婆留下的围巾上有什么？','铜纽扣'),
        ):
            prompt=builder.build(question)
            assert detail in prompt.text,(question,prompt.text)
            assert len(prompt.text)<=2400
            assert '2026-' in prompt.text
        admin=ConversationMemoryAdminService(reopened,tmp_path/'admin.sqlite3',user_id='local-user')
        app=web.Application()
        mount_original_companion_read_api(app,OriginalClientCompanionServiceBackend(memory_admin=admin),trusted_origins=['https://client.example'])
        async with TestClient(TestServer(app)) as client:
            for query in ('','小离',*bodies):
                response=await client.get(MEMORY_PATH,params={'collection':'originals','query':query},headers={'Origin':'https://client.example'})
                assert response.status==200,(query,await response.text())
                payload=await response.json()
                assert payload['archive_total']==87 and payload['archive_indexed']==87
                assert payload['originals'],query
                if query in bodies:
                    row=next(row for row in payload['originals'] if row['speaker']=='user')
                    assert row['text']==bodies[query]
                    assert datetime.fromisoformat(row['created_at']).timestamp()==stamps[query]
            assert reopened.browse_originals(user_id='other-user',query='杯底',limit=20)['originals']==[]
        assert not any(method=='add' for method,_ in provider.calls)
    try:asyncio.run(run())
    finally:archive.close()
