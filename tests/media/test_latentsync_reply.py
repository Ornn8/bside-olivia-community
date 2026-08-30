from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from runtime.media import latentsync_reply


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _fixture(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "latentsync"
    for item in ("scripts/inference.py", "configs/unet/stage2_efficient.yaml", "checkpoints/latentsync_unet.pt"):
        _write(root / item)
    return SimpleNamespace(
        root=root, python=_write(tmp_path / "python.exe"),
        source=_write(tmp_path / "source.mp4"), audio=_write(tmp_path / "speech.wav"),
        output=tmp_path / "reply.mp4", ffmpeg=_write(tmp_path / "ffmpeg/ffmpeg.exe"),
        cache=tmp_path / "cache",
    )


def _prepare(_source: Path, _audio: Path, target: Path, *, environment) -> None:
    assert environment["TEMP"]
    _write(target)


def _render(fixture: SimpleNamespace, **kwargs):
    return latentsync_reply.render_latentsync_video(
        fixture.source, fixture.audio, fixture.output,
        python_path=fixture.python, latentsync_root=fixture.root,
        ffmpeg_path=fixture.ffmpeg, provider_cache_root=fixture.cache, **kwargs,
    )


def test_published_output_survives_locked_temp_cleanup(tmp_path: Path, monkeypatch) -> None:
    fixture, observed = _fixture(tmp_path), {}

    class LockedTemp:
        def __init__(self, *, prefix, dir, ignore_cleanup_errors=False):
            observed["ignored"] = ignore_cleanup_errors
            self.path = Path(dir) / f"{prefix}locked"

        def __enter__(self):
            self.path.mkdir(parents=True)
            return str(self.path)

        def __exit__(self, *_args):
            if not observed["ignored"]:
                raise PermissionError("locked")

    def run(command, **_kwargs):
        _write(Path(command[command.index("--video_out_path") + 1]), b"video")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(latentsync_reply.tempfile, "TemporaryDirectory", LockedTemp)
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    assert _render(fixture)["visual_provider"] == "LatentSync-1.5"
    assert fixture.output.read_bytes() == b"video"
    assert observed["ignored"] is True


def test_timeout_terminates_windows_job_tree(monkeypatch) -> None:
    from runtime.media import managed_subprocess

    observed = {"timeouts": []}

    class Process:
        pid, returncode = 4242, None

        def communicate(self, timeout=None):
            observed["timeouts"].append(timeout)
            if len(observed["timeouts"]) == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout, stderr=b"busy")
            return b"", b"stopped"

    class Job:
        def assign(self, process): observed["assigned"] = process.pid
        def terminate(self): observed["terminated"] = True
        def close(self): observed["closed"] = True

    def popen(_command, **kwargs):
        observed["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", Job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", popen)
    with pytest.raises(subprocess.TimeoutExpired):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=12)
    flags = observed["kwargs"]["creationflags"]
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP and flags & subprocess.CREATE_NO_WINDOW
    assert (observed["assigned"], observed["terminated"], observed["closed"]) == (4242, True, True)
    assert observed["timeouts"] == [12, 15.0]


def test_windows_job_failure_does_not_launch(monkeypatch) -> None:
    from runtime.media import managed_subprocess

    launched = []
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: (_ for _ in ()).throw(OSError("job")))
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: launched.append(True))
    with pytest.raises(OSError):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)
    assert not launched


def test_windows_job_close_failure_is_reported(monkeypatch) -> None:
    from runtime.media import managed_subprocess

    job = SimpleNamespace(assign=lambda _p: None, terminate=lambda: None, close=lambda: (_ for _ in ()).throw(OSError("close")))
    process = SimpleNamespace(returncode=0, communicate=lambda timeout: (b"", b""))
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)
    with pytest.raises(OSError, match="close"):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_real_windows_job_terminates_worker() -> None:
    from runtime.media.managed_subprocess import run_managed_process

    with pytest.raises(subprocess.TimeoutExpired):
        run_managed_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.2)


@pytest.mark.parametrize(("configured", "expected"), [(None, 1800.0), ("999999", 3600.0)])
def test_render_uses_bounded_timeout(tmp_path: Path, monkeypatch, configured, expected) -> None:
    fixture, observed = _fixture(tmp_path), {}

    def run(command, *, timeout_seconds, **_kwargs):
        observed["timeout"] = timeout_seconds
        _write(Path(command[command.index("--video_out_path") + 1]), b"video")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    environment = {"PATH": ""}
    if configured is not None:
        environment["OLIVIA_LATENTSYNC_TIMEOUT_SECONDS"] = configured
    _render(fixture, environment=environment)
    assert observed["timeout"] == expected


def test_failure_persists_redacted_diagnostic(tmp_path: Path, monkeypatch) -> None:
    fixture, secret = _fixture(tmp_path), b"C:\\private api_key=secret letter=words CUDA out of memory"

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 23, b"", secret)

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as failure:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    assert str(failure.value) == "LATENTSYNC_FAILED"
    record = json.loads((data_root / "logs/media-provider.jsonl").read_text())
    assert record["diagnostic"] == failure.value.diagnostic
    assert record["provider"] == "latentsync" and record["error_code"] == "LATENTSYNC_FAILED"
    assert "returncode=23" in record["diagnostic"] and "cuda_out_of_memory" in record["diagnostic"]
    assert len(record["diagnostic"]) <= 240
    assert not any(value in json.dumps(record) for value in ("C:\\private", "secret", "words"))
