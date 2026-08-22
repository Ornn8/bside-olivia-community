from __future__ import annotations

import asyncio

from llm_gateway import GatewayConfig, OfflineDeterministicAdapter, UnconfiguredAdapter


def test_unconfigured_send_is_explicit_and_never_writes_reply(monkeypatch) -> None:
    import local_server

    local_server.store.letters.clear()
    monkeypatch.setattr(local_server.letters_adapter, "gateway", UnconfiguredAdapter())
    result = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic unavailable input"},
            {},
        )
    )
    assert result["code"] == 503
    assert result["data"]["error_code"] == "LLM_UNAVAILABLE"
    assert result["data"]["retryable"] is True
    assert local_server.store.letters[0]["reply_text"] == ""


def test_mock_provider_reaches_b02_send_and_keeps_legacy_out_of_prompt(monkeypatch) -> None:
    import local_server

    local_server.store.letters.clear()
    local_server.store.legacy_letters[:] = [
        {
            "letter_id": "legacy-only",
            "content": "legacy private source text",
            "reply_text": "legacy reply",
            "is_read": 0,
        }
    ]
    adapter = OfflineDeterministicAdapter(
        GatewayConfig(provider="mock", model="offline", max_input_chars=10000)
    )
    monkeypatch.setattr(local_server.letters_adapter, "gateway", adapter)
    result = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "new current letter", "idempotency_key": "send-1"},
            {},
        )
    )
    letter_id = result["data"]["letter_id"]
    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"letter_id": letter_id},
        )
    )
    assert result["code"] == 0
    assert "legacy private source text" not in detail["data"]["reply_text"]
    assert detail["data"]["reply_text"]


def test_send_idempotency_and_conflict_do_not_duplicate_current_letters(monkeypatch) -> None:
    import local_server

    local_server.store.letters.clear()
    local_server.store.request_keys.clear()
    monkeypatch.setattr(
        local_server.letters_adapter,
        "gateway",
        OfflineDeterministicAdapter(GatewayConfig(provider="mock", model="offline")),
    )
    first = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "same", "idempotency_key": "repeat-1"},
            {},
        )
    )
    second = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "same", "idempotency_key": "repeat-1"},
            {},
        )
    )
    conflict = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "different", "idempotency_key": "repeat-1"},
            {},
        )
    )
    assert first["data"]["letter_id"] == second["data"]["letter_id"]
    assert conflict["code"] == 409
    assert len(local_server.store.letters) == 1


def test_health_exposes_llm_profile_and_draft_persona_without_network_probe() -> None:
    import local_server

    health = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "llm"}))
    data = health["data"]
    assert health["code"] == 0
    assert data["status"] in {"HEALTHY", "DEGRADED", "UNAVAILABLE"}
    assert data["providers"]["llm_gateway"]["network_called"] is False
    assert data["providers"]["llm_gateway"]["persona"]["status"] == "DRAFT"
    assert "llm.gateway" in data["capabilities"]
