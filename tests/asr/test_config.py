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


def test_config_accepts_explicit_test_paths_without_changing_production_policy(tmp_path: Path) -> None:
    config = AsrConfig().with_test_paths(tmp_path)
    assert config.strict_storage is False
    assert config.effective_model_path.name.endswith(".gguf")


def test_production_storage_must_be_d_or_f_drive() -> None:
    with pytest.raises(AsrError, match="D:/ or F:/"):
        AsrConfig(runtime_root=Path("C:/not-allowed"))


def test_server_must_be_loopback() -> None:
    with pytest.raises(AsrError, match="loopback"):
        AsrConfig(server_url="ws://example.invalid:8080")
