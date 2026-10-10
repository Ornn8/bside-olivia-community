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
def test_image_preference_defaults_on_and_persists_without_changing_tier(tmp_path, legacy):
    if legacy:
        (tmp_path / 'video_reply_settings.json').write_text(json.dumps({'schema_version': 1, 'settings': {}, 'ledger': {}}))
        settings = VideoReplySettingsStore(tmp_path)
    else:
        settings = VideoReplySettingsStore.initialize(tmp_path)
    # Photos are the main paid feature: on until the user turns them off.
    assert settings.image_snapshot() == {'enabled': True, 'resolution': '1K'}
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


def test_photo_status_exposes_only_sanitized_failure_facts(monkeypatch):
    import local_server as server
    row = dict(letter_id='failed-photo', content='test', reply_text='test', letter_status='COMPLETED',
               reply_mode='text_letter', image_reply_settings={'enabled': True, 'resolution': '1K'},
               image_status='FAILED', image_error_code='GPU_AUTH_FAILED', image_phase='submission',
               image_cloud_status='failed', image_cloud_task_id='private-task', prepared_image='private-path')
    monkeypatch.setattr(server.store, 'letters', [row])
    async def scenario():
        result = (await server.route('GET', '/toy/image/status', {}, {'letter_id': row['letter_id']}))['data']
        assert result['imageErrorCode'] == 'GPU_AUTH_FAILED'
        assert result['imagePhase'] == 'submission'
        assert result['imageCloudStatus'] == 'failed'
        assert 'private-' not in str(result)
        row.update(image_error_code='secret/path?token=private', image_phase='private-path')
        result = (await server.route('GET', '/toy/image/status', {}, {'letter_id': row['letter_id']}))['data']
        assert 'imageErrorCode' not in result and 'imagePhase' not in result
    asyncio.run(scenario())


def test_explicitly_disabled_photos_stay_off(tmp_path):
    settings = VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate_image('video_reply_setting:image-off', {'enabled': False, 'resolution': '1K'})
    assert VideoReplySettingsStore(tmp_path).image_snapshot() == {'enabled': False, 'resolution': '1K'}


def test_image_model_is_persisted_and_frozen_without_backfilling_legacy(tmp_path):
    settings = VideoReplySettingsStore.initialize(tmp_path)
    assert 'model' not in settings.image_snapshot()
    image = {'enabled': True, 'resolution': '2K', 'model': 'approved-photo'}
    settings.mutate_image('video_reply_setting:model', image)
    frozen = settings.image_snapshot()
    restored = VideoReplySettingsStore(tmp_path)
    assert restored.image_snapshot() == image
    assert restored.mutate_image('video_reply_setting:model', image)['status'] == 'DUPLICATE'
    restored.mutate_image('video_reply_setting:other-model', {**image, 'model': 'other-photo'})
    assert frozen == image
    with pytest.raises(VideoReplySettingsError):
        restored.mutate_image('video_reply_setting:model', {**image, 'model': 'other-photo'})


def test_settings_only_accept_models_and_resolutions_advertised_by_cloud(tmp_path, monkeypatch):
    import local_server as server
    from runtime import remote_generation
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(server, 'video_reply_settings_store', settings)
    monkeypatch.setattr(server, '_route_readiness', lambda: {})
    caps = {'kinds': ['image'], 'shared_assets': [], 'image': {'models': [
        {'id': 'approved-photo', 'display_name': 'Approved photo', 'resolutions': ['2K'],
         'price_ranges_cents': {'2K': [32, 32]}}
    ], 'default_model': 'approved-photo'}}
    class Cloud:
        def __init__(self, *args): self.url, self.token = 'synthetic', 'synthetic'
        async def request(self, action, data):
            assert (action, data) == ('capabilities', {})
            return caps
    monkeypatch.setattr(remote_generation, 'RemoteGeneration', Cloud)
    async def scenario():
        state = (await server.route('GET', '/toy/settings/reply-routes', {}, {}))['data']
        assert state['image_capability'] == caps['image']
        for model, resolution in [('unapproved-photo', '2K'), ('approved-photo', '4K')]:
            response = await server.route('POST', '/toy/settings/reply-routes', {
                'request_id': 'video_reply_setting:rejected-' + model + resolution,
                'image': {'enabled': True, 'resolution': resolution, 'model': model}}, {})
            assert response['code'] != 0 and response['data']['error_code'] == 'IMAGE_MODEL_UNAVAILABLE'
        assert 'model' not in settings.image_snapshot()
        selected = {'enabled': True, 'resolution': '2K', 'model': 'approved-photo'}
        assert (await server.route('POST', '/toy/settings/reply-routes', {
            'request_id': 'video_reply_setting:approved', 'image': selected}, {}))['code'] == 0
        caps['image']['models'] = []
        assert (await server.route('POST', '/toy/settings/reply-routes', {
            'request_id': 'video_reply_setting:approved', 'image': selected}, {}))['code'] == 0
        # Saving a text/audio tier must not depend on a retired image model.
        assert (await server.route('POST', '/toy/settings/reply-routes', {
            'request_id': 'video_reply_setting:tier-after-retirement', 'tier': 'audio', 'image': selected}, {}))['code'] == 0
        # Users can still disable an unavailable saved model.
        assert (await server.route('POST', '/toy/settings/reply-routes', {
            'request_id': 'video_reply_setting:disable', 'image': {**selected, 'enabled': False}}, {}))['code'] == 0
        caps['image'].pop('models')
        assert 'image_capability' not in (await server.route('GET', '/toy/settings/reply-routes', {}, {}))['data']
    asyncio.run(scenario())


