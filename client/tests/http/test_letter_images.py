"""Pictures the user puts in a letter: upload, send, what she sees of them, world and memory, the letter view."""
import asyncio
import io
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from original_client_letter_contract import serialize_letter_detail
from runtime import image_understanding as vision
from runtime.media import letter_images


def picture(fmt='PNG', size=(64, 48), noisy=False):
    image = Image.frombytes('RGB', size, os.urandom(size[0] * size[1] * 3)) if noisy else Image.new('RGB', size, (120, 90, 60))
    output = io.BytesIO()
    image.save(output, fmt)
    return output.getvalue()


def observation(summary='窗台上趴着一只橘猫。'):
    return dict(summary=summary, sha256='b' * 64, source='user', width=64, height=48,
                observed_at=datetime.now(timezone.utc).isoformat(), evidence_kind='visual_observation')


def sending(monkeypatch, tmp_path):
    import local_server as server
    from runtime.video_reply_settings import VideoReplySettingsStore
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    monkeypatch.delenv('OLIVIA_JEV_BILLING_ENABLED', raising=False)
    monkeypatch.setattr(server, 'video_reply_settings_store', VideoReplySettingsStore.initialize(tmp_path))
    monkeypatch.setattr(server.store, 'letters', [])
    monkeypatch.setattr(server.store, 'request_keys', {})
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    monkeypatch.setattr(server, '_schedule_reply_job', lambda *a, **k: None)
    monkeypatch.setattr(server, '_missing_memory_component', lambda: None)
    monkeypatch.setattr(server, '_proactive_busy', False)
    monkeypatch.setattr(server, '_current_life_rhythm', lambda: {})
    return server


def test_upload_keeps_png_jpg_and_webp_in_the_media_folder_and_refuses_the_rest(tmp_path, monkeypatch):
    import local_server as server
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    headers = {'X-Olivia-Companion-Action': 'confirmed', 'Content-Type': 'application/octet-stream'}
    async def run():
        app = web.Application(); app.router.add_route('*', '/{tail:.*}', server.handler)
        async with TestClient(TestServer(app)) as client:
            refused = await client.post('/toy/letter/image/upload', data=picture())
            assert refused.status == 403
            for fmt, suffix, mime in (('PNG', '.png', 'image/png'), ('JPEG', '.jpg', 'image/jpeg'), ('WEBP', '.webp', 'image/webp')):
                response = await client.post('/toy/letter/image/upload', data=picture(fmt), headers=headers)
                body = await response.json()
                assert response.status == 200 and body['code'] == 0, body
                name = f'letter-image-{body["data"]["image_id"]}{suffix}'
                assert body['data']['url'] == f'http://127.0.0.1:{server.PORT}/toy/media/{name}'
                served = await client.get('/toy/media/' + name)
                assert served.status == 200 and served.content_type == mime
                assert await served.read() == picture(fmt)
            for raw in (b'', b'not a picture', picture('GIF'), picture('BMP')):
                response = await client.post('/toy/letter/image/upload', data=raw, headers=headers)
                assert response.status == 400 and (await response.json())['data']['error_code'] == 'LETTER_IMAGE_INVALID'
            monkeypatch.setattr(letter_images, 'MAX_UPLOAD_BYTES', 4096)
            response = await client.post('/toy/letter/image/upload', data=picture(size=(200, 200), noisy=True), headers=headers)
            assert response.status == 400 and (await response.json())['data']['error_code'] == 'LETTER_IMAGE_TOO_LARGE'
    asyncio.run(run())
    assert sorted(path.suffix for path in (tmp_path / 'media').iterdir()) == ['.jpg', '.png', '.webp']


