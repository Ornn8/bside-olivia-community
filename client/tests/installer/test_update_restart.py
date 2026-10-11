import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from installer import version_launcher as launcher
import original_client_update_api as api


def test_spawn_escapes_old_job_and_waits_for_handshake(tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert "CreateFlags=[uint32]520" in command[-1]
        assert "ShowWindow=[uint16]0" in command[-1]
        assert kwargs["creationflags"] == launcher.subprocess.CREATE_NO_WINDOW
        payload = json.loads(kwargs["input"])
        assert 'pythonw.exe' in payload['command'] and 'update-restart' in payload['command']
        assert payload['cwd'] == str(tmp_path)
        (tmp_path / 'data/logs/restart-test.ready').touch()
        return launcher.subprocess.CompletedProcess(command, 0)

    import secrets
    monkeypatch.setattr(secrets, 'token_hex', lambda size: 'test')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    launcher.spawn_update_restart(tmp_path)
    assert not (tmp_path / 'data/logs/restart-test.ready').exists()
    assert 'update_restart_accepted' in (tmp_path / 'data/logs/launcher.jsonl').read_text()


@pytest.mark.parametrize("action", ["apply", "rollback"])
def test_restart_requires_success_and_is_one_shot(tmp_path, monkeypatch, action):
    exits = []
    monkeypatch.setattr(api, "_exit_for_restart", lambda: exits.append(True))
    package = tmp_path / "test.oliviapatch"
    package.touch()

    class Updater:
        fail = True
        restart_fail = False
        restarts = 0

        def apply(self, *args):
            return self.rollback()

        def rollback(self):
            if self.fail:
                raise RuntimeError()
            return {"status": "APPLIED", "component": "local_backend", "version": "2.2.17"}

        def restart(self):
            if self.restart_fail:
                raise OSError()
            self.restarts += 1

    async def scenario():
        updater = Updater()
        app = web.Application()
        api.mount_original_client_update_api(app, updater, trusted_origins=["https://test.example"],
                                             authorize_session=lambda token: None)
        headers = {"Origin": "https://test.example", api.CONFIRM_HEADER: "confirmed"}
        payload = {"action": action}
        if action == "apply":
            payload.update(package_path=str(package), manifest_sha256="a" * 64)
        async with TestClient(TestServer(app)) as client:
            async def restart():
                return await client.post(api.RESTART_PATH, headers=headers, json={})
            assert (await restart()).status == 409
            assert (await client.post(api.ACTION_PATH, headers=headers, json=payload)).status == 503
            assert (await restart()).status == 409
            updater.fail = False
            assert (await client.post(api.ACTION_PATH, headers=headers, json=payload)).status == 200
            assert (await client.post(api.RESTART_PATH, json={})).status == 403
            updater.restart_fail = True
            assert (await restart()).status == 503
            updater.restart_fail = False
            assert (await restart()).status == 202
            assert (await restart()).status == 409
            assert (await client.post(api.ACTION_PATH, headers=headers, json=payload)).status == 409
            assert updater.restarts == 1
            await asyncio.sleep(1.1)
            assert exits == [True]
    asyncio.run(scenario())


@pytest.mark.parametrize("exe,command,pid,expected", [
    (r"F:\安装 空格\app\Olivia.exe", "", 11, True),
    (r"F:\安装 空格2\app\Olivia.exe", "", 11, False),
    (r"F:\runtime\python-3.12.10-embed-amd64\pythonw.exe", 'pythonw "F:\\安装 空格\\launcher\\version_launcher.py"', 11, True),
    (r"F:\runtime\python-3.12.10-embed-amd64\pythonw.exe", 'pythonw "F:\\安装 空格2\\launcher\\version_launcher.py"', 11, False),
    (r"C:\Windows\python.exe", 'python "F:\\安装 空格\\launcher\\version_launcher.py"', 11, False),
    (r"C:\Windows\notepad.exe", 'notepad "F:\\安装 空格\\data\\logs\\launcher.jsonl"', 11, False),
    (r"F:\安装 空格\runtime\pythonw.exe", "", 10, False),
])
def test_installation_ownership(exe, command, pid, expected):
    assert launcher._belongs_to_installation(Path(r"F:\安装 空格"), exe, command, pid, 10) is expected


@pytest.mark.parametrize("timeout", [False, True])
def test_wait_before_timeout_kill(tmp_path, monkeypatch, timeout):
    steps = []
    clock = [0]
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(launcher, "_restart_event", lambda root, event, **kw: steps.append(event))

    class Kernel:
        killed = False
        def WaitForSingleObject(self, handle, milliseconds):
            return 0 if self.killed or (not timeout and clock[0] >= 1) else 258
        def TerminateProcess(self, handle, code):
            assert clock[0] >= 20
            steps.append("kill")
            self.killed = True
            return True
    launcher._wait_restart_processes(tmp_path, Kernel(), [(123, "retained handle")])
    assert steps == (["waiting", "timeout", "kill", "stopped"] if timeout else ["waiting", "stopped"])


def test_client_gets_short_grace_after_backend_exit(tmp_path, monkeypatch):
    steps = []
    clock = [0]
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(launcher, "_restart_event", lambda root, event, **kw: steps.append(event))

    class Kernel:
        killed = False
        def WaitForSingleObject(self, handle, milliseconds):
            # The backend exits after 1 s; the client keeps running until terminated.
            return 0 if self.killed or (handle == "backend" and clock[0] >= 1) else 258
        def TerminateProcess(self, handle, code):
            assert handle == "client" and 4 <= clock[0] < 5
            steps.append("kill")
            self.killed = True
            return True
    launcher._wait_restart_processes(tmp_path, Kernel(), [(7, "backend"), (8, "client")], 7)
    assert steps == ["waiting", "grace_elapsed", "kill", "stopped"]


def test_helper_failure_is_logged_and_visible(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(launcher, "_restart_processes", lambda root: (_ for _ in ()).throw(OSError("private detail")))
    monkeypatch.setattr(launcher, "_restart_failure_prompt", lambda: events.append("prompt"))
    assert launcher._update_restart(tmp_path, tmp_path / "ready") == 2
    log = (tmp_path / "data/logs/launcher.jsonl").read_text()
    assert "UPDATE_RESTART_HELPER_FAILED" in log
    assert "private detail" not in log
    assert events == ["prompt"]


@pytest.mark.parametrize("fail_start", [False, True])
def test_helper_orders_shutdown_before_normal_start(tmp_path, monkeypatch, fail_start):
    steps = []
    ready = tmp_path / "ready"
    log = tmp_path / "data/logs/launcher.jsonl"

    class Kernel:
        def CloseHandle(self, handle):
            steps.append("release")

    monkeypatch.setattr(launcher, "_restart_processes", lambda root: (Kernel(), [(123, "handle")]))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: ready.unlink(missing_ok=True))
    monkeypatch.setattr(launcher, "_close_restart_windows", lambda pids: steps.append("close"))
    monkeypatch.setattr(launcher, "_wait_restart_processes", lambda *args: steps.append("wait"))
    monkeypatch.setattr(launcher, "_restart_failure_prompt", lambda: steps.append("prompt"))

    class Process:
        def poll(self):
            return 2 if fail_start else None

    def start(command, **kwargs):
        assert steps == ["close", "wait"]
        assert command[-1] == "start"
        assert command[0] == str(tmp_path.parent / "runtime/python-3.12.10-embed-amd64/pythonw.exe")
        steps.append("start")
        if not fail_start:
            with log.open("a") as stream:
                stream.write('{"event": "backend_ready"}\n{"event": "client_start"}\n')
        return Process()

    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    assert launcher._update_restart(tmp_path, ready) == (2 if fail_start else 0)
    assert steps == ["close", "wait", "start", *(["prompt"] if fail_start else []), "release"]
    assert ("UPDATE_RESTART_START_FAILED" if fail_start else "update_restart_ready") in log.read_text()
