from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import tomllib
from types import SimpleNamespace

import pytest
from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from conversation_memory_port import (
    MemoryWriteStatus,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)
from mem0_memory import (
    MEM0_EMBEDDING_MODEL,
    MEM0_EMBEDDING_MODEL_REVISION,
    MEM0_OSS_VERSION,
    Mem0AdapterError,
    Mem0Config,
    Mem0ConversationMemoryAdapter,
    create_mem0_adapter,
    load_mem0_config,
)
import mem0_memory


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

    @staticmethod
    def _assert_filters(filters: object) -> None:
        assert isinstance(filters, dict)
        assert isinstance(filters.get("user_id"), str)
        assert filters.get("agent_id") == "linli"
        assert filters.get("domain") == "conversation_memory"
        assert set(filters) <= {
            "user_id",
            "agent_id",
            "domain",
            "source_id",
        }

    def add(self, messages, **kwargs):
        self._raise("add")
        self.calls.append(("add", {"messages": messages, **kwargs}))
        self.counter += 1
        metadata = dict(kwargs.get("metadata", {}))
        actor = metadata.get("history_actor")
        text = (
            messages
            if isinstance(messages, str)
            else "我记得自己曾认真回复这封信。"
            if actor == "linli"
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
        return {
            "results": [
                {
                    "id": row["id"],
                    "memory": row["memory"],
                    "event": "ADD",
                }
            ]
        }

    def search(self, query, **kwargs):
        self._raise("search")
        self._assert_filters(kwargs.get("filters"))
        self.calls.append(("search", {"query": query, **kwargs}))
        return {"results": [{**row, "score": 0.91} for row in self.rows]}

    def get_all(self, **kwargs):
        self._raise("get_all")
        filters = kwargs.get("filters")
        self._assert_filters(filters)
        assert 1 <= kwargs.get("top_k", 0) <= 1000
        self.calls.append(("get_all", kwargs))
        source_id = filters.get("source_id") if isinstance(filters, dict) else None
        rows = [
            row
            for row in self.rows
            if row.get("user_id") == filters.get("user_id")
            and row.get("agent_id") == filters.get("agent_id")
            and row.get("metadata", {}).get("domain") == filters.get("domain")
            and (source_id is None or row.get("metadata", {}).get("source_id") == source_id)
        ]
        return {"results": rows[: kwargs["top_k"]]}

    def delete(self, memory_id):
        self._raise("delete")
        self.calls.append(("delete", memory_id))
        self.rows[:] = [row for row in self.rows if row["id"] != memory_id]
        return {"message": "Memory deleted successfully!"}

    def delete_all(self, user_id=None, agent_id=None, run_id=None):
        self._raise("delete_all")
        assert user_id == "local-user"
        assert agent_id == "linli"
        assert run_id is None
        self.calls.append(("delete_all", {"user_id": user_id, "agent_id": agent_id}))
        self.rows.clear()
        return {"message": "Memories deleted successfully!"}


def _config(tmp_path: Path) -> Mem0Config:
    return Mem0Config(
        enabled=True,
        data_root=tmp_path / "memory" / "mem0",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="fixture-model",
        embedding_cache=tmp_path / "models",
    )


def _write_verified_embedding_cache(config: Mem0Config) -> None:
    files = {
        "1_Pooling/config.json": b"{\"word_embedding_dimension\": 512}",
        "config.json": b"{\"model_type\": \"bert\"}",
        "config_sentence_transformers.json": b"{}",
        "model.safetensors": b"synthetic weights",
        "modules.json": b"[]",
        "sentence_bert_config.json": b"{}",
        "special_tokens_map.json": b"{}",
        "tokenizer.json": b"{}",
        "tokenizer_config.json": b"{}",
        "vocab.txt": b"synthetic\n",
    }
    for relative_path, content in files.items():
        destination = config.embedding_snapshot.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (config.model_cache / "olivia-mem0-embedding-manifest.json").write_text(
        json.dumps(
            {
                "model": MEM0_EMBEDDING_MODEL,
                "revision": MEM0_EMBEDDING_MODEL_REVISION,
                "files": {
                    relative_path: hashlib.sha256(content).hexdigest()
                    for relative_path, content in files.items()
                },
            }
        ),
        encoding="utf-8",
    )


def test_version_and_config_match_current_mem0_oss_contract(tmp_path: Path) -> None:
    assert MEM0_OSS_VERSION == "2.0.18"
    config = _config(tmp_path)
    mapping = config.provider_config({"DEEPSEEK_API_KEY": "fixture-secret"})
    assert replace(config, user_id="u" * 128).user_id == "u" * 128
    with pytest.raises(ValueError, match="user_id is invalid"):
        replace(config, user_id="u" * 129)

    assert mapping["vector_store"] == {
        "provider": "qdrant",
        "config": {
            "collection_name": "olivia_conversation_memory_v1",
            "path": str(config.qdrant_path),
            "on_disk": True,
            "embedding_model_dims": 512,
        },
    }
    assert mapping["history_db_path"] == str(config.history_path)
    assert mapping["embedder"]["provider"] == "huggingface"
    assert mapping["embedder"]["config"]["model_kwargs"] == {
        "device": "cpu",
        "cache_folder": str(config.model_cache),
        "local_files_only": True,
        "revision": MEM0_EMBEDDING_MODEL_REVISION,
    }
    assert mapping["llm"]["config"]["openai_base_url"] == "http://127.0.0.1:9/v1"
    assert mapping["llm"]["config"]["api_key"] == "fixture-secret"
    assert "使用与输入消息相同的语言和文字" in mapping["custom_instructions"]
    assert "不得把中文内容翻译成英文" in mapping["custom_instructions"]
    assert "林离的第一人称" in mapping["custom_instructions"]
    assert "不得称为助手" in mapping["custom_instructions"]
    assert "private_world" not in repr(mapping)


def test_memory_extra_pins_the_verified_mem0_embedding_dependencies() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text("utf-8"))

    assert project["project"]["optional-dependencies"]["memory-mem0"] == [
        "mem0ai==2.0.18",
        "sentence-transformers==5.7.0",
    ]


def test_config_preserves_legacy_positional_config_error_slot(tmp_path: Path) -> None:
    config = Mem0Config(
        True,
        tmp_path / "legacy-mem0",
        "legacy-user",
        "legacy-agent",
        "legacy-collection",
        "http://127.0.0.1:9/v1",
        "legacy-model",
        "LEGACY_KEY",
        "BAAI/bge-small-zh-v1.5",
        512,
        tmp_path / "legacy-model-cache",
        1200,
        "MEM0_LEGACY_CONFIG_ERROR",
    )

    assert config.config_error == "MEM0_LEGACY_CONFIG_ERROR"
    assert config.write_timeout_seconds == 30.0
    assert config.search_timeout_seconds == 8.0


def test_config_timeout_values_are_real_numbers_and_normalized_to_float(
    tmp_path: Path,
) -> None:
    low = replace(
        _config(tmp_path),
        write_timeout_seconds=1,
        search_timeout_seconds=0.1,
    )
    high = replace(
        _config(tmp_path),
        write_timeout_seconds=300.0,
        search_timeout_seconds=300,
    )

    assert low.write_timeout_seconds == 1.0
    assert low.search_timeout_seconds == 0.1
    assert high.write_timeout_seconds == 300.0
    assert high.search_timeout_seconds == 300.0
    for field_name in ("write_timeout_seconds", "search_timeout_seconds"):
        for invalid in ("0.1", True, None):
            try:
                replace(_config(tmp_path), **{field_name: invalid})
            except ValueError:
                pass
            else:
                raise AssertionError(f"{field_name} must reject {invalid!r}")


def test_load_config_reuses_primary_llm_and_fails_closed(tmp_path: Path) -> None:
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
            "OLIVIA_MEMORY_ROOT": str(tmp_path / "missing"),
        },
        project_root=tmp_path,
    )
    assert incomplete.config_error == "MEM0_LLM_CONFIG_INCOMPLETE"


