import asyncio
from copy import deepcopy
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from runtime.cloud_service import CloudError
from runtime.remote_generation import RemoteGeneration
from runtime.wardrobe import DAILY_CATALOG,DAILY_STYLES


def state():
    return {'catalog_version':DAILY_CATALOG,'date':'2026-10-04','timezone':'Asia/Shanghai',
            'wardrobe':{'style_id':'dark','preference_revision':2},
            'daily_outfit':{'date':'2026-10-04','timezone':'Asia/Shanghai','style_id':'dark','look_id':'dark-01',
                'preference_revision':2,'catalog_version':DAILY_CATALOG,'reference_sha256':'a'*64},
            'wardrobe_styles':[{'style_id':s,'label':s,'description':s,'looks':[]} for s in DAILY_STYLES]}


def test_cloud_preference_sends_only_style_and_validates_server_snapshot():
    async def scenario():
        calls=[]
        async def handler(request):
            assert request.headers['Authorization']=='Bearer synthetic'
            calls.append(await request.json())
            return web.json_response(state())
        app=web.Application();app.router.add_post('/v1/wardrobe',handler)
        async with TestServer(app) as server:
            api=RemoteGeneration(str(server.make_url('/')),'synthetic')
            result=await api.request('wardrobe_set',{'request_id':'style-unique','style_id':'dark'})
            assert result['daily_outfit']['look_id']=='dark-01'
            assert calls==[{'request_id':'style-unique','style_id':'dark'}]
            with pytest.raises(CloudError):
                await api.request('wardrobe_set',{'request_id':'style-unique','style_id':'dark','look_id':'dark-01'})
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value',[('style_id','mori'),('look_id','dark-99'),('preference_revision',1),('reference_sha256','invalid'),('catalog_version','other')])
def test_server_task_rejects_invalid_or_mismatched_outfit(field,value):
    async def scenario():
        result=state();result['daily_outfit'][field]=value
        async def handler(request):return web.json_response(result)
        app=web.Application();app.router.add_get('/v1/wardrobe',handler)
        async with TestServer(app) as server:
            with pytest.raises(CloudError,match='GPU_RESPONSE_INVALID'):
                await RemoteGeneration(str(server.make_url('/')),'synthetic').request('wardrobe_get',{})
    asyncio.run(scenario())


def test_local_proxy_confirmation_and_cloud_failure_does_not_change_local_preferences(monkeypatch,tmp_path):
    import local_server
    from runtime.video_reply_settings import VideoReplySettingsStore
    store=VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(local_server,'video_reply_settings_store',store)
    class API:
        def __init__(self,*args):pass
        async def request(self,action,payload):
            if action=='wardrobe_set':raise CloudError('GPU_CONNECTION_FAILED')
            return state()
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    async def scenario():
        body={'request_id':'style-unique','style_id':'dark'}
        denied=await local_server.route('POST','/toy/world/wardrobe',body,{})
        assert denied['code']==403
        failed=await local_server.route('POST','/toy/world/wardrobe',body,{},companion_confirmed=True)
        assert failed['code']!=0
        assert store.wardrobe_snapshot()['style_id']=='original'
        result=await local_server.route('GET','/toy/world/wardrobe',{}, {})
        assert result['data']['wardrobe']['style_id']=='dark'
    asyncio.run(scenario())


def test_remote_photo_accepts_readonly_daily_metadata_and_legacy_tasks():
    async def scenario():
        outfit=state()['daily_outfit']
        plan={'prompt':'Synthetic portrait','photo_type':'selfie','room':'none','time_of_day':'night','daily_outfit':outfit}
        async def handler(request):
            assert request.headers.get('X-Olivia-Wardrobe-Protocol')=='daily-v1'
            return web.json_response({'task_id':'photo','status':'succeeded','media_plan':plan})
        app=web.Application();app.router.add_get('/v1/tasks/photo',handler)
        app.router.add_post('/v1/tasks/photo/cancel',handler)
        app.router.add_post('/v1/tasks',handler)
        async with TestServer(app) as server:
            api=RemoteGeneration(str(server.make_url('/')),'synthetic')
            assert (await api.request('status',{'task_id':'photo'}))['media_plan']['daily_outfit']==outfit
            assert (await api.request('submit',{'request_id':'photo-replay','kind':'image','input':{}}))['media_plan']['daily_outfit']==outfit
            assert (await api.request('cancel',{'task_id':'photo'}))['media_plan']['daily_outfit']==outfit
            plan.pop('daily_outfit')
            assert 'daily_outfit' not in (await api.request('status',{'task_id':'photo'}))['media_plan']
            plan['photo_type']='snapshot';plan['daily_outfit']=outfit
            with pytest.raises(CloudError):await api.request('status',{'task_id':'photo'})
    asyncio.run(scenario())


@pytest.mark.parametrize('format',['PNG','WEBP'])
def test_cloud_reference_preview_and_local_proxy_keep_correct_media_type(format,monkeypatch):
    from io import BytesIO
    from PIL import Image
    from aiohttp.test_utils import TestClient
    import local_server
    output=BytesIO(); Image.new('RGB',(32,48)).save(output,format=format); raw=output.getvalue()
    async def scenario():
        async def image(request):
            assert request.headers['Authorization']=='Bearer synthetic'
            return web.Response(body=raw)
        app=web.Application();app.router.add_get('/v1/wardrobe/images/mori-01',image)
        async with TestServer(app) as server:
            api=RemoteGeneration(str(server.make_url('/')),'synthetic')
            assert await api.wardrobe_image('mori-01')==raw
            with pytest.raises(CloudError):await api.wardrobe_image('../secret')
            class API:
                def __init__(self,*args):pass
                async def wardrobe_image(self,look_id):return await api.wardrobe_image(look_id)
            monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
            proxy=web.Application();proxy.router.add_get('/toy/wardrobe/images/mori-01',local_server.handler)
            async with TestClient(TestServer(proxy)) as client:
                response=await client.get('/toy/wardrobe/images/mori-01')
                assert response.status==200 and response.headers['Content-Type']=='image/'+format.lower()
                assert await response.read()==raw
    asyncio.run(scenario())
