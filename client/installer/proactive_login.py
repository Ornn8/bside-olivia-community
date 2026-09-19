"""Windowless login-time scanner for proactive-letter intents.

This process reads only ``data/proactive/settings.json`` until both proactive
switches are enabled.  It then delegates one file-only scan to
``runtime.reply.proactive_letters`` and never starts the web server, UI, voice,
or LLM runtime.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable


CHECK_INTERVAL_SECONDS = 300.0
EMBEDDED_RUNTIME_DIR = "python-3.12.10-embed-amd64"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "BSideOliviaProactiveLogin"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_WORKER_CREATE_TIMEOUT_SECONDS = 15


class _InstanceLease:
    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release

    def close(self) -> None:
        release, self._release = self._release, None
        if release is not None:
            release()


def _try_acquire_instance(install_root: Path) -> _InstanceLease | None:
    """Acquire a process-wide Windows mutex without creating another data file."""

    if os.name != "nt":
        # The shipped process is Windows-only.  Keeping non-Windows imports
        # runnable makes focused packaging tests possible without fake files.
        return _InstanceLease(lambda: None)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes

    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    identity = hashlib.sha256(
        os.path.normcase(os.fspath(install_root.expanduser().absolute())).encode("utf-8")
    ).hexdigest()
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(
        None, False, f"Local\\Olivia.Local.ProactiveLogin.{identity}"
    )
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return _InstanceLease(lambda: kernel32.CloseHandle(handle))


def _read_settings(data_root: Path) -> dict[str, object]:
    try:
        value = json.loads(
            (data_root / "proactive" / "settings.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _enabled(settings: dict[str, object]) -> bool:
    return settings.get("enabled") is True and settings.get("login_check_enabled") is True


def _scan_once(data_root: Path, *, now: float) -> dict:
    # Import the scanner only after both switches are on.  This keeps the
    # login path independent of server, voice, and provider imports.
    from runtime.reply.proactive_letters import scan_pending

    return scan_pending(data_root, now=now)


def run(
    install_root: Path,
    *,
    interval_seconds: float = CHECK_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    scan: Callable[..., dict] = _scan_once,
) -> int:
    """Run the quiet login worker until settings disable it or it is stopped."""

    root = install_root.expanduser().resolve()
    lease = _try_acquire_instance(root)
    if lease is None:
        return 0
    try:
        data_root = root / "data"
        while _enabled(_read_settings(data_root)):
            try:
                scan(data_root, now=clock())
            except (OSError, ValueError):
                # An unavailable opportunity file is retried on the next tick.
                pass
            sleep(interval_seconds)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        lease.close()


def windows_login_command(
    install_root: Path,
    *,
    python_executable: Path | None = None,
    launcher: Path | None = None,
) -> str:
    """Build the command using the installed embedded ``pythonw.exe`` path."""

    root = install_root.expanduser().resolve()
    pythonw = python_executable or (
        root.parent / "runtime" / EMBEDDED_RUNTIME_DIR / "pythonw.exe"
    )
    stable_launcher = launcher or (root / "launcher" / "version_launcher.py")
    return subprocess.list2cmdline(
        [
            os.fspath(pythonw),
            os.fspath(stable_launcher),
            "--install-root",
            os.fspath(root),
            "proactive-login",
        ]
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE") from exc
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _refresh_stable_launcher(root: Path) -> Path:
    """Refresh the stable launcher from the manifest-validated active backend."""

    try:
        from installer import version_launcher

        backend = version_launcher.resolve_active_backend(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError("PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE") from exc

    source = backend / "installer" / "version_launcher.py"
    launcher_directory = root / "launcher"
    stable_launcher = launcher_directory / "version_launcher.py"
    if (
        not source.is_file()
        or _is_reparse_point(source)
        or not launcher_directory.is_dir()
        or _is_reparse_point(launcher_directory)
    ):
        raise RuntimeError("PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE")
    if stable_launcher.exists() and _is_reparse_point(stable_launcher):
        raise RuntimeError("PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE")

    temporary = launcher_directory / f".version_launcher.py.update-{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, stable_launcher)
    except OSError as exc:
        raise RuntimeError("PROACTIVE_LOGIN_LAUNCHER_REFRESH_FAILED") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return stable_launcher


def _validated_login_paths(
    data_root: Path,
    *,
    python_executable: Path | None = None,
    launcher: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Resolve the installed data root, stable launcher, and hidden interpreter."""

    root = data_root.expanduser().resolve().parent
    pythonw = python_executable or (
        root.parent / "runtime" / EMBEDDED_RUNTIME_DIR / "pythonw.exe"
    )
    if not pythonw.is_file():
        raise RuntimeError("PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE")
    stable_launcher = launcher or _refresh_stable_launcher(root)
    if not stable_launcher.is_file():
        raise RuntimeError("PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE")
    return root, pythonw, stable_launcher


