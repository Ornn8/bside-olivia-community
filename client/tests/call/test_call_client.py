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
    lines = [{'role': 'user' if i % 2 else 'her', 'text': '这是一句比较长的话' * 12} for i in range(60)]
    row = finished(server, transcript=lines, seconds=1800)
    rows = list(call_store.memory_rows(row))
    assert len(rows) > 1
    assert rows[0]['content'].startswith('（用户和她的语音电话记录，约30分钟，第1/')
    assert rows[0]['reply_text'] == '通话笔记：聊了加班。；她答应：明天弹琴给他听'
    assert rows[1]['reply_text'] == '（同一通电话的后续）'
    assert len({r['letter_id'] for r in rows}) == len(rows)
    for item in rows:
        delivery = _delivery_from_row(item, user_id='olivia-user')
        assert isinstance(delivery, CanonicalMemoryDelivery)
        assert len(item['content']) <= call_store.CHUNK_CHARS + 60
    assert list(call_store.memory_rows({'missed': True, 'call_id': 'x'})) == []


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
