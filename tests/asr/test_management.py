from __future__ import annotations

from pathlib import Path

import pytest

from asr.config import AsrConfig
from asr.errors import AsrError
import asr.management as management
from asr.management import install, install_plan, switch_provider, uninstall, uninstall_plan


def test_install_plan_is_idempotent_and_pins_sources(tmp_path: Path) -> None:
    config = AsrConfig(provider="nemotron-speech-cpp").with_test_paths(tmp_path)
    plan = install_plan(config)
    assert plan["mode"] == "dry-run"
    assert plan["runtime"]["revision"]
    assert plan["model"]["revision"]
    assert plan["runtime"]["release"] == "none"
    assert plan["paths_are_external"] is True


def test_install_plan_is_offline_and_records_fixed_transfer_closure() -> None:
    plan = install_plan(
        AsrConfig(provider="nemotron-speech-cpp"),
        transfer_root=Path("D:/b05-runtime-transfer"),
    )

    assert plan["model"]["download"].startswith("disabled")
    assert plan["runtime"]["source"].endswith("NeMo-Speech.cpp")
    assert plan["runtime"]["submodules"]["ggml"].startswith("c03b4e2")
    assert plan["provenance"]["replacement_boundary"]


def test_apply_requires_explicit_offline_transfer_root(tmp_path: Path) -> None:
    config = AsrConfig(provider="nemotron-speech-cpp").with_test_paths(tmp_path)

    with pytest.raises(AsrError, match="transfer_root"):
        install(config, apply=True)


def test_switch_provider_round_trip_uses_json_config(tmp_path: Path) -> None:
    config_path = tmp_path / "asr.json"
    switched = switch_provider(config_path, "nemotron-speech-cpp")
    assert switched.provider == "nemotron-speech-cpp"
    assert config_path.exists()
    assert AsrConfig.from_json(config_path).provider == "nemotron-speech-cpp"


def test_switch_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(AsrError, match="unsupported provider"):
        switch_provider(tmp_path / "asr.json", "mock-asr")


def test_uninstall_plan_does_not_delete_without_apply(tmp_path: Path) -> None:
    config = AsrConfig(provider="nemotron-speech-cpp").with_test_paths(tmp_path)
    plan = uninstall_plan(config)
    assert plan["mode"] == "dry-run"
    assert plan["deleted"] == []


def test_install_and_uninstall_apply_roundtrip_removes_only_owned_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AsrConfig(provider="nemotron-speech-cpp").with_test_paths(tmp_path)
    runtime_executable = config.runtime_root / "nemo-speech.exe"
    runtime_executable.parent.mkdir(parents=True)
    runtime_executable.write_bytes(b"fixture-runtime")
    monkeypatch.setattr(
        management,
        "_validate_transfer_source",
        lambda _root: {"root": str(tmp_path / "transfer"), "revision": "pinned"},
    )
    monkeypatch.setattr(
        management,
        "_validate_model",
        lambda _path: {"path": str(config.effective_model_path), "sha256": "a" * 64, "bytes": 1},
    )

    applied = install(config, apply=True, transfer_root=Path("F:/fixture-transfer"))
    assert applied["mode"] == "applied"
    assert (config.runtime_root / ".b05-install.json").is_file()

    removed = uninstall(config, apply=True)
    assert removed["mode"] == "applied"
    assert set(removed["deleted"]) == {
        str(config.runtime_root),
        str(config.model_root),
        str(config.cache_root),
    }
    assert not config.runtime_root.exists()
    assert not config.model_root.exists()
    assert not config.cache_root.exists()
