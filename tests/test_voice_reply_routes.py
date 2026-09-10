import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from letter_triage import RoutingContext, _validated_result
from original_client_letter_contract import serialize_letter_detail


@pytest.mark.parametrize('mode', ['voice_reply', 'singing_video', 'voice_song_video'])
@pytest.mark.parametrize('status', ['PENDING', 'QUEUED', 'PROCESSING'])
def test_audio_letter_waits_for_complete_delivery(mode, status):
    letter = dict(letter_id='pending-audio', reply_mode=mode, reply_video_enabled=False,
                  letter_status='COMPLETED', reply_text='尚未寄出的正文', media_status=status)
    out = serialize_letter_detail(letter)
    assert out['letterStatus'] != 4
    assert out['replyText'] == ''
    assert out['replyType'] == 0
    assert out['audioStatus'] == status
    letter.update(media_status='COMPLETED', reply_audio_url='http://127.0.0.1:8899/toy/media/ready.wav')
    out = serialize_letter_detail(letter)
    assert out['letterStatus'] == 4
    assert out['replyText'] == '尚未寄出的正文'
    assert out['replyAudioUrl'].endswith('/ready.wav')


def test_native_audio_pending_caption_is_idempotent():
    from patch_companion_settings import _repair_native_letter_audio
    source = 'A.videoPending?"林离录视频中":o(i)("mailbox_waiting_for_reply")'
    patched = _repair_native_letter_audio(source)
    assert '林离正在录语音…' in patched
    assert _repair_native_letter_audio(patched) == patched


def test_native_download_saves_voice_and_letter_with_existing_button(tmp_path):
    import subprocess
    from patch_companion_settings import _repair_native_letter_audio
    source = 'O.replyTextImage&&await yn(O.replyTextImage,`${R}/mail-${H}-reply.png`),yt.hide(),Ds(R)'
    patched = _repair_native_letter_audio(source)
    assert _repair_native_letter_audio(patched) == patched
    script = tmp_path / 'download.cjs'
    script.write_text('''const assert=require('node:assert/strict');
async function run(audioUrl){
 const calls=[],O={replyTextImage:'paper'},R='chosen-folder',H='letter';
 const M={value:{received:{audioUrl}}};
 const yn=async(...args)=>calls.push(['image',...args]);
 const yt={hide(){}}; const Ds=p=>calls.push(['open',p]);
 const d={startVideoDownload:async(...args)=>calls.push(['audio',...args])};
 await (async()=>{''' + patched + '''})();return calls;
}
(async()=>{
 assert.deepEqual(await run('http://127.0.0.1:8899/toy/media/voice.wav'),[
 ['image','paper','chosen-folder/mail-letter-reply.png'],
 ['audio',['http://127.0.0.1:8899/toy/media/voice.wav'],'chosen-folder']]);
 assert.deepEqual(await run(''),[['image','paper','chosen-folder/mail-letter-reply.png'],['open','chosen-folder']]);
})();''', encoding='utf-8')
    subprocess.run(['node', str(script)], check=True, capture_output=True)


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


