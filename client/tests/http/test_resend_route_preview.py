import asyncio
import pytest
from letter_triage import TriageResult
from runtime.video_reply_settings import VideoReplySettingsStore, REPLY_ROUTES


@pytest.mark.parametrize('extra,code,error', [
    ({'letter_status': 'PROCESSING'}, 409, 'LETTER_IN_PROGRESS'),
    ({'letter_status': 'PENDING'}, 409, 'LETTER_IN_PROGRESS'),
    ({'letter_status': 'FAILED', 'superseded_by': 'replacement'}, 410, 'LETTER_SUPERSEDED'),
])
def test_resend_preview_reports_existing_attempt_before_route_or_charge(monkeypatch, extra, code, error):
    import local_server as server
    original = {'letter_id': 'old', 'content': 'synthetic', **extra}
    monkeypatch.setattr(server.store, 'letters', [original])
    async def classify(*args, **kwargs):
        raise AssertionError('Invalid resend must not invoke the provider')
    monkeypatch.setattr(server, '_classify_managed_route', classify)
    result = asyncio.run(server.route('POST', '/toy/letter/route-preview', {'letter_id': 'old'}, {}))
    assert result['code'] == code
    assert result['message'] == error
    if error == 'LETTER_SUPERSEDED':
        assert result['data']['replacement_letter_id'] == 'replacement'
    assert server.store.letters == [original]


def test_resend_uses_original_content_and_forwards_confirmed_token(tmp_path, monkeypatch):
    import local_server as server
    settings=VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(server,'video_reply_settings_store',settings)
    original=dict(letter_id='failed',letter_status='FAILED',content='看明天的搭配',
                  material={'stamp_id':'s2'},error_code='JEV_PLAN_UNSUPPORTED')
    monkeypatch.setattr(server.store,'letters',[original])
    monkeypatch.setattr(server.store,'request_keys',{})
    monkeypatch.setattr(server,'_reply_route_previews',{})
    monkeypatch.setattr(server,'_persist_store_state',lambda:None)
    monkeypatch.setattr(server,'_schedule_reply_job',lambda *a,**k:None)
    monkeypatch.setattr(server,'_missing_memory_component',lambda:None)
    monkeypatch.setattr(server,'_video_reply_dependencies_ready',lambda:True)
    monkeypatch.setattr(server,'_route_readiness',lambda *a,**k:dict.fromkeys(REPLY_ROUTES,True))
    calls=[]
    async def classify(content,routes):
        calls.append(content)
        return TriageResult('normal','text_letter','jev_image_request','completed',True)
    monkeypatch.setattr(server,'_classify_managed_route',classify)
    async def run():
        preview=await server.route('POST','/toy/letter/route-preview',{'letter_id':'failed','content':'must ignore'}, {})
        assert preview['code']==0,preview
        assert preview['data']['image_requested'] is True
        result=await server.route('POST','/toy/letter/resend',{'letter_id':'failed',
            'material':{'route_preview_token':preview['data']['token'],'stamp_id':'tampered'}},{},defer_reply=True)
        assert result['code']==0,result
        assert calls==['看明天的搭配']
        replacement=next(x for x in server.store.letters if x['letter_id']!=original['letter_id'])
        assert replacement['material']=={'stamp_id':'s2'}
        assert replacement['route_preflight']['reason_code']=='jev_image_request'
    asyncio.run(run())
