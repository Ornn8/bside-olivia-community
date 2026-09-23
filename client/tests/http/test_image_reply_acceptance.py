import asyncio
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from runtime.video_reply_settings import VideoReplySettingsStore, VideoReplySettingsError


@pytest.mark.parametrize('legacy', [False, True])
def test_image_preference_defaults_off_and_persists_without_changing_tier(tmp_path, legacy):
    if legacy:
        (tmp_path / 'video_reply_settings.json').write_text(json.dumps({'schema_version': 1, 'settings': {}, 'ledger': {}}))
        settings = VideoReplySettingsStore(tmp_path)
    else:
        settings = VideoReplySettingsStore.initialize(tmp_path)
    assert settings.image_snapshot() == {'enabled': False, 'resolution': '1K'}
    before = settings.tier_snapshot()
    value = {'enabled': True, 'resolution': '4K'}
    settings.mutate_image('video_reply_setting:image', value)
    restored = VideoReplySettingsStore(tmp_path)
    assert restored.image_snapshot() == value and restored.tier_snapshot() == before
    assert restored.mutate_image('video_reply_setting:image', value)['status'] == 'DUPLICATE'
    with pytest.raises(VideoReplySettingsError):
        restored.mutate_image('video_reply_setting:image', {'enabled': True, 'resolution': '2K'})


def test_image_settings_http_preserves_receive_snapshot(tmp_path, monkeypatch):
    import local_server as server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(server, 'video_reply_settings_store', settings)
    monkeypatch.setattr(server, '_route_readiness', lambda: {})
    async def scenario():
        response = await server.route('POST', '/toy/settings/reply-routes',
            {'request_id': 'video_reply_setting:image', 'image': {'enabled': True, 'resolution': '2K'}}, {})
        assert response['code'] == 0
        response = await server.route('GET', '/toy/settings/reply-routes', {}, {})
        assert response['data']['image'] == {'enabled': True, 'resolution': '2K'}
        saved = settings.image_snapshot()
        settings.mutate_image('video_reply_setting:image-next', {'enabled': False, 'resolution': '1K'})
        assert saved == {'enabled': True, 'resolution': '2K'}
    asyncio.run(scenario())


def test_reply_format_updates_tier_and_photo_in_one_transaction(tmp_path, monkeypatch):
    import local_server as server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(server, 'video_reply_settings_store', settings)
    monkeypatch.setattr(server, '_route_readiness', lambda: {})
    async def scenario():
        body = {'request_id': 'video_reply_setting:combined', 'tier': 'audio',
                'image': {'enabled': True, 'resolution': '2K'}}
        response = await server.route('POST', '/toy/settings/reply-routes', body, {})
        assert response['code'] == 0
        assert settings.tier_snapshot() == 'audio'
        assert settings.image_snapshot() == body['image']
        assert (await server.route('POST', '/toy/settings/reply-routes', body, {}))['code'] == 0
    asyncio.run(scenario())


def test_native_png_serving_returns_pixels_and_rejects_traversal(tmp_path, monkeypatch):
    import local_server as server
    path = tmp_path / ('photo-' + 'a' * 32 + '.png')
    Image.new('RGB', (16, 16), 'gray').save(path)
    monkeypatch.setattr(server, '_media_root', lambda: tmp_path)
    async def scenario():
        app = web.Application(); app.router.add_get('/toy/media/{name}', server._media_handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/toy/media/' + path.name)
            assert response.status == 200 and response.content_type == 'image/png'
            assert await response.read() == path.read_bytes()
            assert (await client.get('/toy/media/private.txt')).status == 404
            assert (await client.get('/toy/media/..%5Cprivate.png')).status == 404
    asyncio.run(scenario())


def test_photo_projection_and_ack_wait_for_publication(tmp_path, monkeypatch):
    import local_server as server
    from original_client_letter_contract import serialize_letter_detail
    from runtime import image_understanding
    calls = []
    async def commit(owner, row): calls.append(row['image_delivery_status'])
    monkeypatch.setattr(image_understanding, 'commit_image_memory', commit)
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    name = 'photo-' + 'b' * 32 + '.png'
    row = dict(letter_id='photo-test', content='看看', reply_text='给你看看', letter_status='COMPLETED',
               reply_mode='text_letter', image_reply_settings={'enabled': True, 'resolution': '2K'},
               image_status='COMPLETED', reply_image_url='http://127.0.0.1:8876/toy/media/' + name,
               image_resolution='2K', image_render_mode='resized', reply_not_before=4_102_444_800)
    monkeypatch.setattr(server.store, 'letters', [row])
    assert 'replyImageUrl' not in serialize_letter_detail(row)
    async def scenario():
        rejected = await server.route('POST', '/toy/image/ack', {'filename': name}, {}, companion_confirmed=True)
        assert rejected['code'] == 409 and not calls
        row['reply_not_before'] = 0
        projected = serialize_letter_detail(row)
        assert projected['imageRequestId'] == row['letter_id'] and projected['imageResolution'] == '2K'
        assert projected['imageRenderMode'] == 'resized'
        status = await server.route('GET', '/toy/image/status', {}, {'letter_id': row['letter_id']})
        assert status['data']['imageStatus'] == 'COMPLETED'
        result = await server.route('POST', '/toy/image/ack', {'filename': name}, {}, companion_confirmed=True)
        assert result['code'] == 0 and calls == ['DELIVERED']
        assert row['image_world_status'] == 'PENDING'
        assert row['reply_text'] == '给你看看'
    asyncio.run(scenario())


def test_settings_bootstrap_javascript_syntax(tmp_path):
    from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
    path = tmp_path / 'settings.js'
    path.write_text(BOOTSTRAP_JAVASCRIPT, encoding='utf8')
    result = subprocess.run(['node', '--check', str(path)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode('utf8', errors='replace')