@pytest.mark.parametrize('mode', ['voice_reply', 'singing_video', 'voice_song_video'])
@pytest.mark.parametrize('video', [False, True])
@pytest.mark.parametrize('cover', [False, True])
def test_six_delivery_combinations_select_only_required_stages(tmp_path, monkeypatch, mode, video, cover):
    import local_server as server
    import runtime.reply.reply_media as media
    calls = []
    letter = dict(letter_id='matrix', reply_mode=mode, reply_video_enabled=video,
                  letter_status='COMPLETED', reply_text='synthetic')
    import hashlib
    from runtime.private_world.daily_life import DailyLifeStore
    world = DailyLifeStore(tmp_path/'world.sqlite')
    letter.update(reply_revision=1, private_world_delivery_id='matrix:1',
        private_world_reply_sha256=hashlib.sha256(b'synthetic').hexdigest())
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=world))
    monkeypatch.setattr(server.store, 'letters', [letter])
    monkeypatch.setattr(server, 'media_semaphore', asyncio.Semaphore(1))
    monkeypatch.setattr(server, '_local_data_root', lambda *a: tmp_path)
    monkeypatch.setattr(server, '_persist_media_state', lambda: None)
    monkeypatch.setattr(server, 'require_breeze_hardware', lambda: calls.append('video_gate'))
    scene = tmp_path/'scene.mp4'; scene.write_bytes(b'synthetic')
    monkeypatch.setattr(server, '_current_music_performance', lambda env: scene)
    monkeypatch.setattr(server, 'configured_media_path', lambda *a: scene)
    async def plan(*a): return SimpleNamespace(spoken_text='synthetic')
    monkeypatch.setattr(server, '_voice_plan_for_letter', plan)
    monkeypatch.setattr(server, '_music_voice_plan_for_letter', plan)
    def speech(text, output, **kw):
        calls.append('speech_audio'); output.write_bytes(b'speech'); return {'duration_seconds':1}
    def speech_video(text, output, **kw):
        calls.append('speech_video'); output.write_bytes(b'video')
    def song(content, text, output, **kw):
        calls.append(('music', kw['include_spoken'], kw.get('render_video', True)))
        output.write_bytes(b'song'); return {}
    def concat(speech, song, output, env):
        calls.append('audio_concat'); output.write_bytes(b'joined')
    monkeypatch.setattr(server, 'render_reply_audio', speech)
    monkeypatch.setattr(server, 'render_reply_video', speech_video)
    monkeypatch.setattr(server, 'render_musical_reply', song)
    if cover:
        import runtime.media.cover_reply as covers
        import runtime.media.cover_upload as uploads
        letter.update(music_provider='ace_step_xl_cover', material={'cover_source_id': 'a'*32, 'cover_lyrics': '这句歌词不是承诺。'})
        monkeypatch.setattr(covers, 'render_cover_reply', song)
        monkeypatch.setattr(uploads, 'source_path', lambda *a: scene)
    monkeypatch.setattr(media, 'concatenate_reply_audio', concat)
    asyncio.run(server._render_media_job('matrix', 'synthetic', 'synthetic', mode))
    assert letter['media_status'] == 'COMPLETED', letter
    music = 'cover' if cover else 'music'
    expected = {'speech'} if mode == 'voice_reply' else {music} if mode == 'singing_video' else {'speech', music}
    assert {event['component'] for event in letter['media_deliveries']} == expected
    assert all(event['presentation'] == ('video' if video else 'audio') for event in letter['media_deliveries'])
    assert letter['media_world_status'] == 'COMMITTED'
    assert len(world.history()['moments']) == len(expected)
    if cover and mode != 'voice_reply':
        event = next(e for e in letter['media_deliveries'] if e['component'] == 'cover')
        assert event['source_audio_id'] == 'a'*32
        assert 'lyrics' not in str(event) and '这句歌词' not in str(event)
    assert ('video_gate' in calls) is video
    if mode == 'voice_reply':
        assert calls == (['video_gate', 'speech_video'] if video else ['speech_audio'])
    else:
        assert ('music', video and mode == 'voice_song_video', video) in calls
        assert 'audio_concat' not in calls
    if not video and mode == 'voice_song_video':
        assert letter['reply_audio_url'].endswith('-speech.wav')
        assert letter['reply_song_url'].endswith('-song.wav')
        assert ('speech_audio' in calls) is (not video and mode == 'voice_song_video')
    projected = serialize_letter_detail(letter)
    if video:
        assert projected['replyVideoUrl'].endswith('.mp4')
        assert 'replyAudioUrl' not in projected
    else:
        assert projected['replyAudioUrl'].endswith('.wav')
        assert projected['replyType'] == 1
