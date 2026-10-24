"""Her QQ gestures: what each one asks NapCat for, and how a reply carries them (2.5.0)."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.qq import run_qq as _run_qq
from runtime.personal_chat.service import PersonalChatService
from functools import partial
from tests.http.test_personal_chat_qq import TOKEN, event, login

run_qq = partial(_run_qq, merge_seconds=0)


def napcat(calls, answers):
    """Drive the connection's gestures and answer each action as NapCat would."""
    async def scenario():
        stop = asyncio.Event()
        outcomes = []
        async def handler(message, send):
            for call in calls:
                try:
                    outcomes.append(await call(send))
                except Exception as exc:
                    outcomes.append(type(exc).__name__ + ':' + str(exc))
            stop.set()
        seen = []
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event())
            while not stop.is_set():
                try:
                    outgoing = await asyncio.wait_for(ws.receive_json(), .5)
                except asyncio.TimeoutError:
                    continue
                seen.append({'action': outgoing['action'], 'params': outgoing['params']})
                await ws.send_json({'echo': outgoing['echo'], **answers.get(outgoing['action'],
                                    {'status': 'ok', 'retcode': 0, 'data': None})})
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get('/', socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url('/')), TOKEN, '100', '200', handler, stop), 5)
        return outcomes, seen
    return asyncio.run(scenario())


def test_each_gesture_names_its_own_action_and_the_owner():
    outcomes, seen = napcat([lambda s: s.poke(), lambda s: s.like(10),
                             lambda s: s.status('8', '睡觉中'), lambda s: s.signature('今天弹了很久的肖邦')],
                            {})
    assert outcomes == [None] * 4
    assert seen == [
        {'action': 'friend_poke', 'params': {'user_id': 200}},
        {'action': 'send_like', 'params': {'user_id': 200, 'times': 10}},
        {'action': 'set_diy_online_status', 'params': {'face_id': '8', 'face_type': '1', 'wording': '睡觉中'}},
        {'action': 'set_self_longnick', 'params': {'longNick': '今天弹了很久的肖邦'}},
    ]


def test_a_refused_gesture_is_an_error_and_bad_input_never_reaches_napcat():
    outcomes, seen = napcat([lambda s: s.poke(), lambda s: s.like(11),
                             lambda s: s.status('8', '这是一个太长太长太长太长的状态文字'), lambda s: s.signature('两行\n签名')],
                            {'friend_poke': {'status': 'failed', 'retcode': 1200, 'data': None}})
    assert outcomes == ['RuntimeError:QQ_ACTION_UNCONFIRMED', 'ValueError:QQ_LIKE_INVALID',
                        'ValueError:QQ_STATUS_INVALID', 'ValueError:QQ_SIGNATURE_INVALID']
    assert [item['action'] for item in seen] == ['friend_poke']


def test_a_qzone_post_returns_its_id_and_shows_only_to_her_friends(tmp_path):
    photo = tmp_path / 'today.png'
    photo.write_bytes(b'png')
    outcomes, seen = napcat([lambda s: s.qzone_post('今天的猫好乖', [photo]), lambda s: s.cookies('user.qzone.qq.com'),
                             lambda s: s.cookies('evil.example')],
                            {'send_qzone_msg': {'status': 'ok', 'retcode': 0, 'data': {'tid': 'abc123'}},
                             'get_cookies': {'status': 'ok', 'retcode': 0, 'data': {'cookies': 'p_skey=x; uin=o100', 'bkn': '1'}}})
    assert outcomes == ['abc123', 'p_skey=x; uin=o100', 'ValueError:QQ_COOKIE_DOMAIN_INVALID']
    assert seen[0] == {'action': 'send_qzone_msg', 'params': {'content': '今天的猫好乖', 'images': [photo.resolve().as_uri()],
                                                            'ugc_right': 4}}
    assert seen[1] == {'action': 'get_cookies', 'params': {'domain': 'user.qzone.qq.com'}}


class Sender:
    """A QQ sender that records what a reply sends, in order."""
    def __init__(self, fail=()):
        self.log, self.fail = [], set(fail)
        self.plain = self
    async def __call__(self, text):
        self.log.append(('text', text)); return 'm1'
    async def audio(self, path):
        self.log.append(('audio', str(path))); return 'm2'
    async def poke(self):
        self.log.append(('poke',))
        if 'poke' in self.fail:
            raise RuntimeError('QQ_ACTION_UNCONFIRMED')


def reply(text, *, poke=False, audio=None, fail=(), message_id='42'):
    async def scenario():
        rows = []
        async def generate(event, row):
            if poke:
                row['qq_poke'] = 'PENDING'
            if audio:
                row['prepared_audio'] = audio
            return text
        async def commit(row):
            pass
        sender = Sender(fail)
        chat = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
        await chat.handle(PersonalMessage('qq', '100', '200', message_id, '晚安'), sender)
        return sender.log, rows[-1]
    return asyncio.run(scenario())


def test_her_poke_goes_just_before_her_message():
    log, row = reply('戳你一下', poke=True)
    assert log == [('poke',), ('text', '戳你一下')] and row['qq_poke'] == 'SENT'


def test_a_lost_poke_never_holds_the_message_back():
    log, row = reply('戳你一下', poke=True, fail={'poke'})
    assert log[-1] == ('text', '戳你一下') and row['qq_poke'] == 'FAILED' and row['delivery_status'] == 'DELIVERED'


def test_a_face_in_a_voice_reply_follows_the_voice_and_is_never_spoken(tmp_path, monkeypatch):
    from runtime.personal_chat import backend
    spoken = []
    class Server:
        def render_reply_audio(self, text, path, **kwargs):
            spoken.append(text); return {'duration_seconds': 1}
    monkeypatch.setenv('OLIVIA_TTS_CONFIG', str(tmp_path / 'config.json'))
    asyncio.run(backend.prepare_chat_audio(Server(), '晚安[月亮]，明天见', tmp_path / 'v.wav'))
    assert spoken == ['晚安，明天见']
    audio = tmp_path / 'v.wav'
    audio.write_bytes(b'RIFF')
    log, row = reply('晚安[月亮]，明天见', audio=str(audio))
    assert [entry[0] for entry in log] == ['audio', 'text'] and log[1] == ('text', '[月亮]')
    assert row['qq_face_after_voice'] == 'SENT'


def test_the_exchange_runner_passes_every_gesture_to_the_live_connection():
    from runtime.personal_chat.exchange_runner import ExchangeRunner
    calls = []
    class Live:
        def is_available(self):
            return True
        def for_exchange(self, event):
            return self
        async def __call__(self, text):
            calls.append(('text', text))
    live = Live()
    for name in ('poke', 'like', 'status', 'signature', 'qzone_post', 'cookies'):
        async def record(*args, _name=name):
            calls.append((_name, *args))
        setattr(live, name, record)
    async def handler(event, send):
        pass
    runner = ExchangeRunner(handler)
    runner.ready('qq', live)
    async def scenario():
        await runner.send.poke()
        await runner.send.status('8', '睡觉中')
        await runner.send.like(10)
        correlated = runner.send.for_exchange(PersonalMessage('qq', '100', '200', '42', 'hi'))
        await correlated.poke()
        await correlated.plain('[月亮]')
    asyncio.run(scenario())
    assert calls == [('poke',), ('status', '8', '睡觉中'), ('like', 10), ('poke',), ('text', '[月亮]')]
