"""NapCat that exits on every start (2026-10-27: one user's, about 3500 times in a day, while the page said 正在重连):
its missing files are put back, it is reported instead of restarted every 15 s, and the bundle keeps how it ended."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import zipfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest


class _Store:
    def __init__(self) -> None:
        self.letters = []


class _Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = _Store()
        self.private_world_port = None
        self.logged = []

    def _state_root(self) -> Path:
        return self.root

    def _safe_log(self, event, **fields):
        self.logged.append((event, fields))


def _archive(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A stand-in NapCat archive, installed: (component folder, archive)."""
    from runtime.personal_chat import napcat_installer
    root = tmp_path / "personal-chat" / "napcat"
    root.mkdir(parents=True)
    archive = root / napcat_installer.NAPCAT_ASSET
    with zipfile.ZipFile(archive, "w") as made:
        made.writestr("napcat.bat", "@echo off\r\n")
        made.writestr("NapCatWinBootMain.exe", b"MZ" + b"\0" * 64)
        made.writestr("lib/NapCatWinBootHook.dll", b"MZ" + b"\1" * 64)
    monkeypatch.setattr(napcat_installer, "NAPCAT_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    component = napcat_installer._component_root(tmp_path)
    napcat_installer._safe_extract(archive, component)
    return component, archive


def test_files_a_security_program_took_are_put_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime.personal_chat import napcat_installer
    component, archive = _archive(tmp_path, monkeypatch)
    assert napcat_installer.missing_files(tmp_path) == []
    assert napcat_installer.repair_component(tmp_path) == 0
    (component / "NapCatWinBootMain.exe").unlink()
    (component / "lib" / "NapCatWinBootHook.dll").unlink()
    assert sorted(napcat_installer.missing_files(tmp_path)) == ["NapCatWinBootMain.exe", "lib/NapCatWinBootHook.dll"]
    (component / "napcat.bat").write_text("changed by NapCat", encoding="utf-8")   # present files are left alone
    assert napcat_installer.repair_component(tmp_path, download=False) == 2
    assert (component / "lib" / "NapCatWinBootHook.dll").read_bytes() == b"MZ" + b"\1" * 64
    assert (component / "napcat.bat").read_text(encoding="utf-8") == "changed by NapCat"
    # Without its verified archive nothing is fetched in the background, and nothing changes.
    (component / "NapCatWinBootMain.exe").unlink()
    archive.write_bytes(archive.read_bytes() + b"tampered")
    assert napcat_installer.repair_component(tmp_path, download=False) == 0
    assert not (component / "NapCatWinBootMain.exe").exists()


def _managed(tmp_path: Path) -> None:
    config = tmp_path / "personal-chat" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"qq": {
        "url": "ws://127.0.0.1:3001", "account": "123456789", "owner": "987654321",
        "credentials_file": str(tmp_path / "personal-chat" / "qq.dpapi"), "managed": True}}), encoding="utf-8")


class _Exited:
    """A NapCat that ended with a crash code right after it started."""
    returncode = 3221225477

    def poll(self):
        return self.returncode


def test_a_napcat_that_exits_on_every_start_is_repaired_once_then_reported_and_paced(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime.personal_chat import napcat_installer, setup
    _managed(tmp_path)
    server = _Server(tmp_path)
    started: list[object] = []
    repairs: list[bool] = []
    up = {"value": False}

    def fake_ensure(_root):
        process = _Exited()
        started.append(process)
        return process

    def fake_repair(_root, *, download=True):
        repairs.append(download)
        return 2
    monkeypatch.setattr(setup, "_NAPCAT_WATCHDOG_SECONDS", 0.005)
    monkeypatch.setattr(napcat_installer, "ensure_shell", fake_ensure)
    monkeypatch.setattr(napcat_installer, "repair_component", fake_repair)
    monkeypatch.setattr(napcat_installer, "onebot_available", lambda: False)
    monkeypatch.setattr(napcat_installer, "webui_available", lambda _root: up["value"])
    app = web.Application()
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]

    async def scenario() -> None:
        async with TestClient(TestServer(app)):
            await runtime["napcat_start_task"]
            for _ in range(400):
                await asyncio.sleep(0.005)
                if runtime.get("napcat_error") == "NAPCAT_EXITS_ON_START":
                    break
            assert runtime["napcat_state"] == "FAILED" and runtime["napcat_error"] == "NAPCAT_EXITS_ON_START"
            assert repairs == [False]                    # once, from the archive already here
            assert len(started) == setup._NAPCAT_GIVE_UP_AFTER
            # Paused: no restart every few seconds any more.
            await asyncio.sleep(0.05)
            assert len(started) == setup._NAPCAT_GIVE_UP_AFTER
            # The pause is over: one more try; it comes up, waiting for a new scan, and is no longer reported.
            up["value"] = True
            runtime["napcat_retry_at"] = 0
            for _ in range(400):
                await asyncio.sleep(0.005)
                if runtime.get("napcat_state") == "AWAITING_QQ_LOGIN":
                    break
            assert runtime["napcat_state"] == "AWAITING_QQ_LOGIN" and "napcat_error" not in runtime
            assert "napcat_quick_exits" not in runtime

    asyncio.run(scenario())
    states = [fields for event, fields in server.logged if event == "personal_chat_napcat_state"]
    assert any(item.get("reason") == "process_exit_restart" and item.get("exit_code") == 3221225477 for item in states)
    assert sum(1 for item in states if item["reason"] == "watchdog_failed") == 1   # reported once, not each try
    assert ("personal_chat_napcat_repair", {"channel": "qq", "restored": 2}) in server.logged


def test_the_bundle_keeps_how_napcat_ended() -> None:
    import local_server
    record = local_server._runtime_diagnostic_record("personal_chat_napcat_state", {
        "channel": "qq", "status": "starting", "reason": "process_exit_restart", "exit_code": 3221225477,
        "recorded_at_ms": 1793109687709})
    assert record == {"event": "personal_chat_napcat_state", "channel": "qq", "status": "starting",
                      "reason": "process_exit_restart", "exit_code": 3221225477, "recorded_at_ms": 1793109687709}
    repair = local_server._runtime_diagnostic_record("personal_chat_napcat_repair", {"channel": "qq", "restored": 2})
    assert repair == {"event": "personal_chat_napcat_repair", "channel": "qq", "restored": 2}
    odd = local_server._runtime_diagnostic_record("personal_chat_napcat_state", {"status": "Not A Status!"})
    assert odd == {"event": "personal_chat_napcat_state"}
