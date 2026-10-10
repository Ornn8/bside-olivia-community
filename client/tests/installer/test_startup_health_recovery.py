"""Exercise startup recovery over real loopback HTTP without launching Olivia."""
import asyncio
import hashlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from installer import start_local


def payload(identity="synthetic-owned"):
    return {"code": 0, "message": "ok", "data": {
        "schema_version": 1, "contract_version": "b02.v1", "profile": "core",
        "status": "HEALTHY", "backend_id": identity,
        "required_checks": {name: "available" for name in (
            "core.health", "core.session", "letters.read", "music.catalog")}}}


@contextmanager
def listener(modes, *, identity="synthetic-owned"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            mode = modes[min(self.server.calls, len(modes) - 1)]
            self.server.calls += 1
            self.server.paths.append(self.path)
            if mode == "timeout":
                time.sleep(1.7)
            self.send_response(503 if mode == "busy" else 200)
            body = json.dumps({} if mode in {"busy", "foreign"} else payload(identity)).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.calls, server.paths = 0, []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def wait_for_owned(monkeypatch, tmp_path, port, *, exited=False, virtual_time=False):
    child = SimpleNamespace(poll=lambda: 1 if exited else None)
    monkeypatch.setattr(start_local.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(start_local, "_backend_executable", lambda: Path("never-executed"))
    monkeypatch.setattr(start_local, "_BACKEND_START_TIMEOUT_SECONDS", 2)
    if virtual_time:
        clock = [0.0]
        monkeypatch.setattr(start_local, "time", SimpleNamespace(
            time=time.time, monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    return start_local._start_backend_server(
        backend=tmp_path, entrypoint=tmp_path / "never-executed.py", environment={},
        port=port, data_root=tmp_path / "data", expected_backend_id="synthetic-owned")


def test_ready_and_identity_use_one_response_without_a_failing_duplicate(monkeypatch, tmp_path):
    with listener(["healthy", "healthy", "busy"]) as http:
        child, health = wait_for_owned(monkeypatch, tmp_path, http.server_port)
        assert child.poll() is None
        assert health == "READY"
        assert http.calls == 1


@pytest.mark.parametrize("mode", ["busy", "timeout"])
def test_transient_probe_failure_recovers_within_the_startup_budget(monkeypatch, tmp_path, mode):
    with listener([mode, "healthy"]) as http:
        child, health = wait_for_owned(monkeypatch, tmp_path, http.server_port)
        assert child.poll() is None and health == "READY"
        assert http.calls == 2


def test_exhausted_busy_budget_is_unavailable_and_records_a_finite_reason(monkeypatch, tmp_path):
    with listener(["busy"]) as http:
        child, health = wait_for_owned(monkeypatch, tmp_path, http.server_port, virtual_time=True)
        assert child.poll() is None and health == "UNAVAILABLE"
    rows = [json.loads(line) for line in (tmp_path / "data/logs/launcher.jsonl").read_text().splitlines()]
    assert rows[-1]["reason"] == "startup_timeout"


@pytest.mark.parametrize("mode,identity", [("foreign", "synthetic-owned"), ("healthy", "someone-else")])
def test_foreign_listener_is_rejected_without_waiting_out_the_budget(monkeypatch, tmp_path, mode, identity):
    with listener([mode], identity=identity) as http:
        _child, health = wait_for_owned(monkeypatch, tmp_path, http.server_port, virtual_time=True)
        assert health == "PORT_CONFLICT"
        assert http.calls == 1


def test_exited_backend_cannot_adopt_a_healthy_listener(monkeypatch, tmp_path):
    with listener(["healthy"]) as http:
        _child, health = wait_for_owned(monkeypatch, tmp_path, http.server_port, exited=True)
        assert health == "UNAVAILABLE"
        assert http.calls == 0


def test_local_health_and_identity_bypass_an_inherited_proxy():
    with listener(["healthy"]) as target, listener(["busy"]) as trap:
        environment = {key: value for key, value in os.environ.items()
                       if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
        environment.update({"HTTP_PROXY": f"http://127.0.0.1:{trap.server_port}", "NO_PROXY": ""})
        program = ("import json; from installer import start_local; "
                   f"print(json.dumps([start_local._health({target.server_port}), "
                   f"start_local._server_backend_id({target.server_port})]))")
        completed = subprocess.run([sys.executable, "-B", "-c", program], env=environment,
                                   capture_output=True, text=True, timeout=15,
                                   cwd=Path(start_local.__file__).resolve().parents[1])
        assert completed.returncode == 0
        assert json.loads(completed.stdout) == ["READY", "synthetic-owned"]
        assert target.calls == 2 and trap.calls == 0


def test_startup_core_probe_skips_optional_status_collection(monkeypatch):
    import local_server

    def unavailable_optional():
        pytest.fail("startup readiness must not probe optional ASR/memory/provider state")

    monkeypatch.setattr(local_server, "_asr_health", unavailable_optional)
    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "core", "probe": "startup"}))
    assert result["code"] == 0
    assert result["data"]["status"] == "HEALTHY"
    assert result["data"]["backend_id"] == "legacy"
    assert set(result["data"]["required_checks"]) == set(start_local._CORE_HEALTH_REQUIRED_CHECKS)


def test_startup_probe_does_not_override_an_invalid_profile():
    import local_server
    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "unknown", "probe": "startup"}))
    assert result["code"] != 0


