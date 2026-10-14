import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.call import context as call_context, http as call_http, store as call_store
from runtime.memory.conversation_memory_delivery import CanonicalMemoryDelivery
from runtime.memory.conversation_memory_outbox import _delivery_from_row

NOW = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)  # 21:30 Beijing


class Server:
    def __init__(self):
        self.store = SimpleNamespace(personal_chats=[], calls=[])
        self.persisted = 0

    def _persist_store_state(self):
        self.persisted += 1

    @staticmethod
    def ok(data=None):
        return {'code': 200, 'data': data}

    @staticmethod
    def err(code, msg, data=None):
        return {'code': code, 'msg': msg, 'data': data}

    @staticmethod
    def CORS_HEADERS(request):
        return {}


@pytest.mark.parametrize('state,expected', [
    ({'rhythm': {'phase': 'bathing'}}, 'missed'),
    ({'rhythm': {'phase': 'free', 'authored_bath': {'source_id': 'b'}}}, 'missed'),
    ({'rhythm': {'phase': 'focus'}, 'current': {'activity_kind': 'class'}, 'schedule': {'current_class': {'title': '心理学'}}}, 'brief'),
    ({'rhythm': {'phase': 'focus'}, 'current': {'activity_kind': 'practice'}, 'schedule': {'current_class': {'title': '心理学'}}}, 'answer'),
    ({'rhythm': {'phase': 'sleep'}}, 'sleepy'),
    ({'rhythm': {'phase': 'interrupted_rest'}}, 'sleepy'),
    ({'rhythm': {'phase': 'dinner'}}, 'answer'),
])
def test_pickup_follows_her_state(state, expected):
    assert call_context.pickup(state) == expected


def test_local_time_reads_naturally():
    assert call_context.local_now_text(NOW) == '星期三晚上21:30'


def finished(server, call_id='a1b2c3d4e5f6', transcript=None, seconds=125):
    transcript = transcript or [{'role': 'her', 'text': '喂？'}, {'role': 'user', 'text': '我今天加班好累'},
                                {'role': 'her', 'text': '辛苦啦，快歇会儿。', 'interrupted': True},
                                {'role': 'robot', 'text': 'x'}, {'role': 'user', 'text': '  '}]
    return call_store.record_call(server, {'call_id': call_id, 'seconds': seconds, 'charge_cents': 84, 'reason': 'hangup',
                                           'pickup': 'answer', 'transcript': transcript,
                                           'notes': {'summary': '聊了加班。', 'her_promises': ['明天弹琴给他听'], 'x': 1}}, now=NOW)


def test_finished_call_is_kept_once_and_cleaned():
    server = Server()
    row = finished(server)
    assert finished(server) is row and len(server.store.calls) == 1
    assert [r['role'] for r in row['transcript']] == ['her', 'user', 'her']
    assert row['transcript'][2]['interrupted'] is True
    assert row['seconds'] == 125 and row['charge_cents'] == 84
    assert row['notes'] == {'summary': '聊了加班。', 'user_shared': [], 'her_promises': ['明天弹琴给他听']}
    with pytest.raises(ValueError):
        call_store.record_call(server, {'call_id': '../x'}, now=NOW)


def test_call_becomes_canonical_memory_in_chunks():
    server = Server()
    lines = [{'role': 'user' if i % 2 else 'her', 'text': f'第{i}句。' + '这是一句比较长的话。' * 3} for i in range(80)]
    row = finished(server, transcript=lines, seconds=1800)
    rows = list(call_store.memory_rows(row))
    assert len(rows) > 1
    assert rows[0]['content'].startswith('（语音电话里我对她说的话，约30分钟，第1/')
    # Memory reads the message as the user's words and the reply as hers: never mixed.
    assert '第1句' in rows[0]['content'] and '第0句' not in rows[0]['content']
    assert '第0句' in rows[0]['reply_text'] and '第1句' not in rows[0]['reply_text']
    assert rows[0]['reply_text'].endswith('通话笔记：聊了加班。；她答应：明天弹琴给他听')
    assert '通话笔记' not in rows[1]['reply_text']
    assert len({r['letter_id'] for r in rows}) == len(rows)
    from runtime.call.store import _spans
    for item in rows:
        delivery = _delivery_from_row(item, user_id='olivia-user')
        assert isinstance(delivery, CanonicalMemoryDelivery)
        assert _spans(item['content']) <= 64 and _spans(item['reply_text']) <= 64
    said = ''.join(r['content'] for r in rows) + ''.join(r['reply_text'] for r in rows)
    assert all(f'第{i}句' in said for i in range(80))
    assert list(call_store.memory_rows({'missed': True, 'call_id': 'x'})) == []