def test_cloud_catalog_failure_keeps_old_settings_available(tmp_path, monkeypatch):
    import local_server as server
    from runtime import remote_generation
    from runtime.cloud_service import CloudError
    settings = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(server, 'video_reply_settings_store', settings)
    monkeypatch.setattr(server, '_route_readiness', lambda: {})
    class Cloud:
        def __init__(self, *args): self.url, self.token = 'synthetic', 'synthetic'
        async def request(self, *args): raise CloudError('GPU_CONNECTION_FAILED')
    monkeypatch.setattr(remote_generation, 'RemoteGeneration', Cloud)
    async def scenario():
        result = await server.route('GET', '/toy/settings/reply-routes', {}, {})
        assert result['code'] == 0 and 'image_capability' not in result['data']
        assert (await server.route('POST', '/toy/settings/reply-routes', {
            'request_id': 'video_reply_setting:offline-tier', 'tier': 'audio',
            'image': {'enabled': True, 'resolution': '2K'}}, {}))['code'] == 0
        result = await server.route('POST', '/toy/settings/reply-routes', {
            'request_id': 'video_reply_setting:offline-model',
            'image': {'enabled': True, 'resolution': '1K', 'model': 'unverified-photo'}}, {})
        assert result['code'] == 503 and result['data']['error_code'] == 'IMAGE_MODEL_UNAVAILABLE'
        assert settings.image_snapshot() == {'enabled': True, 'resolution': '2K'}
    asyncio.run(scenario())


def test_image_model_contract_preserves_legacy_and_rejects_unusable_ids():
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from runtime.video_reply_settings import image_model_capability
    validator = Draft202012Validator(json.loads(Path('contracts/video_reply_settings.schema.json').read_text(encoding='utf8')))
    for model in [None, 'approved-photo']:
        image = {'enabled': True, 'resolution': '1K', **({'model': model} if model else {})}
        validator.validate({'request_id': 'video_reply_setting:contract', 'image': image})
        validator.validate({'request_id': 'video_reply_setting:contract', 'tier': 'audio', 'image': image})
    for model in ['', '../provider', 'https://private.invalid/key', 1, 'a' * 129]:
        assert list(validator.iter_errors({'request_id': 'video_reply_setting:bad-model',
            'image': {'enabled': True, 'resolution': '1K', 'model': model}}))
    assert image_model_capability({'image': {'resolutions': {}}}) is None
    assert image_model_capability({'image': {'models': [], 'default_model': {'malformed': True}}}) == {'models': []}
    assert image_model_capability({'image': {'models': [{'id': 'local-photo', 'display_name': 'Photo', 'resolutions': ['8K']}]}}) == {'models': []}


def test_image_model_catalog_preserves_prices_and_existing_default():
    from runtime.video_reply_settings import image_model_capability, require_image_model
    catalog = {'default_model': 'approved-photo', 'models': [
        {'id': 'approved-photo', 'display_name': 'Approved photo', 'resolutions': ['1K']},
        {'id': 'local-image', 'display_name': '本地模型', 'resolutions': ['1K', '2K'],
         'price_ranges_cents': {'1K': [10, 20], '2K': [27, 37]}},
        {'id': 'ranged-photo', 'display_name': 'Ranged photo', 'resolutions': ['1K'],
         'price_ranges_cents': {'1K': [10, 14]}}
    ]}
    result = image_model_capability({'image': catalog})
    assert result == catalog
    require_image_model({'model': 'local-image', 'resolution': '2K'}, {'image': catalog})
    result['models'][1]['price_ranges_cents']['1K'][0] = 99
    assert catalog['models'][1]['price_ranges_cents']['1K'] == [10, 20]


@pytest.mark.parametrize('prices', [
    None, {}, {'2K': [15, 15]}, {'1K': [15]}, {'1K': [15, 15, 15]},
    {'1K': [True, 15]}, {'1K': [15, '15']}, {'1K': [-1, 15]},
    {'1K': [16, 15]}, {'1K': [15, 15.0]}, {'1K': [15, 2**53]},
])
def test_image_model_catalog_rejects_invalid_prices(prices):
    from runtime.video_reply_settings import image_model_capability
    assert image_model_capability({'image': {'models': [
        {'id': 'local-image', 'display_name': '本地模型', 'resolutions': ['1K'],
         'price_ranges_cents': prices}
    ]}}) == {'models': []}
