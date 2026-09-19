from __future__ import annotations

import json
from pathlib import Path

from runtime.packaging.b10b.live_bridge import build_live_service_from_b10b, bridge_health
from runtime.packaging.b10b.errors import B10BError
from runtime.packaging.b10b.manager import B10BManager


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "B10B live bridge project"
    project.mkdir()
    (project / "local_server.py").write_text("# B02 contract\n", encoding="utf-8")
    (project / "http_contract.py").write_text("# B02 contract\n", encoding="utf-8")
    return project, tmp_path / "B10B live bridge data"


def _enabled_live_manager(tmp_path: Path, *, tts_runtime: Path | None = None) -> B10BManager:
    project, data_root = _project(tmp_path)
    manager = B10BManager(project_root=project, data_root=data_root)
    modules = ["core/http", "asr-local", "visual-driver", "tts-local", "live-orchestration"]
    manager.install(modules)
    for module in modules:
        manager.enable(module)
    manager.customize(
        "asr-local",
        {
            "provider": "nemotron-speech-cpp",
            "runtime_root": "F:/provider/asr/runtime",
            "runtime_executable": "F:/provider/asr/runtime/nemo-speech.exe",
            "model_root": "F:/provider/asr/model",
            "model_path": "F:/provider/asr/model/model.gguf",
            "cache_root": "F:/provider/asr/cache",
            "server_url": "ws://127.0.0.1:18081",
        },
    )
    manager.customize(
        "tts-local",
        {
            "provider": "cosyvoice3",
            "runtime_root": str(tts_runtime or Path("F:/provider/tts/runtime")),
            "model_dir": str((tts_runtime / "model") if tts_runtime else Path("F:/provider/tts/model")),
            "reference_audio": str((tts_runtime / "reference.wav") if tts_runtime else Path("F:/provider/tts/reference.wav")),
            "reference_text": "verified reference",
            "fallback": "text",
        },
    )
    return manager


def _visual_settings() -> dict[str, object]:
    return {
        "runtime_root": "F:/provider/livetalking",
        "python_executable": "F:/provider/livetalking/venv/Scripts/python.exe",
        "checkpoint_path": "F:/provider/livetalking/models/wav2lip.pth",
        "checkpoint_sha256": "a" * 64,
        "checkpoint_url": "https://example.invalid/livetalking-checkpoint",
        "checkpoint_revision": "fixed fixture checkpoint",
        "checkpoint_license": "Apache-2.0",
        "avatar_payload": "F:/provider/livetalking/data/avatars/fixture",
        "avatar_id": "fixture",
        "original_reference": "F:/provider/livetalking/fixture.mp4",
        "work_root": "F:/provider/livetalking/work",
        "upstream_source": "https://github.com/lipku/LiveTalking",
        "upstream_revision": "a" * 40,
        "upstream_license": "Apache-2.0",
    }


def test_bridge_rejects_unverified_asr_and_tts_settings_even_when_enabled(tmp_path: Path) -> None:
    manager = _enabled_live_manager(tmp_path)

    service = build_live_service_from_b10b(
        project_root=manager.project_root,
        data_root=manager.data_root,
        environ={
            "OLIVIA_LLM_PROVIDER": "openai_compatible",
            "OLIVIA_LLM_BASE_URL": "https://api.deepseek.com/v1",
            "OLIVIA_LLM_MODEL": "deepseek-v4-flash",
            "OLIVIA_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
            "OLIVIA_LLM_REQUIRES_API_KEY": "true",
        },
    )

    assert service.environment is not None
    assert service.environment.asr_config is not None
    assert service.environment.asr_config.provider == "text-fallback"
    assert service.environment.tts_config.enabled is False
    assert service.environment.tts_config.provider_options == {}
    assert service.health()["components"]["llm"]["status"] == "UNAVAILABLE"
    assert service.health()["components"]["visual"]["reason_code"] == "VISUAL_UNAVAILABLE"


