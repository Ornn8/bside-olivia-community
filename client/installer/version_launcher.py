"""Stable root launcher for an atomically selected backend version."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import runpy
import sys
import subprocess
import time
from pathlib import Path, PurePosixPath

STATE_NAME = ".olivia-update-state.json"
STATE_SCHEMA = "olivia.update-state.v1"
INSTALL_SCHEMA = "olivia.full-patch.install.v2"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_LEGACY_VERSION = "0.0.0+legacy"
_LEGACY_MANIFEST_SHA256 = "0" * 64
_LEGACY_PAYLOAD_PATH = "local_backend"


class VersionLauncherError(RuntimeError):
    """Stable, user-safe launcher failure code."""


class _StartInstance:
    """One process-held installation launch lease."""

    def __init__(self, close) -> None:
        self._close = close

    def close(self) -> None:
        close, self._close = self._close, None
        if close is not None:
            close()


def _try_acquire_start_instance(installation: Path) -> _StartInstance | None:
    """Acquire a non-blocking launch lease scoped to one installation root."""

    root = installation.expanduser().absolute()
    identity = hashlib.sha256(
        os.path.normcase(os.fspath(root)).encode("utf-8")
    ).hexdigest()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(
            None,
            False,
            f"Local\\Olivia.Local.Start.{identity}",
        )
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            return None
        return _StartInstance(lambda: kernel32.CloseHandle(handle))

    import fcntl

    lock_path = root / "data" / "launcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return None
        raise

    def close_file_lock() -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    return _StartInstance(close_file_lock)


def _running_client_processes(installation: Path) -> list[int]:
    """Process ids of Olivia.exe copies launched from this installation's app/."""

    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("process_id", wintypes.DWORD),
            ("default_heap_id", ctypes.c_size_t), ("module_id", wintypes.DWORD),
            ("threads", wintypes.DWORD), ("parent_process_id", wintypes.DWORD),
            ("base_priority", ctypes.c_long), ("flags", wintypes.DWORD),
            ("exe_file", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    app_root = os.path.normcase(os.path.abspath(installation / "app")) + os.sep
    found = []
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.exe_file.lower() == "olivia.exe":
                handle = kernel32.OpenProcess(0x1000, False, entry.process_id)  # QUERY_LIMITED_INFORMATION
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        size = wintypes.DWORD(len(buffer))
                        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            if os.path.normcase(os.path.abspath(buffer.value)).startswith(app_root):
                                found.append(int(entry.process_id))
                    finally:
                        kernel32.CloseHandle(handle)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _terminate_processes(process_ids: list[int]) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    for process_id in process_ids:
        handle = kernel32.OpenProcess(0x0001, False, process_id)  # PROCESS_TERMINATE
        if handle:
            try:
                kernel32.TerminateProcess(handle, 0)
            finally:
                kernel32.CloseHandle(handle)


def _replace_running_start(
    installation: Path,
    *,
    wait_seconds: float = 20.0,
    poll_seconds: float = 0.25,
) -> _StartInstance | None:
    """Stop this installation's running client, then take over its launch lease.

    A client hidden in the tray, or one left behind after a failed exit, made a
    second start do nothing. Ending that client lets its own launcher stop the
    backend and release the lease; this start then continues normally. While
    the first launcher is still starting (no client yet), nothing is stopped.
    """

    import time

    running = _running_client_processes(installation)
    if not running:
        return None
    _terminate_processes(running)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        instance = _try_acquire_start_instance(installation)
        if instance is not None:
            return instance
        time.sleep(poll_seconds)
    return None


def _append_launcher_event(installation: Path, event: str, **fields: object) -> None:
    """Persist a path-free stable-launcher event."""

    try:
        log_root = installation / "data" / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        with (log_root / "launcher.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")
    except OSError:
        pass


def _own_windows_start_process_tree() -> object | None:
    """Make descendants exit when this stable launcher process exits."""

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = ExtendedLimitInformation()
    limits.basic_limit_information.limit_flags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ) or not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "Windows Job Object setup failed")
    return job


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VersionLauncherError("UPDATE_STATE_INVALID") from exc
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validated_relative_path(
    value: object,
    version: object,
    digest: object,
) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not isinstance(version, str)
        or not isinstance(digest, str)
        or not _VERSION_RE.fullmatch(version)
        or not _SHA256_RE.fullmatch(digest)
    ):
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    relative = PurePosixPath(value)
    if (
        version == _LEGACY_VERSION
        and digest == _LEGACY_MANIFEST_SHA256
        and value == _LEGACY_PAYLOAD_PATH
    ):
        expected = PurePosixPath(_LEGACY_PAYLOAD_PATH)
    else:
        expected = PurePosixPath(
            "versions",
            "local_backend",
            f"{version}-{digest}",
        )
    if relative != expected or relative.as_posix() != value:
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    return relative


