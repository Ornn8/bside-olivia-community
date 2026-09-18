from __future__ import annotations

import asyncio
import json
from pathlib import Path

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

    def _state_root(self) -> Path:
        return self.root


def test_managed_qq_component_starts_with_olivia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import napcat_installer, setup

    config = tmp_path / "personal-chat" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "qq": {
                    "url": "ws://127.0.0.1:3001",
                    "account": "123456789",
                    "owner": "987654321",
                    "credentials_file": str(tmp_path / "personal-chat" / "qq.dpapi"),
                    "managed": True,
                }
            }
        ),
        encoding="utf-8",
    )
    server = _Server(tmp_path)
    process = object()
    calls: list[Path] = []

    def fake_ensure(root: Path):
        calls.append(root)
        return process

    monkeypatch.setattr(napcat_installer, "ensure_shell", fake_ensure)
    monkeypatch.setattr(napcat_installer, "onebot_available", lambda: True)
    app = web.Application()
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]

    async def scenario() -> None:
        async with TestClient(TestServer(app)):
            assert calls == [tmp_path]
            assert runtime["napcat_shell_process"] is process
            assert runtime["napcat_state"] == "RUNNING"

    asyncio.run(scenario())


def test_managed_qq_watchdog_restarts_dead_napcat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import napcat_installer, setup

    config = tmp_path / "personal-chat" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "qq": {
                    "url": "ws://127.0.0.1:3001",
                    "account": "123456789",
                    "owner": "987654321",
                    "credentials_file": str(tmp_path / "personal-chat" / "qq.dpapi"),
                    "managed": True,
                }
            }
        ),
        encoding="utf-8",
    )

    class Process:
        def __init__(self, alive: bool) -> None:
            self.alive = alive

        def poll(self):
            return None if self.alive else 1

    first = Process(True)
    second = Process(True)
    processes = [first, second]
    calls: list[Path] = []
    availability = {"value": True}

    def fake_ensure(root: Path):
        calls.append(root)
        return processes[min(len(calls) - 1, 1)]

    server = _Server(tmp_path)
    monkeypatch.setattr(setup, "_NAPCAT_WATCHDOG_SECONDS", 0.01)
    monkeypatch.setattr(napcat_installer, "ensure_shell", fake_ensure)
    monkeypatch.setattr(
        napcat_installer, "onebot_available", lambda: availability["value"]
    )

    app = web.Application()
    setup.install_setup_routes(app, server)
    runtime = app[setup._SETUP]

    async def scenario() -> None:
        async with TestClient(TestServer(app)):
            assert runtime["napcat_shell_process"] is first
            first.alive = False
            availability["value"] = False
            for _ in range(100):
                await asyncio.sleep(0.01)
                if runtime["napcat_shell_process"] is second:
                    break
            assert runtime["napcat_shell_process"] is second
            assert len(calls) >= 2

    asyncio.run(scenario())
