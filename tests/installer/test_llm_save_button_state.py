import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('script', ['llm_save_button_check.cjs', 'companion_status_check.cjs'])
def test_settings_interactions(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is not installed')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name(script)),
         str(root / 'original_client_settings_ui.py')],
        capture_output=True, text=True, encoding='utf-8', timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
