import hashlib
import json
from types import SimpleNamespace

import pytest
from runtime.personal_chat import napcat_dependencies as deps, napcat_installer as installer


@pytest.mark.parametrize('corrupt', [False, True])
def test_verified_dependencies_repair_existing_install_without_touching_config(tmp_path, monkeypatch, corrupt):
    shell = tmp_path / 'shell'
    shell.mkdir()
    (shell / 'config.json').write_text('original')
    expected = {name: hashlib.sha256(name.encode()).hexdigest() for name in deps.DLL_HASHES}
    monkeypatch.setattr(deps, 'DLL_HASHES', expected)
    monkeypatch.setattr(deps, 'QQ_RESOURCE_OFFSET', 0)
    monkeypatch.setattr(deps, 'QQ_RESOURCE_SIZE', 3)
    monkeypatch.setattr(deps, 'QQ_SHA256', hashlib.sha256(b'qq!').hexdigest())
    monkeypatch.setattr(deps, 'EXTRACTOR_SHA256', hashlib.sha256(b'7z!').hexdigest())
    downloads = []
    def download(path, **kwargs):
        downloads.append(path.name)
        path.write_bytes(b'qq!' if path.name.startswith('qq') else b'7z!')
    monkeypatch.setattr(installer, '_download_archive', download)
    def extract(args, **kwargs):
        from pathlib import Path
        stage = Path(next(x[2:] for x in args if x.startswith('-o')))
        for name in expected:
            (stage / name).write_bytes(b'wrong' if corrupt else name.encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(deps.subprocess, 'run', extract)
    if corrupt:
        with pytest.raises(installer.NapCatSetupError, match='HASH_MISMATCH'):
            deps.ensure_dependencies(tmp_path, shell)
        assert not (shell / 'crypto.dll').exists()
    else:
        deps.ensure_dependencies(tmp_path, shell)
        deps.ensure_dependencies(tmp_path, shell)
        assert len(downloads) == 2
        assert (shell / 'crypto.dll').read_bytes() == b'crypto.dll'
    assert (shell / 'config.json').read_text() == 'original'


def test_existing_component_install_runs_dependency_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.os, 'name', 'nt')
    monkeypatch.setattr(installer, 'find_shell', lambda root: tmp_path)
    calls = []
    monkeypatch.setattr(deps, 'ensure_dependencies', lambda *args: calls.append(args))
    assert installer.install_component(tmp_path) == tmp_path
    assert calls == [(tmp_path, tmp_path)]


def test_occupied_webui_with_wrong_token_is_not_our_service(tmp_path, monkeypatch):
    config = installer._config_dir(tmp_path) / 'webui.json'
    config.write_text(json.dumps({'port': 6099, 'token': 'synthetic'}))
    monkeypatch.setattr(installer, '_tcp_port_open', lambda port: True)
    monkeypatch.setattr(installer, '_WEBUI_AUTH', {})
    monkeypatch.setattr(installer, 'prepare_onebot', lambda root: None)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'{"code":-1,"message":"token is invalid"}'
    monkeypatch.setattr(installer.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=lambda *a, **k: Response()))
    assert installer.webui_available(tmp_path) is False
    with pytest.raises(installer.NapCatSetupError, match='NAPCAT_PORT_IN_USE'):
        installer.ensure_shell(tmp_path)