def _powershell_path() -> Path:
    powershell = (
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        raise RuntimeError("PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE")
    return powershell


def _encoded_worker_create_command(
    *,
    command_line: str,
    current_directory: Path,
) -> str:
    payload = base64.b64encode(
        json.dumps(
            {
                "command_line": command_line,
                "current_directory": os.fspath(current_directory),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
try {{
    $payload = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('{payload}')
    ) | ConvertFrom-Json
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{
        CommandLine = [string]$payload.command_line
        CurrentDirectory = [string]$payload.current_directory
    }}
    if ($null -eq $result -or [int]$result.ReturnValue -ne 0) {{ exit 1 }}
    exit 0
}} catch {{
    exit 1
}}
"""
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _start_worker_outside_app_job(
    root: Path,
    pythonw: Path,
    stable_launcher: Path,
) -> subprocess.CompletedProcess[bytes]:
    command_line = subprocess.list2cmdline(
        [
            os.fspath(pythonw),
            os.fspath(stable_launcher),
            "--install-root",
            os.fspath(root),
            "proactive-login",
        ]
    )
    helper = _powershell_path()
    encoded = _encoded_worker_create_command(
        command_line=command_line,
        current_directory=root,
    )
    try:
        completed = subprocess.run(
            [
                os.fspath(helper),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            cwd=os.fspath(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
            timeout=_WORKER_CREATE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("PROACTIVE_LOGIN_WORKER_START_FAILED") from exc
    if completed.returncode != 0:
        raise RuntimeError("PROACTIVE_LOGIN_WORKER_START_FAILED")
    return completed


def start_login_worker(
    data_root: Path,
    *,
    python_executable: Path | None = None,
    launcher: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Start the hidden worker after the installed runtime has been verified."""

    if os.name != "nt":
        raise RuntimeError("PROACTIVE_LOGIN_WINDOWS_ONLY")
    root, pythonw, stable_launcher = _validated_login_paths(
        data_root,
        python_executable=python_executable,
        launcher=launcher,
    )
    return _start_worker_outside_app_job(root, pythonw, stable_launcher)


def register_windows_login(
    install_root: Path,
    *,
    enabled: bool = True,
    python_executable: Path | None = None,
    launcher: Path | None = None,
) -> str | None:
    """Set or clear the per-user Windows login value; never runs it here."""

    if os.name != "nt":
        raise RuntimeError("PROACTIVE_LOGIN_WINDOWS_ONLY")
    import winreg

    if not enabled:
        unregister_windows_login()
        return None
    command = windows_login_command(
        install_root,
        python_executable=python_executable,
        launcher=launcher,
    )
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
    return command


def configure_login_start(
    data_root: Path,
    *,
    enabled: bool,
    python_executable: Path | None = None,
    launcher: Path | None = None,
) -> str | None:
    """Apply the native per-user login switch without persisting app state."""

    if type(enabled) is not bool:
        raise ValueError("PROACTIVE_LOGIN_ENABLED_INVALID")
    if not enabled:
        unregister_windows_login()
        return None
    root, pythonw, stable_launcher = _validated_login_paths(
        data_root,
        python_executable=python_executable,
        launcher=launcher,
    )
    return register_windows_login(
        root,
        enabled=True,
        python_executable=pythonw,
        launcher=stable_launcher,
    )


def unregister_windows_login() -> bool:
    """Remove only this product's per-user login value."""

    if os.name != "nt":
        raise RuntimeError("PROACTIVE_LOGIN_WINDOWS_ONLY")
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                return False
    except FileNotFoundError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="olivia-proactive-login")
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.install_root)


__all__ = [
    "CHECK_INTERVAL_SECONDS",
    "RUN_KEY",
    "RUN_VALUE_NAME",
    "main",
    "configure_login_start",
    "register_windows_login",
    "run",
    "start_login_worker",
    "unregister_windows_login",
    "windows_login_command",
]


if __name__ == "__main__":
    raise SystemExit(main())
