"""QQ (NapCat) stays logged in across Olivia's own restarts; a plain quit still ends it (owner 2026-10-19:
users who leave a remote computer running for QQ were pushed off by the update restarts)."""
import json
import time

from installer import start_local
from installer import version_launcher as launcher


def test_the_restart_helper_leaves_napcat_running(tmp_path):
    root = tmp_path / "install"
    napcat = root / "data" / "personal-chat" / "napcat" / "component" / "NapCat.Shell" / "QQ.exe"
    for executable in (str(napcat), str(root / "app" / "Olivia.exe")):
        assert launcher._napcat_process(root, executable) == (executable == str(napcat))
    assert launcher._belongs_to_installation(root, str(napcat), "", 7, 1) is False
    assert launcher._belongs_to_installation(root, str(root / "app" / "Olivia.exe"), "", 7, 1) is True


def test_a_start_that_replaces_a_running_one_keeps_napcat(tmp_path, monkeypatch):
    root = tmp_path / "install"
    monkeypatch.setattr(launcher, "_running_client_processes", lambda _root: [11])
    stopped = []
    monkeypatch.setattr(launcher, "_terminate_processes", stopped.extend)
    monkeypatch.setattr(launcher, "_try_acquire_start_instance", lambda _root: "lease")
    assert launcher._replace_running_start(root) == "lease"
    assert stopped == [11]
    marker = json.loads((root / "data" / "logs" / launcher.NAPCAT_KEEP_NAME).read_text(encoding="utf-8"))
    assert marker["at"] <= time.time()


def _fake_napcat(monkeypatch, stopped):
    from runtime.personal_chat import napcat_installer

    monkeypatch.setattr(start_local.os, "name", "nt")
    monkeypatch.setattr(napcat_installer, "find_shell", lambda _data: "shell")
    monkeypatch.setattr(napcat_installer, "stop_shell", lambda data, *_: stopped.append(data))


def test_a_plain_quit_ends_napcat(tmp_path, monkeypatch):
    stopped = []
    _fake_napcat(monkeypatch, stopped)
    start_local._stop_napcat_unless_kept(tmp_path, tmp_path / "data")
    assert stopped == [tmp_path / "data"]


def test_a_restart_marker_keeps_napcat_once(tmp_path, monkeypatch):
    stopped = []
    _fake_napcat(monkeypatch, stopped)
    marker = tmp_path / "data" / "logs" / launcher.NAPCAT_KEEP_NAME
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    start_local._stop_napcat_unless_kept(tmp_path, tmp_path / "data")
    assert stopped == [] and not marker.exists()
    # A stale marker (no restart followed) does not keep QQ running after a quit.
    marker.write_text(json.dumps({"at": time.time() - 600}), encoding="utf-8")
    start_local._stop_napcat_unless_kept(tmp_path, tmp_path / "data")
    assert stopped == [tmp_path / "data"]
