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


def _append_launcher_event(installation: Path, event: str) -> None:
    """Persist a path-free stable-launcher event."""

    try:
        log_root = installation / "data" / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        with (log_root / "launcher.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event}, sort_keys=True) + "\n")
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
        print(json.dumps({"status": "ERROR", "code": str(exc)}))
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="olivia-version-launcher")
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("action", choices=("start", "configure", "uninstall"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def _run_main(
    args: argparse.Namespace,
    *,
    own_windows_tree: bool = False,
) -> int:
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
            return 0
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


__all__ = ["VersionLauncherError", "main", "resolve_active_backend"]


if __name__ == "__main__":
    raise SystemExit(_cli())
