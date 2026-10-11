import asyncio
import base64
import io
import json
from types import SimpleNamespace
import wave

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from runtime.personal_chat.events import owner_message, combine
from runtime.personal_chat.service import PersonalChatService
from runtime import incoming_media as media
from runtime.personal_chat.qq import run_qq


def event(kind='record', **data):
    return owner_message('qq', dict(post_type='message', message_type='private', self_id=100,
        user_id=200, message_id=kind, message=[dict(type=kind, data=data or {'file': 'a'*32})]),
        account_id='100', owner_id='200')


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 16000)
    return output.getvalue()


def test_media_owner_events_survive_restart_and_merge():
    voice = event()
    video = event('video', file='b'*32)
    pdf = event('file', file_id='c'*32, name='说明.pdf')
    assert voice.input_kind == 'voice' and video.input_kind == 'video' and pdf.input_kind == 'file'
    combined = combine([voice, video, pdf])
    rows = []
    service = PersonalChatService(rows, lambda: None, lambda *args: None, lambda *args: None, {'qq': ('100', '200')})
    asyncio.run(service.ingest(combined))
    restored = service.pending('qq')[0]
    assert restored.media == combined.media and len(restored.media) == 3
    assert event('record', file='C:/Users/secret.wav') is None
    assert event('file', file_id='https://127.0.0.1/private', name='x.pdf') is None


def test_native_audio_sends_bytes_once_and_does_not_guess_on_failed_request(tmp_path, monkeypatch):
    async def scenario():
        calls = []
        async def observe(request):
            calls.append(await request.json())
            assert request.headers['Idempotency-Key'].startswith('media-observation:')
            assert base64.b64decode(calls[-1]['data']) == wav()
            return web.json_response({'summary': '你好，蓝色笔记本十七元。', 'kind': 'audio'})
        app = web.Application(); app.router.add_post('/v1/media/observations', observe)
        async with TestServer(app) as endpoint:
            monkeypatch.setattr('original_client_relay_api.RELAY_BASE', str(endpoint.make_url('/v1')))
            server = SimpleNamespace(_state_root=lambda: tmp_path, _persist_store_state=lambda: None)
            monkeypatch.setattr(media, '_vision_connection', lambda _: (str(endpoint.make_url('/v1')), 'olivia-synthetic'))
            async def resolve(kind, reference):
                assert kind == 'audio' and reference == 'a'*32
                return {'base64': base64.b64encode(wav()).decode()}
            token = media.MEDIA_RESOLVER.set(resolve)
            row = {'letter_id': 'turn', 'incoming_media': [list(part) for part in event().media]}
            try:
                await media.understand_incoming(server, event(), row)
                await media.understand_incoming(server, event(), row)
            finally:
                media.MEDIA_RESOLVER.reset(token)
            assert len(calls) == 1
            assert row['incoming_media_observations'][0]['summary'].startswith('你好')
            assert '不是用户直接输入' in media.incoming_context(row)
            assert len(media.evidence(row)) == 1
    asyncio.run(scenario())


def test_dispatched_media_is_not_replayed_after_timeout_or_restart(tmp_path, monkeypatch):
    async def scenario():
        server = SimpleNamespace(_state_root=lambda: tmp_path, _persist_store_state=lambda: None)
        async def resolve(*args):
            return {'base64': base64.b64encode(wav()).decode()}
        async def fail(*args, **kwargs):
            raise TimeoutError()
        monkeypatch.setattr(media, 'describe_media', fail)
        token = media.MEDIA_RESOLVER.set(resolve)
        row = {'letter_id': 'turn', 'incoming_media': [list(part) for part in event().media]}
        try:
            await media.understand_incoming(server, event(), row)
            await media.understand_incoming(server, event(), row)
        finally:
            media.MEDIA_RESOLVER.reset(token)
        assert row['incoming_media_failed'] == 1
        assert '不能猜' in media.incoming_context(row)
        assert row['incoming_media_attempted']
    asyncio.run(scenario())


def test_gif_observes_multiple_chronological_frames(tmp_path):
    path = tmp_path / 'animated.gif'
    Image.new('RGB', (20, 20), 'red').save(path, save_all=True,
        append_images=[Image.new('RGB', (20,20), 'blue')], duration=[1000,1000], loop=0)
    frames = media.gif_frames(path)
    assert len(frames) == 2
    assert frames[0][0] == 0 and frames[1][0] == 1
    decoded = [Image.open(io.BytesIO(base64.b64decode(uri.split(',')[1]))).getpixel((0,0)) for _,uri in frames]
    assert decoded[0][0] > 200 and decoded[1][2] > 200