def test_exchange_search_export_delete_and_clear(tmp_path: Path) -> None:
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
    assert "system_prompt" not in repr(add_call)
    assert "private_world" not in repr(add_call)

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

    exported = adapter.export_user(user_id="local-user")
    assert exported["provider"] == "mem0"
    assert exported["records"][0]["source_id"] == "letter:fixture:1"
    assert "user_id" not in exported["records"][0]

    assert adapter.delete_memory("memory.fixture.1", user_id="other-user") is False
    assert adapter.delete_memory("memory.fixture.1", user_id="local-user") is True

    adapter.remember_exchange(
        user_message="我喜欢黑咖啡。",
        assistant_message="知道了。",
        occurred_at=NOW,
        source_id="letter:fixture:2",
        user_id="local-user",
    )
    assert adapter.clear_user(user_id="local-user") == 1
    assert not any(name == "delete_all" for name, _value in backend.calls)


def test_manual_memory_uses_exact_infer_false(tmp_path: Path) -> None:
    backend = FakeMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    record = adapter.add_manual_memory(
        "用户明确表示喜欢黑咖啡。",
        user_id="local-user",
        source_id="manual:fixture:1",
    )
    assert record.text == "用户明确表示喜欢黑咖啡。"
    call = next(value for name, value in backend.calls if name == "add")
    assert call["infer"] is False
    assert call["metadata"]["manual"] is True
    assert call["metadata"]["actor"] == "local_user"


def test_provider_failures_degrade_without_echoing_private_text(tmp_path: Path) -> None:
    backend = FakeMem0()
    backend.fail.add("add")
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    result = adapter.remember_exchange(
        user_message="private user text",
        assistant_message="private assistant text",
        occurred_at=NOW,
        source_id="letter:private:1",
        user_id="local-user",
    )
    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_WRITE_FAILED"
    assert "private user text" not in repr(result)
    assert "private assistant text" not in repr(result)

    backend.fail.clear()
    backend.fail.add("get_all")
    status = adapter.status().to_dict()
    assert status["status"] == "unavailable"
    assert status["reason_code"] == "MEM0_LIST_FAILED"


