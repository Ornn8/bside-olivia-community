import json
import zipfile
from pathlib import Path

import pytest

from runtime.media.component_packages import MediaComponents, requirements_for


def package(tmp_path, component='tools'):
    from video_capability_install import write_runtime_root_manifest
    root = tmp_path / 'source'
    root.mkdir(exist_ok=True)
    (root / 'ffmpeg.exe').write_bytes(b'fixture')
    write_runtime_root_manifest(root, version=f'component.{component}.1',
                                environment={'OLIVIA_FFMPEG_EXE': 'ffmpeg.exe'})
    archive = tmp_path / 'component.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for file in root.iterdir():
            z.write(file, file.name)
    return archive


def test_scoped_import_survives_relocation_and_reuses_completed_package(tmp_path):
    manager = MediaComponents(tmp_path / 'data')
    archive = package(tmp_path)
    assert manager.install(archive, expected_component='tools') == 'APPLIED'
    assert manager.install(archive, expected_component='tools') == 'NOOP'
    moved = tmp_path / 'moved'
    (tmp_path / 'data').rename(moved)
    environment = MediaComponents(moved).environment()
    assert Path(environment['OLIVIA_FFMPEG_EXE']).read_bytes() == b'fixture'
    assert str(moved) in environment['OLIVIA_FFMPEG_EXE']


def test_wrong_component_cannot_override_another_component(tmp_path):
    manager = MediaComponents(tmp_path / 'data')
    with pytest.raises(ValueError):
        manager.install(package(tmp_path, 'cover'), expected_component='cover')
    assert manager.environment() == {}


def test_reimport_repairs_corrupt_installed_file(tmp_path):
    manager = MediaComponents(tmp_path / 'data')
    archive = package(tmp_path)
    manager.install(archive, expected_component='tools')
    Path(manager.environment()['OLIVIA_FFMPEG_EXE']).write_bytes(b'corrupt')
    assert manager.install(archive, expected_component='tools') == 'APPLIED'
    assert Path(manager.environment()['OLIVIA_FFMPEG_EXE']).read_bytes() == b'fixture'


def test_corruption_does_not_replace_active_component(tmp_path):
    manager = MediaComponents(tmp_path / 'data')
    archive = package(tmp_path)
    manager.install(archive, expected_component='tools')
    with zipfile.ZipFile(archive, 'a') as z:
        z.writestr('unexpected.txt', 'bad')
    with pytest.raises(ValueError):
        manager.install(archive, expected_component='tools')
    assert Path(manager.environment()['OLIVIA_FFMPEG_EXE']).read_bytes() == b'fixture'


def test_modes_only_require_relevant_components():
    assert requirements_for('voice_reply', False) == ['tools', 'voice']
    assert requirements_for('singing_video', False) == ['tools', 'cover']
    assert requirements_for('singing_video', True) == ['tools', 'cover', 'lipsync', 'scenes', 'separator']
    assert 'transcription' not in requirements_for('voice_song_video', True)


def test_component_status_reuses_legacy_files_without_claiming_inference(tmp_path):
    tool = tmp_path / 'ffmpeg.exe'
    tool.write_bytes(b'legacy')
    status = MediaComponents(tmp_path / 'data').status({'OLIVIA_FFMPEG_EXE': str(tool)})
    row = next(x for x in status['items'] if x['id'] == 'tools')
    assert row['state'] == 'installed'
    assert row['source'] == 'existing'
    assert status['verification'] == 'files_only'


def test_http_component_import_is_confirmed_and_scoped(tmp_path):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_video_capability_api import mount_original_client_video_capability_api
    archive = package(tmp_path)
    seen = []
    class Fake:
        def start(self, path, component):
            seen.append((path, component))
            return 'APPLIED'
        def status(self): return {'capability': 'video'}
        def start_many(self, archives, expected_components):
            seen.append((archives, expected_components))
            return 'APPLIED'
    installer = Fake()
    installer.media_components = installer
    async def run():
        app = web.Application()
        mount_original_client_video_capability_api(app, installer, trusted_origins=(),
            authorize_session=lambda _: None, select_offline_archive=lambda: archive,
            select_component_archives=lambda: [archive])
        headers = {'Origin': 'http://localhost:3000', 'X-Olivia-Capability-Action': 'confirmed', 'X-Olivia-Setup-Session': 'session'}
        async with TestClient(TestServer(app)) as client:
            denied = await client.post('/toy/capabilities/video/action', json={'action': 'import_component', 'component_id': 'tools'})
            assert denied.status == 403
            invalid = await client.post('/toy/capabilities/video/action', json={'action': 'import_component', 'component_id': []}, headers=headers)
            assert invalid.status == 400
            good = await client.post('/toy/capabilities/video/action', json={'action': 'import_component', 'component_id': 'tools'}, headers=headers)
            assert good.status == 200
            batch = await client.post('/toy/capabilities/video/action', json={'action': 'import_components', 'component_ids': ['tools']}, headers=headers)
            assert batch.status == 200
    asyncio.run(run())
    assert seen == [(archive, 'tools'), ([archive], ['tools'])]


def test_batch_continues_after_one_package_fails(tmp_path, monkeypatch):
    import runtime.media.component_packages as module
    manager = MediaComponents(tmp_path / 'data')
    monkeypatch.setattr(module, 'archive_component', lambda path: path.stem)
    seen = []
    def install(path, *, expected_component):
        seen.append(expected_component)
        if expected_component == 'voice':
            raise ValueError('MEDIA_COMPONENT_RUNTIME_NOT_PORTABLE')
        return 'APPLIED'
    monkeypatch.setattr(manager, 'install', install)
    assert manager.start_many([Path('voice.zip'), Path('tools.zip')], ['voice', 'tools']) == 'APPLIED'
    manager._thread.join(2)
    assert seen == ['voice', 'tools']
    assert manager.progress['failed_components'] == ['voice']
    assert manager.progress['completed_count'] == 2
    assert manager.progress['state'] == 'failed'


def test_batch_rejects_duplicate_components_before_install(tmp_path, monkeypatch):
    import runtime.media.component_packages as module
    monkeypatch.setattr(module, 'archive_component', lambda path: 'tools')
    manager = MediaComponents(tmp_path / 'data')
    with pytest.raises(ValueError):
        manager.start_many([Path('first.zip'), Path('second.zip')])
    assert manager._thread is None