def _safe_version_path(root: Path, value: object, version: object, digest: object) -> Path:
    relative = _validated_relative_path(value, version, digest)
    current = root
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            raise VersionLauncherError("UPDATE_STATE_INVALID")
    if not current.is_dir():
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    return current


def resolve_active_backend(installation: str | os.PathLike[str]) -> Path:
    """Resolve one complete backend tree from one atomic state-file read."""

    root = Path(installation).expanduser().absolute()
    try:
        if _is_reparse_point(root) or not root.is_dir():
            raise VersionLauncherError("UPDATE_INSTALLATION_INVALID")
        root = root.resolve()
        marker_path = root / ".olivia-full-patch.json"
        if _is_reparse_point(marker_path):
            raise VersionLauncherError("UPDATE_INSTALLATION_INVALID")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            not isinstance(marker, dict)
            or marker.get("schema_version") != INSTALL_SCHEMA
            or marker.get("owned_root") != str(root)
        ):
            raise VersionLauncherError("UPDATE_INSTALLATION_INVALID")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionLauncherError("UPDATE_INSTALLATION_INVALID") from exc
    state_path = root / STATE_NAME
    if not state_path.exists():
        legacy = root / "local_backend"
        if not legacy.is_dir() or _is_reparse_point(legacy):
            raise VersionLauncherError("UPDATE_COMPONENT_UNAVAILABLE")
        return legacy
    if _is_reparse_point(state_path):
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionLauncherError("UPDATE_STATE_INVALID") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != STATE_SCHEMA
        or set(state) != {"schema_version", "active_components", "previous_components"}
        or not isinstance(state.get("active_components"), dict)
        or set(state["active_components"]) != {"local_backend"}
        or not isinstance(state.get("previous_components"), dict)
        or not set(state["previous_components"]).issubset({"local_backend"})
    ):
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    active = state["active_components"]["local_backend"]
    if not isinstance(active, dict) or set(active) != {
        "version",
        "manifest_sha256",
        "payload_path",
    }:
        raise VersionLauncherError("UPDATE_STATE_INVALID")
    active_path = _safe_version_path(
        root,
        active.get("payload_path"),
        active.get("version"),
        active.get("manifest_sha256"),
    )
    previous = state["previous_components"].get("local_backend")
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {
            "version",
            "manifest_sha256",
            "payload_path",
        }:
            raise VersionLauncherError("UPDATE_STATE_INVALID")
        _validated_relative_path(
            previous.get("payload_path"),
            previous.get("version"),
            previous.get("manifest_sha256"),
        )
    return active_path


def _entrypoint_arguments(
    action: str,
    installation: Path,
) -> tuple[Path, list[str]]:
    backend = resolve_active_backend(installation)
    root = installation.expanduser().resolve()
    entrypoints = {
        "start": ("start_local.py", "--install-root"),
        "proactive-login": ("proactive_login.py", "--install-root"),
        "configure": ("configure.py", "--installation"),
        "uninstall": ("uninstall.py", "--installation"),
    }
    script_name, root_option = entrypoints[action]
    entrypoint = backend / "installer" / script_name
    if (
        not entrypoint.is_file()
        or _is_reparse_point(entrypoint.parent)
        or _is_reparse_point(entrypoint)
    ):
        raise VersionLauncherError("UPDATE_COMPONENT_UNAVAILABLE")
    return entrypoint, [root_option, os.fspath(root)]