def _install_verified_profile(manager: B10BManager, monkeypatch) -> None:
    import runtime.packaging.b10b.manager as manager_module

    profile = {
        "asr-local": manager.active_module_settings("asr-local"),
        "tts-local": manager.active_module_settings("tts-local"),
        "visual-livetalking": _visual_settings(),
    }
    monkeypatch.setattr(manager_module, "profile_settings", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(
        manager_module,
        "verified_local_visual_settings",
        lambda **_kwargs: profile["visual-livetalking"],
    )
    manager.install_profile("verified-local")


def test_bridge_only_injects_existing_cosyvoice_venv_from_verified_profile(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "CosyVoice"
    python = runtime / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"external-python")
    manager = _enabled_live_manager(tmp_path, tts_runtime=runtime)
    _install_verified_profile(manager, monkeypatch)

    service = build_live_service_from_b10b(project_root=manager.project_root, data_root=manager.data_root)

    assert service.environment.asr_config.provider == "nemotron-speech-cpp"
    assert service.environment.tts_config.enabled is True
    assert service.environment.tts_config.provider_options == {"external_python": str(python)}


def test_bridge_fails_closed_after_verified_tts_or_asr_pin_mismatch(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "CosyVoice"
    python = runtime / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"external-python")
    manager = _enabled_live_manager(tmp_path, tts_runtime=runtime)
    _install_verified_profile(manager, monkeypatch)
    manager.customize("tts-local", {"fallback": "unavailable"})
    manager.customize("asr-local", {"server_url": "ws://127.0.0.1:19999"})

    service = build_live_service_from_b10b(project_root=manager.project_root, data_root=manager.data_root)

    assert service.environment.asr_config.provider == "text-fallback"
    assert service.environment.tts_config.enabled is False
    assert service.environment.tts_config.provider_options == {}


def test_bridge_ignores_caller_asr_config_path_bypass(tmp_path: Path) -> None:
    manager = _enabled_live_manager(tmp_path)
    path = tmp_path / "caller-asr.json"
    path.write_text(
        json.dumps({"provider": "nemotron-speech-cpp", "server_url": "ws://127.0.0.1:19999"}),
        encoding="utf-8",
    )

    service = build_live_service_from_b10b(
        project_root=manager.project_root,
        data_root=manager.data_root,
        environ={"ASR_CONFIG_PATH": str(path)},
    )

    assert service.environment.asr_config.provider == "text-fallback"


def test_bridge_ignores_caller_tts_state_root_bypass(tmp_path: Path) -> None:
    manager = _enabled_live_manager(tmp_path)
    state_root = tmp_path / "caller-tts-state"
    profile = "cosyvoice3-live"
    config = {
        "profile": profile,
        "provider": "cosyvoice3",
        "enabled": True,
        "runtime_root": "F:/caller/runtime",
        "model_dir": "F:/caller/model",
        "reference_audio": "F:/caller/reference.wav",
        "reference_text": "caller reference",
        "fallback": "unavailable",
    }
    payload = {"schema_version": "b06.tts-profile.v1", "profile": profile, "status": "enabled", "config": config}
    state_root.mkdir()
    (state_root / f"{profile}.json").write_text(json.dumps(payload), encoding="utf-8")
    (state_root / "profiles").mkdir()
    (state_root / "profiles" / f"{profile}.json").write_text(json.dumps(payload), encoding="utf-8")

    service = build_live_service_from_b10b(
        project_root=manager.project_root,
        data_root=manager.data_root,
        environ={"OLIVIA_TTS_STATE_ROOT": str(state_root)},
    )

    assert service.environment.tts_config.enabled is False
    assert service.environment.tts_config.fallback == "text"


def test_bridge_health_is_offline_and_marks_missing_b11_timestamp_driver_unavailable(tmp_path: Path) -> None:
    manager = _enabled_live_manager(tmp_path)

    health = bridge_health(manager)
    report = manager.health()
    module = next(item for item in report["modules"] if item["id"] == "live-orchestration")

    assert health == {
        "status": "DEGRADED",
        "ready": False,
        "reason": "B11_TIMESTAMPED_VISUAL_DRIVER_UNAVAILABLE",
        "network_called": False,
        "visual_driver_injected": False,
    }
    assert module["status"] == "DEGRADED"
    assert module["provider_health"] == health


def test_bridge_does_not_inject_custom_self_consistent_b11_settings(tmp_path: Path) -> None:
    manager = _enabled_live_manager(tmp_path)
    manager.install(["visual-livetalking"])
    manager.enable("visual-livetalking")
    manager.customize("visual-livetalking", _visual_settings())

    service = build_live_service_from_b10b(project_root=manager.project_root, data_root=manager.data_root)

    assert service.visual_driver is not None
    assert service.visual_driver._backend is None
    assert bridge_health(manager)["reason"] == "UNVERIFIED"


def test_bridge_injects_only_a_current_verified_local_record(tmp_path: Path, monkeypatch) -> None:
    import runtime.packaging.b10b.manager as manager_module
    from runtime.visual.livetalking_backend import LiveTalkingVisualBackend

    manager = _enabled_live_manager(tmp_path)
    profile = {
        "asr-local": manager.active_module_settings("asr-local"),
        "tts-local": manager.active_module_settings("tts-local"),
        "visual-livetalking": _visual_settings(),
    }
    monkeypatch.setattr(manager_module, "profile_settings", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(
        manager_module,
        "verified_local_visual_settings",
        lambda **_kwargs: profile["visual-livetalking"],
    )
    manager.install_profile("verified-local")

    service = build_live_service_from_b10b(project_root=manager.project_root, data_root=manager.data_root)

    assert isinstance(service.visual_driver._backend, LiveTalkingVisualBackend)
    assert service.visual_driver._backend._evidence_root == manager.data_root / ".evidence"
    assert bridge_health(manager)["reason"] == "B11_CAPTURE_DELEGATION_UNPROBED"


def test_bridge_stops_trusting_verified_local_after_visual_customize(tmp_path: Path, monkeypatch) -> None:
    import runtime.packaging.b10b.manager as manager_module

    manager = _enabled_live_manager(tmp_path)
    profile = {
        "asr-local": manager.active_module_settings("asr-local"),
        "tts-local": manager.active_module_settings("tts-local"),
        "visual-livetalking": _visual_settings(),
    }
    monkeypatch.setattr(manager_module, "profile_settings", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(
        manager_module,
        "verified_local_visual_settings",
        lambda **_kwargs: profile["visual-livetalking"],
    )
    manager.install_profile("verified-local")
    manager.customize("visual-livetalking", {"upstream_revision": "b" * 40})

    service = build_live_service_from_b10b(project_root=manager.project_root, data_root=manager.data_root)

    assert service.visual_driver._backend is None
    assert bridge_health(manager)["reason"] == "UNVERIFIED"


def test_bridge_reports_pin_mismatch_without_injecting_or_leaking_settings(tmp_path: Path, monkeypatch) -> None:
    import runtime.packaging.b10b.manager as manager_module

    manager = _enabled_live_manager(tmp_path)
    profile = {
        "asr-local": manager.active_module_settings("asr-local"),
        "tts-local": manager.active_module_settings("tts-local"),
        "visual-livetalking": _visual_settings(),
    }
    monkeypatch.setattr(manager_module, "profile_settings", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(
        manager_module,
        "verified_local_visual_settings",
        lambda **_kwargs: profile["visual-livetalking"],
    )
    manager.install_profile("verified-local")

    def mismatch(*_args, **_kwargs):
        raise B10BError("VERIFIED_PROFILE_PIN_MISMATCH", "private fixture path changed")

    monkeypatch.setattr(manager_module, "profile_settings", mismatch)
    monkeypatch.setattr(manager_module, "verified_local_visual_settings", mismatch)
    service = build_live_service_from_b10b(project_root=manager.project_root, data_root=manager.data_root)
    health = bridge_health(manager)

    assert service.visual_driver._backend is None
    assert health["reason"] == "PIN_MISMATCH"
    assert "path" not in repr(health).lower()
