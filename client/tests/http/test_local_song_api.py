import asyncio
import os

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import local_server
from runtime.media.local_song_library import LocalSongLibrary


def test_local_library_lifecycle_and_range_serving(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setattr(LocalSongLibrary, '_prepare', lambda self, source, output: (output.write_bytes(source.read_bytes()), 10)[1])
    source = tmp_path / 'song.mp4'
    source.write_bytes(b'0123456789')
    opened = []
    monkeypatch.setattr('runtime.media.local_song_library.subprocess.Popen',
                        lambda args, **kwargs: opened.append((args, kwargs)))
    async def scenario():
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', local_server.handler)
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': 'https://olivia.local', 'X-Olivia-Companion-Action': 'confirmed'}
            denied = await client.post('/toy/local-songs/import', json={'path': str(source)})
            assert denied.status == 403
            denied = await client.post('/toy/local-songs/import', json={'path': str(source)}, headers=dict(headers, Origin='https://evil.example'))
            assert denied.status == 403
            imported = await client.post('/toy/local-songs/import', json={'path': str(source)}, headers=headers)
            assert imported.status == 200
            assert (await imported.json())['data']['added'] == 1
            songs = await (await client.get('/toy/local-songs', headers=headers)).json()
            song = songs['data']['songs'][0]
            if os.name == 'nt':
                denied = await client.post('/toy/local-songs/reveal', json={'id': song['id']})
                assert denied.status == 403
                assert not opened
                revealed = await client.post('/toy/local-songs/reveal', json={'id': song['id']}, headers=headers)
                assert revealed.status == 200
                assert opened[0][0][1] == '/select,'
                assert opened[0][0][2] == str(LocalSongLibrary(tmp_path / 'data', {}).media_path(song['id']))
                assert opened[0][1]['shell'] is False
                invalid = await client.post('/toy/local-songs/reveal', json={'id': '../outside'}, headers=headers)
                assert invalid.status == 404
                assert len(opened) == 1
            media = '/toy/local-songs/media/' + song['id'] + '.mp4'
            response = await client.get(media, headers={'Range': 'bytes=2-5'})
            assert response.status == 206
            assert await response.read() == b'2345'
            renamed = await client.post('/toy/local-songs/rename', json={'id': song['id'], 'name': '晚安'}, headers=headers)
            assert (await renamed.json())['data']['songs'][0]['name'] == '晚安'
            deleted = await client.post('/toy/local-songs/delete', json={'id': song['id']}, headers=headers)
            assert deleted.status == 200
            assert (await client.get(media)).status == 404
            assert source.read_bytes() == b'0123456789'
    asyncio.run(scenario())
def test_local_song_catalog_exposes_bounded_native_id(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setattr(LocalSongLibrary, '_prepare', lambda self, source, output: (output.write_bytes(source.read_bytes()), 10)[1])
    source = tmp_path / 'song.mp4'
    source.write_bytes(b'0123456789')

    async def scenario():
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', local_server.handler)
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': 'https://olivia.local', 'X-Olivia-Companion-Action': 'confirmed'}
            imported = await client.post('/toy/local-songs/import', json={'path': str(source)}, headers=headers)
            assert imported.status == 200
            songs = await (await client.get('/toy/local-songs', headers=headers)).json()
            song = songs['data']['songs'][0]
            assert 'native_id' in song
            nid = int(song['native_id'])
            assert 1000000000 <= nid < 2000000000

    asyncio.run(scenario())
