from __future__ import annotations

import json
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

def _prepare(_source: Path, _audio: Path, target: Path, *, environment, deadline) -> None:
    assert environment["TEMP"] and deadline > 0
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
            self.path.mkdir(parents=True, exist_ok=True)
            return str(self.path)
        def __exit__(self, *_args):
            if not observed["ignored"]:
                raise PermissionError("locked")
    def run(command, **_kwargs):
        if "--video_out_path" in command:
            _write(Path(command[command.index("--video_out_path") + 1]), b"video")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        observed["validation"] = command
        return SimpleNamespace(returncode=0, stdout=b"frame=25\nout_time_us=1000000\nprogress=end\n", stderr=b"")
    monkeypatch.setattr(latentsync_reply.tempfile, "TemporaryDirectory", LockedTemp)
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    assert _render(fixture)["visual_provider"] == "LatentSync-1.5"
    assert fixture.output.read_bytes() == b"video" and observed["ignored"] is True
    assert all(flag in observed["validation"] for flag in ("-xerror", "-f", "-progress"))
    def fail(error):
        return lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    monkeypatch.setattr(latentsync_reply.shutil, "copy2", fail(FileNotFoundError("copy failed")))
    monkeypatch.setattr(Path, "unlink", fail(PermissionError("locked")))
    with pytest.raises(FileNotFoundError, match="copy failed"):
        _render(fixture)

@pytest.mark.parametrize("failure", ("corrupt", "missing_audio"))
def test_invalid_output_is_not_published(tmp_path: Path, monkeypatch, failure) -> None:
    fixture = _fixture(tmp_path)
    def run(command, **_kwargs):
        if "--video_out_path" in command:
            _write(Path(command[command.index("--video_out_path") + 1]), failure.encode())
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if failure == "missing_audio":
            return subprocess.CompletedProcess(command, int("0:a:0" in command), b"frame=25\nout_time_us=1000000\nprogress=end\n", b"missing audio")
        return subprocess.CompletedProcess(command, 0, b"frame=0\nout_time_us=0\nprogress=end\n", b"")
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    assert str(caught.value) == "LATENTSYNC_FAILED" and not fixture.output.exists()

@pytest.mark.parametrize(("configured", "expected"), [(None, 1800.0), ("999999", 3600.0)])
def test_render_uses_bounded_timeout(tmp_path: Path, monkeypatch, configured, expected) -> None:
    fixture, observed = _fixture(tmp_path), {}
    def run(command, *, deadline, **_kwargs):
        observed["timeout"] = deadline
        if "--video_out_path" in command:
            _write(Path(command[command.index("--video_out_path") + 1]), b"video")
        return subprocess.CompletedProcess(command, 0, b"frame=25\nout_time_us=1000000\nprogress=end\n", b"")
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    monkeypatch.setattr(latentsync_reply.time, "monotonic", lambda: 0.0)
    environment = {"PATH": ""}
    if configured is not None:
        environment["OLIVIA_LATENTSYNC_TIMEOUT_SECONDS"] = configured
    _render(fixture, environment=environment)
    assert observed["timeout"] == expected

def test_prepare_worker_validation_share_one_absolute_deadline(tmp_path: Path, monkeypatch) -> None:
    fixture, observed = _fixture(tmp_path), {}
    def prepare(_source, _audio, target, *, environment, deadline):
        observed["prepare"] = deadline
        _write(target)
    def run(command, *, deadline, **_kwargs):
        phase = "worker" if "--video_out_path" in command else "validation"
        observed[phase] = deadline
        if phase == "worker":
            _write(Path(command[command.index("--video_out_path") + 1]), b"video")
        return subprocess.CompletedProcess(command, 0, b"frame=25\nout_time_us=1000000\nprogress=end\n", b"")
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    monkeypatch.setattr(latentsync_reply.time, "monotonic", lambda: 0.0)
    _render(fixture)
    assert observed == {"prepare": 1800.0, "worker": 1800.0, "validation": 1800.0}