def test_calls_kept_before_the_split_format_keep_their_delivered_rows():
    server = Server()
    row = dict(finished(server))
    row.pop('memory_format')
    rows = list(call_store.memory_rows(row))
    assert rows[0]['content'].startswith('（用户和她的语音电话记录') and rows[0]['reply_text'].startswith('通话笔记')


def test_world_facts_carry_her_meals_weather_events_and_classes():
    now = datetime(2026, 10, 14, 4, 53, tzinfo=timezone.utc)  # 12:53 Beijing
    snapshot = {
        'current': {'activity': '练琴：乐句衔接', 'location': '家里音乐房', 'note': '顺了一些。'},
        'rhythm': {},
        'world': {
            'today_activities': [{'note': '音乐史：记下疑问。'}, {'note': '顺了一些。'}, {'note': '顺了一些。'}],
            'meals': [
                {'slot': 'lunch', 'food': '米饭和番茄炒蛋', 'status': 'eaten', 'date': '2026-10-14',
                 'started_at': '2026-10-14T04:00:50+00:00'},
                {'slot': 'breakfast', 'food': '豆浆和包子', 'status': 'eaten', 'date': '2026-10-14',
                 'started_at': '2026-10-14T08:30:00+08:00'},
                {'slot': 'dinner', 'food': '清蒸鲈鱼', 'status': 'eaten', 'date': '2026-10-12',
                 'started_at': '2026-10-12T18:30:00+08:00'}],
            'weather': {'city': '上海', 'temperature_c': 24.0, 'cloud_cover': 'CAVOK', 'weather_codes': '', 'status': 'fresh'},
            'recent_episodes': [{'occurred_at': '2026-10-14T04:20:50+00:00', 'process': [{'obstacle': '吃得比预想慢'}],
                                 'result': {'detail': '这一餐吃完了。'}}],
            'schedule': {'classes': [{'title': '音乐史', 'start': '2026-10-14T10:00:00+08:00', 'end': '2026-10-14T11:40:00+08:00'}]},
        },
        'projects': [],
    }
    server = SimpleNamespace(daily_life_runtime=SimpleNamespace(store=SimpleNamespace(snapshot=lambda now: snapshot)))
    facts, _ = call_context.world(server, now)
    assert facts['meals'] == ['早饭：豆浆和包子（08:30，吃过了）', '午饭：米饭和番茄炒蛋（12:00，吃过了）']
    assert facts['weather'].startswith('上海 24度 晴')
    assert facts['events'] == ['12:20 吃得比预想慢，这一餐吃完了。']
    assert facts['classes'] == ['音乐史 10:00-11:40（按课表已经下课）']
    assert facts['today'] == ['音乐史：记下疑问。', '顺了一些。']


def test_a_new_call_knows_the_calls_before_it():
    server = Server()
    finished(server)
    calls = call_context.recent_calls(server, NOW + timedelta(hours=1))
    assert calls == [{'at': calls[0]['at'], 'minutes': 2, 'summary': '聊了加班。'}]
    assert call_context.recent_calls(server, NOW + timedelta(days=3)) == []


def test_qq_sees_recent_call_and_missed_calls_after_the_last_chat():
    server = Server()
    assert call_store.recent_context(server, now=NOW) == ''
    server.store.personal_chats.append({'life_received_at': (NOW - timedelta(hours=2)).isoformat()})
    call_store.record_missed(server, 'bathing', now=NOW - timedelta(hours=3))  # before the last chat: old news
    call_store.record_missed(server, 'bathing', now=NOW - timedelta(minutes=30))
    finished(server)
    text = call_store.recent_context(server, now=NOW + timedelta(minutes=5))
    assert text.startswith('\n\n[语音电话]')
    assert '给你打过电话，你当时在洗澡没接到。' in text and '打了2次' not in text
    assert '通过电话，约2分钟，聊了：聊了加班。' in text
    assert call_store.recent_context(server, now=NOW + timedelta(days=2)) == ''


