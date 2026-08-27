from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

from conversation_memory_port import (
    ConversationMemoryRecord,
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
    UnavailableConversationMemoryPort,
)
from llm_gateway import Gateway, GatewayDelta, GatewayResponse
from local_memory import LocalMemoryAdapter, UnavailableMemoryPort
from memory_port import CONVERSATION_MEMORY, LEGACY_LETTERS, LegacyLetter, NullMemoryPort
from memory_prompt import MEMORY_CONTEXT_BEGIN, MEMORY_CONTEXT_END, MemoryPromptBuilder
from runtime.memory.private_world_delivery import DeliveryStatus
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult
from letter_triage import TriageResult


class CaptureGateway(Gateway):
    def __init__(self, text: str = "synthetic captured reply") -> None:
        self.messages = []
        self.text = text
        self.stream_enabled = False

    async def complete(self, messages, *, request_id=None):
        self.messages = list(messages)
        return GatewayResponse(self.text, request_id or "synthetic-request", "mock", "synthetic")

    async def stream(self, messages, *, request_id=None):
        self.messages = list(messages)
        yield GatewayDelta(self.text, request_id or "synthetic-request", finish_reason="stop")


class CountingConversationMemory:
    enabled = True

    def __init__(self, data_root) -> None:
        self.config = SimpleNamespace(user_id="local-user", data_root=data_root)
        self.calls: list[dict[str, object]] = []

    def status(self) -> ConversationMemoryStatus:
        return ConversationMemoryStatus("available", True, "mem0", "qdrant-local")

    def search_context(self, query, *, user_id, limit):
        del query, user_id, limit
        return ()

    def remember_exchange(self, **kwargs) -> MemoryWriteResult:
        self.calls.append(dict(kwargs))
        return MemoryWriteResult(
            MemoryWriteStatus.WRITTEN,
            str(kwargs["source_id"]),
            ("memory.fixture.1",),
        )

    def list_memories(self, *, user_id, limit=100):
        del user_id, limit
        return ()

    def add_manual_memory(self, text, *, user_id, source_id):
        del text, user_id, source_id
        raise AssertionError("manual memory is outside canonical delivery")

    def delete_memory(self, memory_id, *, user_id):
        del memory_id, user_id
        return False

    def clear_user(self, *, user_id):
        del user_id
        return 0

    def export_user(self, *, user_id):
        del user_id
        return {"records": []}


class SourceAwareConversationMemory(CountingConversationMemory):
    """Synthetic Mem0 rows whose text is intentionally identical."""

    def search_context(self, query, *, user_id, limit):
        self.calls.append(
            {"query": query, "user_id": user_id, "limit": limit}
        )
        return (
            ConversationMemoryRecord(
                memory_id="memory.current",
                text="同文的合成记忆。",
                user_id=user_id,
                source_id="reply:current-letter:1",
            ),
            ConversationMemoryRecord(
                memory_id="memory.older",
                text="同文的合成记忆。",
                user_id=user_id,
                source_id="reply:older-letter:1",
            ),
        )[:limit]


class CountingPrivateWorldCommitter:
    def __init__(self) -> None:
        self.deliveries = []

    def commit(self, delivery):
        self.deliveries.append(delivery)
        return DeliveryStatus.COMMITTED


