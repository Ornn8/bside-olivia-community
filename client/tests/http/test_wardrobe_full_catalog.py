import asyncio
from copy import deepcopy

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from runtime import wardrobe
from runtime.cloud_service import CloudError
from runtime.remote_generation import RemoteGeneration

# Approved category sizes; all labels and hashes below are synthetic fixtures.
COUNTS = {'mori': 14, 'rebellious': 1, 'dark': 4, 'cargo': 1, 'tie-shorts': 1,
          'french': 9, 'earth': 3, 'japanese': 6, 'sweet': 6, 'doll': 10, 'fantasy': 3}
CATEGORIES = [{'id': style, 'name': style, 'description': 'Synthetic clothing',
               'looks': [{'id': f'{style}-{number:02}', 'label': 'Synthetic look',
                          'sha256': 'a' * 64} for number in range(1, count + 1)]}
              for style, count in COUNTS.items()]
ALL_LOOKS = [(c['id'], look['id'], look['sha256']) for c in CATEGORIES for look in c['looks']]


def outfit(style_id='doll', look_id='doll-08', sha256='a' * 64):
    return {'date': '2026-10-05', 'timezone': 'Asia/Shanghai', 'preference_revision': 2,
            'style_id': style_id, 'look_id': look_id, 'catalog_version': wardrobe.DAILY_CATALOG,
            'reference_sha256': sha256}


def state(style_id='original'):
    return {'date': '2026-10-05', 'timezone': 'Asia/Shanghai',
            'catalog_version': wardrobe.DAILY_CATALOG,
            'wardrobe': {'style_id': style_id, 'preference_revision': 2},
            'daily_outfit': None if style_id == 'original' else outfit(style_id, next(
                c for c in CATEGORIES if c['id'] == style_id)['looks'][-1]['id']),
            'wardrobe_styles': [{'style_id': 'original', 'label': 'original', 'description': '', 'looks': []}] + [
                {'style_id': c['id'], 'label': c['name'], 'description': c['description'],
                 'looks': [{'look_id': l['id'], 'label': l['label']} for l in c['looks']]}
                for c in CATEGORIES]}


@pytest.mark.parametrize('style_id,look_id,sha256', ALL_LOOKS)
def test_all_trusted_58_daily_outfits(style_id, look_id, sha256):
    value = outfit(style_id, look_id, sha256)
    assert wardrobe.validate_daily_outfit(value) == value


@pytest.mark.parametrize('style_id', ['original'] + [c['id'] for c in CATEGORIES])
def test_all_12_preferences_accept_complete_server_catalog(style_id):
    value = state(style_id)
    assert wardrobe.validate_cloud_state(value) == value


@pytest.mark.parametrize('key,value', [
    ('reference_sha256', 'A' * 64), ('reference_sha256', '../secret'),
    ('look_id', '../doll-08'), ('look_id', 'doll-11'), ('look_id', 'sweet-06'),
    ('style_id', 'original'), ('style_id', 'unknown'), ('catalog_version', 'untrusted'),
    ('catalog_version', []),
    ('timezone', 'UTC'), ('preference_revision', True), ('preference_revision', 0),
])
def test_daily_metadata_remains_strict(key, value):
    data = outfit(); data[key] = value
    with pytest.raises(ValueError): wardrobe.validate_daily_outfit(data)


def test_daily_schema_cannot_supply_reference_path():
    data = outfit(); data['image'] = '../secret'
    with pytest.raises(ValueError): wardrobe.validate_daily_outfit(data)


@pytest.mark.parametrize('mutation', ['duplicate_style', 'duplicate_look', 'unknown_look', 'path_look', 'original_look', 'unknown_style'])
def test_catalog_remains_bounded_and_rejects_untrusted_ids(mutation):
    data = state()
    if mutation == 'duplicate_style': data['wardrobe_styles'][-1] = deepcopy(data['wardrobe_styles'][1])
    elif mutation == 'duplicate_look': data['wardrobe_styles'][1]['looks'][1] = deepcopy(data['wardrobe_styles'][1]['looks'][0])
    elif mutation == 'unknown_look': data['wardrobe_styles'][-1]['looks'][-1]['look_id'] = 'doll-99'
    elif mutation == 'path_look': data['wardrobe_styles'][-1]['looks'][-1]['look_id'] = '../secret'
    elif mutation == 'original_look': data['wardrobe_styles'][0]['looks'] = [{'look_id': 'original-01', 'label': 'bad'}]
    else: data['wardrobe_styles'][-1]['style_id'] = 'unknown'
    with pytest.raises(ValueError): wardrobe.validate_cloud_state(data)


