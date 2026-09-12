import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[2] / 'installer/bootstrap_install.py'


@pytest.mark.parametrize(('failure', 'code'), [
    ("PermissionError(13, 'private-path-secret')", 'SETUP_PATCH_PERMISSION_DENIED'),
    ("FileNotFoundError(2, 'private-path-secret')", 'SETUP_PATCH_FILE_MISSING'),
    ("OSError(28, 'private-path-secret')", 'SETUP_PATCH_DISK_FULL'),
    ("RuntimeError('private-path-secret')", 'SETUP_PATCH_UNEXPECTED_ERROR'),
    ("(lambda e: (setattr(e, 'winerror', 32), e)[1])(PermissionError(13, 'private-path-secret'))", 'SETUP_PATCH_FILE_IN_USE'),
])
def test_bootstrap_reports_safe_structured_failure(tmp_path, failure, code):
    package = tmp_path / 'installer'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / '__main__.py').write_text(f'def install():\n    raise {failure}\ninstall()\n', encoding='utf-8')
    result = subprocess.run([sys.executable, str(BOOTSTRAP), str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    record = json.loads(result.stdout)
    assert record['code'] == code
    assert record['diagnostic']['frames'][-1]['file'] == 'installer/__main__.py'
    assert 'private-path-secret' not in result.stdout + result.stderr
    assert str(tmp_path) not in result.stdout + result.stderr
    assert not result.stderr


def test_cli_install_result_survives_ascii_output_encoding(tmp_path):
    # Windows redirected stdout may use a code page without the user's characters.
    source = BOOTSTRAP.parent / '__main__.py'
    script = tmp_path / 'cli.py'
    script.write_text("import sys\nsys.path.insert(0," + repr(str(source.parents[1])) + ")\n"
                      "from installer import __main__ as cli\n"
                      "cli.install_full_patch=lambda *a,**k: {'status':'INSTALLED','path':'用户😀'}\n"
                      "raise SystemExit(cli.main(['install','--official-root','source','--destination','dest','--payload','payload']))\n", encoding='utf-8')
    result = subprocess.run([sys.executable, str(script)], env={**os.environ, 'PYTHONIOENCODING': 'ascii'},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'INSTALLED'


def test_storage_snapshot_is_projected_without_private_paths(tmp_path):
    package = tmp_path / 'installer'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / '__main__.py').write_text(
        "error = OSError(28, 'private-secret')\n"
        "error.install_storage = {'operation': 'copy_client', 'destination_free_bytes': 0, "
        "'payload_free_bytes': 2048, 'path': 'private-path'}\nraise error\n")
    result = subprocess.run([sys.executable, str(BOOTSTRAP), str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    record = json.loads(result.stdout)
    assert record['code'] == 'SETUP_PATCH_DISK_FULL'
    assert record['diagnostic']['storage_before_rollback'] == {
        'operation': 'copy_client', 'destination_free_bytes': 0, 'payload_free_bytes': 2048}
    assert 'private-' not in result.stdout + result.stderr


@pytest.mark.parametrize(('failure', 'code'), [
    ("PermissionError(13, 'private-path-secret')", 'SETUP_PATCH_PERMISSION_DENIED'),
    ("OSError(28, 'private-path-secret')", 'SETUP_PATCH_DISK_FULL'),
    ("OSError(__import__('errno').ENAMETOOLONG, 'private-path-secret')", 'SETUP_PATCH_PATH_TOO_LONG'),
    ("FileNotFoundError(2, 'private-path-secret')", 'SETUP_PATCH_FILE_MISSING'),
    ("OSError('[WinError 32] private-path-secret')", 'SETUP_PATCH_FILE_IN_USE'),
    ("OSError('private-path-secret')", 'SETUP_PATCH_COPY_FAILED'),
])
def test_real_copytree_aggregate_reports_safe_underlying_code(tmp_path, failure, code):
    package = tmp_path / 'installer'
    package.mkdir()
    (package / '__init__.py').write_text('')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'private-path-secret.txt').write_text('fixture')
    (package / '__main__.py').write_text(
        'import shutil\n'
        f'def fail(*args, **kwargs):\n    raise {failure}\n'
        f'shutil.copytree({str(source)!r}, {str(tmp_path / "target")!r}, copy_function=fail)\n',
        encoding='utf-8')
    result = subprocess.run([sys.executable, str(BOOTSTRAP), str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    record = json.loads(result.stdout)
    assert record['code'] == code
    assert record['diagnostic']['error_type'] == 'Error'
    assert record['diagnostic']['operation'] == 'copy_tree'
    assert len(record['diagnostic']['copy_errors']) == 1
    assert 'private-path-secret' not in result.stdout + result.stderr
    assert str(tmp_path) not in result.stdout + result.stderr
    assert not result.stderr
