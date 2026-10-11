"""Interface pictures are cloud-owned: new or replaced pictures need no client release."""
import asyncio
import hashlib
from functools import wraps

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from runtime import image_assets as assets
from runtime.cloud_service import CloudError


def _async_test(func):
    @wraps(func)
    def run(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return run


def _entry(raw, name):
    digest = hashlib.sha256(raw).hexdigest()
    return dict(filename=name + '.webp', sha256=digest, size_bytes=len(raw), content_type='image/webp',
                key=f'distribution/olivia-images/ui/{digest}/{name}.webp')


@_async_test
async def test_new_and_replaced_pictures_follow_the_cloud_and_stay_offline(tmp_path, monkeypatch):
    new = b'RIFF\x00\x00\x00\x00WEBPVP8 new kitten'
    old = b'RIFF\x00\x00\x00\x00WEBPVP8 old kitten'
    # The shipped catalog knows an old picture for one id and nothing for the other.
    monkeypatch.setattr(assets, '_catalog', lambda: {'ui': {'pet-cat-1': _entry(old, 'pet-cat-1')}})
    served = {'pet-cat-1': new, 'pet-cat-9': new}

    async def ticket(request):
        name = request.match_info['name']
        if name not in served:
            return web.Response(status=404)
        return web.json_response({**{k: v for k, v in _entry(served[name], name).items() if k != 'key'},
                                  'url': str(client.make_url('/obj/' + name + '.webp'))})

    async def download(request):
        return web.Response(body=served[request.match_info['name'][:-5]])
    app = web.Application()
    app.router.add_get('/v1/components/images/ui/{name}', ticket)
    app.router.add_get('/obj/{name}', download)
    client = TestClient(TestServer(app))
    await client.start_server()
    monkeypatch.setattr(assets, '_validate_download_url', lambda url, _: None)
    try:
        base = str(client.make_url(''))
        for name in ('pet-cat-1', 'pet-cat-9'):
            path = await assets.ensure_image(tmp_path, 'ui', name, base_url=base)
            assert path.read_bytes() == new
    finally:
        await client.close()
    # Offline: the remembered cloud entry still serves the cached picture.
    for name in ('pet-cat-1', 'pet-cat-9'):
        assert (await assets.ensure_image(tmp_path, 'ui', name, base_url='http://127.0.0.1:1')).read_bytes() == new
    with pytest.raises(CloudError):
        await assets.ensure_image(tmp_path, 'ui', 'pet-cat-404', base_url='http://127.0.0.1:1')


def test_ticket_must_point_at_the_official_object_for_its_digest():
    raw = b'RIFF\x00\x00\x00\x00WEBPVP8 x'
    ticket = {k: v for k, v in _entry(raw, 'pet-cat-1').items() if k != 'key'}
    with pytest.raises(CloudError):
        assets._ticket_entry('ui', 'pet-cat-1', {**ticket, 'url': 'https://evil.example/pet-cat-1.webp'})
    with pytest.raises(CloudError):
        assets._ticket_entry('ui', '../x', {**ticket, 'url': 'https://evil.example/x.webp'})
