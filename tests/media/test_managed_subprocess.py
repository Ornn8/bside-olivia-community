from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from runtime.media import managed_subprocess


def test_windows_success_terminates_assigned_job_before_close(monkeypatch) -> None:
    events: list[str] = []
    active_processes = iter((2, 0))
    monotonic_times = iter((100.0, 100.000003, 100.000004))

    class Process:
        pid, returncode = 4242, 0

        def communicate(self, timeout=None):
            events.append(f"reap:{timeout}")
            return b"out", b"err"

    job = SimpleNamespace(
        assign=lambda _process: events.append("assign"),
        resume=lambda _process: events.append("resume"),
        terminate=lambda: events.append("terminate"),
        active_processes=lambda: (
            events.append("active") or next(active_processes)
        ),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(
        managed_subprocess.time, "monotonic", lambda: next(monotonic_times)
    )
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: Process())

    result = managed_subprocess.run_managed_process(["worker"], timeout_seconds=12)

    assert result.stdout == b"out"
    assert events[:4] == ["assign", "resume", "reap:12", "terminate"]
    assert events[4].startswith("reap:")
    assert float(events[4].removeprefix("reap:")) == pytest.approx(15.0)
    assert events[5:] == ["active", "active", "close"]


def test_windows_timeout_owns_and_terminates_suspended_worker(monkeypatch) -> None:
    observed: dict[str, object] = {"timeouts": []}
    monotonic_times = iter((100.0, 100.000003))

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
        def active_processes(self): return 0
        def close(self): observed["closed"] = True

    def popen(_command, **kwargs):
        observed["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", Job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", popen)
    monkeypatch.setattr(
        managed_subprocess.time, "monotonic", lambda: next(monotonic_times)
    )
    with pytest.raises(subprocess.TimeoutExpired):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=12)
    flags = observed["kwargs"]["creationflags"]
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP and flags & subprocess.CREATE_NO_WINDOW
    assert flags & getattr(subprocess, "CREATE_SUSPENDED", 4)
    assert (observed["assigned"], observed["resumed"]) == (4242, 4242)
    assert observed["terminated"] is True and observed["closed"] is True
    timeouts = observed["timeouts"]
    assert isinstance(timeouts, list)
    assert timeouts[0] == 12
    assert timeouts[1] == pytest.approx(15.0)


def test_absolute_deadline_includes_start_timeout_and_cleanup(monkeypatch) -> None:
    clock, waits = [0.0], []
    def spend(seconds):
        clock[0] += seconds
    class Process:
        pid, returncode = 4242, None
        def communicate(self, timeout):
            waits.append(timeout)
            spend(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout)
            return b"", b"stopped"
    class Job:
        def assign(self, _process): spend(1)
        def resume(self, _process): spend(1)
        def terminate(self): spend(1)
        def active_processes(self): return 0
        def close(self): pass
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_TREE_SHUTDOWN_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(managed_subprocess.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: (spend(1) or Job()))
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: (spend(1) or Process()))
    with pytest.raises(subprocess.TimeoutExpired):
        managed_subprocess.run_managed_process(["worker"], deadline=10.0)
    assert clock[0] <= 10.0
    assert waits == [4.0, 1.0]
    with pytest.raises(subprocess.TimeoutExpired):
        managed_subprocess.run_managed_process(["worker"], deadline=10.0)
    assert clock[0] == 10.0


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


def test_windows_assign_failure_survives_cleanup_failures(monkeypatch) -> None:
    observed: list[str] = []
    timeout_error = subprocess.TimeoutExpired(["worker"], 15.0)
    process = SimpleNamespace(
        kill=lambda: (observed.append("kill") or (_ for _ in ()).throw(OSError("kill"))),
        communicate=lambda timeout: (
            observed.append(f"wait:{timeout}") or (_ for _ in ()).throw(timeout_error)
        ),
    )
    job = SimpleNamespace(
        assign=lambda _process: (_ for _ in ()).throw(OSError("assign")),
        resume=lambda _process: observed.append("resume"),
        terminate=lambda: observed.append("terminate"),
        close=lambda: (
            observed.append("close") or (_ for _ in ()).throw(OSError("close"))
        ),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)
    with pytest.raises(OSError, match="assign") as caught:
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)
    assert observed == ["kill", "wait:15.0", "close"]
    assert caught.value.__notes__ == [
        "PROCESS_TREE_CLEANUP_FAILED: kill, reap, close",
    ]