def test_send_cites_legacy_without_mixing_it_into_current_memory(tmp_path, monkeypatch) -> None:
    import local_server

    adapter = LocalMemoryAdapter(tmp_path / "memory.sqlite3")
    try:
        adapter.import_legacy_records([LegacyLetter("legacy synthetic source", "fixture-legacy", "fixture")])
        gateway = CaptureGateway()
        local_server.store.letters.clear()
        local_server.store.request_keys.clear()
        monkeypatch.setattr(local_server.letters_adapter, "memory_port", adapter)
        monkeypatch.setattr(local_server.letters_adapter, "memory_prompt_builder", MemoryPromptBuilder(adapter))
        monkeypatch.setattr(local_server.letters_adapter, "gateway", gateway)
        result = asyncio.run(
            local_server.route(
                "POST",
                "/toy/letter/send",
                {"content": "synthetic source", "idempotency_key": "b04-integration-1"},
                {},
            )
        )
        assert result["code"] == 0
        rendered_messages = "\n".join(message["content"] for message in gateway.messages)
        assert gateway.messages[-1]["content"] == "synthetic source"
        assert rendered_messages.count("<untrusted_history>") == 1
        assert r"\u003cMEMORY_CONTEXT_UNTRUSTED_DATA\u003e" in rendered_messages
        assert r"\u003c/MEMORY_CONTEXT_UNTRUSTED_DATA\u003e" in rendered_messages
        assert MEMORY_CONTEXT_BEGIN not in rendered_messages
        assert MEMORY_CONTEXT_END not in rendered_messages
        assert "LEGACY_LETTERS_REFERENCE_ONLY" in rendered_messages
        exported = adapter.export_records(domains=(LEGACY_LETTERS, CONVERSATION_MEMORY))
        assert len(exported[LEGACY_LETTERS]) == 1
        assert all("legacy synthetic source" not in item["content"] for item in exported[CONVERSATION_MEMORY])
    finally:
        adapter.close()


def test_unavailable_memory_falls_back_and_health_is_truthful(monkeypatch) -> None:
    import local_server

    gateway = CaptureGateway()
    local_server.store.letters.clear()
    local_server.store.request_keys.clear()
    unavailable = UnavailableMemoryPort()
    monkeypatch.setattr(local_server.letters_adapter, "memory_port", unavailable)
    monkeypatch.setattr(local_server.letters_adapter, "memory_prompt_builder", MemoryPromptBuilder(unavailable))
    monkeypatch.setattr(local_server.letters_adapter, "gateway", gateway)
    result = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic plain text fallback", "idempotency_key": "b04-integration-2"},
            {},
        )
    )
    assert result["code"] == 0
    assert MEMORY_CONTEXT_BEGIN not in gateway.messages[-1]["content"]
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    core = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "core"}))
    memory = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "memory"}))
    assert core["data"]["status"] == "HEALTHY"
    assert memory["data"]["status"] == "UNAVAILABLE"
    assert memory["data"]["capabilities"]["memory.local"]["status"] == "disabled"


def test_current_letter_memory_excludes_only_its_exact_source_identity(
    tmp_path,
    monkeypatch,
) -> None:
    """Production generation must not retrieve its own canonical exchange."""

    import local_server

    memory = SourceAwareConversationMemory(tmp_path / "mem0")
    gateway = CaptureGateway()
    letter = {
        "letter_id": "current-letter",
        "content": "请记住这封合成信。",
        "reply_text": "",
        "reply_mode": ReplyMode.TEXT_LETTER.value,
        "letter_status": "PENDING",
    }
    local_server.store.letters[:] = [letter]
    local_server.store.request_keys.clear()
    monkeypatch.setattr(local_server.letters_adapter, "conversation_memory", memory)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "memory_prompt_builder",
        MemoryPromptBuilder(NullMemoryPort(), conversation_memory=memory),
    )
    monkeypatch.setattr(local_server.letters_adapter, "gateway", gateway)

    assert asyncio.run(local_server.generate_reply("current-letter", letter["content"]))

    rendered = "\n".join(message["content"] for message in gateway.messages)
    assert "reply:current-letter:1" not in rendered
    assert "reply:older-letter:1" in rendered
    assert "同文的合成记忆。" in rendered


def test_memory_prompt_legacy_selector_remains_compatible() -> None:
    builder = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None)

    prompt = builder.build("legacy compatible query", max_chars=2400)

    assert prompt.status == "disabled"


