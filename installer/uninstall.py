"""Standalone uninstall entry point for the managed-runtime Windows install."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from .uninstall_safety import (
        MARKER_NAME,
        OWNED_PATHS,
        PRESERVED_PATHS,
        remove_owned_targets,
        safe_owned_targets,
    )
except ImportError:  # Support direct execution and the stable runpy launcher.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from uninstall_safety import (
        MARKER_NAME,
        OWNED_PATHS,
        PRESERVED_PATHS,
        remove_owned_targets,
        safe_owned_targets,
    )


_MANAGED_PROCESS_FILTER = r"""
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($env:OLIVIA_UNINSTALL_TARGET)
$appPrefix = [IO.Path]::GetFullPath((Join-Path $root 'app')) + [IO.Path]::DirectorySeparatorChar
$backendPrefixes = @(
    [IO.Path]::GetFullPath((Join-Path $root 'local_backend')) + [IO.Path]::DirectorySeparatorChar
    [IO.Path]::GetFullPath((Join-Path $root 'versions\local_backend')) + [IO.Path]::DirectorySeparatorChar
)
function Test-IsManagedOliviaProcess {
    param([object]$Process)
    $executable = [string]$Process.ExecutablePath
    if ($executable -and $executable.StartsWith($appPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    if ($Process.Name -notin @('python.exe', 'pythonw.exe')) {
        return $false
    }
    $command = [string]$Process.CommandLine
    foreach ($backendPrefix in $backendPrefixes) {
        if ($command.IndexOf($backendPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $true
        }
    }
    return $false
}
"""


_STOP_MANAGED_PROCESSES = _MANAGED_PROCESS_FILTER + r"""
$taskkill = Join-Path $env:WINDIR 'System32\taskkill.exe'
$targets = @(Get-CimInstance Win32_Process | Where-Object {
    Test-IsManagedOliviaProcess $_
})
foreach ($target in $targets) {
    & $taskkill /PID $target.ProcessId /T /F *> $null
}
for ($attempt = 0; $attempt -lt 50; $attempt += 1) {
    $remaining = @(Get-CimInstance Win32_Process | Where-Object {
        Test-IsManagedOliviaProcess $_
    })
    if ($remaining.Count -eq 0) { exit 0 }
    Start-Sleep -Milliseconds 100
}
exit 1
"""


def _stop_managed_processes(root: Path) -> None:
    if os.name != "nt":
        return
    powershell = (
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        raise OSError("MANAGED_PROCESS_STOP_FAILED")
    environment = dict(os.environ)
    environment["OLIVIA_UNINSTALL_TARGET"] = os.fspath(root)
    command = [
        os.fspath(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _STOP_MANAGED_PROCESSES,
    ]
    for _attempt in range(2):
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=environment,
        )
        if result.returncode == 0:
            return
    raise OSError("MANAGED_PROCESS_STOP_FAILED")


def _remove_launch_shortcuts(root: Path) -> None:
    if os.name != "nt":
        return
    script = Path(__file__).resolve().parent / "Create-Shortcut.ps1"
    try:
        powershell = (
            Path(os.environ["WINDIR"])
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file() or not script.is_file():
            raise OSError
        subprocess.run(
            [
                os.fspath(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                os.fspath(script),
                "-InstallRoot",
                os.fspath(root),
                "-RemoveExisting",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (KeyError, OSError, subprocess.SubprocessError):
        print("SHORTCUT_CLEANUP_FAILED")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.installation.expanduser().absolute()
    marker_path = root / MARKER_NAME
    if not root.is_dir() or not marker_path.is_file():
        print("PATCH_MARKER_NOT_FOUND")
        return 2
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("schema_version") != "olivia.full-patch.install.v2" or marker.get("owned_root") != str(root):
        print("PATCH_MARKER_INVALID")
        return 2
    try:
        safe_owned_targets(root)
    except ValueError:
        print("PATCH_MARKER_INVALID")
        return 2
    print(json.dumps({"status": "UNINSTALLED" if args.apply else "DRY_RUN", "owned_paths": list(OWNED_PATHS), "preserved_paths": list(PRESERVED_PATHS)}, ensure_ascii=False))
    if args.apply:
        try:
            _stop_managed_processes(root)
        except (KeyError, OSError, subprocess.SubprocessError):
            print("MANAGED_PROCESS_STOP_FAILED")
            return 2
        _remove_launch_shortcuts(root)
        try:
            remove_owned_targets(root, deferred_paths=("UNINSTALL.cmd",))
        except ValueError:
            print("PATCH_MARKER_INVALID")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
