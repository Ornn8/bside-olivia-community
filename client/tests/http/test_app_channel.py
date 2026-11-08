"""The phone app chat channel (runtime/personal_chat/app.py) against a stand-in call service.

The stand-in is a small aiohttp server that speaks the desk line and the media endpoints. The model is a
function in the test; nothing here reaches a paid service or a relay."""
import asyncio
import hashlib
import json
import struct
import wave
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from runtime.call import desk, http as call_http
from runtime.personal_chat import app as chat_app
from runtime.personal_chat.channels import proactive_channel, rich
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.exchange_runner import ExchangeRunner
from runtime.personal_chat.service import PersonalChatService, delivery_notice


def png(path: Path, size=(8, 6)):
    def chunk(kind, data):
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body))
    raw = b''.join(b'\x00' + b'\xff\x00\x00' * size[0] for _ in range(size[1]))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size[0], size[1], 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    return path


def wav(path: Path, seconds=0.5):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b'\x00\x00' * int(8000 * seconds))
    return path


class Cloud:
    """Stand-in for the call service: the desk websocket, media upload and the phone's inbox media."""

    def __init__(self):
        self.ws = None
        self.frames = []           # frames from the PC, in order
        self.events = []           # ('frame', type) / ('put', id) in the order they happened
        self.media = {}
        self.inbox = {}
        self.ack_out = True
        self.connected = asyncio.Event()
        self.runner = None
        self.port = None

    async def desk(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        async for msg in ws:
            frame = json.loads(msg.data)
            self.frames.append(frame)
            self.events.append(('frame', frame['type']))
            if frame['type'] == 'app_out' and self.ack_out:
                await ws.send_json({'type': 'app_out_ack', 'ids': [i['id'] for i in frame['items']]})
        self.ws = None
        self.connected.clear()
        return ws

    async def put(self, request):
        data = await request.read()
        media_id = request.match_info['id']
        assert hashlib.sha256(data).hexdigest()[:32] == media_id
        fresh = media_id not in self.media
        self.media[media_id] = (request.content_type, data)
        self.events.append(('put', media_id))
        assert request.headers['Authorization'] == 'Bearer key'
        return web.Response(status=201 if fresh else 200)

    async def get_inbox(self, request):
        data = self.inbox.get(request.match_info['id'])
        return web.Response(status=200, body=data) if data is not None else web.Response(status=404)

    async def start(self):
        application = web.Application()
        application.router.add_get('/v1/call/desk', self.desk)
        application.router.add_put('/v1/call/app/media/{id}', self.put)
        application.router.add_get('/v1/call/app/inbox-media/{id}', self.get_inbox)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        await self.runner.cleanup()

    def sent(self, kind):
        return [f for f in self.frames if f['type'] == kind]

    def items(self):
        return [i for f in self.sent('app_out') for i in f['items']]

    async def say(self, value):
        await self.ws.send_json(value)

    async def until(self, check, timeout=5):
        async with asyncio.timeout(timeout):
            while not check():
                await asyncio.sleep(0.02)


class Rig:
    """The channel as the backend wires it: a real service and exchange runner, a fake model, the stand-in cloud."""

    def __init__(self, root: Path, generate=None, monkeypatch=None):
        self.root = root
        self.cloud = Cloud()
        self.hub = chat_app.Hub()
        self.rows = []
        self.generated = []
        self.states = []
        self.ready_calls = []
        self.gate = None
        self.server = SimpleNamespace(store=SimpleNamespace(personal_chats=self.rows))
        self.server._state_root = lambda: self.root
        self.model = generate or self.default_model
        self.stop = asyncio.Event()

    async def default_model(self, event, row):
        self.generated.append(event.text)
        if self.gate is not None:
            await self.gate.wait()
        text = event.text
        if 'voice' in text:
            row['prepared_audio'] = str(wav(self.root / 'v.wav'))
        if 'sticker' in text:
            row['sticker_id'] = 'linli-05'
        return 'echo: ' + text

    async def __aenter__(self):
        await self.cloud.start()
        self.patch = pytest.MonkeyPatch()
        self.patch.setattr(call_http, '_cloud', lambda: (f'http://127.0.0.1:{self.cloud.port}', 'key'))
        self.patch.setattr(chat_app, 'hub', self.hub)
        self.patch.setattr(desk, 'RETRY_SECONDS', (0.05,))
        desk._task = None
        sticker = png(self.root / 'linli-05.png')

        async def commit(row):
            pass

        async def sticker_asset(key):
            return sticker
        self.service = PersonalChatService(self.rows, lambda: None, self.model, commit,
                                           {'app': (chat_app.ACCOUNT, chat_app.OWNER)},
                                           sticker_allowed=lambda key: True, sticker_asset=sticker_asset)

        async def handle(event, send):
            await self.service.handle(event, send)
        handle.ingest = self.service.ingest
        handle.pending = self.service.pending
        handle.ready = lambda channel, send: self.ready_calls.append(channel)
        self.runner = ExchangeRunner(handle)
        self.job = asyncio.create_task(chat_app.run_app(self.runner, self.stop, self.states.append,
                                                        server=self.server, hub=self.hub))
        await asyncio.sleep(0)
        desk.start(self.server, {})
        await asyncio.wait_for(self.cloud.connected.wait(), 5)
        await self.cloud.until(lambda: 'CONNECTED' in self.states)
        return self

    async def __aexit__(self, *exc):
        self.stop.set()
        self.hub.changed.set()
        if desk._task is not None:
            desk._task.cancel()
            await asyncio.gather(desk._task, return_exceptions=True)
        desk._task = None
        self.job.cancel()
        await asyncio.gather(self.job, return_exceptions=True)
        await self.runner.close()
        self.patch.undo()
        await self.cloud.stop()

    async def phone_says(self, text, *, seq=1, item_id='u1', **extra):
        await self.cloud.say({'type': 'app_in', 'seq': seq,
                              'item': {'id': item_id, 'ts': 1_780_000_000 + seq, 'text': text, 'phone_id': 'p1', **extra}})


def rig(tmp_path, generate=None):
    return Rig(tmp_path, generate)


def run(coro):
    asyncio.run(coro)


def test_ack_comes_after_saving_and_before_the_reply(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            r.gate = asyncio.Event()
            await r.phone_says('hello')
            await r.cloud.until(lambda: r.cloud.sent('app_in_ack'))
            assert r.cloud.sent('app_in_ack') == [{'type': 'app_in_ack', 'seq': 1}]
            # saved locally by the time the ack went out, the reply has not been written yet
            assert r.rows and r.rows[0]['source_messages'] == {'u1': 'hello'} and r.rows[0]['channel'] == 'app'
            assert not r.cloud.items()
            r.gate.set()
            await r.cloud.until(lambda: r.cloud.items())
            kinds = [e for e in r.cloud.events if e[0] == 'frame']
            assert kinds.index(('frame', 'app_in_ack')) < kinds.index(('frame', 'app_out'))
    run(scenario())


def test_redelivery_of_the_same_item_is_answered_once(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            await r.phone_says('same words')
            await r.cloud.until(lambda: r.cloud.items())
            await r.phone_says('same words')   # the cloud hands it over again after a reconnect
            await r.cloud.until(lambda: len(r.cloud.sent('app_in_ack')) == 2)
            await asyncio.sleep(0.4)
            assert r.generated == ['same words'] and len(r.cloud.items()) == 1
            assert [row['source_messages'] for row in r.rows] == [{'u1': 'same words'}]
    run(scenario())


def test_text_reply_item(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            await r.phone_says('hi there')
            await r.cloud.until(lambda: r.cloud.items())
            item = r.cloud.items()[0]
            assert (item['from'], item['kind'], item['text'], item['source']) == ('her', 'text', 'echo: hi there', 'reply')
            assert item['reply_to'] == 'u1' and item['media'] is None and len(item['id']) <= 64
            await r.cloud.until(lambda: not r.hub.store().waiting())   # app_out_ack cleared the outbox
            assert r.rows[0]['delivery_status'] == 'DELIVERED' and r.rows[0]['channel'] == 'app'
    run(scenario())


def test_voice_reply_uploads_media_before_the_item(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            await r.phone_says('say it with voice')
            await r.cloud.until(lambda: any(i['kind'] == 'voice' for i in r.cloud.items()))
            voice = next(i for i in r.cloud.items() if i['kind'] == 'voice')
            assert voice['text'] == 'echo: say it with voice' and voice['media']['mime'] == 'audio/wav'
            assert voice['media']['seconds'] == pytest.approx(0.5, abs=0.01)
            media_id = voice['media']['id']
            assert media_id in r.cloud.media and r.cloud.media[media_id][0] == 'audio/wav'
            assert r.cloud.events.index(('put', media_id)) < r.cloud.events.index(('frame', 'app_out'))
            await r.cloud.until(lambda: r.rows[0].get('delivered_format') == 'audio')
    run(scenario())


def test_sticker_and_picture_items(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            await r.phone_says('a sticker for you')
            await r.cloud.until(lambda: any(i['kind'] == 'sticker' for i in r.cloud.items()))
            sticker = next(i for i in r.cloud.items() if i['kind'] == 'sticker')
            assert sticker['sticker'] == 'linli-05' and sticker['media']['mime'] == 'image/png'
            assert (sticker['media']['w'], sticker['media']['h']) == (8, 6)
            photo = await r.runner.send.for_exchange(PersonalMessage('app', 'linli', 'owner', 'u1', 'x'))\
                .image(png(r.root / 'p.png', (16, 12)))
            await r.cloud.until(lambda: any(i['id'] == photo for i in r.cloud.items()))
            item = next(i for i in r.cloud.items() if i['id'] == photo)
            assert item['kind'] == 'image' and (item['media']['w'], item['media']['h']) == (16, 12)
    run(scenario())


def test_face_song_nudge_game_and_moment_items(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            send = r.runner.send
            await send('[微笑]')
            await send.poke()
            song = r.root / 'song.mp3'
            song.write_bytes(b'ID3' + b'\x00' * 4000)
            await send.file(song, 'My Song.mp3')
            result = await send.game('dice')
            await send.qzone_post('evening walk', [png(r.root / 'm.png')])
            await r.cloud.until(lambda: len(r.cloud.items()) == 5)
            face, nudge, song_item, game, moment = r.cloud.items()
            assert face['kind'] == 'face' and face['face_name'] == '微笑' and face['face']
            assert nudge['kind'] == 'nudge' and nudge['text'] == '拍了拍你'
            assert song_item['kind'] == 'song' and song_item['title'] == 'My Song' and song_item['media']['mime'] == 'audio/mpeg'
            assert game['kind'] == 'game' and game['result'] == result and game['game'] == 'dice'
            assert moment['kind'] == 'moment' and moment['text'] == 'evening walk' and moment['media']['mime'] == 'image/png'
    run(scenario())


def test_typing_frames_and_read_state_surround_the_reply(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            r.gate = asyncio.Event()
            await r.phone_says('are you there')
            await r.cloud.until(lambda: any(f.get('on') for f in r.cloud.sent('app_typing')))
            assert r.cloud.sent('app_delivered')[0] == {'type': 'app_delivered', 'id': 'u1', 'state': 'read'}
            r.gate.set()
            await r.cloud.until(lambda: r.cloud.items() and r.cloud.sent('app_typing')[-1]['on'] is False)
            order = [e[1] for e in r.cloud.events if e[0] == 'frame']
            assert order.index('app_typing') < order.index('app_out')
    run(scenario())


def test_incoming_picture_is_fetched_and_placed_for_the_picture_reader(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            data = png(tmp_path / 'src.png').read_bytes()
            media_id = hashlib.sha256(data).hexdigest()[:32]
            r.cloud.inbox[media_id] = data
            await r.phone_says('', item_id='u9', kind='image', media_id=media_id)
            await r.cloud.until(lambda: r.cloud.items())
            row = r.rows[0]
            assert row['incoming_images'] == [['u9', 'app-media:' + media_id]] and row['input_kind'] == 'image'
            identity = hashlib.sha256(('u9\0app-media:' + media_id).encode()).hexdigest()
            assert (tmp_path / 'media' / ('qq-incoming-' + identity + '.img')).read_bytes() == data
    run(scenario())


def test_poke_face_and_dice_from_the_phone_read_like_qq(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            await r.phone_says('', seq=1, item_id='n1', kind='nudge')
            await r.phone_says('', seq=2, item_id='f1', kind='face', face=14, face_name='微笑')
            await r.phone_says('', seq=3, item_id='g1', kind='game', game='dice', result='4')
            await r.cloud.until(lambda: len(r.cloud.sent('app_in_ack')) == 3)
            await r.cloud.until(lambda: r.rows and r.rows[0]['delivery_status'] == 'DELIVERED')
            texts = ' '.join(row['content'] for row in r.rows)
            assert '[他戳了戳你]' in texts and '[表情:微笑]' in texts and '[骰子:4点]' in texts
    run(scenario())


def test_proactive_contact_follows_the_qq_form_rules(tmp_path):
    async def scenario():
        async def voiceless(event, row):
            row['requested_format'] = 'voice'     # asked for a voice message but none could be rendered
            return 'a text she wrote'
        async with rig(tmp_path, voiceless) as r:
            event = PersonalMessage('app', 'linli', 'owner', 'proactive-1', '')
            await r.service.proactive(event, r.runner.send.for_exchange(event), lambda: True)
            await asyncio.sleep(0.2)
            assert r.cloud.items() == [] and r.rows[0]['skip_reason'] == 'PROACTIVE_NO_REPLY'   # never the text of a lost voice
        async def spoken(event, row):
            row['requested_format'] = 'voice'
            row['prepared_audio'] = str(wav(tmp_path / 'p.wav'))
            return 'good morning'
        async with rig(tmp_path / 'b', spoken) as r:
            event = PersonalMessage('app', 'linli', 'owner', 'proactive-2', '')
            await r.service.proactive(event, r.runner.send.for_exchange(event), lambda: True)
            await r.cloud.until(lambda: r.cloud.items())
            item = r.cloud.items()[0]
            assert item['kind'] == 'voice' and item['source'] == 'proactive' and 'reply_to' not in item
    (tmp_path / 'b').mkdir()
    run(scenario())


def test_proactive_goes_to_the_app_only_without_qq():
    assert proactive_channel('qq', {'qq', 'app'}) and proactive_channel('app', {'app'})
    assert not proactive_channel('app', {'qq', 'app'}) and not proactive_channel('wechat', set())
    assert rich('qq') and rich('app') and not rich('wechat') and not rich('letter')


def test_history_for_the_phone_has_both_sides_and_its_media(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            await r.phone_says('voice please', seq=1, item_id='u1')
            await r.cloud.until(lambda: any(i['kind'] == 'voice' for i in r.cloud.items()))
            r.cloud.media.clear()
            r.cloud.events.clear()
            await r.cloud.say({'type': 'app_history', 'req': 'h1', 'limit': 60, 'before': None})
            await r.cloud.until(lambda: r.cloud.sent('app_history'))
            reply = r.cloud.sent('app_history')[0]
            assert reply['req'] == 'h1'
            assert [i['from'] for i in reply['items']] == ['user', 'her', 'her'][:len(reply['items'])] or reply['items'][0]['from'] == 'user'
            assert reply['items'][0]['id'] == 'u1' and reply['items'][0]['text'] == 'voice please'
            voice = next(i for i in reply['items'] if i['kind'] == 'voice')
            assert voice['media']['id'] in r.cloud.media   # media was uploaded first
            assert r.cloud.events.index(('put', voice['media']['id'])) < r.cloud.events.index(('frame', 'app_history'))
            ts = [i['ts'] for i in reply['items']]
            assert ts == sorted(ts)
            await r.cloud.say({'type': 'app_history', 'req': 'h2', 'limit': 1, 'before': None})
            await r.cloud.until(lambda: len(r.cloud.sent('app_history')) == 2)
            assert len(r.cloud.sent('app_history')[1]['items']) == 1
    run(scenario())


def test_history_falls_back_to_the_stored_rows():
    rows = [{'channel': 'app', 'letter_id': 'L1', 'created_at': 100.0, 'source_messages': {'a': 'hi'},
             'delivery_status': 'DELIVERED', 'reply_text': 'hello', 'private_world_occurred_at': '1970-01-01T00:02:00+00:00'},
            {'channel': 'qq', 'letter_id': 'L2', 'created_at': 50.0, 'source_messages': {'q': 'qq only'},
             'delivery_status': 'DELIVERED', 'reply_text': 'no'}]
    items = chat_app.rows_history(rows, 60, None)
    assert [(i['from'], i['text']) for i in items] == [('user', 'hi'), ('her', 'hello')]
    assert chat_app.rows_history(rows, 60, 110.0)[-1]['from'] == 'user'


def test_profile_carries_language_and_her_status(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_UI_LANGUAGE', 'en')

    async def scenario():
        async with rig(tmp_path) as r:
            await r.cloud.until(lambda: r.cloud.sent('app_profile'))
            first = r.cloud.sent('app_profile')[0]['profile']
            assert first['language'] == 'en' and first['status'] is None and first['timezone']
            await r.runner.send.status(9, '练琴中')
            await r.cloud.until(lambda: len(r.cloud.sent('app_profile')) == 2)
            assert r.cloud.sent('app_profile')[1]['profile']['status']['text'] == '练琴中'
            await r.runner.send.signature('今天也在练琴')
            await r.cloud.until(lambda: len(r.cloud.sent('app_profile')) == 3)
            assert r.cloud.sent('app_profile')[2]['profile']['persona_line'] == '今天也在练琴'
    run(scenario())


def test_a_reply_written_offline_is_kept_and_sent_when_the_line_returns(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            await r.cloud.ws.close()
            await r.cloud.until(lambda: not r.hub.connected())
            item_id = await r.runner.send('written while the line was down')
            assert [i['id'] for i in r.hub.store().waiting()] == [item_id]
            # a restart of the program: the outbox is on disk
            again = chat_app.Hub()
            again.server = r.server
            assert [i['id'] for i in again.store().waiting()] == [item_id]
            await asyncio.wait_for(r.cloud.connected.wait(), 5)
            await r.cloud.until(lambda: any(i['id'] == item_id for i in r.cloud.items()))
            await r.cloud.until(lambda: not r.hub.store().waiting())
            assert [i['id'] for i in r.cloud.items()].count(item_id) == 1
    run(scenario())


def test_an_unacknowledged_item_is_sent_again_and_the_cloud_ignores_the_duplicate(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            r.cloud.ack_out = False
            item_id = await r.runner.send('no ack for this one')
            assert [i['id'] for i in r.hub.store().waiting()] == [item_id]
            r.cloud.ack_out = True
            await r.hub.flush()
            await r.cloud.until(lambda: not r.hub.store().waiting())
            assert [i['id'] for i in r.cloud.items()] == [item_id, item_id]   # same id both times: idempotent for the cloud
    run(scenario())


def test_failure_notice_goes_out_as_an_ordinary_message(tmp_path):
    async def scenario():
        async with rig(tmp_path) as r:
            row = {}
            await delivery_notice(row, r.runner.send, lambda: None, 'generation_failure_notice', 'this did not work, sorry')
            await r.cloud.until(lambda: r.cloud.items())
            assert row['generation_failure_notice'] == 'DELIVERED' and r.cloud.items()[0]['text'] == 'this did not work, sorry'
    run(scenario())


def test_the_channel_is_active_once_a_phone_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(call_http, '_cloud', lambda: ('https://api.example.cn', 'key'))
    server = SimpleNamespace(_state_root=lambda: tmp_path)
    assert not chat_app.active(server)
    assert chat_app.mark_active(server) is True and chat_app.active(server)
    assert chat_app.mark_active(server) is False
    monkeypatch.setattr(call_http, '_cloud', lambda: (None, None))
    assert not chat_app.active(server)   # without an account there is no line


def test_media_cache_drops_old_files_but_keeps_what_waits_in_the_outbox(tmp_path):
    store = chat_app.Store(tmp_path)
    old = store.keep_media(b'old' * 100, {'mime': 'image/png'})
    kept = store.keep_media(b'kept' * 100, {'mime': 'image/png'})
    store.add({'id': 'i1', 'from': 'her', 'ts': 1.0, 'kind': 'image', 'text': '', 'media': {'id': kept}})
    store.queue({'id': 'i1'})
    store.prune(now=Path(store.media_path(old)).stat().st_mtime + 20 * 86400)
    assert not store.media_path(old).exists() and store.media_path(kept).exists()


def _backend_run(tmp_path, monkeypatch, *, flag, qq_config=False, wake=False):
    """Start the personal-chat backend the way the client does; returns (names of jobs that ran, bindings)."""
    import local_server
    from runtime.personal_chat import backend, qq
    monkeypatch.delenv('OLIVIA_PERSONAL_CHAT_CONFIG', raising=False)
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    monkeypatch.setattr(call_http, '_cloud', lambda: ('https://api.example.cn', 'key'))
    folder = tmp_path / 'personal-chat'
    folder.mkdir()
    if flag and not wake:
        (folder / chat_app.FLAG).write_text('{}', encoding='utf-8')
    if qq_config:
        (folder / 'config.json').write_text(json.dumps({'qq': {'url': 'ws://127.0.0.1:3001', 'account': '100', 'owner': '200'}}), encoding='utf-8')
        (folder / 'existing-access.json').write_text(json.dumps({'channels': ['qq']}), encoding='utf-8')
    ran = []

    async def listen_qq(url, token, account, owner, handler, stop, **kwargs):
        kwargs['state_callback']('CONNECTED')
        ran.append('qq')
        await stop.wait()

    async def listen_app(handler, stop, on_state, *, server, hub=None):
        on_state('CONNECTED')
        ran.append('app')
        await stop.wait()
    monkeypatch.setattr(qq, 'run_qq', listen_qq)
    monkeypatch.setattr(chat_app, 'run_app', listen_app)
    result = {}

    async def scenario():
        store = local_server.Store()
        store.letters = []
        server = SimpleNamespace(store=store, _state_root=lambda: tmp_path, _require_store_state_available=lambda: None,
                                 _persist_store_state=lambda: None, _safe_log=lambda *a, **k: None,
                                 _atomic_write_store_file=local_server._atomic_write_store_file)
        application = web.Application()
        backend.install_personal_chat(application, server)
        runner = web.AppRunner(application)
        await runner.setup()
        await asyncio.sleep(.05)
        if wake:   # the first phone connects while the client is running
            chat_app.mark_active(server)
            await server._personal_chat_wake()
            await asyncio.sleep(.05)
        runtime = application.get(backend._RUNTIME)
        result['bindings'] = dict(runtime['service'].bindings) if runtime else None
        result['selected'] = backend.selected_channels(server)
        await runner.cleanup()
    asyncio.run(scenario())
    return ran, result


def test_a_client_without_a_phone_never_starts_the_channel(tmp_path, monkeypatch):
    ran, result = _backend_run(tmp_path, monkeypatch, flag=False)
    assert ran == [] and result['bindings'] is None and 'app' not in result['selected']


def test_a_phone_user_without_qq_gets_the_service_with_the_app_binding(tmp_path, monkeypatch):
    ran, result = _backend_run(tmp_path, monkeypatch, flag=True)
    assert ran == ['app'] and list(result['bindings']) == ['app'] and 'app' in result['selected']


def test_qq_and_the_app_run_side_by_side(tmp_path, monkeypatch):
    ran, result = _backend_run(tmp_path, monkeypatch, flag=True, qq_config=True)
    assert sorted(ran) == ['app', 'qq'] and set(result['bindings']) == {'app', 'qq'}


def test_the_first_phone_starts_the_channel_in_a_running_client(tmp_path, monkeypatch):
    ran, result = _backend_run(tmp_path, monkeypatch, flag=True, wake=True)
    assert ran == ['app'] and list(result['bindings']) == ['app']


def test_the_first_phone_adds_the_app_to_a_running_qq_service(tmp_path, monkeypatch):
    ran, result = _backend_run(tmp_path, monkeypatch, flag=True, qq_config=True, wake=True)
    assert sorted(ran) == ['app', 'qq'] and set(result['bindings']) == {'app', 'qq'}
