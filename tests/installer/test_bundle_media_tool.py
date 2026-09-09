import hashlib
import zipfile
from pathlib import Path

import pytest
from installer import bundle_media_tool as bundle
from runtime.media import latentsync_reply


def test_bundle_rejects_unverified_wheel_before_writing(tmp_path):
    wheel = tmp_path / 'bad.whl'
    wheel.write_bytes(b'bad')
    with pytest.raises(RuntimeError, match='HASH_MISMATCH'):
        bundle.bundle_media_tool(tmp_path / 'payload', wheel=wheel)
    assert not (tmp_path / 'payload').exists()


def test_bundled_tool_resolves_without_path_or_python_package(tmp_path, monkeypatch):
    wheel = tmp_path / 'fixture.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('imageio_ffmpeg/binaries/ffmpeg-win.exe', b'fixture')
        archive.writestr('imageio_ffmpeg.dist-info/LICENSE', 'license')
    monkeypatch.setattr(bundle, 'WHEEL_SHA256', hashlib.sha256(wheel.read_bytes()).hexdigest())
    payload = tmp_path / 'payload'
    bundle.bundle_media_tool(payload, wheel=wheel)
    monkeypatch.setattr(latentsync_reply, '__file__', str(payload / 'runtime/media/latentsync_reply.py'))
    monkeypatch.setattr(latentsync_reply.shutil, 'which', lambda *a, **k: pytest.fail('must use packaged tool'))
    assert latentsync_reply.resolve_ffmpeg_executable({}) == payload / 'media-tools/ffmpeg.exe'
    assert (payload / 'media-tools/LICENSE-0.txt').read_text() == 'license'


def test_new_install_lock_and_offline_allowlist_include_same_wheel():
    root = Path(__file__).resolve().parents[2]
    assert 'imageio-ffmpeg==0.6.0 --hash=sha256:' + bundle.WHEEL_SHA256 in (root / 'installer/runtime-requirements.txt').read_text()
    assert "'wheelhouse/imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl', '" + bundle.WHEEL_SHA256 in (root / 'installer/Install.ps1').read_text(encoding='utf-8')
