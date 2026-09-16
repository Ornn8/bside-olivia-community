import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from gpu_service.result_store import ResultStore
from gpu_service.server import build_app


class PrivateBucket:
    def upload_file(self, source, bucket, key, ExtraArgs):
        assert 'ACL' not in ExtraArgs
        self.size=Path(source).stat().st_size
        self.metadata=ExtraArgs['Metadata']
    def head_object(self, **kwargs):return {'ContentLength':self.size,'Metadata':self.metadata}
    def generate_presigned_url(self, operation, Params, ExpiresIn):
        assert operation=='get_object' and ExpiresIn==900
        return 'https://private.example/result?signature=synthetic'


def test_verified_private_upload_and_owner_only_signed_url(tmp_path, monkeypatch):
    fake=PrivateBucket();store=ResultStore({'bucket':'private','prefix':'results'},client=fake)
    config={'public_url':'https://gpu.example','signing_key':'s'*32,
        'tokens':{'alice':'a'*32,'bob':'b'*32},'profiles':{},'gpus':[],
        'result_store':{'bucket':'private'}}
    monkeypatch.setattr('gpu_service.result_store.ResultStore',lambda _:store)
    app=build_app(config,tmp_path)
    job=tmp_path/'jobs/completed';job.mkdir();(job/'output.bin').write_bytes(b'audio')
    store.publish('completed','tts',job)
    receipt=json.loads((job/'r2-result.json').read_text())
    assert receipt['key']=='results/completed.wav'
    with sqlite3.connect(tmp_path/'tasks.sqlite3') as db:
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',('completed','alice','request','hash','tts','{}','succeeded',0,1))
    async def scenario():
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/v1/tasks/completed')).status==401
            assert (await client.get('/v1/tasks/completed',headers={'Authorization':'Bearer '+'b'*32})).status==404
            response=await client.get('/v1/tasks/completed',headers={'Authorization':'Bearer '+'a'*32})
            assert (await response.json())['outputs']==[{'url':'https://private.example/result?signature=synthetic'}]
    asyncio.run(scenario())


def test_unverified_upload_never_publishes_receipt(tmp_path):
    fake=PrivateBucket()
    fake.head_object=lambda **kwargs:{'ContentLength':0,'Metadata':{}}
    store=ResultStore({'bucket':'private'},client=fake)
    (tmp_path/'output.bin').write_bytes(b'audio')
    with pytest.raises(ValueError,match='RESULT_UPLOAD_INVALID'):store.publish('task','tts',tmp_path)
    assert not (tmp_path/'r2-result.json').exists()