def test_chinese_exchange_rejects_and_deletes_english_extracted_fact(
    tmp_path: Path,
) -> None:
    class EnglishFactMem0(FakeMem0):
        def add(self, messages, **kwargs):
            value = super().add(messages, **kwargs)
            self.rows[-1]["memory"] = "User prefers quiet evenings."
            value["results"][0]["memory"] = "User prefers quiet evenings."
            return value

    backend = EnglishFactMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    result = adapter.remember_exchange(
        user_message="我喜欢安静的晚上。",
        assistant_message="我会记住的。",
        occurred_at=NOW,
        source_id="letter:fixture:chinese-language",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_LANGUAGE_MISMATCH"
    assert backend.rows == []
    assert ("delete", "memory.fixture.1") in backend.calls


def test_non_history_language_mismatch_rolls_back_valid_siblings(tmp_path: Path) -> None:
    class MixedLanguageMem0(FakeMem0):
        def add(self, messages, **kwargs):
            value = super().add(messages, **kwargs)
            self.rows[-1]["memory"] = "用户喜欢安静的晚上。"
            value["results"][0]["memory"] = "用户喜欢安静的晚上。"
            invalid = {
                "id": "memory.fixture.english-sibling",
                "memory": "User prefers quiet evenings.",
                "user_id": kwargs["user_id"],
                "agent_id": kwargs["agent_id"],
                "metadata": dict(kwargs["metadata"]),
                "created_at": NOW.isoformat(),
            }
            self.rows.append(invalid)
            value["results"].append(
                {
                    "id": invalid["id"],
                    "memory": invalid["memory"],
                    "event": "ADD",
                }
            )
            return value

    backend = MixedLanguageMem0()
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="我喜欢安静的晚上。",
        assistant_message="我会记住的。",
        occurred_at=NOW,
        source_id="letter:fixture:mixed-language",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_LANGUAGE_MISMATCH"
    assert backend.rows == []


@pytest.mark.parametrize(
    "legacy_text",
    (
        "Ornn 第一天给弹钢琴的助手写信。",
        "AI 表示很高兴收到 Ornn 的信。",
        "林离说她很高兴收到 Ornn 的信。",
    ),
)
def test_historical_exchange_replaces_non_first_person_identity_memory(
    tmp_path: Path, legacy_text: str,
) -> None:
    backend = FakeMem0()
    backend.rows.append(
        {
            "id": "memory.legacy.assistant",
            "memory": legacy_text,
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "history:fixture",
                "domain": "conversation_memory",
                "canonical": True,
            },
            "created_at": NOW.isoformat(),
        }
    )
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    result = adapter.remember_exchange(
        user_message="这是我第一天写信。",
        assistant_message="很高兴收到你的信。",
        occurred_at=NOW,
        source_id="history:fixture",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.WRITTEN
    assert ("delete", "memory.legacy.assistant") in backend.calls
    assert len([name for name, _value in backend.calls if name == "add"]) == 2


def test_historical_exchange_extracts_user_and_linli_facts_by_actor(
    tmp_path: Path,
) -> None:
    class ActorAwareMem0(FakeMem0):
        def add(self, messages, **kwargs):
            value = super().add(messages, **kwargs)
            actor = kwargs["metadata"]["history_actor"]
            fact = (
                "用户喜欢 AI 绘画，也喜欢林离的钢琴。"
                if actor == "user"
                else "我在回信中鼓励用户继续画画。"
            )
            self.rows[-1]["memory"] = fact
            value["results"][0]["memory"] = fact
            return value

    backend = ActorAwareMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    result = adapter.remember_exchange(
        user_message="我喜欢 AI 绘画，也喜欢林离的钢琴。",
        assistant_message="我会继续用钢琴陪你画画。",
        occurred_at=NOW,
        source_id="history:actor-split",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.WRITTEN
    add_calls = [value for name, value in backend.calls if name == "add"]
    assert [call["metadata"]["history_actor"] for call in add_calls] == [
        "user",
        "linli",
    ]
    assert [call["messages"][0]["role"] for call in add_calls] == [
        "user",
        "user",
    ]
    assert "用户本人" in add_calls[0]["prompt"]
    assert "第一人称" in add_calls[1]["prompt"]
    assert all("不得翻译为英文" in call["prompt"] for call in add_calls)
    assert [row["memory"] for row in backend.rows] == [
        "用户喜欢 AI 绘画，也喜欢林离的钢琴。",
        "我在回信中鼓励用户继续画画。",
    ]
    selected = adapter.search_context("画画", user_id="local-user", limit=8)
    assert [record.metadata["history_actor"] for record in selected] == [
        "user",
        "linli",
    ]


def test_historical_linli_fact_uses_provider_compatible_user_shaped_input(
    tmp_path: Path,
) -> None:
    class RejectsAssistantOnlyMem0(FakeMem0):
        def add(self, messages, **kwargs):
            if len(messages) == 1 and messages[0]["role"] == "assistant":
                raise RuntimeError("assistant-only input is unsupported")
            value = super().add(messages, **kwargs)
            actor = kwargs["metadata"]["history_actor"]
            fact = "用户喜欢画画。" if actor == "user" else "我曾鼓励用户继续画画。"
            self.rows[-1]["memory"] = fact
            value["results"][0]["memory"] = fact
            return value

    backend = RejectsAssistantOnlyMem0()
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="我喜欢画画。",
        assistant_message="我会继续鼓励你画画。",
        occurred_at=NOW,
        source_id="history:user-shaped-linli",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.WRITTEN
    add_calls = [value for name, value in backend.calls if name == "add"]
    assert [call["messages"][0]["role"] for call in add_calls] == ["user", "user"]
    assert "林离的历史回信" in add_calls[1]["messages"][0]["content"]
    assert "我会继续鼓励你画画。" in add_calls[1]["messages"][0]["content"]
    assert add_calls[1]["metadata"]["history_actor"] == "linli"


@pytest.mark.parametrize(
    "bad_text",
    ("AI 回复了来信。", "林离说她回复了来信。", "过去曾温柔地回复这封信。"),
)
def test_historical_exchange_rejects_non_first_person_new_memory(
    tmp_path: Path, bad_text: str,
) -> None:
    class NonFirstPersonMem0(FakeMem0):
        def add(self, messages, **kwargs):
            value = super().add(messages, **kwargs)
            self.rows[-1]["memory"] = bad_text
            value["results"][0]["memory"] = bad_text
            return value

    backend = NonFirstPersonMem0()
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="这是我的旧信。",
        assistant_message="这是过去的回信。",
        occurred_at=NOW,
        source_id="history:non-first-person",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_CHARACTER_IDENTITY_MISMATCH"
    assert backend.rows == []


def test_historical_actor_split_rebuilds_a_partial_source(tmp_path: Path) -> None:
    backend = FakeMem0()
    backend.rows.append(
        {
            "id": "memory.partial.user",
            "memory": "用户喜欢 AI 绘画。",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "history:partial",
                "domain": "conversation_memory",
                "canonical": True,
                "history_actor": "user",
            },
            "created_at": NOW.isoformat(),
        }
    )

    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="我喜欢 AI 绘画。",
        assistant_message="我会记住。",
        occurred_at=NOW,
        source_id="history:partial",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.WRITTEN
    assert ("delete", "memory.partial.user") in backend.calls
    assert {
        row["metadata"]["history_actor"] for row in backend.rows
    } == {"user", "linli"}


def test_historical_exchange_rebuilds_records_from_an_older_extractor(
    tmp_path: Path,
) -> None:
    backend = FakeMem0()
    for actor, text in (
        ("user", "用户喜欢画画。"),
        ("linli", "我会继续鼓励用户画画。"),
    ):
        backend.rows.append(
            {
                "id": f"memory.old.{actor}",
                "memory": text,
                "user_id": "local-user",
                "agent_id": "linli",
                "metadata": {
                    "source_id": "history:old-extractor",
                    "domain": "conversation_memory",
                    "canonical": True,
                    "history_actor": actor,
                },
                "created_at": NOW.isoformat(),
            }
        )

    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="我喜欢画画。",
        assistant_message="我会继续鼓励你画画。",
        occurred_at=NOW,
        source_id="history:old-extractor",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.WRITTEN
    assert {value for name, value in backend.calls if name == "delete"} >= {
        "memory.old.user",
        "memory.old.linli",
    }
    assert {
        row["metadata"]["history_extraction_version"] for row in backend.rows
    } == {"relationship-v2"}


def test_historical_identity_rejection_reports_rollback_failure(tmp_path: Path) -> None:
    class NonFirstPersonMem0(FakeMem0):
        def add(self, messages, **kwargs):
            value = super().add(messages, **kwargs)
            self.rows[-1]["memory"] = "林离说她回复了来信。"
            value["results"][0]["memory"] = "林离说她回复了来信。"
            return value

    backend = NonFirstPersonMem0()
    backend.fail.add("delete")
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="这是我的旧信。",
        assistant_message="这是过去的回信。",
        occurred_at=NOW,
        source_id="history:rollback-failure",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_CHARACTER_IDENTITY_MISMATCH_ROLLBACK_FAILED"
    assert result.memory_ids == ("memory.fixture.1", "memory.fixture.2")


def test_historical_actor_split_rolls_back_user_fact_when_linli_add_fails(
    tmp_path: Path,
) -> None:
    class LinliAddFails(FakeMem0):
        def add(self, messages, **kwargs):
            if kwargs["metadata"]["history_actor"] == "linli":
                raise RuntimeError("synthetic provider failure")
            return super().add(messages, **kwargs)

    backend = LinliAddFails()
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="我喜欢 AI 绘画。",
        assistant_message="我会记住。",
        occurred_at=NOW,
        source_id="history:actor-rollback",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_WRITE_FAILED"
    assert backend.rows == []


def test_historical_actor_split_discards_only_invalid_candidate(tmp_path: Path) -> None:
    class MixedLanguageMem0(FakeMem0):
        def add(self, messages, **kwargs):
            value = super().add(messages, **kwargs)
            actor = kwargs["metadata"]["history_actor"]
            valid = (
                "用户最近改成夜班，也会使用 AI 整理会议记录。"
                if actor == "user"
                else "我记得自己曾认真回复过这封信。"
            )
            self.rows[-1]["memory"] = valid
            value["results"][0]["memory"] = valid
            if actor == "linli":
                invalid = {
                    "id": "memory.fixture.english",
                    "memory": "The assistant summarized the user's letter in English.",
                    "user_id": kwargs["user_id"],
                    "agent_id": kwargs["agent_id"],
                    "metadata": dict(kwargs["metadata"]),
                    "created_at": NOW.isoformat(),
                }
                self.rows.append(invalid)
                value["results"].append(
                    {
                        "id": invalid["id"],
                        "memory": invalid["memory"],
                        "event": "ADD",
                    }
                )
            return value

    backend = MixedLanguageMem0()
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="最近改成夜班，也会用 AI 整理会议记录。",
        assistant_message="我记得自己曾认真回复过这封信。",
        occurred_at=NOW,
        source_id="history:mixed-language-candidates",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.WRITTEN
    assert result.memory_ids == ("memory.fixture.1", "memory.fixture.2")
    assert {row["id"] for row in backend.rows} == {
        "memory.fixture.1",
        "memory.fixture.2",
    }
    assert ("delete", "memory.fixture.english") in backend.calls


def test_historical_actor_split_allows_an_actor_with_no_durable_fact(
    tmp_path: Path,
) -> None:
    class EmptyLinliFactsMem0(FakeMem0):
        def add(self, messages, **kwargs):
            if kwargs["metadata"]["history_actor"] == "linli":
                self.calls.append(("add", {"messages": messages, **kwargs}))
                return {"results": []}
            value = super().add(messages, **kwargs)
            self.rows[-1]["memory"] = "用户最近改成夜班。"
            value["results"][0]["memory"] = "用户最近改成夜班。"
            return value

    backend = EmptyLinliFactsMem0()
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="最近改成夜班。",
        assistant_message="我知道了。",
        occurred_at=NOW,
        source_id="history:empty-linli-facts",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.WRITTEN
    assert result.memory_ids == ("memory.fixture.1",)
    assert [row["memory"] for row in backend.rows] == ["用户最近改成夜班。"]


def test_chinese_exchange_retries_cleanup_instead_of_accepting_english_duplicate(
    tmp_path: Path,
) -> None:
    class EnglishThenChineseMem0(FakeMem0):
        def add(self, messages, **kwargs):
            value = super().add(messages, **kwargs)
            if self.counter == 1:
                self.rows[-1]["memory"] = "User prefers quiet evenings."
                value["results"][0]["memory"] = "User prefers quiet evenings."
            return value

    backend = EnglishThenChineseMem0()
    backend.fail.add("delete")
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    first = adapter.remember_exchange(
        user_message="我喜欢安静的晚上。",
        assistant_message="我会记住的。",
        occurred_at=NOW,
        source_id="letter:fixture:chinese-retry",
        user_id="local-user",
    )
    assert first.error_code == "MEM0_LANGUAGE_MISMATCH_ROLLBACK_FAILED"
    assert len(backend.rows) == 1

    backend.fail.clear()
    retry = adapter.remember_exchange(
        user_message="我喜欢安静的晚上。",
        assistant_message="我会记住的。",
        occurred_at=NOW,
        source_id="letter:fixture:chinese-retry",
        user_id="local-user",
    )

    assert retry.status is MemoryWriteStatus.WRITTEN
    assert retry.memory_ids == ("memory.fixture.2",)
    assert [row["memory"] for row in backend.rows] == ["用户在东京工作。"]


def test_outbox_retry_after_a_timed_out_source_check_never_adds_duplicate(
    tmp_path: Path,
) -> None:
    release = threading.Event()

    class DelayedMem0(FakeMem0):
        def get_all(self, **kwargs):
            release.wait()
            return super().get_all(**kwargs)

    backend = DelayedMem0()
    backend.rows.append(
        {
            "id": "memory.existing",
            "memory": "already canonical",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "reply:letter-1:1",
                "domain": "conversation_memory",
            },
        }
    )
    config = replace(_config(tmp_path), search_timeout_seconds=0.1)
    adapter = Mem0ConversationMemoryAdapter(backend, config)
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "letters": [
                    {
                        "letter_id": "letter-1",
                        "reply_revision": 1,
                        "letter_status": "COMPLETED",
                        "content": "synthetic user",
                        "reply_text": "synthetic reply",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    outbox = CanonicalMemoryOutbox(
        tmp_path / "state.json",
        tmp_path / "memory" / "mem0" / "delivery.sqlite3",
        ConversationMemoryDeliveryCommitter(adapter),
    )
    assert asyncio.run(outbox.scan_once()).pending == 1
    release.set()
    assert asyncio.run(outbox.scan_once()).duplicates == 1
    assert not [call for call in backend.calls if call[0] == "add"]


def test_status_and_list_fail_closed_for_malformed_provider_pages(tmp_path: Path) -> None:
    class PageMem0(FakeMem0):
        def __init__(self, response: object) -> None:
            super().__init__()
            self.response = response

        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return self.response

    pages = (
        {}, [], {"results": [], "error": "synthetic provider error"},
        {"results": [], "status": "failed"}, {"results": [], "next": None},
        {"results": [{}]},
        {"results": [{"memory_id": "memory.alias", "text": "synthetic", "user_id": "local-user", "agent_id": "linli", "metadata": {"source_id": "reply:alias:1", "domain": "conversation_memory"}}]},
        {"results": [{"id": "memory.top-level", "memory": "synthetic", "user_id": "local-user", "agent_id": "linli", "source_id": "reply:top-level:1", "metadata": {"domain": "conversation_memory"}}]},
        *({"results": [{"id": "memory.row-marker", "memory": "synthetic", "user_id": "local-user", "agent_id": "linli", "metadata": {"source_id": "reply:row-marker:1", "domain": "conversation_memory"}, marker: "failed"}]} for marker in ("error", "status")),
    )
    for page in pages:
        adapter = Mem0ConversationMemoryAdapter(PageMem0(page), _config(tmp_path))
        try:
            adapter.list_memories(user_id="local-user", limit=1)
        except Mem0AdapterError as exc:
            assert exc.code == "MEM0_LIST_FAILED"
        else:
            raise AssertionError("malformed list page must not become an empty list")
        assert adapter.status().to_dict()["reason_code"] == "MEM0_LIST_FAILED"


def test_search_fails_closed_for_top_level_only_source_id(tmp_path: Path) -> None:
    class TopLevelSourceIdMem0(FakeMem0):
        def search(self, query, **kwargs):
            super().search(query, **kwargs)
            return {
                "results": [
                    {
                        "id": "memory.top-level-search-source",
                        "memory": "synthetic memory",
                        "user_id": "local-user",
                        "agent_id": "linli",
                        "source_id": "reply:top-level-search-source:1",
                        "metadata": {"domain": "conversation_memory"},
                    }
                ]
            }

    adapter = Mem0ConversationMemoryAdapter(TopLevelSourceIdMem0(), _config(tmp_path))

    assert adapter.search_context("synthetic", user_id="local-user", limit=1) == ()
    assert adapter._last_error_code == "MEM0_SEARCH_FAILED"


def test_exact_dedupe_fails_closed_for_top_level_only_source_id(tmp_path: Path) -> None:
    source_id = "reply:top-level-dedup-source:1"

    class TopLevelSourceIdMem0(FakeMem0):
        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return {
                "results": [
                    {
                        "id": "memory.top-level-dedup-source",
                        "memory": "synthetic memory",
                        "user_id": "local-user",
                        "agent_id": "linli",
                        "source_id": source_id,
                        "metadata": {"domain": "conversation_memory"},
                    }
                ]
            }

    backend = TopLevelSourceIdMem0()
    result = Mem0ConversationMemoryAdapter(backend, _config(tmp_path)).remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id=source_id,
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
    assert not [call for call in backend.calls if call[0] == "add"]


def test_list_fails_closed_when_limited_page_contains_mixed_valid_and_invalid_rows(
    tmp_path: Path,
) -> None:
    class MixedRowsMem0(FakeMem0):
        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return {
                "results": [
                    {
                        "id": "memory.valid-row",
                        "memory": "synthetic memory",
                        "user_id": "local-user",
                        "agent_id": "linli",
                        "metadata": {
                            "source_id": "reply:valid-row:1",
                            "domain": "conversation_memory",
                        },
                    },
                    {},
                ]
            }

    adapter = Mem0ConversationMemoryAdapter(MixedRowsMem0(), _config(tmp_path))

    try:
        adapter.list_memories(user_id="local-user", limit=1)
    except Mem0AdapterError as exc:
        assert exc.code == "MEM0_LIST_FAILED"
    else:
        raise AssertionError("mixed list page must not become an empty list")


def test_list_fails_closed_when_result_row_is_not_fully_scoped(tmp_path: Path) -> None:
    class UnscopedRowMem0(FakeMem0):
        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return {
                "results": [
                    {
                        "id": "memory.unscoped-row",
                        "memory": "synthetic memory",
                        "user_id": "local-user",
                        "metadata": {"source_id": "reply:unscoped-row:1"},
                    }
                ]
            }

    adapter = Mem0ConversationMemoryAdapter(UnscopedRowMem0(), _config(tmp_path))

    try:
        adapter.list_memories(user_id="local-user", limit=1)
    except Mem0AdapterError as exc:
        assert exc.code == "MEM0_LIST_FAILED"
    else:
        raise AssertionError("unscoped list page must not become an empty list")


def test_search_fails_closed_for_malformed_provider_pages(tmp_path: Path) -> None:
    class PageMem0(FakeMem0):
        def __init__(self, response: object) -> None:
            super().__init__()
            self.response = response

        def search(self, query, **kwargs):
            super().search(query, **kwargs)
            return self.response

    pages = (
        {}, [], {"results": [], "error": "synthetic provider error"},
        {"results": [], "status": "failed"}, {"results": [], "next": None},
        {"results": [{}]},
        {"results": [{"id": "memory.alias-search", "text": "synthetic", "user_id": "local-user", "agent_id": "linli", "metadata": {"source_id": "reply:alias-search:1", "domain": "conversation_memory"}}]},
        *({"results": [{"id": "memory.row-marker-search", "memory": "synthetic", "user_id": "local-user", "agent_id": "linli", "metadata": {"source_id": "reply:row-marker-search:1", "domain": "conversation_memory"}, marker: "failed"}]} for marker in ("error", "status")),
    )
    for page in pages:
        adapter = Mem0ConversationMemoryAdapter(PageMem0(page), _config(tmp_path))
        assert adapter.search_context("synthetic", user_id="local-user", limit=1) == ()
        assert adapter._last_error_code == "MEM0_SEARCH_FAILED"


def test_exchange_fails_closed_for_malformed_add_acknowledgements(
    tmp_path: Path,
) -> None:
    class AddMem0(FakeMem0):
        def __init__(self, response: object) -> None:
            super().__init__()
            self.response = response

        def add(self, messages, **kwargs):
            super().add(messages, **kwargs)
            return self.response

    responses = (
        {}, [], {"results": [], "error": "synthetic provider error"},
        {"results": [], "status": "failed"}, {"results": [], "next": None},
        {"results": [{}]},
        {"results": [{"id": "memory.fixture.1"}]},
        {"results": [{"memory_id": "memory.fixture.1", "memory": "synthetic", "event": "ADD"}]},
        {"results": [{"id": "memory.fixture.1", "memory": "synthetic", "event": "ADD", "error": "synthetic provider error"}]},
        {"results": [{"id": "memory.fixture.1", "memory": "synthetic", "event": "ADD", "status": "failed"}]},
    )
    for response in responses:
        result = Mem0ConversationMemoryAdapter(AddMem0(response), _config(tmp_path)).remember_exchange(
            user_message="synthetic user", assistant_message="synthetic reply",
            occurred_at=NOW, source_id="reply:malformed-add:1", user_id="local-user",
        )
        assert result.status is MemoryWriteStatus.UNAVAILABLE
        assert result.error_code == "MEM0_WRITE_FAILED"


def test_exchange_accepts_results_only_empty_add_response(tmp_path: Path) -> None:
    class EmptyAddMem0(FakeMem0):
        def add(self, messages, **kwargs):
            super().add(messages, **kwargs)
            return {"results": []}

    result = Mem0ConversationMemoryAdapter(
        EmptyAddMem0(), _config(tmp_path)
    ).remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:empty-add-response:1",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.SKIPPED
    assert result.error_code is None


def test_results_only_empty_pages_remain_valid_for_status_and_search(tmp_path: Path) -> None:
    class ResultsOnlyMem0(FakeMem0):
        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return {"results": []}

        def search(self, query, **kwargs):
            super().search(query, **kwargs)
            return {"results": []}

    adapter = Mem0ConversationMemoryAdapter(ResultsOnlyMem0(), _config(tmp_path))

    assert adapter.status().to_dict() == {
        "status": "available",
        "enabled": True,
        "provider": "mem0",
        "storage": "qdrant-local",
        "memory_count": 0,
    }
    assert adapter.search_context("synthetic", user_id="local-user", limit=1) == ()
    assert adapter._last_error_code is None


def test_exchange_deduplicates_exact_source_id_beyond_the_listing_window(
    tmp_path: Path,
) -> None:
    backend = FakeMem0()
    for index in range(1000):
        backend.rows.append(
            {
                "id": f"memory.seed.{index}",
                    "memory": "synthetic memory",
                    "user_id": "local-user",
                    "agent_id": "linli",
                    "metadata": {
                    "source_id": f"reply:seed:{index}",
                    "domain": "conversation_memory",
                },
            }
        )
    backend.rows.append(
        {
            "id": "memory.duplicate.outside-window",
            "memory": "synthetic duplicate",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "reply:outside-window:1",
                "domain": "conversation_memory",
            },
        }
    )
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    result = adapter.remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:outside-window:1",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.DUPLICATE
    dedup_call = next(value for name, value in backend.calls if name == "get_all")
    assert dedup_call["filters"]["source_id"] == "reply:outside-window:1"
    assert not [call for call in backend.calls if call[0] == "add"]


def test_exchange_fails_closed_when_exact_source_id_query_fails(tmp_path: Path) -> None:
    backend = FakeMem0()
    backend.fail.add("get_all")
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    result = adapter.remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:query-failure:1",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
    assert not [call for call in backend.calls if call[0] == "add"]


def test_exchange_fails_closed_when_exact_source_id_response_is_not_a_result_page(
    tmp_path: Path,
) -> None:
    class InvalidExactQueryMem0(FakeMem0):
        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return {}

    backend = InvalidExactQueryMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    result = adapter.remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:invalid-query-page:1",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
    assert not [call for call in backend.calls if call[0] == "add"]


def test_exchange_fails_closed_when_exact_source_id_response_is_bare_list(
    tmp_path: Path,
) -> None:
    class BareListExactQueryMem0(FakeMem0):
        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return []

    backend = BareListExactQueryMem0()
    result = Mem0ConversationMemoryAdapter(
        backend, _config(tmp_path)
    ).remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:bare-list-dedup:1",
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
    assert not [call for call in backend.calls if call[0] == "add"]


def test_exchange_fails_closed_for_each_invalid_exact_source_id_response(
    tmp_path: Path,
) -> None:
    class InvalidExactQueryMem0(FakeMem0):
        def __init__(self, response: object) -> None:
            super().__init__()
            self.response = response

        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return self.response

    for response in (
        {"memories": []},
        {"results": {}},
        {"results": "not-a-sequence"},
        {"results": [], "error": "synthetic provider error"},
        {"results": [], "status": "failed"},
        7,
    ):
        backend = InvalidExactQueryMem0(response)
        adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

        result = adapter.remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id="reply:invalid-query-shape:1",
            user_id="local-user",
        )

        assert result.status is MemoryWriteStatus.UNAVAILABLE
        assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
        assert not [call for call in backend.calls if call[0] == "add"]


