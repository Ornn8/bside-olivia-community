import importlib
from pathlib import Path
import sys
import threading

import pytest

from mem0_capability_install import ManagedMem0Runtime


@pytest.mark.parametrize('same_python,verification', [(True, 'ok'), (False, 'ok'),
    (True, 'fail_staging'), (True, 'fail_final')])
def test_verified_runtime_becomes_importable_without_restart(tmp_path, monkeypatch, same_python, verification):
    root = tmp_path / 'install'
    executable = root / 'runtime/python/python.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic')
    (executable.parent / 'python312._pth').write_text('python312.zip\nimport site\n')
    requirements = root / 'local_backend/installer/requirements.txt'
    requirements.parent.mkdir(parents=True)
    requirements.write_text('synthetic==1\n')
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(sys, 'executable', str(executable if same_python else tmp_path / 'other/python.exe'))
    module_name = '_olivia_verified_hotload_fixture'
    def runner(command, **kw):
        target = Path(command[command.index('--target') + 1])
        (target / (module_name + '.py')).write_text('VALUE = 42\n')
        return 0
    layer = ManagedMem0Runtime(install_root=root, python_executable=executable,
        requirements=requirements, sources=('https://mirror.invalid', 'https://official.invalid'),
        download_bytes=1, verifier=lambda runtime, req: verification == 'ok' or (
            verification == 'fail_final' and runtime.name.endswith('.staging')), runner=runner)
    before = list(sys.path)
    try:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)
        if verification != 'ok':
            with pytest.raises(RuntimeError, match='MEM0_RUNTIME_VERIFY_FAILED'):
                layer.install(source_mode='official', offline_root=None,
                    pause_requested=threading.Event(), progress=lambda *args: None)
        else:
            layer.install(source_mode='official', offline_root=None,
                pause_requested=threading.Event(), progress=lambda *args: None)
        if same_python and verification == 'ok':
            assert importlib.import_module(module_name).VALUE == 42
            paths = list(sys.path)
            layer.install(source_mode='official', offline_root=None,
                pause_requested=threading.Event(), progress=lambda *args: None)
            assert sys.path == paths
        else:
            assert sys.path == before
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)
