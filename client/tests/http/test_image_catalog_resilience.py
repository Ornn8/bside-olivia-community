import asyncio
import json

import pytest

from runtime.cloud_service import CloudError
from runtime.video_reply_settings import VideoReplySettingsStore


@pytest.fixture
def catalog_server(tmp_path, monkeypatch):
    import local_server as server
    from runtime import remote_generation

    state = {'failure': False, 'calls': 0, 'catalog': {'models': [
        {'id': 'photo-a', 'display_name': 'Photo A', 'resolutions': ['1K', '2K']}
    ], 'default_model': 'photo-a'}}

    class Cloud:
        def __init__(self, url, token):
            self.url, self.token = url, token

        async def request(self, action, data):
            assert (action, data) == ('capabilities', {})
            state['calls'] += 1
            if state['failure']:
                raise CloudError('GPU_CONNECTION_FAILED')
            return {'image': state['catalog']}

    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://example.invalid')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-account-a')
    monkeypatch.setattr(remote_generation, 'RemoteGeneration', Cloud)
    monkeypatch.setattr(server, '_LAST_IMAGE_CAPABILITY', {})
    monkeypatch.setattr(server, '_state_root', lambda: tmp_path)
    monkeypatch.setattr(server, '_route_readiness', lambda: {})
    monkeypatch.setattr(server, 'video_reply_settings_store', VideoReplySettingsStore.initialize(tmp_path))
    return server, state


def read(server):
    return asyncio.run(server.route('GET', '/toy/settings/reply-routes', {}, {}))['data']


def test_disk_cache_survives_restart_and_is_account_isolated(catalog_server, tmp_path, monkeypatch):
    server, state = catalog_server
    assert read(server)['image_capability'] == state['catalog']
    files = list((tmp_path / 'image-catalog').glob('*.json'))
    assert len(files) == 1
    assert 'synthetic-account-a' not in files[0].read_text() + files[0].name
    server._LAST_IMAGE_CAPABILITY.clear()
    state['failure'] = True
    assert read(server)['image_capability'] == state['catalog']
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-account-b')
    assert 'image_capability' not in read(server)
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-account-a')
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://other.invalid')
    assert 'image_capability' not in read(server)


@pytest.mark.parametrize('broken', ['{', 'null', '{"models":[{"id":"bad"}]}', '\ufffd'])
def test_corrupt_disk_cache_is_ignored(catalog_server, tmp_path, broken):
    server, state = catalog_server
    read(server)
    next((tmp_path / 'image-catalog').glob('*.json')).write_text(broken, encoding='utf8')
    server._LAST_IMAGE_CAPABILITY.clear()
    state['failure'] = True
    assert 'image_capability' not in read(server)


def test_memory_precedes_disk_and_failed_write_keeps_memory(catalog_server, tmp_path, monkeypatch):
    server, state = catalog_server
    read(server)
    state['catalog'] = {'models': [{'id': 'photo-b', 'display_name': 'B', 'resolutions': ['1K']}]}
    def unavailable(*args):
        raise OSError('synthetic failure')
    monkeypatch.setattr(server, '_atomic_write_store_file', unavailable)
    assert read(server)['image_capability'] == state['catalog']
    state['failure'] = True
    assert read(server)['image_capability'] == state['catalog']
    assert json.loads(next((tmp_path / 'image-catalog').glob('*.json')).read_text())['models'][0]['id'] == 'photo-a'


def test_cached_catalog_cannot_authorize_model_change_or_reenable(catalog_server):
    server, state = catalog_server
    read(server)
    server._LAST_IMAGE_CAPABILITY.clear()
    state['failure'] = True
    image = {'enabled': True, 'resolution': '1K', 'model': 'photo-a'}
    def save(request, value):
        return asyncio.run(server.route('POST', '/toy/settings/reply-routes', {
            'request_id': 'video_reply_setting:' + request, 'image': value}, {}))
    assert save('new', image)['code'] == 503
    settings = server.video_reply_settings_store
    settings.mutate_image('video_reply_setting:seed', image)
    assert save('retain', image)['code'] == 0
    assert save('resolution', {**image, 'resolution': '2K'})['code'] == 503
    assert save('disable', {**image, 'enabled': False})['code'] == 0
    assert save('enable', image)['code'] == 503
    state['failure'] = False
    state['catalog'] = {'models': [{'id': 'photo-b', 'display_name': 'B', 'resolutions': ['2K']}], 'default_model': 'photo-b'}
    assert save('fallback', image)['code'] == 0
    assert settings.image_snapshot() == {'enabled': True, 'resolution': '2K'}


def test_invalid_live_catalog_does_not_replace_last_valid_cache(catalog_server):
    server, state = catalog_server
    expected = read(server)['image_capability']
    state['catalog'] = {'models': [{'id': 'invalid'}]}
    assert read(server)['image_capability'] == {'models': []}
    server._LAST_IMAGE_CAPABILITY.clear()
    state['failure'] = True
    assert read(server)['image_capability'] == expected


def test_warmup_retries_once_after_thirty_seconds(catalog_server, monkeypatch):
    server, state = catalog_server
    state['failure'] = True
    delays = []
    async def sleep(delay):
        delays.append(delay)
        state['failure'] = False
    monkeypatch.setattr(server.asyncio, 'sleep', sleep)
    asyncio.run(server._warm_image_catalog())
    assert delays == [30] and state['calls'] == 2
    assert server._LAST_IMAGE_CAPABILITY


def test_warmup_stops_after_second_failure(catalog_server, monkeypatch):
    server, state = catalog_server
    state['failure'] = True
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(server.asyncio, 'sleep', sleep)
    asyncio.run(server._warm_image_catalog())
    assert delays == [30] and state['calls'] == 2


def test_warmup_lifecycle_does_not_block_startup_and_cancels_on_cleanup(catalog_server, monkeypatch):
    server, state = catalog_server
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def warm():
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        monkeypatch.setattr(server, '_warm_image_catalog', warm)
        lifecycle = server._image_catalog_lifecycle(None)
        await anext(lifecycle)
        await asyncio.wait_for(started.wait(), 1)
        await lifecycle.aclose()
        assert stopped.is_set()
    asyncio.run(scenario())
