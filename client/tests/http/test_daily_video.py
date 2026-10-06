"""Synthetic daily video chain: paid readiness is not a world or delivery fact."""
import asyncio
from datetime import datetime, timezone

import pytest

from runtime.personal_chat.daily_video import DailyVideoWorker, validate_input
from runtime.personal_chat.service import PersonalChatService
from runtime.private_world.daily_life import DailyLifeStore
from runtime.reply.media_delivery import make_daily_delivery

NOW = datetime(2026, 10, 6, 2, tzinfo=timezone.utc)


def payload(**changes):
    return dict(version=1, event_id='day:synthetic-housework', target_date='2026-10-06',
        event_at=NOW.isoformat(), event_kind='housework', scene_id='bedroom',
        certainty='live', spoken_text='我整理了桌边这一小块。', **changes)


STAGING = dict(image_direction='A close relaxed selfie with her head slightly tilted.',
               video_direction='She glances toward the tidied corner, then looks back at the lens.')


@pytest.mark.parametrize('staging', [STAGING, {**STAGING, 'steps': 20}])
def test_writer_authors_video_staging_in_same_reply_call(tmp_path, monkeypatch, staging):
    from runtime.personal_chat import backend
    from runtime.personal_chat.events import PersonalMessage
    from tests.http.test_chat_jev_decision import server_fixture, pipeline_result
    from tests.http.test_personal_chat_decision import envelope
    result = pipeline_result(delivery='video_speech', text=envelope(text='刚收拾好，给你看看。',
        daily_video=dict(event_id=payload()['event_id'], spoken_text=payload()['spoken_text'], staging=staging)))
    server, row, calls, _, audio = server_fixture(monkeypatch, tmp_path, result)
    row['daily_video_candidates'] = [{**payload(), 'detail': '实际整理过的小区域。'}]
    assert asyncio.run(backend.generate(server, PersonalMessage('qq', '100', '200', '1', '让我看看'), row)) == '刚收拾好，给你看看。'
    assert len(calls) == 1 and not audio
    assert row['daily_video_request'] == (payload(staging=STAGING) if staging == STAGING else payload())
    assert row['daily_video_status'] == 'PENDING_ACK'


def test_staging_is_forwarded_once_persisted_and_excluded_from_world_recall(tmp_path):
    import json
    from runtime.reply.media_delivery import delivery_evidence
    store, chat, api = world(tmp_path), service(), API()
    data = payload(staging=STAGING)
    publish(store, data)
    async def scenario():
        job = worker(tmp_path, store, chat, api)
        await job.enqueue(data)
        await job.wait_idle()
        await job.close()
        restored = worker(tmp_path, store, chat, api)
        await restored.enqueue(data)
        await restored.wait_idle()
        assert api.calls == [('daily_video', data)]
        assert restored.get(data['event_id'])['input'] == data
        with pytest.raises(ValueError, match='DAILY_VIDEO_EVENT_CONFLICT'):
            await restored.enqueue({**data, 'staging': {**STAGING, 'video_direction': 'Changed motion.'}})
        await restored.close()
    asyncio.run(scenario())
    assert store.reserve_daily_video_delivery(data, task_id='same-task', now=NOW)
    event = make_daily_delivery(data, task_id='same-task', message_id='301', occurred_at=NOW)
    assert store.record_media_delivery(event)
    reference = store.reply_context('你分享过整理桌边的视频吗？', now=NOW, max_chars=8000)
    for direction in STAGING.values():
        assert direction not in reference and direction not in json.dumps(delivery_evidence(event))


@pytest.mark.parametrize('staging', [None, {}, {**STAGING, 'steps': 20},
    {**STAGING, 'image_direction': 'x' * 401}, {**STAGING, 'video_direction': 'x' * 801},
    {**STAGING, 'video_direction': ' '}, {**STAGING, 'video_direction': 'move\x00now'}])
def test_invalid_staging_uses_existing_recipe_and_direct_requests_are_rejected(staging):
    from runtime.personal_chat.decision import decode
    from tests.http.test_personal_chat_decision import envelope
    raw = envelope(text='正常聊天照常回复。', daily_video=dict(event_id=payload()['event_id'],
        spoken_text='刚收拾好了。', staging=staging))
    value = decode(raw, user='让我看看', now=NOW.timestamp(), daily_video_candidates=[payload()])
    assert value['text'] == '正常聊天照常回复。'
    assert value['dropped_daily_video_staging'] is True
    assert value['daily_video_request'] == {**payload(), 'spoken_text': '刚收拾好了。'}
    with pytest.raises(ValueError, match='DAILY_VIDEO_INPUT_INVALID'):
        validate_input(payload(staging=staging))


def world(tmp_path):
    return DailyLifeStore(tmp_path / 'world.sqlite')


def publish(store, data):
    store.publish_day(data['event_id'], {'location': '住处', 'activity': '整理桌边', 'note': '整理好了一小块。'},
        [], occurred_at=datetime.fromisoformat(data['event_at']), activity_kind='housework')


def service():
    return PersonalChatService([], lambda: None, None, None, {'qq': ('100', '200')})


class API:
    url = 'https://synthetic.invalid'
    token = 'synthetic'
    def __init__(self, gate=None):
        self.calls = []
        self.gate = gate

    async def generate(self, kind, data, output, **kwargs):
        self.calls.append((kind, data))
        if self.gate:
            await self.gate.wait()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'validated-mp4')
        return {'task_id': 'same-task'}

    async def request(self, action, data):
        assert action == 'ack'
        return {}


def worker(tmp_path, store, chat, api):
    return DailyVideoWorker(tmp_path / 'jobs', chat, ('100', '200'),
        lambda: api, store, validator=lambda path: None, quiet_seconds=0, clock=lambda: NOW)


