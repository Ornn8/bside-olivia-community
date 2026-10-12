import asyncio
from types import SimpleNamespace
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from runtime.personal_chat.qq import run_qq,QQFileRejected
from runtime.personal_chat.service import PersonalChatService
from runtime.personal_chat.speech import validate_script
from tests.http.test_personal_chat_qq import login,event,TOKEN


@pytest.mark.parametrize('ack,expected',[(dict(status='ok',retcode=0,data={}),None),
    (dict(status='ok',retcode=0,data=None),None),(dict(status='failed',retcode=1,data=None),QQFileRejected),
    (None,TimeoutError)])
@pytest.mark.parametrize('file_size', [9, 8 * 1024 * 1024])
def test_bound_file_action_ack_and_timeout(tmp_path,ack,expected,file_size):
    path=tmp_path/'睡前故事.mp3';path.write_bytes(b'x' * file_size)
    async def scenario():
        stop=asyncio.Event();sent=[]
        async def handler(message,send):
            if expected:
                with pytest.raises(expected):await send.file(path,path.name)
            else:assert (await send.file(path,path.name)).startswith('qq-file:')
            stop.set()
        async def socket(request):
            ws=web.WebSocketResponse();await ws.prepare(request);await login(ws);await ws.send_json(event())
            out=await ws.receive_json();sent.append(out)
            if ack:await ws.send_json({**ack,'echo':out['echo']})
            await stop.wait();await ws.close();return ws
        app=web.Application();app.router.add_get('/',socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url('/')),TOKEN,'100','200',handler,stop,
                                         merge_seconds=.01,media_ack_timeout=.1),2)
        assert len(sent)==1
        assert sent[0]['action']=='upload_private_file'
        assert sent[0]['params']==dict(user_id=200,file=str(path.resolve()),name=path.name)
    asyncio.run(scenario())


def test_background_speech_recovery_deduplicates_and_never_replays_unknown():
    from runtime.personal_chat.events import PersonalMessage
    async def scenario():
        gate=asyncio.Event();started=[]
        async def speech(row,send):started.append(row['letter_id']);await gate.wait()
        row=dict(letter_id='one',channel='qq',binding_id=PersonalMessage('qq','bot','owner','','').binding_id,
                 delivery_status='DELIVERED',speech_script={'title':'灯'},speech_delivery_status='PENDING')
        rows=[row,{**row,'letter_id':'unknown','speech_delivery_status':'UNKNOWN'},
              {**row,'letter_id':'other-owner','binding_id':PersonalMessage('qq','bot','other','','').binding_id},
              {**row,'letter_id':'legacy','binding_id':None}]
        service=PersonalChatService(rows,lambda:None,None,None,{'qq':('bot','owner')},speech=speech)
        async def send(text):return 'ack'
        send.file=lambda *args:None
        service.resume_speech('qq',send);service.resume_speech('qq',send)
        await asyncio.sleep(0)
        assert started==['one']
        assert not service.lock.locked()
        gate.set();await asyncio.gather(*service.speech_tasks.values())
    asyncio.run(scenario())


@pytest.mark.parametrize('text',['（轻声耳语）'+'字'*500,'[停顿]'+'字'*500,'{"internal":'+'字'*500])
def test_no_action_or_json_enters_spoken_script(text):
    with pytest.raises(ValueError):validate_script(dict(title='故事',spoken_text=text,continuation_summary=''))


