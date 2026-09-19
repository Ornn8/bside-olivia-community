import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

import video_capability_install as install


def make_archive(tmp_path, monkeypatch, *, corrupt=False, extra=False):
    tensor = b'synthetic trained weights'
    digest = hashlib.sha256(tensor).hexdigest()
    monkeypatch.setattr(install, '_LINLI_2250_SHA256', digest)
    config = {'schema_version': 1, 'artifact_type': 'breeze_lora_adapter',
              'base_model': {'id': 'BreezeBlue/Breeze-TTS-2', 'revision': 'c1c8ca18b70b30822735633991d9ebf4898e47d4'},
              'adapter': {'file': 'adapter.safetensors', 'sha256': digest},
              'lora': {'variant': 'backbone_depth_projection', 'rank': 8, 'alpha': 16.0, 'seed': 42}}
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, 'w') as archive:
        archive.writestr('adapter.safetensors', b'corrupted' if corrupt else tensor)
        archive.writestr('adapter_config.json', json.dumps(config))
        for name in ('base-model-assets.json', 'provenance.json', 'LICENSE', 'NOTICE'):
            archive.writestr(name, '{}')
        if extra:
            archive.writestr('../escape', 'bad')
    path = tmp_path / 'complete.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Olivia-breeze-2250-offline.zip', nested.getvalue())
    return path


def test_adapter_offline_restore_is_idempotent(tmp_path, monkeypatch):
    archive = make_archive(tmp_path, monkeypatch)
    data = tmp_path / 'data'
    selected = install.restore_combined_supplements(data, archive)
    assert selected == data / 'capabilities/video/shared/linli-2250'
    assert selected.is_dir()
    assert install.restore_combined_supplements(data, archive) == selected
    assert len(list(selected.iterdir())) == 6


@pytest.mark.parametrize(('corrupt', 'extra'), [(True, False), (False, True)])
def test_adapter_offline_rejects_invalid_before_publication(tmp_path, monkeypatch, corrupt, extra):
    archive = make_archive(tmp_path, monkeypatch, corrupt=corrupt, extra=extra)
    data = tmp_path / 'data'
    with pytest.raises(install.VideoCapabilityError, match='VIDEO_ADAPTER_SUPPLEMENT_INVALID'):
        install.restore_combined_supplements(data, archive)
    assert not (data / 'capabilities/video/shared/linli-2250').exists()
    assert not (tmp_path / 'escape').exists()


def test_legacy_offline_zip_remains_unchanged(tmp_path):
    path = tmp_path / 'old.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('old-model.bin', 'unchanged')
    assert install.restore_combined_supplements(tmp_path / 'data', path) is None
    assert not (tmp_path / 'data').exists()


def test_already_installed_models_still_select_new_adapter(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import threading
    archive = make_archive(tmp_path, monkeypatch)
    data = tmp_path / 'data'
    video = data / 'capabilities/video'
    config = video / 'generated/tts_local.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'settings': {'reference_text': 'synthetic reference', 'provider_options': {'seed': 200717}}}))
    fixture = SimpleNamespace(_lock=threading.RLock(), _threads={}, data_root=data,
                              install_root=video, start=lambda **kwargs: 'NOOP')
    assert install.VideoCapabilityInstaller.import_offline(fixture, bundle_id='ordinary_video', offline_root=archive) == 'APPLIED'
    settings = json.loads(config.read_text())['settings']
    assert settings['provider_options']['adapter_dir'] == str(video / 'shared/linli-2250')
    assert settings['provider_options']['seed'] == 200717
    assert settings['reference_text'] == 'synthetic reference'


def test_voice_only_upgrade_never_starts_model_downloads(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import threading
    archive = make_archive(tmp_path, monkeypatch)
    data = tmp_path / 'data'
    def forbidden(**kwargs):
        pytest.fail('voice upgrade must not start full model installation')
    fixture = SimpleNamespace(_lock=threading.RLock(), _threads={}, data_root=data,
        install_root=data / 'capabilities/video', start=forbidden)
    assert install.VideoCapabilityInstaller.import_offline(fixture, bundle_id='ordinary_video', offline_root=archive) == 'APPLIED'
    assert install.VideoCapabilityInstaller.import_offline(fixture, bundle_id='music_video', offline_root=archive) == 'NOOP'
    assert not (data / 'capabilities/video/.downloads').exists()
