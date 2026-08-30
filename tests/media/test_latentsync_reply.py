from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from runtime.media import latentsync_reply


def _write(path: Path, content: bytes = b"synthetic") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _latentsync_fixture(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "latentsync"
    for path in (
        root / "scripts" / "inference.py",
        root / "configs" / "unet" / "stage2_efficient.yaml",
        root / "checkpoints" / "latentsync_unet.pt",
    ):
        _write(path)
    return SimpleNamespace(
        root=root,
        python=_write(tmp_path / "python.exe"),
        source=_write(tmp_path / "source.mp4"),
        audio=_write(tmp_path / "speech.wav"),
        output=tmp_path / "reply.mp4",
        ffmpeg=_write(tmp_path / "ffmpeg" / "ffmpeg.exe"),
        cache=tmp_path / "provider-cache",
    )


def _prepare_source(
    _source: Path,
    _audio: Path,
    prepared: Path,
    *,
    environment: dict[str, str],
) -> None:
    assert environment["TEMP"]
    _write(prepared, b"prepared-video")


def test_render_keeps_success_when_published_output_cleanup_is_locked(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = _latentsync_fixture(tmp_path)

    observed: dict[str, object] = {}

    class LockedTemporaryDirectory:
        def __init__(
            self,
            *,
            prefix: str,
            dir: Path,
            ignore_cleanup_errors: bool = False,
        ) -> None:
            observed["ignore_cleanup_errors"] = ignore_cleanup_errors
            self.path = Path(dir) / f"{prefix}locked"

        def __enter__(self) -> str:
            self.path.mkdir(parents=True)
            return str(self.path)

        def __exit__(self, *_args: object) -> None:
            if not observed["ignore_cleanup_errors"]:
                raise PermissionError("synthetic Windows handle lock")

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if "--video_out_path" in command:
            _write(
                Path(command[command.index("--video_out_path") + 1]),
                b"rendered-video",
            )
        else:
            _write(Path(command[-1]), b"prepared-video")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(
        latentsync_reply.tempfile,
        "TemporaryDirectory",
        LockedTemporaryDirectory,
    )
    monkeypatch.setattr(latentsync_reply.subprocess, "run", run)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run)

    metadata = latentsync_reply.render_latentsync_video(
        fixture.source,
        fixture.audio,
        fixture.output,
        python_path=fixture.python,
        latentsync_root=fixture.root,
        ffmpeg_path=fixture.ffmpeg,
        provider_cache_root=fixture.cache,
    )

    assert fixture.output.read_bytes() == b"rendered-video"
    assert metadata["visual_provider"] == "LatentSync-1.5"
    assert observed["ignore_cleanup_errors"] is True


def test_managed_process_timeout_terminates_windows_process_tree(monkeypatch) -> None:
    from runtime.media import managed_subprocess

    observed: dict[str, object] = {}

    class TimedOutProcess:
        pid = 4242
        returncode: int | None = None

        def communicate(self, timeout: float | None = None):
            timeouts = observed.setdefault("communicate_timeouts", [])
            assert isinstance(timeouts, list)
            timeouts.append(timeout)
            assert timeout is not None
            if len(timeouts) <= 2:
                raise subprocess.TimeoutExpired(
                    cmd=["synthetic-worker"],
                    timeout=timeout,
                    stderr=b"still running",
                )
            return b"", b"terminated"

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            observed["direct_kill"] = True
            self.returncode = -9

    def popen(command: list[str], **kwargs: object) -> TimedOutProcess:
        observed["command"] = command
        observed["popen_kwargs"] = kwargs
        return TimedOutProcess()

    def taskkill(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["taskkill_command"] = command
        observed["taskkill_kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", popen)
    monkeypatch.setattr(managed_subprocess.subprocess, "run", taskkill)

    with pytest.raises(subprocess.TimeoutExpired):
        managed_subprocess.run_managed_process(
            ["synthetic-worker"],
            timeout_seconds=12.0,
        )

    popen_kwargs = observed["popen_kwargs"]
    assert isinstance(popen_kwargs, dict)
    creationflags = int(popen_kwargs["creationflags"])
    assert creationflags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert creationflags & subprocess.CREATE_NO_WINDOW
    assert observed["taskkill_command"] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert observed["direct_kill"] is True
    assert all(value is not None for value in observed["communicate_timeouts"])


@pytest.mark.parametrize(
    ("configured_timeout", "expected_timeout"), [(None, 1800.0), ("999999", 3600.0)]
)
def test_render_uses_managed_process_with_bounded_timeout(
    tmp_path: Path,
    monkeypatch,
    configured_timeout: str | None,
    expected_timeout: float,
) -> None:
    fixture = _latentsync_fixture(tmp_path)
    observed: dict[str, object] = {}

    def run_managed(
        command: list[str],
        *,
        timeout_seconds: float,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        observed["timeout_seconds"] = timeout_seconds
        assert cwd == fixture.root
        assert env["TEMP"]
        _write(
            Path(command[command.index("--video_out_path") + 1]),
            b"rendered-video",
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare_source)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run_managed)
    environment = {"PATH": ""}
    if configured_timeout is not None:
        environment["OLIVIA_LATENTSYNC_TIMEOUT_SECONDS"] = configured_timeout

    latentsync_reply.render_latentsync_video(
        fixture.source,
        fixture.audio,
        fixture.output,
        python_path=fixture.python,
        latentsync_root=fixture.root,
        ffmpeg_path=fixture.ffmpeg,
        provider_cache_root=fixture.cache,
        environment=environment,
    )

    assert fixture.output.read_bytes() == b"rendered-video"
    assert observed["timeout_seconds"] == expected_timeout


def test_render_failure_keeps_code_and_logs_redacted_stderr_diagnostic(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    fixture = _latentsync_fixture(tmp_path)

    def run_managed(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command,
            23,
            b"",
            (
                b"C:\\Users\\someone\\private\\config.yaml "
                b"api_key=super-secret letter=private-words "
                b"CUDA out of memory"
            ),
        )

    monkeypatch.setattr(latentsync_reply, "_prepare_source_clip", _prepare_source)
    monkeypatch.setattr(latentsync_reply, "run_managed_process", run_managed)

    with caplog.at_level(logging.WARNING, logger=latentsync_reply.__name__):
        with pytest.raises(latentsync_reply.LatentSyncReplyError) as failure:
            latentsync_reply.render_latentsync_video(
                fixture.source,
                fixture.audio,
                fixture.output,
                python_path=fixture.python,
                latentsync_root=fixture.root,
                ffmpeg_path=fixture.ffmpeg,
                provider_cache_root=fixture.cache,
            )

    assert str(failure.value) == "LATENTSYNC_FAILED"
    diagnostic = failure.value.diagnostic
    assert "returncode=23" in diagnostic
    assert "stderr_category=cuda_out_of_memory" in diagnostic
    assert len(diagnostic) <= 240
    assert diagnostic in caplog.text
    for private_value in (
        "C:\\Users",
        "super-secret",
        "private-words",
    ):
        assert private_value not in diagnostic
        assert private_value not in caplog.text
