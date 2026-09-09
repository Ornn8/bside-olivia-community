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
            assert await result.json() == {'status': 'READY', **running_component_version()}
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


def test_fresh_install_reports_its_bundled_release_version(tmp_path):
    root = tmp_path / 'install' / 'local_backend'
    (root / 'installer').mkdir(parents=True)
    (root / 'installer' / 'release-version.json').write_text('{"version":"1.1.0"}', encoding='utf-8')
    assert running_component_version(root) == {'version': '1.1.0', 'manifest_sha256': None}


def test_invalid_bundled_version_does_not_break_status(tmp_path):
    (tmp_path / 'installer').mkdir()
    marker = tmp_path / 'installer' / 'release-version.json'
    for content in ('broken', '[]', '{"version":"bad/version"}'):
        marker.write_text(content, encoding='utf-8')
        assert running_component_version(tmp_path) == {'version': None, 'manifest_sha256': None}
