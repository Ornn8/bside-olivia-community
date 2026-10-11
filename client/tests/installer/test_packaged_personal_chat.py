"""Exercise the real chat backend from copied runtime files in a fresh process."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from installer.full_patch import copy_project_payload


ROOT = Path(__file__).resolve().parents[2]
PROBE = """
import importlib.util
from pathlib import Path
import sys
import pytest

payload, test_file, scratch, channel, variant = sys.argv[1:]
sys.path.insert(0, payload)
sys.path.insert(1, str(Path(test_file).parents[2]))  # Synthetic provider helpers only.
spec = importlib.util.spec_from_file_location('chat_acceptance', test_file)
case = importlib.util.module_from_spec(spec)
spec.loader.exec_module(case)
from runtime.personal_chat import backend
assert Path(backend.__file__).is_relative_to(Path(payload))
with pytest.MonkeyPatch.context() as patches:
    case.test_backend_generate_uses_real_pipeline_persona_memory_and_world(
        Path(scratch), patches, None, variant,
        (channel, '早上好', '早上好，今天慢慢开始。'))
"""


@pytest.mark.parametrize('channel,variant', [
    ('qq', 'plain'), ('wechat', 'plain'), ('qq', 'native_media'),
])
def test_copied_payload_generates_and_delivers_chat(tmp_path, channel, variant):
    payload = tmp_path / 'payload'
    copy_project_payload(ROOT, payload, include_base_stickers=False)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('OLIVIA_', 'COMPANION_'))}
    result = subprocess.run(
        [sys.executable, '-I', '-c', PROBE, str(payload),
         str(ROOT / 'tests/http/test_personal_chat_backend.py'), str(tmp_path),
         channel, variant],
        cwd=payload, env=environment, capture_output=True, text=True,
        encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