@pytest.mark.parametrize('change', [dict(version=True), dict(event_kind='wake'),
    dict(target_date='2026-10-05'), dict(event_at='2026-10-06T02:00:00'),
    dict(scene_id='../unsafe'), dict(spoken_text=''), dict(extra='untrusted'),
    dict(event_id='x' * 129), dict(scene_id='x' * 81), dict(spoken_text='x' * 501),
    dict(spoken_text=' ' * 501 + 'x')])
def test_frozen_input_validation(change):
    with pytest.raises(ValueError, match='DAILY_VIDEO_INPUT_INVALID'):
        validate_input({**payload(), **change})


def test_ready_requires_occurred_canonical_source_and_confirmed_qq(tmp_path):
    store = world(tmp_path)
    async def scenario():
        chat, api = service(), API()
        job = worker(tmp_path, store, chat, api)
        sent = []
        async def send(text):
            raise AssertionError('no text reply')
        async def video(path, **kwargs):
            assert chat.lock.locked()
            assert not store.history()['moments'][-1].get('delivery')
            sent.append(path)
            return 'qq-301'
        send.video = video
        send.is_available = lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        assert job.get(payload()['event_id'])['status'] == 'READY'
        assert not sent and not store.history()['moments']
        publish(store, payload())
        job.resume()
        await job.wait_idle()
        row = job.get(payload()['event_id'])
        assert row['delivery_status'] == 'DELIVERED' and row['world_status'] == 'COMMITTED'
        assert len(sent) == 1 and len(api.calls) == 1
        assert len(store.history()['moments']) == 2
        await job.enqueue(payload())
        await job.wait_idle()
        assert len(sent) == 1 and len(api.calls) == 1
        await job.close()
    asyncio.run(scenario())


def test_generation_does_not_hold_chat_lock_and_new_input_only_defers_send(tmp_path):
    store = world(tmp_path)
    publish(store, payload())
    async def scenario():
        chat, gate = service(), asyncio.Event()
        api = API(gate)
        job = worker(tmp_path, store, chat, api)
        receipts = []
        async def send(text):
            return 'unused'
        async def video(path, **kwargs):
            receipts.append('ack')
            return '301'
        send.video = video
        send.is_available = lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await asyncio.sleep(0)
        assert not chat.lock.locked()
        from runtime.personal_chat.events import PersonalMessage
        await asyncio.wait_for(chat.ingest(PersonalMessage('qq', '100', '200', 'new', '新消息')), .2)
        gate.set()
        await job.wait_idle()
        assert not receipts and job.get(payload()['event_id'])['status'] == 'READY'
        chat.rows[0]['delivery_status'] = 'DELIVERED'
        job.resume()
        await job.wait_idle()
        assert receipts == ['ack'] and len(api.calls) == 1
        await job.close()
    asyncio.run(scenario())


def test_ambiguous_video_send_survives_restart_without_resend(tmp_path):
    store = world(tmp_path)
    publish(store, payload())
    async def scenario():
        chat, api, sends = service(), API(), []
        async def send(text):
            return 'unused'
        async def video(path, **kwargs):
            sends.append(path)
            raise TimeoutError('synthetic ack timeout')
        send.video = video
        send.is_available = lambda: True
        job = worker(tmp_path, store, chat, api)
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        assert job.get(payload()['event_id'])['delivery_status'] == 'UNKNOWN'
        await job.close()
        restarted = worker(tmp_path, store, chat, api)
        restarted.bind('qq', send)
        restarted.resume()
        await restarted.wait_idle()
        assert len(sends) == 1 and len(api.calls) == 1
        assert len(store.history()['moments']) == 1
        await restarted.close()
    asyncio.run(scenario())


def test_delivery_does_not_change_authored_actual_and_recall_only_after_ack(tmp_path):
    store, data = world(tmp_path), payload()
    event = make_daily_delivery(data, task_id='same-task', message_id='qq-301', occurred_at=NOW)
    with pytest.raises(ValueError, match='DAILY_VIDEO_RESERVATION_REQUIRED'):
        store.record_media_delivery(event)
    publish(store, data)
    before = store.snapshot(NOW)['current']
    assert store.daily_video_event_matches(data, now=NOW)
    assert store.reserve_daily_video_delivery(data, task_id='same-task', now=NOW)
    assert store.record_media_delivery(event)
    assert not store.record_media_delivery(event)
    assert store.snapshot(NOW)['current'] == before
    reference = store.reply_context('你上次分享整理的视频了吗？', now=NOW, max_chars=8000)
    assert 'daily_life' in reference and 'day:synthetic-housework' in reference
    assert data['spoken_text'] not in reference


