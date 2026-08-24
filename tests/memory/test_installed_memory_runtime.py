from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conversation_memory_port import (
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)
import installed_memory_runtime as runtime
from installed_memory_runtime import (
    InstalledMem0Config,
    create_installed_mem0_adapter,
)
from mem0_memory import Mem0Config


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "OLIVIA_MEMORY_ENABLED": "1",
        "OLIVIA_MEMORY_ROOT": str(tmp_path / "memory" / "mem0"),
        "OLIVIA_MEMORY_EMBEDDING_CACHE": str(
            tmp_path / "memory" / "model-cache"
        ),
        "OLIVIA_MEMORY_EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5",
        "OLIVIA_MEMORY_EMBEDDING_DIMS": "512",
        "OLIVIA_MEMORY_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "OLIVIA_MEMORY_LLM_MODEL": "fixture-model",
        "OLIVIA_MEMORY_LLM_API_KEY_ENV": "FIXTURE_KEY",
        "FIXTURE_KEY": "fixture-secret",
    }


def test_installed_config_preserves_store_and_uses_fastembed(tmp_path: Path) -> None:
    config = InstalledMem0Config(
        enabled=True,
        data_root=tmp_path / "memory" / "mem0",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="fixture-model",
        embedding_cache=tmp_path / "model-cache",
    )
    payload = config.provider_config({"DEEPSEEK_API_KEY": "fixture"})
    assert payload["vector_store"]["provider"] == "qdrant"
    assert payload["llm"]["provider"] == "openai"
    assert payload["embedder"] == {
        "provider": "fastembed",
        "config": {
            "model": "BAAI/bge-small-zh-v1.5",
            "embedding_dims": 512,
        },
    }
    assert "private_world" not in repr(payload)


def test_disabled_profile_remains_dependency_free(tmp_path: Path) -> None:
    value = create_installed_mem0_adapter(
        environ={
            "OLIVIA_MEMORY_ENABLED": "0",
            "OLIVIA_MEMORY_ROOT": str(tmp_path / "disabled"),
        }
    )
    assert isinstance(value, NullConversationMemoryPort)


def test_missing_key_and_model_fail_open_with_stable_reasons(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _environment(tmp_path)
    environment.pop("FIXTURE_KEY")
    missing_key = create_installed_mem0_adapter(environ=environment)
    assert isinstance(missing_key, UnavailableConversationMemoryPort)
    assert missing_key.reason_code == "MEM0_LLM_API_KEY_MISSING"

    environment["FIXTURE_KEY"] = "fixture-secret"
    monkeypatch.setattr(
        runtime,
        "load_memory_model_manifest",
        lambda _path: SimpleNamespace(
            provider="fastembed",
            model="BAAI/bge-small-zh-v1.5",
            dimensions=512,
        ),
    )
    monkeypatch.setattr(
        runtime,
        "validate_model_cache",
        lambda *_args: SimpleNamespace(
            ready=False,
            reason_code="MEMORY_MODEL_NOT_READY",
        ),
    )
    unavailable = create_installed_mem0_adapter(environ=environment)
    assert isinstance(unavailable, UnavailableConversationMemoryPort)
    assert unavailable.reason_code == "MEMORY_MODEL_NOT_READY"


def test_verified_cache_delegates_to_existing_mem0_adapter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _environment(tmp_path)
    captured: dict[str, object] = {}
    sentinel = object()
    monkeypatch.setattr(
        runtime,
        "load_memory_model_manifest",
        lambda _path: SimpleNamespace(
            provider="fastembed",
            model="BAAI/bge-small-zh-v1.5",
            dimensions=512,
        ),
    )
    monkeypatch.setattr(
        runtime,
        "validate_model_cache",
        lambda *_args: SimpleNamespace(ready=True, reason_code=None),
    )
    monkeypatch.setattr(
        runtime,
        "configure_offline_model_environment",
        lambda path: captured.setdefault("cache", path),
    )

    def fake_create(config, *, environ):
        captured["config"] = config
        captured["environment"] = environ
        return sentinel

    monkeypatch.setattr(runtime, "create_mem0_adapter", fake_create)
    value = create_installed_mem0_adapter(environ=environment)
    assert value is sentinel
    assert isinstance(captured["config"], Mem0Config)
    assert isinstance(captured["config"], InstalledMem0Config)
    assert captured["cache"] == Path(
        environment["OLIVIA_MEMORY_EMBEDDING_CACHE"]
    )
    assert captured["environment"] is environment