def test_windows_resume_failure_terminates_assigned_job_once(monkeypatch) -> None:
    observed: list[str] = []
    process = SimpleNamespace(
        communicate=lambda timeout: (observed.append(f"reap:{timeout}") or (b"", b"")),
    )
    job = SimpleNamespace(
        assign=lambda _process: observed.append("assign"),
        resume=lambda _process: (_ for _ in ()).throw(OSError("resume")),
        terminate=lambda: observed.append("terminate"),
        active_processes=lambda: 0,
        close=lambda: observed.append("close"),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(OSError, match="resume"):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)

    assert observed == ["assign", "terminate", "reap:15.0", "close"]


def test_windows_cleanup_failures_do_not_swallow_resume_error(monkeypatch) -> None:
    process = SimpleNamespace(
        communicate=lambda timeout: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["worker"], timeout)
        ),
    )
    job = SimpleNamespace(
        assign=lambda _process: None,
        resume=lambda _process: (_ for _ in ()).throw(OSError("resume")),
        terminate=lambda: (_ for _ in ()).throw(OSError("terminate")),
        active_processes=lambda: 0,
        close=lambda: (_ for _ in ()).throw(OSError("close")),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(OSError, match="resume") as caught:
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)

    assert caught.value.__notes__ == [
        "PROCESS_TREE_CLEANUP_FAILED: terminate, reap, close",
    ]


def test_windows_job_close_failure_warns_after_tree_exit(monkeypatch) -> None:
    observed: list[str] = []
    job = SimpleNamespace(
        assign=lambda _process: None, resume=lambda _process: None,
        terminate=lambda: observed.append("terminate"),
        active_processes=lambda: 0,
        close=lambda: (_ for _ in ()).throw(OSError("close")),
    )
    process = SimpleNamespace(
        returncode=0,
        communicate=lambda timeout: (observed.append(f"reap:{timeout}") or (b"", b"")),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)
    with pytest.warns(RuntimeWarning, match="PROCESS_TREE_CLEANUP_FAILED: close"):
        result = managed_subprocess.run_managed_process(
            ["worker"], timeout_seconds=1,
        )
    assert result.returncode == 0
    assert observed == ["reap:1", "terminate", "reap:15.0"]


