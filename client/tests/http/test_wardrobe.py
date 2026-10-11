import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image

from runtime import image_reply
from runtime.video_reply_settings import VideoReplySettingsStore, VideoReplySettingsError


def test_style_persists_and_frozen_photo_does_not_follow_later_changes(tmp_path):
    store = VideoReplySettingsStore.initialize(tmp_path)
    before = store.image_snapshot()
    store.mutate_wardrobe('video_reply_setting:outfit', {'style_id': 'home_knit'})
    frozen = store.image_snapshot()
    assert frozen['wardrobe_style'] == 'home_knit'
    assert VideoReplySettingsStore(tmp_path).wardrobe_snapshot() == {'style_id': 'home_knit'}
    store.mutate_wardrobe('video_reply_setting:next', {'style_id': 'stage'})
    assert frozen['wardrobe_style'] == 'home_knit'
    assert 'wardrobe_style' not in before
    assert store.tier_snapshot() == 'text'
    assert store.mutate_wardrobe('video_reply_setting:outfit', {'style_id': 'home_knit'})['status'] == 'DUPLICATE'
    with pytest.raises(VideoReplySettingsError):
        store.mutate_wardrobe('video_reply_setting:outfit', {'style_id': 'stage'})
    with pytest.raises(VideoReplySettingsError):
        store.mutate('video_reply_setting:outfit', False)
    with pytest.raises(VideoReplySettingsError):
        store.mutate_wardrobe('video_reply_setting:invalid', {'style_id': 'https://arbitrary.example'})


def test_wardrobe_api_does_not_change_other_preferences(tmp_path, monkeypatch):
    import local_server as server
    store = VideoReplySettingsStore.initialize(tmp_path)
    monkeypatch.setattr(server, 'video_reply_settings_store', store)
    monkeypatch.setattr(server, '_route_readiness', lambda: {})
    async def scenario():
        result = await server.route('POST', '/toy/settings/reply-routes',
            {'request_id': 'video_reply_setting:wardrobe', 'wardrobe': {'style_id': 'city_casual'}}, {})
        assert result['code'] == 0
        result = (await server.route('GET', '/toy/settings/reply-routes', {}, {}))['data']
        assert result['wardrobe'] == {'style_id': 'city_casual'}
        assert len(result['wardrobe_styles']) == 6
        assert result['tier'] == 'text' and result['image']['enabled'] is True
    asyncio.run(scenario())


@pytest.mark.parametrize('supported,applied', [(True, True), (False, False), (True, False)])
def test_server_photo_receives_frozen_style_or_stops_before_paid_dispatch(tmp_path, monkeypatch, supported, applied):
    calls = []
    class API:
        url, token = 'https://gpu.example', 'synthetic'
        def __init__(self, *args): pass
        async def request(self, *args):
            return {'server_media_planning': True, 'wardrobe_styles': ['home_knit'] if supported else []}
        async def generate(self, kind, data, path, **kwargs):
            calls.append(data)
            Image.new('RGB', (675, 900)).save(path)
            kwargs['validate'](path)
            return {'task_id': 'synthetic', 'stage': 'completed', 'media_plan': {
                'photo_type': 'portrait', 'room': 'none', 'time_of_day': 'night',
                'wardrobe_style': 'home_knit' if applied else 'original'}}
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration', API)
    async def describe(*args, **kwargs): return {'source': 'generated'}
    monkeypatch.setattr('runtime.image_understanding.describe_image', describe)
    server = SimpleNamespace(_media_root=lambda: tmp_path, _persist_store_state=lambda: None,
        video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {}), PORT=1234)
    row = {'letter_id': 'synthetic', 'image_reply_settings': {
        'enabled': True, 'resolution': '1K', 'wardrobe_style': 'home_knit'}}
    asyncio.run(image_reply._prepare_once(server, row, 'photo', 'reply', channel='qq'))
    if supported and applied:
        assert row['image_status'] == 'COMPLETED'
        assert calls[0]['media_request']['reference']['wardrobe'] == {'catalog_version': 1, 'style_id': 'home_knit'}
    elif not supported:
        assert row['image_status'] == 'FAILED'
        assert row['image_error_code'] == 'IMAGE_WARDROBE_UNAVAILABLE'
        assert not calls
    else:
        assert row['image_status'] == 'FAILED'
        assert row['image_error_code'] == 'IMAGE_WARDROBE_NOT_APPLIED'
        assert len(calls) == 1


def test_failed_save_keeps_last_style_and_setting_remains_readable(tmp_path):
    store = VideoReplySettingsStore.initialize(tmp_path)
    store.mutate_wardrobe('video_reply_setting:first', {'style_id': 'home_knit'})
    def denied(*args): raise OSError('synthetic denied')
    store._writer = denied
    with pytest.raises(VideoReplySettingsError):
        store.mutate_wardrobe('video_reply_setting:failed', {'style_id': 'stage'})
    assert store.wardrobe_snapshot()['style_id'] == 'home_knit'
    assert VideoReplySettingsStore(tmp_path).wardrobe_snapshot()['style_id'] == 'home_knit'


def test_legacy_prompt_uses_catalog_only_and_snapshot_does_not_add_a_person():
    from runtime.wardrobe import dress_photo
    plan = {'prompt': 'A portrait wearing the old outfit.', 'photo_type': 'portrait'}
    dressed = dress_photo(plan, {'wardrobe_style': 'home_knit'})
    assert 'charcoal knit' in dressed['prompt']
    assert plan['prompt'] == 'A portrait wearing the old outfit.'
    assert dress_photo({**plan, 'photo_type': 'snapshot'}, {'wardrobe_style': 'home_knit'})['prompt'] == plan['prompt']
    assert dress_photo(plan, {}) == plan