def test_exchange_fails_closed_when_exact_source_row_scope_is_missing_or_mismatched(
    tmp_path: Path,
) -> None:
    source_id = "reply:scope-check:1"

    class ScopedExactQueryMem0(FakeMem0):
        def __init__(self, row: dict[str, object]) -> None:
            super().__init__()
            self.row = row

        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return {"results": [self.row]}

    base_row = {
        "id": "memory.scope-check",
        "memory": "synthetic prior exchange",
        "user_id": "local-user",
        "agent_id": "linli",
        "metadata": {
            "source_id": source_id,
            "domain": "conversation_memory",
        },
    }
    invalid_rows = []
    for key in ("user_id", "agent_id"):
        row = dict(base_row)
        row.pop(key)
        invalid_rows.append(row)
    missing_domain = dict(base_row)
    missing_domain["metadata"] = {"source_id": source_id}
    invalid_rows.append(missing_domain)
    for key, value in (
        ("user_id", "other-user"),
        ("agent_id", "other-agent"),
    ):
        row = dict(base_row)
        row[key] = value
        invalid_rows.append(row)
    mismatched_domain = dict(base_row)
    mismatched_domain["metadata"] = {
        "source_id": source_id,
        "domain": "other-domain",
    }
    invalid_rows.append(mismatched_domain)
    invalid_rows.extend(
        {**base_row, marker: "failed"} for marker in ("error", "status")
    )

    for row in invalid_rows:
        backend = ScopedExactQueryMem0(row)
        result = Mem0ConversationMemoryAdapter(
            backend, _config(tmp_path)
        ).remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id=source_id,
            user_id="local-user",
        )

        assert result.status is MemoryWriteStatus.UNAVAILABLE
        assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
        assert not [call for call in backend.calls if call[0] == "add"]


