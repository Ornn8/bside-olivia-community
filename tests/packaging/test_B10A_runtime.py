"""B10A module, config, ownership, and local-process contract tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from runtime.packaging.b10a.cli import main
from runtime.packaging.b10a.manager import B10AManager
from runtime.packaging.b10a.manifest import DEFAULT_MANIFEST_PATH
from runtime.packaging.b10a.security import redact


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "项目 with spaces"
    project.mkdir()
    (project / "local_server.py").write_text("# test B02 file\n", encoding="utf-8")
    (project / "http_contract.py").write_text("# test B02 file\n", encoding="utf-8")
    return project, tmp_path / "B10A data root"


def _invoke(
    capsys: pytest.CaptureFixture[str],
    project: Path,
    data: Path,
    *command: str,
) -> tuple[int, dict[str, Any]]:
    code = main(["--project-root", str(project), "--data-root", str(data), *command])
    output = capsys.readouterr().out.strip()
    assert output, "B10A CLI must return one JSON result"
    return code, json.loads(output)


def _copy_manifest(tmp_path: Path, mutate: Any) -> Path:
    manifest = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    mutate(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_manifest_declares_all_modules_and_pending_boundary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project, data = _project(tmp_path)
    code, result = _invoke(capsys, project, data, "manifest")

    assert code == 0
    assert result["schema_version"] == "b10a.modules.v1"
    modules = {item["id"]: item for item in result["modules"]}
    assert set(modules) == {
        "core/http",
        "llm-api",
        "memory-local",
        "asr-local",
        "tts-local",
        "visual-driver",
        "media-original",
    }
    assert modules["core/http"]["availability"] == "available"
    assert all(modules[module_id]["availability"] == "pending" for module_id in modules if module_id != "core/http")
    assert modules["core/http"]["ownership"]["owned_paths"]
    assert modules["core/http"]["ownership"]["preserved_boundaries"]


def test_install_uninstall_roundtrip_is_idempotent_and_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project, data = _project(tmp_path)
    code, installed = _invoke(capsys, project, data, "install", "--module", "core/http")
    marker = data / "modules" / "core_http" / "marker.json"
    assert code == 0
    assert installed["status"] == "INSTALLED"
    assert marker.is_file()

    code, second_install = _invoke(capsys, project, data, "install", "--module", "core/http")
    assert code == 0
    assert second_install["status"] == "NO_OP"

    code, dry_run = _invoke(capsys, project, data, "uninstall", "--module", "core/http")
    assert code == 0
    assert dry_run["dry_run"] is True
    assert marker.is_file()

    code, removed = _invoke(capsys, project, data, "uninstall", "--module", "core/http", "--apply")
    assert code == 0
    assert removed["status"] == "UNINSTALLED"
    assert not marker.exists()

    code, reinstalled = _invoke(capsys, project, data, "install", "--module", "core/http")
    assert code == 0
    assert reinstalled["status"] == "INSTALLED"
    assert marker.is_file()


def test_uninstall_is_reversible_and_preserves_external_user_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)
    external_original = tmp_path / "original-client" / "media.bin"
    external_original.parent.mkdir()
    external_original.write_bytes(b"original-media")
    _invoke(capsys, project, data, "install", "--module", "core/http")

    code, removed = _invoke(capsys, project, data, "uninstall", "--module", "core/http", "--apply")
    assert code == 0
    assert removed["status"] == "UNINSTALLED"
    assert external_original.read_bytes() == b"original-media"

    code, restored = _invoke(capsys, project, data, "rollback", "--module", "core/http")
    assert code == 0
    assert restored["status"] == "ROLLED_BACK"
    assert (data / "modules/core_http/marker.json").is_file()


def test_uninstall_refuses_running_owned_process(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project, data = _project(tmp_path)
    _invoke(capsys, project, data, "install", "--module", "core/http")
    port = _free_port()
    code, started = _invoke(capsys, project, data, "start", "--service", "mock-http", "--port", str(port))
    assert code == 0
    try:
        code, result = _invoke(capsys, project, data, "uninstall", "--module", "core/http", "--apply")
        assert code == 2
        assert result["code"] == "PROCESS_RUNNING"
    finally:
        _invoke(capsys, project, data, "stop", "--service", "mock-http")


def test_windows_owned_process_isolated_from_runner_console_signals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, data = _project(tmp_path)
    _invoke(capsys, project, data, "install", "--module", "core/http")
    captured: dict[str, int] = {}
    real_popen = subprocess.Popen

    def capture_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        captured["creationflags"] = int(kwargs.get("creationflags", 0))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("runtime.packaging.b10a.manager.subprocess.Popen", capture_popen)
    port = _free_port()
    try:
        code, started = _invoke(capsys, project, data, "start", "--service", "mock-http", "--port", str(port))
        assert code == 0
        assert started["status"] == "RUNNING"
    finally:
        _invoke(capsys, project, data, "stop", "--service", "mock-http")

    expected = 0
    if os.name == "nt":
        expected = subprocess.CREATE_NEW_PROCESS_GROUP
    assert captured["creationflags"] == expected


def test_uninstall_refuses_unhealthy_but_still_running_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)
    _invoke(capsys, project, data, "install", "--module", "core/http")
    port = _free_port()
    code, started = _invoke(capsys, project, data, "start", "--service", "mock-http", "--port", str(port))
    assert code == 0
    state_path = data / "state.json"
    original_state = json.loads(state_path.read_text(encoding="utf-8"))
    try:
        unhealthy_state = json.loads(json.dumps(original_state))
        unhealthy_state["processes"]["mock-http"]["port"] = _free_port()
        state_path.write_text(json.dumps(unhealthy_state), encoding="utf-8")
        code, result = _invoke(capsys, project, data, "uninstall", "--module", "core/http", "--apply")
        assert code == 2
        assert result["code"] == "PROCESS_RUNNING"
    finally:
        state_path.write_text(json.dumps(original_state), encoding="utf-8")
        _invoke(capsys, project, data, "stop", "--service", "mock-http")


def test_upgrade_and_rollback_restore_owned_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project, data = _project(tmp_path)
    manifest = _copy_manifest(tmp_path, lambda value: _set_core_version(value, "b02.v1"))
    assert _invoke_with_manifest(capsys, project, data, manifest, "install", "--module", "core/http")[0] == 0

    manifest_v2 = _copy_manifest(tmp_path, lambda value: _set_core_version(value, "b02.v2"))
    code, upgraded = _invoke_with_manifest(capsys, project, data, manifest_v2, "upgrade", "--module", "core/http")
    assert code == 0
    assert upgraded["status"] == "UPGRADED"
    assert json.loads((data / "modules/core_http/marker.json").read_text(encoding="utf-8"))["version"] == "b02.v2"

    code, rolled_back = _invoke_with_manifest(capsys, project, data, manifest_v2, "rollback", "--module", "core/http")
    assert code == 0
    assert rolled_back["status"] == "ROLLED_BACK"
    assert json.loads((data / "modules/core_http/marker.json").read_text(encoding="utf-8"))["version"] == "b02.v1"


def _set_core_version(manifest: dict[str, Any], version: str) -> None:
    for module in manifest["modules"]:
        if module["id"] == "core/http":
            module["version"] = version
            return
    raise AssertionError("core/http missing")


def _invoke_with_manifest(
    capsys: pytest.CaptureFixture[str], project: Path, data: Path, manifest: Path, *command: str
) -> tuple[int, dict[str, Any]]:
    code = main([
        "--project-root",
        str(project),
        "--data-root",
        str(data),
        "--manifest",
        str(manifest),
        *command,
    ])
    output = capsys.readouterr().out.strip()
    return code, json.loads(output)


def test_pending_module_never_claims_success_and_missing_dependency_is_visible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)
    code, pending = _invoke(capsys, project, data, "install", "--module", "llm-api")
    assert code == 2
    assert pending["code"] == "MODULE_PENDING"
    assert not (data / "modules/llm-api/marker.json").exists()

    manifest = _copy_manifest(tmp_path, lambda value: _make_llm_available(value))
    code, missing = _invoke_with_manifest(capsys, project, data, manifest, "install", "--module", "llm-api")
    assert code == 2
    assert missing["code"] == "MISSING_DEPENDENCY"
    assert missing["details"]["dependencies"] == ["core/http"]


def _make_llm_available(manifest: dict[str, Any]) -> None:
    for module in manifest["modules"]:
        if module["id"] == "llm-api":
            module["availability"] = "available"
            module["version"] = "b03-test"
            return
    raise AssertionError("llm-api missing")


def test_path_escape_and_dirty_config_are_rejected_without_install_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)
    manifest = _copy_manifest(tmp_path, lambda value: _set_core_owned_path(value, "../outside.json"))
    code, escaped = _invoke_with_manifest(capsys, project, data, manifest, "install", "--module", "core/http")
    assert code == 2
    assert escaped["code"] == "PATH_ESCAPE"
    assert not (tmp_path / "outside.json").exists()

    (project / "b10a.config.json").write_text("{ dirty", encoding="utf-8")
    code, dirty = _invoke(capsys, project, data, "install", "--module", "core/http")
    assert code == 2
    assert dirty["code"] == "CONFIG_INVALID"
    assert not (data / "state.json").exists()


def _set_core_owned_path(manifest: dict[str, Any], value: str) -> None:
    for module in manifest["modules"]:
        if module["id"] == "core/http":
            module["ownership"]["owned_paths"][0] = value
            return
    raise AssertionError("core/http missing")


def test_secret_redaction_never_returns_secret_values() -> None:
    value = redact({"api_key": "super-secret-value", "message": "Authorization=Bearer abc123"})
    assert "super-secret-value" not in json.dumps(value)
    assert "abc123" not in json.dumps(value)
    assert value["api_key"] == "<redacted>"


def test_project_secret_config_is_rejected_and_env_secret_is_only_reported_as_presence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data = _project(tmp_path)
    (project / "b10a.config.json").write_text(
        json.dumps({"providers": {"llm-api": {"api_key": "project-secret"}}}), encoding="utf-8"
    )
    code, rejected = _invoke(capsys, project, data, "install", "--module", "core/http")
    assert code == 2
    assert rejected["code"] == "CONFIG_INVALID"
    assert "project-secret" not in json.dumps(rejected)

    (project / "b10a.config.json").unlink()
    monkeypatch.setenv("B10A_LLM_API_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("B10A_LLM_API_KEY", "env-secret-value")
    code, report = _invoke(capsys, project, data, "doctor")
    assert code == 0
    llm = report["config"]["providers"]["llm-api"]
    assert llm["secret_present"] is True
    assert llm["secret_source"] == "B10A_LLM_API_KEY"
    assert "env-secret-value" not in json.dumps(report)


def test_doctor_aggregates_health_and_keeps_pending_modules_degraded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)
    code, fresh = _invoke(capsys, project, data, "doctor")
    assert code == 0
    assert fresh["status"] == "DEGRADED"
    assert fresh["summary"]["pending"] == 6
    assert fresh["summary"]["not_installed"] == 1

    _invoke(capsys, project, data, "install", "--module", "core/http")
    code, healthy_core = _invoke(capsys, project, data, "doctor")
    assert code == 0
    core = next(item for item in healthy_core["modules"] if item["id"] == "core/http")
    assert core["status"] == "HEALTHY"
    assert healthy_core["status"] == "DEGRADED"


def test_port_conflict_duplicate_start_abnormal_exit_and_stop_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)
    _invoke(capsys, project, data, "install", "--module", "core/http")

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    occupied_port = int(occupied.getsockname()[1])
    try:
        code, conflict = _invoke(capsys, project, data, "start", "--service", "mock-http", "--port", str(occupied_port))
        assert code == 2
        assert conflict["code"] == "PORT_CONFLICT"
    finally:
        occupied.close()

    port = _free_port()
    code, started = _invoke(
        capsys,
        project,
        data,
        "start",
        "--service",
        "mock-http",
        "--port",
        str(port),
        "--exit-after",
        "1.0",
    )
    assert code == 0
    pid = int(started["pid"])
    try:
        code, duplicate = _invoke(capsys, project, data, "start", "--service", "mock-http", "--port", str(port))
        assert code == 2
        assert duplicate["code"] == "ALREADY_RUNNING"

        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and B10AManager(project_root=project, data_root=data)._pid_alive(pid):
            time.sleep(0.05)
        code, health = _invoke(capsys, project, data, "doctor")
        assert code == 0
        process = next(item for item in health["processes"] if item["service"] == "mock-http")
        assert process["status"] == "ABNORMAL_EXIT"

        code, stopped = _invoke(capsys, project, data, "stop", "--service", "mock-http")
        assert code == 0
        assert stopped["status"] == "ABNORMAL_EXIT"
    finally:
        state_path = data / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            record = state.get("processes", {}).get("mock-http")
            if record and B10AManager(project_root=project, data_root=data)._pid_alive(int(record["pid"])):
                _invoke(capsys, project, data, "stop", "--service", "mock-http")


@pytest.mark.parametrize("name", ["用户 数据", "space path"])
def test_windows_friendly_unicode_and_space_data_root(tmp_path: Path, name: str, capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = _project(tmp_path)
    data = tmp_path / name
    code, result = _invoke(capsys, project, data, "install", "--module", "core/http")
    assert code == 0
    assert result["status"] == "INSTALLED"
    assert (data / "state.json").is_file()
