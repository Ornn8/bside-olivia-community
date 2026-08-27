"""B10B modular lifecycle behavior through the public manager interface."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from runtime.packaging.b10b.errors import B10BError
from runtime.packaging.b10b.manifest import DEFAULT_MANIFEST_PATH, load_manifest
from runtime.packaging.b10b.manager import B10BManager
from runtime.packaging.b10b.profiles import verified_local_provenance
from runtime.packaging.b10b.security import is_external_reference


def _verified_profile_settings() -> dict[str, dict[str, object]]:
    return {
        "asr-local": {
            "provider": "nemotron-speech-cpp",
            "server_url": "ws://127.0.0.1:18081",
            "runtime_root": "F:/verified/asr/runtime",
            "runtime_executable": "F:/verified/asr/nemo-speech.exe",
            "model_root": "F:/verified/asr/models",
            "model_path": "F:/verified/asr/models/nemotron.gguf",
            "cache_root": "F:/verified/asr/cache",
        },
        "tts-local": {
            "provider": "cosyvoice3",
            "runtime_root": "F:/verified/tts/runtime",
            "model_dir": "F:/verified/tts/model",
            "reference_audio": "F:/verified/tts/reference.wav",
            "reference_text": "verified reference",
            "fallback": "text",
            "fp16": True,
        },
        "visual-livetalking": {
            "runtime_root": "D:/verified/livetalking",
            "python_executable": "D:/verified/livetalking/venv/Scripts/python.exe",
            "checkpoint_path": "D:/verified/livetalking/models/wav2lip.pth",
            "checkpoint_sha256": "a" * 64,
            "checkpoint_url": "https://example.invalid/checkpoint",
            "checkpoint_revision": "verified checkpoint revision",
            "checkpoint_license": "verified checkpoint license",
            "avatar_payload": "D:/verified/livetalking/data/avatars/olivia_b11",
            "avatar_id": "olivia_b11",
            "original_reference": "D:/verified/original.mp4",
            "work_root": "F:/verified/work",
            "upstream_source": "https://github.com/lipku/LiveTalking",
            "upstream_revision": "a97f01ba366e55eeed94e88d6bae38ed77b3a1b9",
            "upstream_license": "Apache-2.0",
        },
    }


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Bside product with spaces"
    project.mkdir()
    (project / "local_server.py").write_text("# synthetic B02 contract\n", encoding="utf-8")
    (project / "http_contract.py").write_text("# synthetic B02 contract\n", encoding="utf-8")
    return project, tmp_path / "external data root"


def _manifest_with_upstream_value(tmp_path: Path, upstream_id: str, field: str, value: str) -> Path:
    manifest = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    upstream = next(item for item in manifest["provenance"]["upstreams"] if item["id"] == upstream_id)
    upstream[field] = value
    path = tmp_path / f"{upstream_id}-{field}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_install_dry_run_then_apply_is_idempotent_and_exact(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    marker = data_root / "modules" / "core_http" / "marker.json"

    dry_run = manager.install(["core/http"], dry_run=True)
    assert dry_run["status"] == "DRY_RUN"
    assert dry_run["dry_run"] is True
    assert not data_root.exists()

    installed = manager.install(["core/http"], dry_run=False)
    assert installed["status"] == "INSTALLED"
    assert marker.is_file()
    assert (data_root / "state.json").is_file()

    repeated = manager.install(["core/http"], dry_run=False)
    assert repeated["status"] == "NO_OP"
    assert repeated["modules"][0]["status"] == "NO_OP"


def test_enable_disable_only_changes_routing_and_respects_dependencies(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "asr-local"])
    core_marker = data_root / "modules/core_http/marker.json"
    asr_marker = data_root / "modules/asr_local/marker.json"
    core_before = core_marker.read_bytes()
    asr_before = asr_marker.read_bytes()

    assert manager.enable("core/http")["status"] == "ENABLED"
    assert manager.enable("asr-local")["status"] == "ENABLED"
    with pytest.raises(B10BError, match="depend") as exc_info:
        manager.disable("core/http")
    assert exc_info.value.code == "ENABLED_DEPENDENTS"

    assert manager.disable("asr-local")["status"] == "DISABLED"
    assert manager.disable("core/http")["status"] == "DISABLED"
    assert manager.disable("core/http")["status"] == "NO_OP"
    assert core_marker.is_file() and asr_marker.is_file()
    assert core_marker.read_bytes() == core_before
    assert asr_marker.read_bytes() == asr_before


def test_uninstall_is_exact_dry_run_safe_and_rollback_preserves_user_data(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http"], dry_run=False)
    user_db = data_root / "user-data" / "legacy-letters.sqlite"
    user_db.parent.mkdir(parents=True)
    user_db.write_bytes(b"legacy user data")
    external_asset = tmp_path / "original-client" / "olivia.png"
    external_asset.parent.mkdir()
    external_asset.write_bytes(b"original asset")

    dry_run = manager.uninstall(["core/http"], dry_run=True)
    assert dry_run["status"] == "DRY_RUN"
    assert dry_run["dry_run"] is True
    assert (data_root / "modules/core_http/marker.json").is_file()

    removed = manager.uninstall(["core/http"], dry_run=False)
    assert removed["status"] == "UNINSTALLED"
    assert not (data_root / "modules/core_http/marker.json").exists()
    assert user_db.read_bytes() == b"legacy user data"
    assert external_asset.read_bytes() == b"original asset"

    restored = manager.rollback("core/http")
    assert restored["status"] == "ROLLED_BACK"
    assert (data_root / "modules/core_http/marker.json").is_file()


def test_uninstall_failure_restores_owned_files_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http"], dry_run=False)
    marker = data_root / "modules/core_http/marker.json"
    before_marker = marker.read_bytes()
    before_state = (data_root / "state.json").read_bytes()

    def fail_transaction(**_kwargs: object) -> str:
        raise B10BError("WRITE_FAILED", "synthetic transaction failure")

    monkeypatch.setattr(manager, "_write_transaction", fail_transaction)
    with pytest.raises(B10BError) as exc_info:
        manager.uninstall(["core/http"], dry_run=False)
    assert exc_info.value.code == "WRITE_FAILED"
    assert marker.read_bytes() == before_marker
    assert (data_root / "state.json").read_bytes() == before_state


def test_install_failure_restores_owned_files_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    marker = data_root / "modules/core_http/marker.json"
    state = data_root / "state.json"

    def fail_transaction(**_kwargs: object) -> str:
        raise B10BError("WRITE_FAILED", "synthetic transaction failure")

    monkeypatch.setattr(manager, "_write_transaction", fail_transaction)
    with pytest.raises(B10BError) as exc_info:
        manager.install(["core/http"], dry_run=False)
    assert exc_info.value.code == "WRITE_FAILED"
    assert not marker.exists()
    assert not state.exists()


def test_recovery_failure_is_explicit_and_cannot_leave_state_claiming_a_missing_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http"], dry_run=False)
    marker = data_root / "modules/core_http/marker.json"

    def fail_transaction(**_kwargs: object) -> str:
        raise B10BError("WRITE_FAILED", "synthetic transaction failure")

    def fail_recovery(*_args: object, **_kwargs: object) -> None:
        raise B10BError("DELETE_FAILED", "synthetic recovery failure")

    monkeypatch.setattr(manager, "_write_transaction", fail_transaction)
    monkeypatch.setattr(manager, "_restore_transition", fail_recovery)
    with pytest.raises(B10BError) as exc_info:
        manager.uninstall(["core/http"], dry_run=False)

    assert exc_info.value.code == "ATOMIC_RECOVERY_FAILED"
    assert not marker.exists()
    state = json.loads((data_root / "state.json").read_text(encoding="utf-8"))
    assert "core/http" not in state["modules"]
    health = manager.health()
    module = next(item for item in health["modules"] if item["id"] == "core/http")
    assert module["status"] == "NOT_INSTALLED"


def test_health_rejects_a_registered_module_without_its_marker(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http"], dry_run=False)
    (data_root / "modules/core_http/marker.json").unlink()

    with pytest.raises(B10BError) as exc_info:
        manager.health()
    assert exc_info.value.code == "STATE_INVALID"


def test_health_rejects_transaction_history_that_does_not_load(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http"], dry_run=False)
    state_path = data_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["transaction_history"][0]["transaction"] = "transactions/missing.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(B10BError) as exc_info:
        manager.health()
    assert exc_info.value.code == "STATE_INVALID"


def test_health_rejects_last_transaction_for_a_different_module(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "asr-local"], dry_run=False)
    state_path = data_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["last_transactions"]["core/http"] = state["last_transactions"]["asr-local"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(B10BError) as exc_info:
        manager.health()
    assert exc_info.value.code == "STATE_INVALID"


def test_customize_revalidates_sensitive_fields_in_existing_config(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "asr-local"], dry_run=False)
    config_path = data_root / "modules/asr_local/config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "b10b.module-config.v1",
                "module_id": "asr-local",
                "settings": {"provider": "text-fallback", "api_key": "secret"},
                "external_assets_copied": False,
                "managed_by": "B10B lifecycle",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(B10BError) as exc_info:
        manager.customize("asr-local", {"provider": "text-fallback"})
    assert exc_info.value.code == "CONFIG_INVALID"


def test_customize_accepts_local_c_drive_and_revalidates_unsafe_existing_config(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "asr-local"], dry_run=False)
    config_path = data_root / "modules/asr_local/config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "b10b.module-config.v1",
                "module_id": "asr-local",
                "settings": {"provider": "text-fallback", "model_root": "C:/allowed/models"},
                "external_assets_copied": False,
                "managed_by": "B10B lifecycle",
            }
        ),
        encoding="utf-8",
    )

    result = manager.customize("asr-local", {"provider": "text-fallback"})
    assert result["status"] == "CUSTOMIZED"

    config_path.write_text(
        json.dumps(
            {
                "schema_version": "b10b.module-config.v1",
                "module_id": "asr-local",
                "settings": {"provider": "text-fallback", "model_root": "C:relative/models"},
                "external_assets_copied": False,
                "managed_by": "B10B lifecycle",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(B10BError) as exc_info:
        manager.customize("asr-local", {"provider": "text-fallback"})
    assert exc_info.value.code == "CONFIG_INVALID"


def test_health_revalidates_existing_config_before_provider_status(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "asr-local"], dry_run=False)
    manager.enable("core/http")
    manager.enable("asr-local")
    config_path = data_root / "modules/asr_local/config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "b10b.module-config.v1",
                "module_id": "asr-local",
                "settings": {"provider": "text-fallback", "model_root": "C:relative/models"},
                "external_assets_copied": False,
                "managed_by": "B10B lifecycle",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(B10BError) as exc_info:
        manager.health()
    assert exc_info.value.code == "CONFIG_INVALID"


def test_health_is_truthful_for_b05_b07_and_unmerged_b06(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "asr-local", "visual-driver"], dry_run=False)
    manager.enable("core/http")
    manager.enable("asr-local")
    manager.enable("visual-driver")

    report = manager.health()
    modules = {item["id"]: item for item in report["modules"]}
    assert report["schema_version"] == "b10b.health.v1"
    assert report["status"] == "DEGRADED"
    assert modules["core/http"]["status"] == "HEALTHY"
    assert modules["asr-local"]["status"] in {"DEGRADED", "UNAVAILABLE"}
    assert modules["visual-driver"]["status"] == "DEGRADED"
    assert modules["tts-local"]["status"] == "NOT_INSTALLED"
    assert report["preservation"] == {
        "external_assets_copied": False,
        "user_data_deleted": False,
        "legacy_letters_mutated": False,
    }


def test_customize_validates_external_references_without_copying_them(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    manager.install(["core/http", "asr-local"], dry_run=False)

    plan = manager.customize(
        "asr-local",
        {
            "provider": "nemotron-speech-cpp",
            "runtime_root": "D:/Bside/external/asr/runtime",
            "model_root": "F:/Bside/external/asr/models",
            "cache_root": "D:/Bside/external/asr/cache",
        },
        dry_run=True,
    )
    assert plan["status"] == "DRY_RUN"
    assert not (data_root / "modules/asr_local/config.json").exists()

    applied = manager.customize(
        "asr-local",
        {
            "provider": "text-fallback",
            "runtime_root": "D:/Bside/external/asr/runtime",
            "model_root": "F:/Bside/external/asr/models",
            "cache_root": "D:/Bside/external/asr/cache",
        },
    )
    assert applied["status"] == "CUSTOMIZED"
    config = (data_root / "modules/asr_local/config.json").read_text(encoding="utf-8")
    assert "external_assets_copied" in config
    assert "D:/Bside/external/asr/runtime" in config
    assert not (project / "D:/Bside/external/asr/runtime").exists()

    with pytest.raises(B10BError) as exc_info:
        manager.customize("asr-local", {"model_root": "C:/"})
    assert exc_info.value.code == "EXTERNAL_REFERENCE_INVALID"


@pytest.mark.parametrize(
    "value",
    [
        "C:/provider/runtime",
        "D:/provider/runtime",
        "F:/provider/runtime",
    ],
)
def test_external_reference_accepts_any_non_root_local_drive(value: str) -> None:
    assert is_external_reference(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "C:/",
        "C:provider/runtime",
        "relative/provider/runtime",
        "C:/provider/../runtime",
        "C:/provider/notepad.exe.",
        "C:/provider/notepad.exe ",
        "C:/provider/NUL .txt",
        r"\\server\share\runtime",
        "https://example.invalid/runtime",
    ],
)
def test_external_reference_rejects_non_local_or_unsafe_paths(value: str) -> None:
    assert is_external_reference(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "C:/NUL",
        "C:/CON/runtime",
        "C:/provider/PRN.txt",
        "C:/provider/AUX.",
        "C:/provider/CLOCK$.log",
        "C:/provider/com1 ",
        "C:/provider/LPT9.tar.gz",
    ],
)
def test_external_reference_rejects_dos_device_name_segments(value: str) -> None:
    assert is_external_reference(value) is False


def test_b10b_runtime_comments_pass_public_hardening_scan() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "baseline_hardening_scan.py"),
            "--root",
            str(root),
            "--mode",
            "comments",
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_b06_tts_is_lifecycle_managed_when_composed_and_fail_closed_before_merge(tmp_path: Path) -> None:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    if importlib.util.find_spec("tts") is None:
        with pytest.raises(B10BError) as exc_info:
            manager.install(["core/http", "tts-local"], dry_run=False)
        assert exc_info.value.code == "MODULE_PROVIDER_MISSING"
        assert not (data_root / "state.json").exists()
        assert not (data_root / "modules/tts_local/marker.json").exists()
        assert not (data_root / "modules/core_http/marker.json").exists()
        return

    installed = manager.install(["core/http", "tts-local"], dry_run=False)
    assert installed["status"] == "INSTALLED"
    manager.enable("core/http")
    manager.enable("tts-local")
    report = manager.health()
    tts = next(item for item in report["modules"] if item["id"] == "tts-local")
    assert tts["status"] in {"HEALTHY", "DEGRADED", "UNAVAILABLE"}
    assert tts["provider_health"]["b06_composed"] is True


def test_verified_local_profile_is_reinstallable_and_uninstalls_only_b10b_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    import runtime.packaging.b10b.manager as manager_module

    monkeypatch.setattr(manager_module, "profile_settings", lambda _profile, **_kwargs: _verified_profile_settings())
    manager = B10BManager(project_root=project, data_root=data_root)

    dry_run = manager.install_profile("verified-local", dry_run=True)
    assert dry_run["status"] == "DRY_RUN"
    assert not data_root.exists()

    installed = manager.install_profile("verified-local")
    assert installed["status"] == "INSTALLED"
    assert installed["external_assets_copied"] is False
    assert (data_root / "modules/asr_local/config.json").is_file()
    assert (data_root / "modules/tts_local/config.json").is_file()
    assert (data_root / "modules/visual_livetalking/config.json").is_file()

    reinstalled = manager.install_profile("verified-local", reinstall=True)
    assert reinstalled["status"] == "REINSTALLED"
    disabled = manager.disable_profile("verified-local")
    assert disabled["status"] == "DISABLED"
    plan = manager.uninstall_profile("verified-local", dry_run=True)
    assert plan["status"] == "DRY_RUN"
    assert plan["external_assets_deleted"] is False

    removed = manager.uninstall_profile("verified-local", dry_run=False)
    assert removed["status"] == "UNINSTALLED"
    assert removed["external_assets_deleted"] is False
    assert not (data_root / "modules/asr_local/marker.json").exists()
    restored = manager.rollback_profile("verified-local")
    assert restored["status"] == "ROLLED_BACK"
    assert (data_root / "modules/asr_local/marker.json").is_file()


def test_verified_local_profile_missing_dependency_fails_before_any_metadata_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    import runtime.packaging.b10b.manager as manager_module

    def missing(_profile: str, **_kwargs: object) -> dict[str, dict[str, object]]:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "missing external dependency",
            {"module_status": "NOT_INSTALLED", "missing": {"asr-local": ["runtime"]}},
        )

    monkeypatch.setattr(manager_module, "profile_settings", missing)
    manager = B10BManager(project_root=project, data_root=data_root)
    with pytest.raises(B10BError) as exc_info:
        manager.install_profile("verified-local")
    assert exc_info.value.code == "MODULE_PROVIDER_MISSING"
    assert not data_root.exists()


def test_verified_local_rejects_env_visual_config_even_when_assets_and_hash_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    fake_root = tmp_path / "fake-livetalking"
    fake_root.mkdir()
    checkpoint = fake_root / "checkpoint.pth"
    checkpoint.write_bytes(b"not-the-verified-checkpoint")
    fake_config = {
        "runtime_root": str(fake_root),
        "python_executable": str(checkpoint),
        "checkpoint_path": str(checkpoint),
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "checkpoint_url": "https://example.invalid/checkpoint",
        "checkpoint_revision": "self-consistent fake checkpoint",
        "checkpoint_license": "Apache-2.0",
        "avatar_payload": str(fake_root),
        "avatar_id": "olivia_b11",
        "original_reference": str(checkpoint),
        "work_root": str(fake_root),
        "upstream_source": "https://github.com/lipku/LiveTalking",
        "upstream_revision": "b" * 40,
        "upstream_license": "Apache-2.0",
    }
    config_path = fake_root / "runtime-config.json"
    config_path.write_text(json.dumps(fake_config), encoding="utf-8")
    monkeypatch.setenv("B10B_VISUAL_CONFIG", str(config_path))

    manager = B10BManager(project_root=project, data_root=data_root)
    with pytest.raises(B10BError) as exc_info:
        manager.install_profile("verified-local")

    assert exc_info.value.code == "VERIFIED_PROFILE_OVERRIDE_FORBIDDEN"
    assert exc_info.value.details["module_status"] == "NOT_INSTALLED"
    assert not data_root.exists()


@pytest.mark.parametrize(
    "variable",
    [
        "B10B_ASR_CACHE_ROOT",
        "B10B_ASR_EXECUTABLE",
        "B10B_ASR_MODEL_PATH",
        "B10B_ASR_MODEL_ROOT",
        "B10B_ASR_RUNTIME_ROOT",
        "B10B_TTS_MODEL_DIR",
        "B10B_TTS_REFERENCE_AUDIO",
        "B10B_TTS_REFERENCE_SCRIPT",
        "B10B_TTS_REFERENCE_SOURCE",
        "B10B_TTS_RUNTIME_ROOT",
    ],
)
def test_verified_local_rejects_every_external_asset_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    project, data_root = _project(tmp_path)
    existing = tmp_path / "existing-provider-input"
    existing.write_bytes(b"exists")
    monkeypatch.setenv(variable, str(existing))

    manager = B10BManager(project_root=project, data_root=data_root)
    with pytest.raises(B10BError) as exc_info:
        manager.install_profile("verified-local")

    assert exc_info.value.code == "VERIFIED_PROFILE_OVERRIDE_FORBIDDEN"
    assert exc_info.value.details["environment_overrides"] == [variable]
    assert not data_root.exists()


def test_verified_local_rejects_self_consistent_unpinned_visual_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    import runtime.packaging.b10b.profiles as profiles_module

    fake_evidence = tmp_path / "visual-evidence"
    fake_runtime = fake_evidence / "runtime"
    fake_runtime.mkdir(parents=True)
    checkpoint = fake_runtime / "wav2lip.pth"
    checkpoint.write_bytes(b"different-but-self-consistent-checkpoint")
    fake_config = {
        "runtime_root": str(fake_runtime),
        "python_executable": str(checkpoint),
        "checkpoint_path": str(checkpoint),
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "checkpoint_url": "https://example.invalid/checkpoint",
        "checkpoint_revision": "self-consistent fake checkpoint",
        "checkpoint_license": "Apache-2.0",
        "avatar_payload": str(fake_runtime),
        "avatar_id": "olivia_b11",
        "original_reference": str(checkpoint),
        "work_root": str(fake_runtime),
        "upstream_source": "https://github.com/lipku/LiveTalking",
        "upstream_revision": "c" * 40,
        "upstream_license": "Apache-2.0",
    }
    (fake_evidence / "runtime-config.json").write_text(json.dumps(fake_config), encoding="utf-8")
    monkeypatch.setattr(profiles_module, "_VISUAL_EVIDENCE", fake_evidence)

    manager = B10BManager(project_root=project, data_root=data_root)
    with pytest.raises(B10BError) as exc_info:
        manager.install_profile("verified-local")

    assert exc_info.value.code == "VERIFIED_PROFILE_PIN_MISMATCH"
    assert exc_info.value.details["module_status"] == "NOT_INSTALLED"
    assert not data_root.exists()


def test_verified_local_rejects_unpinned_asr_runtime_and_acceptance_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runtime.packaging.b10b.profiles as profiles_module

    fake_evidence = tmp_path / "asr"
    fake_model = fake_evidence / "model/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
    fake_model.parent.mkdir(parents=True)
    fake_model.write_bytes(b"synthetic model fixture")
    executable = fake_evidence / "build-cuda-asr-http-vcpkg/bin/nemo-speech.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable fixture")
    acceptance = fake_evidence / "native-probe-accepted/evidence/native_acceptance.json"
    acceptance.parent.mkdir(parents=True)
    acceptance.write_text(
        json.dumps(
            {
                "verified": True,
                "model_revision": "d" * 40,
                "model_sha256": "a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae",
                "runtime_revision": "e" * 40,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles_module, "_ASR_EVIDENCE", fake_evidence)
    monkeypatch.setattr(profiles_module, "_FIXED_ASR_EVIDENCE", fake_evidence)
    expected_hashes = {
        fake_model: profiles_module._ASR_MODEL_SHA256,
        executable: profiles_module._ASR_EXECUTABLE_SHA256,
        acceptance: profiles_module._ASR_ACCEPTANCE_SHA256,
        fake_evidence / "NeMo-Speech.cpp/LICENSE": profiles_module._ASR_RUNTIME_LICENSE_SHA256,
    }
    monkeypatch.setattr(
        profiles_module,
        "_sha256",
        lambda path: expected_hashes[Path(path)],
    )

    provenance = verified_local_provenance(load_manifest())
    runtime_upstream = provenance["b05-nemotron-runtime"]
    model_upstream = provenance["b05-nemotron-model"]

    def fake_git_value(_root: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return str(runtime_upstream["revision"])
        if args == ("remote", "get-url", "origin"):
            return str(runtime_upstream["source"])
        raise AssertionError(f"unexpected git query: {args}")

    monkeypatch.setattr(profiles_module, "_git_value", fake_git_value)
    with pytest.raises(B10BError) as exc_info:
        profiles_module._validate_asr_pins(
            executable,
            fake_model,
            acceptance,
            runtime_upstream,
            model_upstream,
        )

    assert exc_info.value.code == "VERIFIED_PROFILE_PIN_MISMATCH"
    assert exc_info.value.details["component"] == "asr-local"
    assert exc_info.value.details["mismatches"] == ["model_revision", "runtime_revision"]


def test_verified_local_rejects_unpinned_tts_runtime_and_model_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runtime.packaging.b10b.profiles as profiles_module

    fake_root = tmp_path / "client-root"
    fake_runtime = fake_root / "CosyVoice"
    fake_model = fake_runtime / "pretrained_models/Fun-CosyVoice3-0.5B"
    fake_model.mkdir(parents=True)
    (fake_runtime / "LICENSE").write_text("synthetic license", encoding="utf-8")
    (fake_model / "README.md").write_text("synthetic model", encoding="utf-8")
    (fake_model / "cosyvoice3.yaml").write_text("unverified: true\n", encoding="utf-8")
    (fake_model / "flow.pt").write_bytes(b"unverified flow")
    (fake_model / "hift.pt").write_bytes(b"unverified hift")
    (fake_model / "llm.pt").write_bytes(b"unverified llm")
    metadata = fake_model / ".cache/huggingface/download/llm.pt.metadata"
    metadata.parent.mkdir(parents=True)
    reference_audio = fake_root / "output_audio/bv113_prompt_4p85s.wav"
    reference_audio.parent.mkdir(parents=True)
    reference_audio.write_bytes(b"RIFF-unverified")
    reference_script = tmp_path / "run_real_local_tts.py"
    reference_script.write_text("def reference_text():\n    return 'synthetic'\n", encoding="utf-8")
    reference_source = fake_root / "test_cosyvoice3.py"
    reference_source.write_text("# synthetic reference source\n", encoding="utf-8")

    provenance = verified_local_provenance(load_manifest())
    runtime_upstream = provenance["b06-cosyvoice-runtime"]
    model_upstream = provenance["b06-cosyvoice-model"]
    metadata.write_text(f"{model_upstream['revision']}\n", encoding="utf-8")
    monkeypatch.setattr(profiles_module, "_ROOT", fake_root)
    monkeypatch.setattr(profiles_module, "_FIXED_ROOT", fake_root)
    monkeypatch.setattr(profiles_module, "_FIXED_ASR_EVIDENCE", tmp_path / "evidence")

    expected_hashes = {
        fake_runtime / "LICENSE": profiles_module._TTS_RUNTIME_LICENSE_SHA256,
        reference_audio: profiles_module._TTS_REFERENCE_AUDIO_SHA256,
        reference_script: profiles_module._TTS_REFERENCE_SCRIPT_SHA256,
        reference_source: profiles_module._TTS_REFERENCE_SOURCE_SHA256,
    }
    monkeypatch.setattr(
        profiles_module,
        "_sha256",
        lambda path: expected_hashes.get(Path(path), "0" * 64),
    )

    def fake_git_value(_root: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return "f" * 40
        if args == ("remote", "get-url", "origin"):
            return str(runtime_upstream["source"])
        raise AssertionError(f"unexpected git query: {args}")

    monkeypatch.setattr(profiles_module, "_git_value", fake_git_value)

    with pytest.raises(B10BError) as exc_info:
        profiles_module._validate_tts_pins(
            fake_runtime,
            fake_model,
            reference_audio,
            reference_script,
            runtime_upstream,
            model_upstream,
        )

    assert exc_info.value.code == "VERIFIED_PROFILE_PIN_MISMATCH"
    assert exc_info.value.details["component"] == "tts-local"
    assert "upstream_revision" in exc_info.value.details["mismatches"]
    assert "model_llm.pt_sha256" in exc_info.value.details["mismatches"]


def test_verified_local_rejects_manifest_b05_model_license_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    import runtime.packaging.b10b.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "_VISUAL_EVIDENCE", tmp_path / "missing-visual-evidence")
    manifest_path = _manifest_with_upstream_value(
        tmp_path, "b05-nemotron-model", "license", "Apache-2.0"
    )
    manager = B10BManager(project_root=project, data_root=data_root, manifest_path=manifest_path)

    with pytest.raises(B10BError) as exc_info:
        manager.install_profile("verified-local")

    assert exc_info.value.code == "VERIFIED_PROFILE_PIN_MISMATCH"
    assert exc_info.value.details["component"] == "asr-local"
    assert exc_info.value.details["module_status"] == "NOT_INSTALLED"
    assert not data_root.exists()


def test_verified_local_rejects_manifest_b06_runtime_source_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    import runtime.packaging.b10b.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "_VISUAL_EVIDENCE", tmp_path / "missing-visual-evidence")
    manifest_path = _manifest_with_upstream_value(
        tmp_path,
        "b06-cosyvoice-runtime",
        "source",
        "https://github.com/example/CosyVoice",
    )
    manager = B10BManager(project_root=project, data_root=data_root, manifest_path=manifest_path)

    with pytest.raises(B10BError) as exc_info:
        manager.install_profile("verified-local")

    assert exc_info.value.code == "VERIFIED_PROFILE_PIN_MISMATCH"
    assert exc_info.value.details["component"] == "tts-local"
    assert exc_info.value.details["mismatches"] == ["upstream_source"]
    assert not data_root.exists()


def test_verified_local_rejects_manifest_b06_model_revision_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, data_root = _project(tmp_path)
    import runtime.packaging.b10b.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "_VISUAL_EVIDENCE", tmp_path / "missing-visual-evidence")
    manifest_path = _manifest_with_upstream_value(
        tmp_path, "b06-cosyvoice-model", "revision", "f" * 40
    )
    manager = B10BManager(project_root=project, data_root=data_root, manifest_path=manifest_path)

    with pytest.raises(B10BError) as exc_info:
        manager.install_profile("verified-local")

    assert exc_info.value.code == "VERIFIED_PROFILE_PIN_MISMATCH"
    assert exc_info.value.details["component"] == "tts-local"
    assert exc_info.value.details["mismatches"] == ["model_revision"]
    assert not data_root.exists()


def test_verified_local_uses_one_manifest_canonical_provenance() -> None:
    manifest = load_manifest()
    upstreams = {item["id"]: item for item in manifest["provenance"]["upstreams"]}
    provenance = verified_local_provenance(manifest)

    assert provenance["b05-nemotron-runtime"]["source"] == upstreams["b05-nemotron-runtime"]["source"]
    assert provenance["b05-nemotron-runtime"]["revision"] == upstreams["b05-nemotron-runtime"]["revision"]
    assert provenance["b05-nemotron-runtime"]["license"] == upstreams["b05-nemotron-runtime"]["license"]
    assert provenance["b05-nemotron-model"]["license"] == upstreams["b05-nemotron-model"]["license"]
    assert provenance["b06-cosyvoice-runtime"]["source"] == upstreams["b06-cosyvoice-runtime"]["source"]
    assert provenance["b06-cosyvoice-model"]["revision"] == upstreams["b06-cosyvoice-model"]["revision"]
