"""Bounded external media processes with whole-tree cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence


_TREE_SHUTDOWN_TIMEOUT_SECONDS = 15.0


def _create_windows_job():
    """Create a kill-on-close Job Object before the worker is launched."""

    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("per_process_time", ctypes.c_longlong),
            ("per_job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in ("read_operations", "write_operations", "other_operations",
                         "read_bytes", "write_bytes", "other_bytes")
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", IoCounters),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    for function in (
        kernel32.SetInformationJobObject, kernel32.AssignProcessToJobObject,
        kernel32.TerminateJobObject, kernel32.CloseHandle,
    ):
        function.restype = wintypes.BOOL
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = wintypes.LONG

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(handle)
        raise error

    class WindowsJob:
        def __init__(self) -> None:
            self.handle = handle

        def assign(self, process: subprocess.Popen[bytes]) -> None:
            process_handle = getattr(process, "_handle", None)
            if process_handle is None or not kernel32.AssignProcessToJobObject(
                self.handle, process_handle
            ):
                raise ctypes.WinError(ctypes.get_last_error())

        def resume(self, process: subprocess.Popen[bytes]) -> None:
            if ntdll.NtResumeProcess(getattr(process, "_handle", None)) != 0:
                raise OSError("PROCESS_RESUME_FAILED")

        def terminate(self) -> None:
            if not kernel32.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self) -> None:
            if self.handle and not kernel32.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None

    return WindowsJob()


def _report_cleanup_failures(
    active_error: BaseException | None,
    failures: list[tuple[str, BaseException]],
) -> None:
    if not failures:
        return
    message = "PROCESS_TREE_CLEANUP_FAILED: " + ", ".join(
        stage for stage, _error in failures
    )
    if active_error is not None:
        active_error.add_note(message)
        return
    raise OSError(message) from failures[0][1]


def run_managed_process(
    command: Sequence[str], *, timeout_seconds: float | None = None,
    deadline: float | None = None,
    cwd: Path | None = None, env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one worker; Windows descendants are owned before they can execute."""

    arguments = [str(value) for value in command]
    if (timeout_seconds is None) == (deadline is None):
        raise TypeError("provide exactly one of timeout_seconds or deadline")
    def wait_budget(fallback: float) -> float:
        if deadline is None:
            return fallback
        return min(fallback, max(0.0, deadline - time.monotonic()))
    def deadline_error() -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(arguments, 0)
    if deadline is not None and wait_budget(float("inf")) <= 0:
        raise deadline_error()
    kwargs: dict[str, object] = {
        "cwd": cwd, "env": None if env is None else dict(env),
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
    }
    job = _create_windows_job() if os.name == "nt" else None
    if deadline is not None and wait_budget(float("inf")) <= 0:
        if job is not None:
            job.close()
        raise deadline_error()
    if job is not None:
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
    else:
        kwargs["start_new_session"] = True
    process: subprocess.Popen[bytes] | None = None
    assigned = False
    failures: list[tuple[str, BaseException]] = []
    try:
        process = subprocess.Popen(arguments, **kwargs)
        if job is not None:
            try:
                job.assign(process)
                assigned = True
                if deadline is not None and wait_budget(float("inf")) <= 0:
                    raise deadline_error()
                job.resume(process)
            except OSError:
                if not assigned:
                    try:
                        process.kill()
                    except OSError as exc:
                        failures.append(("kill", exc))
                    try:
                        process.communicate(timeout=wait_budget(_TREE_SHUTDOWN_TIMEOUT_SECONDS))
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        failures.append(("reap", exc))
                raise
        try:
            process_timeout = (
                timeout_seconds if timeout_seconds is not None
                else wait_budget(float("inf"))
            )
            if deadline is not None:
                process_timeout -= min(_TREE_SHUTDOWN_TIMEOUT_SECONDS, process_timeout / 2)
            if process_timeout <= 0:
                raise deadline_error()
            stdout, stderr = process.communicate(timeout=process_timeout)
        except subprocess.TimeoutExpired:
            raise
        return subprocess.CompletedProcess(
            arguments, int(process.returncode or 0), stdout, stderr
        )
    finally:
        active_error = sys.exc_info()[1]
        if job is not None:
            if assigned and process is not None:
                try:
                    job.terminate()
                except OSError as exc:
                    failures.append(("terminate", exc))
                try:
                    reaped_stdout, reaped_stderr = process.communicate(
                        timeout=wait_budget(_TREE_SHUTDOWN_TIMEOUT_SECONDS)
                    )
                    if isinstance(active_error, subprocess.TimeoutExpired):
                        active_error.output = reaped_stdout or active_error.output
                        active_error.stderr = reaped_stderr or active_error.stderr
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failures.append(("reap", exc))
            try:
                job.close()
            except OSError as exc:
                failures.append(("close", exc))
            _report_cleanup_failures(active_error, failures)
        elif process is not None:
            failures = []
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                failures.append(("killpg", exc))
            try:
                reaped_stdout, reaped_stderr = process.communicate(
                    timeout=wait_budget(_TREE_SHUTDOWN_TIMEOUT_SECONDS)
                )
                if isinstance(active_error, subprocess.TimeoutExpired):
                    active_error.output = reaped_stdout or active_error.output
                    active_error.stderr = reaped_stderr or active_error.stderr
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(("reap", exc))
            _report_cleanup_failures(active_error, failures)


__all__ = ["run_managed_process"]
