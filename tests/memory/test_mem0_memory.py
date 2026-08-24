from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from conversation_memory_outbox import CanonicalMemoryOutbox
from conversation_memory_port import (
    MemoryWriteStatus,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)
from conversation_memory_runtime import stop_conversation_memory_runtime
from memory_port import NullMemoryPort
from memory_prompt import MemoryPromptBuilder
from mem0_memory import (
    MEM0_OSS_VERSION,
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

    @staticmethod
    def _assert_filters(filters: object) -> None:
        assert filters == {
            "user_id": "local-user",
            "agent_id": "linli",
            "domain": "conversation_memory",
        }

    def add(self, messages, **kwargs):
        self._raise("add")
        self.calls.append(("add", {"messages": messages, **kwargs}))
        self.counter += 1
        metadata = dict(kwargs.get("metadata", {}))
        text = messages if isinstance(messages, str) else "用户在东京工作。"
        row = {
            "id": f"memory.fixture.{self.counter}",
            "memory": text,
            "user_id": kwargs.get("user_id"),
            "agent_id": kwargs.get("agent_id"),
            "metadata": metadata,
            "created_at": NOW.isoformat(),
        }
        self.rows.append(row)
        return [row]

    def search(self, query, **kwargs):
        self._raise("search")
        self._assert_filters(kwargs.get("filters"))
        self.calls.append(("search", {"query": query, **kwargs}))
        return {"results": [{**row, "score": 0.91} for row in self.rows]}

    def get_all(self, **kwargs):
        self._raise("get_all")
        self._assert_filters(kwargs.get("filters"))
        assert 1 <= kwargs.get("top_k", 0) <= 1000
        self.calls.append(("get_all", kwargs))
        return {"results": list(self.rows)[: kwargs["top_k"]]}

    def delete(self, memory_id):
        self._raise("delete")
        self.calls.append(("delete", memory_id))
        self.rows[:] = [row for row in self.rows if row["id"] != memory_id]

    def delete_all(self, user_id=None, agent_id=None, run_id=None):
        self._raise("delete_all")
        assert user_id == "local-user"
        assert agent_id == "linli"
        assert run_id is None
        self.calls.append(("delete_all", {"user_id": user_id, "agent_id": agent_id}))
        self.rows.clear()


def _config(tmp_path: Path) -> Mem0Config:
    return Mem0Config(
        enabled=True,
        data_root=tmp_path / "memory" / "mem0",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="fixture-model",
        embedding_cache=tmp_path / "models",
    )


def test_version_and_config_match_current_mem0_oss_contract(tmp_path: Path) -> None:
    assert MEM0_OSS_VERSION == "2.0.18"
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
    assert mapping["history_db_path"] == str(config.history_path)
    assert mapping["embedder"]["provider"] == "huggingface"
    assert mapping["embedder"]["config"]["model_kwargs"] == {
        "device": "cpu",
        "cache_folder": str(config.model_cache),
        "local_files_only": True,
    }
    assert mapping["llm"]["config"]["openai_base_url"] == "http://127.0.0.1:9/v1"
    assert mapping["llm"]["config"]["api_key"] == "fixture-secret"
    assert "private_world" not in repr(mapping)


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


def test_explicit_empty_environment_never_inherits_host_mem0_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OLIVIA_MEMORY_ENABLED", "true")
    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(tmp_path / "host-mem0"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "host-secret")

    config = load_mem0_config(environ={}, project_root=tmp_path)

    assert config.enabled is False
    assert config.data_root == tmp_path / ".olivia_data" / "memory" / "mem0"
    assert _config(tmp_path).provider_config({})["llm"]["config"]["api_key"] == ""


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
    assert any(name == "delete_all" for name, _value in backend.calls)


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
    assert status["status"] == "degraded"
    assert status["reason_code"] == "MEM0_LIST_FAILED"


def test_exchange_never_writes_when_source_deduplication_times_out(tmp_path: Path) -> None:
    for delayed in (False, True):
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
                "metadata": {"source_id": "reply:timeout:1", "domain": "conversation_memory"},
            }
        )
        config = _config(tmp_path)
        config = Mem0Config(**{**config.__dict__, "search_timeout_seconds": 0.1})
        adapter = Mem0ConversationMemoryAdapter(backend, config)
        timer = threading.Timer(0.2, release.set) if delayed else None
        if timer is not None:
            timer.start()
        deferred = adapter.remember_exchange(
            user_message="synthetic user",
            assistant_message="synthetic reply",
            occurred_at=NOW,
            source_id="reply:timeout:1",
            user_id="local-user",
        )
        assert deferred.status is MemoryWriteStatus.UNAVAILABLE
        assert deferred.error_code == "MEM0_SOURCE_DEDUP_UNAVAILABLE"
        assert not [call for call in backend.calls if call[0] == "add"]
        if delayed:
            release.set()
            if timer is not None:
                timer.cancel()
            time.sleep(0.02)
            retry = adapter.remember_exchange(
                user_message="synthetic user",
                assistant_message="synthetic reply",
                occurred_at=NOW,
                source_id="reply:timeout:1",
                user_id="local-user",
            )
            assert retry.status is MemoryWriteStatus.DUPLICATE
        else:
            retry = adapter.remember_exchange(
                user_message="synthetic user",
                assistant_message="synthetic reply",
                occurred_at=NOW,
                source_id="reply:timeout:1",
                user_id="local-user",
            )
            assert retry.status is MemoryWriteStatus.UNAVAILABLE
        assert not [call for call in backend.calls if call[0] == "add"]
        release.set()
        time.sleep(0.02)


