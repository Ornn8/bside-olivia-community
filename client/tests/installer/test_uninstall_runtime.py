from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from installer import uninstall
from installer.uninstall_safety import remove_owned_targets


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership is required")
def test_uninstall_process_filter_covers_versioned_backend_without_lookalikes(
    tmp_path: Path,
) -> None:
    powershell = (
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    environment = dict(os.environ)
    environment["OLIVIA_UNINSTALL_TARGET"] = os.fspath(
        tmp_path / "Olivia Local" / "install"
    )
    script = uninstall._MANAGED_PROCESS_FILTER + r"""
$versioned = Join-Path $root 'versions\local_backend\0.1.2\local_server.py'
$legacy = Join-Path $root 'local_backend\local_server.py'
$outside = Join-Path (Split-Path $root -Parent) 'outside\versions\local_backend\0.1.2\local_server.py'
$sibling = Join-Path $root 'versions\local_backend-copy\0.1.2\local_server.py'
$actual = @(
    Test-IsManagedOliviaProcess ([PSCustomObject]@{ Name = 'pythonw.exe'; ExecutablePath = ''; CommandLine = $versioned })
    Test-IsManagedOliviaProcess ([PSCustomObject]@{ Name = 'python.exe'; ExecutablePath = ''; CommandLine = $legacy })
    Test-IsManagedOliviaProcess ([PSCustomObject]@{ Name = 'pythonw.exe'; ExecutablePath = ''; CommandLine = $outside })
    Test-IsManagedOliviaProcess ([PSCustomObject]@{ Name = 'pythonw.exe'; ExecutablePath = ''; CommandLine = $sibling })
    Test-IsManagedOliviaProcess ([PSCustomObject]@{ Name = 'node.exe'; ExecutablePath = ''; CommandLine = $versioned })
)
if (($actual -join ',') -eq 'True,True,False,False,False') { exit 0 }
Write-Error ($actual -join ',')
exit 1
"""
    result = subprocess.run(
        [
            os.fspath(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    assert result.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership is required")
def test_uninstall_stops_only_the_managed_process_tree_before_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = tmp_path / "Windows"
    powershell = (
        windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    powershell.parent.mkdir(parents=True)
    powershell.touch()
    install = tmp_path / "Olivia Local" / "install"
    install.mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setenv("WINDIR", os.fspath(windows))
    monkeypatch.setattr(uninstall.subprocess, "run", run)

    uninstall._stop_managed_processes(install)

    assert len(calls) == 1
    command, options = calls[0]
    assert command[:4] == [
        os.fspath(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    assert options["env"]["OLIVIA_UNINSTALL_TARGET"] == os.fspath(install)
    assert "Get-CimInstance Win32_Process" in command[4]
    assert "versions\\local_backend" in command[4]
    assert "/PID $target.ProcessId /T /F" in command[4]
    assert "Olivia.exe" not in command[4]


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership is required")
def test_uninstall_rechecks_after_a_process_exits_during_tree_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = tmp_path / "Windows"
    powershell = (
        windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    powershell.parent.mkdir(parents=True)
    powershell.touch()
    install = tmp_path / "install"
    install.mkdir()
    returncodes = iter((1, 0))

    monkeypatch.setenv("WINDIR", os.fspath(windows))
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Result", (), {"returncode": next(returncodes)}
        )(),
    )

    uninstall._stop_managed_processes(install)


def test_standalone_uninstall_defers_only_its_running_batch_file(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "managed.txt").write_text("managed", encoding="utf-8")
    command = tmp_path / "UNINSTALL.cmd"
    command.write_text("managed", encoding="utf-8")

    remove_owned_targets(tmp_path, deferred_paths=("UNINSTALL.cmd",))

    assert not app.exists()
    assert command.read_text(encoding="utf-8") == "managed"


def test_uninstall_removes_empty_runtime_parent_but_preserves_unknown_content(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean"
    managed = clean / "runtime" / "mem0-site-packages"
    managed.mkdir(parents=True)
    (managed / "managed.txt").write_text("managed", encoding="utf-8")

    remove_owned_targets(clean)

    assert not (clean / "runtime").exists()

    mixed = tmp_path / "mixed"
    managed = mixed / "runtime" / "mem0-site-packages"
    managed.mkdir(parents=True)
    (managed / "managed.txt").write_text("managed", encoding="utf-8")
    unknown = mixed / "runtime" / "user-owned.txt"
    unknown.write_text("keep", encoding="utf-8")

    remove_owned_targets(mixed)

    assert unknown.read_text(encoding="utf-8") == "keep"