def test_mem0_unavailable_keeps_production_letter_generation_available(
    monkeypatch,
) -> None:
    import local_server

    unavailable = UnavailableConversationMemoryPort("MEM0_IMPORT_FAILED")
    gateway = CaptureGateway()
    letter = {
        "letter_id": "mem0-unavailable-letter",
        "content": "Mem0 不可用时仍应生成这封合成信。",
        "reply_text": "",
        "reply_mode": ReplyMode.TEXT_LETTER.value,
        "letter_status": "PENDING",
    }
    local_server.store.letters[:] = [letter]
    local_server.store.request_keys.clear()
    monkeypatch.setattr(local_server.letters_adapter, "conversation_memory", unavailable)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "memory_prompt_builder",
        MemoryPromptBuilder(NullMemoryPort(), conversation_memory=unavailable),
    )
    monkeypatch.setattr(local_server.letters_adapter, "gateway", gateway)

    assert asyncio.run(
        local_server.generate_reply(letter["letter_id"], letter["content"])
    )
    assert letter["letter_status"] == "COMPLETED"
    assert gateway.messages[-1]["content"] == letter["content"]


def test_legacy_import_activates_read_only_library_without_enabling_chat_memory(tmp_path, monkeypatch) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server.letters_adapter, "memory_port", NullMemoryPort())
    monkeypatch.setattr(
        local_server.letters_adapter,
        "memory_prompt_builder",
        MemoryPromptBuilder(NullMemoryPort()),
    )
    send = asyncio.run(
        local_server.route("POST", "/toy/letter/send", {"content": "new"}, {"scope": "legacy"})
    )
    import_result = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/legacy/import",
            {
                "mode": "read_only",
                "letters": [
                    {
                        "source_record_id": "synthetic-legacy-1",
                        "source": "synthetic-test",
                        "content": "immutable synthetic legacy body",
                        "occurred_at": "2026-08-14T00:00:00Z",
                        "metadata": {"kind": "synthetic"},
                    }
                ],
            },
            {},
        )
    )
    health = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "memory"}))
    listed = asyncio.run(local_server.route("GET", "/toy/letter/list", {}, {"scope": "legacy"}))
    assert send["code"] == 403
    assert send["data"]["error_code"] == "READ_ONLY_SCOPE"
    assert import_result["code"] == 0
    assert import_result["data"]["inserted"] == 1
    assert import_result["data"]["read_only"] is True
    assert health["data"]["status"] == "HEALTHY"
    assert health["data"]["providers"]["memory"]["conversation_enabled"] is False
    assert listed["data"]["total"] == 1
    assert listed["data"]["list"][0]["letter_id"]
    local_server.memory_adapter.close()


def test_canonical_letter_commits_mem0_and_private_world_once_across_recovery_and_media_retry(
    tmp_path,
    monkeypatch,
) -> None:
    import local_server
    from conversation_memory_runtime import stop_conversation_memory_runtime

    stop_conversation_memory_runtime()
    root = tmp_path / "data"
    memory = CountingConversationMemory(root / "memory" / "mem0")
    private_world = CountingPrivateWorldCommitter()
    letter = {
        "letter_id": "canonical-once",
        "content": "synthetic current letter",
        "reply_text": "",
        "reply_mode": ReplyMode.TEXT_LETTER.value,
        "letter_status": "PENDING",
    }

    async def classify(_content):
        return TriageResult(
            "normal",
            ReplyMode.TEXT_LETTER.value,
            "direct_words_are_enough",
            "completed",
            True,
        )

    async def run_pipeline(_request, _context):
        return PipelineResult(
            "canonical-once",
            ReplyState.COMPLETED,
            text="synthetic canonical reply",
            quality_status="accepted_degraded",
        )

    async def no_voice_plan(_letter, _reply):
        return None

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(root))
    monkeypatch.setenv("OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS", "0.25")
    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "private_world_committer", private_world)
    monkeypatch.setattr(local_server.letters_adapter, "conversation_memory", memory)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "memory_prompt_builder",
        MemoryPromptBuilder(NullMemoryPort(), conversation_memory=memory),
    )
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)
    monkeypatch.setattr(local_server, "_voice_plan_for_letter", no_voice_plan)
    local_server.store.letters[:] = [letter]
    local_server.store.request_keys.clear()

    try:
        assert asyncio.run(local_server.generate_reply(letter["letter_id"], letter["content"]))
        deadline = time.monotonic() + 2.0
        while len(memory.calls) != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(memory.calls) == 1
        assert memory.calls[0]["source_id"] == "reply:canonical-once:1"
        assert memory.calls[0]["user_message"] == letter["content"]
        assert memory.calls[0]["assistant_message"] == "synthetic canonical reply"
        assert len(private_world.deliveries) == 1

        assert local_server.recover_pending_private_world() == 0
        asyncio.run(
            local_server._render_media_job(
                letter["letter_id"],
                letter["content"],
                letter["reply_text"],
                ReplyMode.SPOKEN_VIDEO.value,
            )
        )
        assert len(memory.calls) == 1
        assert len(private_world.deliveries) == 1
    finally:
        stop_conversation_memory_runtime()