def test_an_unknown_busy_listener_is_not_stopped_or_given_a_second_backend(monkeypatch, tmp_path, capsys):
    clock = [0.0]
    monkeypatch.setattr(start_local, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    monkeypatch.setattr(start_local.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("duplicate backend"))
    monkeypatch.setattr(start_local, "_stop_stale_backend", lambda *args: pytest.fail("unverified termination"))
    with listener(["busy"]) as http:
        result = start_local._launch(SimpleNamespace(port=http.server_port), tmp_path, tmp_path,
                                     tmp_path / "unused.py", tmp_path / "data")
    assert result == 2
    assert capsys.readouterr().out.strip() == "LOCAL_SERVER_UNRESPONSIVE"
    assert clock[0] == 15


def test_frontend_phase_transient_failure_does_not_restart_a_live_owned_backend(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(start_local, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    with listener(["busy", "healthy"]) as http:
        child = SimpleNamespace(poll=lambda: None)
        health, _reason = start_local._wait_owned_backend(child, http.server_port, "synthetic-owned", 15)
        assert health == "READY" and http.calls == 2


def test_startup_core_reports_failure_of_a_required_capability(monkeypatch):
    import local_server
    import http_contract
    monkeypatch.setitem(http_contract.CAPABILITIES, "core.session", {"status": "unavailable"})
    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "core", "probe": "startup"}))
    assert result["data"]["status"] == "FAILED"
    assert result["data"]["required_checks"]["core.session"] == "unavailable"


def test_actual_http_handler_serves_the_light_probe_when_optional_status_raises(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    import local_server

    monkeypatch.setattr(local_server, "_asr_health", lambda: pytest.fail("optional probe"))
    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            return await asyncio.to_thread(start_local._probe_health, client.server.port)
    assert asyncio.run(exercise()) == ("READY", "legacy")


@pytest.mark.parametrize("stage", ["first_readiness", "after_frontend"])
def test_launch_reaches_the_client_once_without_replacing_a_transiently_busy_backend(monkeypatch, tmp_path, stage):
    from installer import patch_local_login
    root = tmp_path / "installed"
    backend = root / "local_backend"
    (backend / "installer").mkdir(parents=True)
    for name in ("original_client_server.py", "local_server.py"):
        (backend / name).write_text("# never executed", encoding="utf-8")
    (backend / "installer/full-patch-manifest.json").write_text(
        json.dumps({"client_version": "0.0.9.627"}), encoding="utf-8")
    client = root / "app/0.0.9.627/Olivia.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"never executed")
    original = b"synthetic-prefix" + bytes.fromhex("74 5b") + b"synthetic-tail"
    patched = original.replace(bytes.fromhex("74 5b"), bytes.fromhex("eb 5b"))
    plugin = root / patch_local_login._RELATIVE
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(original)
    monkeypatch.setattr(patch_local_login, "_SIZE_BYTES", len(original))
    monkeypatch.setattr(patch_local_login, "_OFFSET", original.index(bytes.fromhex("74 5b")))
    monkeypatch.setattr(patch_local_login, "_ORIGINAL_SHA256", hashlib.sha256(original).hexdigest())
    monkeypatch.setattr(patch_local_login, "_PATCHED_SHA256", hashlib.sha256(patched).hexdigest())
    events = []
    modes = ["healthy", "healthy", "busy"] if stage == "first_readiness" else ["healthy"]
    monkeypatch.setenv("OLIVIA_STARTUP_VIDEO", str(tmp_path / "missing-animation"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(start_local, "_active_backend", lambda: backend)
    monkeypatch.setattr(start_local, "_repair_native_navigation", lambda *_args: "PATCHED")
    monkeypatch.setattr(start_local, "_run_client_with_native_layout",
                        lambda *_args, **_kwargs: events.append("client_start") or 0)
    real_probe = start_local._probe_health
    real_bindable = start_local._port_is_bindable
    # The loopback fixture exists early; model its absence until Popen owns it.
    monkeypatch.setattr(start_local, "_port_is_bindable",
                        lambda port: real_bindable(port) if events else True)
    class Child:
        exited = False
        def poll(self):
            return 0 if self.exited else None
        def terminate(self):
            self.exited = True
            events.append("terminate")
        def wait(self, **_kwargs):
            return 0
    def spawn(*_args, **_kwargs):
        events.append("backend_start")
        return Child()
    monkeypatch.setattr(start_local.subprocess, "Popen", spawn)
    monkeypatch.setattr(start_local, "_probe_health",
                        lambda port: real_probe(port) if events else ("UNAVAILABLE", None))
    def frontend(*_args):
        if stage == "after_frontend":
            modes[:] = ["healthy"] * http.calls + ["busy", "healthy"]
        return "PATCHED"
    monkeypatch.setattr(start_local, "_repair_client_frontend", frontend)
    with listener(modes, identity=start_local._backend_id(backend, root.resolve())) as http:
        result = start_local.main(["--install-root", str(root), "--port", str(http.server_port)])
    assert result == 0
    assert plugin.read_bytes() == patched
    assert events == ["backend_start", "client_start", "terminate"]
    log = (root / "data/logs/launcher.jsonl").read_text(encoding="utf-8")
    assert '"event": "backend_replaced"' not in log