class Request:
    def __init__(self, path, method='GET', body=None, confirmed=True):
        self.path, self.method = path, method
        self.headers = {'X-Olivia-Companion-Action': 'confirmed'} if confirmed else {}
        self._body = json.dumps(body or {})

    async def text(self):
        return self._body


def answer(response):
    return response.status, json.loads(response.body)


def test_config_without_an_account_is_closed(monkeypatch):
    monkeypatch.delenv('OLIVIA_GPU_API_URL', raising=False)
    monkeypatch.delenv('OLIVIA_GPU_API_KEY', raising=False)
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/config'))))
    assert status == 200 and value['data'] == {'available': False, 'reason': 'account'}


def test_start_misses_the_call_while_she_bathes(monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://api.example.cn')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'secret')
    monkeypatch.setattr(call_context, 'build', lambda server, backend: ({'persona': 'p'}, 'missed'))
    server = Server()
    status, value = answer(asyncio.run(call_http.call_http(server, Request('/toy/call/start', 'POST'))))
    assert status == 200 and value['data'] == {'pickup': 'missed'}
    assert server.store.calls[0]['missed'] is True and server.store.calls[0]['reason'] == 'bathing'


def test_start_trades_the_key_for_a_ticket(monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://api.example.cn')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'secret')
    monkeypatch.setattr(call_context, 'build', lambda server, backend: ({'persona': 'p', 'world': {'now': 'x'}}, 'sleepy'))
    sent = {}

    async def cloud(method, path, *, timeout, headers=None, body=None):
        sent.update(method=method, path=path, headers=headers, body=body)
        return 200, {'ticket': 't' * 40, 'expires_in': 60, 'cents_per_minute': 40}
    monkeypatch.setattr(call_http, '_cloud_json', cloud)
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/start', 'POST'))))
    assert status == 200
    assert value['data'] == {'pickup': 'sleepy', 'ticket': 't' * 40, 'ws_url': 'wss://api.example.cn/v1/call',
                             'cents_per_minute': 40}
    assert sent['path'] == '/v1/call/ticket' and sent['headers']['Authorization'] == 'Bearer secret'
    assert sent['body'] == {'v': 1, 'context': {'persona': 'p', 'world': {'now': 'x'}}, 'pickup': 'sleepy'}
    assert 'secret' not in json.dumps(value)


def test_start_needs_confirmation_and_maps_cloud_refusals(monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://api.example.cn')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'secret')
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/start', 'POST', confirmed=False))))
    assert status == 403
    monkeypatch.setattr(call_context, 'build', lambda server, backend: ({'persona': 'p'}, 'answer'))

    async def refused(*args, **kwargs):
        return 503, None
    monkeypatch.setattr(call_http, '_cloud_json', refused)
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/start', 'POST'))))
    assert status == 503 and value['data']['error_code'] == 'CALL_BUSY'


def test_end_report_is_saved(monkeypatch):
    server = Server()
    body = {'call_id': 'abcdef123456', 'seconds': 61, 'charge_cents': 41, 'transcript': [{'role': 'her', 'text': '喂？'}]}
    status, value = answer(asyncio.run(call_http.call_http(server, Request('/toy/call/end', 'POST', body))))
    assert status == 200 and value['data'] == {'saved': True}
    assert server.store.calls[0]['call_id'] == 'abcdef123456' and server.persisted == 1


def test_call_page_script_is_in_the_injected_bootstrap():
    from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
    from runtime.call_ui import JAVASCRIPT
    assert JAVASCRIPT in BOOTSTRAP_JAVASCRIPT
    assert "getUserMedia({audio: {echoCancellation: true" in JAVASCRIPT
    assert '/toy/call/start' in JAVASCRIPT and 'OLIVIA_GPU_API_KEY' not in JAVASCRIPT


def test_page_scripts_invalidate_the_frontend_repair_receipt():
    # call.2 changed only runtime/call_ui.py and clients kept the call.1 page (2026-10-14).
    from pathlib import Path
    from installer.start_local import _frontend_patcher_files
    source = Path(__file__).resolve().parents[2]
    names = {path.relative_to(source).as_posix() for path in _frontend_patcher_files(source)}
    assert {'runtime/call_ui.py', 'runtime/native_piano_ui.py', 'original_client_settings_ui.py'} <= names


def test_the_chosen_model_goes_with_the_ticket_and_config_lists_the_models(monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://api.example.cn')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'secret')
    monkeypatch.setattr(call_context, 'build', lambda server, backend: ({'persona': 'p'}, 'answer'))
    sent = []

    async def cloud(method, path, *, timeout, headers=None, body=None):
        sent.append(body)
        if path == '/v1/call/config':
            return 200, {'available': True, 'cents_per_minute': 40, 'default_model': 'flash', 'models': [
                {'id': 'flash', 'cents_per_minute': 40, 'base_cents_per_minute': 40, 'busy_cents_per_minute': 48},
                {'id': 'plus', 'cents_per_minute': 80, 'base_cents_per_minute': 80, 'busy_cents_per_minute': 96}]}
        return 200, {'ticket': 't' * 40, 'cents_per_minute': 80, 'model': 'plus'}
    monkeypatch.setattr(call_http, '_cloud_json', cloud)
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/config'))))
    assert [m['id'] for m in value['data']['models']] == ['flash', 'plus']
    assert value['data']['models'][1]['busy_cents_per_minute'] == 96 and value['data']['default_model'] == 'flash'
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/start', 'POST', {'model': 'plus'}))))
    assert status == 200 and sent[-1]['model'] == 'plus' and value['data']['cents_per_minute'] == 80
    status, value = answer(asyncio.run(call_http.call_http(Server(), Request('/toy/call/start', 'POST', {'model': '../x'}))))
    assert status == 400


def _life_server(snapshot, concerns=()):
    emotion = SimpleNamespace(view=lambda now, read_only=False: {'concerns': [{'summary': c} for c in concerns]})
    return SimpleNamespace(daily_life_runtime=SimpleNamespace(store=SimpleNamespace(snapshot=lambda now: snapshot),
                                                              emotion=emotion))


def test_an_old_observation_is_not_presented_as_what_she_does_now():
    now = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # 18:00 Beijing
    snapshot = {'stale': True, 'rhythm': {}, 'world': {}, 'projects': [],
                'current': {'activity': '练琴', 'location': '家里音乐房', 'note': '顺了一些。',
                            'occurred_at': '2026-10-14T04:20:50+00:00'}}
    facts, _ = call_context.world(_life_server(snapshot), now)
    assert 'activity' not in facts and 'location' not in facts
    assert facts['last'] == '12:20 练琴，家里音乐房，顺了一些。'


def test_what_is_on_her_mind_goes_with_the_call():
    server = _life_server({}, concerns=['对方说最近加班很累', '对方明天面试'])
    assert call_context.concerns(server, NOW) == ['对方说最近加班很累', '对方明天面试']


def test_a_report_the_page_never_got_is_fetched_from_the_cloud(monkeypatch):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://api.example.cn')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'secret')
    asked = []

    async def cloud(method, path, *, timeout, headers=None, body=None):
        asked.append((path, headers))
        if path.endswith('pending0001'):
            return 404, None
        return 200, {'type': 'end', 'call_id': 'abcdef123456', 'seconds': 61, 'charge_cents': 41,
                     'transcript': [{'role': 'her', 'text': '喂？'}], 'notes': {'summary': '打了个招呼。'}}
    monkeypatch.setattr(call_http, '_cloud_json', cloud)
    server = Server()
    status, value = answer(asyncio.run(call_http.call_http(server, Request('/toy/call/recover', 'POST', {'call_id': 'abcdef123456'}))))
    assert status == 200 and value['data'] == {'saved': True}
    assert asked[0] == ('/v1/call/report?call_id=abcdef123456', {'Authorization': 'Bearer secret'})
    assert server.store.calls[0]['call_id'] == 'abcdef123456' and server.store.calls[0]['notes']['summary'] == '打了个招呼。'
    status, value = answer(asyncio.run(call_http.call_http(server, Request('/toy/call/recover', 'POST', {'call_id': 'pending0001'}))))
    assert status == 503 and value['data']['error_code'] == 'CALL_REPORT_PENDING'


def test_no_proactive_contact_during_a_call_or_right_after_it():
    server = Server()
    now = NOW.timestamp()
    assert call_store.quiet_reason(server, now, 600) is None
    call_store.mark_active(server, NOW)
    assert call_store.quiet_reason(server, now + 60, 600) == 'in_call'          # the page is in a call
    call_store.mark_ended(server)
    finished(server)                                                          # ended at NOW
    assert call_store.quiet_reason(server, now + 300, 600) == 'in_call'       # just hung up
    assert call_store.quiet_reason(server, now + 900, 600) is None


def test_finished_calls_reach_her_diary_as_exchanges():
    from runtime.diary.diary import day_messages
    server = Server()
    finished(server)
    rows = call_store.diary_rows(server)
    messages = day_messages(rows, (NOW - timedelta(minutes=2)).astimezone(timezone(timedelta(hours=8))).date().isoformat())
    assert len(messages) == 1 and messages[0]['channel'] == 'call'
    assert messages[0]['user'].endswith('我今天加班好累') and '辛苦啦' in messages[0]['linli']


class Memories:
    def __init__(self):
        self.queries = []

    def list_memories(self, *, query, limit):
        self.queries.append(query)
        return [SimpleNamespace(text='对方周五有面试'), SimpleNamespace(text='对方养了一只猫')]

    def browse_memories(self, **options):
        return {'memories': [SimpleNamespace(text='对方养了一只猫'),
                             SimpleNamespace(text='林' + '离已发给用户的生成照片，画面：琴房'),
                             SimpleNamespace(text='对方怕冷')]}


def test_call_memories_put_what_they_talked_about_first_then_the_newest():
    backend = Memories()
    picked = call_context.memories(backend, '我周五要面试了')
    assert picked == ['对方周五有面试', '对方养了一只猫', '对方怕冷']   # no photo descriptions among the newest
    assert backend.queries == ['我周五要面试了']
    assert call_context.recall(backend, '嗯') == []                    # too short to look anything up
    assert call_context.recall(backend, '你还记得我那只猫吗') == ['对方周五有面试', '对方养了一只猫']


def test_call_days_count_toward_the_relationship_like_chats():
    from runtime.memory.effective_relationship import relationship_history
    now = NOW.timestamp()
    store = SimpleNamespace(letters=[], personal_chats=[], calls=[
        {'call_id': 'a', 'started_at': (NOW - timedelta(days=1)).isoformat(), 'seconds': 300},
        {'call_id': 'b', 'started_at': (NOW - timedelta(days=2)).isoformat(), 'seconds': 10},     # too short
        {'call_id': 'c', 'started_at': (NOW - timedelta(days=3)).isoformat(), 'missed': True},
        {'call_id': 'd', 'started_at': (NOW - timedelta(days=30)).isoformat(), 'seconds': 300}]) # too old
    assert relationship_history(store, now) == {14: 1, 21: 1}


def test_a_finished_call_enters_her_life_once():
    from runtime.call import life
    consumed = []

    class Runtime:
        store = SimpleNamespace(exchange_relationship=lambda source, user, her: None)

        async def consume_exchange(self, source_id, user, her, *, occurred_at, received_at):
            consumed.append((source_id, user, her, received_at < occurred_at))

    server = Server()
    server.daily_life_runtime = Runtime()
    row = finished(server)
    assert row['life_status'] == 'PENDING'
    asyncio.run(life.absorb(server, row))
    assert consumed == [('reply:call-a1b2c3d4e5f6:1', '我今天加班好累', '喂？\n辛苦啦，快歇会儿。', True)]
    assert row['life_status'] == 'COMMITTED' and not life.pending(row)
