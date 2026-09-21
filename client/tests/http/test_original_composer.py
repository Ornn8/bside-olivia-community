import asyncio
import pytest
from runtime.video_reply_settings import VideoReplySettingsStore, REPLY_ROUTES

@pytest.mark.parametrize('output',['audio','video'])
def test_original_composer_preserves_options_and_explicit_route(tmp_path,monkeypatch,output):
    import local_server as server
    settings=VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate_tier('video_reply_setting:original','video')
    monkeypatch.setattr(server,'video_reply_settings_store',settings)
    monkeypatch.setattr(server.store,'letters',[])
    monkeypatch.setattr(server.store,'request_keys',{})
    monkeypatch.setattr(server,'_reply_route_previews',{})
    monkeypatch.setattr(server,'_persist_store_state',lambda:None)
    monkeypatch.setattr(server,'_schedule_reply_job',lambda *a,**k:None)
    monkeypatch.setattr(server,'_route_readiness',lambda *a,**k:dict.fromkeys(REPLY_ROUTES,True))
    monkeypatch.setattr(server,'_classify_managed_route',lambda *a:pytest.fail('explicit composer must not need model guess'))
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT',str(tmp_path))
    async def scenario():
        options={'caption':'gentle guitar','guidance_scale':7,'use_cot':True,'thinking':False}
        body={'content':'请原创演唱秋天。','original_output':output,'music_options':options}
        preview=await server.route('POST','/toy/letter/route-preview',body,{})
        assert preview['code']==0,preview
        preview=preview['data']
        assert preview['requested_route']=='singing_video'
        assert preview['video_enabled']==(output=='video')
        material={k:v for k,v in body.items() if k!='content'}
        material['route_preview_token']=preview['token']
        changed={**material,'music_options':{**options,'guidance_scale':9}}
        bad=await server.route('POST','/toy/letter/send',{'content':body['content'],'material':changed},{},defer_reply=True)
        assert bad['code']==409 and not server.store.letters
        result=await server.route('POST','/toy/letter/send',{'content':body['content'],'material':material},{},defer_reply=True)
        assert result['code']==0,result
        letter=server.store.letters[0]
        assert letter['material']['music_options']==options
        assert letter['route_preflight']['music_intent']=='compose'
    asyncio.run(scenario())
