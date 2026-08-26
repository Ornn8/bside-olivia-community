"""B10B CLI contract and fail-closed provider behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runtime.packaging.b10b.cli import build_parser, main
from runtime.packaging.b10b.errors import B10BError
from runtime.packaging.b10b.manifest import DEFAULT_MANIFEST_PATH, validate_manifest
from runtime.packaging.b10b.manager import B10BManager


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "B10B cli project"
    project.mkdir()
    (project / "local_server.py").write_text("# B02 contract\n", encoding="utf-8")
    (project / "http_contract.py").write_text("# B02 contract\n", encoding="utf-8")
    return project, tmp_path / "B10B data"


def _invoke(
    capsys: pytest.CaptureFixture[str],
    project: Path,
    data: Path,
    *command: str,
) -> tuple[int, dict[str, Any]]:
    code = main(["--project-root", str(project), "--data-root", str(data), *command])
    output = capsys.readouterr().out.strip()
    assert output, "B10B CLI must return one JSON result"
    return code, json.loads(output)


def test_cli_covers_manifest_dry_run_routing_customize_uninstall_and_rollback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)

    code, manifest = _invoke(capsys, project, data, "manifest")
    assert code == 0
    assert manifest["schema_version"] == "b10b.modules.v1"

    code, plan = _invoke(capsys, project, data, "install", "--module", "core/http", "--dry-run")
    assert code == 0
    assert plan["status"] == "DRY_RUN"
    assert not data.exists()

    code, installed = _invoke(capsys, project, data, "install", "--module", "core/http")
    assert code == 0
    assert installed["status"] == "INSTALLED"

    code, enabled = _invoke(capsys, project, data, "enable", "--module", "core/http")
    assert code == 0
    assert enabled["status"] == "ENABLED"

    code, customized = _invoke(
        capsys,
        project,
        data,
        "customize",
        "--module",
        "core/http",
        "--set",
        'route_policy={"mode":"local"}',
        "--dry-run",
    )
    assert code == 0
    assert customized["status"] == "DRY_RUN"

    code, disabled = _invoke(capsys, project, data, "disable", "--module", "core/http")
    assert code == 0
    assert disabled["status"] == "DISABLED"

    code, customized = _invoke(
        capsys,
        project,
        data,
        "customize",
        "--module",
        "core/http",
        "--set",
        'route_policy={"mode":"local"}',
    )
    assert code == 0
    assert customized["status"] == "CUSTOMIZED"

    code, dry_uninstall = _invoke(capsys, project, data, "uninstall", "--module", "core/http")
    assert code == 0
    assert dry_uninstall["status"] == "DRY_RUN"
    assert (data / "modules/core_http/marker.json").is_file()

    code, removed = _invoke(capsys, project, data, "uninstall", "--module", "core/http", "--apply")
    assert code == 0
    assert removed["status"] == "UNINSTALLED"

    code, restored = _invoke(capsys, project, data, "rollback", "--module", "core/http")
    assert code == 0
    assert restored["status"] == "ROLLED_BACK"
    assert (data / "modules/core_http/marker.json").is_file()


def test_b10b_manifest_publishes_any_local_non_root_path_contract() -> None:
    manifest = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    schema = json.loads(
        (DEFAULT_MANIFEST_PATH.parents[1] / "schemas" / "b10b.modules.schema.json").read_text(encoding="utf-8")
    )

    references = [reference for module in manifest["modules"] for reference in module["external_references"]]
    assert references
    assert {reference["drive_policy"] for reference in references if "drive_policy" in reference} == {
        "local-absolute-non-root"
    }
    assert (
        schema["$defs"]["external_reference"]["properties"]["drive_policy"]["const"]
        == "local-absolute-non-root"
    )


def test_cli_errors_are_sanitized_and_unknown_customization_is_atomic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, data = _project(tmp_path)
    code, error = _invoke(capsys, project, data, "install", "--module", "missing")
    assert code == 2
    assert error["status"] == "ERROR"
    assert error["code"] == "UNKNOWN_MODULE"
    assert not data.exists()

    _invoke(capsys, project, data, "install", "--module", "core/http")
    code, error = _invoke(
        capsys,
        project,
        data,
        "customize",
        "--module",
        "core/http",
        "--set",
        "not_declared=true",
    )
    assert code == 2
    assert error["code"] == "CUSTOMIZATION_FIELD_UNKNOWN"
    assert not (data / "modules/core_http/config.json").exists()


def test_tts_provider_missing_fails_before_any_selected_module_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data = _project(tmp_path)
    import runtime.packaging.b10b.manager as manager_module

    real_find_spec = manager_module.importlib.util.find_spec

    def missing_tts(name: str, *args: object, **kwargs: object) -> object:
        if name == "tts":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(manager_module.importlib.util, "find_spec", missing_tts)
    manager = B10BManager(project_root=project, data_root=data)
    with pytest.raises(B10BError) as exc_info:
        manager.install(["core/http", "tts-local"])
    assert exc_info.value.code == "MODULE_PROVIDER_MISSING"
    assert exc_info.value.details["module_status"] == "NOT_INSTALLED"
    assert not data.exists()


def test_manifest_validation_is_fail_closed_for_unknown_fields(tmp_path: Path) -> None:
    raw = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))

    unknown_top = json.loads(json.dumps(raw))
    unknown_top["unexpected"] = True
    with pytest.raises(B10BError) as top_error:
        validate_manifest(unknown_top)
    assert top_error.value.code == "INVALID_MANIFEST"

    unknown_module = json.loads(json.dumps(raw))
    unknown_module["modules"][0]["unexpected"] = True
    with pytest.raises(B10BError) as module_error:
        validate_manifest(unknown_module)
    assert module_error.value.code == "INVALID_MANIFEST"


def test_state_shape_is_fail_closed(tmp_path: Path) -> None:
    project, data = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data)
    manager.install(["core/http"])
    state_path = data / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["transaction_history"] = {}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(B10BError) as error:
        manager.health()
    assert error.value.code == "STATE_INVALID"


def test_cli_declares_profile_lifecycle_commands() -> None:
    parser = build_parser()
    install = parser.parse_args(["install", "--profile", "verified-local", "--reinstall"])
    assert install.profile == "verified-local"
    assert install.reinstall is True
    assert parser.parse_args(["reinstall", "--profile", "verified-local"]).command == "reinstall"
    assert parser.parse_args(["disable", "--profile", "verified-local"]).profile == "verified-local"
    assert parser.parse_args(["uninstall", "--profile", "verified-local", "--apply"]).profile == "verified-local"
    assert parser.parse_args(["rollback", "--profile", "verified-local"]).profile == "verified-local"
