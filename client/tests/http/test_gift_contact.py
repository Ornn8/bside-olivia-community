"""A credited top-up (a cloud 'gift' event) is answered at once: QQ first, else a letter."""
import asyncio
from types import SimpleNamespace

from runtime import cloud_events

GIFT = {'id': 'gift:00000000-0000-4000-8000-000000000001', 'kind': 'gift', 'at': 1_800_000_000,
        'brief': '对方刚刚为你们的相处添了一份心意。', 'photo': True}


def test_events_are_validated_and_unknown_kinds_ignored():
    assert cloud_events.validate({'events': [GIFT, {**GIFT, 'kind': 'newer'}, {**GIFT, 'id': '../x'},
                                             {**GIFT, 'brief': 'x' * 601}]}) == [GIFT]


def test_first_run_answers_only_recent_gifts_and_each_once(tmp_path):
    now = GIFT['at'] + 60
    old = {**GIFT, 'id': 'gift:00000000-0000-4000-8000-000000000002', 'at': now - 3 * 3600}
    cloud_events.record(tmp_path, [GIFT, old], now)
    assert [item['id'] for item in cloud_events.pending(tmp_path, now)] == [GIFT['id']]
    later = {**old, 'id': 'gift:00000000-0000-4000-8000-000000000003', 'at': now + 10}
    cloud_events.record(tmp_path, [GIFT, old, later], now + 20)          # after the first run nothing is skipped
    assert len(cloud_events.pending(tmp_path, now + 20)) == 2
    # A letter waits until QQ has had its head start on each gift.
    assert [item['id'] for item in cloud_events.pending(tmp_path, now + 190, settled=180)] == [GIFT['id']]
    cloud_events.mark(tmp_path, GIFT['id'], 'qq')
    cloud_events.record(tmp_path, [GIFT], now + 30)                      # a replayed event stays answered
    assert [item['id'] for item in cloud_events.pending(tmp_path, now + 30)] == [later['id']]


def test_instruction_mentions_a_photo_only_when_one_is_sent():
    assert '照片' in cloud_events.instruction(GIFT, photo=True)
    assert '照片' not in cloud_events.instruction(GIFT, photo=False)


class Initiative:
    def __init__(self, target, free=True):
        self.target, self._free, self.attempts = target, free, 0
    def free(self):
        return self._free
    def attempted(self):
        self.attempts += 1


def qq_world(tmp_path, monkeypatch, *, free=True, status='CONNECTED'):
    from runtime.personal_chat import backend
    from runtime.personal_chat.events import PersonalMessage
    calls = []
    class Service:
        async def proactive(self, event, send, eligible, gift=None):
            calls.append((event.exchange_id, eligible(), gift))
    async def send(text):
        return 'ok'
    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    monkeypatch.setattr('runtime.reply.proactive_runtime.enabled', lambda: False)
    owner = PersonalMessage('qq', 'bot', 'owner', 'm1', '在吗')
    initiative = Initiative((owner, send), free)
    runtime = {'service': Service(), 'status': {'qq': status}}
    server = SimpleNamespace(_state_root=lambda: tmp_path)
    return backend, server, runtime, initiative, calls


def test_qq_answers_a_gift_ahead_of_ordinary_initiative(tmp_path, monkeypatch):
    backend, server, runtime, initiative, calls = qq_world(tmp_path, monkeypatch)
    cloud_events.record(tmp_path, [GIFT], GIFT['at'] + 5)
    monkeypatch.setattr('time.time', lambda: GIFT['at'] + 10)
    assert asyncio.run(backend._gift_contact(server, runtime, initiative)) is True
    assert len(calls) == 1 and calls[0][1] is True and calls[0][2]['id'] == GIFT['id']
    assert initiative.attempts == 1
    assert asyncio.run(backend._gift_contact(server, runtime, initiative)) is False   # answered once
    assert len(calls) == 1


def test_qq_waits_for_an_unresolved_user_turn_or_a_disconnected_channel(tmp_path, monkeypatch):
    cloud_events.record(tmp_path, [GIFT], GIFT['at'] + 5)
    monkeypatch.setattr('time.time', lambda: GIFT['at'] + 10)
    for free, status in ((False, 'CONNECTED'), (True, 'STOPPED')):
        backend, server, runtime, initiative, calls = qq_world(tmp_path, monkeypatch, free=free, status=status)
        assert asyncio.run(backend._gift_contact(server, runtime, initiative)) is False
        assert calls == []
    assert [item['id'] for item in cloud_events.pending(tmp_path, GIFT['at'] + 10)] == [GIFT['id']]


