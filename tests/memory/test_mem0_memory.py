from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from conversation_memory_port import (
    MemoryWriteStatus,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)
from mem0_memory import (
    Mem0AdapterError,
    Mem0Config,
    Mem0ConversationMemoryAdapter,
    create_mem0_adapter,
    load_mem0_config,
)


NOW = datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc)


class FakeMem0:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.calls: list[tuple[str, object]] = []
        self.fail: set[str] = set()
        self.counter = 0

    def _raise(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"private provider error from {name}")

    def add(self, messages, **kwargs):
        self._raise("add")
        self.calls.append(("add", {"messages": messages, **kwargs}))
        self.counter += 1
        metadata = dict(kwargs.get("metadata", {}))
        text = (
            messages
            if isinstance(messages, str)
            else "用户在东京工作。"
        )
        row = {
            "id": f"memory.fixture.{self.counter}",
            "memory": text,
            "user_id": kwargs.get("user_id"),
            "agent_id": kwargs.get("agent_id"),
            "metadata": metadata,
            "created_at": NOW.isoformat(),
        }
        self.rows.append(row)
        return {"results": [row]}

    def search(self, query, **kwargs):
        self._raise("search")
        self.calls.append(("search", {"query": query, **kwargs}))
        return {"results": [{**row, "score": 0.91} for row in self.rows]}

    def get_all(self, **kwargs):
        self._raise("get_all")
        self.calls.append(("get_all", kwargs))
        return {"results": list(self.rows)}

    def delete(self, memory_id):
        self._raise("delete")
        self.calls.append(("delete", memory_id))
        self.rows[:] = [row for row in self.rows if row["id"] != memory_id]

    def delete_all(self, **kwargs):
        self._raise("delete_all")
        self.calls.append(("delete_all", kwargs))
        self.rows.clear()


def _config(tmp_path: Path) -> Mem0Config:
    return Mem0Config(
        enabled=True,
        data_root=tmp_path / "memory" / "mem0",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="fixture-model",
        embedding_cache=tmp_path / "models",
    )


