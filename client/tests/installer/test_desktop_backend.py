from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from jsonschema import validate

import desktop_backend


def test_desktop_ready_message_matches_contract() -> None:
    message = desktop_backend.desktop_ready_message(port=49152, pid=1234, nonce="a" * 48)
    schema = json.loads(
        (Path(__file__).parents[2] / "contracts" / "desktop_handshake.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validate(message, schema)
    assert json.loads(json.dumps(message)) == message


def test_desktop_ready_message_rejects_invalid_port() -> None:
    try:
        desktop_backend.desktop_ready_message(port=0, pid=1234, nonce="nonce")
    except ValueError as exc:
        assert str(exc) == "DESKTOP_PORT_INVALID"
    else:
        raise AssertionError("invalid port was accepted")


def test_desktop_update_status_keeps_manifest_field(monkeypatch) -> None:
    monkeypatch.setenv("OLIVIA_DESKTOP_VERSION", "0.0.1")
    payload = desktop_backend.desktop_update_status()
    assert set(payload) == {"version", "manifest_sha256"}
    # The bundled release metadata wins over the shell version.
    release = json.loads(
        (Path(desktop_backend.__file__).parent / "installer" / "release-version.json").read_text(encoding="utf-8")
    )
    assert payload["version"] == release["version"]


def test_desktop_update_status_falls_back_to_shell_version(monkeypatch) -> None:
    import original_client_update_api

    monkeypatch.setattr(
        original_client_update_api,
        "running_component_version",
        lambda: {"version": None, "manifest_sha256": None},
    )
    monkeypatch.setenv("OLIVIA_DESKTOP_VERSION", "0.0.1")
    assert desktop_backend.desktop_update_status()["version"] == "0.0.1"
    monkeypatch.delenv("OLIVIA_DESKTOP_VERSION")
    assert desktop_backend.desktop_update_status()["version"] is None


@pytest.mark.parametrize("token", [None, ""])
def test_desktop_auth_refuses_to_run_without_a_token(token) -> None:
    from aiohttp import web

    app = web.Application()
    with pytest.raises(RuntimeError, match="DESKTOP_AUTH_TOKEN_REQUIRED"):
        desktop_backend.install_desktop_auth(app, token)
    assert list(app.middlewares) == []


@pytest.mark.skipif(sys.platform == "win32", reason="the Electron backend runs on macOS")
def test_data_root_lock_rejects_a_second_backend_until_the_first_exits(tmp_path) -> None:
    import subprocess
    import sys

    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys, desktop_backend; from pathlib import Path; "
            "held = desktop_backend.acquire_data_root_lock(Path(sys.argv[1])); "
            "print('locked', flush=True); sys.stdin.read()"
        ), str(tmp_path)],
        cwd=Path(desktop_backend.__file__).parent,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        try:
            desktop_backend.acquire_data_root_lock(tmp_path)
        except desktop_backend.DesktopBackendLocked:
            pass
        else:
            raise AssertionError("a second backend locked the same data root")
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    # The lock belongs to the process, so a crashed backend never blocks a restart.
    handle = desktop_backend.acquire_data_root_lock(tmp_path)
    handle.close()


@pytest.mark.skipif(sys.platform == "win32", reason="the Electron backend runs on macOS")
def test_parent_pipe_close_stops_the_backend() -> None:
    import asyncio
    import os

    async def scenario() -> None:
        read_fd, write_fd = os.pipe()
        stopped = asyncio.Event()
        try:
            desktop_backend.watch_parent_pipe(asyncio.get_running_loop(), read_fd, stopped)
            await asyncio.sleep(0.05)
            assert not stopped.is_set()
            os.close(write_fd)  # Electron exited, including SIGKILL.
            await asyncio.wait_for(stopped.wait(), timeout=2)
        finally:
            os.close(read_fd)

    asyncio.run(scenario())