def test_initiative_is_not_free_while_a_user_turn_is_in_flight():
    from runtime.personal_chat.initiative import Initiative as Real
    rows = [{'letter_id': 'u1', 'delivery_status': 'GENERATING', 'created_at': 1}]
    assert Real(rows, clock=lambda: 10, interval=lambda: 1).free() is False
    rows[0]['delivery_status'] = 'DELIVERED'
    assert Real(rows, clock=lambda: 10, interval=lambda: 1).free() is True


def test_gift_photo_is_planned_like_a_requested_photo(tmp_path, monkeypatch):
    from runtime import image_reply
    calls = []

    class Cloud:
        url, token = 'https://gpu.example', 'synthetic'
        def __init__(self, *args): pass
        async def request(self, action, data):
            return {'server_media_planning': True}
        async def generate(self, kind, data, path, **kwargs):
            calls.append(data)
            return {'task_id': 't', 'stage': 'skipped'}
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration', Cloud)
    monkeypatch.setattr(image_reply, '_photo_reference', lambda *a: {'world_current_location': '家里'})
    server = SimpleNamespace(video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': True, 'resolution': '1K'}),
                             _media_root=lambda: tmp_path, _persist_store_state=lambda: None, PORT=1234)
    for row, requested in (({'letter_id': 'gift-1', 'gift_photo': True}, True), ({'letter_id': 'plain-1'}, False)):
        calls.clear()
        asyncio.run(image_reply._prepare_once(server, row, '', '收到啦，好开心', channel='qq'))
        assert calls[0]['media_request']['reference']['requested_image'] is requested


def letter_world(monkeypatch, tmp_path, now):
    import local_server as server
    monkeypatch.setattr(server.time, 'time', lambda: now)
    monkeypatch.setattr(server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(server, 'daily_life_runtime', None)
    monkeypatch.setattr(server.store, 'letters', [{'letter_id': 'a', 'content': '具体交流', 'created_at': 1000,
        'letter_status': 'COMPLETED', 'private_world_delivery_id': 'a:1', 'reply_text': '好呀'}])
    monkeypatch.setattr(server.store, 'personal_chats', [])
    monkeypatch.setattr(server, '_proactive_ready', lambda: True)
    monkeypatch.setattr(server, 'video_reply_settings_store',
                        SimpleNamespace(image_snapshot=lambda: {'enabled': True, 'resolution': '1K'}))
    return server


def test_a_gift_qq_did_not_take_becomes_one_letter(monkeypatch, tmp_path):
    seen = GIFT['at']
    cloud_events.record(tmp_path, [GIFT], seen)
    published = []
    async def prepare(intent, *, now):
        return {'intent': intent}
    async def publish(intent, plan, *, turn):
        published.append((intent, plan))
    server = letter_world(monkeypatch, tmp_path, seen + 60)
    monkeypatch.setattr(server, '_prepare_proactive_turn', prepare)
    monkeypatch.setattr(server, '_publish_proactive', publish)
    asyncio.run(server._gift_letter_tick())
    assert published == []                                    # QQ still has its head start
    monkeypatch.setattr(server.time, 'time', lambda: seen + 200)
    asyncio.run(server._gift_letter_tick())
    asyncio.run(server._gift_letter_tick())
    assert len(published) == 1
    intent, plan = published[0]
    assert (intent['kind'], intent['gift_id'], intent['photo'], intent['source_id']) == ('gift', GIFT['id'], True, 'reply:a:1')
    assert plan['decision'] == 'send'


def test_a_gift_letter_ignores_unread_and_budget_and_names_the_photo(monkeypatch, tmp_path):
    server = letter_world(monkeypatch, tmp_path, GIFT['at'])
    monkeypatch.setattr(server, '_proactive_settings', lambda: {'enabled': True, 'allow_voice': False})
    monkeypatch.setattr(server, '_refresh_proactive_context',
                        lambda **kwargs: {'blocked': False, 'unread': True, 'remaining': 0, 'candidates': []})
    intent = {'id': 'x' * 32, 'kind': 'gift', 'gift_id': GIFT['id'], 'brief': GIFT['brief'], 'photo': True,
              'source_id': 'reply:a:1'}
    assert server._proactive_opportunity_current(intent) is True
    assert server._proactive_opportunity_current({**intent, 'kind': 'life_share'}) is False
    task = server._proactive_instruction(intent, planning=False)
    assert GIFT['brief'] in task and '照片' in task
    monkeypatch.setattr(server, '_proactive_settings', lambda: {'enabled': False, 'allow_voice': False})
    assert server._proactive_opportunity_current(intent) is False   # letters switched off by the user