def test_exchange_fails_closed_for_exact_source_read_field_aliases(
    tmp_path: Path,
) -> None:
    source_id = "reply:alias-dedup:1"

    class AliasExactQueryMem0(FakeMem0):
        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return {
                "results": [
                    {
                        "memory_id": "memory.alias-dedup",
                        "text": "synthetic memory",
                        "user_id": "local-user",
                        "agent_id": "linli",
                        "metadata": {
                            "source_id": source_id,
                            "domain": "conversation_memory",
                        },
                    }
                ]
            }

    backend = AliasExactQueryMem0()
    result = Mem0ConversationMemoryAdapter(
        backend, _config(tmp_path)
    ).remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id=source_id,
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.UNAVAILABLE
    assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
    assert not [call for call in backend.calls if call[0] == "add"]


def test_exchange_accepts_results_only_empty_exact_source_id_page(
    tmp_path: Path,
) -> None:
    class ExactQueryMem0(FakeMem0):
        def __init__(self, response: object) -> None:
            super().__init__()
            self.response = response

        def get_all(self, **kwargs):
            super().get_all(**kwargs)
            return self.response

    for response in ({"results": []}, {"results": [], "has_more": False}):
        backend = ExactQueryMem0(response)
        adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

        result = adapter.remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id="reply:valid-query-page:1",
            user_id="local-user",
        )

        assert result.status is MemoryWriteStatus.WRITTEN
        assert len([call for call in backend.calls if call[0] == "add"]) == 1


def test_exchange_accepts_results_only_existing_exact_source_id_page(
    tmp_path: Path,
) -> None:
    source_id = "reply:results-only-existing:1"

    class ExactQueryMem0(FakeMem0):
        def get_all(self, **kwargs):
            response = super().get_all(**kwargs)
            return {"results": response["results"]}

    backend = ExactQueryMem0()
    backend.rows.append(
        {
            "id": "memory.fixture.existing",
            "memory": "synthetic prior exchange",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": source_id,
                "domain": "conversation_memory",
            },
            "created_at": NOW.isoformat(),
        }
    )
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    result = adapter.remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id=source_id,
        user_id="local-user",
    )

    assert result.status is MemoryWriteStatus.DUPLICATE
    assert not [call for call in backend.calls if call[0] == "add"]