def test_file_retry_recovers_changed_download_without_generation(tmp_path,monkeypatch):
    import hashlib
    from runtime.personal_chat.speech import deliver
    content=b'validated-mp3-fixture';downloads=[];sent=[];acks=[]
    result=dict(sha256=hashlib.sha256(content).hexdigest(),bytes=len(content))
    row=dict(letter_id='saved',speech_task_id='same-order',speech_delivery_status='FAILED',
             speech_intent=dict(mode='asmr',target_seconds=600,continuation=False),
             speech_script=dict(title='夜里的小灯',spoken_text='完整正文。'*40,continuation_summary=''),
             speech_result=result)
    output=tmp_path/'media/speech/saved/夜里的小灯.mp3'
    output.parent.mkdir(parents=True);output.write_bytes(b'damaged')
    class API:
        url='https://synthetic.invalid';token='synthetic'
        def __init__(self,*args):pass
        async def generate(self,*args,**kwargs):raise AssertionError('must not generate again')
        async def download_task(self,task_id,path,**kwargs):
            downloads.append(task_id);path.write_bytes(content)
            return dict(task_id=task_id,speech=result)
        async def request(self,action,data):
            assert row['speech_delivery_status']=='DELIVERED'
            acks.append((action,data));return {}
    async def persist(server):pass
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    monkeypatch.setattr('runtime.personal_chat.backend.persist_chat',persist)
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds',lambda *a,**kw:600)
    async def scenario():
        async def send(text):pass
        async def file(path,name):
            assert path.read_bytes()==content;sent.append(name)
            if len(sent)==1:raise QQFileRejected('synthetic rejection')
            return 'qq-file:accepted'
        send.file=file
        server=SimpleNamespace(_state_root=lambda:tmp_path)
        with pytest.raises(QQFileRejected):await deliver(server,row,send)
        assert row['speech_delivery_status']=='FAILED'
        assert not acks
        await deliver(server,row,send)
        assert row['speech_delivery_status']=='DELIVERED'
    asyncio.run(scenario())
    assert downloads==['same-order'] and len(sent)==2
    assert acks==[('ack',{'task_id':'same-order'})]


def test_planned_long_audio_sends_confirmation_then_file_without_short_voice():
    from runtime.personal_chat.events import PersonalMessage
    async def scenario():
        rows=[];sent=[];committed=[];files=[]
        async def generate(event,row):
            row.update(companion_decision={'speech_request':dict(mode='asmr',target_seconds=600,continuation=False)},
                       companion_delivery='audio_speech',speech_script=dict(title='小灯',spoken_text='虚构故事。'*100,continuation_summary='虚构摘要'),
                       speech_delivery_status='PENDING',requested_format='text',
                       speech_intent=dict(mode='asmr',target_seconds=600,continuation=False))
            return '我会把耳语音频整理成文件发给你。'
        async def commit(row):committed.append(row['reply_text'])
        async def speech(row,send):files.append(row['letter_id'])
        async def send(text):sent.append(text);return 'text-ack'
        async def short_audio(path):raise AssertionError('no ordinary short voice')
        send.audio=short_audio;send.file=lambda *a:None
        service=PersonalChatService(rows,lambda:None,generate,commit,{'qq':('bot','owner')},speech=speech)
        await service.handle(PersonalMessage('qq','bot','owner','one','轻声陪我睡觉'),send)
        await asyncio.gather(*service.speech_tasks.values())
        assert len(sent)==1 and committed==sent
        assert '虚构' not in committed[0]
        assert files==[rows[0]['letter_id']]
        assert rows[0]['delivery_status']=='DELIVERED'
    asyncio.run(scenario())


def test_client_forwards_frozen_scene_without_mixing_policy(tmp_path,monkeypatch):
    from runtime.personal_chat.speech import deliver
    calls=[]
    row=dict(letter_id='ambience',speech_delivery_status='PENDING',
             speech_intent=dict(mode='asmr',target_seconds=600,continuation=False,ambience_scene='rain_room'),
             speech_script=dict(title='雨夜',spoken_text='完整台词。'*100,continuation_summary=''))
    class API:
        url='https://synthetic.invalid';token='synthetic'
        def __init__(self,*args):pass
        async def generate(self,kind,data,path,**kwargs):
            calls.append(data);path.write_bytes(b'synthetic')
            return dict(task_id='frozen-task',speech={})
        async def request(self,*args):return {}
    async def persist(server):pass
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    monkeypatch.setattr('runtime.personal_chat.backend.persist_chat',persist)
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds',lambda *a,**kw:600)
    async def send(text):pass
    async def file(*args):return 'accepted'
    send.file=file
    asyncio.run(deliver(SimpleNamespace(_state_root=lambda:tmp_path),row,send))
    assert len(calls)==1 and calls[0]['ambience_scene']=='rain_room'
    assert set(calls[0])=={'channel','text','speech_title','speech_mode','target_seconds','ambience_scene'}