def test_file_only_mem0_configuration_persists_canonical_state_for_the_outbox(
    tmp_path,
    monkeypatch,
) -> None:
    """A configured Mem0 root, not process environment, owns canonical state."""

    import local_server
    from conversation_memory_runtime import stop_conversation_memory_runtime
    from local_memory import create_conversation_memory_adapter, load_memory_config
    import local_memory
    from mem0_memory import create_mem0_adapter, load_mem0_config

    stop_conversation_memory_runtime()
    root = tmp_path / "file-only-data"
    config_path = tmp_path / "memory_config.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": "mem0",
                "data_root": "file-only-data/memory/mem0",
                "llm": {
                    "provider": "openai",
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "synthetic-memory-model",
                    "api_key_env": "SYNTHETIC_MEMORY_KEY",
                },
            }
        ),
        encoding="utf-8",
    )
    memory: CountingConversationMemory | None = None

    def fake_create_mem0_adapter(*, environ):
        nonlocal memory
        mem0_config = load_mem0_config(environ=environ, project_root=tmp_path)
        memory = CountingConversationMemory(mem0_config.data_root)
        memory.config = mem0_config
        return memory

    monkeypatch.setattr(local_memory, "create_mem0_adapter", fake_create_mem0_adapter)
    profile = load_memory_config(config_path, environ={}, root=tmp_path)
    assert profile.config_error is None
    assert profile.provider == "mem0"
    conversation = create_conversation_memory_adapter(profile, environ={})
    assert memory is not None
    letter = {
        "letter_id": "file-only-canonical",
        "content": "synthetic file-only current letter",
        "reply_text": "",
        "reply_mode": ReplyMode.TEXT_LETTER.value,
        "letter_status": "PENDING",
    }

    async def classify(_content):
        return TriageResult(
            "normal",
            ReplyMode.TEXT_LETTER.value,
            "direct_words_are_enough",
            "completed",
            True,
        )

    async def run_pipeline(_request, _context):
        return PipelineResult(
            "file-only-canonical",
            ReplyState.COMPLETED,
            text="synthetic file-only canonical reply",
            quality_status="accepted_degraded",
        )

    monkeypatch.delenv("OLIVIA_LOCAL_DATA_ROOT", raising=False)
    monkeypatch.delenv("OLIVIA_MEMORY_OUTBOX_DATA_ROOT", raising=False)
    monkeypatch.delenv("OLIVIA_MEMORY_OUTBOX_ENABLED", raising=False)
    monkeypatch.delenv("OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS", raising=False)
    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "conversation_memory_adapter", conversation)
    monkeypatch.setattr(local_server.letters_adapter, "conversation_memory", conversation)
    monkeypatch.setattr(
        local_server.letters_adapter,
        "memory_prompt_builder",
        MemoryPromptBuilder(NullMemoryPort(), conversation_memory=conversation),
    )
    local_server.store.letters[:] = [letter]
    local_server.store.request_keys.clear()

    try:
        assert not (root / "state.json").exists()
        assert asyncio.run(local_server.generate_reply(letter["letter_id"], letter["content"]))
        # The file-only profile uses the production default poll interval; it
        # deliberately does not inject an outbox environment override.
        deadline = time.monotonic() + 7.0
        while len(memory.calls) != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (root / "state.json").is_file()
        assert len(memory.calls) == 1
        assert memory.calls[0]["source_id"] == "reply:file-only-canonical:1"
        def fail_mem0(_config):
            raise RuntimeError("synthetic initialization failure")

        unavailable = create_mem0_adapter(memory.config, memory_factory=fail_mem0)
        unavailable_letter = {**letter, "letter_id": "file-only-unavailable", "reply_text": ""}
        monkeypatch.setattr(local_server, "conversation_memory_adapter", unavailable)
        monkeypatch.setattr(local_server.letters_adapter, "conversation_memory", unavailable)
        monkeypatch.setattr(
            local_server.letters_adapter,
            "memory_prompt_builder",
            MemoryPromptBuilder(NullMemoryPort(), conversation_memory=unavailable),
        )
        local_server.store.letters[:] = [unavailable_letter]
        assert asyncio.run(
            local_server.generate_reply(unavailable_letter["letter_id"], unavailable_letter["content"])
        )
        persisted = json.loads((root / "state.json").read_text(encoding="utf-8"))
        assert persisted["letters"][0]["content"] == unavailable_letter["content"]
        assert persisted["letters"][0]["letter_status"] == "COMPLETED"
    finally:
        stop_conversation_memory_runtime()


