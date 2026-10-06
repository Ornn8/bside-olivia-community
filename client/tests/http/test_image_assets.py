import asyncio
import hashlib
import json
from functools import wraps
from pathlib import Path

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


@_async_test
async def test_lazy_r2_download_survives_restart_and_refreshes_expired_ticket(tmp_path, monkeypatch):
    raw = b'\x89PNG\r\n\x1a\n' + b'synthetic image'
    entry = dict(filename='linli-01.png', sha256=hashlib.sha256(raw).hexdigest(),
                 size_bytes=len(raw), content_type='image/png',
                 key=f'distribution/olivia-images/stickers/{hashlib.sha256(raw).hexdigest()}/linli-01.png')
    monkeypatch.setattr(assets, '_catalog', lambda: {'stickers': {'linli-01': entry}})
    calls = []
    async def ticket(request):
        calls.append('ticket')
        return web.json_response({**entry, 'url': str(client.make_url('/r2'))})
    async def download(request):
        assert 'Authorization' not in request.headers
        calls.append('r2')
        return web.Response(status=403) if calls.count('r2') == 1 else web.Response(body=raw)
    app = web.Application()
    app.router.add_get('/v1/components/images/stickers/linli-01', ticket)
    app.router.add_get('/r2', download)
    client = TestClient(TestServer(app))
    await client.start_server()
    monkeypatch.setattr(assets, '_validate_download_url', lambda url, _: None)
    try:
        assert calls == []  # Import/catalog inspection never preloads images.
        paths = await asyncio.gather(*(assets.ensure_image(tmp_path, 'stickers', 'linli-01',
                    base_url=str(client.make_url(''))) for _ in range(5)))
        assert len(set(paths)) == 1 and paths[0].read_bytes() == raw
        assert calls == ['ticket', 'r2', 'ticket', 'r2']
        assert paths[0].is_relative_to(tmp_path / 'image-assets')
        # A new instance/server may be unavailable; durable cache still works.
        await client.close()
        assert await assets.ensure_image(tmp_path, 'stickers', 'linli-01', base_url='http://127.0.0.1:1') == paths[0]
        paths[0].write_bytes(b'corrupt')
        with pytest.raises(CloudError):
            await assets.ensure_image(tmp_path, 'stickers', 'linli-01', base_url='http://127.0.0.1:1')
        assert not list(tmp_path.rglob('*.tmp'))
    finally:
        await client.close()


@pytest.mark.parametrize('url', [
    'http://3fa206f49fd071a9eff9a1c9905208dd.r2.cloudflarestorage.com/vocal-backlog/key',
    'https://evil.example/vocal-backlog/key',
    'https://3fa206f49fd071a9eff9a1c9905208dd.r2.cloudflarestorage.com/vocal-backlog/other',
    'https://user@3fa206f49fd071a9eff9a1c9905208dd.r2.cloudflarestorage.com/vocal-backlog/key',
])
def test_r2_urls_are_pinned_to_exact_object(url):
    with pytest.raises(CloudError):
        assets._validate_download_url(url, {'key':'key'})


@_async_test
async def test_unknown_asset_cannot_read_paths_or_create_cache(tmp_path):
    for kind, asset_id in [('stickers','../../key'), ('wardrobe','mori-99'), ('../ui','wechat-payment')]:
        with pytest.raises(CloudError):
            await assets.ensure_image(tmp_path, kind, asset_id)
    assert list(tmp_path.iterdir()) == []


def test_full_catalog_keeps_every_sticker_and_approved_outfit():
    from runtime.wardrobe import DAILY_LOOKS
    from runtime.letter_stickers.selection import _files
    catalog = assets._catalog()
    assert set(catalog['stickers']) == set(_files())
    assert set(catalog['wardrobe']) == set().union(*DAILY_LOOKS.values())
    assert len(catalog['stickers']) == 272
    assert len(catalog['wardrobe']) == 47


@_async_test
async def test_default_line_art_is_offline_in_fresh_and_image_free_patch_installs(tmp_path, monkeypatch):
    import zipfile
    entry=assets._catalog()['stickers']['linli-01']
    raw=(Path('runtime/letter_stickers')/entry['filename']).read_bytes()
    def no_network(*args,**kwargs):
        raise AssertionError('default line art is bundled, not downloaded')
    monkeypatch.setattr(assets,'ClientSession',no_network)
    # A fresh installation uses its bundled line-art file without a ticket.
    saved=await assets.ensure_image(tmp_path/'fresh','stickers','linli-01')
    assert saved.read_bytes()==raw
    # Program patches lack images in the backend, but retain the native archive.
    root=tmp_path/'installed';data=root/'data';data.mkdir(parents=True)
    (root/'.olivia-full-patch.json').write_text(json.dumps({'client_version':'0.0.9.627'}))
    archive=root/'app/0.0.9.627/resources/feapp.dat';archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive,'w') as output:
        output.writestr('assets/letter-stickers/linli-01.png',raw)
    original=assets._matches
    monkeypatch.setattr(assets,'_matches',lambda path,record:False if path.parent==Path(assets.__file__).parent/'letter_stickers' else original(path,record))
    monkeypatch.setenv('OLIVIA_INSTALL_ROOT',str(root))
    saved=await assets.ensure_image(data,'stickers','linli-01')
    assert saved.read_bytes()==raw and saved.is_relative_to(data)


@_async_test
async def test_local_http_serves_saved_image_offline_and_rejects_foreign_origins(tmp_path, monkeypatch):
    import local_server
    entry=assets._catalog()['stickers']['linli-01']
    target=assets._cache_path(tmp_path,'stickers',entry)
    target.parent.mkdir(parents=True)
    raw=(Path('runtime/letter_stickers')/entry['filename']).read_bytes()
    target.write_bytes(raw)
    monkeypatch.setattr(local_server,'_local_data_root',lambda:tmp_path)
    def no_network(*args,**kwargs):
        raise AssertionError('verified local images must not access the network')
    monkeypatch.setattr(assets,'ClientSession',no_network)
    app=web.Application();app.router.add_route('*','/toy/images/{kind}/{asset_id}',local_server.handler)
    async with TestClient(TestServer(app)) as client:
        response=await client.get('/toy/images/stickers/linli-01')
        assert response.status==200 and response.headers['Content-Type']=='image/png'
        assert await response.read()==raw
        assert (await client.head('/toy/images/stickers/linli-01')).status==200
        assert (await client.post('/toy/images/stickers/linli-01')).status==405
        assert (await client.get('/toy/images/stickers/linli-01',headers={'Origin':'https://evil.example'})).status==403
        assert (await client.get('/toy/images/stickers/linli-999')).status==404


@_async_test
async def test_symlink_cache_cannot_write_outside_data(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    try:
        (data / 'image-assets').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink permission unavailable')
    with pytest.raises(CloudError):
        await assets.ensure_image(data, 'stickers', 'linli-01')
    assert list(outside.iterdir()) == []