def test_a_letter_names_its_uploaded_pictures_and_shows_them_on_the_users_paper(tmp_path, monkeypatch):
    server = sending(monkeypatch, tmp_path)
    ids = [letter_images.save_upload(tmp_path / 'media', picture(fmt))['image_id'] for fmt in ('PNG', 'JPEG')]
    urls = [f'http://127.0.0.1:{server.PORT}/toy/media/letter-image-{ids[0]}.png',
            f'http://127.0.0.1:{server.PORT}/toy/media/letter-image-{ids[1]}.jpg']
    async def run():
        for image_ids, code in (('x', 'LETTER_IMAGES_INVALID'), ([ids[0], ids[0]], 'LETTER_IMAGES_INVALID'),
                                (['../' + ids[0]], 'LETTER_IMAGES_INVALID'), ([ids[0]] + ['0' * 32] * 4, 'LETTER_IMAGES_INVALID'),
                                (['f' * 32], 'LETTER_IMAGE_NOT_FOUND')):
            refused = await server.route('POST', '/toy/letter/send', {'content': '看看这个', 'material': {'image_ids': image_ids}},
                                         {}, defer_reply=True)
            assert refused['code'] == 400 and refused['data']['error_code'] == code, refused
        assert not server.store.letters
        sent = await server.route('POST', '/toy/letter/send', {'content': '看看这个', 'material': {'stampId': 's1', 'image_ids': ids}},
                                  {}, defer_reply=True)
        assert sent['code'] == 0, sent
        letter = server.store.letters[0]
        assert letter['letter_images'] == urls
        detail = serialize_letter_detail(letter)
        assert detail['sentImageUrls'] == urls and detail['material'] == {'stampId': 's1'}
        # A failed letter sent again carries the same pictures.
        letter.update(letter_status='FAILED', error_code='LLM_UNAVAILABLE')
        again = await server.route('POST', '/toy/letter/resend', {'letter_id': letter['letter_id']}, {}, defer_reply=True)
        assert again['code'] == 0, again
        assert server.store.letters[0]['letter_id'] != letter['letter_id'] and server.store.letters[0]['letter_images'] == urls
    asyncio.run(run())
    assert 'sentImageUrls' not in serialize_letter_detail({'letter_id': 'plain', 'content': '没有图片'})
    foreign = {'letter_id': 'odd', 'content': 'x', 'letter_images': [
        'http://evil.test/toy/media/letter-image-' + 'a' * 32 + '.png',
        'http://127.0.0.1:8899/toy/media/photo-' + 'a' * 32 + '.png', 'not a url']}
    assert 'sentImageUrls' not in serialize_letter_detail(foreign)


def test_her_reply_sees_the_pictures_and_they_reach_her_world_and_memory(tmp_path, monkeypatch):
    import local_server as server
    from reply_context import ReplyMode
    from runtime.private_world.daily_life import DailyLifeStore
    from runtime.reply.conversation_context import conversation_context
    names = [letter_images.save_upload(tmp_path / 'media', picture())['name'] for _ in range(2)]
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    world = DailyLifeStore(tmp_path / 'life.sqlite3')
    monkeypatch.setattr(server, 'daily_life_runtime', SimpleNamespace(store=world))
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    described, in_flight = [], []
    async def describe(_server, path, source='generated', *, operation_id=None):
        described.append((path.name, source, operation_id))
        in_flight.append(path.name)
        await asyncio.sleep(0.05)
        assert len(in_flight) == 2, 'the pictures are described at the same time'
        await asyncio.sleep(0.05)
        in_flight.remove(path.name)
        if path.name == names[1]:
            raise RuntimeError('IMAGE_VISION_UNAVAILABLE')
        return observation()
    monkeypatch.setattr(vision, 'describe_image', describe)
    seen = []
    async def run(request, context):
        seen.append(request)
        return object()
    monkeypatch.setattr(server, 'reply_pipeline', SimpleNamespace(run=run))
    letter = {'letter_id': 'letter-with-pictures', 'content': '看看我家猫', 'created_at': 1,
              'letter_images': [f'http://127.0.0.1:{server.PORT}/toy/media/{name}' for name in names]}
    asyncio.run(server._run_reply_pipeline_for_letter(letter, '看看我家猫', ReplyMode.TEXT_LETTER.value, idempotency_key=None))
    request = seen[0]
    assert request.received_user_text == '看看我家猫', 'his own words stay apart from what the pictures show'
    head, block = request.content.split('\n[系统图片观察，非用户原话]\n')
    assert head == '看看我家猫'
    block = json.loads(block)
    assert block['current_turn_has_images'] is True and block['unrecognized_count'] == 1
    assert [(item['summary'], item['channel'], item['parent_id']) for item in block['images']] == [
        ('窗台上趴着一只橘猫。', 'letter', 'letter-with-pictures')]
    assert [item[1] for item in described] == ['user', 'user']
    assert letter['incoming_image_status'] == 'PARTIAL' and letter['reply_timing_seconds']['media_in'] >= 0
    assert len(world.history()['moments']) == 1
    rows = list(vision.image_memory_rows(letter))
    assert len(rows) == 1 and rows[0]['origin'] == 'user'
    assert rows[0]['content'].startswith('用户随信附上的图片，画面：窗台上趴着一只橘猫。')
    # A later turn finds the picture beside this letter.
    letter.update(letter_status='COMPLETED', reply_text='好可爱的猫。')
    dialogue, _ = conversation_context([letter], query='猫', now=datetime.now(timezone.utc))
    assert json.loads(dialogue)['letters'][0]['image_observations'][0]['summary'] == '窗台上趴着一只橘猫。'