def test_recognized_media_is_separate_idempotent_memory_even_when_reply_fails(tmp_path):
    from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
    observation = dict(kind='audio', summary='蓝色笔记本十七元。', source='user',
        evidence_kind='media_observation', input_id='a'*64, sha256='b'*64,
        observed_at='2026-10-06T01:00:00+00:00')
    row = dict(letter_id='qq-voice', content='[语音]', delivery_status='FAILED',
        incoming_media_observations=[observation])
    state = tmp_path/'state.json'
    state.write_text(json.dumps({'personal_chats':[row]}), encoding='utf-8')
    outbox = CanonicalMemoryOutbox.__new__(CanonicalMemoryOutbox)
    outbox.state_path = state
    first = outbox._read_letters()
    assert first == outbox._read_letters() and len(first) == 1
    assert first[0]['origin'] == 'user'
    assert first[0]['letter_id'] != row['letter_id']
    assert '蓝色笔记本十七元' in first[0]['content']
    assert '不是用户直接输入' in first[0]['content']
    assert row['content'] == '[语音]'
    row['incoming_media_observations'][0]['source'] = 'generated'
    state.write_text(json.dumps({'personal_chats':[row]}), encoding='utf-8')
    assert outbox._read_letters() == ()


def test_media_observation_survives_history_role_projection_without_becoming_user_words():
    from datetime import datetime, timezone
    from runtime.reply.conversation_context import conversation_context
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    observed=dict(kind='pdf',source='user',summary='NOTEBOOK Z9P2, quantity 17',evidence_kind='media_observation')
    row=dict(letter_id='qq-pdf',channel='qq',created_at=1,delivery_status='DELIVERED',
        content='看看这个文件',reply_text='我看到了。',incoming_media_observations=[observed])
    recent,_=conversation_context([row],query='文件',now=datetime.now(timezone.utc))
    original=({'role':'system','content':'<untrusted_history>'+json.dumps({'text':recent})+'</untrusted_history>'},
        {'role':'user','content':'文件里数量多少？'})
    projected=prepare_dialogue_messages(original,max_input_chars=20000)
    assert any(observed['summary'] in item['content'] for item in projected if item['role']=='system')
    assert all(observed['summary'] not in item['content'] for item in projected if item['role']=='user')
    assert projected[-1] == original[-1]
    assert prepare_dialogue_messages(projected,max_input_chars=20000) == projected


@pytest.mark.parametrize('result', [{}, {'base64': 'broken!'}, {'url': 'https://localhost/a'},
                                    {'url': 'https://qq.com.evil/a'}, {'file': 'C:/secret.txt'}])
def test_napcat_result_never_reads_local_paths_or_untrusted_urls(result, tmp_path):
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(media.acquire(result, tmp_path/'media'))


def test_qq_socket_get_record_decodes_voice_without_blocking_acks():
    async def scenario():
        stop=asyncio.Event()
        async def handle(incoming,send):
            correlated=send.for_exchange(incoming)
            result=await correlated.resolve_media('audio',incoming.media[0][2])
            assert base64.b64decode(result['base64'])==wav()
            assert await correlated('听到了。')=='reply'
            stop.set()
        async def socket(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            login=await ws.receive_json()
            await ws.send_json({'echo':login['echo'],'status':'ok','retcode':0,'data':{'user_id':100}})
            await ws.send_json(dict(post_type='message',message_type='private',self_id=100,user_id=200,
                message_id=1,message=[{'type':'record','data':{'file':'a'*32}}]))
            action=await ws.receive_json()
            assert action['action']=='get_record' and action['params']=={'file_id':'a'*32,'out_format':'wav'}
            await ws.send_json({'echo':action['echo'],'status':'ok','retcode':0,'data':{'base64':base64.b64encode(wav()).decode()}})
            reply=await ws.receive_json()
            assert reply['params']['message'][0]=={'type':'reply','data':{'id':'1'}}
            await ws.send_json({'echo':reply['echo'],'status':'ok','retcode':0,'data':{'message_id':'reply'}})
            await stop.wait();await ws.close();return ws
        app=web.Application();app.router.add_get('/',socket)
        async with TestServer(app) as endpoint:
            await asyncio.wait_for(run_qq(str(endpoint.make_url('/')),'synthetic-napcat-token','100','200',handle,stop,merge_seconds=0),5)
    asyncio.run(scenario())
