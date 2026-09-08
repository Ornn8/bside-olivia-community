import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from letter_triage import RoutingContext, _validated_result
from original_client_letter_contract import serialize_letter_detail


@pytest.mark.parametrize('contexts,mode', [
    (['explicit_voice_reply_request'],'voice_reply'),
    (['explicit_video_reply_request'],'voice_reply'),
    (['explicit_performance_or_adaptation_request'],'singing_video'),
    (['explicit_voice_and_song_request'],'voice_song_video'),
])
def test_explicit_media_selects_only_requested_parts(contexts, mode):
    payload=dict(mode='text_letter',reason_code='test',emotion_level='normal',music_contexts=contexts,
        music_role='none',music_intent='none',request_disposition='none',direct_response_sufficient=True,
        voice_materially_better=False,music_materially_better=False,character_willing=False)
    result=_validated_result(payload,RoutingContext(True,voice_reply_available=True))
    assert result.reply_mode==mode
    assert result.request_disposition=='fulfill'
    payload['music_contexts']=['explicit_voice_reply_request']
    assert _validated_result(payload,RoutingContext(False,voice_reply_available=True)).reply_mode=='voice_reply'


@pytest.mark.parametrize('mode', ['voice_reply','voice_song_video'])
def test_partial_audio_remains_published_on_paper(mode):
    letter=dict(letter_id='synthetic',reply_mode=mode,letter_status='COMPLETED',reply_text='测试回信',
        media_status='FAILED',reply_audio_url='http://127.0.0.1:8899/toy/media/synthetic.wav')
    out=serialize_letter_detail(letter)
    assert out['replyType']==1
    assert out['replyText']=='测试回信'
    assert out['replyAudioUrl'].endswith('synthetic.wav')
    assert out['audioStatus']=='FAILED'
    letter['reply_audio_url']='https://example.com/private.wav'
    assert serialize_letter_detail(letter)['replyAudioUrl']==''


def test_voice_job_has_no_video_hardware_or_lipsync_dependency(tmp_path,monkeypatch):
    import local_server as server
    letter=dict(letter_id='voice-only',reply_mode='voice_reply',reply_text='测试')
    monkeypatch.setattr(server.store,'letters',[letter])
    monkeypatch.setattr(server,'media_semaphore',asyncio.Semaphore(1))
    monkeypatch.setattr(server,'_local_data_root',lambda *a:tmp_path)
    monkeypatch.setattr(server,'_persist_media_state',lambda:None)
    monkeypatch.setattr(server,'require_breeze_hardware',lambda:pytest.fail('Video VRAM gate must not run'))
    monkeypatch.setattr(server,'render_reply_video',lambda *a,**k:pytest.fail('No lipsync'))
    async def plan(*a):return SimpleNamespace(spoken_text='测试')
    monkeypatch.setattr(server,'_voice_plan_for_letter',plan)
    calls=[]
    def render(text,path,**kwargs):
        calls.append(text);path.write_bytes(b'synthetic-wave')
        return {'duration_seconds':40}
    monkeypatch.setattr(server,'render_reply_audio',render)
    asyncio.run(server._render_media_job('voice-only','来信','测试','voice_reply'))
    assert calls==['测试']
    assert letter['media_status']=='COMPLETED'
    assert letter['reply_audio_url'].endswith('/voice-only.wav')
    assert 'reply_video_url' not in letter


def test_native_audio_patch_is_idempotent_and_binds_props():
    from patch_companion_settings import _repair_native_letter_audio
    source='letterStatus:e.letterStatus,videoUrl:e.replyVideoUrl||void 0;__name:"MailBoxReplyContent",props:{};F(ks,{key:0});[A.type==="error"?'
    patched=_repair_native_letter_audio(source)
    assert 'audioUrl:i.mail.received?.audioUrl' in patched
    assert 'olivia-letter-audio' in patched
    assert _repair_native_letter_audio(patched)==patched
