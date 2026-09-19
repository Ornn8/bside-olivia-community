from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest

from installer import build_windows_setup, uninstall, version_launcher
from installer import proactive_login


def _write_settings(root: Path, **values: object) -> Path:
    path = root / "data" / "proactive" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_login_worker_is_quiet_and_disabled_by_default(tmp_path: Path) -> None:
    calls: list[Path] = []

    def scan(*args, **kwargs):
        calls.append(args[0])
        raise AssertionError("disabled login worker must not import or call scanner")

    assert proactive_login.run(tmp_path, scan=scan) == 0
    assert calls == []
    assert not (tmp_path / "data").exists()


def test_login_worker_calls_file_scanner_every_interval_until_disabled(
    tmp_path: Path,
) -> None:
    settings = _write_settings(
        tmp_path,
        enabled=True,
        login_check_enabled=True,
    )
    calls: list[tuple[Path, float, float]] = []

    def scan(data_root: Path, *, now: float) -> dict:
        calls.append((data_root, now, 1.0))
        return {}

    def sleep(seconds: float) -> None:
        assert seconds == proactive_login.CHECK_INTERVAL_SECONDS
        settings.write_text(
            json.dumps({"enabled": True, "login_check_enabled": False}),
            encoding="utf-8",
        )

    assert proactive_login.run(tmp_path, clock=lambda: 42.0, sleep=sleep, scan=scan) == 0
    assert calls == [(tmp_path / "data", 42.0, 1.0)]
    assert not (tmp_path / "state.json").exists()


def test_login_worker_retries_transient_file_scanner_errors(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path, enabled=True, login_check_enabled=True)
    attempts = 0

    def scan(_data_root: Path, *, now: float) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic snapshot race")
        settings.write_text(
            json.dumps({"enabled": True, "login_check_enabled": False}),
            encoding="utf-8",
        )
        return {}

    assert proactive_login.run(tmp_path, clock=lambda: 42.0, sleep=lambda _: None, scan=scan) == 0
    assert attempts == 2


def test_windows_login_command_uses_existing_embedded_launcher_shape(tmp_path: Path) -> None:
    command = proactive_login.windows_login_command(tmp_path)
    assert "python-3.12.10-embed-amd64" in command
    assert "pythonw.exe" in command
    assert "launcher" in command
    assert "version_launcher.py" in command
    assert "--install-root" in command
    assert command.endswith(" proactive-login")


def test_windows_registration_writes_only_own_run_value(monkeypatch, tmp_path: Path) -> None:
    values: dict[str, str] = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def create_key(*_args):
        return Key()

    def open_key(*_args):
        if not values:
            raise FileNotFoundError
        return Key()

    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_SET_VALUE=1,
        REG_SZ=2,
        CreateKeyEx=create_key,
        OpenKey=open_key,
        SetValueEx=lambda _key, name, _reserved, _kind, value: values.__setitem__(
            name, value
        ),
        DeleteValue=lambda _key, name: values.pop(name),
    )
    monkeypatch.setattr(proactive_login.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    install_root = tmp_path / "install"
    data_root = install_root / "data"
    (install_root / "launcher").mkdir(parents=True)
    (install_root / "launcher" / "version_launcher.py").write_text("", encoding="utf-8")
    pythonw = install_root.parent / "runtime" / proactive_login.EMBEDDED_RUNTIME_DIR / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"")
    command = proactive_login.configure_login_start(
        data_root,
        enabled=True,
        launcher=install_root / "launcher" / "version_launcher.py",
    )
    assert command == values[proactive_login.RUN_VALUE_NAME]
    assert "proactive-login" in command
    assert proactive_login.configure_login_start(data_root, enabled=False) is None
    assert values == {}


def test_configure_login_start_refuses_uninstalled_source_tree(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(proactive_login.os, "name", "nt")
    with pytest.raises(RuntimeError, match="PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE"):
        proactive_login.configure_login_start(tmp_path / "data", enabled=True)


def test_start_login_worker_uses_verified_hidden_installed_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_root = tmp_path / "install"
    data_root = install_root / "data"
    launcher = install_root / "launcher" / "version_launcher.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    pythonw = install_root.parent / "runtime" / proactive_login.EMBEDDED_RUNTIME_DIR / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"")
    powershell = tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return proactive_login.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(proactive_login.os, "name", "nt")
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    monkeypatch.setattr(proactive_login.subprocess, "run", run)
    monkeypatch.setattr(
        proactive_login.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker must be created by CIM, not as an app child")
        ),
    )

    assert proactive_login.start_login_worker(
        data_root,
        launcher=launcher,
    ) is not None
    command, options = calls[0]
    assert command[:6] == [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
    ]
    assert command[6] == "-EncodedCommand"
    script = base64.b64decode(command[7]).decode("utf-16le")
    assert "Invoke-CimMethod" in script
    assert "Win32_Process" in script
    assert str(pythonw) not in script
    assert str(launcher) not in script
    assert options["shell"] is False
    assert options["cwd"] == str(install_root.resolve())
    assert options["stdin"] is proactive_login.subprocess.DEVNULL
    assert options["stdout"] is proactive_login.subprocess.DEVNULL
    assert options["stderr"] is proactive_login.subprocess.DEVNULL


