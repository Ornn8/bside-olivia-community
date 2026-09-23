import asyncio
import base64
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from runtime import image_understanding as vision
from runtime.personal_chat.events import PersonalMessage, owner_message, combine
from runtime.personal_chat.service import PersonalChatService
from runtime.personal_chat import backend
from runtime.personal_chat.qq import run_qq
from runtime.private_world.daily_life import DailyLifeStore
from runtime.reply.conversation_context import conversation_context
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from runtime.memory.conversation_memory_delivery import CanonicalMemoryDeliveryResult, CanonicalMemoryDeliveryStatus


def observation(source='generated'):
    return dict(source=source, sha256='a' * 64, summary='木桌上放着咖啡杯和一本展开的乐谱。',
                observed_at=datetime.now(timezone.utc).isoformat(), evidence_kind='visual_observation')


def test_qq_images_owner_only_and_burst_preserves_original_ids():
    payload = dict(post_type='message', message_type='private', self_id=100, user_id=200,
                   message_id=1, message=[dict(type='image', data={'url': 'https://gchat.qpic.cn/a.png'})])
    first = owner_message('qq', payload, account_id='100', owner_id='200')
    second = owner_message('qq', {**payload, 'message_id': 2}, account_id='100', owner_id='200')
    assert first.text == '[图片]' and first.input_kind == 'image'
    assert combine([first, second]).images == (('1', 'https://gchat.qpic.cn/a.png'), ('2', 'https://gchat.qpic.cn/a.png'))
    assert owner_message('qq', {**payload, 'user_id': 300}, account_id='100', owner_id='200') is None
    assert owner_message('qq', {**payload, 'message_type': 'group'}, account_id='100', owner_id='200') is None


def test_qq_photo_send_requires_platform_ack(tmp_path):
    async def scenario():
        stop = asyncio.Event()
        async def handle(event, send):
            assert event.input_kind == 'image'
            assert await send.image(tmp_path / 'photo.png') == 'picture-ack'
            stop.set()
        async def socket(request):
            ws = web.WebSocketResponse(); await ws.prepare(request)
            login = await ws.receive_json()
            await ws.send_json(dict(echo=login['echo'], status='ok', retcode=0, data={'user_id': 100}))
            await ws.send_json(dict(post_type='message', message_type='private', self_id=100, user_id=200,
                message_id=1, message=[dict(type='image', data={'url': 'https://gchat.qpic.cn/a.png'})]))
            sent = await ws.receive_json()
            assert sent['params']['message'] == [dict(type='image', data={'file': (tmp_path / 'photo.png').as_uri()})]
            await ws.send_json(dict(echo=sent['echo'], status='ok', retcode=0, data={'message_id': 'picture-ack'}))
            await stop.wait(); await ws.close()
            return ws
        app = web.Application(); app.router.add_get('/', socket)
        async with TestServer(app) as endpoint:
            await asyncio.wait_for(run_qq(str(endpoint.make_url('/')), 'synthetic-qq-token', '100', '200',
                                         handle, stop, merge_seconds=0), 3)
    asyncio.run(scenario())


def test_actual_pixels_are_sent_to_vision_and_long_output_is_rejected(tmp_path):
    path = tmp_path / 'source.png'
    Image.new('RGB', (4096, 3072), (100, 150, 200)).save(path)
    async def scenario():
        requests = []
        async def complete(request):
            payload = await request.json()
            requests.append(payload)
            encoded = payload['messages'][1]['content'][1]['image_url']['url']
            pixels = base64.b64decode(encoded.split(',', 1)[1])
            with Image.open(io.BytesIO(pixels)) as image:
                assert image.size == (1536, 1152)
            assert len(pixels) <= 1024 * 1024 and payload['model'] == 'qwen3.7-flash'
            return web.json_response({'choices': [{'finish_reason': 'stop' if len(requests) == 1 else 'length',
                'message': {'content': json.dumps({'summary': '蓝色画面'})}}]})
        app = web.Application(); app.router.add_post('/v1/chat/completions', complete)
        async with TestServer(app) as endpoint:
            server = SimpleNamespace(letters_adapter=SimpleNamespace(
                config=SimpleNamespace(base_url=str(endpoint.make_url('/v1')), api_key_env=''),
                gateway=SimpleNamespace(_key=lambda: 'synthetic')))
            result = await vision.describe_image(server, path, source='user')
            assert result['source'] == 'user' and result['width'] == 4096 and result['summary'] == '蓝色画面'
            with pytest.raises(ValueError, match='IMAGE_VISION_INCOMPLETE'):
                await vision.describe_image(server, path)
    asyncio.run(scenario())


