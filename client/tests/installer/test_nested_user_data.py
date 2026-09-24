import json
import os
from pathlib import Path
import subprocess

import pytest

from installer.user_data_root import resolve_user_data_root


def seed(path, letters=0, chats=0):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'state.json').write_text(json.dumps({'letters': [{}] * letters, 'personal_chats': [{}] * chats}))


def test_nested_install_reads_original_state_and_credentials_without_writes(tmp_path):
    original = tmp_path / 'install/data'
    nested = tmp_path / 'install/install'
    seed(original, 231, 196)
    seed(nested / 'data')
    (original / 'key.dpapi').write_bytes(b'opaque-original')
    (nested / 'data/key.dpapi').write_bytes(b'opaque-new')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert resolve_user_data_root(nested) == original
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_nested_install_never_hides_new_conversation(tmp_path):
    nested = tmp_path / 'install/install'
    seed(nested.parent / 'data', 231)
    seed(nested / 'data', 1)
    with pytest.raises(ValueError, match='TWO_HISTORIES'):
        resolve_user_data_root(nested)


def test_normal_install_uses_its_own_data(tmp_path):
    root = tmp_path / 'install'
    seed(root / 'data', 5)
    assert resolve_user_data_root(root) == root / 'data'


@pytest.mark.skipif(os.name != 'nt', reason='Windows mutex')
def test_nested_login_worker_shares_original_data_lease(tmp_path):
    from installer import proactive_login

    original = tmp_path / 'install'
    nested = original / 'install'
    seed(original / 'data', 1)
    seed(nested / 'data')
    settings = original / 'data/proactive/settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({'enabled': True, 'login_check_enabled': True}))
    lease = proactive_login._try_acquire_instance(original)
    assert lease is not None
    calls = []
    try:
        proactive_login.run(nested, scan=lambda data, **kw: calls.append(data),
                            sleep=lambda _: settings.write_text('{}'))
        assert calls == []
    finally:
        lease.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows installer')
def test_powershell_normalizes_custom_existing_install_not_arbitrary_named_folder(tmp_path):
    script = (Path(__file__).parents[2] / 'installer/Install.ps1').read_text(encoding='utf-8-sig')
    function = script.split('function Resolve-ProductRoot {', 1)[1].split('$productRoot = Resolve-ProductRoot', 1)[0]
    seed(tmp_path / 'custom/install/data', 3)
    selected = tmp_path / 'custom/install'
    probe = tmp_path / 'probe.ps1'
    probe.write_text('function Resolve-ProductRoot {' + function + '\nResolve-ProductRoot $args[0]', encoding='utf-8-sig')
    def resolve(path):
        return subprocess.check_output(['powershell', '-NoProfile', '-File', str(probe), str(path)], text=True).strip()
    assert Path(resolve(selected)) == selected.parent
    assert Path(resolve(tmp_path / 'fresh/install')) == tmp_path / 'fresh/install'
