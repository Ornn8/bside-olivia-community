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
