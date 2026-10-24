import asyncio
from copy import deepcopy
import hashlib

from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import pytest

from runtime import image_assets as assets,wardrobe
from runtime.cloud_service import CloudError
from runtime.remote_generation import RemoteGeneration


RAW=b'\x89PNG\r\n\x1a\nsynthetic-future-outfit'


def state():
    sha=hashlib.sha256(RAW).hexdigest()
    entry={'filename':'rainwear-01.png','sha256':sha,'size_bytes':len(RAW),'content_type':'image/png',
           'key':f'distribution/olivia-images/wardrobe/{sha}/rainwear-01.png'}
    return {'catalog_protocol':'dynamic-v1','catalog_version':'20261009-aaaaaaaaaaaa',
        'date':'2026-10-09','timezone':'Asia/Shanghai','wardrobe':{'style_id':'rainwear','preference_revision':1},
        'daily_outfit':{'date':'2026-10-09','timezone':'Asia/Shanghai','style_id':'rainwear','look_id':'rainwear-01',
                        'catalog_version':'20261009-aaaaaaaaaaaa','reference_sha256':sha,'preference_revision':1},
        'purchases':{'owned':['rainwear-01'],'free_limit':3,'free_remaining':2,'price_cents':500},
        'wardrobe_styles':[{'style_id':'original','label':'原版','description':'','looks':[]},
            {'style_id':'rainwear','label':'雨日','description':'新的云端分类','looks':[
                {'look_id':'rainwear-01','label':'雨后散步','image_asset':entry}]}]}


def test_future_catalog_get_set_buy_and_download_survives_restart(tmp_path,monkeypatch):
    async def scenario():
        data=state();entry=data['wardrobe_styles'][1]['looks'][0]['image_asset'];calls=[]
        async def catalog(request):
            assert request.headers['X-Olivia-Wardrobe-Catalog']=='dynamic-v1'
            if request.method=='POST': calls.append(await request.json())
            return web.json_response(data)
        async def ticket(request):
            calls.append('ticket')
            return web.json_response({**entry,'url':str(client.make_url('/object'))})
        async def obj(request):
            assert 'Authorization' not in request.headers
            calls.append('object');return web.Response(body=RAW)
        app=web.Application();app.router.add_get('/v1/wardrobe',catalog);app.router.add_post('/v1/wardrobe',catalog)
        app.router.add_get('/v1/components/images/wardrobe/rainwear-01',ticket);app.router.add_get('/object',obj)
        monkeypatch.setattr(assets,'_validate_download_url',lambda *_:None)
        async with TestClient(TestServer(app)) as client:
            base=str(client.make_url('')).rstrip('/')
            api=RemoteGeneration(base,'synthetic-fixture')
            response=await api.request('wardrobe_get',{})
            assets.save_wardrobe_catalog(tmp_path,response)
            assert calls==[] and not list(tmp_path.rglob('*.png'))
            assert await api.request('wardrobe_set',{'style_id':'rainwear','request_id':'choose-rain'})==data
            assert await api.request('wardrobe_buy',{'look_id':'rainwear-01','request_id':'buy-rain','max_charge_cents':0})==data
            target=await assets.ensure_image(tmp_path,'wardrobe','rainwear-01',base_url=base)
            assert target.read_bytes()==RAW
        assert assets.image_entry(tmp_path,'wardrobe','rainwear-01')==entry
        assert await assets.ensure_image(tmp_path,'wardrobe','rainwear-01',base_url='http://127.0.0.1:1')==target
        assert calls[-2:]==['ticket','object']
    asyncio.run(scenario())


def test_local_wardrobe_route_saves_cloud_manifest_before_returning_images(tmp_path,monkeypatch):
    import local_server
    class API:
        def __init__(self,*_):pass
        async def request(self,*_):return state()
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    monkeypatch.setattr(local_server,'_local_data_root',lambda:tmp_path)
    result=asyncio.run(local_server.route('GET','/toy/world/wardrobe',{},{}))
    look=result['data']['wardrobe_styles'][1]['looks'][0]
    assert look['image_url']=='http://127.0.0.1:8899/toy/wardrobe/images/rainwear-01'
    assert assets.image_entry(tmp_path,'wardrobe','rainwear-01')==look['image_asset']
    assert not list(tmp_path.rglob('*.png'))


