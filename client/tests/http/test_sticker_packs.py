import asyncio
import shutil
from pathlib import Path

import pytest

from runtime.letter_stickers import packs
from runtime import image_assets
from runtime.cloud_service import CloudError

SOURCE = Path(packs.__file__).with_name('linli-109.png')


def test_only_verified_files_in_the_pack_folder_count(tmp_path):
    folder = tmp_path / 'sticker-packs' / 'sketchbook'
    folder.mkdir(parents=True)
    assert packs.installed(tmp_path) == {}
    shutil.copy(SOURCE, folder / 'linli-109.png')
    shutil.copy(SOURCE, folder / 'linli-110.png')  # right name, wrong image
    (folder / 'notes.png').write_bytes(b'not a sticker')
    found = packs.installed(tmp_path)
    assert list(found) == ['linli-109']
    sketchbook = next(item for item in packs.status(tmp_path) if item['pack'] == 'sketchbook')
    assert sketchbook == {'pack': 'sketchbook', 'name': '手绘', 'total': 32, 'installed': 1}


def test_pack_sticker_is_served_from_the_folder_and_missing_one_is_not_downloaded(tmp_path):
    folder = tmp_path / 'sticker-packs'
    folder.mkdir()
    shutil.copy(SOURCE, folder / 'linli-109.png')
    path = asyncio.run(image_assets.ensure_image(tmp_path, 'stickers', 'linli-109', base_url='http://127.0.0.1:1'))
    assert path == folder / 'linli-109.png'
    with pytest.raises(CloudError, match='STICKER_PACK_NOT_INSTALLED'):
        asyncio.run(image_assets.ensure_image(tmp_path, 'stickers', 'linli-200', base_url='http://127.0.0.1:1'))


def test_sticker_pack_routes_answer_through_the_local_server(tmp_path, monkeypatch):
    import asyncio
    import subprocess

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path / 'data'))
    opened = []
    monkeypatch.setattr(subprocess, 'Popen', lambda args, **kwargs: opened.append(args))

    async def scenario():
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', local_server.handler)
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': 'https://olivia.local', 'X-Olivia-Companion-Action': 'confirmed'}
            listed = await client.get('/toy/sticker-packs', headers=headers)
            assert listed.status == 200
            data = (await listed.json())['data']
            assert data['folder'] == str(tmp_path / 'data' / 'sticker-packs')
            assert isinstance(data['packs'], list)
            assert (await client.post('/toy/sticker-packs/open', json={})).status == 403
            opened_folder = await client.post('/toy/sticker-packs/open', json={}, headers=headers)
            assert opened_folder.status == 200
            assert (tmp_path / 'data' / 'sticker-packs').is_dir()

    asyncio.run(scenario())