def test_concurrent_exchange_serializes_exact_deduplication_and_write(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class GatedExactQueryMem0(FakeMem0):
        def get_all(self, **kwargs):
            entered.set()
            assert release.wait(1.0)
            return super().get_all(**kwargs)

    backend = GatedExactQueryMem0()
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    results: list[MemoryWriteResult] = []

    def remember() -> None:
        results.append(
            adapter.remember_exchange(
                user_message="synthetic user",
                assistant_message="synthetic reply",
                occurred_at=NOW,
                source_id="reply:concurrent:1",
                user_id="local-user",
            )
        )

    first = threading.Thread(target=remember, daemon=True)
    second = threading.Thread(target=remember, daemon=True)
    first.start()
    assert entered.wait(0.5)
    second.start()
    release.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(result.status for result in results) == [
        MemoryWriteStatus.DUPLICATE,
        MemoryWriteStatus.WRITTEN,
    ]
    assert len([call for call in backend.calls if call[0] == "add"]) == 1


def test_exchange_write_timeout_keeps_one_daemon_and_fails_closed_on_retry(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    existing_threads = set(threading.enumerate())

    class BlockingExactQueryMem0(FakeMem0):
        def get_all(self, **kwargs):
            entered.set()
            release.wait()
            return super().get_all(**kwargs)

    backend = BlockingExactQueryMem0()
    adapter = Mem0ConversationMemoryAdapter(
        backend,
        replace(_config(tmp_path), write_timeout_seconds=0.1),
    )
    try:
        first = adapter.remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id="reply:write-timeout:1",
            user_id="local-user",
        )
        assert entered.is_set()
        retry = adapter.remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id="reply:write-timeout:1",
            user_id="local-user",
        )

        assert first.error_code == "MEM0_WRITE_TIMEOUT"
        assert retry.error_code == "MEM0_WRITE_TIMEOUT"
        assert not [call for call in backend.calls if call[0] == "add"]
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "olivia-mem0-write" and thread not in existing_threads
        ]
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "olivia-mem0-write" and thread not in existing_threads:
                thread.join(timeout=0.5)


def test_timed_out_exchange_can_be_reconciled_to_its_persisted_source(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingAddMem0(FakeMem0):
        def add(self, messages, **kwargs):
            entered.set()
            release.wait()
            return super().add(messages, **kwargs)

    backend = BlockingAddMem0()
    adapter = Mem0ConversationMemoryAdapter(
        backend,
        replace(_config(tmp_path), write_timeout_seconds=0.1),
    )
    timed_out = adapter.remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:write-timeout:settle",
        user_id="local-user",
    )
    assert entered.is_set()
    assert timed_out.error_code == "MEM0_WRITE_TIMEOUT"

    release.set()
    settled = adapter.settle_exchange_write(
        source_id="reply:write-timeout:settle",
        user_id="local-user",
    )

    assert settled.status is MemoryWriteStatus.WRITTEN
    assert settled.memory_ids == ("memory.fixture.1",)


def test_timed_out_exchange_settlement_is_bounded_when_provider_stays_blocked(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingExactQueryMem0(FakeMem0):
        def get_all(self, **kwargs):
            entered.set()
            release.wait()
            return super().get_all(**kwargs)

    adapter = Mem0ConversationMemoryAdapter(
        BlockingExactQueryMem0(),
        replace(_config(tmp_path), write_timeout_seconds=0.1),
    )
    timed_out = adapter.remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:write-timeout:bounded-settle",
        user_id="local-user",
    )
    assert entered.is_set()
    assert timed_out.error_code == "MEM0_WRITE_TIMEOUT"

    delayed_release = threading.Timer(0.4, release.set)
    delayed_release.start()
    try:
        started = time.monotonic()
        settled = adapter.settle_exchange_write(
            source_id="reply:write-timeout:bounded-settle",
            user_id="local-user",
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        delayed_release.cancel()
        delayed_release.join(timeout=1.0)

    assert elapsed < 0.3
    assert settled.status is MemoryWriteStatus.UNAVAILABLE
    assert settled.error_code == "MEM0_WRITE_UNCERTAIN"


def test_timed_out_duplicate_settles_as_duplicate_without_created_ids(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowDedupMem0(FakeMem0):
        def get_all(self, **kwargs):
            entered.set()
            release.wait()
            return super().get_all(**kwargs)

    backend = SlowDedupMem0()
    backend.rows.append(
        {
            "id": "memory.existing",
            "memory": "用户喜欢安静的晚上。",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "reply:write-timeout:duplicate",
                "occurred_at": NOW.isoformat(),
                "domain": "conversation_memory",
                "canonical": True,
            },
            "created_at": NOW.isoformat(),
        }
    )
    adapter = Mem0ConversationMemoryAdapter(
        backend,
        replace(_config(tmp_path), write_timeout_seconds=0.1),
    )
    timed_out = adapter.remember_exchange(
        user_message="这是重复消息。",
        assistant_message="这是重复回复。",
        occurred_at=NOW,
        source_id="reply:write-timeout:duplicate",
        user_id="local-user",
    )
    assert entered.is_set()
    assert timed_out.error_code == "MEM0_WRITE_TIMEOUT"

    release.set()
    settled = adapter.settle_exchange_write(
        source_id="reply:write-timeout:duplicate",
        user_id="local-user",
    )

    assert settled.status is MemoryWriteStatus.DUPLICATE
    assert settled.memory_ids == ()
    assert [row["id"] for row in backend.rows] == ["memory.existing"]


def test_exchange_fails_closed_when_exact_source_id_page_is_incomplete(
    tmp_path: Path,
) -> None:
    class IncompletePageMem0(FakeMem0):
        def __init__(self, marker: str, value: object) -> None:
            super().__init__()
            self.marker = marker
            self.value = value

        def get_all(self, **kwargs):
            response = super().get_all(**kwargs)
            return {**response, self.marker: self.value}

    for marker, value in (
        ("has_more", True),
        ("has_more", "true"),
        ("has_more", 1),
        ("has_more", 0),
        ("has_more", None),
        ("next", "synthetic-next-page"),
        ("next_cursor", "synthetic-next-page"),
        ("next_page", "synthetic-next-page"),
        ("next_token", "synthetic-next-page"),
        ("next", None),
        ("next", ""),
        ("next", 0),
        ("next", False),
        ("next_cursor", None),
        ("next_cursor", ""),
        ("next_cursor", 0),
        ("next_cursor", False),
        ("next_page", None),
        ("next_page", ""),
        ("next_page", 0),
        ("next_page", False),
        ("next_token", None),
        ("next_token", ""),
        ("next_token", 0),
        ("next_token", False),
    ):
        backend = IncompletePageMem0(marker, value)
        adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

        result = adapter.remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id="reply:incomplete-page:1",
            user_id="local-user",
        )

        assert result.status is MemoryWriteStatus.UNAVAILABLE
        assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
        assert not [call for call in backend.calls if call[0] == "add"]


def test_model_facing_calls_share_one_bounded_read_slot_and_dedup_fails_closed(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    entered = threading.Event()
    existing_threads = set(threading.enumerate())

    class BlockingMem0(FakeMem0):
        def __init__(self) -> None:
            super().__init__()
            self.get_all_calls = 0
            self.search_calls = 0

        def get_all(self, **kwargs):
            self.get_all_calls += 1
            entered.set()
            release.wait()
            return super().get_all(**kwargs)

        def search(self, query, **kwargs):
            self.search_calls += 1
            return super().search(query, **kwargs)

    backend = BlockingMem0()
    backend.rows.append(
        {
            "id": "memory.existing",
            "memory": "synthetic memory",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "reply:timeout:1",
                "domain": "conversation_memory",
            },
        }
    )
    adapter = Mem0ConversationMemoryAdapter(
        backend,
        replace(_config(tmp_path), search_timeout_seconds=0.1),
    )
    status_result: list[object] = []
    status_done = threading.Event()

    def read_status() -> None:
        status_result.append(adapter.status())
        status_done.set()

    probe = threading.Thread(target=read_status, daemon=True)

    try:
        probe.start()
        assert status_done.wait(0.25)
        assert status_result[0].reason_code == "MEM0_SEARCH_TIMEOUT"
        assert entered.is_set()
        assert adapter.search_context("synthetic", user_id="local-user", limit=1) == ()
        result = adapter.remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id="reply:timeout:1",
            user_id="local-user",
        )
        assert result.status is MemoryWriteStatus.UNAVAILABLE
        assert result.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
        assert backend.search_calls == 0
        assert not [call for call in backend.calls if call[0] == "add"]
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "olivia-mem0-read" and thread not in existing_threads
        ]
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()
        probe.join(timeout=0.5)
        for thread in threading.enumerate():
            if thread.name == "olivia-mem0-read" and thread not in existing_threads:
                thread.join(timeout=0.5)

    retry = adapter.remember_exchange(
        user_message="synthetic user",
        assistant_message="synthetic reply",
        occurred_at=NOW,
        source_id="reply:timeout:1",
        user_id="local-user",
    )
    assert retry.status is MemoryWriteStatus.DUPLICATE
    assert not [call for call in backend.calls if call[0] == "add"]