def test_outbox_retry_after_a_timed_out_source_check_never_adds_duplicate(tmp_path: Path) -> None:
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
            "metadata": {"source_id": "reply:letter-1:1", "domain": "conversation_memory"},
        }
    )
    config = Mem0Config(
        **{**_config(tmp_path).__dict__, "search_timeout_seconds": 0.1}
    )
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
    time.sleep(0.02)
    assert asyncio.run(outbox.scan_once()).duplicates == 1
    assert not [call for call in backend.calls if call[0] == "add"]


def test_search_timeout_degrades_without_blocking_or_leaking_worker_threads(
    tmp_path: Path,
) -> None:
    release_status = threading.Event()
    release_search = threading.Event()
    status_entered = threading.Event()
    status_done = threading.Event()
    class BlockingMem0(FakeMem0):
        def get_all(self, **kwargs):
            del kwargs
            status_entered.set()
            release_status.wait()
            return {"results": []}

        def search(self, query, **kwargs):
            del query, kwargs
            release_search.wait()
            return {"results": []}
    adapter = Mem0ConversationMemoryAdapter(
        BlockingMem0(),
        Mem0Config(
            enabled=True,
            data_root=tmp_path / "memory" / "mem0",
            llm_base_url="http://127.0.0.1:9/v1",
            llm_model="fixture-model",
            search_timeout_seconds=0.1,
        ),
    )
    status: dict[str, object] = {}
    def read_status() -> None:
        status["value"] = adapter.status()
        status_done.set()

    status_worker = threading.Thread(target=read_status, daemon=True)
    status_worker.start()
    assert status_entered.wait(0.2)
    assert status_done.wait(0.5)
    assert status["value"].reason_code == "MEM0_SEARCH_TIMEOUT"  # type: ignore[union-attr]
    assert adapter.search_context("synthetic query", user_id="local-user", limit=3) == ()
    provider_workers = [
        thread for thread in threading.enumerate() if thread.name == "mem0-provider"
    ]
    assert len(provider_workers) == 1
    assert provider_workers[0].daemon is True
    release_status.set()
    status_worker.join(timeout=0.5)
    started = time.monotonic()
    assert adapter.search_context("synthetic query", user_id="local-user", limit=3) == ()
    assert time.monotonic() - started < 0.5
    # A second call must not create another unbounded worker while the first
    # provider operation is still stuck.
    assert adapter.search_context("synthetic query", user_id="local-user", limit=3) == ()
    completed = threading.Event()
    def build_prompt() -> None:
        MemoryPromptBuilder(
            NullMemoryPort(),
            conversation_memory=adapter,
        ).build("synthetic query", max_chars=200)
        completed.set()

    worker = threading.Thread(target=build_prompt, daemon=True)
    worker.start()
    try:
        assert completed.wait(0.5)
    finally:
        release_search.set()
        worker.join(timeout=0.5)
        stop_conversation_memory_runtime()


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
    unavailable_from_config = create_mem0_adapter(broken)
    assert isinstance(unavailable_from_config, UnavailableConversationMemoryPort)
    assert unavailable_from_config.config is broken

    captured: dict[str, object] = {}
    backend = FakeMem0()

    def factory(config):
        captured.update(config)
        return backend

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
