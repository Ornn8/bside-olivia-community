import json
import time

from installer import version_launcher as launcher


def _root(tmp_path):
    tmp_path.mkdir(parents=True)
    (tmp_path / "Olivia.exe").write_bytes(b"")
    (tmp_path / "data" / "logs").mkdir(parents=True)
    return tmp_path


def _fake_system(monkeypatch, tmp_path, elevated):
    system = tmp_path / "Windows"
    system.mkdir()
    (system / "explorer.exe").write_bytes(b"")
    monkeypatch.setenv("SystemRoot", str(system))
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(launcher, "_is_elevated", lambda: elevated)
    started = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, **_: started.append(command))
    # Never touch real permissions from a test.
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_, **__: type("Done", (), {"returncode": 0})())
    return started


def test_an_elevated_start_is_handed_to_explorer_once(tmp_path, monkeypatch):
    root = _root(tmp_path / "install")
    started = _fake_system(monkeypatch, tmp_path, elevated=True)
    assert launcher._relaunch_unelevated(root) is True
    assert started == [[str(tmp_path / "Windows" / "explorer.exe"), str(root / "Olivia.exe")]]
    marker = root / "data" / "logs" / launcher.ELEVATED_RELAUNCH_NAME
    assert json.loads(marker.read_text(encoding="utf-8"))["at"] <= time.time()
    # Explorer's start is elevated too when UAC is off: run as is instead of looping.
    assert launcher._relaunch_unelevated(root) is False
    assert len(started) == 1


def test_a_normal_start_is_not_touched(tmp_path, monkeypatch):
    root = _root(tmp_path / "install")
    started = _fake_system(monkeypatch, tmp_path, elevated=False)
    assert launcher._relaunch_unelevated(root) is False
    assert started == []
    assert not (root / "data" / "logs" / launcher.ELEVATED_RELAUNCH_NAME).exists()


def _state(root, version):
    (root / launcher.STATE_NAME).write_text(json.dumps(
        {"active_components": {"local_backend": {"version": version}}}), encoding="utf-8")


def _fake_icacls(monkeypatch, returncode=0):
    calls = []

    class Done:
        def __init__(self):
            self.returncode = returncode

    def run(command, **_):
        calls.append(command)
        return Done()

    monkeypatch.setattr(launcher.subprocess, "run", run)
    return calls


def test_an_elevated_start_gives_files_back_their_permissions_once_per_version(tmp_path, monkeypatch):
    root = _root(tmp_path / "install")
    (tmp_path / "runtime").mkdir()
    profile = tmp_path / "Roaming" / "miHoYo" / "Olivia-steam"
    profile.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    _state(root, "2.4.11")
    _fake_system(monkeypatch, tmp_path, elevated=True)
    calls = _fake_icacls(monkeypatch)
    assert launcher._relaunch_unelevated(root) is True
    # The window's cache in the roaming folder too: unreadable, it made the window exit at once.
    assert [command[1:] for command in calls] == [[str(root), "/reset", "/T", "/C", "/Q"],
                                                  [str(tmp_path / "runtime"), "/reset", "/T", "/C", "/Q"],
                                                  [str(profile), "/reset", "/T", "/C", "/Q"]]
    assert calls[0][0].endswith("icacls.exe")
    # A later administrator start of the same version is not slowed down again.
    (root / "data" / "logs" / launcher.ELEVATED_RELAUNCH_NAME).unlink()
    launcher._relaunch_unelevated(root)
    assert len(calls) == 3
    _state(root, "2.4.12")
    (root / "data" / "logs" / launcher.ELEVATED_RELAUNCH_NAME).unlink()
    launcher._relaunch_unelevated(root)
    assert len(calls) == 6


def test_a_normal_start_that_cannot_import_its_files_asks_once_for_a_repair(tmp_path, monkeypatch):
    root = _root(tmp_path / "install")
    _fake_system(monkeypatch, tmp_path, elevated=False)
    prompts = []
    monkeypatch.setattr(launcher, "_run_elevated", lambda executable, parameters, **_: prompts.append(parameters) or 0)
    monkeypatch.setattr(launcher, "_START_ERROR", ImportError("DLL load failed while importing _ssl"))
    assert launcher._repair_after_denied_start(root) is True
    assert prompts and prompts[0].endswith("repair-permissions")
    events = [json.loads(line)["event"] for line in (root / "data" / "logs" / "launcher.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert "permissions_repair_requested" in events
    # Retrying the broken start does not prompt again within ten minutes.
    assert launcher._repair_after_denied_start(root) is False
    assert len(prompts) == 1


def test_a_declined_prompt_or_another_failure_starts_nothing(tmp_path, monkeypatch):
    root = _root(tmp_path / "install")
    _fake_system(monkeypatch, tmp_path, elevated=False)
    monkeypatch.setattr(launcher, "_run_elevated", lambda *_, **__: None)
    monkeypatch.setattr(launcher, "_START_ERROR", PermissionError(13, "denied"))
    assert launcher._repair_after_denied_start(root) is False
    assert "permissions_repair_declined" in (root / "data" / "logs" / "launcher.jsonl").read_text(encoding="utf-8")
    other = _root(tmp_path / "other")
    monkeypatch.setattr(launcher, "_START_ERROR", ValueError("broken"))
    assert launcher._repair_after_denied_start(other) is False
    assert not (other / "data" / "logs" / launcher.PERMISSIONS_REPAIR_NAME).exists()


def test_the_repair_action_needs_an_administrator(tmp_path, monkeypatch):
    root = _root(tmp_path / "install")
    _fake_system(monkeypatch, tmp_path, elevated=False)
    assert launcher._run_main(launcher._parse_arguments(["--install-root", str(root), "repair-permissions"])) == 3


def test_startup_import_failures_name_their_module_without_text():
    error = ImportError("cannot import name 'x' from 'runtime.alpha' (C:/Users/someone/x.py)", name="runtime.alpha")
    assert launcher._startup_exception_fields(error) == {"exception_type": "ImportError", "import_module": "runtime.alpha"}
    assert launcher._startup_exception_fields(ImportError("DLL load failed while importing _ssl"))["dll_load_failed"] == "1"
    assert launcher._startup_exception_fields(PermissionError(13, "denied"))["permission_denied"] == "1"
