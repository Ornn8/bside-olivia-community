import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import local_server
from runtime.media.local_song_library import LocalSongLibrary


def test_local_library_lifecycle_and_range_serving(tmp_path, monkeypatch):
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setattr(LocalSongLibrary, '_prepare', lambda self, source, output: (output.write_bytes(source.read_bytes()), 10)[1])
    source = tmp_path / 'song.mp4'
    source.write_bytes(b'0123456789')
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