def _run_action(args: argparse.Namespace) -> int:
    try:
        entrypoint, root_arguments = _entrypoint_arguments(
            args.action,
            args.install_root,
        )
        previous_argv = sys.argv
        previous_path = list(sys.path)
        try:
            sys.argv = [os.fspath(entrypoint), *root_arguments, *args.arguments]
            sys.path.insert(0, os.fspath(entrypoint.parents[1]))
            runpy.run_path(os.fspath(entrypoint), run_name="__main__")
        finally:
            sys.argv = previous_argv
            sys.path[:] = previous_path
        return 0
    except VersionLauncherError as exc:
        if args.action == "start":
            _append_launcher_event(args.install_root, "startup_failed", code=str(exc), timestamp=time.time())
        print(json.dumps({"status": "ERROR", "code": str(exc)}))
        return 2
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
        if args.action == "start" and exit_code:
            _append_launcher_event(args.install_root, "startup_failed", code="START_EXIT_NONZERO",
                                   exit_code=exit_code, timestamp=time.time())
        return exit_code
    except Exception as exc:
        if args.action != "start":
            raise
        _append_launcher_event(args.install_root, "startup_failed", code="START_ACTION_FAILED",
                               timestamp=time.time(), **_startup_exception_fields(exc))
        print(json.dumps({"status": "ERROR", "code": "START_ACTION_FAILED"}))
        return 2


def _startup_exception_fields(error: BaseException) -> dict[str, str]:
    """Keep useful startup causes without exception text, paths or credentials."""

    fields = {"exception_type": type(error).__name__}
    if isinstance(error, ModuleNotFoundError) and error.name and re.fullmatch(r"[A-Za-z0-9_.]{1,150}", error.name):
        fields["missing_module"] = error.name
    return fields


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="olivia-version-launcher")
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument(
        "action", choices=("start", "proactive-login", "configure", "uninstall", "update-restart")
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def _run_main(
    args: argparse.Namespace,
    *,
    own_windows_tree: bool = False,
) -> int:
    if args.action == "update-restart":
        return _update_restart(args.install_root, Path(args.arguments[0]), int(args.arguments[1]))
    instance = None
    if args.action == "start":
        try:
            instance = _try_acquire_start_instance(args.install_root)
        except OSError:
            _append_launcher_event(args.install_root, "launch_lock_unavailable")
            print(json.dumps({"status": "ERROR", "code": "START_LOCK_UNAVAILABLE"}))
            return 2
        if instance is None:
            _append_launcher_event(args.install_root, "launch_already_running")
            try:
                instance = _replace_running_start(args.install_root)
            except OSError:
                instance = None
            if instance is None:
                return 0
            _append_launcher_event(args.install_root, "launch_replaced_running")
    try:
        if own_windows_tree and args.action == "start":
            try:
                # The raw handle intentionally remains open until process teardown.
                _job = _own_windows_start_process_tree()
            except OSError:
                _append_launcher_event(args.install_root, "launch_job_unavailable")
                print(json.dumps({"status": "ERROR", "code": "START_JOB_UNAVAILABLE"}))
                return 2
        return _run_action(args)
    finally:
        if instance is not None:
            instance.close()


def main(argv: list[str] | None = None) -> int:
    return _run_main(_parse_arguments(argv))


def _cli(argv: list[str] | None = None) -> int:
    return _run_main(_parse_arguments(argv), own_windows_tree=True)


def _restart_event(root: Path, step: str, **fields: object) -> None:
    _append_launcher_event(root, "update_restart_" + step, timestamp=time.time(), **fields)


def _restart_python(root: Path) -> Path:
    # START.cmd keeps runtime next to install/, not inside it.
    return root.parent / "runtime" / "python-3.12.10-embed-amd64" / "pythonw.exe"


def spawn_update_restart(root: Path) -> None:
    import secrets

    ready = root / "data" / "logs" / ("restart-" + secrets.token_hex(16) + ".ready")
    ready.parent.mkdir(parents=True, exist_ok=True)
    try:
        # The existing launcher's kill-on-close Job forbids breakaway. WMI creates
        # the helper outside that Job, including when upgrading an older launcher.
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = subprocess.list2cmdline([
            str(_restart_python(root)), str(root / "launcher" / "version_launcher.py"),
            "--install-root", str(root), "update-restart", str(ready), str(os.getpid()),
        ])
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::InputEncoding=[System.Text.Encoding]::UTF8; "
             "$p=[Console]::In.ReadToEnd() | ConvertFrom-Json; "
             "$s=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property "
             "@{CreateFlags=[uint32]520;ShowWindow=[uint16]0;EnvironmentVariables=[string[]]$p.environment}; "
             "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
             "@{CommandLine=[string]$p.command;CurrentDirectory=[string]$p.cwd;ProcessStartupInformation=$s}; "
             "if ($r.ReturnValue -ne 0) {exit 1}"],
            input=json.dumps({"command": command, "cwd": str(root),
                              "environment": [f"{key}={value}" for key, value in os.environ.items()]}, ensure_ascii=False),
            capture_output=True, encoding="utf-8", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError("UPDATE_RESTART_HELPER_UNAVAILABLE")
    except (OSError, subprocess.SubprocessError, RuntimeError):
        _restart_event(root, "failed", code="UPDATE_RESTART_HELPER_UNAVAILABLE")
        raise
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready.exists():
            ready.unlink()
            _restart_event(root, "accepted")
            return
        time.sleep(0.05)
    _restart_event(root, "failed", code="UPDATE_RESTART_HELPER_UNAVAILABLE")
    raise RuntimeError("UPDATE_RESTART_HELPER_UNAVAILABLE")


