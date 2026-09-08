import sys
import json
import subprocess
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


def test_invalid_request_worker_exit_retains_safe_failure_status(tmp_path):
    from tts import external_breeze_worker as worker
    request = tmp_path / "request.json"
    request.write_text('{"private letter sk-secret":', encoding="utf-8")
    status = tmp_path / "worker-status.json"
    result = subprocess.run([sys.executable, worker.__file__, "--request", str(request),
        "--output", str(tmp_path / "speech.wav"), "--status", str(status)],
        capture_output=True, timeout=10)
    assert result.returncode == 2
    assert json.loads(status.read_text()) == {"status": "failed", "phase": "request",
        "error_type": "JSONDecodeError", "error_code": "BREEZE_REQUEST_INVALID"}
    assert b"sk-secret" not in status.read_bytes() + result.stdout + result.stderr


@pytest.mark.parametrize("phase", ["package_load", "model_load"])
def test_synthesis_failure_records_exact_phase_without_exception_message(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace
    from tts import external_breeze_worker as worker
    def missing(*args, **kwargs):
        raise ModuleNotFoundError("private module C:/Users/private sk-secret")
    runtime = SimpleNamespace(decode_codes=lambda *args: None)
    loader = SimpleNamespace(HYBRID_LABEL="hybrid", load_breeze_bundle=missing)
    monkeypatch.setattr(worker, "_load_package", missing if phase == "package_load" else
        lambda *args: (loader, SimpleNamespace(), runtime))
    status = tmp_path / "worker-status.json"
    with pytest.raises(ModuleNotFoundError):
        worker._synthesize({"runtime_root": "private", "model_dir": "private"},
            tmp_path / "speech.wav", status)
    assert json.loads(status.read_text()) == {"status": "failed", "phase": phase,
        "error_type": "ModuleNotFoundError", "error_code": "BREEZE_MODULE_MISSING"}
    assert "private" not in status.read_text()


@pytest.mark.parametrize("error,code", [
    (RuntimeError("CUDA out of memory private text"), "BREEZE_CUDA_OUT_OF_MEMORY"),
    (ImportError("DLL load failed private path"), "BREEZE_IMPORT_FAILED"),
    (PermissionError("private path"), "BREEZE_PERMISSION_DENIED"),
    (FileNotFoundError("private path"), "BREEZE_FILE_MISSING"),
    (OSError(28, "private path"), "BREEZE_DISK_FULL"),
])
def test_worker_failure_classification_does_not_persist_private_details(tmp_path, error, code):
    from tts.external_breeze_worker import _write_failure
    status = tmp_path / "status.json"
    _write_failure(status, "reference_read", error)
    assert json.loads(status.read_text())["error_code"] == code
    assert "private" not in status.read_text()