def test_persona_v2_uses_the_conversation_memory_context_limit(monkeypatch) -> None:
    import local_server

    memory = CountingConversationMemory(Path("synthetic-memory-root") / "mem0")
    memory.config.context_max_chars = 0
    adapter = local_server.LetterAdapter(
        memory_port=NullMemoryPort(),
        conversation_memory=memory,
    )
    captured: dict[str, int] = {}

    class SpyMemoryPromptBuilder:
        def build(self, _query, *, max_chars):
            captured["max_chars"] = max_chars
            return SimpleNamespace(text="")

    monkeypatch.setattr(adapter, "memory_prompt_builder", SpyMemoryPromptBuilder())
    adapter._messages("synthetic current user message")

    assert captured["max_chars"] == 0


def test_legacy_only_import_is_idempotent_and_survives_restart_without_chat_retention(tmp_path, monkeypatch) -> None:
    import local_server

    from local_memory import create_memory_adapter

    root = tmp_path / "memory"
    monkeypatch.setenv("OLIVIA_MEMORY_ROOT", str(root))
    monkeypatch.setattr(local_server, "memory_adapter", NullMemoryPort())
    monkeypatch.setattr(local_server.letters_adapter, "memory_port", NullMemoryPort())
    monkeypatch.setattr(
        local_server.letters_adapter,
        "memory_prompt_builder",
        MemoryPromptBuilder(NullMemoryPort()),
    )
    body = {
        "mode": "read_only",
        "letters": [{"source_record_id": "synthetic-legacy-2", "content": "same synthetic body"}],
    }
    first = asyncio.run(local_server.route("POST", "/toy/letter/legacy/import", body, {}))
    second = asyncio.run(local_server.route("POST", "/toy/letter/legacy/import", body, {}))
    adapter = local_server.memory_adapter
    before_hashes = adapter.legacy_content_hashes()
    local_server.letters_adapter.remember_conversation("new chat", "new reply")
    assert first["data"]["inserted"] == 1
    assert second["data"]["duplicates"] == 1
    assert adapter.status()["counts"][CONVERSATION_MEMORY] == 0
    adapter.close()
    reopened = create_memory_adapter(environ={"OLIVIA_MEMORY_ROOT": str(root)})
    try:
        assert reopened.status()["enabled"] is True
        assert reopened.status()["conversation_enabled"] is False
        assert reopened.legacy_content_hashes() == before_hashes
        assert reopened.status()["counts"][CONVERSATION_MEMORY] == 0
    finally:
        reopened.close()
