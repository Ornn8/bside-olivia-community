from pathlib import Path

from original_client_update_api import running_component_version


def test_version_http_is_read_only_and_does_not_expose_paths():
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_update_api import mount_original_client_update_api

    async def check():
        app = web.Application()
        mount_original_client_update_api(app, object(), trusted_origins=['https://olivia.local'], authorize_session=lambda token: None)
        async with TestClient(TestServer(app)) as client:
            result = await client.get('/toy/updates/local/status', headers={'Origin': 'https://olivia.local'})
            assert result.status == 200
            assert await result.json() == {'status': 'READY', 'version': None, 'manifest_sha256': None}
            denied = await client.get('/toy/updates/local/status', headers={'Origin': 'https://untrusted.example'})
            assert denied.status == 403
    asyncio.run(check())


def test_running_version_comes_from_loaded_payload_not_selected_update():
    digest = 'a' * 64
    loaded = Path('install/versions/local_backend') / ('0.1.437-' + digest)
    assert running_component_version(loaded) == {
        'version': '0.1.437', 'manifest_sha256': digest,
    }


def test_unversioned_backend_does_not_invent_a_release_number():
    assert running_component_version(Path('install/local_backend')) == {
        'version': None, 'manifest_sha256': None,
    }