@pytest.mark.parametrize('url', ['file:///C:/secret.png', 'https://127.0.0.1/a', 'https://qq.com.evil.test/a',
                               'http://gchat.qpic.cn/a', 'https://gchat.qpic.cn:8443/a',
                               'https://multimedia.nt.qq.com.cn.evil.test/download'])
def test_qq_picture_download_rejects_local_and_foreign_urls(url, tmp_path):
    with pytest.raises(ValueError, match='QQ_IMAGE_URL_INVALID'):
        asyncio.run(vision._download_qq_image(url, tmp_path / 'never'))


def test_qq_picture_allows_napcat_nt_cdn_exact_host():
    assert vision._allowed_qq_image_url('https://multimedia.nt.qq.com.cn/download?fileid=synthetic')
    assert not vision._allowed_qq_image_url('https://multimedia.nt.qq.com.cn.evil.test/download')


def test_confirmed_photo_enters_context_world_and_same_memory_outbox_once(tmp_path):
    async def scenario():
        world = DailyLifeStore(tmp_path / 'life.sqlite3')
        row = dict(letter_id='test-exchange', content='看看你桌上的东西', reply_text='给你看看。',
                   reply_revision=1, letter_status='COMPLETED', channel='qq', delivery_status='DELIVERED',
                   created_at=1, image_delivery_status='SENDING', image_description=observation(),
                   image_plan={'attach': True, 'room': 'music-workstation', 'time_of_day': 'night'})
        server = SimpleNamespace(daily_life_runtime=SimpleNamespace(store=world), _persist_store_state=lambda: None)
        await vision.commit_image_memory(server, row)
        assert vision.image_evidence(row) == []
        assert not world.history()['moments']
        row['image_delivery_status'] = 'DELIVERED'
        await vision.commit_image_memory(server, row)
        await vision.commit_image_memory(server, row)
        assert len(world.history()['moments']) == 1
        assert world.snapshot(datetime.now(timezone.utc))['current'] is None
        context = json.loads(world.reply_context('刚才图片里有什么', now=datetime.now(timezone.utc)))
        assert context['image_observations'][0]['summary'] == observation()['summary']
        assert context['image_observations'][0]['scene_location'] == 'music_room'
        assert context['image_observations'][0]['scene_time_of_day'] == 'night'
        assert context['image_observations'][0]['scene_evidence'] == 'generation_plan_not_current_location'
        dialogue, _ = conversation_context([row], query='图片', now=datetime.now(timezone.utc))
        assert json.loads(dialogue)['letters'][0]['image_observations'][0]['source'] == 'generated'
        assert '生成场景：music_room，night' in list(vision.image_memory_rows(row))[0]['reply_text']
        source = tmp_path / 'state.json'
        source.write_text(json.dumps({'letters': [], 'personal_chats': [row]}), encoding='utf8')
        calls = []
        class Committer:
            async def commit(self, delivery):
                calls.append(delivery)
                return CanonicalMemoryDeliveryResult(CanonicalMemoryDeliveryStatus.WRITTEN, delivery.source_id)
        outbox = CanonicalMemoryOutbox(source, tmp_path / 'outbox.sqlite3', Committer())
        await outbox.scan_once(); await outbox.scan_once()
        assert len(calls) == 2  # one existing text source and one photo source
        assert calls[0].source_id != calls[1].source_id
        assert calls[1].origin == 'proactive' and calls[1].user_message == ''
        assert '不证明画面中的事情真实发生' in calls[1].assistant_message
    asyncio.run(scenario())