def test_factory_is_lazy_and_returns_stable_disabled_or_unavailable_ports(tmp_path: Path) -> None:
    assert isinstance(
        create_mem0_adapter(
            Mem0Config(enabled=False, data_root=tmp_path / "disabled")
        ),
        NullConversationMemoryPort,
    )

    broken = Mem0Config(
        enabled=True,
        data_root=tmp_path / "broken",
        config_error="MEM0_LLM_CONFIG_INCOMPLETE",
    )
    broken_port = create_mem0_adapter(broken)
    assert isinstance(broken_port, UnavailableConversationMemoryPort)
    assert broken_port.config is broken

    captured: dict[str, object] = {}
    backend = FakeMem0()

    def factory(config):
        captured.update(config)
        return backend

    _write_verified_embedding_cache(_config(tmp_path))
    adapter = create_mem0_adapter(
        _config(tmp_path),
        environ={"DEEPSEEK_API_KEY": "fixture-secret"},
        memory_factory=factory,
    )
    assert isinstance(adapter, Mem0ConversationMemoryAdapter)
    assert captured["vector_store"]["provider"] == "qdrant"

    def failing_factory(_config):
        raise RuntimeError("private path and secret")

    unavailable = create_mem0_adapter(
        _config(tmp_path),
        memory_factory=failing_factory,
    )
    assert isinstance(unavailable, UnavailableConversationMemoryPort)
    assert unavailable.reason_code == "MEM0_INITIALIZATION_FAILED"
    assert unavailable.config.data_root == _config(tmp_path).data_root


def test_factory_refuses_an_unverified_local_embedding_cache(tmp_path: Path) -> None:
    factory_called = False

    def factory(_config):
        nonlocal factory_called
        factory_called = True
        return FakeMem0()

    port = create_mem0_adapter(_config(tmp_path), memory_factory=factory)

    assert isinstance(port, UnavailableConversationMemoryPort)
    assert port.reason_code == "MEM0_EMBEDDING_CACHE_UNAVAILABLE"
    assert factory_called is False
    assert "model" not in port.status().to_dict()
    assert "cache" not in port.status().to_dict()


def test_factory_forces_telemetry_off_before_first_mem0_import(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _write_verified_embedding_cache(config)
    observed: dict[str, str | None] = {}

    class FakeMemory:
        @staticmethod
        def from_config(_config):
            observed["from_config"] = os.environ.get("MEM0_TELEMETRY")
            return FakeMem0()

    def import_mem0(name: str):
        assert name == "mem0"
        observed["import"] = os.environ.get("MEM0_TELEMETRY")
        module = SimpleNamespace(Memory=FakeMemory)
        sys.modules[name] = module
        return module

    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    monkeypatch.setenv("MEM0_TELEMETRY", "true")
    monkeypatch.setattr(mem0_memory.importlib, "import_module", import_mem0)

    port = create_mem0_adapter(
        config,
        environ={"MEM0_TELEMETRY": "true"},
    )

    assert isinstance(port, Mem0ConversationMemoryAdapter)
    assert observed == {"import": "False", "from_config": "False"}
    assert os.environ["MEM0_TELEMETRY"] == "False"


def test_factory_reuses_a_product_verified_mem0_import(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _write_verified_embedding_cache(config)
    observed: dict[str, object] = {"imports": 0, "from_config": []}

    class FakeMemory:
        @staticmethod
        def from_config(_config):
            observed["from_config"].append(os.environ.get("MEM0_TELEMETRY"))
            return FakeMem0()

    module = SimpleNamespace(Memory=FakeMemory)

    def import_mem0(name: str):
        assert name == "mem0"
        observed["imports"] += 1
        sys.modules[name] = module
        return module

    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    monkeypatch.setattr(mem0_memory, "_SAFE_MEM0_MODULE", None, raising=False)
    monkeypatch.setattr(mem0_memory.importlib, "import_module", import_mem0)

    first = create_mem0_adapter(config)
    second = create_mem0_adapter(config)

    assert isinstance(first, Mem0ConversationMemoryAdapter)
    assert isinstance(second, Mem0ConversationMemoryAdapter)
    assert observed == {"imports": 1, "from_config": ["False", "False"]}


def test_concurrent_factories_reuse_one_product_verified_mem0_import(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _write_verified_embedding_cache(config)
    import_entered = threading.Event()
    release_import = threading.Event()
    second_started = threading.Event()
    first_finished = threading.Event()
    second_finished = threading.Event()
    observed: dict[str, object] = {"imports": 0, "from_config": []}

    class FakeMemory:
        @staticmethod
        def from_config(_config):
            observed["from_config"].append(os.environ.get("MEM0_TELEMETRY"))
            return FakeMem0()

    module = SimpleNamespace(Memory=FakeMemory)

    def import_mem0(name: str):
        assert name == "mem0"
        observed["imports"] += 1
        sys.modules[name] = module
        import_entered.set()
        assert release_import.wait(1)
        return module

    results: dict[str, object] = {}

    def construct(name: str, finished: threading.Event) -> None:
        if name == "second":
            second_started.set()
        results[name] = create_mem0_adapter(config)
        finished.set()

    monkeypatch.delitem(sys.modules, "mem0", raising=False)
    monkeypatch.setattr(mem0_memory, "_SAFE_MEM0_MODULE", None, raising=False)
    monkeypatch.setattr(mem0_memory.importlib, "import_module", import_mem0)
    first = threading.Thread(target=construct, args=("first", first_finished))
    second = threading.Thread(target=construct, args=("second", second_finished))
    first.start()
    assert import_entered.wait(1)
    second.start()
    assert second_started.wait(1)
    assert second_finished.is_set() is False
    release_import.set()
    assert first_finished.wait(1)
    assert second_finished.wait(1)
    first.join()
    second.join()

    assert isinstance(results["first"], Mem0ConversationMemoryAdapter)
    assert isinstance(results["second"], Mem0ConversationMemoryAdapter)
    assert observed == {"imports": 1, "from_config": ["False", "False"]}


def test_factory_fails_closed_when_mem0_was_preloaded(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _write_verified_embedding_cache(config)
    factory_called = False

    def factory(_config):
        nonlocal factory_called
        factory_called = True
        return FakeMem0()

    monkeypatch.setenv("MEM0_TELEMETRY", "true")
    monkeypatch.setitem(sys.modules, "mem0", SimpleNamespace())

    port = create_mem0_adapter(config, memory_factory=factory)

    assert isinstance(port, UnavailableConversationMemoryPort)
    assert port.reason_code == "MEM0_TELEMETRY_STATE_UNAVAILABLE"
    assert factory_called is False
    assert os.environ["MEM0_TELEMETRY"] == "False"
    assert "path" not in repr(port.status().to_dict())


def test_factory_refuses_corrupt_or_revision_mismatched_embedding_caches(
    tmp_path: Path,
) -> None:
    corrupt = _config(tmp_path / "corrupt")
    _write_verified_embedding_cache(corrupt)
    (corrupt.embedding_snapshot / "model.safetensors").write_bytes(b"corrupt")

    corrupt_port = create_mem0_adapter(corrupt, memory_factory=lambda _: FakeMem0())

    assert isinstance(corrupt_port, UnavailableConversationMemoryPort)
    assert corrupt_port.reason_code == "MEM0_EMBEDDING_CACHE_UNAVAILABLE"

    mismatched_revision = _config(tmp_path / "mismatched-revision")
    _write_verified_embedding_cache(mismatched_revision)
    manifest_path = mismatched_revision.model_cache / "olivia-mem0-embedding-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["revision"] = "0" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    mismatched_port = create_mem0_adapter(
        mismatched_revision, memory_factory=lambda _: FakeMem0()
    )

    assert isinstance(mismatched_port, UnavailableConversationMemoryPort)
    assert mismatched_port.reason_code == "MEM0_EMBEDDING_CACHE_UNAVAILABLE"


def test_manual_failure_uses_stable_error(tmp_path: Path) -> None:
    backend = FakeMem0()
    backend.fail.add("add")
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    try:
        adapter.add_manual_memory(
            "private manual fact",
            user_id="local-user",
            source_id="manual:private:1",
        )
    except Mem0AdapterError as exc:
        assert exc.code == "MEM0_MANUAL_WRITE_FAILED"
        assert "private manual fact" not in str(exc)
    else:
        raise AssertionError("manual provider failure must be explicit")


def test_delete_memory_fails_closed_when_provider_acknowledgement_is_malformed(
    tmp_path: Path,
) -> None:
    class MalformedDeleteMem0(FakeMem0):
        def delete(self, memory_id):
            self._raise("delete")
            self.calls.append(("delete", memory_id))
            return {}

    backend = MalformedDeleteMem0()
    backend.rows.append(
        {
            "id": "memory.delete-malformed",
            "memory": "synthetic manual fact",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "manual:delete-malformed:1",
                "domain": "conversation_memory",
            },
        }
    )
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    assert adapter.delete_memory("memory.delete-malformed", user_id="local-user") is False
    assert adapter._last_error_code == "MEM0_DELETE_FAILED"
    assert [row["id"] for row in backend.rows] == ["memory.delete-malformed"]


def test_clear_user_fails_closed_when_provider_acknowledgement_is_malformed(
    tmp_path: Path,
) -> None:
    class MalformedClearMem0(FakeMem0):
        def delete(self, memory_id):
            self._raise("delete")
            self.calls.append(("delete", memory_id))
            return None

    backend = MalformedClearMem0()
    backend.rows.append(
        {
            "id": "memory.clear-malformed",
            "memory": "synthetic manual fact",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "manual:clear-malformed:1",
                "domain": "conversation_memory",
            },
        }
    )
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    assert adapter.clear_user(user_id="local-user") == 0
    assert adapter._last_error_code == "MEM0_CLEAR_FAILED"
    assert [row["id"] for row in backend.rows] == ["memory.clear-malformed"]


def test_delete_and_clear_accept_pinned_success_acknowledgements(tmp_path: Path) -> None:
    backend = FakeMem0()
    backend.rows.extend(
        [
            {
                "id": "memory.delete-valid",
                "memory": "synthetic manual fact",
                "user_id": "local-user",
                "agent_id": "linli",
                "metadata": {
                    "source_id": "manual:delete-valid:1",
                    "domain": "conversation_memory",
                },
            },
            {
                "id": "memory.clear-valid",
                "memory": "synthetic manual fact",
                "user_id": "local-user",
                "agent_id": "linli",
                "metadata": {
                    "source_id": "manual:clear-valid:1",
                    "domain": "conversation_memory",
                },
            },
        ]
    )
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    assert adapter.delete_memory("memory.delete-valid", user_id="local-user") is True
    assert adapter.clear_user(user_id="local-user") == 1
    assert adapter._last_error_code is None
    assert backend.rows == []


def test_clear_user_deletes_only_conversation_memory_ids_and_verifies_empty(
    tmp_path: Path,
) -> None:
    backend = FakeMem0()
    backend.rows.extend(
        [
            {
                "id": "memory.clear-domain",
                "memory": "synthetic conversation fact",
                "user_id": "local-user",
                "agent_id": "linli",
                "metadata": {
                    "source_id": "manual:clear-domain:1",
                    "domain": "conversation_memory",
                },
            },
            {
                "id": "memory.other-domain",
                "memory": "synthetic other-domain fact",
                "user_id": "local-user",
                "agent_id": "linli",
                "metadata": {
                    "source_id": "other:domain:1",
                    "domain": "other_domain",
                },
            },
        ]
    )
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))

    assert adapter.clear_user(user_id="local-user") == 1
    assert [row["id"] for row in backend.rows] == ["memory.other-domain"]
    assert [value for name, value in backend.calls if name == "delete"] == [
        "memory.clear-domain"
    ]


