from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from runtime.media import managed_subprocess


def test_windows_timeout_owns_and_terminates_suspended_worker(monkeypatch) -> None:
    observed: dict[str, object] = {"timeouts": []}

    class Process:
        pid, returncode = 4242, None

        def communicate(self, timeout=None):
            observed["timeouts"].append(timeout)
            if len(observed["timeouts"]) == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout, stderr=b"busy")
            return b"", b"stopped"

    class Job:
        def assign(self, process): observed["assigned"] = process.pid
        def resume(self, process): observed["resumed"] = process.pid
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
    assert flags & getattr(subprocess, "CREATE_SUSPENDED", 4)
    assert (observed["assigned"], observed["resumed"]) == (4242, 4242)
    assert observed["terminated"] is True and observed["closed"] is True
    assert observed["timeouts"] == [12, 15.0]


def test_windows_job_creation_failure_does_not_launch(monkeypatch) -> None:
    launched: list[bool] = []
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(
        managed_subprocess, "_create_windows_job",
        lambda: (_ for _ in ()).throw(OSError("job")),
    )
    monkeypatch.setattr(
        managed_subprocess.subprocess, "Popen",
        lambda *_args, **_kwargs: launched.append(True),
    )
    with pytest.raises(OSError, match="job"):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)
    assert not launched


def test_windows_assign_failure_kills_suspended_parent_before_resume(monkeypatch) -> None:
    observed: list[str] = []
    process = SimpleNamespace(
        kill=lambda: observed.append("kill"),
        communicate=lambda timeout: (observed.append(f"wait:{timeout}") or (b"", b"")),
    )
    job = SimpleNamespace(
        assign=lambda _process: (_ for _ in ()).throw(OSError("assign")),
        resume=lambda _process: observed.append("resume"),
        terminate=lambda: observed.append("terminate"),
        close=lambda: observed.append("close"),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)
    with pytest.raises(OSError, match="assign"):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)
    assert observed == ["kill", "wait:15.0", "close"]


def test_windows_job_close_failure_is_reported(monkeypatch) -> None:
    job = SimpleNamespace(
        assign=lambda _process: None, resume=lambda _process: None,
        terminate=lambda: None,
        close=lambda: (_ for _ in ()).throw(OSError("close")),
    )
    process = SimpleNamespace(returncode=0, communicate=lambda timeout: (b"", b""))
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)
    with pytest.raises(OSError, match="close"):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_real_windows_timeout_terminates_worker_and_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "worker-pids.txt"
    script = (
        "import os,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()},{child.pid}');"
        "time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        managed_subprocess.run_managed_process(
            [sys.executable, "-c", script, str(pid_file)], timeout_seconds=2,
        )
    for pid in (int(value) for value in pid_file.read_text().split(",")):
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        assert str(pid) not in result.stdout