def _belongs_to_installation(root: Path, executable: str, command_line: str,
                             pid: int, self_pid: int) -> bool:
    import ntpath

    if pid == self_pid:
        return False
    prefix = ntpath.normcase(ntpath.normpath(str(root))).rstrip("\\")
    if executable and ntpath.normcase(ntpath.normpath(executable)).startswith(prefix + "\\"):
        return True
    # Outside the install folder only the bundled runtime runs our launcher and backend;
    # an editor holding a log open names the folder too and must be left alone.
    runtime = ntpath.normcase(ntpath.normpath(str(root.parent / "runtime"))).rstrip("\\")
    if not executable or not ntpath.normcase(ntpath.normpath(executable)).startswith(runtime + "\\"):
        return False
    # Match a complete path boundary, never another installation with this prefix.
    command = command_line.replace("/", "\\").casefold()
    return bool(re.search(r'(?:^|[\s"=])' + re.escape(prefix.casefold()) + r'(?=[\\"\s]|$)', command))


def _restart_processes(root: Path):
    """Retain native handles so PID reuse can never redirect timeout termination."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(ps), "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
         "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine,"
         "@{n='Created';e={$_.CreationDate.ToFileTimeUtc()}}) | ConvertTo-Json -Compress"],
        capture_output=True, encoding="utf-8-sig", timeout=10, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    records = json.loads(result.stdout)
    handles = []
    for record in records:
        pid = int(record["ProcessId"])
        executable = record.get("ExecutablePath") or ""
        if not _belongs_to_installation(root, executable, record.get("CommandLine") or "", pid, os.getpid()):
            continue
        handle = kernel.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)
        if not handle:
            continue
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        identity = kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                         ctypes.byref(kernel_time), ctypes.byref(user_time))
        created_ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        # Recheck the retained process identity, including command-line-only owners.
        if (not identity or abs(created_ticks - int(record["Created"])) >= 10
            or not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            or not executable or os.path.normcase(buffer.value) != os.path.normcase(executable)):
            kernel.CloseHandle(handle)
            continue
        handles.append((pid, handle))
    return kernel, handles


def _close_restart_windows(pids: set[int]) -> None:
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    @callback_type
    def close_window(window, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in pids:
            user.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE
        return True

    user.EnumWindows(close_window, 0)


def _restart_failure_prompt() -> None:
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, "Olivia 自动重启失败，请手动打开", "Olivia", 0x10 | 0x40000)


def _wait_restart_processes(root: Path, kernel, handles, owner_pid: int | None = None) -> None:
    deadline = time.monotonic() + 20
    owner = [handle for pid, handle in handles if pid == owner_pid]
    owner_stopped_at = None
    _restart_event(root, "waiting")
    while any(kernel.WaitForSingleObject(handle, 0) != 0 for _, handle in handles):
        now = time.monotonic()
        if owner and owner_stopped_at is None and kernel.WaitForSingleObject(owner[0], 0) == 0:
            owner_stopped_at = now
        # The client ignores the close request, so once the backend has gone it gets
        # a short grace period instead of the full timeout.
        if now >= deadline or (owner_stopped_at is not None and now >= owner_stopped_at + 3):
            _restart_event(root, "timeout" if now >= deadline else "grace_elapsed")
            for _, handle in handles:
                if kernel.WaitForSingleObject(handle, 0) != 0:
                    if not kernel.TerminateProcess(handle, 0):
                        raise RuntimeError("UPDATE_RESTART_STOP_FAILED")
            if any(kernel.WaitForSingleObject(handle, 5000) != 0 for _, handle in handles):
                raise RuntimeError("UPDATE_RESTART_STOP_FAILED")
            break
        time.sleep(0.1)
    _restart_event(root, "stopped")


def _update_restart(root: Path, ready: Path, owner_pid: int | None = None) -> int:
    handles = []
    try:
        _restart_event(root, "helper_started")
        kernel, handles = _restart_processes(root)
        if owner_pid is not None and owner_pid not in {pid for pid, _ in handles}:
            raise RuntimeError("UPDATE_RESTART_OWNER_UNAVAILABLE")
        ready.touch()
        deadline = time.monotonic() + 10
        while ready.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("UPDATE_RESTART_ACK_TIMEOUT")
            time.sleep(0.05)
        # The backend acknowledges the response before closing itself.
        time.sleep(1.5)
        _close_restart_windows({pid for pid, _ in handles})
        _wait_restart_processes(root, kernel, handles, owner_pid)
        log = root / "data" / "logs" / "launcher.jsonl"
        offset = log.stat().st_size
        _restart_event(root, "starting")
        process = subprocess.Popen(
            [str(_restart_python(root)), str(root / "launcher" / "version_launcher.py"),
             "--install-root", str(root), "start"], cwd=root,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with log.open("rb") as stream:
                stream.seek(offset)
                events = stream.read().decode("utf-8", errors="replace").splitlines()
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (event.get("event") == "client_start"
                    and any('"event": "backend_ready"' in item for item in events)):
                    time.sleep(2)
                    if process.poll() is not None:
                        raise RuntimeError("UPDATE_RESTART_START_FAILED")
                    _restart_event(root, "ready")
                    return 0
            if process.poll() is not None:
                raise RuntimeError("UPDATE_RESTART_START_FAILED")
            time.sleep(0.2)
        raise RuntimeError("UPDATE_RESTART_START_TIMEOUT")
    except Exception as exc:
        code = str(exc) if isinstance(exc, RuntimeError) and re.fullmatch(r"UPDATE_RESTART_[A-Z_]+", str(exc)) else "UPDATE_RESTART_HELPER_FAILED"
        _restart_event(root, "failed", code=code)
        _restart_failure_prompt()
        return 2
    finally:
        for _, handle in handles:
            kernel.CloseHandle(handle)
        ready.unlink(missing_ok=True)


__all__ = ["VersionLauncherError", "main", "resolve_active_backend"]


if __name__ == "__main__":
    raise SystemExit(_cli())