def test_old_six_style_projection_still_valid_and_legacy_original_unchanged():
    value = state('dark'); value['wardrobe_styles'] = value['wardrobe_styles'][:6]
    assert wardrobe.validate_cloud_state(value) == value
    assert wardrobe.validate_daily_outfit(None) is None
    assert wardrobe.DEFAULT_STYLE == 'original'
    assert len(wardrobe.catalog()) == 6
    assert wardrobe.photo_reference({}) == {}
    plan = {'prompt': 'synthetic', 'photo_type': 'portrait'}
    assert wardrobe.dress_photo(plan, {}) == plan


def test_actual_adapter_all_styles_all_previews_and_all_daily_metadata():
    async def scenario():
        headers_seen = []
        async def catalog(request):
            assert request.headers['X-Olivia-Wardrobe-Catalog'] == wardrobe.CLOUD_CATALOG_PROTOCOL
            headers_seen.append(request.headers.get('X-Olivia-Wardrobe-Protocol'))
            style_id = (await request.json())['style_id'] if request.method == 'POST' else 'original'
            return web.json_response(state(style_id))
        async def preview(request):
            headers_seen.append(request.headers.get('X-Olivia-Wardrobe-Protocol'))
            assert request.match_info['look_id'] in {l[1] for l in ALL_LOOKS}
            return web.Response(body=b'\x89PNG\r\n\x1a\nsynthetic')
        async def task(request):
            headers_seen.append(request.headers.get('X-Olivia-Wardrobe-Protocol'))
            style_id, look_id, sha256 = ALL_LOOKS[int(request.match_info['task_id'])]
            return web.json_response({'task_id': request.match_info['task_id'], 'status': 'succeeded', 'outputs': [],
                'media_plan': {'prompt': 'synthetic', 'photo_type': 'portrait', 'room': 'none', 'time_of_day': 'day',
                               'daily_outfit': outfit(style_id, look_id, sha256)}})
        app = web.Application()
        app.router.add_get('/v1/wardrobe', catalog); app.router.add_post('/v1/wardrobe', catalog)
        app.router.add_get('/v1/wardrobe/images/{look_id}', preview)
        app.router.add_get('/v1/tasks/{task_id}', task)
        async with TestServer(app) as server:
            api = RemoteGeneration(str(server.make_url('')).rstrip('/'), 'synthetic-fixture')
            assert len((await api.request('wardrobe_get', {}))['wardrobe_styles']) == 12
            for c in CATEGORIES:
                result = await api.request('wardrobe_set', {'request_id': 'fixture-' + c['id'], 'style_id': c['id']})
                assert result['wardrobe']['style_id'] == c['id']
            for i, (style_id, look_id, sha256) in enumerate(ALL_LOOKS):
                assert (await api.wardrobe_image(look_id)).startswith(b'\x89PNG')
                result = await api.request('status', {'task_id': str(i)})
                assert result['media_plan']['daily_outfit']['look_id'] == look_id
            assert set(headers_seen) == {'daily-v2'}
            for invalid in ('../secret', 'doll/11', 'earth-xx', 'doll-99999'):
                with pytest.raises(CloudError) as caught: await api.wardrobe_image(invalid)
                assert caught.value.code == 'WARDROBE_IMAGE_INVALID'
    asyncio.run(scenario())


def test_rolling_upgrade_accepts_old_catalog_but_not_new_ids_under_old_version():
    value = state()
    value['catalog_version'] = wardrobe.PREVIOUS_DAILY_CATALOG
    old = wardrobe.DAILY_CATALOGS[wardrobe.PREVIOUS_DAILY_CATALOG]
    value['wardrobe_styles'] = [
        {**s, 'looks': [look for look in s['looks'] if look['look_id'] in old.get(s['style_id'], ())]}
        for s in value['wardrobe_styles'] if s['style_id'] == 'original' or s['style_id'] in old]
    assert wardrobe.validate_cloud_state(value) == value
    original = {**outfit(), 'catalog_version': wardrobe.PREVIOUS_DAILY_CATALOG}
    assert wardrobe.validate_daily_outfit(original) == original
    with pytest.raises(ValueError):
        wardrobe.validate_daily_outfit({**outfit('fantasy', 'fantasy-02'),
                                       'catalog_version': wardrobe.PREVIOUS_DAILY_CATALOG})
    value['purchases'] = {'price_cents': 500, 'free_limit': 3, 'free_remaining': 2, 'owned': ['fantasy-02']}
    with pytest.raises(ValueError): wardrobe.validate_cloud_state(value)
