"""Synthetic planner orders: fallback has one new receipt, even after interruption."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from runtime import image_reply
from runtime.personal_chat import backend
from runtime.remote_generation import RemoteGeneration
from tests.http.test_share_moment_video import candidate


@pytest.mark.parametrize('failure,expected', [
    ('DAILY_VIDEO_SOURCE_UNAVAILABLE', 'source_unavailable'),
    ('DAILY_VIDEO_SCENE_UNAVAILABLE', 'scene_unavailable'),
    ('DAILY_VIDEO_CONSENT_REQUIRED', 'consent_required'),
    ('DAILY_VIDEO_CAPABILITY_UNAVAILABLE', 'capability_unavailable'),
    ('DAILY_VIDEO_INPUT_INVALID', 'other'),
    ('cloud_code', 'source_unavailable'),
    ('absent', 'capability_unavailable'),
])
@pytest.mark.parametrize('interrupted', [False, True])
def test_failed_video_sends_one_paid_photo_with_new_request(tmp_path, monkeypatch, failure, expected, interrupted):
    submissions, sent, shared = [], [], []
    responses, orders = {}, {}
    async def request(self, action, data):
        if action == 'capabilities':
            return dict(server_media_planning=True, kinds=['image'], shared_assets=[])
        if action == 'status':
            return responses[data['task_id']]
        assert action == 'submit'
        submissions.append(data)
        if data['request_id'] in orders:
            return responses[orders[data['request_id']]]
        task_id = f'task-{len(orders)+1}'
        orders[data['request_id']] = task_id
        if len(orders) == 1:
            result = dict(task_id=task_id, status='cancelled', stage='skipped', share_video=candidate()['event_id'])
        else:
            result = dict(task_id=task_id, status='succeeded', media_plan=dict(photo_type='selfie', room='bedroom', time_of_day='night'))
        responses[task_id] = result
        if interrupted and len(orders) == 2:
            raise asyncio.CancelledError()  # Accepted order, response lost before saving task_id.
        return result
    async def download(self, task, output, *, validate=None, **kwargs):
        Image.new('RGB', (768, 1024)).save(output)
        validate(output)
        return task
    class Worker:
        def get(self, event_id): return None
        async def share(self, value, author):
            shared.append(value['event_id'])
            if failure == 'cloud_code':
                from runtime.cloud_service import CloudError
                raise CloudError('DAILY_VIDEO_SOURCE_UNAVAILABLE', 409)
            raise ValueError(failure)
    async def persist(*args): pass
    async def describe(*args, **kwargs): return 'A synthetic photo.'
    async def send_image(path):
        sent.append(path)
        return 'photo-ack'
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://synthetic.invalid')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic')
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    monkeypatch.setattr(RemoteGeneration, '_download', download)
    monkeypatch.setattr(image_reply, '_photo_reference', lambda *args: {'world_current_location': 'home'})
    monkeypatch.setattr(backend, 'persist_chat', persist)
    monkeypatch.setattr('runtime.image_understanding.describe_image', describe)
    monkeypatch.setattr('runtime.image_understanding.commit_image_memory', persist)
    server = SimpleNamespace(video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: dict(enabled=True, resolution='1K')),
        _media_root=lambda: tmp_path, _persist_store_state=lambda: None, PORT=1234,
        _daily_video_worker=None if failure == 'absent' else Worker())
    row = dict(letter_id='synthetic-turn', channel='qq', delivery_status='DELIVERED',
               content='A moment?', reply_text='Here it is.', daily_video_candidates=[candidate()])
    send = SimpleNamespace(image=send_image)
    async def scenario():
        if interrupted:
            with pytest.raises(asyncio.CancelledError):
                await backend.deliver_photo(server, row, send)
            restored = json.loads(json.dumps(row))
            row.clear()
            row.update(restored)
        await backend.deliver_photo(server, row, send)
        # A persisted/reloaded turn must neither order nor send the fallback twice.
        restored = json.loads(json.dumps(row))
        await backend.deliver_photo(server, restored, send)
    asyncio.run(scenario())
    assert row['share_video_result'] == expected
    assert row['image_delivery_status'] == 'DELIVERED'
    assert len(sent) == 1 and len(orders) == 2
    assert len(submissions) == (3 if interrupted else 2)
    assert sum(r['status'] == 'succeeded' for r in responses.values()) == 1
    assert len({s['request_id'] for s in submissions}) == 2
    assert all(s['kind'] == 'image' for s in submissions)
    original, fallback = [s['input']['media_request']['reference'] for s in submissions[:2]]
    assert original['video_moments'] and 'video_moments' not in fallback
    assert fallback['requested_image'] is True
    assert len(list(tmp_path.glob('*.task.json'))) == 2


def test_fallback_transition_keeps_frozen_request_and_is_idempotent():
    row = dict(image_status='SKIPPED', image_receipt_required=True, image_generation_binding='old',
        image_server_request=dict(incoming='frozen', reply='reply', reference=dict(video_moments=[candidate()], other='kept')))
    image_reply.start_video_photo_fallback(row)
    row.update(image_receipt_required=True, image_generation_binding='new')
    image_reply.start_video_photo_fallback(row)
    assert row['image_generation_binding'] == 'new' and row['image_receipt_required']
    assert row['image_server_request'] == dict(incoming='frozen', reply='reply', reference=dict(other='kept', requested_image=True))


def test_queued_video_does_not_fallback(monkeypatch):
    async def prepare(*args): pass
    async def persist(*args): pass
    class Worker:
        async def share(self, value, author): return {'status': 'PENDING'}
    monkeypatch.setattr(backend, 'prepare_chat_photo', prepare)
    monkeypatch.setattr(backend, 'persist_chat', persist)
    row = dict(delivery_status='DELIVERED', image_status='SKIPPED', share_video_event=candidate()['event_id'],
               daily_video_candidates=[candidate()])
    asyncio.run(backend.deliver_photo(SimpleNamespace(_daily_video_worker=Worker()), row, None))
    assert row['share_video_result'] == 'queued' and 'share_video_photo_fallback' not in row


@pytest.mark.parametrize('status,queued', [('SHARE_AUTHORING', False), ('SHARE_AUTHORING', True), ('DAILY_VIDEO_SOURCE_UNAVAILABLE', False)])
def test_owner_reconnect_resumes_unfinished_video_choice(monkeypatch, status, queued):
    from runtime.personal_chat.events import PersonalMessage
    from runtime.personal_chat.service import PersonalChatService
    scheduled = []
    row = dict(letter_id='turn', channel='qq', binding_id=PersonalMessage('qq', '100', '200', '', '').binding_id,
        delivery_status='DELIVERED', image_status='SKIPPED', share_video_event=candidate()['event_id'], share_video_status=status)
    async def photo(*args): pass
    service = PersonalChatService([row], lambda: None, None, None, {'qq': ('100', '200')}, photo=photo)
    monkeypatch.setattr(service, '_schedule_photo', lambda row, send: scheduled.append(row))
    service.resume_media('qq', SimpleNamespace())
    assert scheduled == [row]
    async def persist(*args): pass
    async def prepare(server, row, send):
        if row.get('share_video_photo_fallback'):
            row['image_status'] = 'FAILED'  # Stop before transport; this test covers recovery routing.
    monkeypatch.setattr(backend, 'persist_chat', persist)
    monkeypatch.setattr(backend, 'prepare_chat_photo', prepare)
    worker = SimpleNamespace(get=lambda event_id: {'status': 'PENDING'} if queued else None)
    asyncio.run(backend.deliver_photo(SimpleNamespace(_daily_video_worker=worker), row, None))
    assert bool(row.get('share_video_photo_fallback')) is not queued
    if queued:
        assert row['share_video_result'] == 'queued'
    else:
        assert row['share_video_result'] == 'other'