@pytest.mark.parametrize('mutation',['path','key','hash','size','duplicate','category','daily_hash'])
def test_dynamic_catalog_rejects_malformed_entries_before_cache(tmp_path,mutation):
    data=state();look=data['wardrobe_styles'][1]['looks'][0];entry=look['image_asset']
    if mutation=='path':entry['filename']='../../secret.png'
    if mutation=='key':entry['key']='private/secret'
    if mutation=='hash':entry['sha256']='g'*64
    if mutation=='size':entry['size_bytes']=16*1024*1024
    if mutation=='duplicate':data['wardrobe_styles'][1]['looks'].append(deepcopy(look))
    if mutation=='category':data['wardrobe_styles'][1]['style_id']='../rainwear'
    if mutation=='daily_hash':data['daily_outfit']['reference_sha256']='b'*64
    with pytest.raises((ValueError,CloudError)):assets.save_wardrobe_catalog(tmp_path,data)
    assert list(tmp_path.iterdir())==[]


PREVIEW=b'RIFF\x10\x00\x00\x00WEBPsynthetic-preview'


def with_preview(data):
    sha=hashlib.sha256(PREVIEW).hexdigest()
    data['wardrobe_styles'][1]['looks'][0]['preview_asset']={'filename':'rainwear-01-preview.webp','sha256':sha,
        'size_bytes':len(PREVIEW),'content_type':'image/webp',
        'key':f'distribution/olivia-images/wardrobe/{sha}/rainwear-01-preview.webp'}
    data['pinned_look']='rainwear-01'
    return data


def test_the_page_shows_the_small_preview_and_photos_keep_the_original(tmp_path,monkeypatch):
    import local_server
    class API:
        def __init__(self,*_):pass
        async def request(self,*_):return with_preview(state())
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    monkeypatch.setattr(local_server,'_local_data_root',lambda:tmp_path)
    result=asyncio.run(local_server.route('GET','/toy/world/wardrobe',{},{}))
    look=result['data']['wardrobe_styles'][1]['looks'][0]
    assert look['image_url']=='http://127.0.0.1:8899/toy/wardrobe/images/rainwear-01-preview'
    assert assets.image_entry(tmp_path,'wardrobe','rainwear-01-preview')==look['preview_asset']
    assert assets.image_entry(tmp_path,'wardrobe','rainwear-01')==look['image_asset']
    assert result['data']['pinned_look']=='rainwear-01'


def test_he_can_dress_her_in_one_look_he_owns(tmp_path,monkeypatch):
    async def scenario():
        data=with_preview(state());posted=[]
        async def catalog(request):
            posted.append(await request.json())
            return web.json_response(data)
        app=web.Application();app.router.add_post('/v1/wardrobe',catalog)
        async with TestClient(TestServer(app)) as client:
            api=RemoteGeneration(str(client.make_url('')).rstrip('/'),'synthetic-fixture')
            assert await api.request('wardrobe_pin',{'look_id':'rainwear-01','request_id':'wear-rain'})==data
            for bad in ({'look_id':'../x','request_id':'wear'},{'look_id':'rainwear-01','request_id':'wear','extra':1}):
                with pytest.raises(CloudError):
                    await api.request('wardrobe_pin',bad)
        assert posted==[{'look_id':'rainwear-01','request_id':'wear-rain'}]
    asyncio.run(scenario())


def test_the_local_route_passes_a_wear_request_on(tmp_path,monkeypatch):
    import local_server
    seen=[]
    class API:
        def __init__(self,*_):pass
        async def request(self,action,body):
            seen.append((action,body));return with_preview(state())
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    monkeypatch.setattr(local_server,'_local_data_root',lambda:tmp_path)
    body={'request_id':'wear-rain','look_id':'rainwear-01'}
    result=asyncio.run(local_server.route('POST','/toy/world/wardrobe',body,{},companion_confirmed=True))
    assert result['data']['pinned_look']=='rainwear-01' and seen==[('wardrobe_pin',body)]


@pytest.mark.parametrize('mutation',['preview_key','pinned'])
def test_a_malformed_preview_or_pin_is_rejected_before_cache(tmp_path,mutation):
    data=with_preview(state())
    if mutation=='preview_key':data['wardrobe_styles'][1]['looks'][0]['preview_asset']['key']='private/secret'
    if mutation=='pinned':data['pinned_look']='../x'
    with pytest.raises((ValueError,CloudError)):assets.save_wardrobe_catalog(tmp_path,data)
    assert list(tmp_path.iterdir())==[]
