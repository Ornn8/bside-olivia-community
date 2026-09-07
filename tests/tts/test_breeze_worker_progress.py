import sys
from pathlib import Path

import pytest

from tts.delivery import DeliveryAudioError, _run_breeze_worker
from tts.external_breeze_worker import _write_status


def test_status_reader_sharing_violation_does_not_abort_audio(tmp_path, monkeypatch):
    import json
    import os
    status = tmp_path / "status.json"
    _write_status(status, {"generated_frames": 1})
    def locked(*args):
        raise PermissionError("Windows reader has the destination open")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", locked)
        _write_status(status, {"generated_frames": 2})
    assert json.loads(status.read_text())["generated_frames"] == 1
    _write_status(status, {"generated_frames": 3})
    assert json.loads(status.read_text())["generated_frames"] == 3
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("moving", [False, True])
def test_worker_stall_is_bounded_but_progress_can_exceed_idle_limit(tmp_path: Path, moving):
    status = tmp_path / "status.json"
    code = (
        "import json,sys,time; from pathlib import Path; "
        "p=Path(sys.argv[2]); "
        "p.write_text(json.dumps({'phase':'generation','generated_frames':0}));\n"
        "for i in range(12):\n"
        " time.sleep(0.1)\n"
        + (" p.write_text(json.dumps({'phase':'generation','generated_frames':i+1}))\n" if moving else "")
    )
    command = [sys.executable, "-c", code, "--status", str(status)]
    if moving:
        assert _run_breeze_worker(command, timeout=5, progress_timeout=0.5).returncode == 0
    else:
        with pytest.raises(DeliveryAudioError, match="TTS_GENERATION_STALLED"):
            _run_breeze_worker(command, timeout=5, progress_timeout=0.5)


def test_worker_nonzero_exit_is_preserved(tmp_path: Path):
    result = _run_breeze_worker(
        [sys.executable, "-c", "raise SystemExit(2)", "--status", str(tmp_path / "status.json")],
        timeout=5,
    )
    assert result.returncode == 2