def test_start_login_worker_rejects_failed_wmi_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_root = tmp_path / "install"
    launcher = install_root / "launcher" / "version_launcher.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    pythonw = install_root.parent / "runtime" / proactive_login.EMBEDDED_RUNTIME_DIR / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"")
    powershell = tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"")

    monkeypatch.setattr(proactive_login.os, "name", "nt")
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    monkeypatch.setattr(
        proactive_login.subprocess,
        "run",
        lambda *_args, **_kwargs: proactive_login.subprocess.CompletedProcess([], 1),
    )

    with pytest.raises(RuntimeError, match="PROACTIVE_LOGIN_WORKER_START_FAILED"):
        proactive_login.start_login_worker(
            install_root / "data",
            launcher=launcher,
        )


def test_start_login_worker_refuses_uninstalled_runtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(proactive_login.os, "name", "nt")
    with pytest.raises(RuntimeError, match="PROACTIVE_LOGIN_RUNTIME_UNAVAILABLE"):
        proactive_login.start_login_worker(tmp_path / "data")


def test_old_component_updater_path_refreshes_stable_launcher_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_root = tmp_path / "install"
    manifest_sha256 = "1" * 64
    active_backend = (
        install_root
        / "versions"
        / "local_backend"
        / f"1.2.0-{manifest_sha256}"
    )
    source_launcher = active_backend / "installer" / "version_launcher.py"
    source_launcher.parent.mkdir(parents=True)
    source_launcher.write_bytes(Path(version_launcher.__file__).read_bytes())
    (install_root / "local_backend").mkdir(parents=True)
    (install_root / "launcher").mkdir(parents=True)
    stable_launcher = install_root / "launcher" / "version_launcher.py"
    stable_launcher.write_text("old 1.1.5 launcher", encoding="utf-8")
    (install_root / ".olivia-full-patch.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.full-patch.install.v2",
                "owned_root": str(install_root.resolve()),
            }
        ),
        encoding="utf-8",
    )
    state = {
        "schema_version": "olivia.update-state.v1",
        "active_components": {
            "local_backend": {
                "version": "1.2.0",
                "manifest_sha256": manifest_sha256,
                "payload_path": f"versions/local_backend/1.2.0-{manifest_sha256}",
            }
        },
        "previous_components": {},
    }
    (install_root / ".olivia-update-state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    pythonw = (
        install_root.parent
        / "runtime"
        / proactive_login.EMBEDDED_RUNTIME_DIR
        / "pythonw.exe"
    )
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"")
    powershell = tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"")
    calls: list[list[str]] = []

    monkeypatch.setattr(proactive_login.os, "name", "nt")
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    monkeypatch.setattr(
        proactive_login.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or proactive_login.subprocess.CompletedProcess(command, 0),
    )

    proactive_login.start_login_worker(install_root / "data")

    assert stable_launcher.read_bytes() == source_launcher.read_bytes()
    assert calls == [
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            calls[0][-1],
        ]
    ]


def test_uninstall_disables_scanner_without_deleting_user_data(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path, enabled=True, login_check_enabled=True, allow_voice=True)
    sentinel = tmp_path / "data" / "letters.json"
    sentinel.write_text("keep", encoding="utf-8")

    assert uninstall._disable_proactive_login(tmp_path) is True
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "allow_voice": True,
        "enabled": False,
        "login_check_enabled": False,
    }
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_uninstall_removes_only_proactive_run_value_without_real_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from installer import proactive_login as login_module

    calls: list[bool] = []
    monkeypatch.setattr(uninstall.os, "name", "nt")
    monkeypatch.setattr(
        login_module,
        "unregister_windows_login",
        lambda: calls.append(True) or True,
    )

    uninstall._remove_proactive_login_registration()
    assert calls == [True]
    assert "proactive-login" in uninstall._MANAGED_PROCESS_FILTER


def test_version_launcher_resolves_proactive_entrypoint(monkeypatch, tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    entrypoint = backend / "installer" / "proactive_login.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("", encoding="utf-8")
    monkeypatch.setattr(version_launcher, "resolve_active_backend", lambda _root: backend)
    monkeypatch.setattr(version_launcher, "_is_reparse_point", lambda _path: False)

    resolved, arguments = version_launcher._entrypoint_arguments("proactive-login", tmp_path)
    assert resolved == entrypoint
    assert arguments == ["--install-root", str(tmp_path.resolve())]


def test_proactive_entrypoint_is_in_windows_payload_manifests() -> None:
    relative = "installer/proactive_login.py"
    assert relative in build_windows_setup.RELEASE_INSTALLER_FILES
