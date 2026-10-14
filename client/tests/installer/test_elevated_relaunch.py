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