def test_event_input_and_owner_binding_are_immutable(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    async def scenario():
        job = worker(tmp_path, store, chat, api)
        await job.enqueue(payload())
        await job.wait_idle()
        with pytest.raises(ValueError, match='DAILY_VIDEO_EVENT_CONFLICT'):
            await job.enqueue({**payload(), 'spoken_text': 'changed'})
        await job.close()
        other_chat = PersonalChatService([], lambda: None, None, None, {'qq': ('100', '999')})
        other = DailyVideoWorker(tmp_path / 'jobs', other_chat, ('100', '999'), lambda: api, store,
                                validator=lambda path: None, quiet_seconds=0)
        with pytest.raises(ValueError, match='DAILY_VIDEO_BINDING_CONFLICT'):
            await other.enqueue(payload())
        await other.close()
    asyncio.run(scenario())


def test_restart_reuses_saved_paid_task_and_explicit_recovery_never_submits(tmp_path, monkeypatch):
    import json
    from runtime.cloud_service import CloudError
    from runtime.remote_generation import RemoteGeneration
    api = RemoteGeneration('https://synthetic.invalid', 'synthetic')
    calls = []
    available = False
    async def request(action, data):
        calls.append((action, data))
        if action == 'capabilities':
            return dict(kinds=['daily_video'], shared_assets=[], daily_video_enabled=True,
                        result_acknowledgement=True)
        assert action in {'submit', 'status'}
        return dict(task_id='paid-task', status='succeeded' if available else 'failed', outputs=[])
    async def download(task, output, **kwargs):
        if task['status'] != 'succeeded':
            raise CloudError('GPU_TASK_FAILED', 502)
        output.write_bytes(b'validated-mp4')
        return task
    monkeypatch.setattr(api, 'request', request)
    monkeypatch.setattr(api, '_download', download)
    store, chat = world(tmp_path), service()
    async def scenario():
        nonlocal available
        job = worker(tmp_path, store, chat, api)
        await job.enqueue(payload())
        await job.wait_idle()
        row = job.get(payload()['event_id'])
        assert row['status'] == 'FAILED' and row['task_id'] == 'paid-task'
        receipts = list((tmp_path / 'jobs').glob('*.task.json'))
        saved = json.loads(receipts[0].read_text())
        assert saved['task_id'] == 'paid-task'
        await job.close()
        available = True  # Original order was explicitly restored server-side.
        restarted = worker(tmp_path, store, chat, api)
        await restarted.recover(payload()['event_id'])
        await restarted.wait_idle()
        assert restarted.get(payload()['event_id'])['status'] == 'READY'
        assert [action for action, data in calls].count('submit') == 1
        assert ('status', {'task_id': 'paid-task'}) in calls
        assert json.loads(receipts[0].read_text())['submission'] == saved['submission']
        await restarted.close()
    asyncio.run(scenario())


def test_corrupt_paid_daily_receipt_fails_closed_without_new_order(tmp_path, monkeypatch):
    from runtime.remote_generation import RemoteGeneration
    from runtime.cloud_service import CloudError
    receipt = tmp_path / 'existing.task.json'
    receipt.write_bytes(b'{broken')
    api = RemoteGeneration('https://synthetic.invalid', 'synthetic')
    calls = []
    async def request(action, data):
        calls.append(action)
        assert action == 'capabilities'
        return dict(kinds=['daily_video'], shared_assets=[], daily_video_enabled=True)
    monkeypatch.setattr(api, 'request', request)
    with pytest.raises(CloudError, match='GPU_RECOVERY_REQUIRED'):
        asyncio.run(api.generate('daily_video', payload(), tmp_path / 'out.mp4', receipt_path=receipt))
    assert calls == ['capabilities'] and receipt.read_bytes() == b'{broken'


def test_video_validation_rejects_fake_container_or_missing_audio(tmp_path, monkeypatch):
    from runtime.personal_chat.daily_video import validate_video
    output = tmp_path / 'output.mp4'
    output.write_bytes(b'not an MP4 file at all')
    with pytest.raises(ValueError, match='DAILY_VIDEO_MP4_INVALID'):
        validate_video(output)
    output.write_bytes(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 20)
    calls = []
    def decode(path, **kwargs):
        calls.append(kwargs)
        return None
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds', decode)
    with pytest.raises(ValueError, match='DAILY_VIDEO_MP4_INVALID'):
        validate_video(output)
    assert calls == [dict(required_streams=('0:v:0', '0:a:0'))]


def test_local_route_uses_native_origin_confirmation_and_runtime_binding(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from runtime.personal_chat.daily_video import install_routes, PATH
    from runtime.personal_chat.backend import _RUNTIME
    from runtime.personal_chat.setup import CONFIRM_HEADER, CONFIRM_VALUE
    store, chat, api = world(tmp_path), service(), API()
    monkeypatch.setattr('runtime.personal_chat.backend.selected_channels', lambda server: {'qq'})
    async def scenario():
        job = worker(tmp_path, store, chat, api)
        server = type('Server', (), {'origin_allowed': lambda self, origin: origin == 'https://trusted.example'})()
        app = web.Application()
        app[_RUNTIME] = {'daily_video': job}
        install_routes(app, server, _RUNTIME)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(PATH, json=payload())
            assert response.status == 403
            headers = {CONFIRM_HEADER: CONFIRM_VALUE, 'Origin': 'https://evil.example'}
            assert (await client.post(PATH, json=payload(), headers=headers)).status == 403
            headers['Origin'] = 'https://trusted.example'
            response = await client.post(PATH, json={**payload(), 'binding_id': 'another-owner'}, headers=headers)
            assert response.status == 400
            response = await client.post(PATH, json=payload(), headers=headers)
            assert response.status == 202
            assert set(await response.json()) <= {'event_id', 'status', 'delivery_status', 'world_status', 'task_id', 'error_code'}
            await job.wait_idle()
            response = await client.get(PATH, params={'event_id': payload()['event_id']}, headers=headers)
            assert response.status == 200 and (await response.json())['status'] == 'READY'
            assert job.get(payload()['event_id'])['binding'] == ['100', '200']
            job.binding = ['100', '999']
            response = await client.get(PATH, params={'event_id': payload()['event_id']}, headers=headers)
            assert response.status == 409
        await job.close()
    asyncio.run(scenario())


def test_paid_route_requires_existing_native_setup_session_when_mounted(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from runtime.personal_chat.daily_video import install_routes, PATH
    from runtime.personal_chat.backend import _RUNTIME
    from runtime.personal_chat.setup import CONFIRM_HEADER, CONFIRM_VALUE
    from original_client_setup_api import _SERVICE_KEY, SESSION_HEADER, LLMSetupError
    class Setup:
        def require_session(self, supplied):
            if supplied != 'synthetic-session':
                raise LLMSetupError('LLM_SETUP_LOGIN_REQUIRED', status=403)
    store, chat, api = world(tmp_path), service(), API()
    monkeypatch.setattr('runtime.personal_chat.backend.selected_channels', lambda server: {'qq'})
    async def scenario():
        job = worker(tmp_path, store, chat, api)
        app = web.Application()
        app[_RUNTIME], app[_SERVICE_KEY] = {'daily_video': job}, Setup()
        server = type('Server', (), {'origin_allowed': lambda self, origin: origin == 'https://trusted.example'})()
        install_routes(app, server, _RUNTIME)
        async with TestClient(TestServer(app)) as client:
            headers = {CONFIRM_HEADER: CONFIRM_VALUE, 'Origin': 'https://trusted.example'}
            denied = await client.post(PATH, json=payload(), headers=headers)
            assert denied.status == 403 and not api.calls and job.get(payload()['event_id']) is None
            denied = await client.post(PATH, json=payload(), headers={**headers, SESSION_HEADER: 'wrong'})
            assert denied.status == 403 and not api.calls
            preflight = await client.options(PATH, headers={'Origin': 'https://trusted.example'})
            assert preflight.status == 204 and SESSION_HEADER in preflight.headers['Access-Control-Allow-Headers']
            allowed = await client.post(PATH, json=payload(), headers={**headers, SESSION_HEADER: 'synthetic-session'})
            assert allowed.status == 202
            await job.wait_idle()
            assert len(api.calls) == 1
        await job.close()
    asyncio.run(scenario())


def test_qq_video_is_validated_correlated_and_uses_media_ack_budget(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from runtime.personal_chat.qq import run_qq
    from tests.http.test_personal_chat_qq import login, event, TOKEN
    validated, outgoing = [], []
    output = tmp_path / 'output.mp4'
    output.write_bytes(b'synthetic fixture')
    monkeypatch.setattr('runtime.personal_chat.daily_video.validate_video', lambda path: validated.append(path))
    async def scenario():
        stop = asyncio.Event()
        async def handler(message, send):
            assert await send.for_exchange(message).video(output) == '301'
            stop.set()
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await login(ws)
            await ws.send_json(event(1))
            item = await ws.receive_json()
            outgoing.append(item)
            await asyncio.sleep(.08)
            await ws.send_json(dict(echo=item['echo'], status='ok', retcode=0, data=dict(message_id=301)))
            await stop.wait()
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get('/', socket)
        async with TestServer(app) as server:
            await asyncio.wait_for(run_qq(str(server.make_url('/')), TOKEN, '100', '200', handler, stop,
                merge_seconds=0, ack_timeout=.05, media_ack_timeout=.5), 2)
    asyncio.run(scenario())
    assert validated == [output.resolve()]
    assert outgoing[0]['params'] == dict(user_id=200, message=[
        {'type': 'reply', 'data': {'id': '1'}}, {'type': 'video', 'data': {'file': output.resolve().as_uri()}}])


def test_daily_video_submit_public_contract_and_charge_budget(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from runtime.remote_generation import RemoteGeneration
    received = []
    async def scenario():
        async def submit(request):
            received.append((await request.json(), dict(request.headers)))
            return web.json_response(dict(task_id='server-task', status='queued', outputs=[]))
        app = web.Application()
        app.router.add_post('/v1/tasks', submit)
        async with TestServer(app) as server:
            api = RemoteGeneration(str(server.make_url('')).rstrip('/'), 'synthetic')
            result = await api.request('submit', dict(request_id='frozen-key-123', kind='daily_video', input=payload()))
            assert result['task_id'] == 'server-task'
    asyncio.run(scenario())
    assert received[0][0] == dict(request_id='frozen-key-123', kind='daily_video', input=payload())
    assert received[0][1]['X-Olivia-Max-Charge-Cents'] == '500'
    assert received[0][1]['Idempotency-Key'] == 'frozen-key-123'


@pytest.mark.parametrize('difference', ['wrong_kind', 'future', 'exchange_only', 'wrong_time', 'schedule_only'])
def test_canonical_source_must_have_occurred_with_exact_binding(tmp_path, difference):
    store, data = world(tmp_path), payload()
    if difference == 'exchange_only':
        data['event_id'] = 'reply:synthetic-exchange'
        store.record_exchange(data['event_id'], 'synthetic user', 'synthetic reply', [], occurred_at=NOW)
    elif difference != 'schedule_only':
        publish(store, data)
    request = {**data}
    now = NOW
    if difference == 'wrong_kind':
        request['event_kind'] = 'walk'
    elif difference == 'future':
        from datetime import timedelta
        now -= timedelta(seconds=1)
    elif difference == 'wrong_time':
        from datetime import timedelta
        request['event_at'] = (NOW.replace(minute=1)).isoformat()
    elif difference == 'schedule_only':
        request['event_kind'] = 'wake_up'
        store.snapshot(NOW)  # Scheduled wake/bath projections do not author a source.
    assert not store.daily_video_event_matches(request, now=now)


def test_new_input_during_video_validation_defers_without_ambiguous_reservation(tmp_path):
    from runtime.personal_chat.qq import QQVideoDeferred
    from runtime.personal_chat.events import PersonalMessage
    store = world(tmp_path)
    publish(store, payload())
    async def scenario():
        chat, api = service(), API()
        job = worker(tmp_path, store, chat, api)
        calls = []
        async def send(text):
            return 'unused'
        async def video(path, *, eligible):
            await chat.ingest(PersonalMessage('qq', '100', '200', 'during-validation', 'new owner message'))
            assert not eligible()
            calls.append('deferred-before-transport')
            raise QQVideoDeferred('QQ_VIDEO_DEFERRED')
        send.video = video
        send.is_available = lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        row = job.get(payload()['event_id'])
        assert row['status'] == 'READY' and row['delivery_status'] == 'PENDING'
        assert calls == ['deferred-before-transport']
        assert len(store.history()['moments']) == 1
        await job.close()
    asyncio.run(scenario())


def test_shopping_requires_completed_actual_episode_not_errand_marker(tmp_path):
    import json
    store, data = world(tmp_path), {**payload(), 'event_kind': 'shopping'}
    store.publish_day(data['event_id'], {'location': '街区', 'activity': '外出', 'note': '办自己的事情。'},
                      [], occurred_at=NOW, activity_kind='errand')
    assert not store.daily_video_event_matches(data, now=NOW)
    # A marker alone cannot promote preparation into a completed purchase.
    with store._db() as db:
        raw = db.execute('SELECT payload FROM life_moments WHERE source_id=?', (data['event_id'],)).fetchone()[0]
        current = json.loads(raw)
        current['event_kind'] = 'shopping'
        db.execute('UPDATE life_moments SET payload=? WHERE source_id=?', (json.dumps(current), data['event_id']))
    assert not store.daily_video_event_matches(data, now=NOW)
    from runtime.private_world.life_episode import create
    class Port:
        def __init__(self, path='0'):
            self.path = path
        async def ask(self, state, questions, **kwargs):
            return dict(trigger='own_activity', experience=self.path + ':ordinary:none')
    source = 'day:actual-shopping'
    episode = asyncio.run(create(Port(), source, NOW, 'shopping', {}))
    current = dict(location='店里', activity='买日常用品', note=episode['result']['detail'])
    store.publish_day(source, current, [], occurred_at=NOW, activity_kind='shopping', episode=episode)
    actual = {**data, 'event_id': source}
    assert store.daily_video_event_matches(actual, now=NOW)
    assert any(item['event_id'] == source for item in store.daily_video_sources(now=NOW))
    episode = asyncio.run(create(Port('2'), 'day:unfinished-shopping', NOW, 'shopping', {}))
    store.publish_day('day:unfinished-shopping', current, [], occurred_at=NOW, activity_kind='shopping', episode=episode)
    assert not store.daily_video_event_matches({**data, 'event_id': 'day:unfinished-shopping'}, now=NOW)
    assert not any(item['event_id'] == 'day:unfinished-shopping' for item in store.daily_video_sources(now=NOW))


@pytest.mark.parametrize('status', ['eating', 'planned', 'skipped'])
def test_native_meal_lifecycle_only_offers_actual_present_meal_source(tmp_path, status):
    from tests.private_world.test_meal_lifecycle import Port, at
    from runtime.private_world.meal_lifecycle import advance
    store = world(tmp_path)
    port = Port(choose=lambda state, options: next(key for key, item in options.items() if item['status'] == status))
    now = at(8, 5)
    asyncio.run(advance(store, port, now))
    calls = len(port.raw_calls)
    sources = store.daily_video_sources(now=now)
    assert len(port.raw_calls) == calls  # Candidate projection never calls another model.
    if status != 'eating':
        assert not sources
        return
    assert len(sources) == 1 and sources[0]['event_kind'] == 'meal'
    source = sources[0]
    request = {**payload(), 'event_id': source['event_id'], 'event_at': source['event_at'],
               'event_kind': 'meal', 'target_date': '2026-09-28'}
    assert store.daily_video_event_matches(request, now=now)


def test_native_lifecycle_cancels_owned_generation_and_restores_original_order(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from runtime.personal_chat import backend, qq
    from runtime.personal_chat.daily_video import PATH
    from runtime.personal_chat.setup import CONFIRM_HEADER, CONFIRM_VALUE
    from runtime.remote_generation import RemoteGeneration
    import local_server
    store = world(tmp_path)
    publish(store, payload())
    config = tmp_path / 'synthetic-channels.json'
    config.write_text(json.dumps({'qq': {'url': 'ws://127.0.0.1:3001', 'account': '100', 'owner': '200'}}))
    monkeypatch.setenv('OLIVIA_PERSONAL_CHAT_CONFIG', str(config))
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    api = RemoteGeneration('https://synthetic.invalid', 'synthetic')
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration', lambda *args: api)
    operations, submitted, second_run = [], asyncio.Event(), False
    async def request(action, data):
        operations.append((action, data))
        if action == 'capabilities':
            return dict(kinds=['daily_video'], shared_assets=[], daily_video_enabled=True)
        if action == 'submit':
            submitted.set()
            return dict(task_id='same-owned-task', status='queued', outputs=[])
        assert action == 'ack'
        return {}
    async def status(task_id):
        assert task_id == 'same-owned-task' and second_run
        operations.append(('status', {'task_id': task_id}))
        return dict(task_id=task_id, status='succeeded', outputs=[])
    async def download(task, output, **kwargs):
        output.write_bytes(b'validated-mp4')
        return task
    monkeypatch.setattr(api, 'request', request)
    monkeypatch.setattr(api, '_status', status)
    monkeypatch.setattr(api, '_download', download)
    async def scenario():
        nonlocal second_run
        receipts = []
        connected = asyncio.Event()
        async def send(text):
            raise AssertionError('manual media job must not generate text')
        async def video(path, *, eligible):
            assert eligible()
            receipts.append('301')
            return '301'
        send.video = video
        send.is_available = lambda: True
        async def listen(url, token, account, owner, handle, stop, **kwargs):
            handle.ready('qq', send)
            connected.set()
            await stop.wait()
        monkeypatch.setattr(qq, 'run_qq', listen)
        server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
            _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
            _safe_log=lambda *args, **kwargs: None, daily_life_runtime=SimpleNamespace(store=store))
        def application():
            app = web.Application()
            backend.install_personal_chat(app, server)
            return app
        app = application()
        async with TestClient(TestServer(app)) as client:
            job = app[backend._RUNTIME]['daily_video']
            job.validator, job.clock, job.quiet_seconds = lambda path: None, lambda: NOW, 0
            response = await client.post(PATH, json=payload(), headers={CONFIRM_HEADER: CONFIRM_VALUE})
            assert response.status == 202
            await asyncio.wait_for(submitted.wait(), 1)
            await asyncio.sleep(0)
            assert job.get(payload()['event_id'])['task_id'] == 'same-owned-task'
        assert job.closed and not job.tasks and not receipts
        second_run = True
        connected.clear()
        restarted_app = application()
        async with TestClient(TestServer(restarted_app)) as client:
            restored = restarted_app[backend._RUNTIME]['daily_video']
            restored.validator, restored.clock, restored.quiet_seconds = lambda path: None, lambda: NOW, 0
            await asyncio.wait_for(connected.wait(), 1)
            restored.resume()
            await restored.wait_idle()
            assert restored.get(payload()['event_id'])['delivery_status'] == 'DELIVERED', restored.get(payload()['event_id'])
        assert restored.closed and not restored.tasks
        assert receipts == ['301']
        assert sum(action == 'submit' for action, data in operations) == 1
        assert ('status', {'task_id': 'same-owned-task'}) in operations
        assert len(store.history()['moments']) == 2
    asyncio.run(scenario())


def test_first_queued_order_does_not_block_second_durable_cloud_submission(tmp_path):
    import json
    store, chat = world(tmp_path), service()
    async def scenario():
        hold, first_started, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        submissions = []
        first = payload()
        second = {**payload(), 'event_id': 'day:synthetic-second'}
        class Queued:
            async def generate(self, kind, data, output, *, receipt_path, **kwargs):
                task_id = 'task-first' if data['event_id'] == first['event_id'] else 'task-second'
                submissions.append(data['event_id'])
                receipt_path.write_text(json.dumps({'task_id': task_id}))
                self.progress('generation', {'task_id': task_id, 'status': 'queued'})
                (first_started if task_id == 'task-first' else second_started).set()
                await hold.wait()  # A planned cloud order can remain queued until due.
        job = DailyVideoWorker(tmp_path / 'jobs', chat, ('100', '200'), Queued, store,
                               validator=lambda path: None, clock=lambda: NOW)
        await job.enqueue(first)
        await asyncio.wait_for(first_started.wait(), .5)
        await job.enqueue(second)
        await asyncio.wait_for(second_started.wait(), .5)
        assert job.get(first['event_id'])['task_id'] == 'task-first'
        assert job.get(second['event_id'])['task_id'] == 'task-second'
        assert job.get(first['event_id'])['status'] == 'GENERATING'
        await job.enqueue(first)
        await job.enqueue(second)
        await asyncio.sleep(0)
        assert submissions == [first['event_id'], second['event_id']]
        await job.close()
        assert not job.tasks
        assert sorted(json.loads(path.read_text())['task_id']
                      for path in (tmp_path / 'jobs').glob('*.task.json')) == ['task-first', 'task-second']
    asyncio.run(scenario())


def test_confirmed_delivery_world_retry_runs_offline_without_resend(tmp_path):
    store = world(tmp_path)
    publish(store, payload())
    class DelayedWorld:
        failures = 0
        def daily_video_event_matches(self, *args, **kwargs):
            return store.daily_video_event_matches(*args, **kwargs)
        def reserve_daily_video_delivery(self, *args, **kwargs):
            return store.reserve_daily_video_delivery(*args, **kwargs)
        def record_media_delivery(self, event):
            if not self.failures:
                self.failures += 1
                raise OSError('synthetic world unavailable after ACK')
            return store.record_media_delivery(event)
    async def scenario():
        chat, api, sent = service(), API(), []
        delayed = DelayedWorld()
        job = worker(tmp_path, delayed, chat, api)
        async def send(text):
            return 'unused'
        async def video(path, **kwargs):
            sent.append('301')
            return '301'
        send.video = video
        send.is_available = lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        assert job.get(payload()['event_id'])['delivery_status'] == 'DELIVERED'
        assert job.get(payload()['event_id'])['world_status'] == 'PENDING'
        await job.close()
        restarted = worker(tmp_path, delayed, chat, api)
        assert restarted.send is None  # No QQ connection exists during replay.
        restarted.resume()
        await restarted.wait_idle()
        assert restarted.get(payload()['event_id'])['world_status'] == 'COMMITTED'
        assert sent == ['301'] and len(api.calls) == 1
        assert len(store.history()['moments']) == 2
        await restarted.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('duration,accepted', [(4.99, False), (5, True), (15.08, True),
                                              (15.1, True), (15.1001, False), (900, False)])
def test_daily_video_duration_native_pad_boundary(tmp_path, monkeypatch, duration, accepted):
    from runtime.personal_chat.daily_video import validate_video
    output = tmp_path / 'output.mp4'
    output.write_bytes(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 20)
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds', lambda *args, **kwargs: duration)
    if accepted:
        validate_video(output)
    else:
        with pytest.raises(ValueError, match='DAILY_VIDEO_MP4_INVALID'):
            validate_video(output)


@pytest.mark.parametrize('source_change', ['delete', 'change'])
def test_ack_source_retention_survives_source_change_and_consumer_retry(tmp_path, source_change):
    import json
    store = world(tmp_path)
    publish(store, payload())
    class DelayedWorld:
        attempts = 0
        def daily_video_event_matches(self, *args, **kwargs):
            return store.daily_video_event_matches(*args, **kwargs)
        def reserve_daily_video_delivery(self, *args, **kwargs):
            return store.reserve_daily_video_delivery(*args, **kwargs)
        def record_media_delivery(self, event):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError('synthetic consumer failure after confirmed send')
            return store.record_media_delivery(event)
    async def scenario():
        chat, api, sends = service(), API(), []
        delayed = DelayedWorld()
        job = worker(tmp_path, delayed, chat, api)
        async def send(text):
            return 'unused'
        async def video(path, **kwargs):
            sends.append('301')
            with store._db() as db:
                if source_change == 'delete':
                    db.execute('DELETE FROM life_moments WHERE source_id=?', (payload()['event_id'],))
                else:
                    raw = db.execute('SELECT payload FROM life_moments WHERE source_id=?', (payload()['event_id'],)).fetchone()[0]
                    changed = json.loads(raw)
                    changed['activity_kind'] = 'walk'
                    db.execute('UPDATE life_moments SET payload=? WHERE source_id=?', (json.dumps(changed), payload()['event_id']))
            return '301'
        send.video, send.is_available = video, lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        assert job.get(payload()['event_id'])['delivery_status'] == 'DELIVERED'
        assert job.get(payload()['event_id'])['world_status'] == 'PENDING'
        await job.close()
        assert not store.daily_video_event_matches(payload(), now=NOW)
        after_source_change = store.snapshot(NOW)['current']
        restored = worker(tmp_path, delayed, chat, api)
        restored.resume()
        await restored.wait_idle()
        assert restored.get(payload()['event_id'])['world_status'] == 'COMMITTED'
        assert sends == ['301'] and len(api.calls) == 1
        assert store.snapshot(NOW)['current'] == after_source_change
        assert 'daily_life' in store.reply_context('分享视频', now=NOW, max_chars=8000)
        await restored.close()
    asyncio.run(scenario())


def test_daily_commit_without_reservation_is_rejected(tmp_path):
    store = world(tmp_path)
    publish(store, payload())
    event = make_daily_delivery(payload(), task_id='same-task', message_id='301', occurred_at=NOW)
    with pytest.raises(ValueError, match='DAILY_VIDEO_RESERVATION_REQUIRED'):
        store.record_media_delivery(event)


def test_source_reservation_is_invisible_and_cannot_authorize_changed_input_or_missing_ack(tmp_path):
    store, data = world(tmp_path), payload()
    publish(store, data)
    before_history, before_current = store.history(), store.snapshot(NOW)['current']
    assert store.reserve_daily_video_delivery(data, task_id='same-task', now=NOW)
    assert not store.reserve_daily_video_delivery(data, task_id='same-task', now=NOW)
    assert store.history() == before_history and store.snapshot(NOW)['current'] == before_current
    assert 'daily_life' not in store.reply_context('分享视频', now=NOW, max_chars=8000)
    altered = {**data, 'spoken_text': '不同的冻结输入。'}
    changed = make_daily_delivery(altered, task_id='same-task', message_id='301', occurred_at=NOW)
    with pytest.raises(ValueError, match='DAILY_VIDEO_EVENT_CONFLICT'):
        store.record_media_delivery(changed)
    with pytest.raises(ValueError, match='DAILY_VIDEO_EVENT_CONFLICT'):
        store.reserve_daily_video_delivery(altered, task_id='same-task', now=NOW)
    with pytest.raises(ValueError, match='MEDIA_DELIVERY_INVALID'):
        make_daily_delivery(data, task_id='same-task', message_id='', occurred_at=NOW)
    from datetime import timedelta
    with pytest.raises(ValueError, match='DAILY_VIDEO_SOURCE_UNAVAILABLE'):
        store.reserve_daily_video_delivery(data, task_id='same-task', now=NOW - timedelta(seconds=1))
    assert store.history() == before_history


def test_crash_after_sending_reservation_becomes_unknown_and_recovery_is_blocked(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    async def scenario():
        job = worker(tmp_path, store, chat, api)
        await job.enqueue(payload())
        await job.wait_idle()
        row = job.get(payload()['event_id'])
        row['delivery_status'] = 'SENDING'
        job._save(row)  # Durable snapshot a crash would leave behind.
        await job.close()
        restarted = worker(tmp_path, store, chat, api)
        assert restarted.get(payload()['event_id'])['delivery_status'] == 'UNKNOWN'
        with pytest.raises(ValueError, match='DAILY_VIDEO_DELIVERY_UNKNOWN'):
            await restarted.recover(payload()['event_id'])
        assert len(api.calls) == 1
        await restarted.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('now,expected', [
    (NOW, ''),
    (datetime(2026, 10, 6, 2, 20, tzinfo=timezone.utc), '这是今天 10:00拍的。'),
    (datetime(2026, 10, 7, 0, tzinfo=timezone.utc), '这是2026-10-06 10:00拍的。')])
def test_delayed_video_caption_keeps_original_capture_time(now, expected):
    from runtime.personal_chat.daily_video import capture_caption
    assert capture_caption(payload(), now) == expected


def test_delayed_ready_video_sends_capture_caption_with_same_ack(tmp_path):
    from datetime import timedelta
    store = world(tmp_path)
    publish(store, payload())
    async def scenario():
        chat, api, sent = service(), API(), []
        job = DailyVideoWorker(tmp_path / 'jobs', chat, ('100', '200'), lambda: api, store,
            validator=lambda path: None, quiet_seconds=0, clock=lambda: NOW + timedelta(minutes=20))
        async def send(text):
            raise AssertionError('capture caption must share the video ACK')
        async def video(path, *, caption, eligible):
            assert eligible()
            sent.append(caption)
            return '301'
        send.video, send.is_available = video, lambda: True
        job.bind('qq', send)
        await job.enqueue(payload())
        await job.wait_idle()
        assert sent == ['这是今天 10:00拍的。']
        assert job.get(payload()['event_id'])['delivery_status'] == 'DELIVERED'
        assert len(store.history()['moments']) == 2
        await job.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('selection', [dict(event_id='missing', spoken_text='短视频正文。'),
    dict(event_id=payload()['event_id'], spoken_text='x' * 101),
    dict(event_id=payload()['event_id'], spoken_text='正文', scene_id='invented')])
def test_invalid_daily_video_optional_metadata_preserves_text_reply(selection):
    import json
    from runtime.personal_chat.decision import decode
    from tests.http.test_personal_chat_decision import envelope
    raw = json.loads(envelope(text='这条正常聊天照常回复。'))
    raw['daily_video'] = selection
    candidate = {**payload(), 'detail': '实际整理过的小区域。'}
    value = decode(json.dumps(raw), user='新消息', now=NOW.timestamp(), daily_video_candidates=[candidate])
    assert value['text'] == '这条正常聊天照常回复。'
    assert value['dropped_daily_video'] is True and 'daily_video_request' not in value


def test_server_scene_alias_is_required_and_caps_failure_holds_new_spending(tmp_path):
    store = world(tmp_path)
    publish(store, payload())
    async def scenario():
        chat, api = service(), API()
        job = worker(tmp_path, store, chat, api)
        scene = dict(scene_id='bedroom', event_kinds=['housework'], locations=[])
        async def request(action, data):
            assert action == 'capabilities'
            return dict(kinds=['daily_video'], daily_video_enabled=True, daily_video_scenes=[scene])
        api.request = request
        await job.refresh_capabilities()
        assert job.candidates() == []  # 住处 does not silently mean bedroom.
        scene['locations'] = ['住处']
        await job.refresh_capabilities()
        candidates = job.candidates()
        assert len(candidates) == 1 and candidates[0]['event_at'] == payload()['event_at']
        from runtime.personal_chat.daily_video import select_candidate
        data = select_candidate(dict(event_id=payload()['event_id'], spoken_text='刚整理好了这一块。'), candidates)
        assert data['target_date'] == '2026-10-06' and data['scene_id'] == 'bedroom'
        from runtime.personal_chat.events import PersonalMessage
        chat.rows.append(dict(channel='qq', binding_id=PersonalMessage('qq', '100', '200', '', '').binding_id,
            delivery_status='DELIVERED', daily_video_request=data, daily_video_candidates=candidates))
        scene['locations'] = []
        await job.refresh_capabilities()
        await job.intake_replies()
        assert chat.rows[-1]['daily_video_error_code'] == 'DAILY_VIDEO_SCENE_UNAVAILABLE'
        assert not api.calls and job.get(data['event_id']) is None
        chat.rows[-1].pop('daily_video_status')
        async def unavailable(action, data):
            raise TimeoutError('synthetic capabilities unavailable')
        api.request = unavailable
        await job.refresh_capabilities()
        await job.intake_replies()
        assert not api.calls and job.get(data['event_id']) is None
        assert len(store.history()['moments']) == 1
        await job.close()
    asyncio.run(scenario())


def test_night_planned_job_is_durable_without_authoring_tomorrows_event(tmp_path):
    from datetime import timedelta
    store, chat, api = world(tmp_path), service(), API()
    later = NOW + timedelta(days=1)
    data = {**payload(), 'target_date': '2026-10-07', 'event_at': later.isoformat(), 'certainty': 'planned'}
    async def scenario():
        job = worker(tmp_path, store, chat, api)
        await job.enqueue(data)
        await job.wait_idle()
        assert job.get(data['event_id'])['status'] == 'READY'
        assert not store.history()['moments'] and len(api.calls) == 1
        await job.close()
        restored = worker(tmp_path, store, chat, api)
        await restored.enqueue(data)
        await restored.wait_idle()
        assert restored.get(data['event_id'])['input'] == data
        assert not store.history()['moments'] and len(api.calls) == 1
        await restored.close()
    asyncio.run(scenario())


def test_bath_schedule_and_rest_cannot_publish_completed_bath(tmp_path):
    store = world(tmp_path)
    data = {**payload(), 'event_kind': 'bath_finished', 'scene_id': 'bathroom'}
    store.snapshot(NOW)  # The routine's bath phase is only a schedule.
    assert not store.daily_video_event_matches(data, now=NOW)
    with pytest.raises(ValueError, match='DAILY_LIFE_BATH_EPISODE_REQUIRED'):
        store.publish_day(data['event_id'], dict(location='住处', activity='刚洗完澡', note='还只是计划。'), [],
            occurred_at=NOW, activity_kind='bath_finished')
    assert not store.history()['moments']


@pytest.mark.parametrize('kind', ['bath_finished', 'shopping'])
def test_new_completed_activity_does_not_stay_current_for_hours(tmp_path, kind):
    from datetime import timedelta
    from runtime.private_world.life_episode import create
    now, source = NOW.replace(hour=8, minute=10), 'day:short-completed'
    class Port:
        async def ask(self, state, questions, **kwargs):
            return dict(trigger='own_activity', experience='0:ordinary:none')
    store = world(tmp_path)
    episode = asyncio.run(create(Port(), source, now, kind, {}))
    store.publish_day(source, dict(location='住处' if kind == 'bath_finished' else '店里',
        activity='刚洗完澡' if kind == 'bath_finished' else '买日常用品', note=episode['result']['detail']), [],
        occurred_at=now, activity_kind=kind, episode=episode)
    store.record_exchange('reply:active', '我在呢', '嗯嗯', [], occurred_at=now)
    assert not store.snapshot(now + timedelta(minutes=59))['stale']
    assert store.snapshot(now + timedelta(minutes=61))['stale']
