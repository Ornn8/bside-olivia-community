from __future__ import annotations

import os
from pathlib import Path

import pytest

from installer import uninstall
from installer.uninstall_safety import remove_owned_targets


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
    assert "local_backend" in command[4]
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
