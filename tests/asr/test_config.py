from __future__ import annotations

from pathlib import Path

import pytest

from asr.config import MODEL_REPO, MODEL_REVISION, RUNTIME_REVISION, AsrConfig
from asr.errors import AsrError


def test_default_is_text_fallback_and_provenance_is_pinned() -> None:
    config = AsrConfig()
    assert config.provider == "text-fallback"
    assert config.language == "auto"
    assert config.to_dict(include_paths=False)["model_repo"] == MODEL_REPO
    assert config.to_dict(include_paths=False)["model_revision"] == MODEL_REVISION
    assert config.to_dict(include_paths=False)["runtime_revision"] == RUNTIME_REVISION


def test_default_storage_roots_follow_localappdata(monkeypatch, tmp_path: Path) -> None:
    local_appdata = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    config = AsrConfig()

    expected = local_appdata / "BSideOliviaLocal" / "asr"
    assert config.runtime_root == expected / "runtime"
    assert config.model_root == expected / "models"
    assert config.cache_root == expected / "cache"


def test_default_storage_roots_use_user_home_when_localappdata_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    config = AsrConfig()

    expected = Path.home() / "AppData" / "Local" / "BSideOliviaLocal" / "asr"
    assert config.runtime_root == expected / "runtime"


def test_config_accepts_explicit_test_paths_without_changing_production_policy(tmp_path: Path) -> None:
    config = AsrConfig().with_test_paths(tmp_path)
    assert config.strict_storage is False
    assert config.effective_model_path.name.endswith(".gguf")


@pytest.mark.parametrize("drive", ("C:", "D:", "E:", "F:"))
def test_production_storage_accepts_any_local_drive(drive: str) -> None:
    config = AsrConfig(runtime_root=Path(f"{drive}/asr/runtime"))

    assert config.runtime_root == Path(f"{drive}/asr/runtime")


@pytest.mark.parametrize("value", (Path("relative/asr"), Path("http://example.invalid/asr")))
def test_production_storage_rejects_relative_and_url_paths(value: Path) -> None:
    with pytest.raises(AsrError, match="absolute local Windows paths"):
        AsrConfig(runtime_root=value)


def test_server_must_be_loopback() -> None:
    with pytest.raises(AsrError, match="loopback"):
        AsrConfig(server_url="ws://example.invalid:8080")
