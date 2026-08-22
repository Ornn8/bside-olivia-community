from __future__ import annotations

import asyncio

from llm_gateway import Gateway, GatewayDelta, GatewayResponse
from local_memory import LocalMemoryAdapter, UnavailableMemoryPort
from memory_port import CONVERSATION_MEMORY, LEGACY_LETTERS, LegacyLetter, NullMemoryPort
from memory_prompt import MEMORY_CONTEXT_BEGIN, MEMORY_CONTEXT_END, MemoryPromptBuilder


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
                {"content": "new synthetic source", "idempotency_key": "b04-integration-1"},
                {},
            )
        )
        assert result["code"] == 0
        user_message = gateway.messages[-1]["content"]
        assert user_message.count(MEMORY_CONTEXT_BEGIN) == 1
        assert user_message.count(MEMORY_CONTEXT_END) == 1
        assert "LEGACY_LETTERS_REFERENCE_ONLY" in user_message
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
    assert memory["data"]["capabilities"]["memory.local"]["status"] == "unavailable"


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