def test_incoming_picture_is_remembered_before_reply_and_not_as_user_fact(tmp_path):
    row = dict(letter_id='qq-received', delivery_status='FAILED', incoming_image_observations=[observation('user')])
    rows = list(vision.image_memory_rows(row))
    assert len(rows) == 1 and '不证明用户本人' in rows[0]['content']
    assert rows[0]['letter_id'] != row['letter_id']


def test_delivered_photo_scene_does_not_move_world_current_location(tmp_path):
    async def scenario():
        world = DailyLifeStore(tmp_path / 'life.sqlite3')
        now = datetime.now(timezone.utc)
        world.publish_day('day:photo-scene', {'location': '家中厨房', 'activity': '做饭',
                          'note': '我在厨房做饭。'}, [], occurred_at=now)
        row = {'letter_id': 'older-photo', 'image_delivery_status': 'DELIVERED',
               'image_description': observation(),
               'image_plan': {'attach': True, 'room': 'record_shop', 'time_of_day': 'dusk'}}
        server = SimpleNamespace(daily_life_runtime=SimpleNamespace(store=world), _persist_store_state=lambda: None)
        await vision.commit_image_memory(server, row)
        state = world.snapshot(datetime.now(timezone.utc))
        assert state['current']['location'] == '家中厨房'
        context = json.loads(world.reply_context('照片在哪里', now=datetime.now(timezone.utc)))
        assert context['image_observations'][0]['scene_location'] == 'record_shop'
        assert '不证明林离此刻仍在该处' in context['image_observations'][0]['meaning']
    asyncio.run(scenario())


def test_incoming_retry_uses_saved_pixel_observation_and_world_failure_keeps_reply_available(tmp_path, monkeypatch):
    async def scenario():
        calls = []
        async def download(url, path): path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'synthetic')
        async def describe(server, path, source): calls.append(source); return observation(source)
        def unavailable(event): raise OSError('synthetic')
        monkeypatch.setattr(vision, '_download_qq_image', download)
        monkeypatch.setattr(vision, 'describe_image', describe)
        server = SimpleNamespace(_state_root=lambda: tmp_path, _persist_store_state=lambda: None,
            daily_life_runtime=SimpleNamespace(store=SimpleNamespace(record_image_observation=unavailable)))
        event = PersonalMessage('qq', '100', '200', '1', '[图片]', images=(('1', 'https://gchat.qpic.cn/a'),))
        row = dict(letter_id=event.exchange_id, incoming_images=list(event.images))
        await vision.understand_incoming(server, event, row)
        await vision.understand_incoming(server, event, row)
        assert calls == ['user'] and row['image_world_status'] == 'PENDING'
        assert '木桌' in vision.incoming_context(row) and '非用户原话' in vision.incoming_context(row)
    asyncio.run(scenario())


def test_photo_generation_does_not_block_text_or_duplicate_on_replay():
    async def scenario():
        rows, sent = [], []
        started, finish = asyncio.Event(), asyncio.Event()
        async def generate(event, row): return '文字先到。'
        async def commit(row): pass
        async def photo(row, send):
            started.set(); await finish.wait()
            row['image_delivery_status'] = 'DELIVERED'
        async def send(text): sent.append(text); return 'ack'
        async def image(path): return 'image-ack'
        send.image = image
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')}, photo=photo)
        event = PersonalMessage('qq', '100', '200', '1', '给我照片')
        await asyncio.wait_for(service.handle(event, send), .3)
        await started.wait()
        await service.handle(event, send)
        assert len(sent) == 1 and len(service.photo_tasks) == 1
        finish.set(); await asyncio.gather(*service.photo_tasks.values())
        await service.handle(event, send)
        assert not service.photo_tasks
    asyncio.run(scenario())