def test_windows_terminate_failure_warns_after_tree_exit(monkeypatch) -> None:
    observed: list[str] = []
    process = SimpleNamespace(
        returncode=0, communicate=lambda timeout: (b"", b""),
    )
    job = SimpleNamespace(
        assign=lambda _process: None, resume=lambda _process: None,
        terminate=lambda: (_ for _ in ()).throw(OSError("terminate")),
        active_processes=lambda: 0,
        close=lambda: observed.append("close"),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.warns(RuntimeWarning, match="PROCESS_TREE_CLEANUP_FAILED: terminate"):
        result = managed_subprocess.run_managed_process(
            ["worker"], timeout_seconds=1,
        )

    assert result.returncode == 0
    assert observed == ["close"]


@pytest.mark.parametrize("returncode", (0, 7))
def test_cleanup_timeout_fails_closed_for_completed_result(monkeypatch, returncode) -> None:
    clock = [0.0]
    process = SimpleNamespace(
        returncode=returncode, communicate=lambda timeout: (b"out", b"err"),
    )
    job = SimpleNamespace(
        assign=lambda _process: None, resume=lambda _process: None,
        terminate=lambda: None, active_processes=lambda: 1, close=lambda: None,
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_TREE_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(managed_subprocess.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        managed_subprocess.time, "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(
        OSError, match="^PROCESS_TREE_CLEANUP_FAILED: quiesce$",
    ) as caught:
        managed_subprocess.run_managed_process(
            ["worker"], timeout_seconds=1,
        )

    assert caught.value.__cause__ is None


def test_cleanup_timeout_preserves_process_timeout(monkeypatch) -> None:
    clock, calls = [0.0], [0]
    original = subprocess.TimeoutExpired(["worker"], 1, stderr=b"busy")
    def communicate(timeout):
        calls[0] += 1
        if calls[0] == 1:
            raise original
        return b"", b"stopped"
    process = SimpleNamespace(returncode=None, communicate=communicate)
    job = SimpleNamespace(
        assign=lambda _process: None, resume=lambda _process: None,
        terminate=lambda: None, active_processes=lambda: 1, close=lambda: None,
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "nt")
    monkeypatch.setattr(managed_subprocess, "_TREE_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(managed_subprocess.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        managed_subprocess.time, "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(managed_subprocess, "_create_windows_job", lambda: job)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)

    assert caught.value is original
    assert caught.value.__notes__ == ["PROCESS_TREE_CLEANUP_FAILED: quiesce"]


def test_posix_success_terminates_process_group_and_reaps(monkeypatch) -> None:
    observed: list[str] = []
    group_exists = iter((True, False))

    class Process:
        pid, returncode = 4242, 0

        def communicate(self, timeout=None):
            observed.append(f"reap:{timeout}")
            return b"out", b"err"

    def popen(_command, **kwargs):
        assert kwargs["start_new_session"] is True
        return Process()

    def killpg(pid, sig):
        observed.append(f"kill:{pid}:{sig}")
        if sig == 0 and not next(group_exists):
            raise ProcessLookupError

    monkeypatch.setattr(managed_subprocess.os, "name", "posix")
    monkeypatch.setattr(
        managed_subprocess.os, "killpg", killpg, raising=False,
    )
    monkeypatch.setattr(managed_subprocess.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", popen)

    result = managed_subprocess.run_managed_process(["worker"], timeout_seconds=12)

    assert result.stdout == b"out"
    assert observed == [
        "reap:12", f"kill:4242:{managed_subprocess.signal.SIGKILL}", "reap:15.0",
        "kill:4242:0", "kill:4242:0",
    ]


def test_posix_kill_failure_warns_after_group_exit(monkeypatch) -> None:
    process = SimpleNamespace(
        pid=4242, returncode=7, communicate=lambda timeout: (b"out", b"err"),
    )
    def killpg(_pid, sig):
        if sig == 0:
            raise ProcessLookupError
        raise OSError("killpg")
    monkeypatch.setattr(managed_subprocess.os, "name", "posix")
    monkeypatch.setattr(managed_subprocess.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(managed_subprocess.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.warns(RuntimeWarning, match="PROCESS_TREE_CLEANUP_FAILED: killpg"):
        result = managed_subprocess.run_managed_process(
            ["worker"], timeout_seconds=1,
        )

    assert (result.returncode, result.stdout, result.stderr) == (7, b"out", b"err")


def test_posix_cleanup_timeout_fails_closed(monkeypatch) -> None:
    clock = [0.0]
    process = SimpleNamespace(
        pid=4242, returncode=0, communicate=lambda timeout: (b"out", b"err"),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "posix")
    monkeypatch.setattr(managed_subprocess.os, "killpg", lambda *_args: None, raising=False)
    monkeypatch.setattr(managed_subprocess.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(managed_subprocess, "_TREE_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(managed_subprocess.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        managed_subprocess.time, "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(OSError, match="^PROCESS_TREE_CLEANUP_FAILED: quiesce$"):
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)


def test_posix_cleanup_failures_do_not_swallow_timeout(monkeypatch) -> None:
    process = SimpleNamespace(
        pid=4242,
        communicate=lambda timeout: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["worker"], timeout)
        ),
    )
    monkeypatch.setattr(managed_subprocess.os, "name", "posix")
    monkeypatch.setattr(
        managed_subprocess.os, "killpg",
        lambda _pid, _sig: (_ for _ in ()).throw(OSError("killpg")), raising=False,
    )
    monkeypatch.setattr(managed_subprocess.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(managed_subprocess.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        managed_subprocess.run_managed_process(["worker"], timeout_seconds=1)

    assert caught.value.__notes__ == [
        "PROCESS_TREE_CLEANUP_FAILED: killpg, reap, quiesce",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
@pytest.mark.parametrize(("parent_sleep", "times_out"), [(30, True), (0, False)])
def test_real_windows_returns_after_worker_tree_exits(
    tmp_path: Path, parent_sleep: int, times_out: bool,
) -> None:
    pid_file = tmp_path / "worker-pids.txt"
    script = (
        "import os,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()},{child.pid}');"
        "time.sleep(float(sys.argv[2]))"
    )
    command = [sys.executable, "-c", script, str(pid_file), str(parent_sleep)]
    if times_out:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            managed_subprocess.run_managed_process(command, timeout_seconds=2)
        assert not getattr(caught.value, "__notes__", ())
    else:
        assert managed_subprocess.run_managed_process(command, timeout_seconds=2).returncode == 0
    for pid in (int(value) for value in pid_file.read_text().split(",")):
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        assert result.returncode == 0
        assert str(pid) not in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
@pytest.mark.parametrize(("parent_sleep", "times_out"), [(30, True), (0, False)])
def test_real_posix_returns_after_worker_group_exits(
    tmp_path: Path, parent_sleep: int, times_out: bool,
) -> None:
    pid_file = tmp_path / "worker-pgid.txt"
    script = (
        "import os,pathlib,subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
        "time.sleep(float(sys.argv[2]))"
    )
    command = [sys.executable, "-c", script, str(pid_file), str(parent_sleep)]
    if times_out:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            managed_subprocess.run_managed_process(command, timeout_seconds=2)
        assert not getattr(caught.value, "__notes__", ())
    else:
        assert managed_subprocess.run_managed_process(command, timeout_seconds=2).returncode == 0
    with pytest.raises(ProcessLookupError):
        os.killpg(int(pid_file.read_text()), 0)
