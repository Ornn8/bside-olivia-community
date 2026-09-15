import asyncio
import pytest
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from original_client_companion_api import MEMORY_PATH, mount_original_companion_read_api
from original_client_companion_backend import OriginalClientCompanionServiceBackend
from runtime.memory.conversation_memory_admin import ConversationMemoryAdminService
from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
from tests.memory.test_mem0_memory import FakeMem0, _config


def test_multiline_permission_does_not_weaken_other_text_validation():
    from original_client_companion_api import _text
    with pytest.raises(ValueError):
        _text('query\nnext',maximum=100,code='QUERY_INVALID')
    for character in ('\x00','\x1b','\x7f'):
        with pytest.raises(ValueError):
            _text('letter'+character+'text',maximum=100,code='ORIGINAL_TEXT_INVALID',multiline=True)


@pytest.mark.parametrize('query', ['', '小离'])
def test_multiline_original_letter_survives_real_index_and_http(tmp_path, query):
    memory=Mem0ConversationMemoryAdapter(FakeMem0(),_config(tmp_path))
    body='小离：\r\n\r\n今天去了咖啡店。\n\t记得那杯开心果拿铁吗？\r\n署名'
    memory.index_original_exchange(user_id='local-user',source_id='history:multiline',
        user_message=body,assistant_message='记得。\n下次一起去。',occurred_at=datetime(2026,9,1,tzinfo=timezone.utc))
    admin=ConversationMemoryAdminService(memory,tmp_path/'admin.sqlite3',user_id='local-user')
    async def exercise():
        app=web.Application()
        mount_original_companion_read_api(app,OriginalClientCompanionServiceBackend(memory_admin=admin),trusted_origins=['https://client.example'])
        async with TestClient(TestServer(app)) as client:
            response=await client.get(MEMORY_PATH,params={'collection':'originals','query':query},headers={'Origin':'https://client.example'})
            assert response.status==200,await response.text()
            payload=await response.json()
            assert next(row['text'] for row in payload['originals'] if row['speaker']=='user')==body
    asyncio.run(exercise())


def test_original_search_is_separate_read_only_and_origin_protected(tmp_path):
    memory=Mem0ConversationMemoryAdapter(FakeMem0(),_config(tmp_path))
    stamp=datetime(2026,9,1,tzinfo=timezone.utc)
    memory.index_original_exchange(user_id='local-user',source_id='history:fixture',
        user_message='我喜欢开心果冰淇淋。',assistant_message='我记得。',occurred_at=stamp)
    memory.register_archive_sources(user_id='local-user',sources=['history:fixture','history:pending'])
    admin=ConversationMemoryAdminService(memory,tmp_path/'admin.sqlite3',user_id='local-user')
    async def exercise():
        app=web.Application()
        mount_original_companion_read_api(app,OriginalClientCompanionServiceBackend(memory_admin=admin),
                                          trusted_origins=['https://client.example'])
        async with TestClient(TestServer(app)) as client:
            params={'query':'冰淇淋','collection':'originals'}
            response=await client.get(MEMORY_PATH,params=params,headers={'Origin':'https://untrusted.example'})
            assert response.status==403
            response=await client.get(MEMORY_PATH,params=params,headers={'Origin':'https://client.example'})
            assert response.status==200
            payload=await response.json()
            assert payload['archive_total']==2 and payload['archive_indexed']==1
            assert payload['originals'][0]['speaker']=='user'
            assert payload['originals'][0]['created_at']==stamp.isoformat()
            assert '冰淇淋' in payload['originals'][0]['text']
            response=await client.get(MEMORY_PATH,params={'query':'冰淇淋'},headers={'Origin':'https://client.example'})
            assert (await response.json())['memories']==[]
            memory._originals.browse=lambda *a,**kw: (_ for _ in ()).throw(OSError('synthetic private path'))
            response=await client.get(MEMORY_PATH,params=params,headers={'Origin':'https://client.example'})
            assert response.status==503
            assert 'synthetic private path' not in await response.text()
    asyncio.run(exercise())
