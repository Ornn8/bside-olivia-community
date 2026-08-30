from __future__ import annotations

import json
from pathlib import Path
import subprocess
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


def _prepare(
    _source: Path, _audio: Path, target: Path, *, environment, timeout_seconds
) -> None:
    assert environment["TEMP"]
    assert timeout_seconds > 0
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
        return SimpleNamespace(
            returncode=0, stdout=b"frame=25\nout_time_us=1000000\nprogress=end\n", stderr=b"",
        )

    monkeypatch.setattr(latentsync_reply.tempfile, "TemporaryDirectory", LockedTemp)
    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    assert _render(fixture)["visual_provider"] == "LatentSync-1.5"
    assert fixture.output.read_bytes() == b"video"
    assert observed["ignored"] is True
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
            return subprocess.CompletedProcess(
                command, int("0:a:0" in command), b"frame=25\nout_time_us=1000000\nprogress=end\n",
                b"missing audio",
            )
        return subprocess.CompletedProcess(
            command, 0, b"frame=0\nout_time_us=0\nprogress=end\n", b"",
        )

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    assert str(caught.value) == "LATENTSYNC_FAILED"
    assert not fixture.output.exists()


@pytest.mark.parametrize(("configured", "expected"), [(None, 1800.0), ("999999", 3600.0)])
def test_render_uses_bounded_timeout(tmp_path: Path, monkeypatch, configured, expected) -> None:
    fixture, observed = _fixture(tmp_path), {}

    def run(command, *, timeout_seconds, **_kwargs):
        observed["timeout"] = timeout_seconds
        if "--video_out_path" in command:
            _write(Path(command[command.index("--video_out_path") + 1]), b"video")
        return subprocess.CompletedProcess(
            command, 0, b"frame=25\nout_time_us=1000000\nprogress=end\n", b"",
        )

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    monkeypatch.setattr(latentsync_reply.time, "monotonic", lambda: 0.0)
    environment = {"PATH": ""}
    if configured is not None:
        environment["OLIVIA_LATENTSYNC_TIMEOUT_SECONDS"] = configured
    _render(fixture, environment=environment)
    assert observed["timeout"] == expected


def test_prepare_and_worker_share_one_timeout_budget(tmp_path: Path, monkeypatch) -> None:
    fixture, observed = _fixture(tmp_path), {}
    moments = iter((0.0, 100.0, 300.0, 400.0))

    def prepare(_source, _audio, target, *, environment, timeout_seconds):
        observed["prepare"] = timeout_seconds
        _write(target)

    def run(command, *, timeout_seconds, **_kwargs):
        phase = "worker" if "--video_out_path" in command else "validation"
        observed[phase] = timeout_seconds
        if phase == "worker":
            _write(Path(command[command.index("--video_out_path") + 1]), b"video")
        return subprocess.CompletedProcess(
            command, 0, b"frame=25\nout_time_us=1000000\nprogress=end\n", b"",
        )

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    monkeypatch.setattr(latentsync_reply.time, "monotonic", lambda: next(moments))
    _render(fixture)
    assert observed == {"prepare": 1700.0, "worker": 1500.0, "validation": 1400.0}


def test_exhausted_total_deadline_does_not_start_worker(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture(tmp_path)

    def unexpected_worker(*_args, **_kwargs):
        raise AssertionError("worker started after the total deadline")

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", unexpected_worker)
    monkeypatch.setattr(latentsync_reply.time, "monotonic", lambda: next(iter_moments))
    iter_moments = iter((0.0, 100.0, 1801.0))
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    assert str(caught.value) == "LATENTSYNC_FAILED"
    assert caught.value.diagnostic == "returncode=unknown;stderr_category=process_timeout"


def test_prepare_nonzero_uses_stable_reported_failure(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture(tmp_path)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 19, b"", b"ffmpeg failed")

    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    assert str(caught.value) == "LATENTSYNC_FAILED"
    record = json.loads((data_root / "logs/media-provider.jsonl").read_text())
    assert "returncode=19" in record["diagnostic"]
    assert "stderr_category=external_process_failure" in record["diagnostic"]


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
    assert record["diagnostic"] == "returncode=23;stderr_category=cuda_out_of_memory"
    assert not any(value in json.dumps(record) for value in ("C:\\private", "secret", "words"))


@pytest.mark.parametrize(
    ("failure", "category"),
    (
        (subprocess.TimeoutExpired(["ffmpeg"], 1, stderr=b"busy"), "process_timeout"),
        (OSError("PROCESS_TREE_TERMINATION_FAILED"), "process_management_failure"),
    ),
)
def test_prepare_process_failure_is_reported(
    tmp_path: Path, monkeypatch, failure: Exception, category: str
) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(
        latentsync_reply, "run_managed_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    data_root = tmp_path / "data"
    with pytest.raises(latentsync_reply.LatentSyncReplyError) as caught:
        _render(fixture, environment={"PATH": "", "OLIVIA_LOCAL_DATA_ROOT": str(data_root)})
    assert str(caught.value) == "LATENTSYNC_FAILED"
    record = json.loads((data_root / "logs/media-provider.jsonl").read_text())
    assert f"stderr_category={category}" in record["diagnostic"]