def test_manual_add_timeout_is_bounded_and_retry_does_not_accumulate_workers(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    errors: list[str] = []
    existing_threads = set(threading.enumerate())

    class BlockingManualAddMem0(FakeMem0):
        def add(self, messages, **kwargs):
            if isinstance(messages, str):
                entered.set()
                release.wait()
            return super().add(messages, **kwargs)

    adapter = Mem0ConversationMemoryAdapter(
        BlockingManualAddMem0(),
        replace(_config(tmp_path), write_timeout_seconds=0.1),
    )

    def add_manual() -> None:
        try:
            adapter.add_manual_memory(
                "synthetic manual fact",
                user_id="local-user",
                source_id="manual:timeout:1",
            )
        except Mem0AdapterError as exc:
            errors.append(exc.code)
        finally:
            done.set()

    caller = threading.Thread(target=add_manual, daemon=True)
    caller.start()
    try:
        assert entered.wait(0.2)
        assert done.wait(0.25)
        assert errors == ["MEM0_MANUAL_WRITE_TIMEOUT"]

        start = time.monotonic()
        add_manual()
        assert time.monotonic() - start < 0.2
        assert errors == ["MEM0_MANUAL_WRITE_TIMEOUT", "MEM0_MANUAL_WRITE_TIMEOUT"]
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "olivia-mem0-write" and thread not in existing_threads
        ]
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()
        caller.join(timeout=0.5)
        for thread in threading.enumerate():
            if thread.name == "olivia-mem0-write" and thread not in existing_threads:
                thread.join(timeout=0.5)


def test_manual_delete_timeout_is_bounded_and_retry_does_not_accumulate_workers(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    results: list[bool] = []
    existing_threads = set(threading.enumerate())

    class BlockingManualDeleteMem0(FakeMem0):
        def delete(self, memory_id):
            entered.set()
            release.wait()
            return super().delete(memory_id)

    backend = BlockingManualDeleteMem0()
    backend.rows.append(
        {
            "id": "memory.manual-timeout",
            "memory": "synthetic manual fact",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "manual:timeout:delete",
                "domain": "conversation_memory",
            },
        }
    )
    adapter = Mem0ConversationMemoryAdapter(
        backend,
        replace(_config(tmp_path), write_timeout_seconds=0.1),
    )

    def delete_manual() -> None:
        results.append(adapter.delete_memory("memory.manual-timeout", user_id="local-user"))
        done.set()

    caller = threading.Thread(target=delete_manual, daemon=True)
    caller.start()
    try:
        assert entered.wait(0.2)
        assert done.wait(0.25)
        assert results == [False]

        start = time.monotonic()
        delete_manual()
        assert time.monotonic() - start < 0.2
        assert results == [False, False]
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "olivia-mem0-write" and thread not in existing_threads
        ]
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()
        caller.join(timeout=0.5)
        for thread in threading.enumerate():
            if thread.name == "olivia-mem0-write" and thread not in existing_threads:
                thread.join(timeout=0.5)


def test_manual_clear_timeout_is_bounded_and_retry_does_not_accumulate_workers(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    results: list[int] = []
    existing_threads = set(threading.enumerate())

    class BlockingManualClearMem0(FakeMem0):
        def delete(self, memory_id):
            entered.set()
            release.wait()
            return super().delete(memory_id)

    backend = BlockingManualClearMem0()
    backend.rows.append(
        {
            "id": "memory.manual-clear-timeout",
            "memory": "synthetic manual fact",
            "user_id": "local-user",
            "agent_id": "linli",
            "metadata": {
                "source_id": "manual:timeout:clear",
                "domain": "conversation_memory",
            },
        }
    )
    adapter = Mem0ConversationMemoryAdapter(
        backend,
        replace(_config(tmp_path), write_timeout_seconds=0.1),
    )

    def clear_manual() -> None:
        results.append(adapter.clear_user(user_id="local-user"))
        done.set()

    caller = threading.Thread(target=clear_manual, daemon=True)
    caller.start()
    try:
        assert entered.wait(0.2)
        assert done.wait(0.25)
        assert results == [0]

        start = time.monotonic()
        clear_manual()
        assert time.monotonic() - start < 0.2
        assert results == [0, 0]
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "olivia-mem0-write" and thread not in existing_threads
        ]
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()
        caller.join(timeout=0.5)
        for thread in threading.enumerate():
            if thread.name == "olivia-mem0-write" and thread not in existing_threads:
                thread.join(timeout=0.5)
