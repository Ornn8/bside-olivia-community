import asyncio
import json

from runtime.personal_chat.events import PersonalMessage, combine
from runtime.personal_chat.service import PersonalChatService
from runtime.personal_chat.presentation import parse


def event(key, text):
    return PersonalMessage('qq', 'bot', 'owner', key, text)


def test_regrouped_replay_keeps_original_ids_and_only_new_messages_generate():
    async def scenario():
        rows, generated, sent = [], [], []
        async def generate(message, row):
            generated.append(message.text)
            return '回应：' + message.text
        async def send(text):
            sent.append(text)
        async def commit(row):
            pass
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        await service.handle(combine([event('1','刚才没说完'),event('2','今天其实挺开心')]), send)
        restored = json.loads(json.dumps(rows))
        service = PersonalChatService(restored, lambda: None, generate, commit, service.bindings)
        await service.handle(combine([event('2','今天其实挺开心'),event('3','终于交稿了')]), send)
        assert generated == ['刚才没说完\n今天其实挺开心', '终于交稿了']
        assert len(sent) == 2
        assert restored[0]['source_messages'] == {'1':'刚才没说完','2':'今天其实挺开心'}
    asyncio.run(scenario())


def test_long_reply_not_truncated_and_listening_preference_persists():
    long = '这件事我们可以慢慢说。' * 100
    text, mode, pref = parse(long + '\n[[delivery:voice|keep]]', 'text_only')
    assert text == long and mode == 'text' and pref == 'text_only'
    assert parse('好，听你说。[[delivery:voice|voice_ok]]', pref)[1:] == ('voice','voice_ok')


def test_voice_delivery_records_text_once_and_never_speaks_marker():
    async def scenario():
        rows, sounds, commits = [], [], []
        async def generate(message, row):
            row['prepared_audio'] = 'synthetic.wav'
            return parse('我在呢。[[delivery:voice|keep]]')[0]
        async def send(text):
            raise AssertionError('unexpected text duplicate')
        async def audio(path):
            sounds.append(path)
        send.audio = audio
        async def commit(row):
            commits.append(row['reply_text'])
        service=PersonalChatService(rows,lambda:None,generate,commit,{'qq':('bot','owner')})
        await service.handle(event('1','想听你说话'),send)
        await service.handle(event('1','想听你说话'),send)
        assert sounds == ['synthetic.wav']
        assert rows[0]['delivered_format'] == 'audio' and commits == ['我在呢。','我在呢。']
    asyncio.run(scenario())


def test_wechat_burst_uses_latest_context_and_defers_cursor_until_delivery(monkeypatch):
    from runtime.personal_chat import wechat
    from tests.http.test_personal_chat_wechat import message, CREDENTIALS, Cursor
    async def scenario():
        stop, cursor, seen = asyncio.Event(), Cursor(), []
        async def request(session, base, path, **kwargs):
            if path.endswith('getupdates'):
                value=kwargs['body']['get_updates_buf']
                if value=='old':
                    return {'msgs':[message(1)],'get_updates_buf':'a'}
                if value=='a':
                    second=message(2); second['context_token']='latest'
                    return {'msgs':[second],'get_updates_buf':'b'}
                await asyncio.Event().wait()
            assert cursor.value=='old'
            assert kwargs['body']['msg']['context_token']=='latest'
            return {}
        async def handle(event,send):
            seen.append(event)
            await send('回应')
            stop.set()
        monkeypatch.setattr(wechat,'wechat_request',request)
        await asyncio.wait_for(wechat.run_wechat(CREDENTIALS,handle,stop,cursor_store=cursor,merge_seconds=.1),1)
        assert len(seen)==1 and len(seen[0].sources)==2 and cursor.value=='b'
    asyncio.run(scenario())


def test_wechat_sticker_uses_checked_upload_and_same_context_without_audio(monkeypatch):
    from runtime.personal_chat import wechat, wechat_images
    from tests.http.test_personal_chat_wechat import CREDENTIALS
    async def scenario():
        requests=[]
        item={'type':2,'image_item':{'mid_size':100,'media':{'encrypt_query_param':'synthetic'}}}
        async def upload(*args):
            return item
        async def request(session,base,path,**kwargs):
            requests.append(kwargs['body']['msg'])
            return {}
        monkeypatch.setattr(wechat_images,'upload',upload)
        monkeypatch.setattr(wechat,'wechat_request',request)
        message=PersonalMessage('wechat','bot','owner','1','想听你说话')
        send=wechat._sender(None,CREDENTIALS,message,'latest-context')
        assert not hasattr(send, 'audio') and not hasattr(send, 'prepare_audio')
        await send.image('synthetic.png')
        assert requests[0]['context_token']=='latest-context' and requests[0]['item_list']==[item]
    asyncio.run(scenario())


def test_qq_burst_keeps_ack_reader_live():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from runtime.personal_chat.qq import run_qq
    from tests.http.test_personal_chat_qq import login, event as raw_event, TOKEN
    async def scenario():
        stop=asyncio.Event()
        seen=[]
        async def handle(message,send):
            seen.append(message)
            await send('一起回应')
            stop.set()
        async def socket(request):
            ws=web.WebSocketResponse(); await ws.prepare(request); await login(ws)
            await ws.send_json(raw_event(1)); await ws.send_json(raw_event(2))
            outgoing=await ws.receive_json()
            await ws.send_json({'echo':outgoing['echo'],'status':'ok','retcode':0,'data':{'message_id':3}})
            await stop.wait(); await ws.close(); return ws
        app=web.Application(); app.router.add_get('/',socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url('/')),TOKEN,'100','200',handle,stop,merge_seconds=.1),3)
        assert len(seen)==1 and len(seen[0].sources)==2
    asyncio.run(scenario())