def test_config_builds_local_only_provider_mapping(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mapping = config.provider_config({"DEEPSEEK_API_KEY": "fixture-secret"})

    assert mapping["vector_store"] == {
        "provider": "qdrant",
        "config": {
            "collection_name": "olivia_conversation_memory_v1",
            "path": str(config.qdrant_path),
            "on_disk": True,
            "embedding_model_dims": 512,
        },
    }
    assert mapping["embedder"]["provider"] == "huggingface"
    assert mapping["embedder"]["config"]["model_kwargs"] == {
        "device": "cpu",
        "cache_folder": str(config.model_cache),
        "local_files_only": True,
    }
    assert mapping["llm"]["config"]["openai_base_url"] == "http://127.0.0.1:9/v1"
    assert mapping["llm"]["config"]["api_key"] == "fixture-secret"
    assert "private_world" not in repr(mapping)


def test_load_config_reuses_primary_llm_and_rejects_incomplete_enabled_config(tmp_path: Path) -> None:
    config = load_mem0_config(
        environ={
            "OLIVIA_MEMORY_ENABLED": "true",
            "OLIVIA_MEMORY_ROOT": str(tmp_path / "mem0"),
            "OLIVIA_LLM_BASE_URL": "http://127.0.0.1:8000/v1",
            "OLIVIA_LLM_MODEL": "fixture-model",
            "OLIVIA_LLM_API_KEY_ENV": "FIXTURE_KEY",
        },
        project_root=tmp_path,
    )
    assert config.enabled is True
    assert config.llm_base_url == "http://127.0.0.1:8000/v1"
    assert config.llm_model == "fixture-model"
    assert config.llm_api_key_env == "FIXTURE_KEY"
    assert config.config_error is None

    incomplete = load_mem0_config(
        environ={
            "OLIVIA_MEMORY_ENABLED": "true",
            "OLIVIA_MEMORY_ROOT": str(tmp_path / "mem0-missing"),
        },
        project_root=tmp_path,
    )
    assert incomplete.config_error == "MEM0_LLM_CONFIG_INCOMPLETE"


def test_exchange_search_list_export_delete_and_clear(tmp_path: Path) -> None:
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    written = adapter.remember_exchange(
        user_message="我现在在东京工作。",
        assistant_message="这次我记住了。",
        occurred_at=NOW,
        source_id="letter:fixture:1",
        user_id="local-user",
    )
    assert written.status is MemoryWriteStatus.WRITTEN
    assert written.memory_ids == ("memory.fixture.1",)

    add_call = next(value for name, value in backend.calls if name == "add")
    assert add_call["messages"] == [
        {"role": "user", "content": "我现在在东京工作。"},
        {"role": "assistant", "content": "这次我记住了。"},
    ]
    assert add_call["metadata"] == {
        "source_id": "letter:fixture:1",
        "occurred_at": NOW.isoformat(),
        "domain": "conversation_memory",
        "canonical": True,
    }
    serialized = repr(add_call)
    assert "system_prompt" not in serialized
    assert "private_world" not in serialized

    duplicate = adapter.remember_exchange(
        user_message="重复投递",
        assistant_message="重复回复",
        occurred_at=NOW,
        source_id="letter:fixture:1",
        user_id="local-user",
    )
    assert duplicate.status is MemoryWriteStatus.DUPLICATE
    assert len([name for name, _value in backend.calls if name == "add"]) == 1

    searched = adapter.search_context("东京", user_id="local-user", limit=3)
    assert len(searched) == 1
    assert searched[0].text == "用户在东京工作。"
    assert searched[0].score == 0.91
    assert searched[0].source_id == "letter:fixture:1"

    listed = adapter.list_memories(user_id="local-user")
    assert [record.memory_id for record in listed] == ["memory.fixture.1"]
    exported = adapter.export_user(user_id="local-user")
    assert exported["provider"] == "mem0"
    assert exported["records"][0]["source_id"] == "letter:fixture:1"
    assert "user_id" not in exported["records"][0]

    assert adapter.delete_memory("memory.fixture.1", user_id="other-user") is False
    assert adapter.delete_memory("memory.fixture.1", user_id="local-user") is True
    assert adapter.list_memories(user_id="local-user") == ()

    adapter.remember_exchange(
        user_message="我喜欢黑咖啡。",
        assistant_message="知道了。",
        occurred_at=NOW,
        source_id="letter:fixture:2",
        user_id="local-user",
    )
    assert adapter.clear_user(user_id="local-user") == 1
    assert adapter.list_memories(user_id="local-user") == ()


def test_manual_memory_is_exact_and_user_owned(tmp_path: Path) -> None:
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    record = adapter.add_manual_memory(
        "用户不吃香菜。",
        user_id="local-user",
        source_id="manual:fixture:1",
    )
    assert record.text == "用户不吃香菜。"
    add_call = next(value for name, value in backend.calls if name == "add")
    assert add_call["messages"] == "用户不吃香菜。"
    assert add_call["infer"] is False
    assert add_call["metadata"]["manual"] is True
    assert add_call["metadata"]["actor"] == "local_user"


def test_provider_failures_degrade_without_echoing_private_content(tmp_path: Path) -> None:
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    backend.fail.add("add")

    result = adapter.remember_exchange(
        user_message="private user message",
        assistant_message="private assistant message",
        occurred_at=NOW,
        source_id="letter:fixture:failure",
        user_id="local-user",
    )
    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_WRITE_FAILED"
    assert "private user message" not in repr(result)
    assert "private assistant message" not in repr(result)
    assert adapter.status().reason_code in {"MEM0_WRITE_FAILED", "MEM0_LIST_FAILED"}

    backend.fail.clear()
    backend.fail.add("add")
    with pytest.raises(Mem0AdapterError) as manual:
        adapter.add_manual_memory(
            "private manual fact",
            user_id="local-user",
            source_id="manual:fixture:failure",
        )
    assert manual.value.code == "MEM0_MANUAL_WRITE_FAILED"
    assert "private manual fact" not in str(manual.value)


def test_factory_is_lazy_optional_and_returns_stable_ports(tmp_path: Path) -> None:
    disabled = create_mem0_adapter(
        Mem0Config(enabled=False, data_root=tmp_path / "disabled")
    )
    assert isinstance(disabled, NullConversationMemoryPort)

    incomplete = create_mem0_adapter(
        Mem0Config(
            enabled=True,
            data_root=tmp_path / "incomplete",
            config_error="MEM0_LLM_CONFIG_INCOMPLETE",
        )
    )
    assert isinstance(incomplete, UnavailableConversationMemoryPort)
    assert incomplete.reason_code == "MEM0_LLM_CONFIG_INCOMPLETE"

    captured: list[dict[str, object]] = []
    backend = FakeMem0()

    def factory(mapping):
        captured.append(dict(mapping))
        return backend

    created = create_mem0_adapter(
        _config(tmp_path),
        environ={"DEEPSEEK_API_KEY": "fixture-secret"},
        memory_factory=factory,
    )
    assert isinstance(created, Mem0ConversationMemoryAdapter)
    assert captured[0]["vector_store"]["provider"] == "qdrant"
    assert created.status().provider == "mem0"