def test_each_picture_is_described_once_and_a_missing_file_is_not_guessed(tmp_path, monkeypatch):
    name = letter_images.save_upload(tmp_path / 'media', picture())['name']
    calls = []
    async def describe(_server, path, source='generated', *, operation_id=None):
        calls.append(operation_id)
        return observation()
    monkeypatch.setattr(vision, 'describe_image', describe)
    server = SimpleNamespace(_media_root=lambda: tmp_path / 'media', _persist_store_state=lambda: None, daily_life_runtime=None)
    row = {'letter_id': 'once', 'letter_images': ['http://127.0.0.1:8899/toy/media/' + name,
                                                 'http://127.0.0.1:8899/toy/media/letter-image-' + 'c' * 32 + '.png']}
    asyncio.run(vision.understand_letter_images(server, row))
    asyncio.run(vision.understand_letter_images(server, row))
    assert len(calls) == 1 and calls[0].startswith('once:')
    assert row['incoming_image_failed'] == 1 and len(row['incoming_image_observations']) == 1
    del row['letter_images'][1]
    asyncio.run(vision.understand_letter_images(server, row))
    assert len(calls) == 1 and row['incoming_image_status'] == 'COMPLETED'
    asyncio.run(vision.understand_letter_images(server, row))
    assert len(calls) == 1
    assert json.loads(vision.incoming_context({'letter_images': ['x'], 'incoming_image_failed': 1}).split('\n', 2)[2])[
        'current_turn_has_images'] is True


def test_an_ultra_hdr_phone_photo_is_kept_and_seen(tmp_path, monkeypatch):
    # 10-27: a letter's three phone photos never reached her. Android 14+ phones save Ultra HDR photos, JPEGs that carry
    # a gain map as a second picture; Pillow opens them as MPO and the upload refused them as an unknown format.
    import local_server as server
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    output = io.BytesIO()
    Image.new('RGB', (640, 480), (90, 140, 200)).save(output, 'MPO', save_all=True,
                                                     append_images=[Image.new('RGB', (160, 120), (128, 128, 128))])
    raw = output.getvalue()
    assert Image.open(io.BytesIO(raw)).format == 'MPO'
    headers = {'X-Olivia-Companion-Action': 'confirmed', 'Content-Type': 'application/octet-stream'}

    async def run():
        app = web.Application(); app.router.add_route('*', '/{tail:.*}', server.handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/toy/letter/image/upload', data=raw, headers=headers)
            body = await response.json()
            assert response.status == 200 and body['code'] == 0, body
            return body['data']['image_id']
    image_id = asyncio.run(run())
    path = tmp_path / 'media' / f'letter-image-{image_id}.jpg'
    assert path.read_bytes() == raw                                   # kept as sent, named .jpg
    digest, width, height, uri = vision._pixels(path)                # and what she is shown of it
    assert (width, height) == (640, 480) and uri.startswith('data:image/jpeg;base64,')


def test_a_50_megapixel_phone_photo_is_not_refused(tmp_path):
    big = io.BytesIO()
    Image.new('RGB', (8160, 6144), (200, 200, 200)).save(big, 'JPEG', quality=30)
    saved = letter_images.save_upload(tmp_path, big.getvalue())
    assert (saved['width'], saved['height']) == (8160, 6144)
