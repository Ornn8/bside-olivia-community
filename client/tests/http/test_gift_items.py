import asyncio
from datetime import datetime, timezone

import pytest

from runtime.gifts import validate_gifts, validate_purchase


def item(**changes):
    return dict(id='vinyl-record', name='黑胶', category='music', summary='一张唱片', use='听音乐',
                price_cents=0, owned=False, repeatable=True, customizable=True, custom=None, **changes)


def test_catalog_unknown_fields_zero_amount_and_legacy():
    assert validate_gifts({'cameras': []}) == {'cameras': []}
    value = item()
    assert validate_gifts({'cameras': [], 'items': [{**value, 'future': 'field'}]})['items'] == [value]
    assert validate_purchase({'item': 'vinyl-record', 'custom': {'title': '  合成专辑  '}})['custom']['title'] == '合成专辑'


@pytest.mark.parametrize('title', ['', ' '*5, '字'*61, None])
def test_custom_title_rejects_invalid_input(title):
    with pytest.raises(ValueError):
        validate_purchase({'item': 'vinyl-record', 'custom': {'title': title}})


def test_world_preserves_distinct_titles(tmp_path):
    from runtime.diary.diary import DiaryStore
    store = DiaryStore(tmp_path / 'diary.sqlite3')
    now = datetime.now(timezone.utc)
    for title in ('合成专辑甲', '合成专辑乙'):
        store.remember_gifts([{**item(), 'owned': True, 'custom': {'title': title}}], now)
    gifts = store.gifts()
    assert gifts[0]['titles'] == ['合成专辑甲', '合成专辑乙']


def test_repeatable_purchase_is_forwarded_each_time():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from runtime.remote_generation import RemoteGeneration
    async def scenario():
        purchases = []
        async def handler(request):
            purchases.append(await request.json())
            return web.json_response({'cameras': [], 'items': [{**item(), 'owned': True,
                'custom': purchases[-1]['custom']}]})
        app = web.Application(); app.router.add_post('/v1/gifts', handler)
        async with TestServer(app) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'synthetic')
            request = {'item': 'vinyl-record', 'custom': {'title': '合成专辑'}}
            for _ in range(2):
                assert (await api.request('gifts_buy', request))['items'][0]['owned']
        assert purchases == [request, request]
    asyncio.run(scenario())
