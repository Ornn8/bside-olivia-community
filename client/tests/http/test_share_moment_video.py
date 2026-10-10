"""A QQ photo turn may become a short video of her current moment, chosen by the cloud planner."""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from runtime import image_reply
from runtime.cloud_service import CloudError
from runtime.personal_chat.daily_video import select_candidate
from runtime.remote_generation import RemoteGeneration
from tests.http.test_daily_video import API, payload, publish, service, world, worker

SCENES = [dict(scene_id='bedroom', event_kinds=['housework'], locations=['住处'])]


@pytest.mark.parametrize('shared,ok', [('day:synthetic-housework', True), ('../other', False), (7, False)])
def test_task_response_keeps_a_valid_shared_video_event(shared, ok):
    async def scenario():
        async def handler(request):
            return web.json_response({'task_id': 'task-1', 'status': 'cancelled', 'stage': 'skipped', 'share_video': shared})
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', handler)
        async with TestServer(app) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'synthetic')
            if ok:
                assert (await api.request('status', {'task_id': 'task-1'}))['share_video'] == shared
            else:
                with pytest.raises(CloudError):
                    await api.request('status', {'task_id': 'task-1'})
    asyncio.run(scenario())


def photo_turn(tmp_path, monkeypatch, response, row):
    calls = []

    class Cloud:
        url = 'https://gpu.example'
        token = 'synthetic'
        def __init__(self, *args): pass
        async def request(self, action, data):
            return {'server_media_planning': True}
        async def generate(self, kind, data, path, **kwargs):
            calls.append(data)
            return response
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration', Cloud)
    monkeypatch.setattr(image_reply, '_photo_reference', lambda *a: {'world_current_location': '家里'})
    server = SimpleNamespace(video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': True, 'resolution': '1K'}),
                             _media_root=lambda: tmp_path, _persist_store_state=lambda: None, PORT=1234)
    asyncio.run(image_reply._prepare_once(server, row, '在干嘛', '刚收拾好桌边', channel=row.get('channel', 'qq')))
    return calls


def candidate():
    value = {key: item for key, item in payload().items() if key != 'spoken_text'}
    return {**value, 'detail': '整理好了一小块。', 'location': '住处'}


def test_qq_photo_offers_her_moments_and_records_the_video_choice(tmp_path, monkeypatch):
    row = {'letter_id': 'chat-1', 'channel': 'qq', 'daily_video_candidates': [candidate()]}
    calls = photo_turn(tmp_path, monkeypatch, {'task_id': 't', 'stage': 'skipped', 'share_video': 'day:synthetic-housework'}, row)
    assert calls[0]['media_request']['reference']['video_moments'] == [{
        'event_id': 'day:synthetic-housework', 'event_kind': 'housework', 'detail': '整理好了一小块。', 'location': '住处'}]
    assert row['image_status'] == 'SKIPPED' and row['share_video_event'] == 'day:synthetic-housework'


def test_unknown_event_or_plain_skip_shares_nothing(tmp_path, monkeypatch):
    row = {'letter_id': 'chat-2', 'channel': 'qq', 'daily_video_candidates': [candidate()]}
    photo_turn(tmp_path, monkeypatch, {'task_id': 't', 'stage': 'skipped', 'share_video': 'day:another'}, row)
    assert row['image_status'] == 'SKIPPED' and 'share_video_event' not in row
    requested = {'letter_id': 'chat-3', 'channel': 'qq', 'daily_video_candidates': [candidate()]}
    monkeypatch.setattr(image_reply, 'is_companion_image', lambda row: True)  # the user asked for a photo
    (tmp_path / 'r').mkdir()
    calls = photo_turn(tmp_path / 'r', monkeypatch, {'task_id': 't', 'stage': 'skipped'}, requested)
    assert 'video_moments' not in calls[0]['media_request']['reference']


def test_worker_authors_a_shared_moment_once_and_delivers_it(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish(store, payload())
    authored = []

    async def author(value):
        authored.append(value)
        return select_candidate(dict(event_id=value['event_id'], spoken_text='刚收拾好，你看。',
                                     share_text='顺手录给你看看。'), [value])

    async def scenario():
        job = worker(tmp_path, store, chat, api)
        job.scenes, job.capabilities_at = SCENES, __import__('time').monotonic()
        sent = []
        async def send(text):
            raise AssertionError('the clip carries its own caption')
        async def video(path, *, caption, eligible):
            sent.append(caption)
            return 'qq-share-1'
        send.video, send.is_available = video, lambda: True
        job.bind('qq', send)
        offered = job.candidates()
        assert [item['event_id'] for item in offered] == ['day:synthetic-housework']
        await job.share(offered[0], author)
        await job.share(offered[0], author)                  # a replay never authors or orders again
        await job.wait_idle()
        row = job.get('day:synthetic-housework')
        assert row['delivery_status'] == 'DELIVERED' and row['input']['spoken_text'] == '刚收拾好，你看。'
        assert len(authored) == 1 and len(api.calls) == 1 and sent == ['顺手录给你看看。']
        await job.close()
    asyncio.run(scenario())


def test_worker_refuses_a_moment_without_an_approved_scene(tmp_path):
    store, chat, api = world(tmp_path), service(), API()
    publish(store, payload())

    async def author(value):
        raise AssertionError('no writer call without an approved scene')

    async def scenario():
        job = worker(tmp_path, store, chat, api)
        job.scenes, job.capabilities_at = [dict(scene_id='kitchen', event_kinds=['meal'], locations=['厨房'])], __import__('time').monotonic()
        with pytest.raises(ValueError, match='DAILY_VIDEO_SCENE_UNAVAILABLE'):
            await job.share(candidate(), author)
        assert api.calls == []
        await job.close()
    asyncio.run(scenario())


def test_qq_photo_path_queues_the_shared_moment_once(monkeypatch):
    from runtime.personal_chat import backend
    shared = []

    class Worker:
        async def share(self, value, author):
            shared.append(value['event_id'])
    async def persist(server):
        pass
    monkeypatch.setattr(backend, 'persist_chat', persist)
    server = SimpleNamespace(_daily_video_worker=Worker())
    row = {'share_video_event': 'day:synthetic-housework', 'daily_video_candidates': [candidate()]}
    asyncio.run(backend.share_moment_video(server, row))
    assert row['share_video_status'] == 'SHARE_QUEUED' and shared == ['day:synthetic-housework']
    missing = {'share_video_event': 'day:gone', 'daily_video_candidates': [candidate()]}
    asyncio.run(backend.share_moment_video(server, missing))
    assert missing['share_video_status'] == 'DAILY_VIDEO_SOURCE_UNAVAILABLE' and shared == ['day:synthetic-housework']