def test_exhausted_total_deadline_is_redacted(tmp_path: Path, monkeypatch) -> None:
    fixture, clock = _fixture(tmp_path), [0.0]
    def prepare(_source, _audio, target, *, environment, deadline):
        _write(target)
        clock[0] = deadline
    def expired(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 0, stderr=b"private")
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", expired)
    monkeypatch.setattr(latentsync_reply.time, "monotonic", lambda: clock[0])
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    assert str(caught.value) == "LATENTSYNC_FAILED"
    assert caught.value.diagnostic == "returncode=unknown;stderr_category=process_timeout"

def test_prepare_nonzero_uses_stable_reported_failure(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", lambda command, **_kwargs: subprocess.CompletedProcess(command, 19, b"", b"ffmpeg failed"))
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    record = json.loads((data_root / "logs/media-provider.jsonl").read_text())
    assert str(caught.value) == "LATENTSYNC_FAILED" and "returncode=19" in record["diagnostic"]
    assert "stderr_category=external_process_failure" in record["diagnostic"]

def test_failure_persists_redacted_diagnostic(tmp_path: Path, monkeypatch) -> None:
    fixture, secret = _fixture(tmp_path), b"C:\\private api_key=secret letter=words CUDA out of memory"
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", lambda command, **_kwargs: subprocess.CompletedProcess(command, 23, b"", secret))
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as failure:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    record = json.loads((data_root / "logs/media-provider.jsonl").read_text())
    assert str(failure.value) == "LATENTSYNC_FAILED" and record["diagnostic"] == failure.value.diagnostic
    assert record["provider"] == "latentsync" and record["error_code"] == "LATENTSYNC_FAILED"
    assert record["diagnostic"] == "returncode=23;stderr_category=cuda_out_of_memory"
    assert not any(value in json.dumps(record) for value in ("C:\\private", "secret", "words"))

@pytest.mark.parametrize(("failure", "category"), ((subprocess.TimeoutExpired(["ffmpeg"], 1, stderr=b"busy"), "process_timeout"), (OSError("PROCESS_TREE_TERMINATION_FAILED"), "process_management_failure")))
def test_prepare_process_failure_is_reported(tmp_path: Path, monkeypatch, failure: Exception, category: str) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", lambda *_a, **_k: (_ for _ in ()).throw(failure))
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    record = json.loads((data_root / "logs/media-provider.jsonl").read_text())
    assert str(caught.value) == "LATENTSYNC_FAILED"
    assert f"stderr_category={category}" in record["diagnostic"]


@pytest.mark.parametrize("known", [True, False])
def test_missing_job_audio_is_reported_without_paths(tmp_path, monkeypatch, known):
    fixture = _fixture(tmp_path / "private folder")
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    def fail(command, **kwargs):
        fixture.audio.unlink()
        missing = str(fixture.audio) if known else "C:/private folder/secret letter.wav"
        return subprocess.CompletedProcess(command, 1, b"", f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'")
    monkeypatch.setattr(latentsync_reply, "run_managed_process", fail)
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError):
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    raw = (data_root / "logs/media-provider.jsonl").read_text()
    record = json.loads(raw)
    assert record["phase"] == "inference"
    assert record["missing_component"] == ("audio" if known else "unknown")
    assert record["inputs"] == {
        "audio": {"exists": False, "readable": False},
        "video": {"exists": True, "readable": True},
        "output_parent": {"exists": True, "readable": True},
    }
    assert "private folder" not in raw and "speech.wav" not in raw and "secret letter" not in raw


def test_missing_component_relative_path_is_mapped_without_scanning(tmp_path):
    result = latentsync_reply._failure_context("inference",
        "FileNotFoundError: [Errno 2] No such file or directory: 'configs/scheduler_config.json'",
        {"runtime_root": tmp_path, "scheduler": tmp_path / "configs/scheduler_config.json"})
    assert result == {"phase": "inference", "missing_component": "scheduler"}


@pytest.mark.parametrize(("relative", "expected"), [
    ("temp/private name.mp4", "runtime_temp"),
    ("temp/../private name.mp4", "unknown"),
    ("temporary/private name.mp4", "unknown"),
])
def test_runtime_temp_component_requires_containment(tmp_path, relative, expected):
    result = latentsync_reply._failure_context("inference",
        f"FileNotFoundError: [Errno 2] No such file or directory: '{relative}'",
        {"runtime_root": tmp_path})
    assert result == {"phase": "inference", "missing_component": expected}
    assert "private name" not in json.dumps(result)


def test_worker_reports_only_safe_exception_location(tmp_path):
    _write(tmp_path / "scripts/__init__.py", b"")
    _write(tmp_path / "scripts/inference.py", b"open('private letter secret.wav', 'rb')\n")
    worker = Path(latentsync_reply.__file__).resolve().parents[2] / "tools/latentsync_diagnostic_worker.py"
    result = subprocess.run([sys.executable, str(worker)], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0
    projected = latentsync_reply._failure_context("inference", result.stderr, {})
    assert projected["exception_type"] == "FileNotFoundError"
    assert projected["frames"] == [{"module": "scripts.inference", "line": 1}]
    assert "secret" not in json.dumps(projected)
    assert str(tmp_path) not in json.dumps(projected)


def test_worker_marker_cannot_export_arguments_or_foreign_frames():
    raw = 'OLIVIA_LATENTSYNC_FAILURE=' + json.dumps({
        'exception_type': 'FileNotFoundError', 'missing_component': 'ffmpeg',
        'frames': [{'module': 'subprocess', 'line': 99, 'args': 'private'},
                   {'module': 'C:/private/letter.py', 'line': 1}],
        'inputs': {'audio': {'exists': False}}, 'phase': 'source_prepare',
        'filename': 'private',
    })
    projected = latentsync_reply._failure_context('inference', raw, {})
    assert projected == {'phase': 'inference', 'missing_component': 'ffmpeg',
                         'exception_type': 'FileNotFoundError',
                         'frames': [{'module': 'subprocess', 'line': 99}]}


def test_worker_identifies_subprocess_executable_without_filename(tmp_path):
    _write(tmp_path / "scripts/__init__.py", b"")
    # Force the Windows-style failure without depending on host PATH contents.
    _write(tmp_path / "scripts/inference.py", b"import subprocess\nexec(compile('def fail():\\n executable = \\\"ffmpeg\\\"\\n raise FileNotFoundError(2, \\\"missing\\\")', 'synthetic.py', 'exec'), subprocess.__dict__)\nsubprocess.fail()\n")
    worker = Path(latentsync_reply.__file__).resolve().parents[2] / "tools/latentsync_diagnostic_worker.py"
    result = subprocess.run([sys.executable, str(worker)], cwd=tmp_path, capture_output=True)
    projected = latentsync_reply._failure_context("inference", result.stderr, {})
    assert projected["missing_component"] == "ffmpeg"
    assert projected["exception_type"] == "FileNotFoundError"
    assert projected["frames"][-1]["module"] == "subprocess"


@pytest.mark.parametrize(('relative', 'expected'), [
    ('checkpoints/whisper/tiny.pt', 'whisper_weights'),
    ('checkpoints/whisper/small.pt', 'whisper_weights'),
    ('stabilityai/sd-vae-ft-mse/diffusion_pytorch_model.bin', 'vae_weights'),
    ('ffmpeg.exe', 'ffmpeg'),
])
def test_missing_runtime_alternatives_are_classified(tmp_path, relative, expected):
    result = latentsync_reply._failure_context('inference',
        f"FileNotFoundError: No such file or directory: '{relative}'", {'runtime_root': tmp_path})
    assert result == {'phase': 'inference', 'missing_component': expected}