def test_unconfirmed_photo_send_keeps_text_and_does_not_commit_photo(tmp_path, monkeypatch):
    async def scenario():
        from runtime import image_reply
        row = dict(letter_id='x', delivery_status='DELIVERED', reply_text='文字已发', content='看看',
                   image_status='COMPLETED', prepared_image=str(tmp_path / 'photo.png'), image_description=observation())
        async def prepare(*a, **kw): pass
        async def send_image(path): raise TimeoutError('synthetic')
        monkeypatch.setattr(image_reply, 'prepare', prepare)
        server = SimpleNamespace(_persist_store_state=lambda: None)
        with pytest.raises(TimeoutError):
            await backend.deliver_photo(server, row, SimpleNamespace(image=send_image))
        assert row['delivery_status'] == 'DELIVERED' and row['image_delivery_status'] == 'UNKNOWN'
        assert not vision.image_evidence(row)
    asyncio.run(scenario())


def test_confirmed_qq_photo_persists_world_recovery_marker_before_commit(tmp_path, monkeypatch):
    async def scenario():
        from runtime import image_reply
        row = dict(letter_id='x', delivery_status='DELIVERED', reply_text='文字已发', content='看看',
                   image_status='COMPLETED', prepared_image=str(tmp_path / 'photo.png'))
        persisted = []
        async def prepare(*a, **kw): pass
        async def send_image(path): return 'picture-ack'
        async def interrupted(*a, **kw): raise RuntimeError('synthetic interruption')
        monkeypatch.setattr(image_reply, 'prepare', prepare)
        monkeypatch.setattr(vision, 'commit_image_memory', interrupted)
        server = SimpleNamespace(_persist_store_state=lambda: persisted.append(dict(row)))
        with pytest.raises(RuntimeError, match='synthetic interruption'):
            await backend.deliver_photo(server, row, SimpleNamespace(image=send_image))
        assert persisted[-1]['image_delivery_status'] == 'DELIVERED'
        assert persisted[-1]['image_world_status'] == 'PENDING'
        assert persisted[-1]['image_delivery_receipt'] == 'picture-ack'
    asyncio.run(scenario())


def test_delivered_photo_vision_failure_is_pending_and_retry_recovers(tmp_path, monkeypatch):
    async def scenario():
        world = DailyLifeStore(tmp_path / 'life.sqlite3')
        row = dict(letter_id='x', image_delivery_status='DELIVERED', prepared_image=str(tmp_path / 'photo.png'))
        calls = []
        async def describe(*a, **kw):
            calls.append(1)
            if len(calls) == 1: raise TimeoutError('synthetic')
            return observation()
        monkeypatch.setattr(vision, 'describe_image', describe)
        server = SimpleNamespace(daily_life_runtime=SimpleNamespace(store=world), _persist_store_state=lambda: None)
        await vision.commit_image_memory(server, row)
        await vision.commit_image_memory(server, row)
        assert len(calls) == 1 and row['image_world_status'] == 'PENDING' and not vision.image_evidence(row)
        row['image_description_retry_at'] = 0
        await vision.commit_image_memory(server, row)
        assert len(calls) == 2 and row['image_world_status'] == 'COMMITTED'
        assert len(world.history()['moments']) == 1
    asyncio.run(scenario())


def test_repeated_concurrent_ack_uses_one_vision_request(tmp_path, monkeypatch):
    async def scenario():
        calls = []
        async def describe(*a, **kw):
            calls.append(1); await asyncio.sleep(.01)
            return observation()
        monkeypatch.setattr(vision, 'describe_image', describe)
        server = SimpleNamespace(_persist_store_state=lambda: None,
            daily_life_runtime=SimpleNamespace(store=DailyLifeStore(tmp_path / 'life.sqlite3')))
        row = dict(letter_id='x', image_delivery_status='DELIVERED', prepared_image=str(tmp_path / 'photo.png'))
        await asyncio.gather(vision.commit_image_memory(server, row), vision.commit_image_memory(server, row))
        assert len(calls) == 1
    asyncio.run(scenario())
