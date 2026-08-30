from __future__ import annotations

import asyncio

from llm_gateway import (
    Gateway,
    GatewayConfig,
    GatewayRequestScope,
    GatewayResponse,
    OfflineDeterministicAdapter,
    UnconfiguredAdapter,
)


def test_saved_llm_config_replaces_the_next_reply_gateway_without_restart(
    monkeypatch,
) -> None:
    import local_server

    previous = GatewayConfig(
        provider="openai_compatible",
        base_url="https://old.example/v1",
        model="old-model",
        api_key_env="OLIVIA_LLM_RUNTIME_KEY_OLD",
        requires_api_key=True,
    )
    marker = object()
    monkeypatch.setattr(local_server, "LLM_CONFIG", previous)
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", previous.timeout_seconds)
    monkeypatch.setattr(local_server, "LLM_CFG", previous.public_dict())
    monkeypatch.setattr(local_server.letters_adapter, "config", previous)
    monkeypatch.setattr(local_server.letters_adapter, "gateway", object())
    candidate_analyzer = type(
        "CandidateAnalyzer",
        (),
        {"gateway": object(), "timeout_seconds": 1.0},
    )()
    monkeypatch.setattr(
        local_server,
        "GatewayPrivateWorldCandidateAnalyzer",
        type(candidate_analyzer),
    )
    monkeypatch.setattr(
        local_server,
        "private_world_candidate_analyzer",
        candidate_analyzer,
    )
    monkeypatch.setattr(local_server.reply_engine, "timeout_seconds", previous.timeout_seconds)
    resolvers = []
    monkeypatch.setattr(
        local_server,
        "create_gateway",
        lambda config, *, key_resolver=None: resolvers.append(key_resolver) or marker,
    )
    previous_triage_gateway = object()
    monkeypatch.setattr(local_server.emotion_triage, "gateway", previous_triage_gateway)
    monkeypatch.delenv("OLIVIA_LLM_RUNTIME_KEY_CONFIGURED", raising=False)

    local_server.apply_runtime_llm_config(
        "https://gateway.example/v1",
        "new-model",
        "synthetic-runtime-key",
    )

    assert local_server.letters_adapter.gateway is marker
    assert local_server.emotion_triage.gateway is marker
    assert candidate_analyzer.gateway is marker
    assert candidate_analyzer.timeout_seconds == 180.0
    assert local_server.letters_adapter.config.base_url == "https://gateway.example/v1"
    assert local_server.letters_adapter.config.model == "new-model"
    assert (
        local_server.letters_adapter.config.api_key_env
        == "OLIVIA_LLM_RUNTIME_KEY_CONFIGURED"
    )
    assert local_server._os.environ["OLIVIA_LLM_RUNTIME_KEY_CONFIGURED"] == "1"
    assert resolvers[0]() == "synthetic-runtime-key"
    assert local_server.LLM_CONFIG is local_server.letters_adapter.config
    assert local_server.reply_engine.timeout_seconds == 180.0
    assert local_server.LLM_CFG["model"] == "new-model"
    assert "synthetic-runtime-key" not in repr(local_server.LLM_CFG)


def test_deepseek_flash_runtime_keeps_generic_engine_timeout_for_non_scoped_calls(
    monkeypatch,
) -> None:
    import local_server

    previous = GatewayConfig(
        provider="openai_compatible",
        base_url="https://old.example/v1",
        model="old-model",
    )
    marker = object()
    candidate_analyzer = type(
        "CandidateAnalyzer",
        (),
        {"gateway": object(), "timeout_seconds": 1.0},
    )()
    monkeypatch.setattr(local_server, "LLM_CONFIG", previous)
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", previous.timeout_seconds)
    monkeypatch.setattr(local_server, "LLM_CFG", previous.public_dict())
    monkeypatch.setattr(local_server.letters_adapter, "config", previous)
    monkeypatch.setattr(local_server.letters_adapter, "gateway", object())
    monkeypatch.setattr(
        local_server,
        "GatewayPrivateWorldCandidateAnalyzer",
        type(candidate_analyzer),
    )
    monkeypatch.setattr(
        local_server,
        "private_world_candidate_analyzer",
        candidate_analyzer,
    )
    monkeypatch.setattr(local_server.reply_engine, "timeout_seconds", 1.0)
    monkeypatch.setattr(local_server, "create_gateway", lambda *_args, **_kwargs: marker)
    monkeypatch.setattr(local_server.emotion_triage, "gateway", object())
    monkeypatch.delenv("OLIVIA_LLM_RUNTIME_KEY_CONFIGURED", raising=False)

    local_server.apply_runtime_llm_config(
        "https://gateway.example/v1",
        "deepseek-v4-flash",
        "synthetic-runtime-key",
    )

    assert local_server.LLM_CONFIG.timeout_seconds == 180.0
    assert local_server.LLM_TIMEOUT_SECONDS == 180.0
    assert candidate_analyzer.timeout_seconds == 180.0
    assert local_server.reply_engine.timeout_seconds == 180.0


def test_letter_gateway_preserves_scoped_text_call_without_changing_request_id() -> None:
    import local_server

    seen = []

    class RecordingGateway(Gateway):
        async def complete(self, messages, *, request_id=None):
            raise AssertionError("text Letter scope was dropped")

        async def complete_scoped(self, messages, *, request_id=None, scope):
            seen.append((tuple(messages), request_id, scope))
            return GatewayResponse("scoped reply", request_id or "", "mock", "mock")

    config = GatewayConfig(provider="mock", persona_v2_enabled=False)
    adapter = local_server.LetterAdapter(config)
    adapter.replace_runtime(config, RecordingGateway())
    bridge = local_server._LetterGateway(adapter)

    response = asyncio.run(
        bridge.complete_scoped(
            ({"role": "user", "content": "synthetic letter"},),
            request_id="letter-reply:stable",
            scope=GatewayRequestScope.TEXT_LETTER_MAX_REASONING,
        )
    )

    assert response.request_id == "letter-reply:stable"
    assert seen[0][1:] == (
        "letter-reply:stable",
        GatewayRequestScope.TEXT_LETTER_MAX_REASONING,
    )


def test_failed_llm_runtime_replacement_keeps_the_previous_gateway(monkeypatch) -> None:
    import local_server

    previous_gateway = object()
    monkeypatch.setattr(local_server.letters_adapter, "gateway", previous_gateway)
    monkeypatch.delenv("OLIVIA_LLM_RUNTIME_KEY_CONFIGURED", raising=False)

    def fail(_config, **_kwargs):
        raise RuntimeError("synthetic gateway failure")

    monkeypatch.setattr(local_server, "create_gateway", fail)

    try:
        local_server.apply_runtime_llm_config(
            "https://gateway.example/v1",
            "new-model",
            "replacement-key",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("runtime replacement failure must remain visible")

    assert local_server.letters_adapter.gateway is previous_gateway
    assert "OLIVIA_LLM_RUNTIME_KEY_CONFIGURED" not in local_server._os.environ


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


def test_health_exposes_enabled_persona_v2_without_network_probe() -> None:
    import local_server

    health = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "llm"}))
    data = health["data"]
    assert health["code"] == 0
    assert data["status"] in {"HEALTHY", "DEGRADED", "UNAVAILABLE"}
    assert data["providers"]["llm_gateway"]["network_called"] is False
    assert data["providers"]["llm_gateway"]["persona"] == {
        "status": "READY",
        "source": "persona_v2",
        "error_code": None,
    }
    assert "llm.gateway" in data["capabilities"]
