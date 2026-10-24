"""Her QQ life around the chat: status, daily like, 说说, and answers on QQ空间 (2.5.0)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from runtime.personal_chat import qq_presence, qzone
from runtime.personal_chat.events import PersonalMessage

SH = timezone(timedelta(hours=8))


def at(hour, minute=0, day=24):
    return datetime(2026, 10, day, hour, minute, tzinfo=SH)


class Sender:
    def __init__(self, fail=()):
        self.log, self.fail = [], set(fail)
    async def _do(self, *entry):
        self.log.append(entry)
        if entry[0] in self.fail:
            raise RuntimeError('QQ_ACTION_UNCONFIRMED')
    async def status(self, face, wording): await self._do('status', face, wording)
    async def like(self, times): await self._do('like', times)
    async def signature(self, text): await self._do('signature', text)
    async def qzone_post(self, content, images=()):
        await self._do('qzone_post', content, [str(p) for p in images]); return 'tid-1'
    async def cookies(self, domain):
        await self._do('cookies', domain); return 'p_skey=abc; uin=o0100'


class Life:
    def __init__(self, kind='practice', phase=None, today=()):
        self.kind, self.phase, self.today = kind, phase, list(today)
    def snapshot(self, now):
        return {'current': {'activity_kind': self.kind}, 'rhythm': {'phase': self.phase} if self.phase else {},
                'world': {'today_activities': self.today}}


class Gateway:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []
    async def complete_structured_scoped(self, messages, *, response_format, scope, request_id=None):
        self.calls.append({'request_id': request_id, 'packet': json.loads(messages[1]['content'])})
        return SimpleNamespace(text=json.dumps(self.answers.pop(0), ensure_ascii=False))


def server(tmp_path, *, life=None, rows=(), answers=()):
    value = SimpleNamespace(_state_root=lambda: tmp_path, daily_life_runtime=SimpleNamespace(store=life or Life()),
                            store=SimpleNamespace(personal_chats=list(rows)),
                            letters_adapter=SimpleNamespace(gateway=Gateway(answers)), logs=[])
    value._safe_log = lambda name, **fields: value.logs.append((name, fields))
    return value


@pytest.fixture(autouse=True)
def no_persona(monkeypatch):
    from runtime.call import context
    monkeypatch.setattr(context, 'persona_text', lambda server: '林离。')
    monkeypatch.setattr(context, 'relationship_text', lambda server: '关系阶段：朋友。')


def run(srv, sender, now, connected=True):
    runtime = {'status': {'qq': 'CONNECTED' if connected else 'RECONNECTING'}}
    initiative = SimpleNamespace(target=(PersonalMessage('qq', '100', '200', 'm', 'hi'), sender))
    asyncio.run(qq_presence.tick(srv, runtime, initiative, now=now.astimezone(timezone.utc)))
    path = srv._state_root() / qq_presence.STATE_NAME
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def test_switches_default_on_and_only_whole_valid_settings_are_saved(tmp_path):
    assert qq_presence.settings(tmp_path) == {'status': True, 'like': True, 'qzone': True, 'gestures': True}
    qq_presence.save_settings(tmp_path, {'status': False, 'like': True, 'qzone': False, 'gestures': True})
    assert qq_presence.settings(tmp_path)['status'] is False and qq_presence.settings(tmp_path)['qzone'] is False
    for bad in ({'status': 1, 'like': True, 'qzone': True, 'gestures': True}, {'status': True}, []):
        with pytest.raises(ValueError):
            qq_presence.save_settings(tmp_path, bad)


@pytest.mark.parametrize('kind, phase, hour, expected', [
    ('practice', None, 15, ('学习', '练琴中')), ('meal', None, 12, ('干饭', '吃饭中')),
    ('rest', None, 14, ('困', '休息一下')), ('rest', None, 23, ('睡', '睡觉中')),
    ('class', 'sleep', 2, ('睡', '睡觉中')), ('walk', 'bathing', 21, ('转圈', '洗澡中')), (None, None, 15, None),
])
def test_her_status_follows_what_she_is_doing(kind, phase, hour, expected):
    assert qq_presence.status_for(Life(kind, phase).snapshot(None), at(hour)) == expected


def test_an_old_activity_is_not_shown_as_what_she_is_doing_now():
    stale = {'current': {'activity_kind': 'practice'}, 'rhythm': {}, 'stale': True}
    assert qq_presence.status_for(stale, at(4)) == ('睡', '睡觉中')
    assert qq_presence.status_for(stale, at(15)) is None
    assert qq_presence.status_for({'current': None, 'rhythm': {}}, at(2)) == ('睡', '睡觉中')


def test_she_is_not_shown_asleep_while_she_is_chatting(tmp_path):
    assert qq_presence.status_for(Life('rest', 'sleep').snapshot(None), at(3), chatting=True) == ('困', '刚被吵醒')
    assert qq_presence.status_for(Life('rest').snapshot(None), at(23, 30), chatting=True) == ('困', '刚被吵醒')
    rows = [{'channel': 'qq', 'delivery_status': 'DELIVERED', 'created_at': at(2, 50).timestamp(), 'letter_id': 'x'}]
    srv, sender = server(tmp_path, life=Life('rest', 'sleep'), rows=rows), Sender()
    qq_presence.save_settings(tmp_path, {'status': True, 'like': False, 'qzone': False, 'gestures': True})
    run(srv, sender, at(3))
    run(srv, sender, at(3, 40))
    assert sender.log == [('status', qq_presence_face('困'), '刚被吵醒'), ('status', qq_presence_face('睡'), '睡觉中')]


def test_status_changes_only_when_her_activity_does_and_not_more_than_every_two_minutes(tmp_path):
    life = Life('practice')
    srv, sender = server(tmp_path, life=life), Sender()
    qq_presence.save_settings(tmp_path, {'status': True, 'like': False, 'qzone': False, 'gestures': True})
    run(srv, sender, at(15))
    run(srv, sender, at(15, 1))
    life.kind = 'meal'
    run(srv, sender, at(15, 1))
    run(srv, sender, at(15, 3))
    assert sender.log == [('status', qq_presence_face('学习'), '练琴中'), ('status', qq_presence_face('干饭'), '吃饭中')]


def qq_presence_face(name):
    from runtime.personal_chat.qq_faces import FACE_IDS
    return FACE_IDS[name]


def test_one_like_a_day_from_nine(tmp_path):
    srv, sender = server(tmp_path), Sender(fail={'like'})
    qq_presence.save_settings(tmp_path, {'status': False, 'like': True, 'qzone': False, 'gestures': True})
    run(srv, sender, at(8))
    state = run(srv, sender, at(9))
    run(srv, sender, at(10))  # the failed try is not repeated today
    run(srv, sender, at(9, day=25))
    assert sender.log == [('like', 10), ('like', 10)] and state['error'] == 'QQ_ACTION_UNCONFIRMED'


def test_nothing_happens_while_qq_is_not_connected_or_everything_is_off(tmp_path):
    srv, sender = server(tmp_path), Sender()
    run(srv, sender, at(21), connected=False)
    assert sender.log == []
    qq_presence.save_settings(tmp_path, {'status': False, 'like': False, 'qzone': False, 'gestures': False})
    run(srv, sender, at(21))
    assert sender.log == []


def photo_row(tmp_path, key, when, description):
    path = tmp_path / f'{key}.png'
    path.write_bytes(b'png')
    return {'letter_id': key, 'channel': 'qq', 'image_delivery_status': 'DELIVERED', 'prepared_image': str(path),
            'created_at': when.timestamp(), 'image_description': description}


def evening_only(tmp_path):
    qq_presence.save_settings(tmp_path, {'status': False, 'like': False, 'qzone': True, 'gestures': True})
    (tmp_path / qq_presence.STATE_NAME).write_text(json.dumps({'watched_at': 9e12}), encoding='utf-8')


def test_an_evening_post_uses_a_photo_she_sent_today_and_may_change_her_signature(tmp_path):
    rows = [photo_row(tmp_path, 'old', at(21, day=23), '昨天的猫'), photo_row(tmp_path, 'cat', at(16), '猫趴在琴凳上')]
    life = Life(today=[{'occurred_at': '2026-10-24T07:00:00+00:00', 'activity': '练了一下午肖邦', 'note': ''}])
    srv, sender = server(tmp_path, life=life, rows=rows,
                         answers=[{'post': '下午练琴，猫一直趴在琴凳上陪我', 'photo': 'cat', 'signature': '琴声和猫'}]), Sender()
    evening_only(tmp_path)
    run(srv, sender, at(19, 59))
    assert sender.log == []
    state = run(srv, sender, at(20, 30))
    call = srv.letters_adapter.gateway.calls[0]
    assert call['request_id'] == 'qzone-post:2026-10-24' and [p['id'] for p in call['packet']['photos']] == ['cat']
    assert sender.log == [('qzone_post', '下午练琴，猫一直趴在琴凳上陪我', [rows[1]['prepared_image']]), ('signature', '琴声和猫')]
    assert state['posted'] == '2026-10-24' and state['post_tid'] == 'tid-1'
    run(srv, sender, at(21))
    assert len(sender.log) == 2  # one 说说 a day


def test_she_may_choose_not_to_post_and_a_failed_post_is_tried_an_hour_later(tmp_path):
    life = Life(today=[{'occurred_at': 'x', 'activity': '看书', 'note': ''}])
    srv, sender = server(tmp_path, life=life, answers=[{'post': '', 'photo': None, 'signature': None}]), Sender()
    evening_only(tmp_path)
    assert run(srv, sender, at(20))['post_result'] == 'skipped' and sender.log == []
    srv, sender = server(tmp_path / 'b', life=life, answers=[{'post': '今天读完了一本书', 'photo': None, 'signature': None}] * 2), Sender()
    (tmp_path / 'b').mkdir()
    evening_only(tmp_path / 'b')
    sender.fail.add('qzone_post')
    state = run(srv, sender, at(20))
    assert state['error'] == 'QQ_ACTION_UNCONFIRMED'
    state = run(srv, sender, at(20, 30))
    assert len(srv.letters_adapter.gateway.calls) == 1  # waiting for the retry hour
    sender.fail.clear()
    state = run(srv, sender, at(21, 1))
    assert state['post_tid'] == 'tid-1'


class Zone:
    """QQ空间 as the client sees it, recorded."""
    lists, calls = {}, []
    def __init__(self, cookies, self_uin):
        assert cookies == 'p_skey=abc; uin=o0100' and self_uin == '100'
    async def posts(self, uin, count=10):
        return Zone.lists.get(uin, [])
    async def like(self, uin, tid, created):
        Zone.calls.append(('like', uin, tid))
    async def comment(self, uin, tid, text, *, reply_to=None):
        Zone.calls.append(('comment', uin, tid, text, reply_to))


def test_his_new_post_gets_a_like_and_a_comment_and_his_comment_an_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(qzone, 'Qzone', Zone)
    now = at(21)
    old = {'tid': 'old', 'text': '旧的', 'created': int(now.timestamp()) - 7200, 'pictures': 0, 'comments': []}
    Zone.lists, Zone.calls = {'200': [old]}, []
    srv, sender = server(tmp_path, answers=[{'comment': '下次带我一起去'}, {'reply': '那你明天早点起'}]), Sender()
    qq_presence.save_settings(tmp_path, {'status': False, 'like': False, 'qzone': True, 'gestures': True})
    (tmp_path / qq_presence.STATE_NAME).write_text(json.dumps({'posted': '2026-10-24'}), encoding='utf-8')
    run(srv, sender, now)
    assert Zone.calls == []  # the first look only learns what is already there
    new = {'tid': 'new', 'text': '今天去爬山了', 'created': int(now.timestamp()) + 60, 'pictures': 2, 'comments': []}
    mine = {'tid': 'mine', 'text': '下午练琴', 'created': int(now.timestamp()), 'pictures': 1,
            'comments': [{'id': '1', 'uin': '200', 'text': '想听你弹', 'created': int(now.timestamp()) + 100},
                         {'id': '2', 'uin': '999', 'text': '路人', 'created': int(now.timestamp()) + 100}]}
    Zone.lists = {'200': [new, old], '100': [mine]}
    run(srv, sender, now + timedelta(minutes=31))
    assert Zone.calls == [('like', '200', 'new'), ('comment', '200', 'new', '下次带我一起去', None),
                          ('comment', '100', 'mine', '那你明天早点起', ('1', '200'))]
    calls = srv.letters_adapter.gateway.calls
    assert calls[0]['request_id'] == 'qzone-comment:new' and calls[0]['packet'] == {'post': '今天去爬山了', 'pictures': 2}
    assert calls[1]['request_id'] == 'qzone-reply:mine:1'
    run(srv, sender, now + timedelta(minutes=62))
    assert len(Zone.calls) == 3  # nothing twice


def test_qzone_token_and_responses():
    assert qzone.g_tk('uin=o1; p_skey=abc') == qzone.g_tk('p_skey=abc') == ((5381 * 33 + 97) * 33 + 98) * 33 + 99 & 0x7fffffff
    with pytest.raises(qzone.QzoneUnavailable):
        qzone.g_tk('skey=x')
    assert qzone._json('_preloadCallback({"code":0,"msglist":[]});') == {'code': 0, 'msglist': []}
    assert qzone._json('<script>frameElement.callback({"code":0});</script>') == {'code': 0}
    page = ('<script type="text/javascript">\nvar cb;try{cb=frameElement.callback;}catch(e){}\n'
            'frameElement.callback(\n{\n\t"code":0,\n\t"data":{"ret":0}\n}\n);\n</script>')
    assert qzone._json(page) == {'code': 0, 'data': {'ret': 0}}  # the shape QQ空间 really answers (2026-10-24)
    with pytest.raises(qzone.QzoneUnavailable):
        qzone._json('<html>login</html>')


def test_qzone_requests_carry_the_token_cookies_and_fields(monkeypatch):
    async def scenario():
        seen = []
        async def handler(request):
            form = dict(await request.post()) if request.method == 'POST' else {}
            seen.append((request.method, request.path, dict(request.query), form, request.headers.get('Cookie')))
            if request.path.endswith('emotion_cgi_msglist_v6'):
                return web.Response(text=json.dumps({'code': 0, 'msglist': [{'tid': 't1', 'content': '爬山', 'created_time': 5,
                    'pic': [{}, {}], 'commentlist': [{'tid': 3, 'uin': 200, 'content': '好看', 'create_time': 6}]}]}))
            return web.Response(text='<script>frameElement.callback({"code":0});</script>')
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', handler)
        async with TestServer(app) as srv:
            monkeypatch.setattr(qzone, 'BASE', str(srv.make_url('')).rstrip('/'))
            zone = qzone.Qzone('p_skey=abc; uin=o0100', '100')
            posts = await zone.posts('200')
            await zone.like('200', 't1', 5)
            await zone.comment('200', 't1', '真好')
            await zone.comment('100', 't9', '回你', reply_to=('3', '200'))
        return posts, seen
    posts, seen = asyncio.run(scenario())
    assert posts == [{'tid': 't1', 'text': '爬山', 'created': 5, 'pictures': 2,
                      'comments': [{'id': '3', 'uin': '200', 'text': '好看', 'created': 6}]}]
    token = str(qzone.g_tk('p_skey=abc'))
    assert seen[0][2]['uin'] == '200' and seen[0][2]['g_tk'] == token and seen[0][4] == 'p_skey=abc; uin=o0100'
    assert seen[1][3]['fid'] == 't1' and seen[1][3]['opuin'] == '100' and seen[1][2]['g_tk'] == token
    assert seen[2][3]['topicId'] == '200_t1__1' and seen[2][3]['content'] == '真好' and 'commentId' not in seen[2][3]
    assert seen[3][3]['commentId'] == '3' and seen[3][3]['commentUin'] == '200' and seen[3][3]['hostUin'] == '100'
